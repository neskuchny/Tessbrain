# -*- coding: utf-8 -*-
"""Орг-слой skill_store (перенос из QM): propose → approve → навык виден
всей организации; reject убирает предложение; чужой навык предложить
нельзя; личный навык затеняет орг-навык. Без сети/БД: организация
резолвится инъектируемым org_resolver-стабом."""
from __future__ import annotations

from backend.core.hermes.skill_store import SkillDoc, SkillStore

ORG = "org-1"
AUTHOR = "alice"
MEMBER = "bob"
ADMIN = "carol"


def _org_resolver(user_id):
    """Стаб membership.get_org_for_user: все трое — в одной орге."""
    return ORG if user_id in {AUTHOR, MEMBER, ADMIN} else None


def _store(tmp_path, **kw):
    kw.setdefault("org_resolver", _org_resolver)
    kw.setdefault("clock", lambda: "2026-08-02T00:00:00+00:00")
    return SkillStore(str(tmp_path), **kw)


def _doc(name="deploy-checklist", category="ops"):
    return SkillDoc(
        name=name, description="как деплоить", category=category,
        when_to_use="перед деплоем", procedure="1. проверь CI\n2. деплой",
        pitfalls="не деплоить в пятницу", verification="прод жив",
    )


def test_propose_approve_visible_to_whole_org(tmp_path):
    s = _store(tmp_path)
    assert s.create(AUTHOR, _doc())
    assert s.propose_to_org(AUTHOR, ORG, "deploy-checklist")

    # до одобрения — в pending, но не в орг-слое и не у другого члена
    assert [d.name for d in s.list_pending(ORG)] == ["deploy-checklist"]
    assert s.list_org(ORG) == []
    assert all(m.name != "deploy-checklist" for m in s.list(MEMBER))

    assert s.approve(ORG, "ops", "deploy-checklist", ADMIN)

    # появился в list() автора И другого члена орги
    assert any(m.name == "deploy-checklist" for m in s.list(AUTHOR))
    assert any(m.name == "deploy-checklist" for m in s.list(MEMBER))
    # pending опустел, орг-слой заполнен
    assert s.list_pending(ORG) == []
    assert [m.name for m in s.list_org(ORG)] == ["deploy-checklist"]


def test_approved_doc_carries_shared_by_and_approved_by(tmp_path):
    s = _store(tmp_path)
    s.create(AUTHOR, _doc())
    s.propose_to_org(AUTHOR, ORG, "deploy-checklist")

    pend = s.list_pending(ORG)[0]
    assert pend.shared_by == AUTHOR
    assert pend.approved_by == ""

    s.approve(ORG, "ops", "deploy-checklist", ADMIN)
    doc = s.view_org(ORG, "deploy-checklist")
    assert doc is not None
    assert doc.shared_by == AUTHOR
    assert doc.approved_by == ADMIN
    assert doc.approved_at == "2026-08-02T00:00:00+00:00"
    # тело навыка доехало без потерь
    assert doc.procedure == "1. проверь CI\n2. деплой"


def test_reject_removes_pending(tmp_path):
    s = _store(tmp_path)
    s.create(AUTHOR, _doc())
    s.propose_to_org(AUTHOR, ORG, "deploy-checklist")
    assert s.reject(ORG, "ops", "deploy-checklist")
    assert s.list_pending(ORG) == []
    assert s.list_org(ORG) == []
    # повторный reject — честный False, не исключение
    assert s.reject(ORG, "ops", "deploy-checklist") is False


def test_cannot_propose_foreign_skill(tmp_path):
    s = _store(tmp_path)
    s.create(AUTHOR, _doc())
    # у Боба нет ЛИЧНОГО навыка с таким именем — предлагать нечего
    assert s.propose_to_org(MEMBER, ORG, "deploy-checklist") is False
    # и чужое pending-предложение перетереть нельзя
    assert s.propose_to_org(AUTHOR, ORG, "deploy-checklist")
    s.create(MEMBER, _doc())  # Боб завёл одноимённый личный навык
    assert s.propose_to_org(MEMBER, ORG, "deploy-checklist") is False
    # своё же предложение автор перезаписать может
    assert s.propose_to_org(AUTHOR, ORG, "deploy-checklist")


def test_personal_skill_shadows_org_skill(tmp_path):
    s = _store(tmp_path)
    s.create(AUTHOR, _doc(name="report", category="ops"))
    s.propose_to_org(AUTHOR, ORG, "report")
    s.approve(ORG, "ops", "report", ADMIN)

    # у Боба свой «report» — в list() он один и это ЛИЧНАЯ версия
    personal = _doc(name="report", category="general")
    personal.description = "личная версия Боба"
    s.create(MEMBER, personal)
    metas = [m for m in s.list(MEMBER) if m.name == "report"]
    assert len(metas) == 1
    assert metas[0].description == "личная версия Боба"

    # у автора без личного «report» после его удаления — орг-версия
    s.delete(AUTHOR, "report")
    metas = [m for m in s.list(AUTHOR) if m.name == "report"]
    assert len(metas) == 1
    assert metas[0].description == "как деплоить"


def test_view_falls_back_to_org_layer(tmp_path):
    s = _store(tmp_path)
    s.create(AUTHOR, _doc())
    s.propose_to_org(AUTHOR, ORG, "deploy-checklist")
    s.approve(ORG, "ops", "deploy-checklist", ADMIN)
    # у Боба личного нет — view() отдаёт орг-навык (list его показывает,
    # значит открыть обязан)
    doc = s.view(MEMBER, "deploy-checklist")
    assert doc is not None and doc.approved_by == ADMIN


def test_no_org_resolver_and_no_backend_is_honest_empty(tmp_path):
    # без org_resolver и без membership (юнит-окружение): list() отдаёт
    # только личные, ничего не падает
    s = SkillStore(str(tmp_path))
    s.create(AUTHOR, _doc())
    assert [m.name for m in s.list(AUTHOR)] == ["deploy-checklist"]
    assert s.list_org(ORG) == []
    assert s.propose_to_org(AUTHOR, ORG, "нет-такого") is False
