# -*- coding: utf-8 -*-
"""Cognitive Core — read-only API «Единая память».

Отдаёт фрагменты знания пользователя (узлы графа), их состояние консолидации
(В ТЕНИ / НА ОРБИТЕ / В ЯДРЕ), связи, семантический поиск и индекс синхронизации
ядра S(t). ВСЁ на реальных данных (см. core_service). За флагом
ENABLE_COGNITIVE_CORE: выключено → {"enabled": false}, фронт прячет вид.

Анти-IDOR: user_id обязан совпадать с токеном (паттерн documents/entities).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from litestar import Router, get
from litestar.params import Parameter

logger = logging.getLogger(__name__)


def _trusted_uid(authorization: Optional[str], user_id: Optional[str]) -> Optional[str]:
    """Анти-IDOR (паттерн documents/entities): токен валиден, но user_id чужой →
    None (отказ). Без токена (внутренние вызовы) — доверяем user_id."""
    try:
        from backend.core.auth.service_token import trusted_user_id
        headers = {"authorization": authorization} if authorization else {}
        uid, src = trusted_user_id(headers, user_id or "")
        if src != "unverified" and uid:
            return uid
    except PermissionError:
        logger.warning("cognitive-core: токен валиден, но запрошен чужой "
                       "user_id (req=%s…) — отказ", str(user_id)[:8])
        return None
    except Exception:
        logger.debug("cognitive-core: trusted_user_id unavailable", exc_info=True)
    return user_id


@get("/cognitive-core/overview")
async def cc_overview(
    user_id: Optional[str] = None,
    limit: int = 240,
    with_sync: bool = True,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> Dict[str, Any]:
    """Обзор ядра: счётчики по типам/состояниям, топ-фрагменты, sync%."""
    from backend.core.cognitive.core_service import cognitive_core_enabled, core_overview
    if not cognitive_core_enabled():
        return {"enabled": False}
    uid = _trusted_uid(authorization, user_id)
    if not uid:
        return {"enabled": True, "error": "Authentication required", "fragments": []}
    lim = max(1, min(1000, int(limit or 240)))
    return await core_overview(uid, limit=lim, with_sync=bool(with_sync))


@get("/cognitive-core/search")
async def cc_search(
    q: str = "",
    user_id: Optional[str] = None,
    limit: int = 24,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> Dict[str, Any]:
    """Семантический поиск по фрагментам знания."""
    from backend.core.cognitive.core_service import cognitive_core_enabled, core_search
    if not cognitive_core_enabled():
        return {"enabled": False, "results": []}
    uid = _trusted_uid(authorization, user_id)
    if not uid:
        return {"enabled": True, "error": "Authentication required", "results": []}
    return await core_search(uid, q, limit=max(1, min(50, int(limit or 24))))


@get("/cognitive-core/fragment/{node_id:str}")
async def cc_fragment(
    node_id: str,
    user_id: Optional[str] = None,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> Dict[str, Any]:
    """Один фрагмент + связанные (соседи графа)."""
    from backend.core.cognitive.core_service import cognitive_core_enabled, core_fragment
    if not cognitive_core_enabled():
        return {"enabled": False}
    uid = _trusted_uid(authorization, user_id)
    if not uid:
        return {"enabled": True, "error": "Authentication required", "fragment": None}
    return await core_fragment(uid, node_id)


router = Router(path="", route_handlers=[cc_overview, cc_search, cc_fragment],
                tags=["Cognitive Core"])
