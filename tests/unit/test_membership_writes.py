"""Unit-тесты для writer-API membership + org_store.

Покрывает:
  - add_member / remove_member / update_member_role / list_members
  - can_manage_org / is_org_member
  - Atomic write (failed write не разрушает существующий файл)
  - File lock (concurrent writers не теряют данные)
  - org_store: create_org / get_org / list_orgs
  - Role validation (unknown role → ValueError)
  - Cross-org защита (user уже в org A — нельзя добавить в B)
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import multiprocessing
import sys
import threading
import time
import types
import uuid
from pathlib import Path
from typing import Any

import pytest

# Same bootstrap pattern as test_meeting_idempotency: real backend.core.store
# модули нужно зафиксировать ДО других тестов в общем suite.
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

from backend.core.ingest import membership, org_store  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_modules():
    for name, mod in _REAL_STORE_MODULES.items():
        sys.modules[name] = mod
    yield


@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setattr(_tp, "_DATA_ROOT", tmp_path)
    membership.clear_cache()
    org_store.clear_cache()
    return tmp_path


# =============================================================================
# membership.add_member / get / remove / update
# =============================================================================


def test_add_member_creates_entry(tmp_data):
    u = str(uuid.uuid4())
    o = str(uuid.uuid4())
    result = membership.add_member(u, o, role="founder")
    assert result["org_id"] == o
    assert result["role"] == "founder"
    assert "joined_at" in result
    assert membership.get_org_for_user(u) == o
    assert membership.get_user_org_role(u) == "founder"


def test_add_member_rejects_unknown_role(tmp_data):
    u = str(uuid.uuid4())
    o = str(uuid.uuid4())
    with pytest.raises(ValueError, match="unknown role"):
        membership.add_member(u, o, role="god-king")


def test_add_member_blocks_cross_org(tmp_data):
    """User уже в org A — не может быть добавлен в org B."""
    u = str(uuid.uuid4())
    org_a = str(uuid.uuid4())
    org_b = str(uuid.uuid4())
    membership.add_member(u, org_a, role="employee")
    with pytest.raises(ValueError, match="already in org"):
        membership.add_member(u, org_b, role="employee")


def test_add_member_idempotent_in_same_org(tmp_data):
    """Повторный add в ту же org — обновляет role/access_level."""
    u = str(uuid.uuid4())
    o = str(uuid.uuid4())
    r1 = membership.add_member(u, o, role="employee")
    r2 = membership.add_member(u, o, role="manager")
    assert r2["role"] == "manager"
    # joined_at — preserved (не сбрасываем при «обновлении» role)
    assert r2["joined_at"] == r1["joined_at"]


def test_remove_member_returns_true_when_present(tmp_data):
    u = str(uuid.uuid4())
    o = str(uuid.uuid4())
    membership.add_member(u, o, role="employee")
    assert membership.remove_member(u, o) is True
    assert membership.get_org_for_user(u) is None


def test_remove_member_returns_false_when_absent(tmp_data):
    assert membership.remove_member(str(uuid.uuid4()), str(uuid.uuid4())) is False


def test_remove_member_wrong_org_no_op(tmp_data):
    """Защита: попытка удалить user из не-его org → False, ничего не меняем."""
    u = str(uuid.uuid4())
    real_org = str(uuid.uuid4())
    other_org = str(uuid.uuid4())
    membership.add_member(u, real_org, role="employee")
    assert membership.remove_member(u, other_org) is False
    assert membership.get_org_for_user(u) == real_org


def test_update_member_role(tmp_data):
    u = str(uuid.uuid4())
    o = str(uuid.uuid4())
    membership.add_member(u, o, role="employee")
    assert membership.update_member_role(u, o, "manager") is True
    assert membership.get_user_org_role(u) == "manager"


def test_update_member_role_rejects_unknown(tmp_data):
    u = str(uuid.uuid4())
    o = str(uuid.uuid4())
    membership.add_member(u, o, role="employee")
    with pytest.raises(ValueError):
        membership.update_member_role(u, o, "supreme-leader")


def test_list_members_returns_all_org_members(tmp_data):
    o = str(uuid.uuid4())
    other_o = str(uuid.uuid4())
    u1 = str(uuid.uuid4())
    u2 = str(uuid.uuid4())
    u_other = str(uuid.uuid4())
    membership.add_member(u1, o, role="founder")
    membership.add_member(u2, o, role="employee")
    membership.add_member(u_other, other_o, role="employee")

    members = membership.list_members(o)
    member_ids = {m["user_id"] for m in members}
    assert member_ids == {u1, u2}
    roles = {m["user_id"]: m["role"] for m in members}
    assert roles[u1] == "founder"
    assert roles[u2] == "employee"


# =============================================================================
# can_manage_org / is_org_member
# =============================================================================


def test_can_manage_org_founder_yes(tmp_data):
    u = str(uuid.uuid4())
    o = str(uuid.uuid4())
    membership.add_member(u, o, role="founder")
    assert membership.can_manage_org(u, o) is True


def test_can_manage_org_admin_yes(tmp_data):
    u = str(uuid.uuid4())
    o = str(uuid.uuid4())
    membership.add_member(u, o, role="admin")
    assert membership.can_manage_org(u, o) is True


def test_can_manage_org_employee_no(tmp_data):
    u = str(uuid.uuid4())
    o = str(uuid.uuid4())
    membership.add_member(u, o, role="employee")
    assert membership.can_manage_org(u, o) is False


def test_can_manage_org_other_org_no(tmp_data):
    u = str(uuid.uuid4())
    o = str(uuid.uuid4())
    other_o = str(uuid.uuid4())
    membership.add_member(u, o, role="founder")
    assert membership.can_manage_org(u, other_o) is False


def test_is_org_member_any_role(tmp_data):
    u = str(uuid.uuid4())
    o = str(uuid.uuid4())
    membership.add_member(u, o, role="contractor")
    assert membership.is_org_member(u, o) is True
    assert membership.is_org_member(u, str(uuid.uuid4())) is False
    assert membership.is_org_member(str(uuid.uuid4()), o) is False


# =============================================================================
# Atomic write — partial write не должен разрушать существующий файл
# =============================================================================


def test_atomic_write_does_not_leave_partial_on_disk(tmp_data):
    u = str(uuid.uuid4())
    o = str(uuid.uuid4())
    membership.add_member(u, o, role="employee")

    # Сохраняем содержимое production-файла до симуляции crash.
    from backend.core.ingest import membership as m
    mapping_path = m._user_org_mapping_path()
    original = json.loads(open(mapping_path).read())

    # Подменяем json.dump только локально — НЕ через monkeypatch (он отменит
    # и наш setattr на _DATA_ROOT из tmp_data fixture).
    real_dump = m.json.dump

    def _broken_dump(*args, **kwargs):
        raise RuntimeError("simulated mid-write crash")

    m.json.dump = _broken_dump
    try:
        with pytest.raises(RuntimeError):
            membership.add_member(str(uuid.uuid4()), o, role="employee")
    finally:
        m.json.dump = real_dump

    after_crash = json.loads(open(mapping_path).read())
    assert after_crash == original, \
        "partial write leaked into production file!"
    # Дополнительно: tempfile не должен висеть в parent dir после rollback.
    parent = Path(mapping_path).parent
    leftover = list(parent.glob(".tmp_mapping_*.json"))
    assert not leftover, f"tempfile leak: {leftover}"


# =============================================================================
# Concurrent writers (thread-level) — два потока добавляют разных members,
# оба попадают в файл.
# =============================================================================


def test_concurrent_thread_writes_dont_lose_entries(tmp_data):
    o = str(uuid.uuid4())
    users = [str(uuid.uuid4()) for _ in range(8)]

    def _add(uid):
        membership.add_member(uid, o, role="employee")

    threads = [threading.Thread(target=_add, args=(u,)) for u in users]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    membership.clear_cache()
    members = membership.list_members(o)
    member_ids = {m["user_id"] for m in members}
    assert member_ids == set(users), \
        f"lost entries under concurrent writes: missing {set(users) - member_ids}"


# =============================================================================
# org_store
# =============================================================================


def test_create_org_persists_metadata(tmp_data):
    o = str(uuid.uuid4())
    u = str(uuid.uuid4())
    org_store.create_org(o, name="Acme Inc", created_by=u)
    got = org_store.get_org(o)
    assert got is not None
    assert got["name"] == "Acme Inc"
    assert got["created_by"] == u
    assert "created_at" in got


def test_create_org_rejects_duplicate(tmp_data):
    o = str(uuid.uuid4())
    u = str(uuid.uuid4())
    org_store.create_org(o, name="Acme", created_by=u)
    with pytest.raises(ValueError, match="already exists"):
        org_store.create_org(o, name="Acme2", created_by=u)


def test_get_org_returns_none_for_unknown(tmp_data):
    assert org_store.get_org(str(uuid.uuid4())) is None


def test_list_orgs(tmp_data):
    u = str(uuid.uuid4())
    ids = [str(uuid.uuid4()) for _ in range(3)]
    for i, oid in enumerate(ids):
        org_store.create_org(oid, name=f"Org{i}", created_by=u)
    org_store.clear_cache()
    listed_ids = {o["org_id"] for o in org_store.list_orgs()}
    assert listed_ids == set(ids)
