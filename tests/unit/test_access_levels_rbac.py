"""Тесты для RBAC по access_level в org-graph (P1 #5).

Проверяет:
  - max_access_level_for_user: role → max int (founder=4, employee=2, etc.)
  - Unknown role / solo user → fail-safe (только PUBLIC)
  - node_visible_to: owner-check, promoted_from check, level-фильтр
  - Интеграция в merged_graph_view_for_user:
      * employee видит только узлы с access_level ≤ 2
      * manager видит ≤ 3 (включая «manager-only» plans)
      * founder видит всё (≤ 4)
      * personal-узлы (owner=caller) — без фильтра, даже если CONFIDENTIAL
      * promoted-from-self узлы — видны caller'у независимо от level
      * рёбра между видимыми + невидимыми узлами — отфильтрованы
        (не leak'аем «есть связь с чем-то невидимым»)
"""
from __future__ import annotations

import asyncio
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
_gv = _force_load("backend.core.store.graph_view", "backend/core/store/graph_view.py")
_gb_mod = _force_load("backend.core.store.graph_builder", "backend/core/store/graph_builder.py")

_store_pkg = sys.modules["backend.core.store"]
setattr(_store_pkg, "tenant_paths", _tp)
setattr(_store_pkg, "graph_view", _gv)
setattr(_store_pkg, "graph_builder", _gb_mod)

from backend.core.access import levels  # noqa: E402
from backend.core.ingest import membership  # noqa: E402


@pytest.fixture(autouse=True)
def _restore():
    for name, mod in _REAL.items():
        sys.modules[name] = mod
    yield


@pytest.fixture(autouse=True)
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setattr(_tp, "_DATA_ROOT", tmp_path)
    membership.clear_cache()
    return tmp_path


def _run(coro):
    return asyncio.run(coro)


def _uid() -> str:
    return str(uuid.uuid4())


# =============================================================================
# AccessLevel constants + max_access_level_for_user
# =============================================================================


def test_access_level_constants_order():
    """Шкала монотонна: PUBLIC < INTERNAL_LOW < INTERNAL < CONFIDENTIAL < RESTRICTED."""
    assert levels.AccessLevel.PUBLIC < levels.AccessLevel.INTERNAL_LOW
    assert levels.AccessLevel.INTERNAL_LOW < levels.AccessLevel.INTERNAL
    assert levels.AccessLevel.INTERNAL < levels.AccessLevel.CONFIDENTIAL
    assert levels.AccessLevel.CONFIDENTIAL < levels.AccessLevel.RESTRICTED


def test_max_access_level_for_each_role(tmp_data):
    org = _uid()

    contractor = _uid()
    employee = _uid()
    manager = _uid()
    admin = _uid()
    founder = _uid()

    membership.add_member(contractor, org, role="contractor")
    membership.add_member(employee, org, role="employee")
    membership.add_member(manager, org, role="manager")
    membership.add_member(admin, org, role="admin")
    membership.add_member(founder, org, role="founder")

    assert levels.max_access_level_for_user(contractor) == levels.AccessLevel.INTERNAL_LOW
    assert levels.max_access_level_for_user(employee) == levels.AccessLevel.INTERNAL
    assert levels.max_access_level_for_user(manager) == levels.AccessLevel.CONFIDENTIAL
    assert levels.max_access_level_for_user(admin) == levels.AccessLevel.RESTRICTED
    # P2 мульти-аккаунта: «генеральный видит всё» — founder поднят до
    # TOP_SECRET(5); шкала выровнена со schema.py (5 = только CEO).
    assert levels.max_access_level_for_user(founder) == levels.AccessLevel.TOP_SECRET


def test_max_access_level_solo_user_is_public_only(tmp_data):
    """Solo user (без org) → fail-safe: только PUBLIC."""
    assert levels.max_access_level_for_user(_uid()) == levels.AccessLevel.PUBLIC


def test_max_access_level_empty_user_is_public_only(tmp_data):
    assert levels.max_access_level_for_user("") == levels.AccessLevel.PUBLIC
    assert levels.max_access_level_for_user(None) == levels.AccessLevel.PUBLIC  # type: ignore


# =============================================================================
# node_visible_to
# =============================================================================


def test_owner_sees_own_node_regardless_of_level():
    """Свой узел видим всегда, даже если CONFIDENTIAL."""
    user = _uid()
    node = {"user_id": user, "access_level": levels.AccessLevel.RESTRICTED}
    assert levels.node_visible_to(node, user_id=user, max_level=levels.AccessLevel.PUBLIC)


def test_owner_sees_node_when_owner_in_created_by():
    """Owner может быть в created_by (legacy field) — тоже видит."""
    user = _uid()
    node = {"created_by": user, "access_level": levels.AccessLevel.RESTRICTED}
    assert levels.node_visible_to(node, user_id=user, max_level=levels.AccessLevel.PUBLIC)


