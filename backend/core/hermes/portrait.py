# -*- coding: utf-8 -*-
"""Hermes user-portrait (P12g) — портрет клиента в системный промпт.

ПРОБЕЛ (карта переноса, #4): у Hermes USER.md (стиль/привычки/контекст)
грузится снапшотом в системный промпт на старте сессии. У нас
`ProfileCurator` есть и СТРОИТ профиль (allow-list из 11 полей), но
включён только ночью и в промпт per-session не подмешивается.

P12g НЕ дублирует профиль-стор (канон — `memory.user_profiles
.UserProfileService`): берёт уже построенный профиль и рендерит как
USER.md-блок для инжекта в agent_oc. Загрузчик инъектируем → юнит-
тест без БД.

Дизайн (как P0–P12e): чистый stdlib, детерминированный, never-raises,
allow-list строго соблюдён (не протекают произвольные/identity-поля).
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Optional

logger = logging.getLogger(__name__)

# тот же безопасный список, что у ProfileCurator (держим синхронно;
# дублируем литералом, чтобы модуль не тянул memory.profile_curator).
_PORTRAIT_FIELDS: tuple[tuple[str, str], ...] = (
    ("communication_style", "Стиль общения"),
    ("detail_level", "Уровень детализации"),
    ("preferred_language", "Язык"),
    ("expertise", "Экспертиза"),
    ("known_topics", "Знакомые темы"),
    ("blind_spots", "Слепые зоны"),
    ("current_projects", "Текущие проекты"),
    ("current_focus", "Фокус сейчас"),
    ("typical_query_types", "Типичные запросы"),
    ("decision_style", "Стиль решений"),
)

# Fetcher(user_id) -> dict профиля. Инъектируем (фейк в тесте).
ProfileFetcher = Callable[[str], Awaitable[dict]]


def _fmt_value(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple, set)):
        items = [str(x).strip() for x in v if str(x).strip()]
        return ", ".join(items)
    return str(v).strip()


def render_portrait(profile: Any) -> str:
    """Профиль → USER.md-style блок. Только allow-list, только
    непустое. Никогда не raises. '' если нечего показать."""
    try:
        if not isinstance(profile, dict):
            return ""
        lines: list[str] = []
        name = _fmt_value(profile.get("name"))
        role = _fmt_value(profile.get("role"))
        ident = " · ".join(x for x in (name, role) if x)
        for key, label in _PORTRAIT_FIELDS:
            val = _fmt_value(profile.get(key))
            if val:
                lines.append(f"- {label}: {val}")
        if not lines:
            return ""
        head = "Портрет пользователя"
        if ident:
            head += f" ({ident})"
        return head + " — учитывай при ответе:\n" + "\n".join(lines)
    except Exception as exc:
        logger.debug("render_portrait failed: %s", exc)
        return ""


def maybe_portrait_block(profile: Any, *, enabled: bool = False) -> str:
    """Gated рендер. '' если выключено/пусто. Не raises."""
    if not enabled:
        return ""
    return render_portrait(profile)


async def load_profile(
    user_id: str,
    *,
    fetcher: Optional[ProfileFetcher] = None,
) -> dict:
    """Загрузить профиль (инъектируемо). Дефолтный fetcher —
    UserProfileService (ленивый импорт). Никогда не raises → {}."""
    if not user_id:
        return {}
    if fetcher is not None:
        try:
            r = await fetcher(user_id)
            return r if isinstance(r, dict) else {}
        except Exception as exc:
            logger.debug("load_profile fetcher failed: %s", exc)
            return {}
    try:
        from backend.memory.user_profiles import UserProfileService

        svc = UserProfileService()
        prof = await svc.get_profile(user_id)  # type: ignore[attr-defined]
        if prof is None:
            return {}
        if hasattr(prof, "to_dict"):
            d = prof.to_dict()
            return d if isinstance(d, dict) else {}
        return prof if isinstance(prof, dict) else {}
    except Exception as exc:
        logger.debug("load_profile default fetcher failed: %s", exc)
        return {}


async def maybe_portrait_for_user(
    user_id: str,
    *,
    fetcher: Optional[ProfileFetcher] = None,
    enabled: bool = False,
) -> str:
    """Загрузить профиль и отрендерить блок. Gated, never-raises."""
    if not enabled:
        return ""
    try:
        prof = await load_profile(user_id, fetcher=fetcher)
        return render_portrait(prof)
    except Exception as exc:
        logger.debug("maybe_portrait_for_user failed: %s", exc)
        return ""


__all__ = [
    "ProfileFetcher",
    "render_portrait",
    "maybe_portrait_block",
    "load_profile",
    "maybe_portrait_for_user",
]
