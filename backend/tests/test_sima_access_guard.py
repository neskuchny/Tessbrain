"""Регрессия анти-IDOR SIMA (Фаза 0.2): мутации/экспорт/kanon недоступны
чужому пользователю. Проверяем логику _require_project_access(write=…) на
моках БД — без реального Postgres."""
import asyncio
import sys
import types
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, ".")

from backend.api.routes import sima  # noqa: E402


class _FakeDB:
    """get_project(pid, uid) → проект только если uid == владелец."""

    def __init__(self, owner_uid: str):
        self.owner = owner_uid

    async def get_project(self, pid, uid=None):
        if uid is None:  # запрос владельца для check-гранта
            return {"id": pid, "userId": self.owner}
        return {"id": pid} if uid == self.owner else None


def _req(uid: str):
    return types.SimpleNamespace(_uid=uid)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_owner_passes_read_and_write():
    db = _FakeDB("owner-1")
    import backend.db.sima_client as _sc
    with patch.object(_sc, "get_sima_db", AsyncMock(return_value=db)), \
         patch.object(sima, "get_user_id_from_request", lambda r: "owner-1"):
        assert _run(sima._require_project_access(_req("owner-1"), "p1")) == "owner-1"
        assert _run(sima._require_project_access(_req("owner-1"), "p1", write=True)) == "owner-1"


def test_stranger_denied_write_without_grant():
    db = _FakeDB("owner-1")
    # чужой без гранта: check() запрещает
    fake_perm = types.ModuleType("backend.core.auth.resource_permissions")
    fake_perm.check = AsyncMock(return_value=types.SimpleNamespace(allowed=False))
    import backend.db.sima_client as _sc
    with patch.object(_sc, "get_sima_db", AsyncMock(return_value=db)), \
         patch.object(sima, "get_user_id_from_request", lambda r: "stranger"), \
         patch.dict(sys.modules, {"backend.core.auth.resource_permissions": fake_perm}):
        assert _run(sima._require_project_access(_req("stranger"), "p1", write=True)) is None
        assert _run(sima._require_project_access(_req("stranger"), "p1")) is None


def test_stranger_with_read_grant_cannot_write():
    """Read-грант пускает на чтение, но НЕ на мутацию (write-режим строже)."""
    db = _FakeDB("owner-1")

    async def _check(uid, action, **kw):
        # грант только на read
        return types.SimpleNamespace(allowed=(action == "read"))

    fake_perm = types.ModuleType("backend.core.auth.resource_permissions")
    fake_perm.check = _check
    import backend.db.sima_client as _sc
    with patch.object(_sc, "get_sima_db", AsyncMock(return_value=db)), \
         patch.object(sima, "get_user_id_from_request", lambda r: "reader"), \
         patch.dict(sys.modules, {"backend.core.auth.resource_permissions": fake_perm}):
        assert _run(sima._require_project_access(_req("reader"), "p1")) == "reader"
        assert _run(sima._require_project_access(_req("reader"), "p1", write=True)) is None


def test_empty_project_id_denied():
    assert _run(sima._require_project_access(_req("x"), "")) is None
    assert _run(sima._require_project_access(_req("x"), "", write=True)) is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
