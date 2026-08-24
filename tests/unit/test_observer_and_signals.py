# -*- coding: utf-8 -*-
"""Наблюдатель и очередь сигналов — два прод-бага одного лога.

1. enqueue_signal падал целиком: valid_until уходил ISO-строкой, а asyncpg
   требует datetime («invalid input for query argument $11 … got 'str'») —
   сигнал наблюдателя не вставал в очередь доставки вовсе.
2. Карточка наблюдения кэширует board_id и уверяла «доска создана —
   найдёте в „Моих досках“», не проверяя, что доска ещё существует.
"""
import asyncio
import datetime as dt
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def test_enqueue_signal_passes_datetime(monkeypatch):
    from backend.core.reactive import signal_queue as sq

    captured = {}

    class _Session:
        async def execute(self, stmt, params):
            captured.update(params)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _PG:
        def session(self, apply_tenant=False):
            return _Session()

    async def _get_pg():
        return _PG()

    monkeypatch.setattr("backend.db.postgres.get_postgres", _get_pg)
    sid = asyncio.run(sq.enqueue_signal(
        user_id="u1", signal_type="observer_hook", title="👁 Рутина",
        valid_until_minutes=1440))
    assert sid, "сигнал поставлен"
    vu = captured["vu"]
    assert isinstance(vu, dt.datetime), \
        f"valid_until обязан быть datetime, не {type(vu).__name__}"
    assert vu.tzinfo is not None, "aware datetime (timestamptz)"


def test_enqueue_signal_without_ttl(monkeypatch):
    from backend.core.reactive import signal_queue as sq

    captured = {}

    class _Session:
        async def execute(self, stmt, params):
            captured.update(params)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _PG:
        def session(self, apply_tenant=False):
            return _Session()

    async def _get_pg():
        return _PG()

    monkeypatch.setattr("backend.db.postgres.get_postgres", _get_pg)
    sid = asyncio.run(sq.enqueue_signal(
        user_id="u1", signal_type="observer_hook", title="без TTL"))
    assert sid and captured["vu"] is None


def test_observer_cached_board_verified(monkeypatch):
    """Доска исчезла из board_workflows → кэш сбрасывается и идёт
    пересоздание, а не уверенное «найдёте в Моих досках»."""
    from backend.core.observer import agent as oa

    calls = {"reset": [], "design": 0}

    monkeypatch.setattr(
        "backend.core.observer.state.get_observation",
        lambda uid, oid: {"id": oid, "front_id": "routine_to_automate",
                          "hook": "рутина", "board_id": "board_gone",
                          "board_name": "👁 Автоматизация"})
    monkeypatch.setattr(
        "backend.core.observer.state.set_board",
        lambda uid, oid, bid, name: calls["reset"].append((bid, name)))

    class _SB:
        async def _request(self, method, path, params=None, **kw):
            return []  # доски больше нет

    monkeypatch.setattr("backend.db.supabase_client.get_supabase_client",
                        lambda: _SB())

    async def _design(uid, text):
        calls["design"] += 1
        return {"success": False, "error": "стоп для теста"}

    monkeypatch.setattr("backend.core.board.nl_designer.design_process",
                        _design)
    out = asyncio.run(oa.propose_board("u1", "obs-1"))
    assert calls["reset"] and calls["reset"][0] == ("", ""), \
        "протухший board_id сброшен"
    assert calls["design"] == 1, "пошло пересоздание"
    assert out["status"] == "design_failed", "тест останавливает на дизайне"


def test_observer_cached_board_alive_returns_cached(monkeypatch):
    from backend.core.observer import agent as oa

    monkeypatch.setattr(
        "backend.core.observer.state.get_observation",
        lambda uid, oid: {"id": oid, "board_id": "board_alive",
                          "board_name": "👁 Жива"})

    class _SB:
        async def _request(self, method, path, params=None, **kw):
            return [{"id": "board_alive"}]

    monkeypatch.setattr("backend.db.supabase_client.get_supabase_client",
                        lambda: _SB())
    out = asyncio.run(oa.propose_board("u1", "obs-1"))
    assert out == {"status": "ok", "board_id": "board_alive",
                   "board_name": "👁 Жива", "cached": True}
