"""Unit-тесты для core.tz.validator_judge (W18)."""
from __future__ import annotations

import asyncio

from backend.core.tz.validator_judge import (
    JudgeResult,
    _coerce_score,
    _coerce_str_list,
    _coerce_verdict,
    _parse_response,
    judge_specification,
)


def _run(coro):
    return asyncio.run(coro)


# === _coerce_score =======================================================

def test_coerce_score_int() -> None:
    assert _coerce_score(7) == 7.0


def test_coerce_score_str() -> None:
    assert _coerce_score("8.5") == 8.5


def test_coerce_score_clamped_high() -> None:
    assert _coerce_score(99) == 10.0


def test_coerce_score_clamped_low() -> None:
    assert _coerce_score(-5) == 0.0


def test_coerce_score_invalid() -> None:
    assert _coerce_score("not a number") == 5.0
    assert _coerce_score(None) == 5.0


# === _coerce_verdict =====================================================

def test_coerce_verdict_known() -> None:
    assert _coerce_verdict("sufficient", 9) == "sufficient"
    assert _coerce_verdict("INSUFFICIENT", 2) == "insufficient"
    assert _coerce_verdict("needs-clarification", 5) == "needs_clarification"


def test_coerce_verdict_fallback_by_score() -> None:
    """Unknown verdict → определяется по score."""
    assert _coerce_verdict("garbage", 9) == "sufficient"
    assert _coerce_verdict("garbage", 5) == "needs_clarification"
    assert _coerce_verdict("garbage", 1) == "insufficient"


def test_coerce_verdict_none() -> None:
    assert _coerce_verdict(None, 8) == "sufficient"


# === _coerce_str_list ====================================================

def test_coerce_str_list_basic() -> None:
    assert _coerce_str_list(["a", "b"]) == ["a", "b"]


def test_coerce_str_list_filters_empty() -> None:
    assert _coerce_str_list(["a", "", None, "b"]) == ["a", "b"]


def test_coerce_str_list_truncates() -> None:
    long = "x" * 1000
    out = _coerce_str_list([long])
    assert len(out[0]) == 500


def test_coerce_str_list_caps() -> None:
    out = _coerce_str_list([f"item{i}" for i in range(50)], max_items=10)
    assert len(out) == 10


def test_coerce_str_list_invalid_input() -> None:
    assert _coerce_str_list("not a list") == []
    assert _coerce_str_list(None) == []


# === _parse_response =====================================================

def test_parse_response_full() -> None:
    raw = {
        "score": 8,
        "verdict": "sufficient",
        "gaps": ["check pricing currency"],
        "concrete_issues": [],
        "rationale": "Mostly complete",
    }
    r = _parse_response(raw)
    assert r.score == 8.0
    assert r.is_sufficient is True
    assert r.gaps == ["check pricing currency"]
    assert r.rationale == "Mostly complete"


def test_parse_response_from_string() -> None:
    raw = '{"score": 7, "verdict": "sufficient", "gaps": [], "concrete_issues": []}'
    r = _parse_response(raw)
    assert r.score == 7
    assert r.verdict == "sufficient"


def test_parse_response_invalid_json() -> None:
    r = _parse_response("not json")
    # fallback по score 5.0 → needs_clarification
    assert r.verdict == "needs_clarification"


def test_parse_response_empty_dict() -> None:
    r = _parse_response({})
    assert r.score == 5.0


def test_parse_response_not_a_dict() -> None:
    r = _parse_response(["unexpected"])
    assert r.score == 5.0


# === judge_specification ================================================

class _FakeRouter:
    def __init__(self, response, raise_error=False) -> None:
        self._response = response
        self.raise_error = raise_error
        self.last_kwargs = {}

    async def generate_json(self, **kwargs):
        self.last_kwargs = kwargs
        if self.raise_error:
            raise RuntimeError("LLM down")
        return self._response


def test_judge_no_router_returns_fallback() -> None:
    r = _run(judge_specification(
        task_description="x", tz_markdown="some tz",
    ))
    assert r.verdict == "needs_clarification"
    assert any("unavailable" in g.lower() for g in r.gaps)


def test_judge_empty_tz() -> None:
    router = _FakeRouter({"score": 9, "verdict": "sufficient"})
    r = _run(judge_specification(
        task_description="x", tz_markdown="", llm_router=router,
    ))
    assert r.verdict == "insufficient"
    assert r.score == 0.0


def test_judge_happy_path() -> None:
    router = _FakeRouter({
        "score": 8.5, "verdict": "sufficient",
        "gaps": [], "concrete_issues": [],
    })
    r = _run(judge_specification(
        task_description="Make a landing",
        tz_markdown="## Hero\nHeadline: Best widgets\n## Pricing\n...",
        llm_router=router,
    ))
    assert r.is_sufficient is True
    assert r.score == 8.5


def test_judge_llm_failure_returns_fallback() -> None:
    router = _FakeRouter(None, raise_error=True)
    r = _run(judge_specification(
        task_description="x", tz_markdown="some tz",
        llm_router=router,
    ))
    assert isinstance(r, JudgeResult)
    assert r.verdict == "needs_clarification"


def test_judge_temperature_zero() -> None:
    """Для prompt-cache hit нужно temperature=0."""
    router = _FakeRouter({"score": 7, "verdict": "sufficient"})
    _run(judge_specification(
        task_description="x", tz_markdown="some tz",
        llm_router=router,
    ))
    assert router.last_kwargs.get("temperature") == 0


def test_judge_required_blocks_passed_to_prompt() -> None:
    router = _FakeRouter({"score": 7, "verdict": "sufficient"})
    _run(judge_specification(
        task_description="x", tz_markdown="some tz",
        template_required_blocks=["hero", "pricing"],
        llm_router=router,
    ))
    prompt = router.last_kwargs["prompt"]
    assert "hero" in prompt
    assert "pricing" in prompt
