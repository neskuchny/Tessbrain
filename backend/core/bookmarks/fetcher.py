"""URL fetcher для bookmark auto-enrich (W17).

Дизайн:
- Single-purpose: даём URL → возвращаем (status, title, plain_text).
- Air-gap-aware: если `enterprise_mode=true`, запрещаем не-internal URL'ы
  (используем `_is_internal_url` из config — тот же gate что W8 LLM).
- Никаких heavy deps: лёгкий HTML-stripper из stdlib + httpx (уже есть).
- Размер ответа лимитирован (`MAX_BYTES`) — против накачки 100MB-страниц.
- Только text/html и application/pdf (последний — оставлен hook'ом, парсинг
  опционально через pdfplumber, но не делаем по умолчанию).
- robots.txt не читаем намеренно: bookmark — это URL который оператор
  уже выбрал добавить; respect robots — задача crawler'ов.

Пытаемся быть устойчивыми к проблемным сайтам — каждая ошибка возвращает
`FetchError` вместо raise; caller сам решает что с ней делать.
"""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


MAX_BYTES = 5 * 1024 * 1024     # 5 MB — типичная страница ≤200KB
DEFAULT_TIMEOUT = 10.0          # сек
USER_AGENT = "TessentBookmarkFetcher/1.0 (+https://tessent.ai)"


_ALLOWED_CONTENT_TYPES = (
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "application/pdf",
)


# Очень простая html-cleanup без дополнительных зависимостей.
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)


class FetchError(Exception):
    """Любая ошибка fetch (сеть, content-type, размер, air-gap-rejection)."""


class AirGapRejected(FetchError):
    """URL отклонён в enterprise air-gap режиме."""


@dataclass(frozen=True)
class FetchResult:
    url: str
    status_code: int
    content_type: str
    title: str
    text: str          # plain text, обрезано до ~MAX_TEXT_LEN
    bytes_read: int


MAX_TEXT_LEN = 20_000   # 20K плейн-текста хватает для LLM-summary


def is_url_safe(url: str, *, enterprise_mode: bool = False) -> bool:
    """Можно ли fetch'ить этот URL.

    - enterprise_mode=True → только internal (loopback / RFC1918 / *.local /
      *.svc.cluster.local) — переиспользуем `_is_internal_url` из W8.
    - enterprise_mode=False → пропускаем всё что выглядит как `http(s)://...`.
    """
    if not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    if not parsed.hostname:
        return False
    if not enterprise_mode:
        return True
    try:
        from backend.config import _is_internal_url
    except ImportError:
        return False
    return _is_internal_url(url)


def extract_title(html_text: str) -> str:
    """Извлечь содержимое <title>, или вернуть пустую строку."""
    m = _TITLE_RE.search(html_text)
    if not m:
        return ""
    raw = m.group(1)
    raw = _TAG_RE.sub("", raw)  # тэги внутри title — рубим
    return html.unescape(raw).strip()


def html_to_text(html_text: str) -> str:
    """Очень простой HTML→plain converter.

    Удаляет <script>/<style>, потом теги, потом нормализует whitespace.
    Для production-grade extraction рекомендуется trafilatura/readability,
    но они тяжёлые deps; для MVP-bookmark достаточно этого.
    """
    cleaned = _SCRIPT_STYLE_RE.sub(" ", html_text)
    cleaned = _TAG_RE.sub(" ", cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    return cleaned


async def fetch_url(
    url: str,
    *,
    enterprise_mode: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = MAX_BYTES,
) -> FetchResult:
    """Скачать URL и извлечь title + plain text.

    Raises:
        AirGapRejected: enterprise_mode=True и URL не internal.
        FetchError: HTTP-ошибка, неподдерживаемый content-type, слишком большой
                    payload, timeout, network failure.
    """
    if not is_url_safe(url, enterprise_mode=enterprise_mode):
        if enterprise_mode:
            raise AirGapRejected(
                f"URL {url!r} is not internal; rejected in enterprise mode"
            )
        raise FetchError(f"URL {url!r} is not http(s) or has no host")

    try:
        import httpx
    except ImportError as exc:
        raise FetchError("httpx not installed; cannot fetch URLs") from exc

    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/pdf,*/*"}
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, headers=headers,
        ) as client:
            resp = await client.get(url)
    except httpx.TimeoutException as exc:
        raise FetchError(f"timeout after {timeout}s") from exc
    except httpx.HTTPError as exc:
        raise FetchError(f"http error: {exc}") from exc

    if resp.status_code >= 400:
        raise FetchError(f"HTTP {resp.status_code}")

    content_type = (resp.headers.get("content-type") or "").lower().split(";")[0].strip()
    if content_type and not content_type.startswith(_ALLOWED_CONTENT_TYPES):
        raise FetchError(f"content-type {content_type!r} not allowed")

    body = resp.content[:max_bytes]
    if len(resp.content) > max_bytes:
        logger.debug("fetch_url: truncated body from %d to %d bytes", len(resp.content), max_bytes)

    if content_type.startswith("application/pdf"):
        # PDF parsing — оставлен для отдельного PR. Пока просто помечаем.
        return FetchResult(
            url=str(resp.url),
            status_code=resp.status_code,
            content_type=content_type,
            title="",
            text="",
            bytes_read=len(body),
        )

    try:
        html_text = body.decode("utf-8", errors="replace")
    except Exception as exc:
        raise FetchError(f"decode error: {exc}") from exc

    title = extract_title(html_text)
    text = html_to_text(html_text)[:MAX_TEXT_LEN]

    return FetchResult(
        url=str(resp.url),
        status_code=resp.status_code,
        content_type=content_type,
        title=title,
        text=text,
        bytes_read=len(body),
    )


__all__ = [
    "AirGapRejected",
    "FetchError",
    "FetchResult",
    "extract_title",
    "fetch_url",
    "html_to_text",
    "is_url_safe",
]
