# -*- coding: utf-8 -*-
"""«Мозг компании целиком»: статистика через призму прав зрителя +
strict-дефолт NULL-легаси (гигиена)."""
from __future__ import annotations

from backend.core.access.levels import AccessLevel
from backend.core.access.org_stats import org_brain_stats

NODES = [
    {"_label": "Meeting", "access_level": 2, "department": "Продажи"},
    {"_label": "Decision", "access_level": 2, "department": "Продажи"},
    {"_label": "Document", "access_level": 2, "department": "Инженерия"},
    {"_label": "Meeting", "access_level": 4},                    # HR/финансы
    {"_label": "Decision", "access_level": 5},                   # только CEO
]


def test_employee_sees_own_department_only():
    st = org_brain_stats(NODES, user_id="emp", max_level=int(AccessLevel.INTERNAL),
                         viewer_department="Продажи")
    assert st["total_nodes"] == 5
    assert st["visible_nodes"] == 2          # только свой отдел, уровень ≤2
    assert st["hidden_nodes"] == 3
    assert st["by_department"] == {"Продажи": 2}


def test_manager_sees_cross_department():
    st = org_brain_stats(NODES, user_id="mgr", max_level=int(AccessLevel.CONFIDENTIAL),
                         viewer_department="Продажи")
    assert st["visible_nodes"] == 3          # + чужой отдел (порог 3), но не 4/5


def test_founder_sees_everything_including_level5():
    st = org_brain_stats(NODES, user_id="ceo", max_level=int(AccessLevel.TOP_SECRET),
                         viewer_department=None)
    assert st["visible_nodes"] == 5 and st["hidden_nodes"] == 0
    assert st["by_level"].get("5") == 1


def test_empty_brain_is_honest():
    st = org_brain_stats([], user_id="x", max_level=5, viewer_department=None)
    assert st == {"total_nodes": 0, "visible_nodes": 0, "hidden_nodes": 0,
                  "by_type": {}, "by_department": {}, "by_level": {}}


# ── strict-дефолт NULL-легаси ────────────────────────────────────────────

def test_tenant_strict_default_flag(monkeypatch):
    from backend.core.store import graph_builder as gb
    legacy = {"tenant_id": None}
    mine = {"tenant_id": "t1"}

    monkeypatch.delenv("TENANT_STRICT_DEFAULT", raising=False)
    assert gb.GraphBuilder._tenant_networkx_pass(legacy, "t1") is True   # старое поведение
    assert "IS NULL" in gb.GraphBuilder._tenant_cypher_clause()

    monkeypatch.setenv("TENANT_STRICT_DEFAULT", "on")
    assert gb.GraphBuilder._tenant_networkx_pass(legacy, "t1") is False  # легаси скрыт
    assert gb.GraphBuilder._tenant_networkx_pass(mine, "t1") is True     # своё видно
    assert "IS NULL" not in gb.GraphBuilder._tenant_cypher_clause()
