"""Unit-тесты для validator.ai_judge (W21)."""
from __future__ import annotations

import asyncio

from backend.core.validator.ai_judge import (
    AIJudgeValidator,
    _coerce_severity,
    _parse_response,
    _summarize_artifacts,
)
from backend.core.validator.base import IssueSeverity


def _run(coro):
    return asyncio.run(coro)


# === _coerce_severity ==================================================

def test_coerce_severity_known() -> None:
    assert _coerce_severity("blocker") == IssueSeverity.BLOCKER
    assert _coerce_severity("ERROR") == IssueSeverity.ERROR
    assert _coerce_severity("warning") == IssueSeverity.WARNING
    assert _coerce_severity("info") == IssueSeverity.INFO


def test_coerce_severity_unknown_defaults_to_warning() -> None:
    assert _coerce_severity("garbage") == IssueSeverity.WARNING
    assert _coerce_severity(None) == IssueSeverity.WARNING


# === _summarize_artifacts ==============================================

def test_summarize_basic() -> None:
    out = _summarize_artifacts([
        {"name": "a.md", "kind": "file", "content": "Hello"},
        {"name": "b.md", "kind": "file", "content": "World"},
    ])
    assert "a.md" in out and "Hello" in out
    assert "b.md" in out and "World" in out


def test_summarize_skips_binary() -> None:
    out = _summarize_artifacts([
        {"name": "img.png", "kind": "binary", "size_bytes": 5000},
    ])
    assert "img.png" in out
    assert "no content" in out


def test_summarize_truncates_at_budget() -> None:
    huge = "x" * 50_000
    out = _summarize_artifacts(
        [{"name": "huge.md", "kind": "file", "content": huge}],
        max_chars=2000,
    )
    # Должно быть обрезано до budget'а
    assert len(out) < 5000


def test_summarize_empty() -> None:
    assert _summarize_artifacts([]) == "[no artifacts]"


# === _parse_response ===================================================

def test_parse_response_full() -> None:
    raw = {
        "score": 8.5,
        "verdict": "minor_revisions",
        "issues": [
            {"severity": "warning", "category": "tone", "message": "слишком формально"},
        ],
        "rationale": "ok overall",
    }
    score, issues, rationale = _parse_response(raw)
    assert score == 8.5
    assert len(issues) == 1
    assert rationale == "ok overall"


def test_parse_response_string_json() -> None:
    raw = '{"score": 7, "issues": [], "rationale": "ok"}'
    score, _issues, rationale = _parse_response(raw)
    assert score == 7
    assert rationale == "ok"


def test_parse_response_invalid_json() -> None:
    score, issues, _rationale = _parse_response("not json")
    assert score == 5.0
    assert issues == []


def test_parse_response_clamps_score() -> None:
    score, _, _ = _parse_response({"score": 99})
    assert score == 10.0
    score, _, _ = _parse_response({"score": -5})
    assert score == 0.0


def test_parse_response_filters_empty_messages() -> None:
    raw = {"issues": [
        {"message": ""},
        {"message": "valid issue"},
        "not a dict",
        {},
    ]}
    _, issues, _ = _parse_response(raw)
    assert len(issues) == 1
    assert issues[0].message == "valid issue"


# === Validator end-to-end ==============================================

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


def test_validate_no_router_returns_neutral() -> None:
    v = AIJudgeValidator(llm_router=None)
    r = _run(v.validate(
        task_description="x", tz_markdown="y",
        artifacts=[{"name": "a.md", "content": "z", "kind": "file"}],
    ))
    assert r.score == 5.0
    assert r.error == "no llm_router"


def test_validate_empty_artifacts_blocker() -> None:
    v = AIJudgeValidator(llm_router=_FakeRouter({"score": 9}))
    r = _run(v.validate(
        task_description="x", tz_markdown="y", artifacts=[],
    ))
    assert r.has_blocker is True
    assert r.score == 0.0


def test_validate_happy_path() -> None:
    router = _FakeRouter({
        "score": 9.0,
        "verdict": "approve",
        "issues": [],
        "rationale": "looks good",
    })
    v = AIJudgeValidator(llm_router=router)
    r = _run(v.validate(
        task_description="x", tz_markdown="y",
        artifacts=[{"name": "a.md", "content": "result", "kind": "file"}],
    ))
    assert r.score == 9.0
    assert r.error is None


def test_validate_uses_temperature_zero() -> None:
    router = _FakeRouter({"score": 8})
    v = AIJudgeValidator(llm_router=router)
    _run(v.validate(
        task_description="x", tz_markdown="y",
        artifacts=[{"name": "a.md", "content": "z", "kind": "file"}],
    ))
    assert router.last_kwargs.get("temperature") == 0


def test_validate_llm_failure_returns_neutral_with_error() -> None:
    router = _FakeRouter(None, raise_error=True)
    v = AIJudgeValidator(llm_router=router)
    r = _run(v.validate(
        task_description="x", tz_markdown="y",
        artifacts=[{"name": "a.md", "content": "z", "kind": "file"}],
    ))
    assert r.score == 5.0
    assert "LLM call failed" in (r.error or "")


def test_validate_brand_block_in_prompt() -> None:
    router = _FakeRouter({"score": 8})
    v = AIJudgeValidator(llm_router=router)
    _run(v.validate(
        task_description="x", tz_markdown="y",
        artifacts=[{"name": "a.md", "content": "z", "kind": "file"}],
        brand_context_block="<brand>Acme</brand>",
    ))
    assert "Acme" in router.last_kwargs["prompt"]
