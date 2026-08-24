"""Unit-тесты для core.bookmarks.enricher (W17)."""
from __future__ import annotations

import asyncio

import pytest
from backend.core.bookmarks.enricher import EnrichResult, _coerce_tags, _fallback, enrich

# === _coerce_tags =======================================================

def test_coerce_tags_basic() -> None:
    assert _coerce_tags(["foo", "bar"]) == ["foo", "bar"]


def test_coerce_tags_strips_hash_and_lowercases() -> None:
    assert _coerce_tags(["#Marketing", "  Sales  "]) == ["marketing", "sales"]


def test_coerce_tags_dedupes() -> None:
    """Дубликаты убираем (после lowercase)."""
    assert _coerce_tags(["foo", "FOO", "Foo"]) == ["foo"]


def test_coerce_tags_skips_non_strings() -> None:
    assert _coerce_tags(["foo", 123, None, ""]) == ["foo"]


def test_coerce_tags_truncates_at_50_chars() -> None:
    long_tag = "x" * 100
    out = _coerce_tags([long_tag])
    assert len(out) == 1
    assert len(out[0]) == 50


def test_coerce_tags_caps_at_10() -> None:
    many = [f"tag{i}" for i in range(20)]
    out = _coerce_tags(many)
    assert len(out) == 10


def test_coerce_tags_invalid_input() -> None:
    assert _coerce_tags("not a list") == []
    assert _coerce_tags(None) == []


# === _fallback ==========================================================

def test_fallback_uses_page_title() -> None:
    r = _fallback(page_title="Page", user_hint=None, url="https://x.com")
    assert r.title == "Page"
    assert r.tags == []


def test_fallback_uses_user_hint_when_no_title() -> None:
    r = _fallback(page_title="", user_hint="my doc", url="https://x.com")
    assert r.title == "my doc"


def test_fallback_uses_url_as_last_resort() -> None:
    r = _fallback(page_title="", user_hint=None, url="https://x.com")
    assert r.title == "https://x.com"


def test_fallback_truncates_long_title() -> None:
    long = "x" * 500
    r = _fallback(page_title=long, user_hint=None, url="u")
    assert len(r.title) == 200


# === enrich (mocked LLM) =================================================

class _FakeRouter:
    def __init__(self, response, raise_error: bool = False) -> None:
        self._response = response
        self.raise_error = raise_error
        self.last_kwargs = {}

    async def generate_json(self, **kwargs):
        self.last_kwargs = kwargs
        if self.raise_error:
            raise RuntimeError("LLM down")
        return self._response


def _run(coro):
    return asyncio.run(coro)


def test_enrich_happy_path() -> None:
    router = _FakeRouter({
        "title": "Sales Methodology",
        "description": "Internal guide for sales team",
        "tags": ["sales", "methodology", "internal"],
    })
    out = _run(enrich(
        url="https://wiki.local/sales",
        page_title="raw title",
        page_text="lots of sales process text...",
        llm_router=router,
    ))
    assert out.title == "Sales Methodology"
    assert "sales" in out.tags


def test_enrich_no_router_returns_fallback() -> None:
    out = _run(enrich(
        url="https://x.com", page_title="P", page_text="t",
        llm_router=None,
    ))
    assert isinstance(out, EnrichResult)
    assert out.title == "P"


def test_enrich_llm_failure_returns_fallback() -> None:
    router = _FakeRouter(None, raise_error=True)
    out = _run(enrich(
        url="https://x.com", page_title="page", page_text="text",
        llm_router=router,
    ))
    assert out.title == "page"
    assert out.tags == []


def test_enrich_llm_returns_string_json() -> None:
    """Иногда модель возвращает stringified JSON — должны раcпарсить."""
    router = _FakeRouter('{"title": "T", "description": "D", "tags": ["a"]}')
    out = _run(enrich(
        url="https://x.com", page_title="", page_text="",
        llm_router=router,
    ))
    assert out.title == "T"
    assert out.tags == ["a"]


def test_enrich_llm_returns_invalid_json_string() -> None:
    router = _FakeRouter("{not valid json")
    out = _run(enrich(
        url="https://x.com", page_title="P", page_text="",
        llm_router=router,
    ))
    assert out.title == "P"  # fallback


def test_enrich_temperature_zero() -> None:
    """Для prompt-cache hit нужно temperature=0."""
    router = _FakeRouter({"title": "T", "description": "D", "tags": []})
    _run(enrich(url="https://x.com", page_title="", page_text="", llm_router=router))
    assert router.last_kwargs.get("temperature") == 0


def test_enrich_user_hint_passed_to_prompt() -> None:
    router = _FakeRouter({"title": "T", "description": "D", "tags": []})
    _run(enrich(
        url="https://x.com", page_title="", page_text="",
        user_hint="главная презентация для клиентов",
        llm_router=router,
    ))
    assert "главная презентация для клиентов" in router.last_kwargs["prompt"]


def test_enrich_includes_url_in_prompt() -> None:
    router = _FakeRouter({"title": "T", "description": "D", "tags": []})
    _run(enrich(
        url="https://wiki.example.com/foo",
        page_title="", page_text="",
        llm_router=router,
    ))
    assert "wiki.example.com/foo" in router.last_kwargs["prompt"]
