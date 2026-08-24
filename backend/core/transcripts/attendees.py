# -*- coding: utf-8 -*-
"""Meeting attendees resolution (issue #112 T-4 lite + T-5 helper).

Собирает «ground truth» списка участников встречи из нескольких источников
в порядке надёжности:
  1. Явные structured attendees (из API / CRM lookup) — {name, email, role}
  2. Google Calendar API (graceful — пусто если не сконфигурирован)
  3. Plain participants list (legacy, только имена)

Зачем: attribution в системе памяти (issue #112) опирается на знание
«кто вообще был на встрече». Без этого LLM приписывает задачи кому угодно
(включая людей, которых не было — см. дыру T-5). Attendees дают
constraint satisfaction: assignee ОБЯЗАН быть в этом списке, иначе флаг
needs_human_review.

Архитектура (graceful degradation):
  - Если есть structured attendees → используем (max confidence)
  - Если есть calendar_event_id + GCal сконфигурирован → дёргаем API
  - Иначе → fallback на participants (имена без email)
  - В худшем случае пустой список — система работает, но без validation

GCal API намеренно вынесен в отдельную функцию-stub с graceful fallback:
в средах без OAuth (dev/staging) она просто возвращает []. Реальный
adapter подключается через GOOGLE_CALENDAR_* env когда secrets готовы.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _normalize_email(email: Optional[str]) -> Optional[str]:
    """Email → lowercase strip, либо None если пусто/мусор."""
    if not email or not isinstance(email, str):
        return None
    e = email.strip().lower()
    # Минимальная валидация: есть @ и точка после
    if "@" in e and "." in e.split("@", 1)[1]:
        return e
    return None


def normalize_attendee(raw: Any) -> Optional[Dict[str, Any]]:
    """Нормализовать один attendee к {name, email, role, source}.

    Принимает:
      - str: «Иван Петров» → {name: ..., email: None}
      - dict: {name, email, role, ...} → нормализованный dict
    Возвращает None для невалидных (пустых) записей.
    """
    if isinstance(raw, str):
        name = raw.strip()
        if not name:
            return None
        return {"name": name, "email": None, "role": "", "source": "name_only"}

    if isinstance(raw, dict):
        name = (raw.get("name") or raw.get("displayName") or "").strip()
        email = _normalize_email(raw.get("email"))
        # Если нет имени, но есть email — извлечём имя из local-part
        if not name and email:
            name = email.split("@", 1)[0].replace(".", " ").title()
        if not name and not email:
            return None
        return {
            "name": name,
            "email": email,
            "role": (raw.get("role") or "").strip(),
            "source": raw.get("source") or ("structured" if email else "name_only"),
        }

    return None


def normalize_attendees(raw_list: Optional[List[Any]]) -> List[Dict[str, Any]]:
    """Нормализовать список attendees, отбросив дубли и пустые.

    Дедупликация: по email И по lower(name). Если человек встречается
    с email в одном источнике и без email (только имя) в другом — это
    один человек, оставляем запись с email (она богаче). Это важно для
    resolve_attendees, который мерджит structured + participants.
    """
    if not raw_list:
        return []
    seen_emails: set[str] = set()
    seen_names: set[str] = set()
    out: List[Dict[str, Any]] = []
    for raw in raw_list:
        att = normalize_attendee(raw)
        if not att:
            continue
        email = att["email"]
        name_key = att["name"].lower() if att["name"] else None
        # Дубль по email
        if email and email in seen_emails:
            continue
        # Дубль по имени (даже если у текущего нет email, а у виденного был)
        if name_key and name_key in seen_names:
            continue
        if email:
            seen_emails.add(email)
        if name_key:
            seen_names.add(name_key)
        out.append(att)
    return out


async def fetch_calendar_attendees(
    calendar_event_id: str,
    tenant_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Дёрнуть attendees из Google Calendar по event_id.

    issue #112 T-4: graceful — если GCal не сконфигурирован (нет
    GOOGLE_CALENDAR_API_KEY / OAuth), возвращает [] без ошибки. Реальный
    adapter подключается когда secrets готовы.

    Намеренно отдельная функция: легко замокать в тестах, легко заменить
    на реальную реализацию не трогая остальной flow.
    """
    api_key = os.getenv("GOOGLE_CALENDAR_API_KEY") or os.getenv("GCAL_API_KEY")
    if not api_key:
        logger.debug(
            "fetch_calendar_attendees: GCal not configured, skipping "
            "(event_id=%s). issue #112 T-4 — graceful fallback.",
            calendar_event_id,
        )
        return []

    # Реальная имплементация GCal API будет здесь. Пока — заглушка,
    # которая логирует намерение. Когда подключим OAuth — заменим тело.
    try:
        # TODO(issue #112 T-4): реальный вызов
        #   service = build('calendar', 'v3', credentials=...)
        #   event = service.events().get(calendarId=..., eventId=...).execute()
        #   return normalize_attendees(event.get('attendees', []))
        logger.info(
            "fetch_calendar_attendees: GCal configured but adapter not yet "
            "implemented (event_id=%s)", calendar_event_id,
        )
        return []
    except Exception as e:
        logger.warning("fetch_calendar_attendees failed: %s", e)
        return []


