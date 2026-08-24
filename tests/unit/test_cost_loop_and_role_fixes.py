# -*- coding: utf-8 -*-
"""Фиксы runaway-расхода и отката ролей (2026-07-04).

1. processed_meetings: CHECK в Supabase знал только старый словарь статусов →
   'no_transcript' отбивался (23514), строка не писалась, встреча вечно
   переобрабатывалась. Тест: legacy-fallback в mark_completion.
2. EntityResolver: LLM-вердикты пар («Олег»=«Олежа»?) кешируются процессно и
   на диске (раньше кеш умирал с инстансом = с каждой встречей) + кап новых
   сверок за прогон.
3. EnhancedSnapshotGenerator.set_storage_path: перечитывает per-user
   оверрайды (раньше правка CEO терялась после рестарта/в worker-процессе).
"""
from __future__ import annotations

import asyncio
import json

import pytest

import backend.core.ingest.meeting_upsert as upsert
from backend.core.store.entity_resolver import EntityResolver


# ── 1. mark_completion: legacy-fallback при 23514 ──────────────────────────

class _FakeSupabase:
    """PATCH/POST c программируемым поведением; пишет все вызовы в journal."""

    def __init__(self, allowed_statuses):
        self.allowed = set(allowed_statuses)
        self.journal = []  # (method, status, json_data)
        self.rows_exist = False  # PATCH находит строку?

    async def _request(self, method, path, params=None, json_data=None,
                       headers=None):
        status = (json_data or {}).get("status")
        self.journal.append((method, status, dict(json_data or {})))
        if status is not None and status not in self.allowed:
            raise RuntimeError(
                "Client error '400 Bad Request': code 23514 check constraint")
        if method == "PATCH":
            return [{"id": "row1"}] if self.rows_exist else []
        return [{"id": "row1"}]


_LEGACY_ALLOWED = {"pending", "processing", "completed", "failed"}


def _mark(supabase, status):
    asyncio.run(upsert.mark_completion(
        supabase, user_id="u1", meeting_id="m1", org_id=None, source=None,
        external_id=None, content_hash=None, subscription_id=None,
        status=status))


def test_no_transcript_falls_back_to_legacy_status():
    sb = _FakeSupabase(_LEGACY_ALLOWED)
    _mark(sb, upsert.STATUS_NO_TRANSCRIPT)
    posts = [(m, s, j) for m, s, j in sb.journal if m == "POST"]
    # Первый POST с no_transcript отбит, ретрай ушёл с 'completed'
    assert posts[-1][1] == "completed"
    assert posts[-1][2].get("last_error") == "no_transcript"
    # Терминальность сохранена: 'completed' входит в TERMINAL_OK → skip в поллере
    assert posts[-1][1] in upsert.TERMINAL_OK


def test_allowed_status_posts_once():
    sb = _FakeSupabase(_LEGACY_ALLOWED)
    _mark(sb, upsert.STATUS_COMPLETED)
    posts = [j for m, s, j in sb.journal if m == "POST"]
    assert len(posts) == 1


def test_patch_check_violation_retries_legacy_patch():
    sb = _FakeSupabase(_LEGACY_ALLOWED)
    sb.rows_exist = True
    _mark(sb, upsert.STATUS_NO_TRANSCRIPT)
    patches = [(s, j) for m, s, j in sb.journal if m == "PATCH"]
    assert patches[-1][0] == "completed"
    # После успешного legacy-PATCH POST не нужен
    assert not [1 for m, _, _ in sb.journal if m == "POST"]


def test_new_check_passes_native_statuses():
    # После миграции 267 нативные статусы проходят без ретрая
    sb = _FakeSupabase(_LEGACY_ALLOWED | {"running", "no_transcript"})
    _mark(sb, upsert.STATUS_NO_TRANSCRIPT)
    posts = [(s, j) for m, s, j in sb.journal if m == "POST"]
    assert len(posts) == 1 and posts[0][0] == "no_transcript"


def test_legacy_payload_maps_unknown_to_failed():
    out = upsert._legacy_payload({"status": "error: boom"})
    assert out["status"] == "failed"
    assert "error: boom" in out["last_error"]


# ── 2. EntityResolver: персистентный кеш вердиктов + кап ───────────────────

class _CountingLLM:
    def __init__(self):
        self.calls = 0

    async def generate(self, prompt=None, temperature=None, max_tokens=None):
        self.calls += 1
        return {"text": json.dumps(
            {"is_same": True, "confidence": 0.9, "reasoning": "diminutive"})}


@pytest.fixture()
def resolver_env(tmp_path, monkeypatch):
    monkeypatch.setattr(EntityResolver, "_llm_verdicts_shared", None)
    monkeypatch.setattr(EntityResolver, "_llm_verdicts_path",
                        tmp_path / "llm_verdicts.json")
    llm = _CountingLLM()
    monkeypatch.setattr(EntityResolver, "_llm_client", llm)
    return llm


def test_llm_verdict_cached_across_instances_and_restarts(resolver_env):
    llm = resolver_env
    r1 = EntityResolver()
    is_same, conf, _ = asyncio.run(
        r1.check_same_entity_llm("Олег", "Олежа", "person"))
    assert is_same and llm.calls == 1

    # Новый инстанс (= следующая встреча) — вердикт из процессного кеша
    r2 = EntityResolver()
    asyncio.run(r2.check_same_entity_llm("Олег", "Олежа", "person"))
    assert llm.calls == 1

    # «Рестарт процесса»: сбрасываем процессный кеш → читается с диска
    EntityResolver._llm_verdicts_shared = None
    r3 = EntityResolver()
    asyncio.run(r3.check_same_entity_llm("Олежа", "Олег", "person"))
    assert llm.calls == 1


def test_llm_new_checks_capped(resolver_env):
    llm = resolver_env
    r = EntityResolver()
    r.llm_max_new_checks = 1
    asyncio.run(r.check_same_entity_llm("Аня", "Анна", "person"))
    is_same, _, reason = asyncio.run(
        r.check_same_entity_llm("Петя", "Пётр", "person"))
    assert llm.calls == 1
    assert reason == "llm_budget_exhausted"
    assert is_same is False  # консервативно: не сливаем без вердикта


# ── 3. Снапшоты: per-user оверрайды переживают смену пути/рестарт ──────────

def test_set_storage_path_reloads_tenant_overrides(tmp_path):
    from backend.core.sleep.enhanced_snapshot import EnhancedSnapshotGenerator

    gen = EnhancedSnapshotGenerator(storage_path=str(tmp_path / "default"))
    assert gen._company_overrides == {}

    user_dir = tmp_path / "user1"
    user_dir.mkdir()
    (user_dir / "company_overrides.json").write_text(
        json.dumps({"founder": {"name": "Антон"}}), encoding="utf-8")

    # Как в фабрике/nightly: переключение на per-user путь
    gen.set_storage_path(user_dir)
    assert gen._company_overrides.get("founder", {}).get("name") == "Антон"

    # Смена тенанта без своего файла: чужие оверрайды НЕ протекают
    gen.set_storage_path(tmp_path / "user2")
    assert gen._company_overrides == {}
