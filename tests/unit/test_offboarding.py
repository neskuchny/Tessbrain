# -*- coding: utf-8 -*-
"""Уход сотрудника: подчинённые, слепок, согласия.

Три хвоста, которые раньше оставались после `remove_member`:
подчинённые повисали на несуществующем руководителе, слепок продолжал
говорить от лица ушедшего, выданные им согласия оставались активными.
"""
from __future__ import annotations

import pytest

from backend.core.ingest import offboarding as OB
from backend.core.twin import policy


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("TWIN_FROZEN_DIR", str(tmp_path / "frozen"))
    monkeypatch.setenv("CONSENTS_DIR", str(tmp_path / "consents"))
    yield


@pytest.fixture
def org(monkeypatch):
    """u-boss ← u-lead ← {u-a, u-b}; у u-lead сшивка с p:lead."""
    state = {
        "reports": {"u-lead": "u-boss", "u-a": "u-lead", "u-b": "u-lead"},
        "persons": {"u-lead": "p:lead", "u-a": "p:a"},
        "removed": [],
    }

    def _members(org_id):
        return [{"user_id": uid, "reports_to": state["reports"].get(uid),
                 "person_entity_id": state["persons"].get(uid), "role": "member"}
                for uid in ("u-boss", "u-lead", "u-a", "u-b")]

    def _set_reports_to(uid, org_id, rt):
        if rt:
            state["reports"][uid] = rt
        else:
            state["reports"].pop(uid, None)
        return True

    def _remove(uid, org_id):
        state["removed"].append(uid)
        return True

    m = "backend.core.ingest.membership"
    monkeypatch.setattr(f"{m}.list_members", _members)
    monkeypatch.setattr(f"{m}.set_member_reports_to", _set_reports_to)
    monkeypatch.setattr(f"{m}.remove_member", _remove)
    monkeypatch.setattr(f"{m}.get_user_org_reports_to",
                        lambda uid: state["reports"].get(uid))
    monkeypatch.setattr(f"{m}.get_user_person_entity",
                        lambda uid: state["persons"].get(uid))
    monkeypatch.setattr(f"{m}.get_org_for_user", lambda uid: "org-1")
    monkeypatch.setattr(f"{m}.can_manage_org", lambda uid, o: uid == "u-boss")
    return state


# ── подчинённые ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reports_move_up_to_grand_manager(org):
    """Главное: команда ушедшего не повисает, а поднимается на уровень выше."""
    res = await OB.offboard("u-lead", "org-1", actor_id="u-boss")
    assert res["status"] == "success"
    moved = {r["user_id"]: r["new_manager"] for r in res["reassigned"]}
    assert moved == {"u-a": "u-boss", "u-b": "u-boss"}
    assert org["reports"]["u-a"] == "u-boss"
    assert res["orphaned"] == []


@pytest.mark.asyncio
async def test_explicit_reassign_target_wins(org):
    res = await OB.offboard("u-lead", "org-1", actor_id="u-boss",
                            reassign_reports_to="u-b")
    moved = {r["user_id"]: r["new_manager"] for r in res["reassigned"]}
    assert moved == {"u-a": "u-b"}
    # u-b нельзя подчинить самому себе — он честно помечен как осиротевший
    assert res["orphaned"] == ["u-b"]
    assert "u-b" not in org["reports"]


@pytest.mark.asyncio
async def test_top_person_leaves_reports_are_orphaned_not_hidden(org):
    """Уходит первое лицо — передать некому. Это должно быть видно."""
    org["reports"].pop("u-lead")          # у u-lead больше нет руководителя
    res = await OB.offboard("u-lead", "org-1", actor_id="u-boss")
    assert set(res["orphaned"]) == {"u-a", "u-b"}
    assert res["reassigned"] == []
    assert "u-a" not in org["reports"], "висячая ссылка должна быть снята"


@pytest.mark.asyncio
async def test_membership_removed_last(org):
    res = await OB.offboard("u-lead", "org-1", actor_id="u-boss")
    assert res["membership_removed"] is True
    assert org["removed"] == ["u-lead"]


@pytest.mark.asyncio
async def test_can_skip_membership_removal(org):
    """Заморозить слепок можно и без удаления из организации (декрет,
    длительный отпуск)."""
    res = await OB.offboard("u-lead", "org-1", actor_id="u-boss",
                            remove_membership=False)
    assert res["membership_removed"] is False
    assert org["removed"] == []
    assert res["twin_frozen"] is True


# ── слепок ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_twin_frozen_by_default(org):
    res = await OB.offboard("u-lead", "org-1", actor_id="u-boss")
    assert res["twin_frozen"] is True
    assert OB.is_twin_frozen("org-1", "p:lead") is True


