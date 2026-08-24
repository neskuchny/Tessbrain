# -*- coding: utf-8 -*-
"""Приём сессии локального захвата от десктоп-агента.

Агент пишет звук на устройстве сотрудника без бота-участника в звонке и
шлёт сюда уже расшифрованные дорожки. Ключевое отличие от бота: приходит
не один смешанный поток, а раздельно микрофон владельца (автор известен
точно) и системный звук (все остальные). Подробности сборки — в
backend/core/capture/local_session.py.

Функция за флагом ENABLE_LOCAL_CAPTURE (по умолчанию OFF): десктоп-агента
пока нет, и держать открытым приём данных от несуществующего клиента
незачем.

Роут намеренно НЕ пишет в память компании — он разбирает сессию и
возвращает результат. Пока агент не написан и не проверен на реальных
встречах, автоматическая запись в общий граф из непроверенного источника
была бы преждевременной. Подключение к пайплайну встреч — следующий шаг,
когда будет что подключать.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from litestar import Request, Router, post
from litestar.exceptions import HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Разумный потолок на сессию: часовая встреча — это порядка сотен реплик.
# Ограничение защищает от случайной заливки часов записи одним запросом.
_MAX_SEGMENTS = 5000


def capture_enabled() -> bool:
    return os.environ.get("ENABLE_LOCAL_CAPTURE", "").strip().lower() in (
        "1", "on", "true", "yes")


def _resolve_user(request: Request, user_id: Optional[str]) -> str:
    try:
        from backend.core.auth.service_token import trusted_user_id
        uid, _src = trusted_user_id(request.headers, user_id or "")
    except PermissionError:
        raise HTTPException(status_code=403, detail="token/user_id mismatch")
    except Exception:
        uid = user_id or ""
    if not uid:
        raise HTTPException(status_code=401, detail="user_id required")
    return uid


class CaptureSessionRequest(BaseModel):
    tracks: List[Dict[str, Any]]
    user_id: Optional[str] = None
    meeting_id: Optional[str] = None
    title: Optional[str] = None
    attendees: Optional[List[str]] = None   # имена из календаря, если есть


@post("/session")
async def submit_session(data: CaptureSessionRequest,
                         request: Request) -> Dict[str, Any]:
    """Разобрать сессию захвата: дорожки → транскрипт с авторами."""
    if not capture_enabled():
        return {"status": "disabled",
                "message": ("Локальный захват выключен "
                            "(ENABLE_LOCAL_CAPTURE). Данные не приняты.")}

    uid = _resolve_user(request, data.user_id)

    total = sum(len(t.get("segments") or [])
                for t in (data.tracks or []) if isinstance(t, dict))
    if total > _MAX_SEGMENTS:
        raise HTTPException(
            status_code=413,
            detail=f"слишком много сегментов ({total} > {_MAX_SEGMENTS}); "
                   "разбейте сессию на части")

    # Своей дорожке проставляем владельца из токена, а не из тела запроса:
    # клиент не должен иметь возможности подписать чужую реплику именем
    # другого человека.
    own_name = ""
    try:
        from backend.core.identity.identity_service import _account_identity
        own_name = str((await _account_identity(uid)).get("name") or "")
    except Exception:
        logger.debug("local capture: имя владельца не резолвится",
                     exc_info=True)

    tracks = []
    for t in data.tracks or []:
        if not isinstance(t, dict):
            continue
        t = dict(t)
        if str(t.get("kind") or "") == "own":
            t["owner_user_id"] = uid
            t["owner_name"] = own_name or t.get("owner_name") or uid
        tracks.append(t)

    from backend.core.capture.local_session import build_session
    result = build_session({"tracks": tracks},
                           attendee_names=data.attendees)

    result["meeting_id"] = data.meeting_id
    result["title"] = data.title
    result["persisted"] = False
    result["note"] = ("Сессия разобрана, но в память компании не записана: "
                      "подключение к пайплайну встреч — следующий шаг.")

    st = result.get("stats") or {}
    logger.info("local capture: сессия от %s — %s реплик, атрибуция %.0f%%, "
                "своих дорожек %s", uid, st.get("segments"),
                100 * float(st.get("attribution_rate") or 0),
                st.get("own_tracks"))
    return result


router = Router(path="/local-capture", route_handlers=[submit_session],
                tags=["Local Capture"])
