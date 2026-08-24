# -*- coding: utf-8 -*-
"""Профиль «у Person-узла» — резолв когнитивного профиля по Person-сущности
(CogniLayer Ф1, завершение Части II корректирующего документа).

Методология требует, чтобы единый когнитивный профиль был достижим от
Person-узла графа: (Person)-[:HAS_PROFILE]->(CogniProfile). Мы реализуем это
ЛОГИЧЕСКИ — резолв-слоем через сшивку Ф0 — а не физическим узлом в графе,
и это осознанное решение по безопасности:

- ХРАНЕНИЕ НЕ ПЕРЕНОСИТСЯ. Персона остаётся в public.personas (RLS по
  user_id), наблюдения — в persona_observations (RLS). Граф шарится внутри
  орга, индексируется в поиск, попадает в снепшоты и ночную обработку —
  положить туда профиль значило бы открыть его всем, кто видит граф, и
  создать новую поверхность утечки (ровно класс прошлого cross-tenant бага).
- ДОСТУП ЧЕРЕЗ ДВА ЗАМКА: (1) Person-сущность должна быть ПОДТВЕРЖДЁННО
  сшита с аккаунтом («это я», Ф0) — несшитый человек профиля не имеет,
  имя-похожесть здесь не работает; (2) запрашивающий обязан состоять в ТОЙ ЖЕ
  организации, что и владелец профиля — чужому тенанту возвращается None,
  как и не-участнику орга.
- НАРУЖУ ОТДАЁТСЯ ФРЕЙМ АДАПТАЦИИ, НЕ СЫРАЯ ПЕРСОНА: стиль/метафоры/
  когнитивные измерения (то, что нужно «говорить с человеком на его языке»),
  без behavioral-графика, слабостей и client_profiles — минимально
  необходимое, а не всё подряд.

Never-raise: любая ошибка → None/пусто, вызывающий не падает.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _same_org(requester_uid: str, target_uid: str, org_id: str) -> bool:
    """Оба аккаунта состоят в org_id (замок №2)."""
    try:
        from backend.core.ingest import membership
        return (membership.get_org_for_user(requester_uid) == org_id
                and membership.get_org_for_user(target_uid) == org_id)
    except Exception:
        logger.debug("person_profile: org check failed", exc_info=True)
        return False


async def profile_for_person(
    person_entity_id: str, org_id: str, requester_uid: str,
) -> Optional[Dict[str, Any]]:
    """Когнитивный профиль человека по его Person-сущности графа.

    Возвращает {user_id, frame, cogni} либо None (несшит / чужой орг /
    нет данных). frame — CogniFrame.to_dict() для адаптации формы,
    cogni — нормализованные измерения extended["cogni"]."""
    if not (person_entity_id and org_id and requester_uid):
        return None

    # замок №1: только подтверждённая сшивка
    try:
        from backend.core.identity.identity_service import resolve_account_for_person
        target_uid = resolve_account_for_person(person_entity_id, org_id)
    except Exception:
        logger.debug("person_profile: stitch resolve failed", exc_info=True)
        return None
    if not target_uid:
        return None

    # замок №2: запрашивающий в той же организации
    if not _same_org(requester_uid, target_uid, org_id):
        logger.info("person_profile: доступ отклонён (запрашивающий вне орга)")
        return None

    # замок №3: чужой профиль — только руководителям. Когнитивный профиль
    # (стиль, метафоры, примеры речи) — чувствительные данные о человеке;
    # «любой коллега по org» — слишком широкий круг. Свой профиль — всегда.
    if requester_uid != target_uid:
        try:
            from backend.core.ingest import membership
            role = membership.get_user_org_base_role(requester_uid)
            if role not in ("founder", "admin", "manager"):
                logger.info("person_profile: доступ отклонён (роль %s не "
                            "руководящая)", role)
                return None
        except Exception:
            logger.debug("person_profile: role check failed", exc_info=True)
            return None

    persona: Any = None
    try:
        from backend.core.persona.store import get_persona_store
        persona = await get_persona_store().get(target_uid)
        if asyncio.iscoroutine(persona):
            persona = await persona
    except Exception:
        logger.debug("person_profile: persona load failed", exc_info=True)
        return None
    if persona is None:
        # сшивка есть, персона ещё не построена — честный «пока пусто»
        return {"user_id": target_uid, "frame": None, "cogni": None}

    try:
        from backend.core.cogni.adapt import _frame_from_persona
        from backend.core.cogni.dimensions import get_cogni
        frame = _frame_from_persona(persona)
        return {
            "user_id": target_uid,
            "frame": frame.to_dict(),
            "cogni": get_cogni(getattr(persona, "extended", None)),
        }
    except Exception:
        logger.debug("person_profile: frame build failed", exc_info=True)
        return {"user_id": target_uid, "frame": None, "cogni": None}


async def frame_for_person(
    person_entity_id: str, org_id: str, requester_uid: str,
):
    """CogniFrame человека по Person-сущности (для cogni-adapt в код-путях,
    где известна сущность графа, а не аккаунт: desync, сверки, каскады).
    Пустой фрейм — валидный ответ (несшит/нет данных/нет доступа)."""
    from backend.core.cogni.adapt import CogniFrame
    prof = await profile_for_person(person_entity_id, org_id, requester_uid)
    if not prof or not prof.get("frame"):
        return CogniFrame()
    f = prof["frame"]
    return CogniFrame(
        language=f.get("language"), style=f.get("style"),
        vocabulary=f.get("vocabulary"), abstraction=f.get("abstraction"),
        metaphors=list(f.get("metaphors") or []),
        speech_examples=list(f.get("speech_examples") or []),
        equalizer_leanings=list(f.get("equalizer_leanings") or []),
        phase=f.get("phase"), light_cone=list(f.get("light_cone") or []),
    )
