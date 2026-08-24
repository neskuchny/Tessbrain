"""Endpoint-тесты для backend/api/routes/orgs.py.

Вместо запуска полного ASGI-стека вызываем handler-функции напрямую с
сгенерированными JWT-токенами разных ролей. Litestar handler оборачивает
async-функцию — достаём оригинал через handler.fn.
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

_REAL_STORE_MODULES: dict[str, Any] = {}


def _force_load(name: str, rel_path: str):
    full = Path(__file__).resolve().parents[2] / rel_path
    spec = importlib.util.spec_from_file_location(name, full)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    _REAL_STORE_MODULES[name] = mod
    return mod


if not getattr(sys.modules.get("backend.core.store"), "__file__", None):
    pkg = types.ModuleType("backend.core.store")
    pkg.__path__ = [str(Path(__file__).resolve().parents[2] / "backend" / "core" / "store")]
    sys.modules["backend.core.store"] = pkg
    _REAL_STORE_MODULES["backend.core.store"] = pkg

_tp = _force_load("backend.core.store.tenant_paths", "backend/core/store/tenant_paths.py")

from backend.core.auth.rbac import PermissionDenied  # noqa: E402
from backend.core.ingest import invite_store, membership, org_store  # noqa: E402

# Импортируем orgs route module НАПРЯМУЮ. backend.api.routes/__init__.py
# тянет agents → autogen (нет в этом env, pre-existing). Обходим через
# importlib.spec_from_file_location.
_orgs_routes_path = (
    Path(__file__).resolve().parents[2] / "backend" / "api" / "routes" / "orgs.py"
)
_spec = importlib.util.spec_from_file_location(
    "backend.api.routes.orgs", _orgs_routes_path
)
orgs_routes = importlib.util.module_from_spec(_spec)
# Создаём parent-package stub если его нет (avoid __init__ side-effects).
if "backend.api" not in sys.modules:
    _api_pkg = types.ModuleType("backend.api")
    _api_pkg.__path__ = [str(Path(__file__).resolve().parents[2] / "backend" / "api")]
    sys.modules["backend.api"] = _api_pkg
if "backend.api.routes" not in sys.modules:
    _routes_pkg = types.ModuleType("backend.api.routes")
    _routes_pkg.__path__ = [str(Path(__file__).resolve().parents[2] / "backend" / "api" / "routes")]
    sys.modules["backend.api.routes"] = _routes_pkg
sys.modules["backend.api.routes.orgs"] = orgs_routes
_spec.loader.exec_module(orgs_routes)


@pytest.fixture(autouse=True)
def _restore_modules():
    for name, mod in _REAL_STORE_MODULES.items():
        sys.modules[name] = mod
    yield


@pytest.fixture(autouse=True)
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setattr(_tp, "_DATA_ROOT", tmp_path)
    membership.clear_cache()
    org_store.clear_cache()
    invite_store.clear_cache()
    return tmp_path


@pytest.fixture(autouse=True)
def _jwt_secret(monkeypatch):
    """orgs.py теперь verify-first: роль признаётся только из токена с
    ПРОВЕРЕННОЙ подписью. Тесты подписывают токены "test-secret" — делаем
    его настоящим секретом окружения, чтобы verify_user_token их принимал."""
    monkeypatch.setenv("JWT_SECRET", "test-secret")


@pytest.fixture(autouse=True)
def _stub_audit_log(monkeypatch):
    """audit_log.emit — async, в этой среде Postgres недоступен. No-op."""
    async def _noop(*args, **kwargs):
        return None
    monkeypatch.setattr(orgs_routes.audit_log, "emit", _noop)


def _call(handler, **kwargs):
    """Вызвать оригинальную async-функцию из Litestar route handler."""
    fn = getattr(handler, "fn", handler)
    return asyncio.run(fn(**kwargs))


def _token(user_id: str, *, role: str | None = None) -> str:
    payload: dict[str, Any] = {"sub": user_id}
    if role:
        payload["role"] = role
    return "Bearer " + jwt.encode(payload, "test-secret", algorithm="HS256")


def _uid() -> str:
    return str(uuid.uuid4())


def _bootstrap_org(name: str = "Acme") -> tuple[str, str]:
    """Создать org через system-admin и вернуть (org_id, founder_id)."""
    founder = _uid()
    org_id = _uid()
    _call(
        orgs_routes.create_org,
        data={"org_id": org_id, "name": name, "founder_user_id": founder},
        authorization=_token(_uid(), role="admin"),
    )
    return org_id, founder


# =============================================================================
# create_org
# =============================================================================


def test_create_org_self_serve_for_regular_user():
    """P3 мульти-аккаунта: обычный пользователь БЕЗ орги создаёт компанию
    для себя (founder=он сам, org_id генерируется — свой id занять нельзя)."""
    caller = _uid()
    requested_org_id = _uid()
    result = _call(
        orgs_routes.create_org,
        data={"org_id": requested_org_id, "name": "Моя компания"},
        authorization=_token(caller),
    )
    assert result["founder"]["user_id"] == caller
    assert result["founder"]["role"] == "founder"
    assert result["org"]["org_id"] != requested_org_id  # id сгенерирован

    # второй раз — 409 (одна орга на пользователя)
    with pytest.raises(HTTPException) as e:
        _call(orgs_routes.create_org, data={"name": "Вторая"},
              authorization=_token(caller))
    assert e.value.status_code == 409


def test_create_org_regular_user_cannot_set_other_founder():
    with pytest.raises(HTTPException) as e:
        _call(
            orgs_routes.create_org,
            data={"name": "Acme", "founder_user_id": _uid()},
            authorization=_token(_uid()),
        )
    assert e.value.status_code == 403


def test_create_org_happy_path():
    caller = _uid()
    org_id = _uid()
    result = _call(
        orgs_routes.create_org,
        data={"org_id": org_id, "name": "Acme"},
        authorization=_token(caller, role="admin"),
    )
    assert result["org"]["org_id"] == org_id
    assert result["org"]["name"] == "Acme"
    assert result["founder"]["user_id"] == caller
    assert result["founder"]["role"] == "founder"


def test_create_org_can_set_other_founder():
    caller = _uid()
    founder = _uid()
    org_id = _uid()
    result = _call(
        orgs_routes.create_org,
        data={"org_id": org_id, "name": "Acme", "founder_user_id": founder},
        authorization=_token(caller, role="admin"),
    )
    assert result["founder"]["user_id"] == founder


def test_create_org_duplicate_returns_409():
    org_id = _uid()
    _call(
        orgs_routes.create_org,
        data={"org_id": org_id, "name": "Acme"},
        authorization=_token(_uid(), role="admin"),
    )
    with pytest.raises(HTTPException) as exc:
        _call(
            orgs_routes.create_org,
            data={"org_id": org_id, "name": "Acme2"},
            authorization=_token(_uid(), role="admin"),
        )
    assert exc.value.status_code == 409


def test_create_org_missing_required_fields():
    with pytest.raises(HTTPException) as exc:
        _call(
            orgs_routes.create_org,
            data={"org_id": _uid()},  # no name
            authorization=_token(_uid(), role="admin"),
        )
    assert exc.value.status_code == 400


# =============================================================================
# get_org_info / list_org_members
# =============================================================================


def test_get_org_info_member_can_read():
    org_id, founder = _bootstrap_org()
    info = _call(orgs_routes.get_org_info, org_id=org_id, authorization=_token(founder))
    assert info["org_id"] == org_id
    assert info["member_count"] == 1


def test_get_org_info_non_member_denied():
    org_id, _ = _bootstrap_org()
    with pytest.raises(HTTPException) as exc:
        _call(orgs_routes.get_org_info, org_id=org_id, authorization=_token(_uid()))
    assert exc.value.status_code == 403


def test_get_org_info_unknown_org_404():
    with pytest.raises(HTTPException) as exc:
        _call(
            orgs_routes.get_org_info,
            org_id=_uid(),
            authorization=_token(_uid(), role="admin"),
        )
    assert exc.value.status_code == 404


def test_list_org_members():
    org_id, founder = _bootstrap_org()
    employee = _uid()
    _call(
        orgs_routes.add_org_member,
        org_id=org_id, data={"user_id": employee, "role": "employee"},
        authorization=_token(founder),
    )
    result = _call(orgs_routes.list_org_members, org_id=org_id, authorization=_token(founder))
    assert result["count"] == 2
    assert {m["user_id"] for m in result["members"]} == {founder, employee}


# =============================================================================
# add_org_member
# =============================================================================


def test_add_org_member_by_founder():
    org_id, founder = _bootstrap_org()
    new_user = _uid()
    result = _call(
        orgs_routes.add_org_member,
        org_id=org_id, data={"user_id": new_user, "role": "manager"},
        authorization=_token(founder),
    )
    assert result["user_id"] == new_user
    assert result["role"] == "manager"


def test_add_org_member_by_employee_denied():
    """Employee не может пригласить нового member'а."""
    org_id, founder = _bootstrap_org()
    employee = _uid()
    _call(
        orgs_routes.add_org_member,
        org_id=org_id, data={"user_id": employee, "role": "employee"},
        authorization=_token(founder),
    )
    with pytest.raises(HTTPException) as exc:
        _call(
            orgs_routes.add_org_member,
            org_id=org_id, data={"user_id": _uid(), "role": "employee"},
            authorization=_token(employee),
        )
    assert exc.value.status_code == 403


