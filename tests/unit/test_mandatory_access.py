"""Unit-тесты для Mandatory / team-based access (P5).

Чистая логика: stub-пакеты + загрузка schema.py/ontology.sdk/
access.mandatory через importlib, обходя тяжёлый core.store.__init__.
Сеть не нужна — fetcher инъектируем.
"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import types

_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load(mod_name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(mod_name, _ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _bootstrap():
    for pkg in ("backend", "backend.core", "backend.core.store",
                "backend.core.ontology", "backend.core.access"):
        if pkg not in sys.modules:
            m = types.ModuleType(pkg)
            m.__path__ = []  # type: ignore[attr-defined]
            sys.modules[pkg] = m
    _load("backend.core.store.schema", "backend/core/store/schema.py")
    _load("backend.core.ontology.sdk", "backend/core/ontology/sdk.py")
    return _load("backend.core.access.mandatory",
                 "backend/core/access/mandatory.py")


_mac = _bootstrap()

EnforcementMode = _mac.EnforcementMode
TeamMembership = _mac.TeamMembership
build_membership = _mac.build_membership
can_see_node = _mac.can_see_node
filter_nodes = _mac.filter_nodes
load_team_membership = _mac.load_team_membership

OFF = EnforcementMode.OFF
WARN = EnforcementMode.WARN
STRICT = EnforcementMode.STRICT


def _run(c):
    return asyncio.run(c)


# === build_membership ===================================================

def test_clearance_from_role() -> None:
    assert build_membership("u", "viewer", None).clearance == 2
    assert build_membership("u", "manager", None).clearance == 3
    assert build_membership("u", "admin", None).clearance == 5
    assert build_membership("u", "unknown-role", None).clearance == 2


def test_team_rows_add_teams_and_can_raise_clearance() -> None:
    m = build_membership("u", "employee", [
        {"team_id": "finance", "clearance": 4},
        {"team_id": "eng"},
    ])
    assert m.team_ids == frozenset({"finance", "eng"})
    assert m.clearance == 4  # поднято командой
    assert m.source == "role+teams"


def test_team_never_lowers_below_role() -> None:
    m = build_membership("u", "admin", [{"team_id": "x", "clearance": 1}])
    assert m.clearance == 5  # роль admin не понижается командой


def test_build_membership_ignores_garbage_rows() -> None:
    m = build_membership("u", "employee", ["nope", {"no_team": 1}, 42])
    assert m.team_ids == frozenset()
    assert m.clearance == 2


# === can_see_node =======================================================

def _node(level, groups=None):
    p = {"access_level": level}
    if groups is not None:
        p["access_groups"] = groups
    return p


def test_off_mode_everything_visible() -> None:
    m = build_membership("u", "viewer", None)
    ok, _ = can_see_node(_node(5, ["secret"]), m, OFF)
    assert ok is True


def test_public_always_visible_even_strict() -> None:
    m = build_membership("u", "viewer", None)
    ok, _ = can_see_node(_node(1, ["any"]), m, STRICT)
    assert ok is True


def test_strict_blocks_above_clearance() -> None:
    m = build_membership("u", "employee", None)  # clearance 2
    ok, reason = can_see_node(_node(4), m, STRICT)
    assert ok is False and "clearance" in reason


def test_strict_blocks_when_not_in_access_group() -> None:
    m = build_membership("u", "admin", [{"team_id": "eng"}])  # clr 5
    ok, reason = can_see_node(_node(3, ["finance"]), m, STRICT)
    assert ok is False and "access_groups" in reason


def test_strict_allows_when_in_group_and_cleared() -> None:
    m = build_membership("u", "manager", [{"team_id": "finance"}])  # clr 3
    ok, _ = can_see_node(_node(3, ["finance", "legal"]), m, STRICT)
    assert ok is True


def test_empty_access_groups_means_only_clearance_matters() -> None:
    m = build_membership("u", "manager", None)  # clr 3
    ok, _ = can_see_node(_node(3, []), m, STRICT)
    assert ok is True


def test_warn_mode_observes_but_does_not_hide() -> None:
    m = build_membership("u", "viewer", None)  # clr 2
    ok, reason = can_see_node(_node(5, ["x"]), m, WARN)
    assert ok is True          # не режем
    assert reason != ""        # но фиксируем, что скрыли бы


def test_can_see_node_bad_access_level_defaults_safe() -> None:
    m = build_membership("u", "manager", None)  # clr 3
    ok, _ = can_see_node({"access_level": "junk"}, m, STRICT)
    assert ok is True  # junk → 2 <= 3


# === filter_nodes =======================================================

def test_filter_nodes_strict_hides_and_counts() -> None:
    m = build_membership("u", "employee", [{"team_id": "a"}])  # clr 2
    nodes = [
        _node(1),                       # public → visible
        _node(2, []),                   # cleared, no group → visible
        _node(4),                       # over clearance → hidden
        _node(2, ["a"]),                # in group → visible
        _node(2, ["b"]),                # not in group → hidden
        "garbage",                      # ignored
    ]
    vis, hidden = filter_nodes(nodes, m, STRICT)
    assert hidden == 2
    assert len(vis) == 3


def test_filter_nodes_off_returns_input_untouched() -> None:
    m = build_membership("u", "viewer", None)
    nodes = [_node(5, ["x"])]
    vis, hidden = filter_nodes(nodes, m, OFF)
    assert vis == nodes and hidden == 0


def test_filter_nodes_non_list_safe() -> None:
    m = build_membership("u", "viewer", None)
    vis, hidden = filter_nodes("not a list", m, STRICT)  # type: ignore[arg-type]
    assert vis == [] and hidden == 0


# === load_team_membership (инъектируемый fetcher) =======================

def test_load_off_mode_skips_fetch() -> None:
    called = []

    async def fetcher(uid):
        called.append(uid)
        return [{"team_id": "x"}]

    m = _run(load_team_membership("u", "employee", fetcher=fetcher, mode=OFF))
    assert called == []                 # OFF не дёргает сеть
    assert m.team_ids == frozenset()


def test_load_strict_uses_fetcher() -> None:
    async def fetcher(uid):
        return [{"team_id": "finance", "clearance": 4}]

    m = _run(load_team_membership("u", "employee", fetcher=fetcher, mode=STRICT))
    assert "finance" in m.team_ids and m.clearance == 4
    assert m.source == "role+teams"


def test_load_fetch_failure_is_fail_safe() -> None:
    async def boom(uid):
        raise RuntimeError("supabase down")

    m = _run(load_team_membership("u", "manager", fetcher=boom, mode=STRICT))
    # не raises; членство по роли, помечено unavailable
    assert m.clearance == 3
    assert m.team_ids == frozenset()
    assert m.source == "unavailable"


def test_load_empty_rows_marked_unavailable() -> None:
    async def empty(uid):
        return []

    m = _run(load_team_membership("u", "viewer", fetcher=empty, mode=WARN))
    assert m.source == "unavailable"
    assert m.clearance == 2


def test_membership_to_dict_shape() -> None:
    m = build_membership("u", "manager", [{"team_id": "z"}])
    d = m.to_dict()
    assert d["clearance"] == 3 and d["team_ids"] == ["z"]
