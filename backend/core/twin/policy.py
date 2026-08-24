# -*- coding: utf-8 -*-
"""Кто имеет право спрашивать слепок человека — внутри организации.

Отличие от backend/core/consent: тот контур про ВНЕШНИЙ обмен профилями
между компаниями и жёстко завязан на ENABLE_PROFILE_EXCHANGE. Здесь другой
сценарий — люди одной организации. Механика согласий переиспользуется
(хранилище то же, человек управляет списком из своего кабинета), но гейт
отдельный: включение внешнего обмена не должно быть условием внутренней
проверки, и наоборот.

Модель прав построена от рабочих потребностей, а не от абстрактной
приватности. Слепок — инструмент работы, и запирать его от тех, кому он
нужен по должности, значит сломать продукт ради галочки:

  - СВОЙ слепок — всегда. Это твои же данные.
  - РУКОВОДИТЕЛЬ (прямой и любой выше по цепочке reports_to) — всегда.
    Готовится к разговору, человек в отпуске, решение по его проекту:
    это ровно та работа, ради которой слепки и делались.
  - АДМИНИСТРАТОР организации — всегда, по той же логике, что и остальной
    административный доступ.
  - ВНЕШНИЙ человек (партнёр, клиент, кандидат), не сшитый ни с одним
    аккаунтом — субъекта согласия не существует, спрашивать было бы нельзя
    никогда. Такие слепки открыты: запрет здесь защищал бы не человека,
    а никого.
  - ВСЕ ОСТАЛЬНЫЕ — зависит от политики организации (см. ниже).

Политика организации (TWIN_ACCESS_POLICY, по умолчанию `open`):

  open   — как было: слепок доступен всем внутри тенанта. Ничего не меняется
           у работающих клиентов.
  team   — коллеги своего отдела спрашивают свободно, люди из других
           отделов — только с согласия владельца. Разумная середина для
           компании, где отделы работают автономно.
  strict — за пределами «сам / руководитель / админ» нужно согласие.

Fail-open по инфраструктуре: если членство или хранилище согласий
недоступны, мы не знаем ни владельца, ни иерархию — и не блокируем.
Молча остановить работу продукта из-за сбоя чтения файла хуже, чем не
применить дополнительную проверку. Причина пропуска пишется в журнал
обращений, так что это видно, а не теряется.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)

# Насколько глубоко идём вверх по цепочке руководителей. Защита от цикла в
# данных (A подчиняется B, B подчиняется A) и от бесконечно длинной цепочки.
_MAX_CHAIN_DEPTH = 20

POLICY_OPEN = "open"
POLICY_TEAM = "team"
POLICY_STRICT = "strict"
_POLICIES = (POLICY_OPEN, POLICY_TEAM, POLICY_STRICT)


def access_policy() -> str:
    """Политика организации. По умолчанию `open` — поведение как было."""
    raw = os.environ.get("TWIN_ACCESS_POLICY", "").strip().lower()
    if raw in _POLICIES:
        return raw
    # Обратная совместимость: булев флаг из первой версии == strict.
    if os.environ.get("TWIN_ASK_REQUIRE_CONSENT", "").strip().lower() in (
            "1", "on", "true", "yes"):
        return POLICY_STRICT
    return POLICY_OPEN


@dataclass
class TwinAccessDecision:
    allowed: bool
    reason: str                       # машинный код причины, идёт в журнал
    owner_uid: Optional[str] = None   # аккаунт владельца слепка, если нашли
    relation: str = ""                # self/manager/admin/teammate/other
    message: str = ""                 # человеческое объяснение отказа


def _owner_of_twin(person_id: str, org_id: str) -> Optional[str]:
    """Аккаунт человека, чей это слепок, либо None.

    person_id снапшота — id Person-узла графа; в членстве он лежит как
    person_entity_id (сшивку «это я» подтверждает сам человек)."""
    try:
        from backend.core.ingest import membership
        return membership.find_user_by_person_entity(str(person_id), org_id)
    except Exception:
        logger.debug("twin policy: владелец слепка не резолвится", exc_info=True)
        return None


def manager_chain(user_id: str, org_id: str) -> List[str]:
    """Цепочка руководителей снизу вверх: [прямой, его руководитель, ...].

    Публичная — тем же деревом пользуется журнал обращений, чтобы решить,
    чьи слепки руководитель вправе смотреть."""
    out: List[str] = []
    try:
        from backend.core.ingest import membership
        seen = {str(user_id)}
        cur = str(user_id)
        for _ in range(_MAX_CHAIN_DEPTH):
            nxt = membership.get_user_org_reports_to(cur)
            nxt = str(nxt or "").strip()
            if not nxt or nxt in seen:
                break
            out.append(nxt)
            seen.add(nxt)
            cur = nxt
    except Exception:
        logger.debug("twin policy: цепочка руководителей не строится",
                     exc_info=True)
    return out


def is_manager_of(candidate_uid: str, subordinate_uid: str,
                  org_id: str) -> bool:
    """candidate — руководитель subordinate (прямой или выше по цепочке)."""
    if not candidate_uid or not subordinate_uid:
        return False
    return str(candidate_uid) in manager_chain(subordinate_uid, org_id)


def _same_department(a_uid: str, b_uid: str) -> bool:
    try:
        from backend.core.ingest import membership
        da = str(membership.get_user_org_department(a_uid) or "").strip().lower()
        db = str(membership.get_user_org_department(b_uid) or "").strip().lower()
        return bool(da) and da == db
    except Exception:
        logger.debug("twin policy: отдел не резолвится", exc_info=True)
        return False


def _is_org_admin(user_id: str, org_id: str) -> bool:
    try:
        from backend.core.ingest import membership
        return bool(org_id) and membership.can_manage_org(user_id, org_id)
    except Exception:
        logger.debug("twin policy: права админа не проверяются", exc_info=True)
        return False


def _has_consent(owner_uid: str, asker_uid: str) -> Optional[bool]:
    """True/False, либо None если хранилище согласий недоступно."""
    try:
        from backend.core.consent.consent_store import has_active_consent
        return bool(has_active_consent(owner_uid, grantee=asker_uid))
    except Exception:
        logger.debug("twin policy: согласия не читаются", exc_info=True)
        return None


def check_twin_access(*, person_id: str, asker_uid: str) -> TwinAccessDecision:
    """Можно ли asker_uid спросить слепок person_id."""
    try:
        from backend.core.ingest import membership
        org_id = membership.get_org_for_user(asker_uid)
    except Exception:
        logger.debug("twin policy: организация не резолвится", exc_info=True)
        org_id = None

    # Заморозка сильнее любой политики и любой роли: слепок ушедшего
    # человека молчит и для руководителя, и для админа. Снять заморозку —
    # отдельное осознанное действие, а не побочный эффект прав доступа.
    if org_id:
        try:
            from backend.core.ingest.offboarding import is_twin_frozen
            if is_twin_frozen(org_id, person_id):
                return TwinAccessDecision(
                    False, "twin_frozen", relation="frozen",
                    message=("Слепок заморожен: человек больше не работает в "
                             "организации. Разморозить может администратор — "
                             "это отдельное решение, а не право доступа."))
        except Exception:
            logger.debug("twin policy: реестр заморозок недоступен",
                         exc_info=True)

    policy = access_policy()
    if policy == POLICY_OPEN:
        return TwinAccessDecision(True, "policy_open")

    if not org_id:
        # Одиночный аккаунт без организации: иерархии и согласий нет.
        return TwinAccessDecision(True, "no_org")

    owner_uid = _owner_of_twin(person_id, org_id)
    if not owner_uid:
        return TwinAccessDecision(True, "no_linked_owner", relation="external")

    if owner_uid == asker_uid:
        return TwinAccessDecision(True, "self", owner_uid=owner_uid,
                                  relation="self")

    # Руководитель — до проверки согласия: это рабочая необходимость, а не
    # привилегия, которую подчинённый может отозвать.
    if is_manager_of(asker_uid, owner_uid, org_id):
        return TwinAccessDecision(True, "manager", owner_uid=owner_uid,
                                  relation="manager")

    if _is_org_admin(asker_uid, org_id):
        return TwinAccessDecision(True, "org_admin", owner_uid=owner_uid,
                                  relation="admin")

    if policy == POLICY_TEAM and _same_department(asker_uid, owner_uid):
        return TwinAccessDecision(True, "same_department", owner_uid=owner_uid,
                                  relation="teammate")

    consent = _has_consent(owner_uid, asker_uid)
    if consent is None:
        return TwinAccessDecision(True, "consent_store_unavailable",
                                  owner_uid=owner_uid, relation="other")
    if consent:
        return TwinAccessDecision(True, "consent_granted", owner_uid=owner_uid,
                                  relation="other")

    return TwinAccessDecision(
        False, "consent_missing", owner_uid=owner_uid, relation="other",
        message=("Этот человек не давал согласия на обращение к своему слепку. "
                 "Согласие он выдаёт сам в разделе «Мои согласия», а "
                 "руководителю и администратору доступ открыт по умолчанию."))
