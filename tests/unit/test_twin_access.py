# -*- coding: utf-8 -*-
"""Приватность слепка: журнал обращений + гейт согласия.

Проверяем ровно те инварианты, ради которых это писалось:
  - обращение видно владельцу слепка, как бы человека ни звали;
  - текст вопроса не утекает в журнал;
  - по умолчанию поведение не меняется (флаг выключен);
  - при включённом флаге чужой слепок без согласия закрыт, свой — открыт.
"""
from __future__ import annotations

import json

import pytest

from backend.core.twin import access_log, policy


@pytest.fixture(autouse=True)
def _isolated_log_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TWIN_ACCESS_LOG_DIR", str(tmp_path / "twin_access"))
    yield


# ── журнал ──────────────────────────────────────────────────────────


def test_record_and_read_back():
    access_log.record(person_id="p:ivan", asker_uid="u-2", tenant_uid="u-2",
                      person_name="Иван Петров", question_chars=42)
    rows = access_log.list_for_person("p:ivan")
    assert len(rows) == 1
    assert rows[0]["asker_uid"] == "u-2"
    assert rows[0]["question_chars"] == 42
    assert rows[0]["granted"] is True


def test_question_text_never_stored(tmp_path):
    """Длина вопроса — да, сам вопрос — никогда."""
    secret = "как он отнесётся к моему переводу в другой отдел"
    access_log.record(person_id="p:ivan", asker_uid="u-2", tenant_uid="u-2",
                      question_chars=len(secret))
    rows = access_log.list_for_person("p:ivan")
    dumped = json.dumps(rows, ensure_ascii=False)
    assert "переводу" not in dumped
    assert rows[0]["question_chars"] == len(secret)


def test_person_key_is_normalized():
    """Слепок зовут по-разному — журнал должен быть один."""
    access_log.record(person_id="Иван Петров", asker_uid="u-2", tenant_uid="u-2")
    access_log.record(person_id="  иван   петров ", asker_uid="u-3",
                      tenant_uid="u-3")
    rows = access_log.list_for_person("ИВАН ПЕТРОВ")
    assert len(rows) == 2


def test_aliases_merge_id_and_name():
    """Звали по id узла и по имени — владелец видит оба обращения."""
    access_log.record(person_id="p:ivan", asker_uid="u-2", tenant_uid="u-2")
    access_log.record(person_id="Иван Петров", asker_uid="u-3", tenant_uid="u-3")
    rows = access_log.list_for_person("p:ivan", aliases=["Иван Петров"])
    assert {r["asker_uid"] for r in rows} == {"u-2", "u-3"}


def test_newest_first_and_limit():
    for i in range(5):
        access_log.record(person_id="p:ivan", asker_uid=f"u-{i}",
                          tenant_uid="t")
    rows = access_log.list_for_person("p:ivan", limit=2)
    assert len(rows) == 2
    assert rows[0]["ts"] >= rows[1]["ts"]


def test_summary_counts_denied():
    access_log.record(person_id="p:ivan", asker_uid="u-2", tenant_uid="t")
    access_log.record(person_id="p:ivan", asker_uid="u-3", tenant_uid="t",
                      granted=False, reason="consent_missing")
    s = access_log.summary_for_person("p:ivan")
    assert s["total"] == 2
    assert s["denied"] == 1
    assert s["last_7d"] == 2
    assert s["askers"][0]["count"] == 1


def test_empty_log_is_empty_not_error():
    assert access_log.list_for_person("p:nobody") == []
    assert access_log.summary_for_person("p:nobody")["total"] == 0


def test_record_never_raises_on_bad_input():
    """Журнал не имеет права уронить ответ слепка."""
    access_log.record(person_id="", asker_uid="u", tenant_uid="t")  # no-op
    access_log.record(person_id="p:x", asker_uid="u", tenant_uid="t",
                      question_chars=-5)
    assert access_log.list_for_person("p:x")[0]["question_chars"] == 0


# ── доступ к слепку: роли ───────────────────────────────────────────


