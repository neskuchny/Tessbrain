# -*- coding: utf-8 -*-
"""Bookmarks API (W17).

URL-закладки с описанием, тегами и семантическим поиском. Живут в
DocumentsPanel UI вместе с документами и презентациями.

Endpoints:
- `POST   /api/v1/bookmarks`           — добавить (auto-enrich URL через LLM)
- `GET    /api/v1/bookmarks`            — list мои + по фильтрам
- `GET    /api/v1/bookmarks/search?q=`  — семантический поиск (Qdrant)
- `GET    /api/v1/bookmarks/{id}`        — read
- `PATCH  /api/v1/bookmarks/{id}`        — обновить title/description/tags
- `DELETE /api/v1/bookmarks/{id}`        — удалить (cascade Qdrant)

Все endpoints — JWT-authed, tenant-scoped через RLS.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from litestar import Router, delete, get, patch, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter

from backend.core.observability import audit_log

logger = logging.getLogger(__name__)


def _decode_jwt_unsafe(token: str) -> dict[str, Any]:
    # issue #107 T-1: verify-first (strict → {} на плохой подписи → 401;
    # compat по умолчанию → decode без проверки, как раньше).
    from backend.core.auth.service_token import decode_claims_guarded
    return decode_claims_guarded(token)


def _extract_user(authorization: Optional[str]) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.split(" ", 1)[1]
    try:
        claims = _decode_jwt_unsafe(token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")
    user_id = claims.get("sub") or claims.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing sub/user_id")
    return user_id


def _build_service():
    """Лениво инстанцируем зависимости — на routes не тянем тяжёлые модули."""
    from backend.core.bookmarks.service import BookmarkService
    postgres = qdrant = embedder = llm = None
    try:
        from backend.db.postgres import PostgresClient
        postgres = PostgresClient()
    except Exception as exc:
        logger.debug("Bookmarks: postgres unavailable: %s", exc)
    try:
        from backend.db.qdrant import QdrantClient
        qdrant = QdrantClient()
    except Exception as exc:
        logger.debug("Bookmarks: qdrant unavailable: %s", exc)
    try:
        from backend.core.llm.ollama_embeddings import get_ollama_embeddings
        embedder = get_ollama_embeddings()
    except Exception as exc:
        logger.debug("Bookmarks: embedder unavailable: %s", exc)
    try:
        from backend.core.llm.router import LLMRouter
        llm = LLMRouter()
    except Exception as exc:
        logger.debug("Bookmarks: llm unavailable: %s", exc)
    return BookmarkService(postgres=postgres, qdrant=qdrant, embedder=embedder, llm_router=llm)


def _enterprise_mode() -> bool:
    try:
        from backend.config import get_settings
        return bool(getattr(get_settings(), "enterprise_mode", False))
    except Exception:
        return False


# === Endpoints ============================================================

@post("/")
async def add_bookmark(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Добавить bookmark.

    Body:
        {
          "url": "https://...",
          "section_id": "regulations",        # опц
          "title": "...",                       # опц — иначе LLM сгенерит
          "description": "...",                 # опц
          "tags": ["...", "..."],              # опц
          "user_hint": "наша главная презентация для клиентов",   # опц
          "auto_enrich": true                  # default true
        }
    """
    user_id = _extract_user(authorization)
    url = (data.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url is required")

    svc = _build_service()
    try:
        bookmark = await svc.add(
            user_id=user_id,
            url=url,
            section_id=data.get("section_id"),
            title=data.get("title"),
            description=data.get("description"),
            tags=data.get("tags"),
            user_hint=data.get("user_hint"),
            auto_enrich=bool(data.get("auto_enrich", True)),
            enterprise_mode=_enterprise_mode(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Bookmark add failed")
        raise HTTPException(status_code=500, detail=f"add failed: {exc}")

    await audit_log.emit(
        action="bookmark.add",
        user_id=user_id,
        resource=f"bookmark:{bookmark.id}",
        metadata={
            "section": bookmark.section_id,
            "tags_count": len(bookmark.tags),
            "had_fetch_error": bool(bookmark.fetch_error),
        },
    )
    return {"bookmark": bookmark.to_dict()}


@get("/search")
async def search_bookmarks(
    q: str = Parameter(query="q"),
    section_id: Optional[str] = Parameter(query="section_id", default=None),
    top_k: int = Parameter(query="top_k", default=20),
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Семантический поиск через Qdrant.

    Возвращает hits с score, отсортированные по релевантности.
    """
    user_id = _extract_user(authorization)
    if not q or not q.strip():
        return {"hits": [], "count": 0}

    svc = _build_service()
    hits = await svc.search(query=q.strip(), user_id=user_id, section_id=section_id, top_k=top_k)
    return {"hits": hits, "count": len(hits)}


@delete("/{bookmark_id:str}", status_code=204)
async def delete_bookmark(
    bookmark_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> None:
    user_id = _extract_user(authorization)
    svc = _build_service()
    try:
        await svc.delete(bookmark_id=bookmark_id)
    except Exception as exc:
        logger.exception("Bookmark delete failed")
        raise HTTPException(status_code=500, detail=f"delete failed: {exc}")
    await audit_log.emit(
        action="bookmark.delete",
        user_id=user_id,
        resource=f"bookmark:{bookmark_id}",
    )


router = Router(
    path="/bookmarks",
    route_handlers=[add_bookmark, search_bookmarks, delete_bookmark],
    tags=["Bookmarks"],
)
