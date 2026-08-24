# -*- coding: utf-8 -*-
"""Feedback loop — автономная петля «исполнил → проверил → доработал» (P0).

Это недостающее звено видения: после того как executor (W19/W20) вернул
артефакты, система **сама** прогоняет валидаторы (W21), агрегирует вердикт
и, если результат не дотягивает до auto-approve, **переотправляет** ТЗ
в executor с добавленным блоком замечаний — ограниченное число итераций.

Что переиспользуется (НЕ переписываем — оркеструем существующее):
- `core.executors.*` — backend.submit / get_result / store
- `core.validator.*` — CodeValidator/StructuralValidator + aggregate()
- `core.tz.iteration.generate_delta` — опц. LLM-доработка ТЗ

Дизайн:
- async-first, никаких raises на «нормальных» ошибках
- всё инъектируемо (backend, validators, refine_tz, sleep) → юнит-тест
  без реального CLI/LLM (NoopExecutor)
- bounded iterations (по умолчанию 2) — защита от зацикливания
- детерминированный дефолтный `refine_tz` (без LLM): добавляет секцию
  замечаний к ТЗ. LLM-доработка — опциональный апгрейд.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

from backend.core.executors.base import (
    ExecutorBackend,
    TaskResult,
    TaskStatus,
    TaskSubmission,
)
from backend.core.validator.aggregator import (
    AggregatedReport,
    ValidationDecision,
    aggregate,
)
from backend.core.validator.base import Issue, IssueSeverity, Validator, ValidatorReport

logger = logging.getLogger(__name__)


# === Результаты ==========================================================

@dataclass
class IterationRecord:
    """Одна итерация петли: что отправили, что получили, как оценили."""
    iteration: int                       # 1-based
    handle_id: str
    decision: str                        # auto_approve | human_review | reject
    aggregate_score: float               # 0..10
    issue_count: int
    blocker_seen: bool
    success: bool                        # executor success
    rationale: str = ""
    error_message: Optional[str] = None  # если executor/валидация упали

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "handle_id": self.handle_id,
            "decision": self.decision,
            "aggregate_score": self.aggregate_score,
            "issue_count": self.issue_count,
            "blocker_seen": self.blocker_seen,
            "success": self.success,
            "rationale": self.rationale,
            "error_message": self.error_message,
        }


@dataclass
class FeedbackLoopResult:
    """Итог всей петли."""
    converged: bool                      # достигли AUTO_APPROVE
    final_decision: str
    final_score: float
    iterations: list[IterationRecord] = field(default_factory=list)
    final_handle_id: Optional[str] = None
    final_artifacts: list[dict[str, Any]] = field(default_factory=list)
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "converged": self.converged,
            "final_decision": self.final_decision,
            "final_score": self.final_score,
            "final_handle_id": self.final_handle_id,
            "iterations": [r.to_dict() for r in self.iterations],
            "iteration_count": len(self.iterations),
            "artifact_count": len(self.final_artifacts),
            "rationale": self.rationale,
        }


# === Дефолтный детерминированный refine (без LLM) =======================

_SEVERITY_ORDER = {
    IssueSeverity.BLOCKER: 0,
    IssueSeverity.ERROR: 1,
    IssueSeverity.WARNING: 2,
    IssueSeverity.INFO: 3,
}


def _format_issues_block(issues: Sequence[Issue], *, iteration: int) -> str:
    """Собрать markdown-блок замечаний для следующей итерации.

    Детерминированно, без LLM. Сортировка по severity, INFO отбрасываем
    (не actionable для доработки кода).
    """
    actionable = [i for i in issues if i.severity != IssueSeverity.INFO]
    if not actionable:
        return ""
    actionable.sort(key=lambda i: (_SEVERITY_ORDER.get(i.severity, 9), i.category))

    lines: list[str] = [
        "",
        f"## ⚠️ Замечания по предыдущей реализации (итерация {iteration})",
        "",
        "Предыдущая попытка не прошла авто-проверку. Исправь следующее, "
        "сохранив всё, что уже сделано корректно:",
        "",
    ]
    for idx, issue in enumerate(actionable, 1):
        loc = f" (`{issue.location}`)" if issue.location else ""
        hint = f" — {issue.fix_hint}" if issue.fix_hint else ""
        lines.append(
            f"{idx}. **[{issue.severity.value.upper()}] {issue.category}**{loc}: "
            f"{issue.message}{hint}"
        )
    lines.append("")
    return "\n".join(lines)


def default_refine_tz(
    *, original_tz: str, aggregated: AggregatedReport, iteration: int
) -> str:
    """Дефолтная доработка ТЗ: дописать секцию замечаний. Без LLM."""
    block = _format_issues_block(aggregated.all_issues, iteration=iteration)
    if not block:
        return original_tz
    return original_tz.rstrip() + "\n" + block


# Тип кастомного refine: (original_tz, aggregated, iteration) -> new_tz
RefineFn = Callable[..., str]
# Тип хука на итерацию: (record) -> Awaitable | None
IterationHook = Callable[[IterationRecord], Optional[Awaitable[None]]]


# === Поллинг результата ==================================================

async def _await_result(
    *,
    backend: ExecutorBackend,
    handle,
    poll_interval: float,
    poll_timeout: float,
    sleep: Callable[[float], Awaitable[None]],
) -> Optional[TaskResult]:
    """Дождаться TaskResult. None если timeout / не финализировался."""
    waited = 0.0
    while True:
        try:
            res = await backend.get_result(handle)
        except Exception as exc:  # backend сам сломался — не падаем
            logger.warning("feedback_loop: get_result error: %s", exc)
            res = None
        if res is not None and res.status in {
            TaskStatus.DONE,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }:
            return res
        if waited >= poll_timeout:
            return res  # может быть None — обработаем выше
        await sleep(poll_interval)
        waited += poll_interval


async def _run_validators(
    *,
    validators: Sequence[Validator],
    task_description: str,
    tz_markdown: str,
    artifacts: list[dict[str, Any]],
) -> list[ValidatorReport]:
    """Прогнать валидаторы конкурентно. Каждый не raises (по контракту)."""
    if not validators:
        return []

    async def _safe(v: Validator) -> ValidatorReport:
        try:
            return await v.validate(
                task_description=task_description,
                tz_markdown=tz_markdown,
                artifacts=artifacts,
            )
        except Exception as exc:  # на всякий — контракт говорит не raises
            logger.warning("feedback_loop: validator %s crashed: %s", v.name, exc)
            return ValidatorReport(validator=v.name, score=0.0, error=str(exc))

    return list(await asyncio.gather(*[_safe(v) for v in validators]))


# === Главная петля =======================================================

async def run_feedback_loop(
    *,
    submission: TaskSubmission,
    backend: ExecutorBackend,
    validators: Sequence[Validator],
    task_description: str = "",
    max_iterations: int = 2,
    auto_approve_threshold: float = 8.5,
    human_review_threshold: float = 5.0,
    poll_interval: float = 2.0,
    poll_timeout: float = 1800.0,
    refine_tz: Optional[RefineFn] = None,
    on_iteration: Optional[IterationHook] = None,
    sleep: Optional[Callable[[float], Awaitable[None]]] = None,
) -> FeedbackLoopResult:
    """Запустить автономный цикл submit → validate → (refine → resubmit).

    Args:
        submission: первичное ТЗ для executor'а.
        backend: executor backend (Noop/ClaudeCodeCLI/OpenHands/...).
        validators: список валидаторов (CodeValidator, StructuralValidator...).
        task_description: исходная просьба юзера (для validator-контекста).
        max_iterations: макс. число попыток (>=1). Защита от зацикливания.
        auto_approve_threshold / human_review_threshold: пороги aggregate().
        poll_interval / poll_timeout: ожидание результата от executor'а.
        refine_tz: как доработать ТЗ между итерациями
                   (default — детерминированно, без LLM).
        on_iteration: async/sync хук после каждой итерации (для audit/log).
        sleep: инъекция asyncio.sleep (тесты передают no-op).

    Returns:
        FeedbackLoopResult. Никогда не raises.
    """
    if max_iterations < 1:
        max_iterations = 1
    refine = refine_tz or default_refine_tz
    _sleep = sleep or asyncio.sleep

    result = FeedbackLoopResult(
        converged=False,
        final_decision=ValidationDecision.REJECT.value,
        final_score=0.0,
    )

    current_tz = submission.tz_markdown
    task_desc = task_description or submission.metadata.get("task_description") or ""

    for i in range(1, max_iterations + 1):
        # 1) Сабмит текущего ТЗ
        sub = TaskSubmission(
            tz_markdown=current_tz,
            task_type=submission.task_type,
            working_dir=submission.working_dir,
            metadata={**submission.metadata, "feedback_iteration": i},
            callback_url=submission.callback_url,
            timeout_seconds=submission.timeout_seconds,
        )
        try:
            handle = await backend.submit(sub)
        except Exception as exc:
            logger.warning("feedback_loop: submit failed (iter %d): %s", i, exc)
            rec = IterationRecord(
                iteration=i, handle_id="", decision=ValidationDecision.REJECT.value,
                aggregate_score=0.0, issue_count=0, blocker_seen=False,
                success=False, error_message=f"submit failed: {exc}",
            )
            result.iterations.append(rec)
            await _emit_hook(on_iteration, rec)
            break

        # 2) Дождаться результата
        task_result = await _await_result(
            backend=backend, handle=handle,
            poll_interval=poll_interval, poll_timeout=poll_timeout,
            sleep=_sleep,
        )

        if task_result is None:
            rec = IterationRecord(
                iteration=i, handle_id=handle.id,
                decision=ValidationDecision.HUMAN_REVIEW.value,
                aggregate_score=0.0, issue_count=0, blocker_seen=False,
                success=False, error_message="executor result timeout",
            )
            result.iterations.append(rec)
            result.final_handle_id = handle.id
            await _emit_hook(on_iteration, rec)
            break

        # 3) Если executor сам провалился — нет смысла валидировать пустоту
        if not task_result.success or not task_result.artifacts:
            rec = IterationRecord(
                iteration=i, handle_id=handle.id,
                decision=ValidationDecision.REJECT.value,
                aggregate_score=0.0, issue_count=0, blocker_seen=False,
                success=task_result.success,
                error_message=task_result.error_message or "no artifacts produced",
            )
            result.iterations.append(rec)
            result.final_handle_id = handle.id
            result.final_artifacts = list(task_result.artifacts)
            await _emit_hook(on_iteration, rec)
            # executor-фейл — переотправлять тем же ТЗ смысла мало; стоп
            break

        # 4) Валидация
        reports = await _run_validators(
            validators=validators,
            task_description=task_desc,
            tz_markdown=current_tz,
            artifacts=task_result.artifacts,
        )
        aggregated = aggregate(
            reports,
            auto_approve_threshold=auto_approve_threshold,
            human_review_threshold=human_review_threshold,
        )

        rec = IterationRecord(
            iteration=i,
            handle_id=handle.id,
            decision=aggregated.decision.value,
            aggregate_score=aggregated.aggregate_score,
            issue_count=len(aggregated.all_issues),
            blocker_seen=aggregated.blocker_seen,
            success=True,
            rationale=aggregated.rationale,
        )
        result.iterations.append(rec)
        result.final_handle_id = handle.id
        result.final_artifacts = list(task_result.artifacts)
        result.final_decision = aggregated.decision.value
        result.final_score = aggregated.aggregate_score
        result.rationale = aggregated.rationale
        await _emit_hook(on_iteration, rec)

        # 5) Сошлось?
        if aggregated.decision == ValidationDecision.AUTO_APPROVE:
            result.converged = True
            break

        # 6) Последняя итерация — стоп без доработки
        if i >= max_iterations:
            break

        # 7) Доработать ТЗ для следующей попытки
        try:
            refined = refine(
                original_tz=current_tz, aggregated=aggregated, iteration=i
            )
        except Exception as exc:
            logger.warning("feedback_loop: refine_tz failed: %s", exc)
            break
        if not refined or refined.strip() == current_tz.strip():
            # Нечего улучшать (нет actionable issues) — дальше бессмысленно
            result.rationale = (
                aggregated.rationale + " | no actionable issues to iterate on"
            )
            break
        current_tz = refined

    return result


async def _emit_hook(
    hook: Optional[IterationHook], record: IterationRecord
) -> None:
    if hook is None:
        return
    try:
        maybe = hook(record)
        if maybe is not None and hasattr(maybe, "__await__"):
            await maybe
    except Exception as exc:  # хук не должен ломать петлю
        logger.debug("feedback_loop: on_iteration hook error: %s", exc)


__all__ = [
    "FeedbackLoopResult",
    "IterationRecord",
    "default_refine_tz",
    "run_feedback_loop",
]
