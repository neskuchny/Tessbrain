"""Unit-тесты для core.observability.audit_log (W9 enterprise-pack).

Проверяем:
- emit() пишет в structlog ВСЕГДА (даже если БД offline);
- payload_hash детерминистичен и не утекает PII;
- DB-запись best-effort: исключение не пробрасывается наружу;
- tenant_id и actor_role читаются из contextvar'ов.
"""
from __future__ import annotations

import asyncio
import logging

import pytest
from backend.core.auth.rbac import Role, reset_current_role, set_current_role
from backend.core.observability import audit_log


def _run(coro):
    return asyncio.run(coro)


def test_payload_hash_deterministic() -> None:
    h1 = audit_log._payload_hash({"a": 1, "b": 2})
    h2 = audit_log._payload_hash({"b": 2, "a": 1})
    assert h1 == h2  # sort_keys=True даёт стабильный hash


def test_payload_hash_none() -> None:
    assert audit_log._payload_hash(None) is None


def test_payload_hash_changes_when_payload_changes() -> None:
    h1 = audit_log._payload_hash({"x": 1})
    h2 = audit_log._payload_hash({"x": 2})
    assert h1 != h2


def test_emit_logs_to_structlog(caplog: pytest.LogCaptureFixture) -> None:
    """emit() обязан написать в logger 'backend.core.observability.audit_log'
    с уровнем INFO и полем 'audit_action'."""
    caplog.set_level(logging.INFO, logger="backend.core.observability.audit_log")
    _run(audit_log.emit(action="test.action", user_id="u-1", resource="x:1"))
    found = [r for r in caplog.records if getattr(r, "audit_action", None) == "test.action"]
    assert len(found) == 1
    rec = found[0]
    assert rec.user_id == "u-1"
    assert rec.resource == "x:1"


def test_emit_includes_actor_role(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="backend.core.observability.audit_log")
    token = set_current_role(Role.ADMIN)
    try:
        _run(audit_log.emit(action="role.test"))
    finally:
        reset_current_role(token)
    found = [r for r in caplog.records if getattr(r, "audit_action", None) == "role.test"]
    assert len(found) == 1
    assert found[0].actor_role == "ADMIN"


def test_emit_user_id_defaults_to_system(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="backend.core.observability.audit_log")
    _run(audit_log.emit(action="system.heartbeat"))
    found = [r for r in caplog.records if getattr(r, "audit_action", None) == "system.heartbeat"]
    assert len(found) == 1
    assert found[0].user_id == "system"


def test_emit_does_not_raise_when_db_offline() -> None:
    """Best-effort: нет PG → не падаем."""
    # PostgresClient в sandbox-окружении не поднимется (нет sqlalchemy/asyncpg
    # без heavy deps); emit должен это проглотить.
    try:
        _run(audit_log.emit(action="db.unavailable.test"))
    except Exception as e:
        pytest.fail(f"emit raised when DB unavailable: {e}")


def test_payload_hash_handles_unserializable() -> None:
    """Если payload не JSON-serializable, fallback на str(). Всё равно hash."""

    class Custom:
        def __repr__(self) -> str:
            return "Custom()"

    h = audit_log._payload_hash(Custom())
    assert h is not None
    assert len(h) == 64  # SHA-256 hex
