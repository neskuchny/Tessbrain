# -*- coding: utf-8 -*-
"""Участник встречи → аккаунт сотрудника (для доставки артефактов).

Зачем. Кофе-сценарий («пока человек идёт от переговорки до стола, черновик
уже у него») собирал артефакты и не доставлял их: `participant_agent`
присваивает участнику СЛУЧАЙНЫЙ uuid4, если во входных данных нет id, а
доставка использовала этот идентификатор как user_id аккаунта. Случайный
uuid никогда не совпадёт с реальным аккаунтом — резолв канала возвращал
«no telegram chat_id linked», и всё садилось в pending. То есть главный
поток «данные из встречи → сотруднику» не работал ни для кого, кроме
владельца ингеста.

Резолв идёт по трём ступеням, от самой надёжной к самой слабой, и на
каждой ступени видно, чем именно подтверждено соответствие:

  1. `user_id` пришёл во входных данных — доверяем как есть;
  2. подтверждённая сшивка «это я»: Person-узел графа ↔ аккаунт
     (membership.person_entity_id) — то же основание, что у слепков;
  3. точное совпадение имени с именем участника организации.

Догадок по частичному совпадению имён НЕТ намеренно: доставить личный
разбор встречи не тому человеку хуже, чем не доставить никому. Не
разрешилось — возвращаем None, и вызывающий честно кладёт артефакт в
pending с причиной.

Чистый stdlib на входе, тестируется с подставными резолверами.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

_UUID_LEN = 36


def _looks_like_uuid(value: Any) -> bool:
    s = str(value or "")
    return len(s) == _UUID_LEN and s.count("-") == 4


def resolve_participant_account(
    participant: Dict[str, Any],
    org_id: str,
    *,
    person_lookup: Optional[Callable[[str, str], Optional[str]]] = None,
    members_lookup: Optional[Callable[[str], list]] = None,
) -> Dict[str, Any]:
    """Найти аккаунт сотрудника по участнику встречи.

    Возвращает {"user_id": str|None, "basis": str}, где basis объясняет,
    на чём основано соответствие: "explicit" | "person_link" | "name_exact"
    | "unresolved". basis нужен вызывающему, чтобы писать честную причину
    в pending, а не «доставка не удалась».
    """
    if not isinstance(participant, dict):
        return {"user_id": None, "basis": "unresolved"}

    # 1. Явный user_id аккаунта. id/participant_id НЕ берём: participant_agent
    #    кладёт туда случайный uuid4, когда во входных данных id не было.
    explicit = str(participant.get("user_id") or "").strip()
    if explicit:
        return {"user_id": explicit, "basis": "explicit"}

    name = str(participant.get("name")
               or participant.get("display_name") or "").strip()

    # 2. Подтверждённая сшивка Person↔аккаунт. person_id участника приходит
    #    из графа, когда встречу разбирали с резолвом сущностей.
    person_id = str(participant.get("person_id")
                    or participant.get("person_entity_id") or "").strip()
    if person_id and org_id:
        lookup = person_lookup
        if lookup is None:
            try:
                from backend.core.identity.identity_service import (
                    resolve_account_for_person,
                )
                lookup = resolve_account_for_person
            except Exception:
                lookup = None
        if lookup is not None:
            try:
                uid = lookup(person_id, org_id)
                if uid:
                    return {"user_id": str(uid), "basis": "person_link"}
            except Exception:
                logger.debug("coffee: person→account lookup failed",
                             exc_info=True)

    # 3. Точное совпадение имени с участником организации.
    if name and org_id:
        members = members_lookup
        if members is None:
            try:
                from backend.core.ingest import membership
                members = membership.list_members
            except Exception:
                members = None
        if members is not None:
            try:
                target = " ".join(name.lower().split())
                matches = []
                for m in (members(org_id) or []):
                    if not isinstance(m, dict):
                        continue
                    mname = " ".join(str(
                        m.get("name") or m.get("display_name") or ""
                    ).lower().split())
                    if mname and mname == target and m.get("user_id"):
                        matches.append(str(m["user_id"]))
                # Тёзки в организации — не догадываемся, кто из них.
                if len(matches) == 1:
                    return {"user_id": matches[0], "basis": "name_exact"}
                if len(matches) > 1:
                    logger.info("coffee: имя %r неоднозначно (%d совпадений) "
                                "— не доставляем наугад", name, len(matches))
                    return {"user_id": None, "basis": "ambiguous_name"}
            except Exception:
                logger.debug("coffee: members lookup failed", exc_info=True)

    return {"user_id": None, "basis": "unresolved"}


def unresolved_reason(basis: str, name: str = "") -> str:
    """Человеческая причина для pending — чтобы в интерфейсе было видно,
    что чинить, а не просто «не доставлено»."""
    who = f" «{name}»" if name else ""
    if basis == "ambiguous_name":
        return (f"В организации несколько сотрудников с именем{who} — "
                f"нужно указать, кому именно доставлять")
    return (f"Участник{who} не сшит с аккаунтом: подтвердите «это я» в "
            f"профиле или добавьте человека в организацию")