async def resolve_attendees(
    meeting_context: Optional[Dict[str, Any]],
    tenant_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Собрать итоговый список attendees из всех источников.

    Приоритет (от надёжного к слабому):
      1. meeting_context['attendees'] — structured (API / CRM)
      2. Google Calendar (если calendar_event_id + GCal сконфигурирован)
      3. meeting_context['participants'] — plain names (legacy)

    Источники МЕРДЖАТСЯ: structured attendees обогащаются GCal-данными,
    participants добавляются если их ещё нет. Дедуп по email/name.

    Возвращает [] если ничего нет — система работает, но без T-5
    validation (assignee membership check).
    """
    ctx = meeting_context or {}
    merged: List[Dict[str, Any]] = []

    # 1. Structured attendees (max confidence)
    structured = normalize_attendees(ctx.get("attendees"))
    merged.extend(structured)

    # 2. Google Calendar (graceful)
    event_id = ctx.get("calendar_event_id") or ctx.get("event_id")
    if event_id:
        try:
            cal_attendees = await fetch_calendar_attendees(event_id, tenant_id)
            merged.extend(cal_attendees)
        except Exception as e:
            logger.warning("resolve_attendees: GCal fetch failed: %s", e)

    # 3. Plain participants (legacy fallback)
    participants = normalize_attendees(ctx.get("participants"))
    merged.extend(participants)

    # Финальная дедупликация по email/name
    return normalize_attendees(merged)


def attendee_names(attendees: List[Dict[str, Any]]) -> List[str]:
    """Извлечь только имена — для передачи в extractor'ы как
    known_participants whitelist."""
    return [a["name"] for a in attendees if a.get("name")]


def is_assignee_in_attendees(
    assignee_name: str,
    assignee_email: Optional[str],
    attendees: List[Dict[str, Any]],
) -> bool:
    """Проверить, был ли assignee среди attendees (issue #112 T-5).

    Email-first (если есть с обеих сторон), затем по имени (exact или
    token-set). Консервативно: при сомнении — False (caller поднимет
    needs_human_review).

    Если attendees пуст — возвращает True (нет данных для проверки,
    не блокируем; T-5 validation работает только когда attendees известны).
    """
    if not attendees:
        # Нет ground truth — не можем валидировать, не блокируем
        return True
    if not assignee_name and not assignee_email:
        return False

    norm_email = _normalize_email(assignee_email)
    if norm_email:
        for a in attendees:
            if a.get("email") and a["email"] == norm_email:
                return True

    if assignee_name:
        target = assignee_name.strip().lower()
        target_tokens = set(target.split())
        for a in attendees:
            an = (a.get("name") or "").strip().lower()
            if not an:
                continue
            # Exact match
            if an == target:
                return True
            # Token-set: 'Иван Петров' ↔ 'Петров Иван'
            a_tokens = set(an.split())
            if len(target_tokens) >= 2 and len(a_tokens) >= 2:
                if target_tokens == a_tokens:
                    return True
            # First-name match: 'Максим' ∈ 'Максим Иванов'
            if target in a_tokens or (a_tokens and an.split()[0] == target):
                return True

    return False
