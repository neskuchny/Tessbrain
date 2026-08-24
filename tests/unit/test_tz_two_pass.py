"""Unit-тесты для core.tz.two_pass (W18)."""
from __future__ import annotations

import asyncio

from backend.core.tz.two_pass import (
    CritiqueResult,
    _parse_critique,
    critique_draft,
    revise_draft,
    two_pass_generate,
)


def _run(coro):
    return asyncio.run(coro)


# === _parse_critique ====================================================

def test_parse_critique_minimal() -> None:
    r = _parse_critique({"issues": [], "needs_revision": False})
    assert r.needs_revision is False
    assert r.issues == []


def test_parse_critique_full() -> None:
    r = _parse_critique({
        "issues": [
            {"block": "hero", "issue": "subheadline weak"},
            {"block": "pricing", "issue": "no currency"},
        ],
        "overall_quality": 6,
        "needs_revision": True,
    })
    assert len(r.issues) == 2
    assert r.issues[0]["block"] == "hero"
    assert r.overall_quality == 6


def test_parse_critique_invalid_input() -> None:
    r = _parse_critique("not json")
    assert r.needs_revision is False


def test_parse_critique_filters_bad_issues() -> None:
    r = _parse_critique({
        "issues": [
            {"block": "x", "issue": "valid"},
            "not a dict",
            {"block": "y"},  # no issue text → skip
            {"issue": ""},   # empty
        ],
    })
    assert len(r.issues) == 1


def test_parse_critique_clamps_quality() -> None:
    r = _parse_critique({"overall_quality": 99})
    assert r.overall_quality == 10.0
    r = _parse_critique({"overall_quality": -5})
    assert r.overall_quality == 0.0


def test_parse_critique_infers_needs_revision_from_issues() -> None:
    """Если needs_revision не указан, но issues есть — True."""
    r = _parse_critique({"issues": [{"block": "x", "issue": "y"}]})
    assert r.needs_revision is True


# === critique_draft (mocked) ===========================================

class _FakeRouter:
    def __init__(self, json_resp=None, gen_resp="", raise_json=False, raise_gen=False) -> None:
        self.json_resp = json_resp
        self.gen_resp = gen_resp
        self.raise_json = raise_json
        self.raise_gen = raise_gen
        self.json_kwargs = {}
        self.gen_kwargs = {}

    async def generate_json(self, **kwargs):
        self.json_kwargs = kwargs
        if self.raise_json:
            raise RuntimeError("json down")
        return self.json_resp

    async def generate(self, **kwargs):
        self.gen_kwargs = kwargs
        if self.raise_gen:
            raise RuntimeError("gen down")
        return self.gen_resp


def test_critique_draft_no_router() -> None:
    r = _run(critique_draft(task_description="x", draft_markdown="d", llm_router=None))
    assert r.needs_revision is False


def test_critique_draft_empty_draft() -> None:
    router = _FakeRouter(json_resp={"issues": [{"block": "x", "issue": "y"}]})
    r = _run(critique_draft(task_description="x", draft_markdown="", llm_router=router))
    assert r.needs_revision is False


def test_critique_draft_happy() -> None:
    router = _FakeRouter(json_resp={
        "issues": [{"block": "hero", "issue": "weak"}],
        "needs_revision": True,
        "overall_quality": 5,
    })
    r = _run(critique_draft(
        task_description="task", draft_markdown="some markdown",
        llm_router=router,
    ))
    assert r.needs_revision is True
    assert len(r.issues) == 1


def test_critique_draft_llm_failure() -> None:
    router = _FakeRouter(raise_json=True)
    r = _run(critique_draft(
        task_description="x", draft_markdown="d", llm_router=router,
    ))
    assert isinstance(r, CritiqueResult)
    assert r.needs_revision is False


def test_critique_uses_temperature_zero() -> None:
    router = _FakeRouter(json_resp={"issues": []})
    _run(critique_draft(task_description="x", draft_markdown="d", llm_router=router))
    assert router.json_kwargs.get("temperature") == 0


def test_critique_includes_brand_block() -> None:
    router = _FakeRouter(json_resp={"issues": []})
    _run(critique_draft(
        task_description="x", draft_markdown="d",
        brand_context_block="<brand>Acme</brand>",
        llm_router=router,
    ))
    assert "Acme" in router.json_kwargs["prompt"]


# === revise_draft =======================================================

def test_revise_draft_no_router_returns_original() -> None:
    out = _run(revise_draft(
        task_description="x", draft_markdown="original",
        issues=[{"block": "h", "issue": "fix"}],
        llm_router=None,
    ))
    assert out == "original"


def test_revise_draft_no_issues_returns_original() -> None:
    router = _FakeRouter(gen_resp="should not be used")
    out = _run(revise_draft(
        task_description="x", draft_markdown="original",
        issues=[], llm_router=router,
    ))
    assert out == "original"


def test_revise_draft_happy() -> None:
    router = _FakeRouter(gen_resp="## Hero\n\nrevised content with more detail and concrete numbers like 50K customers")
    out = _run(revise_draft(
        task_description="x",
        draft_markdown="## Hero\n\nold content with lots of words to ensure draft is long enough",
        issues=[{"block": "hero", "issue": "weak"}],
        llm_router=router,
    ))
    assert "revised" in out


def test_revise_draft_llm_failure_keeps_original() -> None:
    router = _FakeRouter(raise_gen=True)
    out = _run(revise_draft(
        task_description="x",
        draft_markdown="original draft",
        issues=[{"block": "h", "issue": "fix"}],
        llm_router=router,
    ))
    assert out == "original draft"


def test_revise_draft_too_short_output_keeps_original() -> None:
    """Подозрительно короткий revise → возвращаем draft."""
    router = _FakeRouter(gen_resp="hi")
    long_draft = "x" * 2000
    out = _run(revise_draft(
        task_description="x", draft_markdown=long_draft,
        issues=[{"block": "h", "issue": "fix"}],
        llm_router=router,
    ))
    assert out == long_draft


# === two_pass_generate ==================================================

def test_two_pass_no_revision_when_quality_high() -> None:
    """quality >= min → revise skipped."""
    router = _FakeRouter(json_resp={
        "issues": [{"block": "h", "issue": "minor"}],
        "needs_revision": True,
        "overall_quality": 9,
    })
    r = _run(two_pass_generate(
        task_description="x", draft_markdown="some draft",
        llm_router=router, min_quality_for_revision=8,
    ))
    assert r.revised is False
    assert r.final_markdown == "some draft"


def test_two_pass_revises_when_quality_low() -> None:
    long_draft = "## Hero\n\n" + ("words " * 100)
    revised = "## Hero\n\nrevised content " + ("more words " * 50)

    class _Mixed:
        async def generate_json(self, **kwargs):
            return {
                "issues": [{"block": "hero", "issue": "weak"}],
                "needs_revision": True,
                "overall_quality": 5,
            }

        async def generate(self, **kwargs):
            return revised

    r = _run(two_pass_generate(
        task_description="x", draft_markdown=long_draft,
        llm_router=_Mixed(), min_quality_for_revision=8,
    ))
    assert r.revised is True
    assert "revised content" in r.final_markdown


def test_two_pass_no_issues_skips_revise() -> None:
    router = _FakeRouter(json_resp={
        "issues": [],
        "needs_revision": False,
        "overall_quality": 9,
    })
    r = _run(two_pass_generate(
        task_description="x", draft_markdown="ok draft",
        llm_router=router,
    ))
    assert r.revised is False