@pytest.fixture
def org(monkeypatch):
    """Организация с иерархией:

        u-boss  (руководитель отдела продаж, админ орг)
          └── u-lead (руководитель группы, продажи)
                └── u-owner (владелец слепка p:ivan, продажи)
        u-peer   — коллега того же отдела, никому не подчиняется
        u-far    — другой отдел (маркетинг)
    """
    reports = {"u-owner": "u-lead", "u-lead": "u-boss"}
    depts = {"u-owner": "Продажи", "u-lead": "Продажи", "u-boss": "Продажи",
             "u-peer": "Продажи", "u-far": "Маркетинг"}

    monkeypatch.setattr(policy, "_owner_of_twin",
                        lambda person_id, org_id: "u-owner")
    monkeypatch.setattr("backend.core.ingest.membership.get_org_for_user",
                        lambda uid: "org-1")
    monkeypatch.setattr("backend.core.ingest.membership.get_user_org_reports_to",
                        lambda uid: reports.get(uid))
    monkeypatch.setattr("backend.core.ingest.membership.get_user_org_department",
                        lambda uid: depts.get(uid))
    monkeypatch.setattr("backend.core.ingest.membership.can_manage_org",
                        lambda uid, org_id: uid == "u-boss")
    monkeypatch.setattr(
        "backend.core.consent.consent_store.has_active_consent",
        lambda owner, **kw: False)
    return reports


def test_policy_open_by_default(monkeypatch):
    monkeypatch.delenv("TWIN_ACCESS_POLICY", raising=False)
    monkeypatch.delenv("TWIN_ASK_REQUIRE_CONSENT", raising=False)
    d = policy.check_twin_access(person_id="p:ivan", asker_uid="u-2")
    assert d.allowed is True
    assert d.reason == "policy_open"


def test_legacy_flag_maps_to_strict(monkeypatch):
    """Булев флаг первой версии продолжает работать как strict."""
    monkeypatch.delenv("TWIN_ACCESS_POLICY", raising=False)
    monkeypatch.setenv("TWIN_ASK_REQUIRE_CONSENT", "1")
    assert policy.access_policy() == policy.POLICY_STRICT


def test_direct_manager_always_allowed(org, monkeypatch):
    """Главное бизнес-требование: руководитель видит подчинённого."""
    monkeypatch.setenv("TWIN_ACCESS_POLICY", "strict")
    d = policy.check_twin_access(person_id="p:ivan", asker_uid="u-lead")
    assert d.allowed is True
    assert d.relation == "manager"


def test_manager_up_the_chain_allowed(org, monkeypatch):
    """Руководитель руководителя — тоже руководитель."""
    monkeypatch.setenv("TWIN_ACCESS_POLICY", "strict")
    d = policy.check_twin_access(person_id="p:ivan", asker_uid="u-boss")
    assert d.allowed is True
    assert d.relation == "manager"


def test_subordinate_cannot_ask_manager_twin(org, monkeypatch):
    """Обратное направление не работает: подчинённый ≠ руководитель."""
    monkeypatch.setenv("TWIN_ACCESS_POLICY", "strict")
    monkeypatch.setattr(policy, "_owner_of_twin",
                        lambda person_id, org_id: "u-lead")
    d = policy.check_twin_access(person_id="p:lead", asker_uid="u-owner")
    assert d.allowed is False
    assert d.reason == "consent_missing"


def test_org_admin_allowed(org, monkeypatch):
    monkeypatch.setenv("TWIN_ACCESS_POLICY", "strict")
    # u-boss и руководитель, и админ — проверим админа отдельно, вне цепочки
    monkeypatch.setattr("backend.core.ingest.membership.get_user_org_reports_to",
                        lambda uid: None)
    monkeypatch.setattr("backend.core.ingest.membership.can_manage_org",
                        lambda uid, org_id: uid == "u-admin")
    d = policy.check_twin_access(person_id="p:ivan", asker_uid="u-admin")
    assert d.allowed is True
    assert d.relation == "admin"


def test_self_always_allowed(org, monkeypatch):
    monkeypatch.setenv("TWIN_ACCESS_POLICY", "strict")
    d = policy.check_twin_access(person_id="p:ivan", asker_uid="u-owner")
    assert d.allowed is True
    assert d.relation == "self"


def test_team_policy_allows_same_department(org, monkeypatch):
    monkeypatch.setenv("TWIN_ACCESS_POLICY", "team")
    d = policy.check_twin_access(person_id="p:ivan", asker_uid="u-peer")
    assert d.allowed is True
    assert d.relation == "teammate"


def test_team_policy_blocks_other_department(org, monkeypatch):
    monkeypatch.setenv("TWIN_ACCESS_POLICY", "team")
    d = policy.check_twin_access(person_id="p:ivan", asker_uid="u-far")
    assert d.allowed is False
    assert d.reason == "consent_missing"


