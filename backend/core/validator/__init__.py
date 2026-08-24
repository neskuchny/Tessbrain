"""Result validator (W21) — финальный этап loop'а.

После того как executor (W19/W20) вернул artifacts, мы проверяем что
результат **соответствует** ТЗ. Pipeline:

  TaskResult + TaskSubmission  →  validators[]  →  ValidatorReport
                                                         │
                                                         ▼
                                            score ≥ threshold? auto-approve : human review

Validators:
- StructuralValidator — для markdown/документов: сверяет блоки с template (W18)
- CodeValidator — для кода: lint/type-check/tests
- AIJudgeValidator — LLM-based brief-vs-result
- VisualDiffValidator — UI screenshots vs brief (Playwright + AI)

Aggregator комбинирует scores и принимает финальное решение.
"""
from backend.core.validator.aggregator import (
    AggregatedReport,
    ValidationDecision,
    aggregate,
)
from backend.core.validator.base import (
    Issue,
    IssueSeverity,
    Validator,
    ValidatorReport,
)
from backend.core.validator.store import (
    QuotaConfig,
    QuotaExceeded,
    StoredValidation,
    ValidationResultService,
    assert_within_quota,
)

__all__ = [
    "AggregatedReport",
    "Issue",
    "IssueSeverity",
    "QuotaConfig",
    "QuotaExceeded",
    "StoredValidation",
    "ValidationDecision",
    "ValidationResultService",
    "Validator",
    "ValidatorReport",
    "aggregate",
    "assert_within_quota",
]
