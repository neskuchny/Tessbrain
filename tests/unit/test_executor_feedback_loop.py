"""Unit-тесты для feedback_loop (P0 — автономная петля).

Покрытие:
- сходимость с первой попытки (validators проходят)
- доработка: 1-я попытка фейл → refined ТЗ проходит
- стоп по max_iterations без сходимости
- executor-фейл (no artifacts) → reject, без зацикливания
- дефолтный refine дописывает блок замечаний
- on_iteration хук вызывается на каждой итерации
- timeout результата → human_review
"""
from __future__ import annotations

import asyncio

import pytest
from backend.core.executors.backends.noop import NoopExecutor
from backend.core.executors.base import (
    ExecutorBackend,
    TaskHandle,
    TaskResult,
    TaskStatus,
    TaskSubmission,
)
from backend.core.executors.feedback_loop import (
    default_refine_tz,
    run_feedback_loop,
)
from backend.core.executors.store import reset_memory_store
from backend.core.validator.aggregator import aggregate
from backend.core.validator.base import (
    Issue,
    IssueSeverity,
    Validator,
    ValidatorReport,
)


@pytest.fixture(autouse=True)
def _clear_store():
    reset_memory_store()
    yield
    reset_memory_store()


def _run(coro):
    return asyncio.run(coro)


async def _no_sleep(_: float) -> None:
    return None


# === Тестовые дублёры ====================================================

class _ScriptedValidator(Validator):
    """Возвращает заранее заданные scores по очереди вызовов."""

    name = "code"

    def __init__(self, scores: list[float], *, with_issue: bool = True) -> None:
        self._scores = list(scores)
        self._call = 0
        self._with_issue = with_issue

    async def validate(self, *, task_description, tz_markdown, artifacts, **kw):
        score = self._scores[min(self._call, len(self._scores) - 1)]
        self._call += 1
        issues = []
        if self._with_issue and score < 8.5:
            issues = [
                Issue(
                    IssueSeverity.ERROR,
                    "lint_error",
                    "unused import",
                    location="src/index.ts:3",
                    fix_hint="remove unused import",
                )
            ]
        return ValidatorReport(
            validator=self.name, score=score, issues=issues
        )


class _BlockerValidator(Validator):
    name = "code"

    async def validate(self, *, task_description, tz_markdown, artifacts, **kw):
        return ValidatorReport(
            validator=self.name,
            score=0.0,
            issues=[Issue(IssueSeverity.BLOCKER, "crash", "syntax error")],
        )


class _FailingExecutor(ExecutorBackend):
    """Executor который возвращает success=False / no artifacts."""

    name = "failing"

    async def submit(self, submission: TaskSubmission) -> TaskHandle:
        h = TaskHandle.new(backend=self.name)
        h.status = TaskStatus.FAILED
        return h

    async def get_status(self, handle: TaskHandle) -> TaskStatus:
        return TaskStatus.FAILED

    async def get_result(self, handle: TaskHandle):
        return TaskResult(
            handle_id=handle.id,
            status=TaskStatus.FAILED,
            success=False,
            artifacts=[],
            error_message="executor blew up",
        )


class _NeverReadyExecutor(ExecutorBackend):
    """get_result всегда None → проверяем timeout-ветку."""

    name = "never"

    async def submit(self, submission: TaskSubmission) -> TaskHandle:
        return TaskHandle.new(backend=self.name)

    async def get_status(self, handle: TaskHandle) -> TaskStatus:
        return TaskStatus.RUNNING

    async def get_result(self, handle: TaskHandle):
        return None


# === Сходимость с первой попытки =========================================

def test_converges_first_iteration_when_score_high() -> None:
    res = _run(run_feedback_loop(
        submission=TaskSubmission(tz_markdown="build a thing"),
        backend=NoopExecutor(),
        validators=[_ScriptedValidator([9.5])],
        max_iterations=3,
        sleep=_no_sleep,
    ))
    assert res.converged is True
    assert res.final_decision == "auto_approve"
    assert len(res.iterations) == 1
    assert res.final_score == 9.5
    assert res.iterations[0].iteration == 1


# === Доработка: фейл → refined проходит ==================================

def test_iterates_then_converges() -> None:
    # 1-я попытка 6.0 (human_review), 2-я — 9.0 (auto_approve)
    res = _run(run_feedback_loop(
        submission=TaskSubmission(tz_markdown="build a thing"),
        backend=NoopExecutor(),
        validators=[_ScriptedValidator([6.0, 9.0])],
        max_iterations=3,
        sleep=_no_sleep,
    ))
    assert res.converged is True
    assert len(res.iterations) == 2
    assert res.iterations[0].decision == "human_review"
    assert res.iterations[1].decision == "auto_approve"
    assert res.final_score == 9.0


