"""Unit-тесты для validator.visual (W21)."""
from __future__ import annotations

import asyncio

from backend.core.validator.visual import VisualDiffValidator, _has_renderable_artifact


def _run(coro):
    return asyncio.run(coro)


def test_no_renderable_artifacts_returns_high_score() -> None:
    """Не UI-задача — validator не должен снижать score."""
    v = VisualDiffValidator()
    r = _run(v.validate(
        task_description="x", tz_markdown="y",
        artifacts=[{"name": "config.json", "content": "{}", "kind": "file"}],
    ))
    assert r.score == 10.0
    assert r.metadata.get("applicable") is False


def test_html_artifact_triggers_check_path() -> None:
    """С HTML — validator идёт по check-path, но без Playwright stops."""
    v = VisualDiffValidator()
    r = _run(v.validate(
        task_description="x", tz_markdown="y",
        artifacts=[{"name": "index.html", "content": "<p>x</p>", "kind": "file"}],
    ))
    # В test env Playwright не установлен → score=5.0 (neutral) + skip flag
    assert r.score == 5.0
    assert r.metadata.get("applicable") is True


def test_url_artifact_also_renderable() -> None:
    v = VisualDiffValidator()
    r = _run(v.validate(
        task_description="x", tz_markdown="y",
        artifacts=[{"name": "preview", "url": "https://x.com", "kind": "url"}],
    ))
    # Тоже идёт по check-path.
    assert r.metadata.get("applicable") is True


def test_has_renderable_html() -> None:
    assert _has_renderable_artifact([
        {"name": "page.html", "kind": "file"},
    ]) is True


def test_has_renderable_htm() -> None:
    assert _has_renderable_artifact([
        {"name": "old.HTM", "kind": "file"},
    ]) is True


def test_has_renderable_url_kind() -> None:
    assert _has_renderable_artifact([
        {"name": "x", "kind": "url"},
    ]) is True


def test_has_renderable_no_match() -> None:
    assert _has_renderable_artifact([
        {"name": "x.md", "kind": "file"},
        {"name": "y.json", "kind": "file"},
    ]) is False


def test_has_renderable_empty() -> None:
    assert _has_renderable_artifact([]) is False
