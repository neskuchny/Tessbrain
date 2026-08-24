"""W32 (preview content) + W37 (binary downloads) — fetch ресурсов для shared view.

Эволюция:
- W32: `FetchedResource` с текстовым preview (DLP-redacted, truncated).
- W37: `FileArtifact` + download handlers — реальные PDF/blob bytes для
  партнёров с permissions=read+download. Streaming, без буферизации
  всего файла.

Дизайн:
- `ResourceFetcher` — pluggable registry с двумя картами:
  - `_handlers[type]` → preview (text content)
  - `_download_handlers[type]` → file bytes (async iterator)
- Permission check делается в route layer (sharing.py), не здесь:
  fetcher знает только как достать файл, не кому он принадлежит.
- Best-effort: handler упал / не нашёл → None, route layer возвращает 404
- Лимиты: размер файла (50MB default) проверяется в handler'е либо в
  route layer; fetcher только стримит.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


DEFAULT_MAX_CHARS = 20_000
DEFAULT_MAX_FILE_BYTES = 50 * 1024 * 1024   # 50 MB
ELLIPSIS = "\n\n…"


@dataclass
class FetchedResource:
    resource_type: str
    resource_id: str
    title: str = ""
    content_format: str = "metadata-only"   # markdown / html / text / metadata-only
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    truncated: bool = False
    available: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "title": self.title,
            "content_format": self.content_format,
            "content": self.content,
            "metadata": self.metadata,
            "truncated": self.truncated,
            "available": self.available,
        }


# Тип fetcher'а: async fn(resource_id) -> Optional[FetchedResource].
# None → ресурс не найден / fetch failed → fallback на metadata-only.
FetcherCallable = Callable[[str], Awaitable[Optional[FetchedResource]]]


@dataclass
class FileArtifact:
    """W37: бинарный файл для скачивания партнёром."""

    file_name: str                         # "Q4_report.pdf"
    mime_type: str                         # "application/pdf"
    size_bytes: int                        # известный размер; -1 если стрим
    chunks: AsyncIterator[bytes]           # async iterator для streaming response

    def safe_filename(self) -> str:
        """Sanitize: убираем path separators, control chars, ограничиваем длину."""
        name = (self.file_name or "download").strip()
        name = name.replace("/", "_").replace("\\", "_").replace("\x00", "")
        name = "".join(c for c in name if c.isprintable())
        return name[:200] or "download"


# Тип download-handler'а: async fn(resource_id) -> Optional[FileArtifact]
DownloadCallable = Callable[[str], Awaitable[Optional["FileArtifact"]]]


def _metadata_only(resource_type: str, resource_id: str) -> FetchedResource:
    return FetchedResource(
        resource_type=resource_type,
        resource_id=resource_id,
        title="",
        content_format="metadata-only",
        content="",
        metadata={},
        truncated=False,
        available=False,
    )


def _truncate(res: FetchedResource, max_chars: int) -> FetchedResource:
    if not res.content or len(res.content) <= max_chars:
        return res
    res.content = res.content[: max_chars - len(ELLIPSIS)] + ELLIPSIS
    res.truncated = True
    return res


def _apply_dlp(content: str) -> str:
    """Best-effort DLP redaction (W14). Если фильтр не доступен — оставляем как есть."""
    if not content:
        return content
    try:
        from backend.core.llm.output_filter import filter_output
        result = filter_output(content)
        return getattr(result, "text", content) or content
    except Exception:
        return content


class ResourceFetcher:
    """Pluggable resolver — registry handlers по resource_type.

    Две независимые карты:
    - `_handlers` (W32): preview content (text)
    - `_download_handlers` (W37): file bytes (FileArtifact streaming)
    """

    def __init__(self) -> None:
        self._handlers: dict[str, FetcherCallable] = {}
        self._download_handlers: dict[str, DownloadCallable] = {}

    def register(self, resource_type: str, handler: FetcherCallable) -> None:
        if not resource_type or handler is None:
            raise ValueError("resource_type and handler required")
        self._handlers[resource_type] = handler

    def register_download(
        self, resource_type: str, handler: DownloadCallable,
    ) -> None:
        if not resource_type or handler is None:
            raise ValueError("resource_type and download handler required")
        self._download_handlers[resource_type] = handler

    def supported_types(self) -> list[str]:
        return sorted(self._handlers.keys())

    def supported_download_types(self) -> list[str]:
        return sorted(self._download_handlers.keys())

    async def fetch(
        self,
        *,
        resource_type: str,
        resource_id: str,
        max_chars: int = DEFAULT_MAX_CHARS,
        apply_dlp: bool = True,
    ) -> FetchedResource:
        handler = self._handlers.get(resource_type)
        if handler is None:
            logger.debug(
                "ResourceFetcher: no handler for %s, returning metadata-only",
                resource_type,
            )
            return _metadata_only(resource_type, resource_id)

        try:
            result = await handler(resource_id)
        except Exception as exc:
            logger.warning(
                "ResourceFetcher handler failed for %s:%s — %s",
                resource_type, resource_id, exc,
            )
            return _metadata_only(resource_type, resource_id)

        if result is None:
            return _metadata_only(resource_type, resource_id)

        if apply_dlp and result.content:
            result.content = _apply_dlp(result.content)

        return _truncate(result, max(500, int(max_chars)))

    async def download(
        self,
        *,
        resource_type: str,
        resource_id: str,
    ) -> Optional[FileArtifact]:
        """W37: вернуть FileArtifact для streaming response.

        Returns:
            None если: handler не зарегистрирован / ресурс не найден /
            handler упал. Caller (route layer) отдаёт 404.
        """
        handler = self._download_handlers.get(resource_type)
        if handler is None:
            logger.debug(
                "ResourceFetcher: no download handler for %s", resource_type,
            )
            return None
        try:
            artifact = await handler(resource_id)
        except Exception as exc:
            logger.warning(
                "ResourceFetcher download handler failed for %s:%s — %s",
                resource_type, resource_id, exc,
            )
            return None
        return artifact


# === Default production handlers ============================================
# Best-effort: если backend (Supabase / Postgres) недоступен — handler
# возвращает None, fetcher отдаёт metadata-only.

async def default_document_fetcher(resource_id: str) -> Optional[FetchedResource]:
    if not resource_id:
        return None
    try:
        from backend.core.integrations.supabase_client import get_supabase_client
    except Exception:
        return None
    try:
        supabase = get_supabase_client()
        rows = await supabase._request(
            "GET",
            "/rest/v1/documents",
            params={
                "id": f"eq.{resource_id}",
                "select": "id,title,content,doc_type,file_name,created_at,metadata",
            },
        )
    except Exception as exc:
        logger.debug("default_document_fetcher: supabase failed: %s", exc)
        return None

    if not rows:
        return None
    row = rows[0]
    return FetchedResource(
        resource_type="document",
        resource_id=resource_id,
        title=row.get("title") or row.get("file_name") or resource_id,
        content_format=_format_for_doc(row.get("doc_type")),
        content=str(row.get("content") or ""),
        metadata={
            "doc_type": row.get("doc_type"),
            "file_name": row.get("file_name"),
            "created_at": row.get("created_at"),
        },
        available=True,
    )


def _format_for_doc(doc_type: Any) -> str:
    if not doc_type:
        return "text"
    dt = str(doc_type).lower()
    if dt in {"md", "markdown"}:
        return "markdown"
    if dt in {"html", "htm"}:
        return "html"
    return "text"


async def default_meeting_fetcher(resource_id: str) -> Optional[FetchedResource]:
    if not resource_id:
        return None
    try:
        from backend.core.integrations.supabase_client import get_supabase_client
    except Exception:
        return None
    try:
        supabase = get_supabase_client()
        rows = await supabase._request(
            "GET",
            "/rest/v1/meetings",
            params={
                "id": f"eq.{resource_id}",
                "select": "id,title,transcript,started_at,duration_seconds,summary",
            },
        )
    except Exception as exc:
        logger.debug("default_meeting_fetcher: supabase failed: %s", exc)
        return None

    if not rows:
        return None
    row = rows[0]
    transcript = str(row.get("transcript") or "")
    summary = str(row.get("summary") or "")
    body = summary
    if transcript:
        # Summary первым — partner сразу видит главное; transcript ниже.
        body = (
            f"## Резюме\n{summary}\n\n## Транскрипт\n{transcript}"
            if summary
            else transcript
        )
    return FetchedResource(
        resource_type="meeting",
        resource_id=resource_id,
        title=row.get("title") or resource_id,
        content_format="markdown",
        content=body,
        metadata={
            "started_at": row.get("started_at"),
            "duration_seconds": row.get("duration_seconds"),
        },
        available=True,
    )


# === W37: default download handler (Supabase Storage best-effort) =========


_DEFAULT_CHUNK_BYTES = 64 * 1024


async def _bytes_to_chunks(data: bytes, chunk: int = _DEFAULT_CHUNK_BYTES) -> AsyncIterator[bytes]:
    for i in range(0, len(data), chunk):
        yield data[i : i + chunk]


async def default_document_download(resource_id: str) -> Optional[FileArtifact]:
    """Best-effort download через Supabase Storage.

    Берём `file_name` + `storage_path` из таблицы documents → скачиваем
    blob через Supabase Storage API → стримим chunk'ами.

    Возвращает None если: документа нет / storage_path пустой / Supabase
    недоступен / файл больше DEFAULT_MAX_FILE_BYTES.
    """
    if not resource_id:
        return None
    try:
        from backend.core.integrations.supabase_client import get_supabase_client
    except Exception:
        return None
    try:
        supabase = get_supabase_client()
        rows = await supabase._request(
            "GET",
            "/rest/v1/documents",
            params={
                "id": f"eq.{resource_id}",
                "select": "id,file_name,storage_path,storage_bucket,mime_type,file_size",
            },
        )
    except Exception as exc:
        logger.debug("default_document_download: lookup failed: %s", exc)
        return None
    if not rows:
        return None
    row = rows[0]
    storage_path = (row.get("storage_path") or "").strip()
    if not storage_path:
        return None
    file_size = int(row.get("file_size") or 0)
    if file_size > DEFAULT_MAX_FILE_BYTES:
        logger.warning(
            "default_document_download: %s exceeds limit (%d > %d)",
            resource_id, file_size, DEFAULT_MAX_FILE_BYTES,
        )
        return None
    bucket = row.get("storage_bucket") or "documents"

    try:
        # Supabase Storage REST: GET /storage/v1/object/<bucket>/<path>
        blob = await supabase._request(
            "GET",
            f"/storage/v1/object/{bucket}/{storage_path}",
            raw=True,
        )
    except Exception as exc:
        logger.warning("default_document_download: blob fetch failed: %s", exc)
        return None
    if not blob or not isinstance(blob, (bytes, bytearray)):
        return None
    if len(blob) > DEFAULT_MAX_FILE_BYTES:
        return None

    return FileArtifact(
        file_name=row.get("file_name") or f"{resource_id}.bin",
        mime_type=row.get("mime_type") or "application/octet-stream",
        size_bytes=len(blob),
        chunks=_bytes_to_chunks(bytes(blob)),
    )


# === Module-level singleton =================================================

_default_instance: Optional[ResourceFetcher] = None


def get_resource_fetcher() -> ResourceFetcher:
    """Возвращает global instance с default handler'ами.

    Тесты используют new ResourceFetcher() локально — singleton не глобально
    мутируется.
    """
    global _default_instance
    if _default_instance is None:
        f = ResourceFetcher()
        f.register("document", default_document_fetcher)
        f.register("meeting", default_meeting_fetcher)
        # W37: только document имеет download; meeting transcript отдаётся как
        # text content, отдельный binary download не нужен.
        f.register_download("document", default_document_download)
        _default_instance = f
    return _default_instance


def reset_resource_fetcher() -> None:
    """Тестовый helper — сбросить cached singleton."""
    global _default_instance
    _default_instance = None


__all__ = [
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MAX_FILE_BYTES",
    "DownloadCallable",
    "FetchedResource",
    "FetcherCallable",
    "FileArtifact",
    "ResourceFetcher",
    "default_document_download",
    "default_document_fetcher",
    "default_meeting_fetcher",
    "get_resource_fetcher",
    "reset_resource_fetcher",
]