def test_add_org_member_by_system_admin():
    """System-admin может добавлять в любую org."""
    org_id, _ = _bootstrap_org()
    new_user = _uid()
    result = _call(
        orgs_routes.add_org_member,
        org_id=org_id, data={"user_id": new_user},
        authorization=_token(_uid(), role="admin"),
    )
    assert result["user_id"] == new_user


def test_add_org_member_blocks_cross_org():
    org_id_a, founder_a = _bootstrap_org("OrgA")
    org_id_b, founder_b = _bootstrap_org("OrgB")
    employee = _uid()
    _call(
        orgs_routes.add_org_member,
        org_id=org_id_a, data={"user_id": employee, "role": "employee"},
        authorization=_token(founder_a),
    )
    with pytest.raises(HTTPException) as exc:
        _call(
            orgs_routes.add_org_member,
            org_id=org_id_b, data={"user_id": employee, "role": "employee"},
            authorization=_token(founder_b),
        )
    assert exc.value.status_code == 409


# =============================================================================
# remove_org_member
# =============================================================================


def test_remove_member_by_admin():
    org_id, founder = _bootstrap_org()
    employee = _uid()
    _call(
        orgs_routes.add_org_member,
        org_id=org_id, data={"user_id": employee, "role": "employee"},
        authorization=_token(founder),
    )
    result = _call(
        orgs_routes.remove_org_member,
        org_id=org_id, user_id=employee, authorization=_token(founder),
    )
    assert result["removed"] is True
    assert membership.is_org_member(employee, org_id) is False


