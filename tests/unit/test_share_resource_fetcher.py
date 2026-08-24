"""Unit-тесты для core.sharing.resource_fetcher (W32)."""
from __future__ import annotations

import asyncio

import pytest
from backend.core.sharing.resource_fetcher import (
    DEFAULT_MAX_CHARS,
    FetchedResource,
    ResourceFetcher,
    get_resource_fetcher,
    reset_resource_fetcher,
)


def _run(coro):
    return asyncio.run(coro)


# === FetchedResource.to_dict =============================================

def test_to_dict_includes_all_fields() -> None:
    r = FetchedResource(
        resource_type="document",
        resource_id="doc_1",
        title="Title",
        content_format="markdown",
        content="hello",
        metadata={"x": 1},
        truncated=False,
        available=True,
    )
    d = r.to_dict()
    assert d["resource_type"] == "document"
    assert d["title"] == "Title"
    assert d["content_format"] == "markdown"
    assert d["content"] == "hello"
    assert d["metadata"] == {"x": 1}
    assert d["truncated"] is False
    assert d["available"] is True


# === ResourceFetcher.register / fetch ====================================

def test_unknown_type_returns_metadata_only() -> None:
    f = ResourceFetcher()
    out = _run(f.fetch(resource_type="unknown", resource_id="x"))
    assert out.available is False
    assert out.content == ""
    assert out.content_format == "metadata-only"


def test_handler_returning_none_metadata_only() -> None:
    f = ResourceFetcher()

    async def _none(rid):
        return None

    f.register("document", _none)
    out = _run(f.fetch(resource_type="document", resource_id="x"))
    assert out.available is False


def test_handler_success_returns_content() -> None:
    f = ResourceFetcher()

    async def _ok(rid):
        return FetchedResource(
            resource_type="document",
            resource_id=rid,
            title="My Doc",
            content_format="markdown",
            content="# Hello",
        )

    f.register("document", _ok)
    out = _run(f.fetch(resource_type="document", resource_id="d_1"))
    assert out.available is True
    assert out.title == "My Doc"
    assert out.content == "# Hello"


def test_handler_exception_falls_back() -> None:
    f = ResourceFetcher()

    async def _broken(rid):
        raise RuntimeError("supabase down")

    f.register("document", _broken)
    out = _run(f.fetch(resource_type="document", resource_id="x"))
    assert out.available is False
    assert out.content == ""


def test_truncate_long_content() -> None:
    f = ResourceFetcher()
    long = "x" * (DEFAULT_MAX_CHARS + 1000)

    async def _h(rid):
        return FetchedResource(
            resource_type="document", resource_id=rid,
            content=long, content_format="text",
        )

    f.register("document", _h)
    out = _run(f.fetch(
        resource_type="document", resource_id="x", max_chars=1000,
    ))
    assert out.truncated is True
    assert len(out.content) <= 1000
    assert out.content.endswith("…")


def test_short_content_not_truncated() -> None:
    f = ResourceFetcher()

    async def _h(rid):
        return FetchedResource(
            resource_type="document", resource_id=rid, content="short",
        )

    f.register("document", _h)
    out = _run(f.fetch(resource_type="document", resource_id="x"))
    assert out.truncated is False
    assert out.content == "short"


def test_min_max_chars_clamped() -> None:
    """max_chars=0 не должен дать 0-длинный output."""
    f = ResourceFetcher()
    body = "x" * 600

    async def _h(rid):
        return FetchedResource(resource_type="document", resource_id=rid, content=body)

    f.register("document", _h)
    out = _run(f.fetch(
        resource_type="document", resource_id="x", max_chars=0,
    ))
    # _truncate floor=500 → если контент 600, обрежется до 500.
    assert len(out.content) <= 500
    assert out.truncated is True


def test_register_validates_args() -> None:
    f = ResourceFetcher()
    with pytest.raises(ValueError):
        f.register("", lambda x: x)
    with pytest.raises(ValueError):
        f.register("document", None)


def test_supported_types() -> None:
    f = ResourceFetcher()

    async def _h(rid):
        return None

    f.register("document", _h)
    f.register("meeting", _h)
    assert f.supported_types() == ["document", "meeting"]


# === DLP application ====================================================

def test_dlp_applied_to_content(monkeypatch) -> None:
    f = ResourceFetcher()

    class _FakeResult:
        def __init__(self, text):
            self.text = text

    def _fake_filter(text, config=None):
        return _FakeResult(text.replace("secret", "***"))

    monkeypatch.setattr(
        "backend.core.llm.output_filter.filter_output",
        _fake_filter,
        raising=False,
    )

    async def _h(rid):
        return FetchedResource(
            resource_type="document", resource_id=rid,
            content="secret stuff",
        )

    f.register("document", _h)
    out = _run(f.fetch(resource_type="document", resource_id="x"))
    assert "secret" not in out.content or "***" in out.content


def test_dlp_can_be_disabled() -> None:
    f = ResourceFetcher()

    async def _h(rid):
        return FetchedResource(
            resource_type="document", resource_id=rid, content="secret stuff",
        )

    f.register("document", _h)
    out = _run(f.fetch(
        resource_type="document", resource_id="x", apply_dlp=False,
    ))
    # apply_dlp=False → контент не модифицируется (если real DLP'а нет).
    assert "secret stuff" == out.content


# === get_resource_fetcher / reset =======================================

def test_singleton_returns_same_instance() -> None:
    reset_resource_fetcher()
    f1 = get_resource_fetcher()
    f2 = get_resource_fetcher()
    assert f1 is f2


def test_reset_creates_new_instance() -> None:
    a = get_resource_fetcher()
    reset_resource_fetcher()
    b = get_resource_fetcher()
    assert a is not b


def test_default_singleton_has_document_and_meeting_handlers() -> None:
    reset_resource_fetcher()
    f = get_resource_fetcher()
    types = f.supported_types()
    assert "document" in types
    assert "meeting" in types
