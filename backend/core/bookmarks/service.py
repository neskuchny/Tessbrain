"""BookmarkService — primary CRUD + Qdrant intg + auto-enrich (W17).

Дизайн:
- БД-операции делегируются Postgres (через единый PostgresClient).
  RLS уже привязывает к tenant_id, мы добавляем только параметры.
- Embeddings кладутся в Qdrant в коллекцию `bookmarks`. Один namespace
  на инстанс — tenant_id хранится как payload-фильтр, как в основном
  storage (см. backend/db/qdrant.py: TENANT_PAYLOAD_KEY).
- Enrich (LLM) и fetch (HTTP) — best-effort: если упали, сохраняем
  что есть (минимум — URL + user-hint). Никогда не блокируем create
  только из-за неработающего LLM/сети.
- payload_text для embedding = `title + description + ' '.join(tags)`
  (компактно, поисковые слова в одном blob'е).
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


_BOOKMARKS_COLLECTION = "bookmarks"
_VECTOR_PAYLOAD_TYPE = "bookmark"


@dataclass
class Bookmark:
    """Public model — то что отдаём API + UI."""
    id: str
    user_id: str
    url: str
    title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    section_id: Optional[str] = None
    content_summary: Optional[str] = None
    qdrant_point_id: Optional[str] = None
    status: str = "active"
    last_fetched_at: Optional[str] = None
    fetch_error: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    tenant_id: Optional[str] = None

    @classmethod
    def from_row(cls, row: Any) -> "Bookmark":
        """Конструктор из dict-like (asyncpg Record / dict)."""
        get = row.get if hasattr(row, "get") else lambda k, default=None: row[k] if k in row else default  # type: ignore[index]
        return cls(
            id=str(get("id")),
            user_id=str(get("user_id")),
            url=str(get("url")),
            title=str(get("title") or ""),
            description=str(get("description") or ""),
            tags=list(get("tags") or []),
            section_id=get("section_id"),
            content_summary=get("content_summary"),
            qdrant_point_id=get("qdrant_point_id"),
            status=str(get("status") or "active"),
            last_fetched_at=get("last_fetched_at"),
            fetch_error=get("fetch_error"),
            created_at=get("created_at"),
            updated_at=get("updated_at"),
            tenant_id=get("tenant_id"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "url": self.url,
            "title": self.title,
            "description": self.description,
            "tags": list(self.tags),
            "section_id": self.section_id,
            "content_summary": self.content_summary,
            "status": self.status,
            "last_fetched_at": str(self.last_fetched_at) if self.last_fetched_at else None,
            "fetch_error": self.fetch_error,
            "created_at": str(self.created_at) if self.created_at else None,
            "updated_at": str(self.updated_at) if self.updated_at else None,
        }


def make_search_payload(*, title: str, description: str, tags: list[str]) -> str:
    """Текст для embedding: одна строка, нормализованные пробелы."""
    parts = [p for p in (title, description, " ".join(tags)) if p]
    return " | ".join(parts)


class BookmarkService:
    """High-level CRUD над bookmarks.

    Не singleton'ится глобально — caller (route handler) создаёт инстанс
    с зависимостями (`postgres`, `qdrant`, `embedder`, `llm_router`).
    Все зависимости опциональные: BookmarkService умеет работать
    minimally (без enrich, без vector index) — но тогда поиск будет
    только substring-based.
    """

    def __init__(
        self,
        *,
        postgres: Any = None,
        qdrant: Any = None,
        embedder: Any = None,
        llm_router: Any = None,
    ) -> None:
        self.postgres = postgres
        self.qdrant = qdrant
        self.embedder = embedder
        self.llm_router = llm_router

    # === Helpers ========================================================

    @staticmethod
    def _new_id() -> str:
        return str(uuid.uuid4())

    async def _embed(self, text: str) -> Optional[list[float]]:
        if self.embedder is None or not text.strip():
            return None
        try:
            return await self.embedder.embed(text)
        except Exception as exc:
            logger.warning("BookmarkService: embed failed: %s", exc)
            return None

    async def _index(
        self,
        *,
        bookmark_id: str,
        text: str,
        title: str,
        description: str,
        tags: list[str],
        section_id: Optional[str],
        url: str,
    ) -> Optional[str]:
        """Положить в Qdrant; вернуть point_id или None."""
        if self.qdrant is None:
            return None
        vector = await self._embed(text)
        if not vector:
            return None
        point_id = self._new_id()
        payload = {
            "type": _VECTOR_PAYLOAD_TYPE,
            "bookmark_id": bookmark_id,
            "url": url,
            "title": title,
            "description": description,
            "tags": tags,
            "section_id": section_id,
        }
        try:
            await self.qdrant.upsert(
                points=[{"id": point_id, "vector": vector, "payload": payload}],
                collection=_BOOKMARKS_COLLECTION,
            )
        except Exception as exc:
            logger.warning("BookmarkService: qdrant upsert failed: %s", exc)
            return None
        return point_id

    async def _enrich_url(
        self,
        *,
        url: str,
        user_hint: Optional[str],
        enterprise_mode: bool,
    ) -> tuple[str, str, list[str], Optional[str], Optional[str]]:
        """Вернуть (title, description, tags, content_summary, fetch_error).

        Если fetch / LLM упали — возвращаем fallback значения и
        соответствующий fetch_error. Не raises.
        """
        from backend.core.bookmarks.enricher import enrich
        from backend.core.bookmarks.fetcher import FetchError, fetch_url

        title = ""
        text = ""
        fetch_error: Optional[str] = None
        try:
            fetched = await fetch_url(url, enterprise_mode=enterprise_mode)
            title = fetched.title
            text = fetched.text
        except FetchError as exc:
            fetch_error = str(exc)
            logger.info("BookmarkService: fetch failed for %s: %s", url, exc)

        result = await enrich(
            url=url,
            page_title=title,
            page_text=text,
            user_hint=user_hint,
            llm_router=self.llm_router,
        )
        return (
            result.title,
            result.description,
            result.tags,
            text[:4000] if text else None,
            fetch_error,
        )

    # === CRUD ===========================================================

    async def add(
        self,
        *,
        user_id: str,
        url: str,
        section_id: Optional[str] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        tags: Optional[list[str]] = None,
        user_hint: Optional[str] = None,
        auto_enrich: bool = True,
        enterprise_mode: bool = False,
    ) -> Bookmark:
        """Добавить bookmark.

        Если `auto_enrich=True` — fetch URL и enrich через LLM (даже если
        title/description заданы caller'ом — мы дополним tags и summary).
        Если caller заполнил все три (title/description/tags), enrich
        пропускается (мы доверяем).
        """
        if not url or "://" not in url:
            raise ValueError("url must be a full http(s) URL")

        bookmark_id = self._new_id()
        eff_title = (title or "").strip()
        eff_description = (description or "").strip()
        eff_tags = list(tags or [])
        content_summary: Optional[str] = None
        fetch_error: Optional[str] = None

        need_enrich = auto_enrich and not (eff_title and eff_description and eff_tags)
        if need_enrich:
            (
                gen_title,
                gen_description,
                gen_tags,
                content_summary,
                fetch_error,
            ) = await self._enrich_url(
                url=url, user_hint=user_hint, enterprise_mode=enterprise_mode,
            )
            if not eff_title:
                eff_title = gen_title
            if not eff_description:
                eff_description = gen_description
            if not eff_tags:
                eff_tags = gen_tags

        # Index в Qdrant до записи в БД — point_id нужен в строке.
        search_text = make_search_payload(
            title=eff_title, description=eff_description, tags=eff_tags,
        )
        point_id = await self._index(
            bookmark_id=bookmark_id,
            text=search_text,
            title=eff_title,
            description=eff_description,
            tags=eff_tags,
            section_id=section_id,
            url=url,
        )

        # Persist в Postgres.
        if self.postgres is not None:
            await self._persist(
                bookmark_id=bookmark_id,
                user_id=user_id,
                url=url,
                section_id=section_id,
                title=eff_title,
                description=eff_description,
                tags=eff_tags,
                content_summary=content_summary,
                qdrant_point_id=point_id,
                fetch_error=fetch_error,
            )

        return Bookmark(
            id=bookmark_id,
            user_id=user_id,
            url=url,
            section_id=section_id,
            title=eff_title,
            description=eff_description,
            tags=eff_tags,
            content_summary=content_summary,
            qdrant_point_id=point_id,
            status="broken" if fetch_error else "active",
            fetch_error=fetch_error,
        )

    async def _persist(
        self,
        *,
        bookmark_id: str,
        user_id: str,
        url: str,
        section_id: Optional[str],
        title: str,
        description: str,
        tags: list[str],
        content_summary: Optional[str],
        qdrant_point_id: Optional[str],
        fetch_error: Optional[str],
    ) -> None:
        """Записать строку в bookmarks через PostgresClient.

        SQL — ON CONFLICT по (tenant_id, url) → UPDATE; идемпотентно.
        """
        sql = """
        INSERT INTO public.bookmarks
            (id, user_id, url, section_id, title, description, tags,
             content_summary, qdrant_point_id, status, fetch_error,
             last_fetched_at)
        VALUES (:id, :user_id, :url, :section_id, :title, :description,
                CAST(:tags AS TEXT[]), :content_summary, :qdrant_point_id,
                :status, :fetch_error, now())
        ON CONFLICT (tenant_id, url) DO UPDATE SET
            title = EXCLUDED.title,
            description = EXCLUDED.description,
            tags = EXCLUDED.tags,
            content_summary = EXCLUDED.content_summary,
            qdrant_point_id = EXCLUDED.qdrant_point_id,
            status = EXCLUDED.status,
            fetch_error = EXCLUDED.fetch_error,
            last_fetched_at = now()
        """
        params = {
            "id": bookmark_id,
            "user_id": user_id,
            "url": url,
            "section_id": section_id,
            "title": title,
            "description": description,
            "tags": tags,
            "content_summary": content_summary,
            "qdrant_point_id": qdrant_point_id,
            "status": "broken" if fetch_error else "active",
            "fetch_error": fetch_error,
        }
        try:
            async with self.postgres.session() as session:  # type: ignore[attr-defined]
                await session.execute(sql, params)
        except Exception as exc:
            logger.error("BookmarkService: persist failed: %s", exc)
            raise

    async def search(
        self,
        *,
        query: str,
        user_id: Optional[str] = None,
        section_id: Optional[str] = None,
        top_k: int = 20,
    ) -> list[dict[str, Any]]:
        """Семантический поиск через Qdrant. Возвращает payload + score.

        Если qdrant/embedder отсутствуют — возвращаем пустой список
        (caller fallback'ит на DB ILIKE).
        """
        if not query or self.qdrant is None or self.embedder is None:
            return []
        vector = await self._embed(query)
        if not vector:
            return []
        filter_conditions: dict[str, Any] = {"type": _VECTOR_PAYLOAD_TYPE}
        if section_id:
            filter_conditions["section_id"] = section_id
        try:
            hits = await self.qdrant.search(
                vector=vector,
                limit=top_k,
                filter_conditions=filter_conditions,
                collection=_BOOKMARKS_COLLECTION,
            )
        except Exception as exc:
            logger.warning("BookmarkService: qdrant search failed: %s", exc)
            return []
        return [
            {
                "score": h.get("score", 0.0),
                "bookmark_id": h.get("payload", {}).get("bookmark_id"),
                "url": h.get("payload", {}).get("url"),
                "title": h.get("payload", {}).get("title"),
                "description": h.get("payload", {}).get("description"),
                "tags": h.get("payload", {}).get("tags") or [],
                "section_id": h.get("payload", {}).get("section_id"),
            }
            for h in (hits or [])
        ]

    async def delete(self, *, bookmark_id: str) -> None:
        """Удалить bookmark из БД и Qdrant (best-effort)."""
        if self.postgres is not None:
            try:
                async with self.postgres.session() as session:  # type: ignore[attr-defined]
                    await session.execute(
                        "DELETE FROM public.bookmarks WHERE id = :id",
                        {"id": bookmark_id},
                    )
            except Exception as exc:
                logger.error("BookmarkService: db delete failed: %s", exc)
                raise
        if self.qdrant is not None:
            try:
                await self.qdrant.delete_by_filter(
                    filter_conditions={"bookmark_id": bookmark_id},
                    collection=_BOOKMARKS_COLLECTION,
                )
            except Exception as exc:
                logger.warning("BookmarkService: qdrant delete failed: %s", exc)


__all__ = ["Bookmark", "BookmarkService", "make_search_payload"]
