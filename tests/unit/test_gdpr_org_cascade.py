"""Тесты для GDPR org-aware каскада (P1 #9).

Покрывает:
  - erase_filesystem_user_data: удаляет 4 файла, dry_run preview
  - erase_org_membership: убирает из mapping
  - revoke_user_invites: pending → revoked; used сохраняется
  - tombstone_in_org_graphs:
      * user_id заменяется на детерминированный hash
      * promoted_from_user_id заменяется
      * PII-поля удаляются
      * сам узел остаётся (бизнес-IP)
      * tombstoned_at / tombstoned_reason записаны
      * один tombstone_id для одного user_id (детерминированность)
      * другие узлы (не этого user) не тронуты
  - erase_user_full: orchestrator вызывает всё
  - Idempotency: повторный вызов не падает
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import json
import sys
import types
import uuid
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

from backend.core.gdpr import org_cascade  # noqa: E402
from backend.core.ingest import invite_store, membership  # noqa: E402


@pytest.fixture(autouse=True)
def _restore():
    for name, mod in _REAL.items():
        sys.modules[name] = mod
    yield


@pytest.fixture(autouse=True)
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setattr(_tp, "_DATA_ROOT", tmp_path)
    membership.clear_cache()
    invite_store.clear_cache()
    return tmp_path


def _run(coro):
    return asyncio.run(coro)


def _uid() -> str:
    return str(uuid.uuid4())


# =============================================================================
# erase_filesystem_user_data
# =============================================================================


def test_filesystem_erase_removes_existing_files(tmp_data):
    user = _uid()
    # Создаём 4 файла, которые erase должен удалить.
    g = Path(_tp.graph_path_for_user(user))
    v = Path(_tp.vector_index_path_for_user(user))
    b = Path(_tp.bm25_index_path_for_user(user))
    t = Path(_tp.temporal_db_path_for_user(user))
    for p in (g, v, b, t):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}")

    result = org_cascade.erase_filesystem_user_data(user)
    assert all(result["deleted"].values()), result
    for p in (g, v, b, t):
        assert not p.exists()


def test_filesystem_erase_handles_missing_files(tmp_data):
    """Если файлов нет — не падаем, просто возвращаем False."""
    user = _uid()
    result = org_cascade.erase_filesystem_user_data(user)
    assert all(v is False for v in result["deleted"].values())
    assert result["errors"] == {}


def test_filesystem_erase_dry_run_does_not_delete(tmp_data):
    user = _uid()
    g = Path(_tp.graph_path_for_user(user))
    g.parent.mkdir(parents=True, exist_ok=True)
    g.write_text("{}")

    result = org_cascade.erase_filesystem_user_data(user, dry_run=True)
    assert result["deleted"]["graph"] is True  # would delete
    assert g.exists()  # но файл на месте


def test_filesystem_erase_empty_user_id_returns_error(tmp_data):
    result = org_cascade.erase_filesystem_user_data("")
    assert "user_id required" in result["errors"]["_"]


# =============================================================================
# erase_org_membership
# =============================================================================


def test_membership_erase_removes_entry(tmp_data):
    user = _uid()
    org = _uid()
    membership.add_member(user, org, role="employee")
    assert membership.is_org_member(user, org) is True

    result = org_cascade.erase_org_membership(user)
    assert result["removed"] is True
    assert result["org_id"] == org
    assert membership.is_org_member(user, org) is False


def test_membership_erase_solo_user_no_op(tmp_data):
    result = org_cascade.erase_org_membership(_uid())
    assert result["removed"] is False
    assert result["org_id"] is None


def test_membership_erase_dry_run(tmp_data):
    user = _uid()
    org = _uid()
    membership.add_member(user, org, role="founder")

    result = org_cascade.erase_org_membership(user, dry_run=True)
    assert result["removed"] is True  # would remove
    assert membership.is_org_member(user, org) is True  # but didn't


# =============================================================================
# revoke_user_invites
# =============================================================================


def test_revoke_pending_invites_by_user(tmp_data):
    user = _uid()
    other = _uid()
    org = _uid()

    # 2 pending от user'а, 1 от other'а
    invite_store.create_invite(org, invited_by=user)
    invite_store.create_invite(org, invited_by=user)
    other_inv = invite_store.create_invite(org, invited_by=other)

    result = org_cascade.revoke_user_invites(user)
    assert result["revoked"] == 2

    # other'а invite не тронут
    looked = invite_store.get_invite_by_token(other_inv["token"])
    assert looked["status"] == "pending"


def test_revoke_invites_skips_used(tmp_data):
    """Used invites — historical artifact, не отзываются."""
    user = _uid()
    org = _uid()
    used_inv = invite_store.create_invite(org, invited_by=user)
    invite_store.consume_invite(used_inv["token"], consuming_user_id=_uid())

    result = org_cascade.revoke_user_invites(user)
    assert result["revoked"] == 0
    assert result["skipped_used"] == 1


def test_revoke_invites_dry_run(tmp_data):
    user = _uid()
    org = _uid()
    inv = invite_store.create_invite(org, invited_by=user)

    result = org_cascade.revoke_user_invites(user, dry_run=True)
    assert result["revoked"] == 1  # would revoke
    looked = invite_store.get_invite_by_token(inv["token"])
    assert looked["status"] == "pending"  # but didn't


# =============================================================================
# tombstone_in_org_graphs
# =============================================================================


def _write_org_graph(org_id: str, nodes: list[dict]):
    """Создать org-graph файл напрямую (минуя GraphBuilder для теста)."""
    org_dir = Path(_tp.org_graph_dir(org_id))
    org_dir.mkdir(parents=True, exist_ok=True)
    (org_dir / "graph.json").write_text(json.dumps({
        "nodes": nodes,
        "edges": [],
    }))


def _read_org_graph(org_id: str) -> dict:
    return json.loads(Path(_tp.org_graph_path(org_id)).read_text())


def test_tombstone_replaces_user_id_in_org_graphs(tmp_data):
    user = _uid()
    org = _uid()
    _write_org_graph(org, [
        {"id": "n1", "user_id": user, "summary": "decision 1"},
        {"id": "n2", "user_id": "other_user", "summary": "decision 2"},
    ])

    result = org_cascade.tombstone_in_org_graphs(user)
    assert result["nodes_tombstoned"] == 1

    data = _read_org_graph(org)
    nodes = {n["id"]: n for n in data["nodes"]}
    assert nodes["n1"]["user_id"].startswith("tombstone_")
    assert nodes["n1"]["user_id"] != user
    assert "tombstoned_at" in nodes["n1"]
    assert nodes["n1"]["tombstoned_reason"] == "gdpr_erasure"
    # other_user не тронут
    assert nodes["n2"]["user_id"] == "other_user"
    assert "tombstoned_at" not in nodes["n2"]


def test_tombstone_replaces_promoted_from(tmp_data):
    user = _uid()
    org = _uid()
    _write_org_graph(org, [
        {"id": "n1", "user_id": "system", "promoted_from_user_id": user, "summary": "x"},
    ])

    org_cascade.tombstone_in_org_graphs(user)
    nodes = _read_org_graph(org)["nodes"]
    assert nodes[0]["promoted_from_user_id"].startswith("tombstone_")


def test_tombstone_strips_pii_fields(tmp_data):
    user = _uid()
    org = _uid()
    _write_org_graph(org, [
        {
            "id": "n1",
            "user_id": user,
            "email": "alice@example.com",
            "phone": "+1234567890",
            "display_name": "Alice Smith",
            "summary": "ship feature X",
        },
    ])

    org_cascade.tombstone_in_org_graphs(user)
    nodes = _read_org_graph(org)["nodes"]
    assert "email" not in nodes[0]
    assert "phone" not in nodes[0]
    assert "display_name" not in nodes[0]
    # Бизнес-content остаётся
    assert nodes[0]["summary"] == "ship feature X"


def test_tombstone_is_deterministic(tmp_data):
    """Один user_id → один tombstone в разных org-graphs (связь сохраняется)."""
    user = _uid()
    org_a = _uid()
    org_b = _uid()
    _write_org_graph(org_a, [{"id": "n1", "user_id": user}])
    _write_org_graph(org_b, [{"id": "n2", "user_id": user}])

    org_cascade.tombstone_in_org_graphs(user)
    a = _read_org_graph(org_a)["nodes"][0]["user_id"]
    b = _read_org_graph(org_b)["nodes"][0]["user_id"]
    assert a == b
    assert a.startswith("tombstone_")
    # И НЕ leak'ает оригинал
    assert user not in a


def test_tombstone_dry_run_does_not_modify(tmp_data):
    user = _uid()
    org = _uid()
    _write_org_graph(org, [{"id": "n1", "user_id": user, "summary": "x"}])

    result = org_cascade.tombstone_in_org_graphs(user, dry_run=True)
    assert result["nodes_tombstoned"] == 1  # would tombstone
    nodes = _read_org_graph(org)["nodes"]
    assert nodes[0]["user_id"] == user  # but didn't


def test_tombstone_idempotent_second_run_no_op(tmp_data):
    user = _uid()
    org = _uid()
    _write_org_graph(org, [{"id": "n1", "user_id": user, "summary": "x"}])

    org_cascade.tombstone_in_org_graphs(user)
    second = org_cascade.tombstone_in_org_graphs(user)
    # Узлы уже tombstoned — больше нечего трогать
    assert second["nodes_tombstoned"] == 0


def test_tombstone_no_orgs_no_error(tmp_data):
    """Если нет ни одной org — не падаем."""
    result = org_cascade.tombstone_in_org_graphs(_uid())
    assert result["nodes_tombstoned"] == 0
    assert result["orgs_scanned"] == 0


# =============================================================================
# erase_user_full orchestrator
# =============================================================================


def test_erase_user_full_calls_all_steps(tmp_data, monkeypatch):
    """Orchestrator вызывает все 5 фаз (Postgres + 4 локальные)."""
    user = _uid()
    org = _uid()

    # Mock Postgres-каскад (нет живой БД в этом env).
    async def _fake_pg(uid, *, dry_run=False):
        return {"deleted": {"profiles": 1}, "total_rows": 1, "errors": {}}
    monkeypatch.setattr("backend.core.gdpr.erasure.erase_user", _fake_pg)

    # Подготовим состояние: membership + invite + org-graph узел.
    membership.add_member(user, org, role="employee")
    invite_store.create_invite(org, invited_by=user)
    _write_org_graph(org, [{"id": "n1", "user_id": user, "summary": "x"}])
    g = Path(_tp.graph_path_for_user(user))
    g.parent.mkdir(parents=True, exist_ok=True)
    g.write_text("{}")

    result = _run(org_cascade.erase_user_full(user))

    assert result["postgres"]["total_rows"] == 1
    assert result["membership"]["removed"] is True
    assert result["invites"]["revoked"] == 1
    assert result["org_graphs"]["nodes_tombstoned"] == 1
    assert result["filesystem"]["deleted"]["graph"] is True

    # State после: nothing left
    assert not g.exists()
    assert membership.is_org_member(user, org) is False
    nodes = _read_org_graph(org)["nodes"]
    assert nodes[0]["user_id"].startswith("tombstone_")


def test_erase_user_full_postgres_failure_does_not_stop_local(tmp_data, monkeypatch):
    """REGRESSION: если Postgres-каскад упал — локальный cascade всё равно работает."""
    user = _uid()
    org = _uid()

    async def _bad_pg(uid, *, dry_run=False):
        raise RuntimeError("postgres unreachable")
    monkeypatch.setattr("backend.core.gdpr.erasure.erase_user", _bad_pg)

    membership.add_member(user, org, role="employee")
    g = Path(_tp.graph_path_for_user(user))
    g.parent.mkdir(parents=True, exist_ok=True)
    g.write_text("{}")

    result = _run(org_cascade.erase_user_full(user))
    assert "error" in result["postgres"]
    # Но локальные шаги выполнены:
    assert result["membership"]["removed"] is True
    assert result["filesystem"]["deleted"]["graph"] is True


def test_erase_user_full_dry_run_does_not_modify(tmp_data, monkeypatch):
    user = _uid()
    org = _uid()

    async def _fake_pg(uid, *, dry_run=False):
        return {"deleted": {}, "total_rows": 0, "dry_run": dry_run}
    monkeypatch.setattr("backend.core.gdpr.erasure.erase_user", _fake_pg)

    membership.add_member(user, org, role="employee")
    g = Path(_tp.graph_path_for_user(user))
    g.parent.mkdir(parents=True, exist_ok=True)
    g.write_text("x")

    _run(org_cascade.erase_user_full(user, dry_run=True))

    # Ничего не должно быть изменено.
    assert g.exists()
    assert membership.is_org_member(user, org) is True


def test_pii_fields_list_covers_common_identifiers():
    """Sanity: что PII_FIELDS_TO_STRIP не пустой и содержит email."""
    assert "email" in org_cascade.PII_FIELDS_TO_STRIP
    assert "phone" in org_cascade.PII_FIELDS_TO_STRIP
    assert "display_name" in org_cascade.PII_FIELDS_TO_STRIP


def test_tombstone_id_format():
    """Проверим формат tombstone_id."""
    user = "test-user-uuid"
    t = org_cascade._tombstone_id(user)
    assert t.startswith("tombstone_")
    assert len(t) == len("tombstone_") + 16  # hash16
    # Детерминированный
    assert org_cascade._tombstone_id(user) == t
    # Different user → different tombstone
    assert org_cascade._tombstone_id("another") != t