def test_remove_member_self_leave():
    """Self-leave: любой member может удалить себя."""
    org_id, founder = _bootstrap_org()
    employee = _uid()
    _call(
        orgs_routes.add_org_member,
        org_id=org_id, data={"user_id": employee, "role": "employee"},
        authorization=_token(founder),
    )
    result = _call(
        orgs_routes.remove_org_member,
        org_id=org_id, user_id=employee, authorization=_token(employee),
    )
    assert result["removed"] is True


def test_remove_member_by_unrelated_user_denied():
    org_id, founder = _bootstrap_org()
    target = _uid()
    _call(
        orgs_routes.add_org_member,
        org_id=org_id, data={"user_id": target, "role": "employee"},
        authorization=_token(founder),
    )
    with pytest.raises(HTTPException) as exc:
        _call(
            orgs_routes.remove_org_member,
            org_id=org_id, user_id=target, authorization=_token(_uid()),
        )
    assert exc.value.status_code == 403


def test_remove_last_founder_blocked():
    """Последний founder не может уйти — org остался бы без admin'а."""
    org_id, founder = _bootstrap_org()
    with pytest.raises(HTTPException) as exc:
        _call(
            orgs_routes.remove_org_member,
            org_id=org_id, user_id=founder, authorization=_token(founder),
        )
    assert exc.value.status_code == 409
    assert "founder" in exc.value.detail.lower()


def test_remove_founder_ok_when_another_admin_exists():
    org_id, founder = _bootstrap_org()
    co_admin = _uid()
    _call(
        orgs_routes.add_org_member,
        org_id=org_id, data={"user_id": co_admin, "role": "admin"},
        authorization=_token(founder),
    )
    result = _call(
        orgs_routes.remove_org_member,
        org_id=org_id, user_id=founder, authorization=_token(founder),
    )
    assert result["removed"] is True


def test_remove_non_member_returns_404():
    org_id, founder = _bootstrap_org()
    with pytest.raises(HTTPException) as exc:
        _call(
            orgs_routes.remove_org_member,
            org_id=org_id, user_id=_uid(), authorization=_token(founder),
        )
    assert exc.value.status_code == 404


# =============================================================================
# update_org_member
# =============================================================================


def test_update_member_role_by_admin():
    org_id, founder = _bootstrap_org()
    employee = _uid()
    _call(
        orgs_routes.add_org_member,
        org_id=org_id, data={"user_id": employee, "role": "employee"},
        authorization=_token(founder),
    )
    result = _call(
        orgs_routes.update_org_member,
        org_id=org_id, user_id=employee, data={"role": "manager"},
        authorization=_token(founder),
    )
    assert result["role"] == "manager"
    assert membership.get_user_org_role(employee) == "manager"