def test_strict_policy_blocks_teammate(org, monkeypatch):
    """В strict принадлежность к отделу уже не даёт доступа."""
    monkeypatch.setenv("TWIN_ACCESS_POLICY", "strict")
    d = policy.check_twin_access(person_id="p:ivan", asker_uid="u-peer")
    assert d.allowed is False


def test_consent_overrides_block(org, monkeypatch):
    monkeypatch.setenv("TWIN_ACCESS_POLICY", "strict")
    monkeypatch.setattr(
        "backend.core.consent.consent_store.has_active_consent",
        lambda owner, **kw: True)
    d = policy.check_twin_access(person_id="p:ivan", asker_uid="u-far")
    assert d.allowed is True
    assert d.reason == "consent_granted"


def test_external_person_allowed(org, monkeypatch):
    """Внешний человек без учётки: согласие давать некому — не блокируем."""
    monkeypatch.setenv("TWIN_ACCESS_POLICY", "strict")
    monkeypatch.setattr(policy, "_owner_of_twin", lambda person_id, org_id: None)
    d = policy.check_twin_access(person_id="p:client", asker_uid="u-far")
    assert d.allowed is True
    assert d.relation == "external"


def test_consent_does_not_require_exchange_flag(monkeypatch, tmp_path):
    """Внутренний гейт не зависит от флага ВНЕШНЕГО обмена.

    consent_store.require_person_consent завязан на ENABLE_PROFILE_EXCHANGE —
    если бы мы звали его, внутренняя проверка падала бы всегда. Здесь
    работаем с НАСТОЯЩИМ хранилищем согласий, поэтому фикстура org с её
    заглушкой намеренно не используется."""
    monkeypatch.setenv("TWIN_ACCESS_POLICY", "strict")
    monkeypatch.delenv("ENABLE_PROFILE_EXCHANGE", raising=False)
    monkeypatch.setenv("CONSENTS_DIR", str(tmp_path / "consents"))

    monkeypatch.setattr(policy, "_owner_of_twin",
                        lambda person_id, org_id: "u-owner")
    monkeypatch.setattr("backend.core.ingest.membership.get_org_for_user",
                        lambda uid: "org-1")
    monkeypatch.setattr("backend.core.ingest.membership.get_user_org_reports_to",
                        lambda uid: None)
    monkeypatch.setattr("backend.core.ingest.membership.can_manage_org",
                        lambda uid, org_id: False)

    from backend.core.consent import consent_store
    d = policy.check_twin_access(person_id="p:ivan", asker_uid="u-far")
    assert d.allowed is False, "без согласия доступа быть не должно"

    consent_store.grant("u-owner", grantee="u-far", days=30)
    d = policy.check_twin_access(person_id="p:ivan", asker_uid="u-far")
    assert d.allowed is True, "согласие выдано — внутренний доступ должен быть"
    assert d.reason == "consent_granted"


def test_fails_open_when_consent_store_unavailable(org, monkeypatch):
    """Сбой чтения согласий не должен останавливать работу продукта."""
    monkeypatch.setenv("TWIN_ACCESS_POLICY", "strict")

    def _boom(*a, **kw):
        raise OSError("storage down")

    monkeypatch.setattr(
        "backend.core.consent.consent_store.has_active_consent", _boom)
    d = policy.check_twin_access(person_id="p:ivan", asker_uid="u-far")
    assert d.allowed is True
    assert d.reason == "consent_store_unavailable"


def test_manager_chain_survives_cycle(monkeypatch):
    """Цикл в данных (A→B→A) не должен вешать резолв."""
    loop = {"a": "b", "b": "a"}
    monkeypatch.setattr("backend.core.ingest.membership.get_user_org_reports_to",
                        lambda uid: loop.get(uid))
    chain = policy.manager_chain("a", "org-1")
    assert chain == ["b"]


def test_no_org_is_not_blocked(monkeypatch):
    """Одиночный аккаунт без организации: иерархии и согласий нет."""
    monkeypatch.setenv("TWIN_ACCESS_POLICY", "strict")
    monkeypatch.setattr("backend.core.ingest.membership.get_org_for_user",
                        lambda uid: None)
    d = policy.check_twin_access(person_id="p:ivan", asker_uid="u-solo")
    assert d.allowed is True
    assert d.reason == "no_org"
