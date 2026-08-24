"""Aggregator — комбинирует ValidatorReport'ы в финальный verdict (W21).

Логика:
- Если хоть один report имеет BLOCKER → verdict=REJECT, decision=human_review
- Иначе weighted average по scores
- decision:
    aggregate_score >= auto_approve_threshold (default 8.5) → AUTO_APPROVE
    aggregate_score >= human_review_threshold (default 5.0) → HUMAN_REVIEW
    иначе → REJECT

Веса по умолчанию (validator → weight):
- structural: 1.0
- code: 1.5
- ai_judge: 2.0
- visual_diff: 1.5

Custom веса можно передать в `aggregate(weights={...})`.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from backend.core.validator.base import Issue, ValidatorReport


class ValidationDecision(str, enum.Enum):
    AUTO_APPROVE = "auto_approve"
    HUMAN_REVIEW = "human_review"
    REJECT = "reject"


_DEFAULT_WEIGHTS: dict[str, float] = {
    "structural": 1.0,
    "code": 1.5,
    "ai_judge": 2.0,
    "visual_diff": 1.5,
}

_DEFAULT_AUTO_APPROVE = 8.5
_DEFAULT_HUMAN_REVIEW = 5.0


@dataclass
class AggregatedReport:
    """Финальный итог по всем validators."""
    decision: ValidationDecision
    aggregate_score: float          # 0..10
    reports: list[ValidatorReport] = field(default_factory=list)
    blocker_seen: bool = False
    rationale: str = ""

    @property
    def all_issues(self) -> list[Issue]:
        out: list[Issue] = []
        for r in self.reports:
            out.extend(r.issues)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "aggregate_score": self.aggregate_score,
            "blocker_seen": self.blocker_seen,
            "rationale": self.rationale,
            "reports": [r.to_dict() for r in self.reports],
            "issue_count": len(self.all_issues),
        }


def aggregate(
    reports: list[ValidatorReport],
    *,
    weights: Optional[Mapping[str, float]] = None,
    auto_approve_threshold: float = _DEFAULT_AUTO_APPROVE,
    human_review_threshold: float = _DEFAULT_HUMAN_REVIEW,
) -> AggregatedReport:
    """Комбинировать reports в финальный verdict.

    Если reports пустой — REJECT (нечего апрувить).
    """
    if not reports:
        return AggregatedReport(
            decision=ValidationDecision.REJECT,
            aggregate_score=0.0,
            reports=[],
            rationale="no validator reports — cannot approve",
        )

    weight_map = dict(_DEFAULT_WEIGHTS)
    if weights:
        weight_map.update({k: float(v) for k, v in weights.items()})

    # Если хотя бы один blocker — REJECT.
    blocker = any(r.has_blocker for r in reports)
    if blocker:
        return AggregatedReport(
            decision=ValidationDecision.REJECT,
            aggregate_score=0.0,
            reports=list(reports),
            blocker_seen=True,
            rationale="at least one validator reported a blocker issue",
        )

    # Weighted average. Default weight 1.0 для validators которые не в map.
    total_score = 0.0
    total_weight = 0.0
    for r in reports:
        if r.error is not None:
            # Validator сам упал — пропускаем в weighted average,
            # но не блокируем (у него нет issues).
            continue
        w = weight_map.get(r.validator, 1.0)
        total_score += r.score * w
        total_weight += w

    if total_weight == 0:
        # Все validators упали → не можем оценить, сразу human review.
        return AggregatedReport(
            decision=ValidationDecision.HUMAN_REVIEW,
            aggregate_score=0.0,
            reports=list(reports),
            rationale="all validators failed — needs manual review",
        )

    aggregate_score = total_score / total_weight
    aggregate_score = round(max(0.0, min(10.0, aggregate_score)), 2)

    if aggregate_score >= auto_approve_threshold:
        decision = ValidationDecision.AUTO_APPROVE
        rationale = f"score {aggregate_score} ≥ auto-approve threshold {auto_approve_threshold}"
    elif aggregate_score >= human_review_threshold:
        decision = ValidationDecision.HUMAN_REVIEW
        rationale = f"score {aggregate_score} in review band ({human_review_threshold}..{auto_approve_threshold})"
    else:
        decision = ValidationDecision.REJECT
        rationale = f"score {aggregate_score} < review threshold {human_review_threshold}"

    return AggregatedReport(
        decision=decision,
        aggregate_score=aggregate_score,
        reports=list(reports),
        rationale=rationale,
    )


__all__ = ["AggregatedReport", "ValidationDecision", "aggregate"]
