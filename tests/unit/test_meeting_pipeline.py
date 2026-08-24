"""Unit-тесты для meeting_pipeline (P1 — авто-триггер meeting_ended).

Все тяжёлые зависимости (CaptureOrchestrator / TaskAnalyzer /
EnhancedTZGenerator / feedback loop) инъектируются фейками — без LLM/графа/CLI.
"""
from __future__ import annotations

import asyncio

import pytest
from backend.core.automations.meeting_pipeline import (
    PipelineConfig,
    _coerce_config,
    _extract_task_list,
    _reset_registration_for_tests,
    _select_tasks,
    handle_meeting_ended,
    register_meeting_pipeline,
    run_meeting_pipeline,
)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _reset_reg():
    _reset_registration_for_tests()
    yield
    _reset_registration_for_tests()


# === Фейки ===============================================================

class _FakeOrchestrator:
    def __init__(self, tasks, *, status="completed"):
        self._tasks = tasks
        self._status = status

    async def process_meeting(self, *, transcript, meeting_id, meeting_context):
        return {"meeting_id": meeting_id, "status": self._status, "tasks": self._tasks}


class _FakeAnalyzer:
    def __init__(self, *, level="medium", fail=False):
        self._level = level
        self._fail = fail

    async def analyze(self, *, task_description, task_id, additional_context):
        if self._fail:
            raise RuntimeError("analyzer boom")
        return {
            "task_id": task_id,
            "analysis": {"complexity": {"level": self._level}},
        }


class _FakeTZ:
    def __init__(self, markdown="# ТЗ\nделай X", task_type="feature", fail=False):
        self._md = markdown
        self._tt = task_type
        self._fail = fail

    async def generate(self, request):
        if self._fail:
            raise RuntimeError("tz boom")

        class _R:
            markdown = self._md
            task_type = self._tt
            completeness_score = 0.8

        return _R()


def _orch(tasks, **kw):
    return lambda: _FakeOrchestrator(tasks, **kw)


def _an(**kw):
    return lambda: _FakeAnalyzer(**kw)


def _tz(**kw):
    return lambda: _FakeTZ(**kw)


# === Базовый happy-path ==================================================

def test_pipeline_extracts_analyzes_generates_tz() -> None:
    tasks = [
        {"title": "Сделать лендинг", "description": "по макету"},
        {"title": "Настроить аналитику", "description": ""},
    ]
    res = _run(run_meeting_pipeline(
        transcript="длинный транскрипт встречи",
        meeting_id="m1",
        user_id="u1",
        orchestrator_factory=_orch(tasks),
        analyzer_factory=_an(level="high"),
        tz_generator_factory=_tz(),
    ))
    assert res.status == "ok"
    assert res.tasks_extracted == 2
    assert res.tasks_processed == 2
    assert all(r.analyzed for r in res.records)
    assert all(r.tz_generated for r in res.records)
    assert res.records[0].complexity == "high"
    assert res.records[0].execution_decision == "skipped"  # auto_execute off


# === Нет транскрипта =====================================================

def test_no_transcript_skips() -> None:
    res = _run(run_meeting_pipeline(
        transcript="   ",
        meeting_id="m2",
        orchestrator_factory=_orch([{"title": "x"}]),
        analyzer_factory=_an(),
        tz_generator_factory=_tz(),
    ))
    assert res.status == "no_transcript"
    assert res.tasks_extracted == 0


# === Orchestrator error ==================================================

def test_orchestrator_error_status() -> None:
    res = _run(run_meeting_pipeline(
        transcript="t",
        meeting_id="m3",
        orchestrator_factory=_orch([], status="error"),
        analyzer_factory=_an(),
        tz_generator_factory=_tz(),
    ))
    assert res.status == "extract_failed"


def test_orchestrator_exception_caught() -> None:
    class _Boom:
        async def process_meeting(self, **kw):
            raise RuntimeError("kaboom")

    res = _run(run_meeting_pipeline(
        transcript="t", meeting_id="m4",
        orchestrator_factory=lambda: _Boom(),
        analyzer_factory=_an(), tz_generator_factory=_tz(),
    ))
    assert res.status == "extract_failed"
    assert "kaboom" in (res.error or "")


# === Нет задач ===========================================================

def test_no_tasks_ok_empty() -> None:
    res = _run(run_meeting_pipeline(
        transcript="t", meeting_id="m5",
        orchestrator_factory=_orch([]),
        analyzer_factory=_an(), tz_generator_factory=_tz(),
    ))
    assert res.status == "ok"
    assert res.tasks_extracted == 0
    assert res.records == []


# === max_tasks ограничивает ==============================================

def test_max_tasks_bounds_processing() -> None:
    tasks = [{"title": f"t{i}"} for i in range(10)]
    res = _run(run_meeting_pipeline(
        transcript="t", meeting_id="m6",
        config=PipelineConfig(max_tasks=3),
        orchestrator_factory=_orch(tasks),
        analyzer_factory=_an(), tz_generator_factory=_tz(),
    ))
    assert res.tasks_extracted == 10
    assert res.tasks_processed == 3
    assert len(res.records) == 3