def test_demote_last_founder_blocked():
    org_id, founder = _bootstrap_org()
    with pytest.raises(HTTPException) as exc:
        _call(
            orgs_routes.update_org_member,
            org_id=org_id, user_id=founder, data={"role": "employee"},
            authorization=_token(founder),
        )
    assert exc.value.status_code == 409


def test_update_member_unknown_role_400():
    org_id, founder = _bootstrap_org()
    employee = _uid()
    _call(
        orgs_routes.add_org_member,
        org_id=org_id, data={"user_id": employee, "role": "employee"},
        authorization=_token(founder),
    )
    with pytest.raises(HTTPException) as exc:
        _call(
            orgs_routes.update_org_member,
            org_id=org_id, user_id=employee, data={"role": "supreme-leader"},
            authorization=_token(founder),
        )
    assert exc.value.status_code == 400


# =============================================================================
# Wave 3.1 — Invite endpoints
# =============================================================================


def _token_with_email(user_id: str, email: str) -> str:
    """JWT с email-claim — для email-pinning тестов."""
    payload = {"sub": user_id, "email": email}
    return "Bearer " + jwt.encode(payload, "test-secret", algorithm="HS256")


def test_create_invite_by_founder():
    org_id, founder = _bootstrap_org()
    result = _call(
        orgs_routes.create_org_invite,
        org_id=org_id, data={"role": "manager"},
        authorization=_token(founder),
    )
    assert "token" in result
    assert len(result["token"]) >= 32
    assert result["role"] == "manager"
    assert result["status"] == "pending"


def test_create_invite_by_employee_denied():
    org_id, founder = _bootstrap_org()
    employee = _uid()
    _call(orgs_routes.add_org_member, org_id=org_id,
          data={"user_id": employee, "role": "employee"},
          authorization=_token(founder))
    with pytest.raises(HTTPException) as exc:
        _call(orgs_routes.create_org_invite,
              org_id=org_id, data={"role": "manager"},
              authorization=_token(employee))
    assert exc.value.status_code == 403


def test_create_invite_unknown_role_400():
    org_id, founder = _bootstrap_org()
    with pytest.raises(HTTPException) as exc:
        _call(orgs_routes.create_org_invite,
              org_id=org_id, data={"role": "supreme-leader"},
              authorization=_token(founder))
    assert exc.value.status_code == 400


def test_create_invite_with_email():
    org_id, founder = _bootstrap_org()
    result = _call(orgs_routes.create_org_invite,
                   org_id=org_id,
                   data={"role": "employee", "invited_email": "Alice@Example.com"},
                   authorization=_token(founder))
    assert result["invited_email"] == "alice@example.com"


def test_list_invites_only_pending_by_default():
    org_id, founder = _bootstrap_org()
    pending = _call(orgs_routes.create_org_invite, org_id=org_id,
                    data={}, authorization=_token(founder))
    consumed = _call(orgs_routes.create_org_invite, org_id=org_id,
                     data={}, authorization=_token(founder))
    # Накручиваем «used» через store напрямую (handler accept ниже).
    invite_store.consume_invite(consumed["token"], consuming_user_id=_uid())

    result = _call(orgs_routes.list_org_invites, org_id=org_id,
                   authorization=_token(founder), include_used=False)
    ids = {i["invite_id"] for i in result["invites"]}
    assert pending["invite_id"] in ids
    assert consumed["invite_id"] not in ids


def test_list_invites_include_used():
    org_id, founder = _bootstrap_org()
    _call(orgs_routes.create_org_invite, org_id=org_id, data={}, authorization=_token(founder))
    used = _call(orgs_routes.create_org_invite, org_id=org_id, data={}, authorization=_token(founder))
    invite_store.consume_invite(used["token"], consuming_user_id=_uid())

    result = _call(orgs_routes.list_org_invites, org_id=org_id,
                   authorization=_token(founder), include_used=True)
    assert result["count"] == 2


def test_revoke_invite_by_admin():
    org_id, founder = _bootstrap_org()
    inv = _call(orgs_routes.create_org_invite, org_id=org_id, data={}, authorization=_token(founder))
    result = _call(orgs_routes.revoke_org_invite,
                   org_id=org_id, invite_id=inv["invite_id"],
                   authorization=_token(founder))
    assert result["revoked"] is True


