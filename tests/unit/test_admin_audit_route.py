"""Тесты для GET /admin/audit-log (P2 #13).

Через прямой вызов handler.fn (как orgs_routes), мокая Postgres.
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

import jwt
import pytest
from litestar.exceptions import HTTPException

from backend.core.auth.rbac import PermissionDenied

# Загрузка admin_audit напрямую (минуя routes/__init__ с autogen-issue).
if "backend.api" not in sys.modules:
    pkg = types.ModuleType("backend.api")
    pkg.__path__ = [str(Path(__file__).resolve().parents[2] / "backend" / "api")]
    sys.modules["backend.api"] = pkg
if "backend.api.routes" not in sys.modules:
    pkg = types.ModuleType("backend.api.routes")
    pkg.__path__ = [str(Path(__file__).resolve().parents[2] / "backend" / "api" / "routes")]
    sys.modules["backend.api.routes"] = pkg

_audit_path = Path(__file__).resolve().parents[2] / "backend" / "api" / "routes" / "admin_audit.py"
_spec = importlib.util.spec_from_file_location("backend.api.routes.admin_audit", _audit_path)
admin_audit = importlib.util.module_from_spec(_spec)
sys.modules["backend.api.routes.admin_audit"] = admin_audit
_spec.loader.exec_module(admin_audit)


async def _call(handler, **kwargs):
    """Прямой вызов Litestar route handler.

    Litestar `Parameter(query=..., default=X)` создаёт ParameterKwarg —
    при request-parsing он резолвится в X, но при прямом вызове `fn(...)`
    Python видит objects ParameterKwarg как default. Чтобы handler-код
    работал — подставляем нужный default из ParameterKwarg.default
    (или None если не указан).
    """
    import inspect
    fn = getattr(handler, "fn", handler)
    sig = inspect.signature(fn)
    for name, param in sig.parameters.items():
        if name in kwargs:
            continue
        d = param.default
        if d is inspect.Parameter.empty:
            continue
        # Litestar ParameterKwarg хранит реальный default в .default
        real = getattr(d, "default", d)
        kwargs[name] = real
    return await fn(**kwargs)


def _token(user_id: str, *, role: str | None = None) -> str:
    payload: dict[str, Any] = {"sub": user_id}
    if role:
        payload["role"] = role
    return "Bearer " + jwt.encode(payload, "test-secret", algorithm="HS256")


def _uid() -> str:
    return str(uuid.uuid4())


# === Mock Postgres for query =================================================


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows
        self.executed_queries: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, query, params=None):
        self.executed_queries.append((str(query), params or {}))
        return _FakeResult(self._rows)


class _FakePg:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.last_session: _FakeSession | None = None

    def session(self, apply_tenant=False):
        self.last_session = _FakeSession(self.rows)
        return self.last_session


@pytest.fixture
def fake_pg(monkeypatch):
    """Подменить get_postgres на FakePg."""
    pg = _FakePg()

    async def _get():
        return pg

    fake_pg_mod = types.ModuleType("backend.db.postgres")
    fake_pg_mod.get_postgres = _get
    monkeypatch.setitem(sys.modules, "backend.db.postgres", fake_pg_mod)
    # Parent-attr подмена
    pkg = sys.modules.get("backend.db")
    if pkg is not None:
        monkeypatch.setattr(pkg, "postgres", fake_pg_mod, raising=False)
    return pg


@pytest.fixture(autouse=True)
def _stub_tenant_context(monkeypatch):
    """get_current_tenant возвращает фиксированный tenant для default-case."""
    fake_ctx = types.ModuleType("backend.core.observability.tenant_context")
    fake_ctx.get_current_tenant = lambda: "ctx-tenant-default"
    monkeypatch.setitem(sys.modules, "backend.core.observability.tenant_context", fake_ctx)


# === Tests ===================================================================


async def test_audit_log_requires_admin_role(fake_pg):
    """VIEWER role → PermissionDenied."""
    with pytest.raises(PermissionDenied):
        await _call(admin_audit.list_audit_events, authorization=_token(_uid()))


async def test_audit_log_unauthenticated_401(fake_pg):
    with pytest.raises(HTTPException) as exc:
        await _call(admin_audit.list_audit_events, authorization=None)
    assert exc.value.status_code == 401


async def test_audit_log_returns_events(fake_pg):
    from datetime import datetime, timezone
    fake_pg.rows = [
        (1, datetime(2026, 6, 20, 10, 0, tzinfo=timezone.utc),
         "gdpr.delete", "alice-uid", "ADMIN", "user:alice-uid",
         None, None, None, "abc123", {"reason": "self"}),
        (2, datetime(2026, 6, 20, 9, 0, tzinfo=timezone.utc),
         "orgs.create", "founder-uid", "OWNER", "org:acme",
         None, None, None, "def456", {"name": "Acme"}),
    ]
    result = await _call(
        admin_audit.list_audit_events,
        authorization=_token(_uid(), role="admin"),
    )
    assert result["tenant_id"] == "ctx-tenant-default"
    assert result["count"] == 2
    actions = [e["action"] for e in result["events"]]
    assert "gdpr.delete" in actions
    assert "orgs.create" in actions


async def test_audit_log_uses_ctx_tenant_when_not_specified(fake_pg):
    await _call(admin_audit.list_audit_events, authorization=_token(_uid(), role="admin"))
    q, params = fake_pg.last_session.executed_queries[0]
    assert params["tenant_id"] == "ctx-tenant-default"


async def test_audit_log_owner_can_query_another_tenant(fake_pg):
    other = "other-tenant-id"
    await _call(
        admin_audit.list_audit_events,
        authorization=_token(_uid(), role="owner"),
        tenant_id=other,
    )
    q, params = fake_pg.last_session.executed_queries[0]
    assert params["tenant_id"] == other


async def test_audit_log_admin_cannot_query_another_tenant(fake_pg):
    """ADMIN < OWNER → forbidden to cross-tenant read."""
    with pytest.raises(HTTPException) as exc:
        await _call(
            admin_audit.list_audit_events,
            authorization=_token(_uid(), role="admin"),
            tenant_id="other-tenant",
        )
    assert exc.value.status_code == 403


async def test_audit_log_filter_by_action(fake_pg):
    await _call(
        admin_audit.list_audit_events,
        authorization=_token(_uid(), role="admin"),
        action="gdpr.delete",
    )
    q, params = fake_pg.last_session.executed_queries[0]
    assert "action = :action" in q
    assert params["action"] == "gdpr.delete"


async def test_audit_log_filter_by_action_wildcard(fake_pg):
    await _call(
        admin_audit.list_audit_events,
        authorization=_token(_uid(), role="admin"),
        action="gdpr.*",
    )
    q, params = fake_pg.last_session.executed_queries[0]
    assert "action LIKE :action_pat" in q
    assert params["action_pat"] == "gdpr.%"


async def test_audit_log_filter_by_user_id(fake_pg):
    await _call(
        admin_audit.list_audit_events,
        authorization=_token(_uid(), role="admin"),
        user_id="alice-uid",
    )
    q, params = fake_pg.last_session.executed_queries[0]
    assert "user_id = :user_id" in q
    assert params["user_id"] == "alice-uid"


async def test_audit_log_filter_by_time_range(fake_pg):
    await _call(
        admin_audit.list_audit_events,
        authorization=_token(_uid(), role="admin"),
        since="2026-06-01",
        until="2026-06-30",
    )
    q, params = fake_pg.last_session.executed_queries[0]
    assert params["since"] == "2026-06-01"
    assert params["until"] == "2026-06-30"


async def test_audit_log_limit_capped_at_1000(fake_pg):
    await _call(
        admin_audit.list_audit_events,
        authorization=_token(_uid(), role="admin"),
        limit=99999,
    )
    q, params = fake_pg.last_session.executed_queries[0]
    assert params["limit"] == 1000


async def test_audit_log_pagination(fake_pg):
    await _call(
        admin_audit.list_audit_events,
        authorization=_token(_uid(), role="admin"),
        limit=50, offset=100,
    )
    q, params = fake_pg.last_session.executed_queries[0]
    assert params["limit"] == 50
    assert params["offset"] == 100


async def test_audit_log_db_unavailable_503(monkeypatch):
    """Если Postgres не подключается — 503."""
    async def _bad():
        raise RuntimeError("connection refused")
    fake_pg_mod = types.ModuleType("backend.db.postgres")
    fake_pg_mod.get_postgres = _bad
    monkeypatch.setitem(sys.modules, "backend.db.postgres", fake_pg_mod)

    with pytest.raises(HTTPException) as exc:
        await _call(admin_audit.list_audit_events, authorization=_token(_uid(), role="admin"))
    assert exc.value.status_code == 503
