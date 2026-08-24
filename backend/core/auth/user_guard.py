# -*- coding: utf-8 -*-
"""Единый анти-IDOR guard: user_id из запроса ОБЯЗАН совпадать с токеном.

Дыра, которую закрывает (подтверждена в проде): десятки эндпоинтов берут
user_id голым query-параметром/полем body и отдают per-tenant данные ЛЮБОЙ
валидной сессии с чужим id — граф/оргсхема/доски/SIMA показывали данные
другого аккаунта (стейлный localStorage после смены аккаунта).

Паттерн уже жил локальными копиями в documents/boards/entities — этот модуль
делает его переиспользуемым. Семантика (обратная совместимость):
  • валидный токен и user_id СВОЙ / не передан → доверенный user_id из токена;
  • валидный токен, но user_id ЧУЖОЙ → None (отказ) + warning в лог;
  • токена нет (внутренние вызовы/легаси) → переданный user_id как раньше.

Использование в роуте:
    from backend.core.auth.user_guard import resolve_user_or_none
    uid = resolve_user_or_none(authorization, user_id, scope="boards")
    if uid is None:
        return {...401/отказ...}
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def resolve_user_or_none(authorization: Optional[str],
                         user_id: Optional[str],
                         *, scope: str = "api") -> Optional[str]:
    """Доверенный user_id или None (= токен валиден, но id чужой → отказ).

    Never-raise: недоступность проверки → легаси-поведение (переданный id),
    чтобы внутренние вызовы без токена не ломались."""
    try:
        from backend.core.auth.service_token import trusted_user_id
        headers = {"authorization": authorization} if authorization else {}
        uid, src = trusted_user_id(headers, user_id or "")
        if src != "unverified" and uid:
            return uid
    except PermissionError:
        logger.warning(f"{scope}: токен валиден, но запрошен чужой user_id — отказ")
        return None
    except Exception:
        logger.debug(f"{scope}: trusted_user_id unavailable", exc_info=True)
    return user_id or None


def enforce_user_id_matches_token(connection, route_handler) -> None:
    """Litestar-guard роутера: если запрос несёт user_id (query ИЛИ path) и
    валидный токен, чей uid НЕ совпадает — отказ. Защищает ВСЕ хендлеры роутера
    одной строкой (для тех, что берут user_id из query/path).

    Семантика как у resolve_user_or_none: нет токена (внутренние вызовы) или
    нет user_id в запросе → пропускаем (легаси-совместимость); чужой id при
    валидном токене → 401. Тело запроса тут недоступно (не распарсено) —
    body-based user_id закрывается отдельно в самих хендлерах."""
    from litestar.exceptions import NotAuthorizedException

    # что запросили (query имеет приоритет, иначе path)
    requested = None
    try:
        requested = connection.query_params.get("user_id")
    except Exception:
        requested = None
    if not requested:
        try:
            requested = (connection.path_params or {}).get("user_id")
        except Exception:
            requested = None
    if not requested:
        return  # нечего сверять

    try:
        authz = connection.headers.get("authorization")
    except Exception:
        authz = None
    if not authz:
        return  # внутренний/легаси вызов без токена — как раньше

    try:
        from backend.core.auth.service_token import trusted_user_id
        uid, src = trusted_user_id({"authorization": authz}, requested)
    except PermissionError:
        logger.warning(f"guard: токен valid, user_id чужой ({str(requested)[:8]}…) — отказ")
        raise NotAuthorizedException(detail="user_id does not match token")
    except Exception:
        logger.debug("guard: trusted_user_id unavailable — пропуск", exc_info=True)
        return
    # доп. страховка: расхождение без исключения
    if src != "unverified" and uid and uid != requested:
        logger.warning(f"guard: токен uid≠user_id ({str(requested)[:8]}…) — отказ")
        raise NotAuthorizedException(detail="user_id does not match token")