# === Анализатор упал на одной задаче — конвейер продолжает ===============

def test_analyzer_failure_isolated_per_task() -> None:
    res = _run(run_meeting_pipeline(
        transcript="t", meeting_id="m7",
        orchestrator_factory=_orch([{"title": "a"}, {"title": "b"}]),
        analyzer_factory=_an(fail=True),
        tz_generator_factory=_tz(),
    ))
    assert res.status == "ok"
    assert res.tasks_processed == 2
    assert all(not r.analyzed for r in res.records)
    assert all("analyze" in (r.error or "") for r in res.records)


# === ТЗ-генератор упал — record с ошибкой, без падения ==================

def test_tz_failure_recorded() -> None:
    res = _run(run_meeting_pipeline(
        transcript="t", meeting_id="m8",
        orchestrator_factory=_orch([{"title": "a"}]),
        analyzer_factory=_an(),
        tz_generator_factory=_tz(fail=True),
    ))
    assert res.records[0].analyzed is True
    assert res.records[0].tz_generated is False
    assert "tz" in (res.records[0].error or "")


# === auto_execute → вызывает feedback_runner ============================

def test_auto_execute_invokes_feedback_runner() -> None:
    calls: list[dict] = []

    async def fake_runner(**kw):
        calls.append(kw)

        class _LR:
            final_decision = "auto_approve"

        return _LR()

    res = _run(run_meeting_pipeline(
        transcript="t", meeting_id="m9", user_id="u1",
        config=PipelineConfig(auto_execute=True, feedback_max_iterations=3),
        orchestrator_factory=_orch([{"title": "build api"}]),
        analyzer_factory=_an(),
        tz_generator_factory=_tz(task_type="api"),
        feedback_runner=fake_runner,
    ))
    assert len(calls) == 1
    assert calls[0]["task_type"] == "api"
    assert calls[0]["max_iterations"] == 3
    assert res.records[0].executed is True
    assert res.records[0].execution_decision == "auto_approve"


def test_auto_execute_off_by_default_skips_runner() -> None:
    called = []

    async def fake_runner(**kw):
        called.append(1)

    res = _run(run_meeting_pipeline(
        transcript="t", meeting_id="m10",
        orchestrator_factory=_orch([{"title": "x"}]),
        analyzer_factory=_an(), tz_generator_factory=_tz(),
        feedback_runner=fake_runner,
    ))
    assert called == []
    assert res.records[0].executed is False
    assert res.records[0].execution_decision == "skipped"


# === handle_meeting_ended никогда не raises ==============================

def test_handle_meeting_ended_never_raises_on_bad_event() -> None:
    class _BadEvent:
        payload = None  # триггерит .get на None внутри

    # не должно бросить
    _run(handle_meeting_ended(_BadEvent()))


def test_handle_meeting_ended_with_dict_event_like() -> None:
    class _Ev:
        id = "ev1"
        user_id = "u1"
        payload = {"meeting_id": "mz", "transcript": ""}  # пустой → no_transcript

    _run(handle_meeting_ended(_Ev()))  # не падает


# === Регистрация идемпотентна ===========================================

def test_register_is_idempotent() -> None:
    class _FakeBus:
        def __init__(self):
            self.subs: list = []

        def subscribe(self, et, h):
            self.subs.append((et, h))
            return "sub1"

    bus = _FakeBus()
    assert register_meeting_pipeline(bus) is True
    assert register_meeting_pipeline(bus) is False  # уже зарегистрирован
    assert len(bus.subs) == 1
    assert bus.subs[0][0] == "meeting_ended"


# === Хелперы =============================================================

def test_extract_task_list_handles_shapes() -> None:
    assert _extract_task_list({"tasks": [{"title": "a"}, "skip"]}) == [{"title": "a"}]
    assert _extract_task_list({"results": {"tasks": [{"x": 1}]}}) == [{"x": 1}]
    assert _extract_task_list({}) == []
    assert _extract_task_list({"tasks": "nope"}) == []
    assert _extract_task_list("not a dict") == []


def test_coerce_config_from_payload() -> None:
    cfg = _coerce_config({
        "max_tasks": 99, "auto_execute": True,
        "executor_backend": "noop", "feedback_max_iterations": 9,
    })
    assert cfg.max_tasks == 20          # clamped
    assert cfg.auto_execute is True
    assert cfg.executor_backend == "noop"
    assert cfg.feedback_max_iterations == 5  # clamped

    cfg2 = _coerce_config({})
    assert cfg2.max_tasks == 5
    assert cfg2.auto_execute is False
    assert cfg2.executor_backend is None


# === Vibe-tasking: выбор задач ==========================================

