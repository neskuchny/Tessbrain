"""Unit-тесты для VisualDiffValidator full path (W22).

Мокаем Screenshot и MultimodalClient — без реального Playwright/LLM.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from backend.core.validator.base import IssueSeverity
from backend.core.validator.visual import (
    VisualDiffValidator,
    _coerce_severity,
    _parse_judge_response,
    _pick_renderable_source,
)


def _run(coro):
    return asyncio.run(coro)


# === _pick_renderable_source ==========================================

def test_pick_url_kind() -> None:
    out = _pick_renderable_source([
        {"name": "preview", "kind": "url", "url": "https://x.com"},
    ])
    assert out == "https://x.com"


def test_pick_url_from_path_field() -> None:
    """Некоторые artifacts кладут URL в `path`."""
    out = _pick_renderable_source([
        {"name": "x", "kind": "url", "path": "https://y.com"},
    ])
    assert out == "https://y.com"


def test_pick_html_content() -> None:
    out = _pick_renderable_source([
        {"name": "page.html", "kind": "file", "content": "<html>...</html>"},
    ])
    assert out == "<html>...</html>"


def test_pick_skips_irrelevant() -> None:
    out = _pick_renderable_source([
        {"name": "config.json", "kind": "file", "content": "{}"},
        {"name": "page.html", "kind": "file", "content": "<p>hi</p>"},
    ])
    assert out == "<p>hi</p>"


def test_pick_no_renderable() -> None:
    assert _pick_renderable_source([
        {"name": "doc.md", "kind": "file", "content": "# x"},
    ]) is None


def test_pick_empty() -> None:
    assert _pick_renderable_source([]) is None


# === _coerce_severity ===================================================

def test_coerce_severity_known() -> None:
    assert _coerce_severity("blocker") is IssueSeverity.BLOCKER
    assert _coerce_severity("WARNING") is IssueSeverity.WARNING


def test_coerce_severity_unknown() -> None:
    assert _coerce_severity("???") is IssueSeverity.WARNING
    assert _coerce_severity(None) is IssueSeverity.WARNING


# === _parse_judge_response ============================================

def test_parse_judge_full() -> None:
    score, issues, rationale = _parse_judge_response({
        "score": 8.5,
        "verdict": "minor",
        "issues": [
            {"severity": "warning", "category": "spacing",
             "message": "block hero — слишком много воздуха",
             "location": "hero"},
        ],
        "rationale": "ok overall",
    })
    assert score == 8.5
    assert len(issues) == 1
    assert issues[0].severity is IssueSeverity.WARNING
    assert rationale == "ok overall"


def test_parse_clamps_score() -> None:
    score, _, _ = _parse_judge_response({"score": 99})
    assert score == 10.0
    score, _, _ = _parse_judge_response({"score": -3})
    assert score == 0.0


def test_parse_invalid_input() -> None:
    score, issues, _ = _parse_judge_response("not a dict")
    assert score == 5.0
    assert issues == []


def test_parse_filters_empty_messages() -> None:
    _, issues, _ = _parse_judge_response({
        "issues": [
            {"message": ""},
            "string",
            {"message": "real one"},
        ],
    })
    assert len(issues) == 1
    assert issues[0].message == "real one"


# === Validator full path ===============================================

class _StubScreenshot:
    """Minimal stand-in для core.validator.screenshot.Screenshot."""
    def __init__(self, viewport: str, size: int = 1000):
        self.viewport = viewport
        self.width = 1440
        self.height = 900
        self.png_bytes = b"\x89PNG" + b"x" * size
        self.source = "test"

    def to_metadata_dict(self):
        return {"viewport": self.viewport, "size_bytes": len(self.png_bytes)}


class _StubMultimodalClient:
    def __init__(self, response, enabled: bool = True) -> None:
        self._response = response
        self._enabled = enabled
        self.last_kwargs = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def judge(self, **kwargs):
        self.last_kwargs = kwargs
        return self._response


from backend.core.llm.multimodal import MultimodalResponse


def test_screenshots_failed_returns_blocker() -> None:
    """Render всех viewports failed → BLOCKER."""
    async def _empty(**kwargs):
        return []

    v = VisualDiffValidator()
    with patch("backend.core.validator.screenshot.screenshot", new=_empty):
        r = _run(v.validate(
            task_description="x", tz_markdown="y",
            artifacts=[{"name": "page.html", "kind": "file", "content": "<p>x</p>"}],
        ))
    assert r.has_blocker is True
    assert r.score == 0.0


def test_no_multimodal_returns_neutral() -> None:
    async def _shots(**kwargs):
        return [_StubScreenshot("desktop"), _StubScreenshot("mobile")]

    client = _StubMultimodalClient(response=None, enabled=False)
    v = VisualDiffValidator(multimodal_client=client)
    with patch("backend.core.validator.screenshot.screenshot", new=_shots):
        r = _run(v.validate(
            task_description="x", tz_markdown="y",
            artifacts=[{"name": "page.html", "kind": "file", "content": "<p>x</p>"}],
        ))
    assert r.score == 5.0
    assert r.metadata.get("tool_missing") == "multimodal_llm"
    assert "screenshots" in r.metadata


def test_happy_path_with_multimodal() -> None:
    async def _shots(**kwargs):
        return [_StubScreenshot("desktop"), _StubScreenshot("mobile")]

    client = _StubMultimodalClient(
        response=MultimodalResponse(
            success=True,
            content={
                "score": 9.0,
                "issues": [{"severity": "info", "category": "ui",
                            "message": "looks good"}],
                "rationale": "approved",
            },
        ),
    )
    v = VisualDiffValidator(multimodal_client=client)
    with patch("backend.core.validator.screenshot.screenshot", new=_shots):
        r = _run(v.validate(
            task_description="x", tz_markdown="y",
            artifacts=[{"name": "page.html", "kind": "file", "content": "<p>x</p>"}],
        ))
    assert r.score == 9.0
    assert r.metadata.get("implemented") is True
    assert "rationale" not in r.summary  # rationale внутри summary напрямую
    assert r.summary == "approved"


def test_multimodal_failure_records_error() -> None:
    async def _shots(**kwargs):
        return [_StubScreenshot("desktop")]

    client = _StubMultimodalClient(
        response=MultimodalResponse(success=False, error="LLM down"),
    )
    v = VisualDiffValidator(multimodal_client=client)
    with patch("backend.core.validator.screenshot.screenshot", new=_shots):
        r = _run(v.validate(
            task_description="x", tz_markdown="y",
            artifacts=[{"name": "page.html", "kind": "file", "content": "<p>x</p>"}],
        ))
    assert r.error == "LLM down"


def test_multimodal_returns_non_json_content() -> None:
    async def _shots(**kwargs):
        return [_StubScreenshot("desktop")]

    client = _StubMultimodalClient(
        response=MultimodalResponse(
            success=True, content=None, raw_text="just plain text response",
            error="not JSON",
        ),
    )
    v = VisualDiffValidator(multimodal_client=client)
    with patch("backend.core.validator.screenshot.screenshot", new=_shots):
        r = _run(v.validate(
            task_description="x", tz_markdown="y",
            artifacts=[{"name": "page.html", "kind": "file", "content": "<p>x</p>"}],
        ))
    assert r.score == 5.0
    assert "non-JSON" in r.summary
    assert "raw_text_excerpt" in r.metadata


def test_screenshot_raises_screenshot_error() -> None:
    """ScreenshotError → tool_missing="playwright"."""
    from backend.core.validator.screenshot import ScreenshotError

    async def _raise(**kwargs):
        raise ScreenshotError("playwright not installed")

    v = VisualDiffValidator()
    with patch("backend.core.validator.screenshot.screenshot", new=_raise):
        r = _run(v.validate(
            task_description="x", tz_markdown="y",
            artifacts=[{"name": "page.html", "kind": "file", "content": "<p>x</p>"}],
        ))
    assert r.metadata.get("tool_missing") == "playwright"
    assert r.score == 5.0
