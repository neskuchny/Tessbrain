# -*- coding: utf-8 -*-
"""P1 мульти-аккаунта: write-грант на PATCH проекта и read-грант на артефакт
(иначе «правка» из «Поделиться» была бы пустой). Тестируем лёгкий хелпер
core/auth/shared_access (роуты — тонкие обёртки над ним)."""
from __future__ import annotations

import asyncio

import backend.core.auth.shared_access as sa


def _run(coro):
    return asyncio.run(coro)


class FakeDB:
    def __init__(self, owner_id: str):
        self.owner = owner_id
        self.updated_as: list[str] = []
        self.artifacts = {"a1": {"id": "a1", "userId": owner_id, "name": "Куб"}}

    async def get_project(self, pid, uid=None):
        proj = {"id": pid, "userId": self.owner, "name": "P"}
        if uid and uid != self.owner:
            return None
        return proj

    async def update_project(self, pid, uid, data):
        if uid != self.owner:
            return None
        self.updated_as.append(uid)
        return {"id": pid, "userId": self.owner, **data}

    async def get_artifact(self, aid, uid=None):
        a = self.artifacts.get(aid)
        if not a:
            return None
        if uid and uid != a["userId"]:
            return None
        return dict(a)


def _wire_check(monkeypatch, *, allowed: bool):
    import backend.core.auth.resource_permissions as rp

    async def fake_check(user_id, required, **kw):
        return {"allowed": allowed, "level": required if allowed else None,
                "via": "grant" if allowed else None}
    monkeypatch.setattr(rp, "check", fake_check)


def test_owner_updates_directly(monkeypatch):
    db = FakeDB("owner-1")
    _wire_check(monkeypatch, allowed=False)
    res = _run(sa.update_project_with_grant(db, "owner-1", "p1", {"name": "X"}))
    assert res.get("name") == "X" and "error" not in res
    assert db.updated_as == ["owner-1"]


def test_write_grantee_updates_via_owner(monkeypatch):
    db = FakeDB("owner-1")
    _wire_check(monkeypatch, allowed=True)
    res = _run(sa.update_project_with_grant(db, "colleague-2", "p1", {"name": "Y"}))
    assert res.get("name") == "Y"
    assert res.get("access_via") == "grant"
    assert db.updated_as == ["owner-1"]  # запись от имени владельца


def test_stranger_without_grant_denied(monkeypatch):
    db = FakeDB("owner-1")
    _wire_check(monkeypatch, allowed=False)
    res = _run(sa.update_project_with_grant(db, "stranger-3", "p1", {"name": "Z"}))
    assert res == {"error": "Project not found"}
    assert db.updated_as == []


def test_artifact_read_grant_fallback(monkeypatch):
    db = FakeDB("owner-1")
    _wire_check(monkeypatch, allowed=True)
    art = _run(sa.artifact_with_grant(db, "colleague-2", "a1"))
    assert art is not None and art["access_via"] == "grant"


def test_artifact_owner_direct_and_stranger_denied(monkeypatch):
    db = FakeDB("owner-1")
    _wire_check(monkeypatch, allowed=False)
    assert _run(sa.artifact_with_grant(db, "owner-1", "a1"))["name"] == "Куб"
    assert _run(sa.artifact_with_grant(db, "stranger-3", "a1")) is None
    assert _run(sa.artifact_with_grant(db, "owner-1", "missing")) is None