def test_select_tasks_by_title_and_assignee() -> None:
    tasks = [
        {"title": "Сделать презентацию", "assignee": "Anton"},
        {"title": "Разбор таблицы продаж", "assignee": "Masha"},
        {"title": "Медиаплан на квартал", "assignee": "Anton"},
    ]
    # без фильтра — все
    assert len(_select_tasks(tasks, PipelineConfig())) == 3
    # по заголовку (подстрочно)
    sel = _select_tasks(tasks, PipelineConfig(task_titles=["презентац"]))
    assert len(sel) == 1 and "презентац" in sel[0]["title"].lower()
    # по исполнителю
    assert len(_select_tasks(tasks, PipelineConfig(assignee_filter="anton"))) == 2
    # комбо
    cfg = PipelineConfig(task_titles=["медиаплан"], assignee_filter="anton")
    assert len(_select_tasks(tasks, cfg)) == 1


def test_coerce_config_parses_task_selection() -> None:
    cfg = _coerce_config({
        "task_titles": ["a", "  ", "b"], "assignee_filter": " Masha ",
        "task_ids": ["t1", " ", "t2"],
    })
    assert cfg.task_titles == ["a", "b"]
    assert cfg.task_ids == ["t1", "t2"]
    assert cfg.assignee_filter == "Masha"


def test_select_tasks_by_id_exact() -> None:
    # Выбор по id — точное совпадение, id важнее заголовков.
    tasks = [
        {"task_id": "t1", "title": "Сделать лендинг"},
        {"task_id": "t2", "title": "Отчёт по метрикам"},
        {"task_id": "t3", "title": "Медиаплан"},
    ]
    sel = _select_tasks(tasks, PipelineConfig(task_ids=["t1", "t3"]))
    assert {t["task_id"] for t in sel} == {"t1", "t3"}


def test_select_tasks_title_no_reverse_swallow() -> None:
    # Регресс: одно длинное ВЫБРАННОЕ название не должно «поглощать» все
    # короткие задачи (раньше `title in w` тянул всё). Выбрана длинная фраза,
    # которая содержит слова из других задач — но матч только forward (w in
    # title), поэтому лишние задачи НЕ попадают.
    tasks = [
        {"title": "Лендинг"},
        {"title": "Отчёт"},
        {"title": "Медиаплан"},
    ]
    cfg = PipelineConfig(task_titles=["Сделать лендинг, отчёт и медиаплан"])
    sel = _select_tasks(tasks, cfg)
    assert sel == []  # ни один короткий заголовок не содержит длинную фразу


def test_preselected_tasks_skip_extraction() -> None:
    # preselected_tasks заданы → оркестратор НЕ зовётся (пере-извлечения нет),
    # обрабатываются ровно переданные задачи.
    def _boom():
        raise AssertionError("orchestrator must not be called when preselected")

    res = _run(run_meeting_pipeline(
        transcript="t", meeting_id="ms", user_id="u1",
        preselected_tasks=[{"task_id": "t1", "title": "Выбранная задача"}],
        orchestrator_factory=_boom,
        analyzer_factory=_an(), tz_generator_factory=_tz(),
    ))
    assert res.tasks_extracted == 1
    assert res.tasks_processed == 1


def test_pipeline_task_selection_filters_before_processing() -> None:
    tasks = [
        {"title": "Сделать лендинг", "assignee": "Anton"},
        {"title": "Отчёт по метрикам", "assignee": "Masha"},
    ]
    res = _run(run_meeting_pipeline(
        transcript="t", meeting_id="ms", user_id="u1",
        config=PipelineConfig(task_titles=["лендинг"]),
        orchestrator_factory=_orch(tasks),
        analyzer_factory=_an(), tz_generator_factory=_tz(),
    ))
    assert res.tasks_extracted == 2      # извлекли обе
    assert res.tasks_processed == 1      # обработали только выбранную


# === Vibe-tasking: on_tz колбэк ==========================================

def test_on_tz_called_per_generated_tz() -> None:
    seen: list[dict] = []

    async def on_tz(*, task_description, task_type, tz_markdown, record):
        seen.append({"desc": task_description, "type": task_type,
                     "len": len(tz_markdown)})
        record.delivered = True
        record.dispatch_status = "delivered"

    res = _run(run_meeting_pipeline(
        transcript="t", meeting_id="mt", user_id="u1",
        orchestrator_factory=_orch([{"title": "a"}, {"title": "b"}]),
        analyzer_factory=_an(), tz_generator_factory=_tz(),
        on_tz=on_tz,
    ))
    assert len(seen) == 2
    assert all(s["len"] > 0 for s in seen)
    assert all(r.delivered and r.dispatch_status == "delivered"
               for r in res.records)


def test_on_tz_failure_does_not_break_pipeline() -> None:
    async def boom_on_tz(**kw):
        raise RuntimeError("deliver boom")

    res = _run(run_meeting_pipeline(
        transcript="t", meeting_id="mt2", user_id="u1",
        orchestrator_factory=_orch([{"title": "a"}]),
        analyzer_factory=_an(), tz_generator_factory=_tz(),
        on_tz=boom_on_tz,
    ))
    # колбэк упал, но конвейер завершился и ТЗ сгенерилось
    assert res.status == "ok"
    assert res.records[0].tz_generated is True
