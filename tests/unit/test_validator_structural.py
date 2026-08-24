"""Unit-тесты для validator.structural (W21)."""
from __future__ import annotations

import asyncio

from backend.core.validator.structural import (
    StructuralValidator,
    _field_appears,
    _pick_markdown,
)


def _run(coro):
    return asyncio.run(coro)


# === _pick_markdown =====================================================

def test_pick_markdown_prefers_md() -> None:
    artifacts = [
        {"name": "config.json", "kind": "file", "content": "{}"},
        {"name": "README.md", "kind": "file", "content": "# Hello"},
    ]
    out = _pick_markdown(artifacts)
    assert out == "# Hello"


def test_pick_markdown_no_artifacts() -> None:
    assert _pick_markdown([]) is None


def test_pick_markdown_skips_binaries() -> None:
    artifacts = [
        {"name": "image.png", "kind": "binary", "size_bytes": 1000},
        {"name": "x.html", "kind": "file", "content": "<p>hi</p>"},
    ]
    assert _pick_markdown(artifacts) == "<p>hi</p>"


def test_pick_markdown_skips_empty_content() -> None:
    artifacts = [
        {"name": "empty.md", "kind": "file", "content": ""},
        {"name": "good.md", "kind": "file", "content": "actual content"},
    ]
    assert _pick_markdown(artifacts) == "actual content"


# === _field_appears ====================================================

def test_field_appears_substring() -> None:
    assert _field_appears("Hello headline foo", "headline") is True


def test_field_appears_snake_to_space() -> None:
    """primary_cta → "primary cta" должен match'ить."""
    assert _field_appears("our primary cta button", "primary_cta") is True


def test_field_appears_no_match() -> None:
    assert _field_appears("Hero block content", "subheadline") is False


def test_field_appears_empty() -> None:
    assert _field_appears("", "headline") is False
    assert _field_appears("text", "") is False


# === StructuralValidator ===============================================

def test_structural_no_artifacts_blocker() -> None:
    v = StructuralValidator()
    r = _run(v.validate(
        task_description="x", tz_markdown="...", artifacts=[],
    ))
    assert r.has_blocker is True
    assert r.score == 0.0


def test_structural_short_artifact_deducts() -> None:
    v = StructuralValidator()
    r = _run(v.validate(
        task_description="x", tz_markdown="...",
        artifacts=[{"name": "out.md", "kind": "file", "content": "tiny"}],
    ))
    # 4 chars → too_short → -2
    assert r.error_count >= 1
    assert r.score < 10


def test_structural_with_template_missing_block() -> None:
    template = {
        "task_type": "landing", "name": "t", "blocks": [
            {"name": "hero", "label": "Hero", "required": True,
             "fields": [{"name": "headline", "required": True}]},
            {"name": "pricing", "label": "Pricing", "required": True, "fields": []},
        ],
    }
    long_md = "## Hero\n\nHeadline: best\n\n" + "x" * 300  # без pricing
    v = StructuralValidator()
    r = _run(v.validate(
        task_description="make landing", tz_markdown="...",
        artifacts=[{"name": "out.md", "kind": "file", "content": long_md}],
        template=template,
    ))
    # pricing missing → ERROR + -2
    assert any(i.category == "missing_block" for i in r.issues)
    assert r.score < 10


def test_structural_with_template_all_present() -> None:
    template = {
        "task_type": "x", "name": "t", "blocks": [
            {"name": "hero", "label": "Hero", "required": True,
             "fields": [{"name": "headline", "required": True}]},
        ],
    }
    md = "## Hero\n\nHeadline: " + "x" * 250
    v = StructuralValidator()
    r = _run(v.validate(
        task_description="x", tz_markdown="...",
        artifacts=[{"name": "out.md", "kind": "file", "content": md}],
        template=template,
    ))
    assert r.score >= 9
    assert all(i.category != "missing_block" for i in r.issues)


def test_structural_detects_placeholders() -> None:
    md = "## Hero\n\n[TBD]\n\n" + "x" * 300
    v = StructuralValidator()
    r = _run(v.validate(
        task_description="x", tz_markdown="...",
        artifacts=[{"name": "out.md", "kind": "file", "content": md}],
    ))
    assert any(i.category == "placeholder" for i in r.issues)
    assert r.score < 10


def test_structural_too_many_missing_blocks_blocker() -> None:
    template = {
        "task_type": "x", "name": "t", "blocks": [
            {"name": f"block_{i}", "label": f"B{i}", "required": True, "fields": []}
            for i in range(5)
        ],
    }
    md = "tiny content but enough chars to pass length check " + "x" * 200
    v = StructuralValidator()
    r = _run(v.validate(
        task_description="x", tz_markdown="...",
        artifacts=[{"name": "out.md", "kind": "file", "content": md}],
        template=template,
    ))
    # 5 missing required → blocker
    assert r.has_blocker is True
