# -*- coding: utf-8 -*-
"""Линза кластеров компании — API.

GET  /communities          — сохранённые кластеры (без пересборки)
POST /communities/rebuild  — пересобрать (Louvain + LLM-имена; fingerprint
                             не изменился → отдаст сохранённое без LLM)

Auth: Bearer (trusted_user_id), как client_sim/chat_sources.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from litestar import Router, get, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter

logger = logging.getLogger(__name__)


def _caller(authorization: Optional[str]) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        from backend.core.auth.service_token import trusted_user_id
        uid, src = trusted_user_id({"authorization": authorization}, "")
        if src != "unverified" and uid:
            return str(uid)
    except PermissionError:
        raise HTTPException(status_code=401, detail="token not authorized")
    except Exception:
        pass
    try:
        import jwt
        payload = jwt.decode(authorization[7:],
                             options={"verify_signature": False})
        sub = payload.get("sub") or payload.get("user_id")
        if sub:
            return str(sub)
    except Exception:
        pass
    raise HTTPException(status_code=401, detail="Not authenticated")


@get("/communities")
async def communities_list(
    authorization: Optional[str] = Parameter(header="Authorization",
                                             default=None),
) -> dict[str, Any]:
    from backend.core.insight.communities import list_communities
    user_id = _caller(authorization)
    return {"status": "success", **list_communities(user_id)}


@post("/communities/rebuild")
async def communities_rebuild(
    data: dict[str, Any] | None = None,
    authorization: Optional[str] = Parameter(header="Authorization",
                                             default=None),
) -> dict[str, Any]:
    from backend.core.insight.communities import build_communities
    user_id = _caller(authorization)
    force = bool((data or {}).get("force"))
    return await build_communities(user_id, with_llm=True, force=force)


router = Router(path="", route_handlers=[communities_list,
                                         communities_rebuild],
                tags=["Community Lens"])
