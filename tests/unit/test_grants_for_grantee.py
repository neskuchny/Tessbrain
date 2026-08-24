# -*- coding: utf-8 -*-
"""P1 мульти-аккаунта: обратный lookup «что расшарено мне» (fetch_grants_for_grantee)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import backend.core.auth.resource_permissions as rp


def _run(coro):
    # asyncio.run: свежий loop на вызов — соседние тест-файлы закрывают
    # общий event loop, и get_event_loop() тут получал бы закрытый.
    return asyncio.run(coro)


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows
        self.last_query = ""
        self.last_params = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, q, params=None):
        self.last_query = str(q)
        self.last_params = params or {}
        rows = [SimpleNamespace(_mapping=r) for r in self._rows]
        return SimpleNamespace(fetchall=lambda: rows)


def _wire(monkeypatch, rows):
    sess = _FakeSession(rows)

    class FakePG:
        def session(self):
            return sess

    async def fake_get_pg():
        return FakePG()
    import backend.db.postgres as pg_mod
    monkeypatch.setattr(pg_mod, "get_postgres", fake_get_pg)
    return sess


def test_returns_active_grants_and_filters_expired(monkeypatch):
    rows = [
        {"resource_type": "project", "resource_id": "p1",
         "permission_type": "read", "expires_at": None},
        {"resource_type": "project", "resource_id": "p2",
         "permission_type": "write", "expires_at": "2000-01-01T00:00:00Z"},  # истёк
    ]
    _wire(monkeypatch, rows)
    got = _run(rp.fetch_grants_for_grantee("u1", resource_type="project"))
    assert [g["resource_id"] for g in got] == ["p1"]  # истёкший отфильтрован


def test_resource_type_filter_in_query(monkeypatch):
    sess = _wire(monkeypatch, [])
    _run(rp.fetch_grants_for_grantee("u1", resource_type="project"))
    assert "resource_type = :rt" in sess.last_query
    assert sess.last_params.get("rt") == "project"
    assert sess.last_params.get("gid") == "u1"


def test_empty_grantee_and_no_postgres_are_safe(monkeypatch):
    assert _run(rp.fetch_grants_for_grantee("")) == []

    async def boom():
        raise RuntimeError("no pg")
    import backend.db.postgres as pg_mod
    monkeypatch.setattr(pg_mod, "get_postgres", boom)
    assert _run(rp.fetch_grants_for_grantee("u1")) == []  # best-effort → []
