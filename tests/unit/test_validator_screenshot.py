"""Unit-тесты для core.validator.screenshot (W22).

Без поднятого Playwright (heavy dep). Покрываем pure-logic helpers
+ кейсы где screenshot() raises ScreenshotError.
"""
from __future__ import annotations

import asyncio

import pytest
from backend.core.validator.screenshot import (
    Screenshot,
    ScreenshotError,
    _detect_source_kind,
    screenshot,
)


def _run(coro):
    return asyncio.run(coro)


# === _detect_source_kind ================================================

@pytest.mark.parametrize("source,expected", [
    ("https://example.com", "url"),
    ("http://localhost:3000", "url"),
    ("<html><body>x</body></html>", "html"),
    ("<!DOCTYPE html><html>...</html>", "html"),
    ("<body>fragment</body>", "html"),
])
def test_detect_kind_known(source: str, expected: str) -> None:
    assert _detect_source_kind(source) == expected


def test_detect_kind_file(tmp_path) -> None:
    """Существующий путь → file."""
    p = tmp_path / "page.html"
    p.write_text("<p>hi</p>")
    assert _detect_source_kind(str(p)) == "file"


def test_detect_kind_unknown_falls_back_to_html() -> None:
    """Не URL и не файл — считаем HTML-fragment."""
    assert _detect_source_kind("just plain text") == "html"


# === Screenshot dataclass ==============================================

def test_screenshot_to_base64() -> None:
    import base64

    s = Screenshot(
        viewport="desktop", width=1440, height=900,
        png_bytes=b"\x89PNGfake", source="https://x",
    )
    b64 = s.to_base64()
    # Round-trip: base64-decode возвращает оригинальные bytes.
    assert base64.b64decode(b64) == b"\x89PNGfake"
    assert isinstance(b64, str) and len(b64) > 0


def test_screenshot_metadata_does_not_leak_bytes() -> None:
    """to_metadata_dict не должен содержать bytes (только размер)."""
    s = Screenshot(
        viewport="desktop", width=1440, height=900,
        png_bytes=b"x" * 1000, source="https://x",
    )
    d = s.to_metadata_dict()
    assert "png_bytes" not in d
    assert d["size_bytes"] == 1000
    assert d["viewport"] == "desktop"


# === screenshot() — error paths без сети =============================

def test_screenshot_empty_source_raises() -> None:
    with pytest.raises(ScreenshotError) as exc:
        _run(screenshot(source=""))
    assert "empty source" in str(exc.value)


def test_screenshot_whitespace_source_raises() -> None:
    with pytest.raises(ScreenshotError):
        _run(screenshot(source="   \n\t  "))


def test_screenshot_url_in_enterprise_mode_external_rejected() -> None:
    """Air-gap check: external URL отклоняется ДО Playwright."""
    with pytest.raises(ScreenshotError) as exc:
        _run(screenshot(
            source="https://external.com/page",
            enterprise_mode=True,
        ))
    assert "not internal" in str(exc.value)


def test_screenshot_url_in_enterprise_mode_internal_passes_validation() -> None:
    """Internal URL не отклоняется на этапе validation
    (дальше может упасть на Playwright — это другой error)."""
    # localhost — internal, должен пройти URL-check, но упасть на playwright
    # ImportError если он не установлен в test env.
    try:
        _run(screenshot(
            source="http://localhost:3000",
            enterprise_mode=True,
        ))
    except ScreenshotError as exc:
        msg = str(exc)
        # Должна быть ошибка про playwright, а НЕ про is_url_safe
        assert "not internal" not in msg
