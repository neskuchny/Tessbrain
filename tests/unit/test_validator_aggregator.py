"""Unit-тесты для validator.aggregator (W21)."""
from __future__ import annotations

from backend.core.validator.aggregator import ValidationDecision, aggregate
from backend.core.validator.base import Issue, IssueSeverity, ValidatorReport


def _r(name: str, score: float, *, blocker: bool = False, error: str | None = None) -> ValidatorReport:
    issues = [Issue(IssueSeverity.BLOCKER, "x", "fatal")] if blocker else []
    return ValidatorReport(validator=name, score=score, issues=issues, error=error)


# === Empty input ========================================================

def test_aggregate_empty_reports_returns_reject() -> None:
    out = aggregate([])
    assert out.decision == ValidationDecision.REJECT
    assert out.aggregate_score == 0.0


# === Blocker handling ===================================================

def test_blocker_in_any_report_forces_reject() -> None:
    out = aggregate([
        _r("structural", 9.0),
        _r("ai_judge", 9.5, blocker=True),
    ])
    assert out.decision == ValidationDecision.REJECT
    assert out.blocker_seen is True
    assert out.aggregate_score == 0.0


# === All-failed validators →  human review ===========================

def test_all_validators_failed_returns_human_review() -> None:
    out = aggregate([
        _r("structural", 5.0, error="boom"),
        _r("ai_judge", 5.0, error="LLM down"),
    ])
    assert out.decision == ValidationDecision.HUMAN_REVIEW
    assert "all validators failed" in out.rationale.lower()


# === Score thresholds ===================================================

def test_high_score_auto_approves() -> None:
    out = aggregate([
        _r("structural", 9.0),
        _r("ai_judge", 9.0),
    ])
    assert out.decision == ValidationDecision.AUTO_APPROVE
    assert out.aggregate_score >= 8.5


def test_mid_score_human_review() -> None:
    out = aggregate([
        _r("structural", 6.0),
        _r("ai_judge", 6.0),
    ])
    assert out.decision == ValidationDecision.HUMAN_REVIEW


def test_low_score_rejects() -> None:
    out = aggregate([
        _r("structural", 3.0),
        _r("ai_judge", 3.0),
    ])
    assert out.decision == ValidationDecision.REJECT


def test_custom_thresholds() -> None:
    """Вынесли auto-approve до 7 → 8.0 проходит."""
    out = aggregate(
        [_r("structural", 8.0)],
        auto_approve_threshold=7.0,
        human_review_threshold=4.0,
    )
    assert out.decision == ValidationDecision.AUTO_APPROVE


# === Weights ============================================================

def test_default_weights_emphasize_ai_judge() -> None:
    """ai_judge weight=2.0 vs structural=1.0; low ai_judge тянет вниз."""
    # structural:9 (w=1.0), ai_judge:5 (w=2.0)
    # weighted = (9*1 + 5*2)/(1+2) = 19/3 ≈ 6.33 → human_review
    out = aggregate([
        _r("structural", 9.0),
        _r("ai_judge", 5.0),
    ])
    assert 6.0 <= out.aggregate_score <= 7.0
    assert out.decision == ValidationDecision.HUMAN_REVIEW


def test_custom_weights_override() -> None:
    """Custom weights меняют картину."""
    out = aggregate(
        [_r("structural", 10.0), _r("ai_judge", 5.0)],
        weights={"structural": 5.0, "ai_judge": 1.0},   # перевес structural
    )
    # weighted = (10*5 + 5*1)/6 ≈ 9.17 → auto_approve
    assert out.decision == ValidationDecision.AUTO_APPROVE


def test_unknown_validator_uses_default_weight() -> None:
    """Validator которого нет в _DEFAULT_WEIGHTS получает вес 1.0."""
    out = aggregate([
        _r("custom_validator_xyz", 9.0),
    ])
    assert out.decision == ValidationDecision.AUTO_APPROVE


def test_to_dict_complete() -> None:
    out = aggregate([_r("structural", 9.0)])
    d = out.to_dict()
    assert d["decision"] == "auto_approve"
    assert d["aggregate_score"] >= 8.5
    assert "issue_count" in d
    assert d["issue_count"] == 0


def test_failed_report_excluded_from_average() -> None:
    """Validator с error не должен искажать average (его score игнорируется)."""
    out = aggregate([
        _r("structural", 9.0),
        _r("ai_judge", 0.0, error="failed"),   # должен быть skipped
    ])
    # Average = 9.0 (только structural)
    assert out.aggregate_score == 9.0
    assert out.decision == ValidationDecision.AUTO_APPROVE
