"""Coffee scenario REST endpoints (Phase B).

Endpoints:
- POST /api/v1/coffee/prepare   — сгенерировать (и опционально доставить)
                                  coffee-артефакты для заданной встречи
- GET  /api/v1/coffee/artifacts/{meeting_id} — список ранее сгенерированных
                                  артефактов (через persona observations
                                  или cache — Phase B.2 финализация)

Hook в meeting pipeline (process_meeting_task) триггерит prepare автоматически
после ингеста новой встречи — UI/Mini Tess дёргают эти endpoint'ы только для
ручного re-generation или просмотра.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from litestar import Router, get, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter

from backend.api.middleware.auth_middleware import get_user_id_from_token
from backend.core.coffee import prepare_coffee_for_meeting
from backend.core.coffee.storage import list_artifacts as storage_list_artifacts

logger = logging.getLogger(__name__)


def _extract_user(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization required")
    try:
        uid = get_user_id_from_token(authorization)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}") from exc
    if not uid:
        raise HTTPException(status_code=401, detail="Token missing sub/user_id")
    return uid


@post("/prepare", status_code=200)
async def prepare_coffee_endpoint(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Подготовить coffee-артефакты для указанной встречи.

    Body:
        {
          "meeting_id": "...",
          "deliver": true,        // default true — сразу отправлять
          "only_user_ids": ["..."] // optional — только эти участники
        }

    Если meeting_data отсутствует в body, мы попытаемся подгрузить его из
    Supabase (по meeting_id) — для упрощённого вызова из UI. Phase B.2
    обогатит это полным graph-lookup.
    """
    _extract_user(authorization)  # auth required, user_id не используется

    meeting_id = data.get("meeting_id")
    if not meeting_id:
        raise HTTPException(status_code=400, detail="meeting_id required")

    deliver = bool(data.get("deliver", True))
    only_user_ids = data.get("only_user_ids") if isinstance(data.get("only_user_ids"), list) else None

    # Грузим meeting data из Supabase если нет inline
    meeting_data = data.get("meeting_data")
    if not isinstance(meeting_data, dict):
        meeting_data = await _load_meeting_data(meeting_id)
        if meeting_data is None:
            raise HTTPException(
                status_code=404,
                detail=f"meeting {meeting_id} not found in Supabase",
            )

    # Достаём LLM-router из state — он уже сконфигурирован
    llm_router = None
    try:
        from backend.core.llm.router import get_llm_router
        llm_router = get_llm_router()
    except Exception as exc:
        logger.debug("LLMRouter unavailable, pipelines будут с fallback: %s", exc)

    result = await prepare_coffee_for_meeting(
        meeting_id=meeting_id,
        meeting_data=meeting_data,
        deliver=deliver,
        llm_router=llm_router,
    )
    return result.to_dict()


async def _load_meeting_data(meeting_id: str) -> Optional[dict[str, Any]]:
    """Подгрузить базовый meeting context из Supabase MeetFlow."""
    import os
    import httpx
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.getenv("SUPABASE_KEY", "")
    if not (supabase_url and supabase_key):
        return None
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{supabase_url}/rest/v1/meetings",
                headers={
                    "apikey": supabase_key,
                    "Authorization": f"Bearer {supabase_key}",
                },
                params={
                    "select": "id,title,created_at,transcription_text,metadata,user_id",
                    "id": f"eq.{meeting_id}",
                    "limit": "1",
                },
            )
            if r.status_code != 200:
                return None
            rows = r.json() or []
            if not rows:
                return None
            m = rows[0]
            # Phase B.2: дополнить participants/tasks/decisions через graph
            return {
                "id": m.get("id"),
                "title": m.get("title"),
                "transcript": m.get("transcription_text"),
                "summary": (m.get("metadata") or {}).get("summary") if isinstance(m.get("metadata"), dict) else None,
                "participants": (m.get("metadata") or {}).get("participants") if isinstance(m.get("metadata"), dict) else [],
                "tasks": [],
                "decisions": [],
                "key_topics": [],
                "user_id": m.get("user_id"),
            }
    except Exception as exc:
        logger.warning("_load_meeting_data failed: %s", exc)
        return None


@get("/artifacts/{meeting_id:str}", status_code=200)
async def list_artifacts_endpoint(
    meeting_id: str,
    status: Optional[str] = Parameter(query="status", default=None),
    limit: int = Parameter(query="limit", default=50),
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Список ранее сгенерированных coffee-артефактов для встречи.

    Используется UI и Mini Tess для отображения истории + retry доставки.
    Опциональные фильтры: ?status=pending|delivered|failed|superseded&limit=N.

    Auth: любой авторизованный юзер может видеть артефакты встречи
    (RLS уровня tenant'а должна проверяться отдельно — на Phase B.2
    full enforcement не делается, доверяем session.apply_tenant=False).
    """
    _extract_user(authorization)
    items = await storage_list_artifacts(
        meeting_id=meeting_id,
        status=status,
        limit=max(1, min(int(limit or 50), 200)),
    )
    return {"meeting_id": meeting_id, "count": len(items), "artifacts": items}


@get("/artifacts", status_code=200)
async def list_user_artifacts_endpoint(
    status: Optional[str] = Parameter(query="status", default=None),
    limit: int = Parameter(query="limit", default=50),
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Список coffee-артефактов текущего юзера через всю историю встреч.

    Phase B.2 — для inbox-вьюхи в UI. Возвращает recent первыми.
    """
    user_id = _extract_user(authorization)
    items = await storage_list_artifacts(
        user_id=user_id,
        status=status,
        limit=max(1, min(int(limit or 50), 200)),
    )
    return {"user_id": user_id, "count": len(items), "artifacts": items}


router = Router(
    path="/coffee",
    route_handlers=[
        prepare_coffee_endpoint,
        list_artifacts_endpoint,
        list_user_artifacts_endpoint,
    ],
    tags=["Coffee"],
)
