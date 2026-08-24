"""Unit-тесты для core.tz.iteration (W18)."""
from __future__ import annotations

import asyncio

from backend.core.tz.iteration import (
    DeltaResult,
    _parse_response,
    apply_delta,
    generate_delta,
    parse_blocks,
)


def _run(coro):
    return asyncio.run(coro)


# === parse_blocks =======================================================

def test_parse_blocks_basic() -> None:
    md = "## Hero\n\nsome text\n\n## Pricing\n\nmore text"
    blocks = parse_blocks(md)
    assert len(blocks) == 2
    assert blocks[0].name == "hero"
    assert blocks[1].name == "pricing"


def test_parse_blocks_empty() -> None:
    assert parse_blocks("") == []


def test_parse_blocks_no_headers() -> None:
    """Без заголовков — один блок name='body'."""
    blocks = parse_blocks("just plain text\nwith multiple lines")
    assert len(blocks) == 1
    assert blocks[0].name == "body"


def test_parse_blocks_various_levels() -> None:
    md = "# Top\nA\n## Sub\nB\n### Sub-sub\nC"
    blocks = parse_blocks(md)
    assert len(blocks) == 3


def test_parse_blocks_bold_headers() -> None:
    """Toleration: `**Hero**` тоже как заголовок."""
    md = "**Hero**\n\nA\n\n**Pricing**\n\nB"
    blocks = parse_blocks(md)
    assert len(blocks) == 2
    assert blocks[0].name == "hero"


def test_parse_blocks_slugifies_titles() -> None:
    md = "## Final CTA\n\ntext"
    blocks = parse_blocks(md)
    assert blocks[0].name == "final_cta"


def test_parse_blocks_preserves_content() -> None:
    md = "## Hero\n\nline 1\nline 2\n\n## Pricing\n\nP1"
    blocks = parse_blocks(md)
    assert "line 1" in blocks[0].content
    assert "line 2" in blocks[0].content
    assert "P1" in blocks[1].content


# === _parse_response ====================================================

def test_parse_response_full() -> None:
    raw = {
        "affected_blocks": ["Hero", "Pricing"],
        "revised_markdown": {
            "hero": "## Hero\nnew",
            "pricing": "## Pricing\n$99",
        },
        "summary_for_user": "Updated.",
    }
    r = _parse_response(raw)
    # Lowercase normalization
    assert r.affected_blocks == ["hero", "pricing"]
    assert "hero" in r.revised_markdown_per_block


def test_parse_response_string_json() -> None:
    raw = '{"affected_blocks": ["hero"], "revised_markdown": {"hero": "## Hero\\nnew"}}'
    r = _parse_response(raw)
    assert r.affected_blocks == ["hero"]


def test_parse_response_invalid_json() -> None:
    r = _parse_response("not json")
    assert r.error is not None
    assert r.affected_blocks == []


def test_parse_response_missing_fields() -> None:
    r = _parse_response({})
    assert r.affected_blocks == []
    assert r.revised_markdown_per_block == {}


def test_parse_response_filters_empty_revisions() -> None:
    raw = {
        "affected_blocks": ["hero"],
        "revised_markdown": {"hero": "", "valid": "some text"},
    }
    r = _parse_response(raw)
    assert "hero" not in r.revised_markdown_per_block  # empty filtered
    assert "valid" in r.revised_markdown_per_block


# === apply_delta ========================================================

def test_apply_delta_replaces_block() -> None:
    md = "## Hero\n\nold hero\n\n## Pricing\n\n$99"
    delta = DeltaResult(
        affected_blocks=["hero"],
        revised_markdown_per_block={"hero": "## Hero\n\nNEW HERO"},
    )
    out = apply_delta(md, delta)
    assert "NEW HERO" in out
    assert "old hero" not in out
    assert "$99" in out  # pricing intact


def test_apply_delta_no_changes_returns_original() -> None:
    md = "## Hero\n\nold"
    delta = DeltaResult(affected_blocks=[], revised_markdown_per_block={})
    assert apply_delta(md, delta) == md


def test_apply_delta_preserves_unmentioned_blocks() -> None:
    md = "## A\n\n1\n\n## B\n\n2\n\n## C\n\n3"
    delta = DeltaResult(
        affected_blocks=["b"],
        revised_markdown_per_block={"b": "## B\n\nNEW2"},
    )
    out = apply_delta(md, delta)
    assert "1" in out
    assert "NEW2" in out
    assert "3" in out
    assert out.index("1") < out.index("NEW2") < out.index("3")  # порядок сохранён


def test_apply_delta_appends_new_block() -> None:
    """Если delta упоминает блок которого нет в оригинале — append'ит."""
    md = "## Hero\n\nold"
    delta = DeltaResult(
        affected_blocks=["new_block"],
        revised_markdown_per_block={"new_block": "## NewBlock\n\ncontent"},
    )
    out = apply_delta(md, delta)
    assert "old" in out
    assert "NewBlock" in out


# === generate_delta ====================================================

class _FakeRouter:
    def __init__(self, response, raise_error=False) -> None:
        self.response = response
        self.raise_error = raise_error
        self.last_kwargs = {}

    async def generate_json(self, **kwargs):
        self.last_kwargs = kwargs
        if self.raise_error:
            raise RuntimeError("LLM down")
        return self.response


def test_generate_delta_no_router() -> None:
    r = _run(generate_delta(
        original_markdown="md", user_feedback="fix",
        llm_router=None,
    ))
    assert r.error is not None


def test_generate_delta_empty_inputs() -> None:
    r = _run(generate_delta(
        original_markdown="", user_feedback="fix",
        llm_router=_FakeRouter({}),
    ))
    assert r.error == "empty original markdown"

    r = _run(generate_delta(
        original_markdown="md", user_feedback="",
        llm_router=_FakeRouter({}),
    ))
    assert r.error == "empty user feedback"


def test_generate_delta_happy() -> None:
    router = _FakeRouter({
        "affected_blocks": ["hero"],
        "revised_markdown": {"hero": "## Hero\n\nFIXED"},
        "summary_for_user": "fixed hero",
    })
    r = _run(generate_delta(
        original_markdown="## Hero\n\nold",
        user_feedback="hero is wrong",
        llm_router=router,
    ))
    assert r.affected_blocks == ["hero"]
    assert r.summary_for_user == "fixed hero"


def test_generate_delta_llm_failure() -> None:
    router = _FakeRouter(None, raise_error=True)
    r = _run(generate_delta(
        original_markdown="md", user_feedback="fix",
        llm_router=router,
    ))
    assert r.error is not None
    assert "LLM call failed" in r.error


def test_generate_delta_temperature_zero() -> None:
    router = _FakeRouter({"affected_blocks": []})
    _run(generate_delta(
        original_markdown="md", user_feedback="fix",
        llm_router=router,
    ))
    assert router.last_kwargs.get("temperature") == 0


def test_generate_delta_includes_brand_block() -> None:
    router = _FakeRouter({"affected_blocks": []})
    _run(generate_delta(
        original_markdown="md", user_feedback="fix",
        brand_context_block="<brand>Acme</brand>",
        llm_router=router,
    ))
    assert "Acme" in router.last_kwargs["prompt"]
