# -*- coding: utf-8 -*-
"""Виртуальная планёрка директоров — API.

POST /boardroom/run — задать вопрос «совету директоров»: роли обсуждают по
данным компании, председатель сводит решение с протоколом разногласий,
решение сверяется с данными отдельным проходом. Результат сохраняется в
историю отчётов (report_type=boardroom).

GET /boardroom/directors — каталог ролей для UI (человек выбирает из списка,
никаких ID руками).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from litestar import Request, Router, get, post
from litestar.exceptions import HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)


def _resolve_user(request: Request, user_id: Optional[str]) -> str:
    """Верифицированный токен > query (анти-спуфинг, как в отчётах)."""
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


class BoardroomRequest(BaseModel):
    question: str
    user_id: Optional[str] = None
    directors: Optional[List[str]] = None   # id из каталога; пусто = дефолт
    persons: Optional[List[str]] = None     # слепки живых сотрудников
    rounds: int = 2
    use_brain: bool = True                  # искать в мозге под вопрос
    days_back: int = 30
    lang: Optional[str] = None              # язык интерфейса → язык ответов


class DirectorChatRequest(BaseModel):
    question: str
    director_id: Optional[str] = None            # один директор (совместимость)
    director_ids: Optional[List[str]] = None     # несколько → мини-обсуждение
    user_id: Optional[str] = None
    history: Optional[List[Dict[str, str]]] = None   # [{who, text}]
    use_brain: bool = True
    days_back: int = 30
    lang: Optional[str] = None              # язык интерфейса → язык ответов


@get("/directors")
async def list_directors() -> Dict[str, Any]:
    from backend.core.boardroom import DIRECTORS
    return {"directors": [
        {"id": k, "name": v["name"], "icon": v["icon"],
         "domain": v.get("domain")}
        for k, v in DIRECTORS.items()
    ]}


@post("/run")
async def run(data: BoardroomRequest, request: Request) -> Dict[str, Any]:
    uid = _resolve_user(request, data.user_id)
    from backend.core.boardroom import run_boardroom
    try:
        return await run_boardroom(
            uid, data.question,
            director_ids=data.directors,
            person_ids=data.persons,
            rounds=data.rounds,
            use_brain=data.use_brain,
            days_back=max(1, min(int(data.days_back or 30), 365)),
            lang=str(data.lang or ""),
        )
    except Exception as e:
        logger.error("boardroom run failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"boardroom: {e}")


@post("/chat")
async def chat(data: DirectorChatRequest, request: Request) -> Dict[str, Any]:
    """Диалог с директором(ами). Несколько → мини-обсуждение между собой:
    реплики по очереди, реакция на коллег, итог с несогласными."""
    uid = _resolve_user(request, data.user_id)
    ids = [d for d in (data.director_ids or []) if d] or (
        [data.director_id] if data.director_id else [])
    if not ids:
        raise HTTPException(status_code=400, detail="director_id(s) required")
    from backend.core.boardroom.boardroom_service import ask_directors
    try:
        return await ask_directors(
            uid, ids, data.question,
            history=data.history, use_brain=data.use_brain,
            days_back=max(1, min(int(data.days_back or 30), 365)),
            lang=str(data.lang or ""))
    except Exception as e:
        logger.error("director chat failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"director chat: {e}")


router = Router(path="/boardroom", route_handlers=[list_directors, run, chat],
                tags=["Boardroom"])