def test_promoted_from_self_node_visible():
    """Узел promoted из моего personal в org — видим мне даже если высокий level."""
    me = _uid()
    other = _uid()
    node = {
        "user_id": other,  # tenant_id перебили на org-владельца при promote
        "promoted_from_user_id": me,
        "access_level": levels.AccessLevel.RESTRICTED,
    }
    assert levels.node_visible_to(node, user_id=me, max_level=levels.AccessLevel.PUBLIC)


def test_node_level_within_max_visible():
    other = _uid()
    me = _uid()
    node = {"user_id": other, "access_level": levels.AccessLevel.INTERNAL}
    assert levels.node_visible_to(node, user_id=me, max_level=levels.AccessLevel.INTERNAL)


def test_node_level_above_max_hidden():
    other = _uid()
    me = _uid()
    node = {"user_id": other, "access_level": levels.AccessLevel.CONFIDENTIAL}
    assert not levels.node_visible_to(node, user_id=me, max_level=levels.AccessLevel.INTERNAL)


def test_node_without_access_level_defaults_to_internal():
    """Узлы без явного access_level считаются INTERNAL (graph_builder дефолт)."""
    other = _uid()
    me = _uid()
    node = {"user_id": other}  # нет access_level
    assert levels.node_visible_to(node, user_id=me, max_level=levels.AccessLevel.INTERNAL)
    assert not levels.node_visible_to(node, user_id=me, max_level=levels.AccessLevel.INTERNAL_LOW)


def test_node_corrupt_access_level_fails_closed():
    """Битый access_level — fail-closed (не показываем)."""
    other = _uid()
    me = _uid()
    node = {"user_id": other, "access_level": "not_a_number"}
    assert not levels.node_visible_to(node, user_id=me, max_level=levels.AccessLevel.RESTRICTED)


def test_node_visible_to_uses_membership_when_max_not_provided(tmp_data):
    """Если max_level не передан — берётся из membership.role caller'а."""
    org = _uid()
    me = _uid()
    membership.add_member(me, org, role="employee")  # max = INTERNAL (2)
    other = _uid()
    node = {"user_id": other, "access_level": levels.AccessLevel.CONFIDENTIAL}
    assert not levels.node_visible_to(node, user_id=me)  # confidential скрыт


# =============================================================================
# Integration: merged_graph_view_for_user с RBAC
# =============================================================================


async def _seed_personal(user_id: str, nodes: list[dict]):
    from backend.core.store.graph_builder import GraphBuilder
    gb = GraphBuilder(use_networkx=True, graph_storage_path=_tp.graph_path_for_user(user_id))
    await gb.connect()
    for n in nodes:
        await gb.create_node(
            node_id=n["id"], label=n["label"],
            properties=n.get("props", {"user_id": user_id}),
        )
    await gb.save()
    await gb.close()


async def _seed_org(org_id: str, nodes: list[dict], edges: list = ()):
    from backend.core.store.graph_builder import GraphBuilder
    gb = GraphBuilder(use_networkx=True, graph_storage_path=_tp.org_graph_path(org_id))
    await gb.connect()
    for n in nodes:
        await gb.create_node(
            node_id=n["id"], label=n["label"],
            properties=n.get("props", {"tenant_id": org_id, "access_level": 2}),
        )
    for src, tgt, rel in edges:
        await gb.create_relationship(from_id=src, to_id=tgt, rel_type=rel)
    await gb.save()
    await gb.close()


def test_employee_sees_only_internal_and_below(tmp_data):
    user = _uid()
    org = _uid()
    membership.add_member(user, org, role="employee")

    _run(_seed_org(org, [
        {"id": "public_announcement", "label": "Decision",
         "props": {"tenant_id": org, "summary": "Q4 results",
                   "access_level": int(levels.AccessLevel.PUBLIC)}},
        {"id": "team_decision", "label": "Decision",
         "props": {"tenant_id": org, "summary": "ship X",
                   "access_level": int(levels.AccessLevel.INTERNAL)}},
        {"id": "exec_strategy", "label": "Decision",
         "props": {"tenant_id": org, "summary": "secret",
                   "access_level": int(levels.AccessLevel.CONFIDENTIAL)}},
        {"id": "hr_terminate", "label": "Decision",
         "props": {"tenant_id": org, "summary": "fire alice",
                   "access_level": int(levels.AccessLevel.RESTRICTED)}},
    ]))

    view = _run(_gv.merged_graph_view_for_user(user))
    try:
        ids = set(view.nx_graph.nodes())
        assert "public_announcement" in ids
        assert "team_decision" in ids
        assert "exec_strategy" not in ids
        assert "hr_terminate" not in ids
    finally:
        _run(view.close(save=False))