@pytest.mark.asyncio
async def test_freeze_can_be_declined_explicitly(org):
    """«Человек ушёл, знания остались» — законный сценарий, но осознанный."""
    res = await OB.offboard("u-lead", "org-1", actor_id="u-boss", freeze=False)
    assert res["twin_frozen"] is False
    assert OB.is_twin_frozen("org-1", "p:lead") is False


@pytest.mark.asyncio
async def test_unlinked_person_nothing_to_freeze(org):
    """Аккаунт без сшивки: слепка нет — и врать про заморозку не надо."""
    res = await OB.offboard("u-b", "org-1", actor_id="u-boss")
    assert res["twin_frozen"] is False


def test_freeze_is_idempotent():
    assert OB.freeze_twin("org-1", "p:x", actor_id="a") is True
    assert OB.freeze_twin("org-1", "p:x", actor_id="a") is True
    assert len(OB.list_frozen("org-1")) == 1


def test_unfreeze_returns_false_when_not_frozen():
    assert OB.unfreeze_twin("org-1", "p:never") is False


def test_freeze_is_scoped_to_org():
    OB.freeze_twin("org-1", "p:x", actor_id="a")
    assert OB.is_twin_frozen("org-2", "p:x") is False
    assert OB.list_frozen("org-2") == []


def test_person_key_normalized():
    OB.freeze_twin("org-1", "P:Lead", actor_id="a")
    assert OB.is_twin_frozen("org-1", "p:lead") is True


def test_frozen_info_records_who_and_why():
    OB.freeze_twin("org-1", "p:x", actor_id="u-boss", reason="offboarding")
    info = OB.frozen_twin_info("org-1", "p:x")
    assert info["actor_id"] == "u-boss"
    assert info["reason"] == "offboarding"
    assert info["frozen_at"] > 0


# ── заморозка сильнее прав ──────────────────────────────────────────


def test_frozen_twin_silent_even_for_manager(org, monkeypatch):
    """Заморозка перебивает роль: слепок ушедшего молчит и для начальника."""
    monkeypatch.setenv("TWIN_ACCESS_POLICY", "strict")
    monkeypatch.setattr(policy, "_owner_of_twin",
                        lambda person_id, org_id: "u-lead")
    OB.freeze_twin("org-1", "p:lead", actor_id="u-boss")

    d = policy.check_twin_access(person_id="p:lead", asker_uid="u-boss")
    assert d.allowed is False
    assert d.reason == "twin_frozen"


def test_frozen_twin_silent_in_open_policy(org, monkeypatch):
    """Даже при политике open — заморозка это не про политику доступа."""
    monkeypatch.setenv("TWIN_ACCESS_POLICY", "open")
    OB.freeze_twin("org-1", "p:lead", actor_id="u-boss")
    d = policy.check_twin_access(person_id="p:lead", asker_uid="u-a")
    assert d.allowed is False
    assert d.reason == "twin_frozen"


def test_unfrozen_twin_answers_again(org, monkeypatch):
    monkeypatch.setenv("TWIN_ACCESS_POLICY", "open")
    OB.freeze_twin("org-1", "p:lead", actor_id="u-boss")
    OB.unfreeze_twin("org-1", "p:lead")
    d = policy.check_twin_access(person_id="p:lead", asker_uid="u-a")
    assert d.allowed is True


def test_other_twins_unaffected_by_freeze(org, monkeypatch):
    monkeypatch.setenv("TWIN_ACCESS_POLICY", "open")
    OB.freeze_twin("org-1", "p:lead", actor_id="u-boss")
    d = policy.check_twin_access(person_id="p:a", asker_uid="u-boss")
    assert d.allowed is True


# ── согласия ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_consents_revoked_on_leave(org):
    from backend.core.consent import consent_store
    consent_store.grant("u-lead", grantee="u-a", days=90)
    consent_store.grant("u-lead", grantee="u-b", days=90)

    res = await OB.offboard("u-lead", "org-1", actor_id="u-boss")
    assert res["consents_revoked"] == 2
    assert consent_store.has_active_consent("u-lead", grantee="u-a") is False


@pytest.mark.asyncio
async def test_no_consents_is_zero_not_error(org):
    res = await OB.offboard("u-lead", "org-1", actor_id="u-boss")
    assert res["consents_revoked"] == 0
    assert res["status"] == "success"


# ── устойчивость ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_partial_status_when_step_fails(org, monkeypatch):
    """Один шаг упал — остальные всё равно делаются, статус честный."""
    monkeypatch.setattr("backend.core.ingest.membership.list_members",
                        lambda o: (_ for _ in ()).throw(OSError("db down")))
    res = await OB.offboard("u-lead", "org-1", actor_id="u-boss")
    assert res["status"] == "partial"
    assert res["twin_frozen"] is True, "заморозка не зависит от сбоя выше"
    assert res["membership_removed"] is True


@pytest.mark.asyncio
async def test_missing_ids_rejected():
    res = await OB.offboard("", "org-1", actor_id="a")
    assert res["status"] == "error"
