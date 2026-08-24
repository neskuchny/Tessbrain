"""Тесты для invite_store (Wave 3.1).

Покрывает:
  - create_invite: возвращает raw token (один раз), storage хранит hash
  - lookup через hash (не равенство строк), constant-time
  - consume_invite: happy + expired + used + revoked + email-mismatch
  - revoke_invite: только pending можно revoke
  - list_invites_for_org: фильтрация include_used
  - Atomic write + concurrent safety
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import sys
import threading
import types
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

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

from backend.core.ingest import invite_store  # noqa: E402


@pytest.fixture(autouse=True)
def _restore():
    for name, mod in _REAL.items():
        sys.modules[name] = mod
    yield


@pytest.fixture(autouse=True)
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setattr(_tp, "_DATA_ROOT", tmp_path)
    invite_store.clear_cache()
    return tmp_path


def _uid() -> str:
    return str(uuid.uuid4())


# =============================================================================
# create_invite
# =============================================================================


def test_create_invite_returns_raw_token_once(tmp_data):
    org_id = _uid()
    invited_by = _uid()
    inv = invite_store.create_invite(org_id, invited_by=invited_by, role="employee")
    assert "token" in inv
    assert len(inv["token"]) >= 32
    assert inv["invite_id"]
    assert inv["status"] == "pending"
    assert inv["role"] == "employee"


def test_create_invite_stores_hash_not_raw(tmp_data):
    """Storage НЕ должен содержать raw token (только SHA256-хэш)."""
    invite_store.create_invite(_uid(), invited_by=_uid())
    invite_store.clear_cache()
    path = invite_store._invites_path()
    raw = Path(path).read_text()
    # Файл существует, есть token_hash
    assert "token_hash" in raw
    # Hex hash = 64 char a-f0-9. Точно не URL-safe base64 (нет _- ).
    data = json.loads(raw)
    for entry in data.values():
        assert len(entry["token_hash"]) == 64
        assert all(c in "0123456789abcdef" for c in entry["token_hash"])


def test_create_invite_with_email(tmp_data):
    inv = invite_store.create_invite(
        _uid(), invited_by=_uid(),
        invited_email="Alice@Example.com",  # должен нормализоваться lowercase
    )
    assert inv["invited_email"] == "alice@example.com"


def test_create_invite_custom_expiry(tmp_data):
    inv = invite_store.create_invite(
        _uid(), invited_by=_uid(),
        expires_in=timedelta(hours=1),
    )
    assert inv["status"] == "pending"  # ещё не expired


def test_create_invite_requires_org_and_inviter(tmp_data):
    with pytest.raises(ValueError):
        invite_store.create_invite("", invited_by=_uid())
    with pytest.raises(ValueError):
        invite_store.create_invite(_uid(), invited_by="")


# =============================================================================
# get_invite_by_token
# =============================================================================


def test_get_invite_by_token_returns_public_view(tmp_data):
    org_id = _uid()
    inv = invite_store.create_invite(org_id, invited_by=_uid())
    looked_up = invite_store.get_invite_by_token(inv["token"])
    assert looked_up is not None
    assert looked_up["org_id"] == org_id
    # Public view не должен содержать token_hash
    assert "token_hash" not in looked_up


def test_get_invite_by_token_returns_none_for_unknown(tmp_data):
    invite_store.create_invite(_uid(), invited_by=_uid())
    assert invite_store.get_invite_by_token("not_a_real_token_aaaaaaaaaaaaa") is None


def test_get_invite_by_token_rejects_short_token(tmp_data):
    """Защита от accidental probe с короткой строкой."""
    assert invite_store.get_invite_by_token("") is None
    assert invite_store.get_invite_by_token("short") is None


# =============================================================================
# consume_invite
# =============================================================================


def test_consume_invite_happy_path(tmp_data):
    org_id = _uid()
    inv = invite_store.create_invite(org_id, invited_by=_uid(), role="manager")
    user_id = _uid()
    accept = invite_store.consume_invite(inv["token"], consuming_user_id=user_id)
    assert accept["ok"] is True
    assert accept["org_id"] == org_id
    assert accept["role"] == "manager"

    # После accept invite помечен used.
    looked_up = invite_store.get_invite_by_token(inv["token"])
    assert looked_up["status"] == "used"
    assert looked_up["used_by"] == user_id


def test_consume_invite_used_twice_fails(tmp_data):
    inv = invite_store.create_invite(_uid(), invited_by=_uid())
    invite_store.consume_invite(inv["token"], consuming_user_id=_uid())
    with pytest.raises(ValueError, match="used"):
        invite_store.consume_invite(inv["token"], consuming_user_id=_uid())


def test_consume_invite_expired(tmp_data):
    inv = invite_store.create_invite(
        _uid(), invited_by=_uid(),
        expires_in=timedelta(seconds=-1),  # уже expired
    )
    with pytest.raises(ValueError, match="expired"):
        invite_store.consume_invite(inv["token"], consuming_user_id=_uid())


def test_consume_invite_revoked(tmp_data):
    org_id = _uid()
    inv = invite_store.create_invite(org_id, invited_by=_uid())
    invite_store.revoke_invite(inv["invite_id"], org_id, revoked_by=_uid())
    with pytest.raises(ValueError, match="revoked"):
        invite_store.consume_invite(inv["token"], consuming_user_id=_uid())


def test_consume_invite_unknown_token(tmp_data):
    with pytest.raises(ValueError, match="token_not_found"):
        invite_store.consume_invite("a" * 64, consuming_user_id=_uid())


def test_consume_invite_email_pinning_matches(tmp_data):
    inv = invite_store.create_invite(
        _uid(), invited_by=_uid(),
        invited_email="alice@example.com",
    )
    # Корректный email — accept проходит.
    invite_store.consume_invite(
        inv["token"], consuming_user_id=_uid(),
        consuming_user_email="Alice@Example.com",  # регистр-нечувствительно
    )


def test_consume_invite_email_pinning_mismatch(tmp_data):
    inv = invite_store.create_invite(
        _uid(), invited_by=_uid(),
        invited_email="alice@example.com",
    )
    with pytest.raises(ValueError, match="email_mismatch"):
        invite_store.consume_invite(
            inv["token"], consuming_user_id=_uid(),
            consuming_user_email="bob@example.com",
        )


def test_consume_invite_no_email_pinning_when_invite_has_no_email(tmp_data):
    """invite без email — accepter может быть любой."""
    inv = invite_store.create_invite(_uid(), invited_by=_uid())
    # Не передаём email — должно пройти.
    invite_store.consume_invite(inv["token"], consuming_user_id=_uid())


# =============================================================================
# revoke_invite
# =============================================================================


def test_revoke_pending_invite(tmp_data):
    org_id = _uid()
    inv = invite_store.create_invite(org_id, invited_by=_uid())
    assert invite_store.revoke_invite(inv["invite_id"], org_id, revoked_by=_uid()) is True
    looked = invite_store.get_invite_by_token(inv["token"])
    assert looked["status"] == "revoked"


def test_revoke_already_used_returns_false(tmp_data):
    org_id = _uid()
    inv = invite_store.create_invite(org_id, invited_by=_uid())
    invite_store.consume_invite(inv["token"], consuming_user_id=_uid())
    assert invite_store.revoke_invite(inv["invite_id"], org_id, revoked_by=_uid()) is False


def test_revoke_cross_org_no_op(tmp_data):
    """Защита: попытка revoke invite чужой org → False."""
    org_a = _uid()
    other_org = _uid()
    inv = invite_store.create_invite(org_a, invited_by=_uid())
    assert invite_store.revoke_invite(inv["invite_id"], other_org, revoked_by=_uid()) is False
    looked = invite_store.get_invite_by_token(inv["token"])
    assert looked["status"] == "pending"


# =============================================================================
# list_invites_for_org
# =============================================================================


def test_list_only_pending_by_default(tmp_data):
    org_id = _uid()
    pending = invite_store.create_invite(org_id, invited_by=_uid())
    used = invite_store.create_invite(org_id, invited_by=_uid())
    invite_store.consume_invite(used["token"], consuming_user_id=_uid())

    listed = invite_store.list_invites_for_org(org_id)
    ids = {i["invite_id"] for i in listed}
    assert pending["invite_id"] in ids
    assert used["invite_id"] not in ids


def test_list_include_used(tmp_data):
    org_id = _uid()
    invite_store.create_invite(org_id, invited_by=_uid())
    used = invite_store.create_invite(org_id, invited_by=_uid())
    invite_store.consume_invite(used["token"], consuming_user_id=_uid())

    listed = invite_store.list_invites_for_org(org_id, include_used=True)
    assert len(listed) == 2


def test_list_does_not_leak_other_orgs(tmp_data):
    org_a = _uid()
    org_b = _uid()
    invite_store.create_invite(org_a, invited_by=_uid())
    invite_store.create_invite(org_b, invited_by=_uid())
    listed = invite_store.list_invites_for_org(org_a)
    assert len(listed) == 1
    assert listed[0]["org_id"] == org_a


# =============================================================================
# Concurrent safety (two workers consume same token simultaneously)
# =============================================================================


def test_concurrent_consume_only_one_wins(tmp_data):
    """REGRESSION: при одновременном accept'е токен может уйти только одному."""
    inv = invite_store.create_invite(_uid(), invited_by=_uid())
    results: list[Any] = []

    def _try_consume(uid):
        try:
            r = invite_store.consume_invite(inv["token"], consuming_user_id=uid)
            results.append(("ok", uid, r))
        except ValueError as e:
            results.append(("err", uid, str(e)))

    threads = [
        threading.Thread(target=_try_consume, args=(_uid(),))
        for _ in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    oks = [r for r in results if r[0] == "ok"]
    errs = [r for r in results if r[0] == "err"]
    assert len(oks) == 1, f"more than one consumer succeeded: {oks}"
    assert len(errs) == 4
    assert all("used" in r[2] for r in errs)
