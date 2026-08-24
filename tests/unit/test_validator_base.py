"""Unit-тесты для core.validator.base (W21)."""
from __future__ import annotations

from backend.core.validator.base import Issue, IssueSeverity, ValidatorReport

# === Issue ==============================================================

def test_issue_to_dict() -> None:
    i = Issue(
        severity=IssueSeverity.ERROR,
        category="missing_block",
        message="hero is missing",
        location="hero",
        fix_hint="re-run executor",
    )
    d = i.to_dict()
    assert d["severity"] == "error"
    assert d["category"] == "missing_block"
    assert d["message"] == "hero is missing"


# === ValidatorReport ===================================================

def test_report_severity_counts() -> None:
    r = ValidatorReport(validator="x", score=7.0, issues=[
        Issue(IssueSeverity.WARNING, "a", "w1"),
        Issue(IssueSeverity.WARNING, "b", "w2"),
        Issue(IssueSeverity.ERROR, "c", "e1"),
        Issue(IssueSeverity.INFO, "d", "i1"),
    ])
    assert r.warning_count == 2
    assert r.error_count == 1
    assert r.has_blocker is False


def test_report_blocker_count_includes_blocker() -> None:
    r = ValidatorReport(validator="x", score=0.0, issues=[
        Issue(IssueSeverity.BLOCKER, "x", "fatal"),
    ])
    assert r.has_blocker is True
    # error_count ВКЛЮЧАЕТ blockers — это и errors, и blockers.
    assert r.error_count == 1


def test_report_to_dict_round_trip() -> None:
    r = ValidatorReport(
        validator="structural",
        score=8.5,
        issues=[Issue(IssueSeverity.WARNING, "fmt", "minor")],
        summary="ok",
        metadata={"checked": 5},
    )
    d = r.to_dict()
    assert d["validator"] == "structural"
    assert d["score"] == 8.5
    assert d["error_count"] == 0
    assert d["warning_count"] == 1
    assert d["has_blocker"] is False


def test_report_to_dict_with_error() -> None:
    r = ValidatorReport(validator="x", score=5.0, error="LLM down")
    assert r.to_dict()["error"] == "LLM down"


def test_severity_enum_string_values() -> None:
    """Public contract — не меняем без миграций."""
    assert IssueSeverity.INFO.value == "info"
    assert IssueSeverity.WARNING.value == "warning"
    assert IssueSeverity.ERROR.value == "error"
    assert IssueSeverity.BLOCKER.value == "blocker"
