"""Playwright wrapper для рендеринга HTML/URL → PNG bytes (W22).

Используется VisualDiffValidator для генерации скриншотов разных viewports
(desktop / mobile) перед multimodal LLM-judge.

Дизайн:
- Lazy import playwright — модуль импортируется без heavy deps
- Async-only API (Playwright async chromium)
- Per-screenshot timeout 30s
- Виртуальный display не нужен (headless)
- Виды source: raw HTML string, file path, URL
- Возвращает bytes PNG; caller сам base64'ит для multimodal payload

Air-gap-aware: для URL вход проверяется через `is_url_safe` из W17
(fetcher) — не пробуем рендерить external URL когда enterprise_mode=true.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


_DEFAULT_TIMEOUT_MS = 30_000   # 30s
_MAX_PAGE_LOAD_TIMEOUT_MS = 15_000


# Стандартные viewports — desktop + mobile.
_DEFAULT_VIEWPORTS: tuple[tuple[str, int, int], ...] = (
    ("desktop", 1440, 900),
    ("mobile", 375, 812),
)


class ScreenshotError(RuntimeError):
    """Render failed (Playwright not installed / page load timeout / etc.)."""


@dataclass(frozen=True)
class Screenshot:
    """Один скриншот."""
    viewport: str           # "desktop" / "mobile"
    width: int
    height: int
    png_bytes: bytes
    source: str             # описание source — для логов

    def to_base64(self) -> str:
        return base64.b64encode(self.png_bytes).decode("ascii")

    def to_metadata_dict(self) -> dict[str, Any]:
        """Безопасный сериализатор без bytes."""
        return {
            "viewport": self.viewport,
            "width": self.width,
            "height": self.height,
            "size_bytes": len(self.png_bytes),
            "source": self.source,
        }


async def _ensure_playwright() -> Any:
    """Lazy-import. Raises ScreenshotError если недоступен."""
    try:
        from playwright.async_api import async_playwright  # type: ignore
        return async_playwright
    except ImportError as exc:
        raise ScreenshotError(
            "playwright not installed. Run: pip install playwright "
            "&& python -m playwright install chromium"
        ) from exc


def _detect_source_kind(source: str) -> str:
    """Определить как скармливать source в Playwright."""
    s = source.strip()
    if s.startswith(("http://", "https://")):
        return "url"
    if s.startswith("<") or "<html" in s.lower() or "<body" in s.lower():
        return "html"
    if Path(s).exists():
        return "file"
    return "html"   # fallback — попытка раcсматривать как HTML-fragment


async def screenshot(
    *,
    source: str,
    viewports: tuple[tuple[str, int, int], ...] = _DEFAULT_VIEWPORTS,
    timeout_ms: int = _DEFAULT_TIMEOUT_MS,
    enterprise_mode: bool = False,
) -> list[Screenshot]:
    """Сделать screenshot во всех viewports.

    Args:
        source: URL / raw HTML / путь к HTML-файлу
        viewports: список (name, width, height)
        timeout_ms: общий timeout на всю операцию
        enterprise_mode: если True и source — URL, проверяем is_url_safe

    Returns:
        list[Screenshot] длиной len(viewports). Если render одного viewport
        упал — он отсутствует в списке, остальные — yes (best-effort).

    Raises:
        ScreenshotError: если playwright недоступен или весь URL отклонён
                         в enterprise_mode.
    """
    if not source.strip():
        raise ScreenshotError("empty source")

    kind = _detect_source_kind(source)

    # Air-gap check для URL.
    if kind == "url" and enterprise_mode:
        try:
            from backend.core.bookmarks.fetcher import is_url_safe
        except ImportError:
            raise ScreenshotError("cannot validate URL in enterprise mode")
        if not is_url_safe(source, enterprise_mode=True):
            raise ScreenshotError(
                f"URL {source!r} is not internal; rejected in enterprise_mode"
            )

    factory = await _ensure_playwright()

    out: list[Screenshot] = []
    async with factory() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            for vp_name, w, h in viewports:
                shot = await _capture_viewport(
                    browser=browser,
                    source=source,
                    kind=kind,
                    viewport=vp_name,
                    width=w,
                    height=h,
                    timeout_ms=timeout_ms,
                )
                if shot is not None:
                    out.append(shot)
        finally:
            await browser.close()

    return out


async def _capture_viewport(
    *,
    browser: Any,
    source: str,
    kind: str,
    viewport: str,
    width: int,
    height: int,
    timeout_ms: int,
) -> Optional[Screenshot]:
    """Сделать один screenshot. None при failure (best-effort)."""
    context = await browser.new_context(viewport={"width": width, "height": height})
    page = await context.new_page()

    try:
        if kind == "url":
            await page.goto(source, timeout=_MAX_PAGE_LOAD_TIMEOUT_MS, wait_until="networkidle")
        elif kind == "file":
            file_url = Path(source).resolve().as_uri()
            await page.goto(file_url, timeout=_MAX_PAGE_LOAD_TIMEOUT_MS, wait_until="networkidle")
        else:  # html
            # Записываем HTML во временный файл и грузим через file://
            # это надёжнее чем set_content для сложных страниц.
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".html", delete=False, encoding="utf-8",
            ) as f:
                f.write(source)
                tmp_path = f.name
            try:
                file_url = Path(tmp_path).as_uri()
                await page.goto(file_url, timeout=_MAX_PAGE_LOAD_TIMEOUT_MS, wait_until="networkidle")
            finally:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except Exception:
                    pass

        png_bytes = await asyncio.wait_for(
            page.screenshot(full_page=True, type="png"),
            timeout=timeout_ms / 1000,
        )
        return Screenshot(
            viewport=viewport,
            width=width,
            height=height,
            png_bytes=png_bytes,
            source=source[:200],
        )
    except Exception as exc:
        logger.warning(
            "screenshot.%s failed (%dx%d): %s", viewport, width, height, exc,
        )
        return None
    finally:
        try:
            await context.close()
        except Exception:
            pass


__all__ = [
    "Screenshot",
    "ScreenshotError",
    "screenshot",
]
