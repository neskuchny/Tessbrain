"""OpenHands artifacts collector (W20).

Вытаскивает workspace files из running OpenHands conversation. Sandbox
изолирован per conversation, файлы доступны через REST API.

API endpoints OpenHands (по версии runtime):
- `GET /api/conversations/{cid}/list-files` — listing
- `GET /api/conversations/{cid}/select-file?file=...` — содержимое

Дизайн:
- Pure HTTP-клиент, async через httpx
- Best-effort: при ошибке возвращаем что собрали, не raises
- Лимиты: max_files (default 50), max_total_bytes (default 10MB)
  чтобы не утопить в гигабайтах node_modules
- Skip patterns: .git/, node_modules/, __pycache__/, .venv/
- Per-file size cap (1MB) — большие бинарники только metadata
- Возвращаем `list[Artifact]` совместимый с TaskResult.artifacts
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


_DEFAULT_MAX_FILES = 50
_DEFAULT_MAX_TOTAL_BYTES = 10 * 1024 * 1024
_DEFAULT_PER_FILE_MAX = 1 * 1024 * 1024
_DEFAULT_TIMEOUT = 15.0

_SKIP_PATTERNS = (
    ".git/", "node_modules/", "__pycache__/", ".venv/", "venv/",
    ".pytest_cache/", ".mypy_cache/", ".ruff_cache/", "dist/", "build/",
    ".next/", ".cache/",
)


@dataclass
class CollectedArtifact:
    name: str
    kind: str = "file"             # "file" | "binary" | "url"
    path: Optional[str] = None     # абсолютный путь в sandbox (для логов)
    content: Optional[str] = None  # текст (если читали)
    size_bytes: int = 0
    truncated: bool = False        # True если файл больше per_file_max

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
        }
        if self.path:
            out["path"] = self.path
        if self.content is not None:
            out["content"] = self.content
        if self.truncated:
            out["truncated"] = True
        return out


def _is_skipped(path: str) -> bool:
    p = path.lstrip("/")
    return any(pat in p for pat in _SKIP_PATTERNS)


def _is_text_path(path: str) -> bool:
    """Эвристика: текстовый ли файл по расширению."""
    text_exts = {
        ".md", ".txt", ".html", ".css", ".js", ".jsx", ".ts", ".tsx",
        ".py", ".json", ".yaml", ".yml", ".toml", ".sh", ".rs", ".go",
        ".java", ".kt", ".swift", ".rb", ".php", ".sql", ".csv",
        ".xml", ".env", ".log", ".rst", ".tex", ".vue", ".svelte",
    }
    _, ext = os.path.splitext(path.lower())
    return ext in text_exts or "." not in os.path.basename(path)


async def list_files(
    *,
    base_url: str,
    conversation_id: str,
    headers: Optional[dict[str, str]] = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> list[str]:
    """Листинг файлов в workspace conversation. Возвращает relative paths."""
    try:
        import httpx
    except ImportError:
        logger.warning("httpx not installed; cannot list OpenHands files")
        return []

    url = f"{base_url.rstrip('/')}/api/conversations/{conversation_id}/list-files"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers=headers or {})
    except httpx.HTTPError as exc:
        logger.warning("OpenHands list-files network error: %s", exc)
        return []
    if resp.status_code >= 400:
        logger.warning("OpenHands list-files HTTP %d", resp.status_code)
        return []
    try:
        data = resp.json()
    except Exception as exc:
        logger.warning("OpenHands list-files invalid JSON: %s", exc)
        return []

    # Несколько схем ответа в разных версиях OpenHands.
    if isinstance(data, list):
        return [str(p) for p in data]
    if isinstance(data, dict):
        files = data.get("files") or data.get("paths") or []
        if isinstance(files, list):
            return [str(p) for p in files]
    return []


async def fetch_file(
    *,
    base_url: str,
    conversation_id: str,
    path: str,
    headers: Optional[dict[str, str]] = None,
    timeout: float = _DEFAULT_TIMEOUT,
    max_bytes: int = _DEFAULT_PER_FILE_MAX,
) -> Optional[CollectedArtifact]:
    """Скачать один файл. None при ошибке."""
    try:
        import httpx
    except ImportError:
        return None

    url = f"{base_url.rstrip('/')}/api/conversations/{conversation_id}/select-file"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, params={"file": path}, headers=headers or {})
    except httpx.HTTPError as exc:
        logger.debug("OpenHands fetch_file %s network error: %s", path, exc)
        return None
    if resp.status_code >= 400:
        return None

    body = resp.content[:max_bytes]
    truncated = len(resp.content) > max_bytes
    is_text = _is_text_path(path)
    artifact = CollectedArtifact(
        name=path,
        path=path,
        size_bytes=len(resp.content),
        truncated=truncated,
        kind="file" if is_text else "binary",
    )
    if is_text:
        try:
            artifact.content = body.decode("utf-8", errors="replace")
        except Exception:
            artifact.kind = "binary"
            artifact.content = None
    return artifact


async def collect_artifacts(
    *,
    base_url: str,
    conversation_id: str,
    api_key: Optional[str] = None,
    max_files: int = _DEFAULT_MAX_FILES,
    max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES,
    per_file_max: int = _DEFAULT_PER_FILE_MAX,
) -> list[CollectedArtifact]:
    """Собрать workspace artifacts для conversation.

    Алгоритм:
    1. list_files → relative paths
    2. фильтр skip-patterns
    3. для каждого fetch_file пока не превысим лимиты
    4. бинарники получают metadata без content

    Возвращает list, отсортированный по name. Никогда не raises.
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None

    paths = await list_files(
        base_url=base_url,
        conversation_id=conversation_id,
        headers=headers,
    )
    if not paths:
        return []

    # Сортируем для детерминированности; топ-уровневые .md/.html идут раньше
    # (вероятно — что выдал executor как deliverable).
    paths.sort(key=lambda p: (p.count("/"), p.lower()))
    paths = [p for p in paths if not _is_skipped(p)][:max_files * 2]

    out: list[CollectedArtifact] = []
    total_bytes = 0
    for path in paths:
        if len(out) >= max_files:
            break
        if total_bytes >= max_total_bytes:
            break
        artifact = await fetch_file(
            base_url=base_url,
            conversation_id=conversation_id,
            path=path,
            headers=headers,
            max_bytes=per_file_max,
        )
        if artifact is None:
            continue
        out.append(artifact)
        total_bytes += artifact.size_bytes

    return out


__all__ = [
    "CollectedArtifact",
    "collect_artifacts",
    "fetch_file",
    "list_files",
]
