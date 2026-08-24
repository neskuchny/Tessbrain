# -*- coding: utf-8 -*-
"""P2 мульти-аккаунта: уровень 5 (генеральный видит всё) + видимость по
отделам (чужой отдел — только с уровня руководителя)."""
from __future__ import annotations

import uuid

import pytest

import backend.core.access.levels as levels
import backend.core.ingest.membership as membership


def _uid() -> str:
    return str(uuid.uuid4())


@pytest.fixture()
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setattr(membership, "_user_org_mapping_path",
                        lambda: str(tmp_path / "user_org_mapping.json"))
    membership.clear_cache()
    yield tmp_path
    membership.clear_cache()


# ── Уровень 5: «генеральный видит всё» ────────────────────────────────────

def test_founder_sees_top_secret_admin_does_not(tmp_data):
    org = "org-" + _uid()
    founder, admin = _uid(), _uid()
    membership.add_member(founder, org, role="founder")
    membership.add_member(admin, org, role="admin")
    node = {"access_level": int(levels.AccessLevel.TOP_SECRET)}  # 5, M&A
    assert levels.node_visible_to(node, user_id=founder) is True
    assert levels.node_visible_to(node, user_id=admin) is False


# ── Отделы ────────────────────────────────────────────────────────────────

def test_same_department_sees_other_department_needs_manager(tmp_data):
    org = "org-" + _uid()
    sales_emp, eng_emp, eng_mgr = _uid(), _uid(), _uid()
    membership.add_member(sales_emp, org, role="employee")
    membership.set_member_department(sales_emp, org, "Продажи")
    membership.add_member(eng_emp, org, role="employee")
    membership.set_member_department(eng_emp, org, "Инженерия")
    membership.add_member(eng_mgr, org, role="manager")
    membership.set_member_department(eng_mgr, org, "Продажи")

    node = {"access_level": int(levels.AccessLevel.INTERNAL),  # обычный, 2
            "department": "Инженерия"}
    # свой отдел — видит по обычным правилам
    assert levels.node_visible_to(node, user_id=eng_emp) is True
    # чужой отдел, уровень employee(2) < CONFIDENTIAL(3) → скрыт
    assert levels.node_visible_to(node, user_id=sales_emp) is False
    # руководитель (3) видит чужой отдел
    assert levels.node_visible_to(node, user_id=eng_mgr) is True


def test_department_case_insensitive_and_founder_bypasses(tmp_data):
    org = "org-" + _uid()
    founder, emp = _uid(), _uid()
    membership.add_member(founder, org, role="founder")
    membership.add_member(emp, org, role="employee")
    membership.set_member_department(emp, org, "маркетинг")

    node = {"access_level": 2, "department": "Маркетинг"}  # регистр другой
    assert levels.node_visible_to(node, user_id=emp) is True     # тот же отдел
    assert levels.node_visible_to(node, user_id=founder) is True  # founder видит всё


def test_node_without_department_uses_plain_rules(tmp_data):
    org = "org-" + _uid()
    emp = _uid()
    membership.add_member(emp, org, role="employee")
    membership.set_member_department(emp, org, "Продажи")
    assert levels.node_visible_to({"access_level": 2}, user_id=emp) is True


def test_owner_and_promoter_see_own_regardless_of_department(tmp_data):
    org = "org-" + _uid()
    emp = _uid()
    membership.add_member(emp, org, role="employee")
    membership.set_member_department(emp, org, "Продажи")
    node = {"access_level": 4, "department": "Инженерия",
            "promoted_from_user_id": emp}
    assert levels.node_visible_to(node, user_id=emp) is True  # свой промоушен


def test_precomputed_viewer_department_batch_path(tmp_data):
    # merged view передаёт отдел зрителя явно — membership не дёргается
    node = {"access_level": 2, "department": "Инженерия"}
    assert levels.node_visible_to(
        node, user_id=_uid(), max_level=2, viewer_department="Инженерия") is True
    assert levels.node_visible_to(
        node, user_id=_uid(), max_level=2, viewer_department="Продажи") is False


# ── membership: отдел ────────────────────────────────────────────────────

def test_membership_department_roundtrip_and_readd_preserves(tmp_data):
    org = "org-" + _uid()
    emp = _uid()
    membership.add_member(emp, org, role="employee")
    assert membership.get_user_org_department(emp) is None
    assert membership.set_member_department(emp, org, "Маркетинг") is True
    assert membership.get_user_org_department(emp) == "Маркетинг"
    # идемпотентный re-add (смена роли) не теряет отдел
    membership.add_member(emp, org, role="manager")
    assert membership.get_user_org_department(emp) == "Маркетинг"
    # сброс
    assert membership.set_member_department(emp, org, "") is True
    assert membership.get_user_org_department(emp) is None
    # не член этой орги → False
    assert membership.set_member_department(_uid(), org, "X") is False
    # отдел виден в list_members
    membership.set_member_department(emp, org, "Ops")
    row = [m for m in membership.list_members(org) if m["user_id"] == emp][0]
    assert row["department"] == "Ops"
