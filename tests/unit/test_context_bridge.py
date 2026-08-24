# -*- coding: utf-8 -*-
"""Подключаемый контекст чата: переписки (в т.ч. «только контекст»), CRM,
задачи. Инвариант приватности: context_only-источник доступен в контексте
чата, но digest в мозг для него отказывает, а автоцикл его не выгружает."""
import asyncio
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.core.messengers import chat_ingest as ci  # noqa: E402
from backend.core.messengers import context_bridge as cb  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(ci, "_store_dir", lambda: tmp_path)
    monkeypatch.setenv("ENABLE_CHAT_INGEST", "1")
    yield tmp_path


def _mk_source(key_chat="-9", title="Чат с партнёрами"):
    key = ci.source_key("telegram", key_chat)
    ci._register_source("u1", platform="telegram", chat_id=key_chat,
                        title=title, enabled=True)
    ci.append_messages("u1", key, [
        {"ts": "2026-07-28T10:00:00+00:00", "author": "Андрей",
         "author_id": "777", "text": "white-label интересен, но цену обсудим"},
    ])
    return key


def test_wants_extra_context():
    assert not cb.wants_extra_context(None)
    assert not cb.wants_extra_context({"meeting_filter": ["m1"]})
    assert cb.wants_extra_context({"chat_source_keys": ["telegram:-9"]})
    assert cb.wants_extra_context({"include_crm": True})
    assert cb.wants_extra_context({"include_tasks": True})


def test_chat_source_block_with_confirmed_identity(monkeypatch):
    key = _mk_source()
    ci.confirm_participant("u1", key, "777", "p_andrey", "Андрей Соколов")
    out = asyncio.run(cb.build_extra_context(
        "u1", {"chat_source_keys": [key]}))
    assert "ПЕРЕПИСКА «Чат с партнёрами»" in out
    assert "white-label интересен" in out
    assert "(= Андрей Соколов)" in out, "подтверждённая сшивка аннотируется"
    assert "ПОДКЛЮЧЁННЫЙ КОНТЕКСТ" in out


def test_context_only_source_available_in_chat_but_not_in_brain(monkeypatch):
    """Главный инвариант разделения: «поговорить можно — в мозг нельзя»."""
    key = _mk_source()
    assert ci.set_source_mode("u1", key, "context_only")

    # в контекст чата — доступен
    out = asyncio.run(cb.build_extra_context(
        "u1", {"chat_source_keys": [key]}))
    assert "white-label" in out

    # digest в мозг — отказ
    res = asyncio.run(ci.digest_to_brain("u1", key))
    assert res["status"] == "error" and "только контекст" in res["message"]

    # автоцикл — анализ можно, выгрузка в мозг не происходит
    class _LLM:
        async def generate_json(self, prompt="", temperature=0.2):
            return {"agreements": [], "tasks": [], "client_facts": [],
                    "decisions": []}

    monkeypatch.setattr("backend.core.llm.router.get_llm_router",
                        lambda: _LLM())
    called = {"digest": 0}

    async def _upsert(user_id, *, title, content):
        called["digest"] += 1
        return {"id": "d1", "updated": False}

    monkeypatch.setattr(
        "backend.core.marketing.chat_context.upsert_kb_document", _upsert)
    stats = asyncio.run(ci.auto_cycle("u1"))
    assert stats["digested"] == 0 and called["digest"] == 0, \
        "context_only никогда не уезжает в KB"


def test_crm_block_from_real_clients(monkeypatch):
    async def _clients(uid):
        return {"clients": [
            {"id": "c1", "name": "ООО Ромашка", "industry": "ритейл",
             "status": "переговоры"}], "segments": []}

    monkeypatch.setattr(
        "backend.core.marketing.client_sim.list_clients", _clients)
    out = asyncio.run(cb.build_extra_context("u1", {"include_crm": True}))
    assert "ДАННЫЕ CRM" in out and "ООО Ромашка" in out
    assert "переговоры" in out


def test_tasks_block_from_ledger(monkeypatch):
    async def _ledger(uid, emit_events=True):
        assert emit_events is False, "контекст чата не должен эмитить события"
        return {"tasks": [
            {"title": "Позвонить поставщику", "assignee": "Иван",
             "_pulse": {"bucket": "todo", "deadline": "2026-08-01"}},
            {"title": "Готово", "_pulse": {"bucket": "done"}},
        ]}

    monkeypatch.setattr("backend.core.pulse.ledger.build_ledger", _ledger)
    out = asyncio.run(cb.build_extra_context("u1", {"include_tasks": True}))
    assert "открыто 1, закрыто 1" in out
    assert "Позвонить поставщику" in out and "исп: Иван" in out


def test_empty_sources_are_honest(monkeypatch):
    async def _clients(uid):
        return {"clients": [], "segments": []}

    async def _ledger(uid, emit_events=True):
        return {"tasks": []}

    monkeypatch.setattr(
        "backend.core.marketing.client_sim.list_clients", _clients)
    monkeypatch.setattr("backend.core.pulse.ledger.build_ledger", _ledger)
    out = asyncio.run(cb.build_extra_context(
        "u1", {"include_crm": True, "include_tasks": True}))
    assert "пока нет" in out and "задач в системе нет" in out, \
        "пустой источник говорит «пусто», а не молчит"
