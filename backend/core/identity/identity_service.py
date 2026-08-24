# -*- coding: utf-8 -*-
"""Сшивка идентичности: аккаунт ↔ Person-сущность графа (CogniLayer Ф0).

Проблема: человек существует в системе трижды несшито — как логин-аккаунт
(user_id), как Person-сущность графа (извлечён из встреч агентами) и как
участник орга (роль/отдел). Без связи «этот логин = вот эта Person» ни единый
когнитивный профиль, ни синхронизация «по сотрудникам» не замыкаются.

Этот сервис резолвит связь и предлагает кандидатов для подтверждения «это я».
Источник истины — ЯВНОЕ подтверждение пользователя (confirm_stitch). Матчинг по
имени/email — лишь подсказка с confidence, никогда не сшивает молча (та же
дисциплина, что и «неверный основатель»: слабый сигнал = кандидат, не факт).

Хранилище сшивки — membership (person_entity_id в записи участника, Ф0.1).
Never-raise: любая ошибка → пустой результат, не роняем вызывающего.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def resolve_person_for_account(user_id: str) -> Optional[str]:
    """person_entity_id, ПОДТВЕРЖДЁННО сшитый с этим аккаунтом, либо None."""
    if not user_id:
        return None
    try:
        from backend.core.ingest import membership
        return membership.get_user_person_entity(user_id)
    except Exception:
        logger.debug("resolve_person_for_account failed", exc_info=True)
        return None


def resolve_account_for_person(person_entity_id: str, org_id: str) -> Optional[str]:
    """Обратный резолв: аккаунт, сшитый с Person-сущностью, в рамках org."""
    if not (person_entity_id and org_id):
        return None
    try:
        from backend.core.ingest import membership
        return membership.find_user_by_person_entity(person_entity_id, org_id)
    except Exception:
        logger.debug("resolve_account_for_person failed", exc_info=True)
        return None


async def _account_identity(user_id: str) -> Dict[str, str]:
    """Имя и email аккаунта для матчинга (best-effort, пустые строки если нет)."""
    name, email = "", ""
    try:
        from backend.memory.user_profiles import get_user_profile_service
        prof = await get_user_profile_service().get_profile(user_id)
        if prof is not None:
            name = str(getattr(prof, "name", "") or "")
            email = str(getattr(prof, "email", "") or "")
    except Exception:
        logger.debug("account identity lookup skipped", exc_info=True)
    return {"name": name.strip(), "email": email.strip()}


def _name_score(account_name: str, person_name: str) -> float:
    """Оценка совпадения имени аккаунта и имени Person (0..1). 0 — не совпало."""
    if not (account_name and person_name):
        return 0.0
    a, p = account_name.strip().lower(), person_name.strip().lower()
    if a == p:
        return 1.0
    try:
        from backend.core.store.entity_resolver import _tokens_same_person
        if _tokens_same_person(account_name, person_name):
            # общий токен-костяк имени (Максим ↔ Максим Белухин)
            return 0.75
    except Exception:
        logger.debug("token name match skipped", exc_info=True)
    # подстрочное вхождение имени (одно слово внутри другого)
    if a in p or p in a:
        return 0.5
    return 0.0


async def candidate_persons_for_account(
    user_id: str, org_id: str, *, limit: int = 5,
) -> Dict[str, Any]:
    """Кандидаты Person-сущностей для сшивки с аккаунтом (для UI «это я»).

    Возвращает {already_linked, account, candidates:[{person_id, name, score,
    confidence}]}. Матчит имя/email аккаунта против людей графа. Пусто — не
    ошибка (нет данных / нет совпадений)."""
    out: Dict[str, Any] = {"already_linked": resolve_person_for_account(user_id),
                           "account": {}, "candidates": []}
    if not user_id:
        return out
    ident = await _account_identity(user_id)
    out["account"] = ident

    # люди графа этого тенанта (через генератор снапшотов, per-user)
    people: List[Dict[str, Any]] = []
    try:
        from backend.core.sleep.enhanced_snapshot import (
            get_enhanced_snapshot_generator,
        )
        from backend.core.store.graph_view import merged_graph_view_for_user
        gb = await merged_graph_view_for_user(user_id, use_networkx=None)
        gen = get_enhanced_snapshot_generator(gb, user_id=user_id)
        people = await gen.get_all_people_profiles(tenant_id=user_id) or []
        try:
            await gb.close(save=False)
        except Exception:
            pass
    except Exception:
        logger.debug("graph people lookup skipped", exc_info=True)
        return out

    scored = []
    for p in people:
        pid = p.get("person_id") or p.get("id")
        pname = str(p.get("name") or "")
        if not pid or not pname:
            continue
        s = _name_score(ident.get("name", ""), pname)
        # email-совпадение (если у Person есть email в атрибутах) — сильный сигнал
        pemail = str(p.get("email") or (p.get("attributes") or {}).get("email") or "")
        if ident.get("email") and pemail and ident["email"].lower() == pemail.lower():
            s = max(s, 0.95)
        if s > 0:
            conf = "high" if s >= 0.9 else ("medium" if s >= 0.7 else "low")
            scored.append({"person_id": str(pid), "name": pname,
                           "score": round(s, 2), "confidence": conf})
    scored.sort(key=lambda x: x["score"], reverse=True)
    out["candidates"] = scored[:max(1, limit)]
    return out


def confirm_stitch(user_id: str, org_id: str, person_entity_id: str) -> Dict[str, Any]:
    """Подтвердить сшивку «это я» (источник истины). Возвращает {ok, ...}.

    Занятая Person НЕ перепривязывается автоматически: раньше «последняя
    сшивка — источник истины» позволяла любому члену org присвоить чужую
    подтверждённую сшивку (и через неё выдать себя за коллегу). Теперь
    конфликт → отказ; перепривязывает только founder/admin организации,
    либо владелец сам расшивается через /identity/unlink."""
    if not (user_id and org_id and person_entity_id):
        return {"ok": False, "error": "user_id, org_id, person_entity_id обязательны"}
    try:
        from backend.core.ingest import membership
        holder = membership.find_user_by_person_entity(person_entity_id, org_id)
        if holder and holder != user_id and not membership.can_manage_org(user_id, org_id):
            return {"ok": False, "conflict": True,
                    "error": "эта Person-сущность уже подтверждена другим "
                             "аккаунтом; перепривязку делает администратор "
                             "организации"}
        ok = membership.set_member_person_entity(user_id, org_id, person_entity_id)
        return {"ok": bool(ok), "user_id": user_id,
                "person_entity_id": person_entity_id if ok else None}
    except Exception as e:  # noqa: BLE001
        logger.warning("confirm_stitch failed: %s", e)
        return {"ok": False, "error": str(e)}


def unlink_stitch(user_id: str, org_id: str) -> Dict[str, Any]:
    """Расшить аккаунт с Person-сущностью."""
    try:
        from backend.core.ingest import membership
        ok = membership.set_member_person_entity(user_id, org_id, None)
        return {"ok": bool(ok)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