def test_founder_sees_everything(tmp_data):
    user = _uid()
    org = _uid()
    membership.add_member(user, org, role="founder")

    _run(_seed_org(org, [
        {"id": f"node_{lvl}", "label": "Decision",
         "props": {"tenant_id": org, "summary": "x",
                   "access_level": int(lvl)}}
        for lvl in (
            levels.AccessLevel.PUBLIC,
            levels.AccessLevel.INTERNAL,
            levels.AccessLevel.CONFIDENTIAL,
            levels.AccessLevel.RESTRICTED,
        )
    ]))

    view = _run(_gv.merged_graph_view_for_user(user))
    try:
        ids = set(view.nx_graph.nodes())
        assert len(ids) == 4  # founder видит всё
    finally:
        _run(view.close(save=False))


def test_personal_node_owner_sees_own_confidential(tmp_data):
    """REGRESSION: свой draft с CONFIDENTIAL — видим даже если employee."""
    user = _uid()
    org = _uid()
    membership.add_member(user, org, role="employee")

    _run(_seed_personal(user, [
        {"id": "my_secret", "label": "Decision",
         "props": {"user_id": user, "summary": "my draft",
                   "access_level": int(levels.AccessLevel.RESTRICTED)}},
    ]))

    view = _run(_gv.merged_graph_view_for_user(user))
    try:
        assert "my_secret" in view.nx_graph.nodes()  # свой узел — не фильтруется
    finally:
        _run(view.close(save=False))


def test_promoted_from_self_visible_after_promote(tmp_data):
    """REGRESSION: после promote из personal в org — owner всё ещё видит."""
    user = _uid()
    org = _uid()
    membership.add_member(user, org, role="employee")

    _run(_seed_org(org, [
        # Имитируем promoted узел: tenant_id=org, но promoted_from_user_id=me.
        {"id": "my_promoted_decision", "label": "Decision",
         "props": {
             "tenant_id": org,
             "user_id": "system",  # после promote owner = system, нет cleanup
             "promoted_from_user_id": user,
             "summary": "my high-stake decision",
             "access_level": int(levels.AccessLevel.RESTRICTED),
         }},
    ]))

    view = _run(_gv.merged_graph_view_for_user(user))
    try:
        assert "my_promoted_decision" in view.nx_graph.nodes()
    finally:
        _run(view.close(save=False))


def test_edge_filtered_when_one_endpoint_hidden(tmp_data):
    """RBAC-edge gate: ребро между видимым и невидимым → скрыто,
    чтобы не leak'нуть «есть связь с чем-то невидимым»."""
    user = _uid()
    org = _uid()
    membership.add_member(user, org, role="employee")

    _run(_seed_org(org, [
        {"id": "visible_decision", "label": "Decision",
         "props": {"tenant_id": org, "summary": "x",
                   "access_level": int(levels.AccessLevel.INTERNAL)}},
        {"id": "hidden_decision", "label": "Decision",
         "props": {"tenant_id": org, "summary": "secret",
                   "access_level": int(levels.AccessLevel.RESTRICTED)}},
    ], edges=[
        ("visible_decision", "hidden_decision", "RELATED_TO"),
    ]))

    view = _run(_gv.merged_graph_view_for_user(user))
    try:
        # Видимый узел в графе, скрытый — нет.
        assert "visible_decision" in view.nx_graph.nodes()
        assert "hidden_decision" not in view.nx_graph.nodes()
        # И ребро тоже не должно быть (одна из endpoint'ов hidden).
        assert not view.nx_graph.has_edge("visible_decision", "hidden_decision")
    finally:
        _run(view.close(save=False))


def test_solo_user_sees_only_public_org_nodes(tmp_data):
    """Solo user случайно видит org_id — не имеет права видеть нет PUBLIC.
    Это защита от orphaned/cross-org leak."""
    user = _uid()  # не добавлен ни в одну org
    # У него нет org_id в mapping → merged_view даже не подгрузит org-граф.
    # Этот тест дублирует логику Wave 2.1, но через P1 #5 prism:
    # max_access_level = PUBLIC и так не видит CONFIDENTIAL, даже если бы
    # cross-org leak случился.
    _run(_seed_personal(user, [
        {"id": "my_note", "label": "Decision",
         "props": {"user_id": user, "summary": "personal",
                   "access_level": int(levels.AccessLevel.RESTRICTED)}},
    ]))

    view = _run(_gv.merged_graph_view_for_user(user))
    try:
        ids = set(view.nx_graph.nodes())
        assert ids == {"my_note"}  # только своё, без org
    finally:
        _run(view.close(save=False))