# === Стоп по max_iterations без сходимости ===============================

def test_stops_at_max_iterations() -> None:
    res = _run(run_feedback_loop(
        submission=TaskSubmission(tz_markdown="build a thing"),
        backend=NoopExecutor(),
        validators=[_ScriptedValidator([4.0, 4.0, 4.0])],
        max_iterations=2,
        sleep=_no_sleep,
    ))
    assert res.converged is False
    assert len(res.iterations) == 2  # не больше max
    assert res.final_decision in {"reject", "human_review"}


# === Executor-фейл → reject, без зацикливания ============================

def test_executor_failure_stops_immediately() -> None:
    res = _run(run_feedback_loop(
        submission=TaskSubmission(tz_markdown="build a thing"),
        backend=_FailingExecutor(),
        validators=[_ScriptedValidator([9.0])],
        max_iterations=3,
        sleep=_no_sleep,
    ))
    assert res.converged is False
    assert len(res.iterations) == 1  # не ретраит тем же ТЗ
    assert res.iterations[0].success is False
    assert res.final_decision == "reject"


# === Blocker → reject и стоп ============================================

def test_blocker_forces_reject() -> None:
    res = _run(run_feedback_loop(
        submission=TaskSubmission(tz_markdown="build a thing"),
        backend=NoopExecutor(),
        validators=[_BlockerValidator()],
        max_iterations=2,
        sleep=_no_sleep,
    ))
    # blocker → aggregate REJECT score 0; всё равно даём 2 попытки,
    # но дефолтный refine добавит блок (blocker actionable) → 2 итерации
    assert res.converged is False
    assert res.iterations[0].blocker_seen is True
    assert res.final_decision == "reject"


# === Timeout результата → human_review ==================================

def test_result_timeout_returns_human_review() -> None:
    res = _run(run_feedback_loop(
        submission=TaskSubmission(tz_markdown="x"),
        backend=_NeverReadyExecutor(),
        validators=[_ScriptedValidator([9.0])],
        max_iterations=2,
        poll_interval=1.0,
        poll_timeout=2.0,
        sleep=_no_sleep,
    ))
    assert res.converged is False
    assert len(res.iterations) == 1
    assert res.iterations[0].decision == "human_review"
    assert res.iterations[0].error_message == "executor result timeout"


# === on_iteration хук вызывается ========================================

def test_on_iteration_hook_called() -> None:
    seen: list[int] = []

    async def hook(rec) -> None:
        seen.append(rec.iteration)

    _run(run_feedback_loop(
        submission=TaskSubmission(tz_markdown="x"),
        backend=NoopExecutor(),
        validators=[_ScriptedValidator([5.0, 5.0])],
        max_iterations=2,
        on_iteration=hook,
        sleep=_no_sleep,
    ))
    assert seen == [1, 2]


# === Дефолтный refine дописывает блок замечаний ==========================

def test_default_refine_appends_issues_block() -> None:
    reports = [ValidatorReport(
        validator="code", score=4.0,
        issues=[Issue(IssueSeverity.ERROR, "lint", "bad", fix_hint="fix it")],
    )]
    agg = aggregate(reports)
    new_tz = default_refine_tz(
        original_tz="# Original TZ", aggregated=agg, iteration=1
    )
    assert "# Original TZ" in new_tz
    assert "Замечания по предыдущей реализации (итерация 1)" in new_tz
    assert "fix it" in new_tz
    assert len(new_tz) > len("# Original TZ")


def test_default_refine_noop_when_no_actionable_issues() -> None:
    # только INFO — не actionable, ТЗ не меняется
    reports = [ValidatorReport(
        validator="code", score=9.0,
        issues=[Issue(IssueSeverity.INFO, "note", "fyi")],
    )]
    agg = aggregate(reports)
    new_tz = default_refine_tz(
        original_tz="# Original", aggregated=agg, iteration=1
    )
    assert new_tz == "# Original"


# === ТЗ реально мутирует между итерациями ===============================

def test_tz_grows_between_iterations() -> None:
    """Проверяем что refined ТЗ доходит до executor'а (Noop эхо-артефакт)."""
    captured: list[str] = []

    class _CapturingNoop(NoopExecutor):
        async def submit(self, submission: TaskSubmission) -> TaskHandle:
            captured.append(submission.tz_markdown)
            return await super().submit(submission)

    _run(run_feedback_loop(
        submission=TaskSubmission(tz_markdown="BASE"),
        backend=_CapturingNoop(),
        validators=[_ScriptedValidator([5.0, 9.0])],
        max_iterations=2,
        sleep=_no_sleep,
    ))
    assert len(captured) == 2
    assert captured[0] == "BASE"
    assert captured[1].startswith("BASE")
    assert "Замечания по предыдущей реализации" in captured[1]
