"""Тесты safety-поллера синхронизации (досинк пропущенных webhook'ом встреч)."""
from __future__ import annotations

import asyncio
import sys
import types

import pytest


def _run(coro):
    return asyncio.run(coro)


def test_disabled_by_default(monkeypatch) -> None:
    from backend.core import knowledge_sync_poller as p
    monkeypatch.delenv("SYNC_POLLER", raising=False)
    assert p.poller_enabled() is False
    res = _run(p.poll_all_subscriptions())
    assert res == {"enabled": False, "polled": 0, "processed": 0, "users": 0}


def test_poll_passes_cost_cap(monkeypatch) -> None:
    """Поллер прокидывает max_process (cost-guard) в обработку подписки."""
    from backend.core import knowledge_sync_poller as p
    import backend.core.knowledge_sync as ks
    import backend.db.supabase_client as sc

    monkeypatch.setenv("SYNC_POLLER", "on")
    monkeypatch.setenv("SYNC_POLL_MAX_PER_RUN", "7")

    class FakeSB:
        async def _request(self, method, path, params=None):
            return [{"id": "s1", "user_id": "u1"}]

    monkeypatch.setattr(sc, "get_supabase_client", lambda: FakeSB())

    seen = {}

    async def fake_process(*, subscription_id, user_id, project_id=None,
                           folder_id=None, max_process=None):
        seen["max_process"] = max_process
        return {"processed": 7, "skipped": 3, "capped": True}

    monkeypatch.setattr(ks, "process_meetings_for_subscription", fake_process)
    monkeypatch.setitem(sys.modules, "backend.api.routes.chat",
                        types.SimpleNamespace(reload_graph_for_user=lambda uid: _noop()))

    res = _run(p.poll_all_subscriptions())
    assert seen["max_process"] == 7
    assert res["skipped"] == 3 and res["capped_subs"] == 1


async def _noop():
    return None


def test_enabled_flag(monkeypatch) -> None:
    from backend.core import knowledge_sync_poller as p
    monkeypatch.setenv("SYNC_POLLER", "on")
    assert p.poller_enabled() is True


def test_poll_iterates_and_aggregates(monkeypatch) -> None:
    from backend.core import knowledge_sync_poller as p
    import backend.core.knowledge_sync as ks
    import backend.db.supabase_client as sc

    monkeypatch.setenv("SYNC_POLLER", "on")

    class FakeSB:
        async def _request(self, method, path, params=None):
            assert params.get("status") == "eq.active"
            assert params.get("auto_process_new_meetings") == "eq.true"
            return [
                {"id": "s1", "user_id": "u1", "project_id": "p1"},
                {"id": "s2", "user_id": "u2", "folder_id": "f1"},
                {"id": "s3"},   # без user_id → пропуск
            ]

    monkeypatch.setattr(sc, "get_supabase_client", lambda: FakeSB())

    calls = []

    async def fake_process(*, subscription_id, user_id, project_id=None,
                           folder_id=None, max_process=None):
        calls.append(subscription_id)
        return {"processed": 2 if subscription_id == "s1" else 0}

    monkeypatch.setattr(ks, "process_meetings_for_subscription", fake_process)

    # фейковый chat-модуль, чтобы reload_graph_for_user не тянул routes.__init__
    fake_chat = types.ModuleType("backend.api.routes.chat")

    async def reload_graph_for_user(uid):
        return None

    fake_chat.reload_graph_for_user = reload_graph_for_user
    monkeypatch.setitem(sys.modules, "backend.api.routes.chat", fake_chat)

    res = _run(p.poll_all_subscriptions())
    assert res["enabled"] is True
    assert res["polled"] == 2          # s3 без user_id пропущен
    assert res["processed"] == 2       # только s1 дал новые
    assert res["users"] == 1
    assert calls == ["s1", "s2"]


def test_poll_supabase_failure_safe(monkeypatch) -> None:
    from backend.core import knowledge_sync_poller as p
    import backend.db.supabase_client as sc

    monkeypatch.setenv("SYNC_POLLER", "on")

    def boom():
        raise RuntimeError("no supabase")

    monkeypatch.setattr(sc, "get_supabase_client", boom)
    res = _run(p.poll_all_subscriptions())
    assert res["enabled"] is True and res["polled"] == 0 and res["processed"] == 0
