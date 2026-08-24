"""Тесты для Wave 1 dedup в MeetFlow webhook flow (P1 #7).

Проверяет:
  - handle_meeting_ended до P1 #7 обходил dedup; теперь должен skip
    дубль (вторая webhook'а с тем же external_id не запускает pipeline)
  - Dedup best-effort: если Supabase недоступен → processing продолжается
    (backward compat — не валим webhook'и из-за БД)
  - Pipeline всё ещё вызывается на свежем эпизоде
  - После успешного pipeline вызывается mark_completion в ledger
  - Webhook без external_id → fallback на content_hash дедуп

Подход: мокаем все тяжёлые зависимости (Supabase, run_meeting_pipeline,
membership). Тестируем оркестрацию dedup-гейтa в handle_meeting_ended.
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
import types
import uuid
from pathlib import Path
from typing import Any

import pytest

# Real-module bootstrap (для совместимости с другими suite-tests).
_REAL: dict[str, Any] = {}


def _force_load(name: str, rel_path: str):
    full = Path(__file__).resolve().parents[2] / rel_path
    spec = importlib.util.spec_from_file_location(name, full)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    _REAL[name] = mod
    return mod


if not getattr(sys.modules.get("backend.core.store"), "__file__", None):
    pkg = types.ModuleType("backend.core.store")
    pkg.__path__ = [str(Path(__file__).resolve().parents[2] / "backend" / "core" / "store")]
    sys.modules["backend.core.store"] = pkg
    _REAL["backend.core.store"] = pkg

_tp = _force_load("backend.core.store.tenant_paths", "backend/core/store/tenant_paths.py")

# meeting_pipeline тянет apscheduler в parent __init__ — но сам файл не тянет,
# поэтому импортируем через importlib напрямую.
_mp_path = Path(__file__).resolve().parents[2] / "backend" / "core" / "automations" / "meeting_pipeline.py"
_spec = importlib.util.spec_from_file_location(
    "backend.core.automations.meeting_pipeline", _mp_path
)
# Создаём parent-package stub (избегаем automations/__init__.py с apscheduler).
if "backend.core.automations" not in sys.modules:
    pkg = types.ModuleType("backend.core.automations")
    pkg.__path__ = [str(Path(__file__).resolve().parents[2] / "backend" / "core" / "automations")]
    sys.modules["backend.core.automations"] = pkg
meeting_pipeline = importlib.util.module_from_spec(_spec)
sys.modules["backend.core.automations.meeting_pipeline"] = meeting_pipeline
_spec.loader.exec_module(meeting_pipeline)


@pytest.fixture(autouse=True)
def _restore():
    for name, mod in _REAL.items():
        sys.modules[name] = mod
    yield


def _run(coro):
    return asyncio.run(coro)


def _uid() -> str:
    return str(uuid.uuid4())


class _FakeEvent:
    def __init__(self, payload: dict, user_id: str):
        self.payload = payload
        self.user_id = user_id


# === Fixtures для мокинга зависимостей =====================================


@pytest.fixture
def stub_pipeline(monkeypatch):
    """run_meeting_pipeline счётчик вызовов."""
    calls: list[dict] = []

    async def _fake_run(**kw):
        calls.append(kw)
        # Возвращаем псевдо-результат с нужными атрибутами для логирования.
        return types.SimpleNamespace(
            status="ok", tasks_processed=0, tasks_extracted=0,
        )

    monkeypatch.setattr(meeting_pipeline, "run_meeting_pipeline", _fake_run)
    return calls


@pytest.fixture
def stub_supabase(monkeypatch):
    """Подменяем supabase_client + ingest helpers для прямого контроля
    find_completed_episode / mark_completion.

    Замечание: `from backend.core.ingest import meeting_upsert` использует
    getattr на parent-package, не sys.modules-lookup. Поэтому нужно
    monkeypatch не только sys.modules, но и атрибут на родительском
    модуле. Иначе в bulk-run после теста, который загрузил реальные
    модули, наш stub просто не подхватывается.
    """

    state = {
        "existing": None,
        "mark_calls": [],
        "find_calls": [],
        "org_id_for": {},
    }

    # 1. Supabase client stub
    class _Sb:
        async def _request(self, *a, **kw):
            return []

    fake_sb_mod = types.ModuleType("backend.db.supabase_client")
    fake_sb_mod.get_supabase_client = lambda: _Sb()
    monkeypatch.setitem(sys.modules, "backend.db.supabase_client", fake_sb_mod)

    # 2. meeting_upsert stub — и в sys.modules, и в parent-attr
    fake_upsert = types.ModuleType("backend.core.ingest.meeting_upsert")
    fake_upsert.STATUS_COMPLETED = "completed"

    async def _find(sb, *, org_id, source, external_id, content_hash):
        state["find_calls"].append({
            "org_id": org_id, "source": source,
            "external_id": external_id, "content_hash": content_hash,
        })
        return state["existing"]

    async def _mark(sb, **kw):
        state["mark_calls"].append(kw)

    fake_upsert.find_completed_episode = _find
    fake_upsert.mark_completion = _mark
    monkeypatch.setitem(sys.modules, "backend.core.ingest.meeting_upsert", fake_upsert)

    # 3. membership stub
    fake_membership = types.ModuleType("backend.core.ingest.membership")

    def _get_org(user_id: str):
        return state["org_id_for"].get(user_id)

    fake_membership.get_org_for_user = _get_org
    monkeypatch.setitem(sys.modules, "backend.core.ingest.membership", fake_membership)

    # КРИТИЧНО: parent-package backend.core.ingest может уже иметь bound
    # атрибуты meeting_upsert и membership от предыдущих import'ов.
    # monkeypatch.setattr перебивает атрибуты parent-пакета на нужные.
    ingest_pkg = sys.modules.get("backend.core.ingest")
    if ingest_pkg is not None:
        monkeypatch.setattr(ingest_pkg, "meeting_upsert", fake_upsert, raising=False)
        monkeypatch.setattr(ingest_pkg, "membership", fake_membership, raising=False)

    # 4. Audit-log stub
    async def _noop_audit(*a, **kw):
        return None
    fake_audit = types.ModuleType("backend.core.observability")
    fake_audit.audit_log = types.SimpleNamespace(emit=_noop_audit)
    monkeypatch.setitem(sys.modules, "backend.core.observability", fake_audit)
    core_pkg = sys.modules.get("backend.core")
    if core_pkg is not None:
        monkeypatch.setattr(core_pkg, "observability", fake_audit, raising=False)

    return state


# === Тесты ==================================================================


def test_first_webhook_runs_pipeline_and_marks_ledger(stub_pipeline, stub_supabase):
    """Свежий эпизод: pipeline вызван, ledger помечен."""
    payload = {
        "meeting_id": "m1",
        "transcript": "Q4 review meeting",
        "provider": "zoom",
        "zoom_meeting_id": "zoom_123",
    }
    user = _uid()
    org = _uid()
    stub_supabase["org_id_for"][user] = org
    stub_supabase["existing"] = None  # нет в ledger

    _run(meeting_pipeline.handle_meeting_ended(_FakeEvent(payload, user)))

    # Pipeline вызван.
    assert len(stub_pipeline) == 1
    # find_completed_episode проверен с правильным ключом.
    assert len(stub_supabase["find_calls"]) == 1
    fc = stub_supabase["find_calls"][0]
    assert fc["org_id"] == org
    assert fc["source"] == "zoom"
    assert fc["external_id"] == "zoom_123"
    # mark_completion вызван после success.
    assert len(stub_supabase["mark_calls"]) == 1
    mc = stub_supabase["mark_calls"][0]
    assert mc["status"] == "completed"
    assert mc["meeting_id"] == "m1"


def test_duplicate_webhook_skipped_by_dedup(stub_pipeline, stub_supabase):
    """REGRESSION (главное P1 #7): вторая webhook с тем же external_id
    не запускает pipeline."""
    payload = {
        "meeting_id": "m_dup",
        "transcript": "same content",
        "provider": "zoom",
        "zoom_meeting_id": "zoom_dup_123",
    }
    user = _uid()
    org = _uid()
    stub_supabase["org_id_for"][user] = org
    # Ledger говорит — этот эпизод уже processed (от другого участника).
    stub_supabase["existing"] = {
        "meeting_id": "m_prior",
        "user_id": _uid(),
        "status": "completed",
    }

    _run(meeting_pipeline.handle_meeting_ended(_FakeEvent(payload, user)))

    # Pipeline НЕ вызван — skip
    assert len(stub_pipeline) == 0
    # mark не вызван (это reprocessing protection, не первая запись)
    assert len(stub_supabase["mark_calls"]) == 0


def test_dedup_falls_back_to_content_hash_no_external_id(stub_pipeline, stub_supabase):
    """Без external_id → дедуп по content_hash."""
    payload = {
        "meeting_id": "m2",
        "transcript": "Some long transcript text here",
        # нет provider/zoom_meeting_id/etc.
    }
    user = _uid()
    stub_supabase["existing"] = None

    _run(meeting_pipeline.handle_meeting_ended(_FakeEvent(payload, user)))

    assert len(stub_supabase["find_calls"]) == 1
    fc = stub_supabase["find_calls"][0]
    assert fc["external_id"] is None
    assert fc["source"] is None
    # content_hash должен быть задан (там есть transcript).
    assert fc["content_hash"] is not None
    assert len(fc["content_hash"]) == 64  # sha256 hex
    # Pipeline вызван (свежий эпизод).
    assert len(stub_pipeline) == 1


def test_supabase_unavailable_does_not_block_pipeline(
    stub_pipeline, stub_supabase, monkeypatch,
):
    """REGRESSION: если Supabase падает → pipeline всё равно идёт
    (backward compat — webhook не должен валиться из-за БД)."""
    payload = {
        "meeting_id": "m3", "transcript": "ok",
        "provider": "zoom", "zoom_meeting_id": "z3",
    }
    user = _uid()

    # Подменяем find_completed_episode чтоб бросал.
    async def _bad_find(*a, **kw):
        raise RuntimeError("postgres unreachable")
    fake_upsert = sys.modules["backend.core.ingest.meeting_upsert"]
    fake_upsert.find_completed_episode = _bad_find

    _run(meeting_pipeline.handle_meeting_ended(_FakeEvent(payload, user)))

    # Pipeline всё равно вызван — best-effort dedup
    assert len(stub_pipeline) == 1


def test_no_meeting_id_no_mark_ledger(stub_pipeline, stub_supabase):
    """Webhook без meeting_id — pipeline всё равно идёт (legacy),
    но в ledger ничего не пишем (нет на чём ставить unique)."""
    payload = {"transcript": "x"}  # без meeting_id
    user = _uid()
    stub_supabase["existing"] = None

    _run(meeting_pipeline.handle_meeting_ended(_FakeEvent(payload, user)))

    assert len(stub_pipeline) == 1
    # meeting_id == "unknown" → skip mark
    assert len(stub_supabase["mark_calls"]) == 0


def test_solo_user_no_org_dedup_by_content_hash_only(stub_pipeline, stub_supabase):
    """Solo user без org → org_id=None в find_call. Дедуп всё ещё работает
    через content_hash (UI: Mini Tess founder может через webhook
    повторить тот же транскрипт — не пере-обработаем)."""
    payload = {
        "meeting_id": "m_solo",
        "transcript": "personal voice memo",
        "provider": "telegram_voice_personal",
    }
    user = _uid()
    # Никакого org_id не выставлено → solo

    _run(meeting_pipeline.handle_meeting_ended(_FakeEvent(payload, user)))

    fc = stub_supabase["find_calls"][0]
    assert fc["org_id"] is None
    assert fc["source"] == "telegram_voice_personal"
    assert fc["content_hash"] is not None
    # Pipeline вызван (свежий).
    assert len(stub_pipeline) == 1
