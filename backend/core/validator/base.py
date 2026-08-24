"""Base abstraction для validators (W21).

Все validators возвращают `ValidatorReport` — единая структура, которую
aggregator комбинирует в финальный verdict.

Иерархия severities:
- INFO — наблюдение, не уменьшает score
- WARNING — мелкий косяк (-0.5..-1)
- ERROR — серьёзная проблема (-2..-4)
- BLOCKER — задача провалена, score forced to 0

Score шкала: 0..10 (как в TZ judge).
"""
from __future__ import annotations

import abc
import enum
from dataclasses import dataclass, field
from typing import Any, Optional


class IssueSeverity(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


@dataclass(frozen=True)
class Issue:
    """Одна найденная проблема."""
    severity: IssueSeverity
    category: str          # e.g. "missing_block", "lint_error", "tone_mismatch"
    message: str
    location: Optional[str] = None    # e.g. "hero.headline", "src/index.tsx:42"
    fix_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity.value,
            "category": self.category,
            "message": self.message,
            "location": self.location,
            "fix_hint": self.fix_hint,
        }


@dataclass
class ValidatorReport:
    """Результат одного validator'а."""
    validator: str          # имя validator'а
    score: float            # 0..10
    issues: list[Issue] = field(default_factory=list)
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None    # если validator сам сломался

    @property
    def has_blocker(self) -> bool:
        return any(i.severity == IssueSeverity.BLOCKER for i in self.issues)

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues
                   if i.severity in {IssueSeverity.ERROR, IssueSeverity.BLOCKER})

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == IssueSeverity.WARNING)

    def to_dict(self) -> dict[str, Any]:
        return {
            "validator": self.validator,
            "score": self.score,
            "summary": self.summary,
            "issues": [i.to_dict() for i in self.issues],
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "has_blocker": self.has_blocker,
            "metadata": dict(self.metadata),
            "error": self.error,
        }


class Validator(abc.ABC):
    """Абстрактный validator — наследуют конкретные реализации."""

    name: str = "abstract"

    @abc.abstractmethod
    async def validate(
        self,
        *,
        task_description: str,
        tz_markdown: str,
        artifacts: list[dict[str, Any]],
        **kwargs: Any,
    ) -> ValidatorReport:
        """Валидировать результат относительно ТЗ.

        Args:
            task_description: исходная просьба от юзера.
            tz_markdown: сгенерированный ТЗ (W18).
            artifacts: список TaskResult.artifacts от executor'а.
            **kwargs: backend-specific (template, brand_context_block, ...).

        Returns:
            ValidatorReport. НЕ raises — все ошибки в `error` поле.
        """


__all__ = [
    "Issue",
    "IssueSeverity",
    "Validator",
    "ValidatorReport",
]