def test_revoke_invite_twice_returns_410():
    org_id, founder = _bootstrap_org()
    inv = _call(orgs_routes.create_org_invite, org_id=org_id, data={}, authorization=_token(founder))
    _call(orgs_routes.revoke_org_invite, org_id=org_id, invite_id=inv["invite_id"],
          authorization=_token(founder))
    with pytest.raises(HTTPException) as exc:
        _call(orgs_routes.revoke_org_invite,
              org_id=org_id, invite_id=inv["invite_id"],
              authorization=_token(founder))
    assert exc.value.status_code == 410


def test_get_invite_info_returns_org_name():
    org_id, founder = _bootstrap_org("Acme Corp")
    inv = _call(orgs_routes.create_org_invite, org_id=org_id, data={}, authorization=_token(founder))
    result = _call(orgs_routes.get_invite_info, token=inv["token"])
    assert result["org_name"] == "Acme Corp"
    assert result["status"] == "pending"


def test_get_invite_info_unknown_token_404():
    with pytest.raises(HTTPException) as exc:
        _call(orgs_routes.get_invite_info, token="a" * 64)
    assert exc.value.status_code == 404


def test_accept_invite_happy_path():
    org_id, founder = _bootstrap_org()
    inv = _call(orgs_routes.create_org_invite, org_id=org_id,
                data={"role": "manager"}, authorization=_token(founder))
    new_user = _uid()
    result = _call(orgs_routes.accept_invite,
                   token=inv["token"], authorization=_token(new_user))
    assert result["accepted"] is True
    assert result["member"]["org_id"] == org_id
    assert result["member"]["role"] == "manager"
    assert membership.is_org_member(new_user, org_id) is True


def test_accept_invite_used_returns_410():
    org_id, founder = _bootstrap_org()
    inv = _call(orgs_routes.create_org_invite, org_id=org_id, data={}, authorization=_token(founder))
    _call(orgs_routes.accept_invite, token=inv["token"], authorization=_token(_uid()))
    with pytest.raises(HTTPException) as exc:
        _call(orgs_routes.accept_invite, token=inv["token"], authorization=_token(_uid()))
    assert exc.value.status_code == 410


def test_accept_invite_revoked_returns_410():
    org_id, founder = _bootstrap_org()
    inv = _call(orgs_routes.create_org_invite, org_id=org_id, data={}, authorization=_token(founder))
    _call(orgs_routes.revoke_org_invite, org_id=org_id, invite_id=inv["invite_id"],
          authorization=_token(founder))
    with pytest.raises(HTTPException) as exc:
        _call(orgs_routes.accept_invite, token=inv["token"], authorization=_token(_uid()))
    assert exc.value.status_code == 410


def test_accept_invite_email_mismatch_403():
    org_id, founder = _bootstrap_org()
    inv = _call(orgs_routes.create_org_invite,
                org_id=org_id, data={"invited_email": "alice@example.com"},
                authorization=_token(founder))
    bob = _uid()
    with pytest.raises(HTTPException) as exc:
        _call(orgs_routes.accept_invite, token=inv["token"],
              authorization=_token_with_email(bob, "bob@example.com"))
    assert exc.value.status_code == 403


def test_accept_invite_email_pinning_matches():
    org_id, founder = _bootstrap_org()
    inv = _call(orgs_routes.create_org_invite,
                org_id=org_id, data={"invited_email": "alice@example.com"},
                authorization=_token(founder))
    alice = _uid()
    result = _call(orgs_routes.accept_invite, token=inv["token"],
                   authorization=_token_with_email(alice, "Alice@Example.com"))
    assert result["accepted"] is True


def test_accept_invite_cross_org_block_409():
    """User уже в org A — accept invite в org B → 409."""
    org_a, founder_a = _bootstrap_org("OrgA")
    org_b, founder_b = _bootstrap_org("OrgB")
    user = _uid()
    _call(orgs_routes.add_org_member, org_id=org_a,
          data={"user_id": user, "role": "employee"},
          authorization=_token(founder_a))
    inv = _call(orgs_routes.create_org_invite, org_id=org_b, data={},
                authorization=_token(founder_b))
    with pytest.raises(HTTPException) as exc:
        _call(orgs_routes.accept_invite, token=inv["token"], authorization=_token(user))
    assert exc.value.status_code == 409


def test_accept_invite_unknown_token_404():
    with pytest.raises(HTTPException) as exc:
        _call(orgs_routes.accept_invite, token="a" * 64, authorization=_token(_uid()))
    assert exc.value.status_code == 404
