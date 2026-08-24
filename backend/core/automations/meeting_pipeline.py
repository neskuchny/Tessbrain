# -*- coding: utf-8 -*-
"""Meeting pipeline — авто-триггер `meeting_ended` → весь конвейер (P1).

Это второе недостающее звено видения: «встреча закончилась → задачи
извлеклись → проанализировались → ТЗ сгенерилось → (опц.) ушло в executor
с петлёй P0» — БЕЗ единого ручного клика.

До этого: EventBus эмитил `meeting_ended` (meetflow webhook), но **ни один
подписчик не был зарегистрирован** (`grep .subscribe()` → только сам
EventBus). P1 регистрирует первого реального подписчика.

Конвейер (каждый шаг best-effort, никогда не raises — контракт
EventHandler):

  meeting_ended event
        │
        ▼
  1. CaptureOrchestrator.process_meeting(transcript)  → tasks[]
        │
        ▼
  2. TaskAnalyzerAgent.analyze(task)  → complexity/risks/deps   (top-N задач)
        │
        ▼
  3. EnhancedTZGenerator.generate(TZRequest)  → ТЗ markdown
        │
        ▼
  4. (опц., gated) run_feedback_loop(submission, backend, validators)  → P0

Дизайн:
- async-first, никогда не raises (handler-контракт EventBus)
- ВСЕ тяжёлые зависимости инъектируемы (factory-функции) → юнит-тест
  без LLM/графа/CLI
- bounded: max_tasks (default 5) — не генерим 50 ТЗ с одной встречи
- auto_execute OFF by default — реальное исполнение кода требует явного
  флага (`payload.auto_execute=true` или settings) — безопасность/стоимость
- идемпотентная регистрация подписчика (флаг модуля)
- полный audit_log на каждом шаге
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# === Результаты ==========================================================

@dataclass
class TaskPipelineRecord:
    """Что произошло с одной задачей в конвейере."""
    task_title: str
    analyzed: bool = False
    complexity: str = ""
    tz_generated: bool = False
    tz_task_type: str = ""
    tz_completeness: float = 0.0
    tz_length: int = 0
    closure_summary: str = ""          # P2: «чтобы закрыть, нужно: …»
    closure_extra_context: str = ""    # P2: markdown-блок для ТЗ (не сериализуем целиком)
    executed: bool = False
    execution_decision: str = ""       # auto_approve/human_review/reject/skipped
    delivered: bool = False            # ТЗ доставлено в канал (on_tz)
    handoff_id: str = ""               # id гейт-хэндоффа (dispatch_mode=gate)
    dispatch_status: str = ""          # none|delivered|gated|dispatched|error
    tracker_task_id: str = ""          # id созданной задачи в трекере (L2)
    tracker_system: str = ""           # yougile|trello|jira (L2)
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_title": self.task_title,
            "analyzed": self.analyzed,
            "complexity": self.complexity,
            "closure_summary": self.closure_summary,
            "tz_generated": self.tz_generated,
            "tz_task_type": self.tz_task_type,
            "tz_completeness": self.tz_completeness,
            "tz_length": self.tz_length,
            "executed": self.executed,
            "execution_decision": self.execution_decision,
            "delivered": self.delivered,
            "handoff_id": self.handoff_id,
            "dispatch_status": self.dispatch_status,
            "tracker_task_id": self.tracker_task_id,
            "tracker_system": self.tracker_system,
            "error": self.error,
        }


@dataclass
class MeetingPipelineResult:
    """Итог обработки одной встречи."""
    meeting_id: str
    status: str                        # ok | no_transcript | extract_failed | error
    tasks_extracted: int = 0
    tasks_processed: int = 0
    records: list[TaskPipelineRecord] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "meeting_id": self.meeting_id,
            "status": self.status,
            "tasks_extracted": self.tasks_extracted,
            "tasks_processed": self.tasks_processed,
            "records": [r.to_dict() for r in self.records],
            "error": self.error,
        }


# === Инъектируемые фабрики (default = реальные, test = фейки) ============

# () -> объект с .process_meeting(transcript, meeting_id, meeting_context)
OrchestratorFactory = Callable[[], Any]
# () -> объект с .analyze(task_description, task_id, additional_context)
AnalyzerFactory = Callable[[], Any]
# () -> объект с .generate(TZRequest) -> TZResult
TZGeneratorFactory = Callable[[], Any]


def _default_orchestrator() -> Any:
    from backend.core.capture.orchestrator import CaptureOrchestrator
    return CaptureOrchestrator()


def _default_analyzer() -> Any:
    from backend.core.think.task_specification.task_analyzer import (
        TaskAnalyzerAgent,
    )
    return TaskAnalyzerAgent()


def _default_tz_generator() -> Any:
    from backend.core.think.task_specification.enhanced_tz_generator import (
        EnhancedTZGenerator,
    )
    return EnhancedTZGenerator()


# === Конфиг ==============================================================

@dataclass
class _TZRequestLike:
    """Дак-тайп под EnhancedTZGenerator.generate(request).

    Генератор только атрибутно читает request (нет isinstance-проверки),
    поэтому не тащим тяжёлый импорт enhanced_tz_generator в этот модуль —
    он остаётся ленивым в default-фабрике. Поля зеркалят реальный TZRequest.
    """
    description: str
    user_id: str = ""
    meeting_ids: list = field(default_factory=list)
    document_ids: list = field(default_factory=list)
    project_filter: list = field(default_factory=list)
    extra_context: str = ""
    output_format: str = "full"


@dataclass
class PipelineConfig:
    max_tasks: int = 5                 # верхний предел ТЗ с одной встречи
    auto_execute: bool = False         # запускать ли P0 executor-петлю
    executor_backend: Optional[str] = None  # override; None → active
    feedback_max_iterations: int = 2
    # --- vibe-tasking: выбор конкретных задач ---
    # Ручной запуск: пользователь выбирает задачи галочками. ПРЕДПОЧТИТЕЛЬНО
    # приходят их task_id (стабильная сущность) — тогда caller грузит именно
    # эти задачи из хранилища и передаёт как preselected_tasks (без пере-
    # извлечения и нечёткого матча). task_titles — legacy-фолбэк на случай
    # старого клиента без id. Для event-триггера оба списка пусты → отбор
    # критериями ниже.
    task_ids: list = field(default_factory=list)
    task_titles: list = field(default_factory=list)
    assignee_filter: str = ""          # только задачи этого исполнителя


def _coerce_config(payload: dict[str, Any]) -> PipelineConfig:
    """Собрать конфиг из payload события (+ безопасные дефолты)."""
    cfg = PipelineConfig()
    try:
        if "max_tasks" in payload:
            cfg.max_tasks = max(1, min(int(payload["max_tasks"]), 20))
    except (TypeError, ValueError):
        pass
    cfg.auto_execute = bool(payload.get("auto_execute", False))
    backend = payload.get("executor_backend")
    if isinstance(backend, str) and backend.strip():
        cfg.executor_backend = backend.strip()
    try:
        if "feedback_max_iterations" in payload:
            cfg.feedback_max_iterations = max(
                1, min(int(payload["feedback_max_iterations"]), 5)
            )
    except (TypeError, ValueError):
        pass
    ids = payload.get("task_ids")
    if isinstance(ids, list):
        cfg.task_ids = [str(i).strip() for i in ids if str(i).strip()]
    titles = payload.get("task_titles")
    if isinstance(titles, list):
        cfg.task_titles = [str(t).strip() for t in titles if str(t).strip()]
    cfg.assignee_filter = str(payload.get("assignee_filter") or "").strip()
    return cfg


def _select_tasks(
    tasks: list[dict[str, Any]], cfg: PipelineConfig,
) -> list[dict[str, Any]]:
    """Отобрать задачи по критериям конфига (до ограничения max_tasks).

    - task_ids: если задан — ТОЧНОЕ совпадение по id (стабильная сущность,
      не путается формулировками). Это основной путь ручного выбора.
    - task_titles: legacy-фолбэк (id нет) — выбранное значение содержится в
      ЗАГОЛОВКЕ задачи (подстрочно, регистронезависимо: «лендинг» → «Сделать
      лендинг»). Раньше матч шёл в ОБЕ стороны и по «title. description», из-за
      чего одно длинное выбранное название «поглощало» много коротких задач
      (и в ТЗ уходили все) — обратное направление и описание убраны.
    - assignee_filter: если задан — только задачи этого исполнителя.
    Пустые критерии → без фильтрации (берём как есть).
    """
    out = tasks
    if cfg.task_ids:
        wanted_ids = {str(i).strip() for i in cfg.task_ids}
        def _id_match(task: dict[str, Any]) -> bool:
            tid = str(task.get("id") or task.get("task_id") or "").strip()
            return bool(tid) and tid in wanted_ids
        out = [t for t in out if _id_match(t)]
    elif cfg.task_titles:
        wanted = [t.strip().lower() for t in cfg.task_titles if t.strip()]
        def _title_match(task: dict[str, Any]) -> bool:
            title = (task.get("title") or task.get("task_title")
                     or task.get("name") or "").strip().lower()
            return bool(title) and any(w in title for w in wanted)
        out = [t for t in out if _title_match(t)]
    if cfg.assignee_filter:
        af = cfg.assignee_filter.lower()
        out = [t for t in out
               if af in str(t.get("assignee") or t.get("owner") or "").lower()]
    return out


# === Извлечение задач ====================================================

def _extract_task_list(capture_result: dict[str, Any]) -> list[dict[str, Any]]:
    """Достать список задач из результата CaptureOrchestrator.

    Контракт orchestrator: top-level `tasks` (из TaskAgent). Защищаемся
    от вложенного `results.tasks` и не-list.
    """
    if not isinstance(capture_result, dict):
        return []
    tasks = capture_result.get("tasks")
    if tasks is None and isinstance(capture_result.get("results"), dict):
        tasks = capture_result["results"].get("tasks")
    if not isinstance(tasks, list):
        return []
    return [t for t in tasks if isinstance(t, dict)]


def _closure_to_extra_context(closure: dict[str, Any]) -> str:
    """Собрать markdown-блок предусловий из dict dependency_closure (P2).

    Зеркалит DependencyClosure.to_extra_context() без импорта think-модуля
    (meeting_pipeline не должен тащить тяжёлый граф/LLM в тесты).
    """
    if not isinstance(closure, dict) or closure.get("is_empty", True) and not closure.get("summary"):
        return ""
    lines = ["## Предусловия и зависимости (что ещё нужно сделать)"]
    summary = str(closure.get("summary", "") or "")
    if summary:
        lines.append(summary)
    label_map = [
        ("blocked_by", "Блокируется: "),
        ("must_precede", "Должно быть сделано до: "),
        ("missing", "Не хватает: "),
        ("follow_ups", "После — доделать: "),
    ]
    for key, label in label_map:
        items = closure.get(key)
        if isinstance(items, list) and items:
            lines.append(label + "; ".join(str(x) for x in items[:8]))
    return "\n".join(lines) if len(lines) > 1 else ""


def _task_to_description(task: dict[str, Any]) -> str:
    """Свести task-dict (TaskAgent shape) в текстовое описание для анализа."""
    title = (
        task.get("title")
        or task.get("task_title")
        or task.get("name")
        or ""
    ).strip()
    desc = (task.get("description") or "").strip()
    if title and desc:
        return f"{title}. {desc}"
    return title or desc or "Без названия"


# === Основной конвейер ===================================================

async def run_meeting_pipeline(
    *,
    transcript: str,
    meeting_id: str,
    user_id: str = "",
    meeting_context: Optional[dict[str, Any]] = None,
    config: Optional[PipelineConfig] = None,
    preselected_tasks: Optional[list[dict[str, Any]]] = None,
    orchestrator_factory: OrchestratorFactory = _default_orchestrator,
    analyzer_factory: AnalyzerFactory = _default_analyzer,
    tz_generator_factory: TZGeneratorFactory = _default_tz_generator,
    feedback_runner: Optional[Callable[..., Awaitable[Any]]] = None,
    on_tz: Optional[Callable[..., Awaitable[None]]] = None,
    audit: Optional[Callable[..., Awaitable[None]]] = None,
) -> MeetingPipelineResult:
    """Прогнать встречу через конвейер. Никогда не raises.

    Args:
        transcript: текст транскрипта.
        meeting_id: ID встречи.
        user_id: владелец (для TZRequest / audit).
        meeting_context: доп. контекст (проект, участники).
        config: PipelineConfig (max_tasks, auto_execute, task-фильтр, ...).
        preselected_tasks: если задан (ручной выбор по id) — берём ЭТИ задачи
            как источник, без пере-извлечения из транскрипта. Caller грузит их
            из того же хранилища, откуда пользователь выбирал галочками.
        *_factory: инъекция тяжёлых зависимостей (тесты передают фейки).
        feedback_runner: инъекция P0 run_feedback_loop (тесты — фейк).
        on_tz: async-колбэк, вызывается на КАЖДОЕ сгенерированное ТЗ с
            kwargs (task_description, task_type, tz_markdown, record). Через
            него вызывающий доставляет ТЗ в канал и/или ставит его в гейт
            подтверждения — сам pipeline не тащит доставку/гейт (тестируемость,
            markdown не раздувает record).
        audit: инъекция audit_log.emit (тесты — no-op).
    """
    cfg = config or PipelineConfig()
    ctx = meeting_context or {}
    result = MeetingPipelineResult(meeting_id=meeting_id, status="ok")

    async def _audit(action: str, **meta: Any) -> None:
        if audit is None:
            return
        try:
            await audit(
                action=action,
                user_id=user_id or "system",
                resource=f"meeting:{meeting_id}",
                metadata=meta,
            )
        except Exception as exc:  # audit не должен ломать конвейер
            logger.debug("meeting_pipeline: audit failed: %s", exc)

    if not transcript or not transcript.strip():
        result.status = "no_transcript"
        await _audit("meeting_pipeline.skip", reason="no_transcript")
        return result

    # === 1. Источник задач ===
    # Ручной выбор по id: caller уже загрузил ИМЕННО выбранные задачи из
    # хранилища (meeting_tasks) и передал их сюда. Не пере-извлекаем — иначе
    # получим ДРУГОЙ список (свежие формулировки/id), который приходилось
    # нечётко сопоставлять, и одна задача «поглощала» все. Берём как есть.
    preset = [t for t in (preselected_tasks or []) if isinstance(t, dict)]
    if preset:
        tasks = preset
        result.tasks_extracted = len(tasks)
        await _audit("meeting_pipeline.preselected", tasks=len(tasks))
    else:
        try:
            orchestrator = orchestrator_factory()
            capture = await orchestrator.process_meeting(
                transcript=transcript,
                meeting_id=meeting_id,
                meeting_context=ctx,
            )
        except Exception as exc:
            logger.warning("meeting_pipeline: extract failed (%s): %s", meeting_id, exc)
            result.status = "extract_failed"
            result.error = str(exc)
            await _audit("meeting_pipeline.extract_failed", error=str(exc))
            return result

        if isinstance(capture, dict) and capture.get("status") == "error":
            result.status = "extract_failed"
            result.error = str(capture.get("error") or "orchestrator error")
            await _audit("meeting_pipeline.extract_failed", error=result.error)
            return result

        tasks = _extract_task_list(capture)
        result.tasks_extracted = len(tasks)
        await _audit("meeting_pipeline.extracted", tasks=len(tasks))

        # Авто-свежесть снапшотов: экстракция дописала граф → кэш устарел.
        if user_id:
            try:
                from backend.core.sleep.enhanced_snapshot import (
                    invalidate_snapshot_cache,
                )
                invalidate_snapshot_cache(user_id)
            except Exception:
                logger.debug("snapshot invalidate skipped", exc_info=True)

    if not tasks:
        return result

    # Выбор конкретных задач (по заголовкам/исполнителю) до ограничения
    # max_tasks. Для event-триггера критерии пусты → берём как есть.
    tasks = _select_tasks(tasks, cfg)
    if not tasks:
        await _audit("meeting_pipeline.no_tasks_after_filter",
                     task_titles=cfg.task_titles, assignee=cfg.assignee_filter)
        return result

    # === 2-4. По каждой задаче (bounded) ===
    for task in tasks[: cfg.max_tasks]:
        desc = _task_to_description(task)
        rec = TaskPipelineRecord(task_title=desc[:120])
        task_id = str(task.get("id") or task.get("task_id") or f"{meeting_id}:{len(result.records)}")

        # 2. Анализ
        try:
            analyzer = analyzer_factory()
            analysis = await analyzer.analyze(
                task_description=desc,
                task_id=task_id,
                additional_context={"meeting_id": meeting_id, **ctx},
            )
            rec.analyzed = True
            a = analysis.get("analysis", {}) if isinstance(analysis, dict) else {}
            comp = a.get("complexity", {}) if isinstance(a, dict) else {}
            rec.complexity = str(comp.get("level", "")) if isinstance(comp, dict) else ""
            # P2: замыкание зависимостей (что ещё надо сделать, чтобы закрыть)
            closure = (
                analysis.get("dependency_closure", {})
                if isinstance(analysis, dict) else {}
            )
            if isinstance(closure, dict):
                rec.closure_summary = str(closure.get("summary", "") or "")
                rec.closure_extra_context = _closure_to_extra_context(closure)
        except Exception as exc:
            logger.warning("meeting_pipeline: analyze failed: %s", exc)
            rec.error = f"analyze: {exc}"
            result.records.append(rec)
            result.tasks_processed += 1
            continue

        # 3. Генерация ТЗ
        tz_markdown = ""
        try:
            generator = tz_generator_factory()
            extra = (
                f"Источник: встреча {meeting_id}. "
                f"Сложность: {rec.complexity or 'unknown'}."
            )
            if rec.closure_extra_context:
                extra += "\n\n" + rec.closure_extra_context
            tz_req = _TZRequestLike(
                description=desc,
                user_id=user_id,
                extra_context=extra,
            )
            tz = await generator.generate(tz_req)
            tz_markdown = getattr(tz, "markdown", "") or ""
            rec.tz_generated = bool(tz_markdown.strip())
            rec.tz_task_type = getattr(tz, "task_type", "") or ""
            rec.tz_completeness = float(getattr(tz, "completeness_score", 0.0) or 0.0)
            rec.tz_length = len(tz_markdown)
        except Exception as exc:
            logger.warning("meeting_pipeline: tz gen failed: %s", exc)
            rec.error = f"tz: {exc}"
            result.records.append(rec)
            result.tasks_processed += 1
            continue

        # 3.5. Колбэк на готовое ТЗ — доставка в канал и/или постановка в гейт
        # подтверждения. Делает вызывающий (не pipeline), best-effort.
        if on_tz is not None and tz_markdown.strip():
            try:
                await on_tz(
                    task_description=desc,
                    task_type=rec.tz_task_type,
                    tz_markdown=tz_markdown,
                    record=rec,
                )
            except Exception as exc:
                logger.warning("meeting_pipeline: on_tz callback failed: %s", exc)

        # 4. (опц.) Авто-исполнение через P0 петлю.
        # Дневной кап (VIBE_AUTO_DAILY_CAP, дефолт 10) — runaway-защита ДО
        # того, как auto включат «на каждую встречу».
        if cfg.auto_execute and tz_markdown.strip() and not _auto_cap_allow():
            rec.execution_decision = ("skipped: дневной кап авто-исполнений "
                                      "исчерпан (VIBE_AUTO_DAILY_CAP)")
            logger.warning("vibe auto: дневной кап исчерпан — задача "
                           "осталась ТЗ без исполнения")
        elif cfg.auto_execute and tz_markdown.strip():
            try:
                runner = feedback_runner or _default_feedback_runner
                loop_res = await runner(
                    tz_markdown=tz_markdown,
                    task_type=rec.tz_task_type or None,
                    user_id=user_id,
                    backend_override=cfg.executor_backend,
                    max_iterations=cfg.feedback_max_iterations,
                )
                rec.executed = True
                rec.execution_decision = str(
                    getattr(loop_res, "final_decision", "")
                    or (loop_res.get("final_decision") if isinstance(loop_res, dict) else "")
                    or "unknown"
                )
            except Exception as exc:
                logger.warning("meeting_pipeline: auto-exec failed: %s", exc)
                rec.error = f"exec: {exc}"
        else:
            rec.execution_decision = "skipped"

        result.records.append(rec)
        result.tasks_processed += 1
        await _audit(
            "meeting_pipeline.task_processed",
            task=rec.task_title,
            tz_generated=rec.tz_generated,
            executed=rec.executed,
            decision=rec.execution_decision,
        )

    return result


async def _default_feedback_runner(
    *,
    tz_markdown: str,
    task_type: Optional[str],
    user_id: str,
    backend_override: Optional[str],
    max_iterations: int,
) -> Any:
    """Дефолтный мост к P0 run_feedback_loop с реальными зависимостями."""
    from backend.core.executors.base import TaskSubmission
    from backend.core.executors.feedback_loop import run_feedback_loop
    from backend.core.executors.registry import (
        get_backend,
        resolve_backend_name_for_task_type,
    )
    from backend.core.validator.code import CodeValidator
    from backend.core.validator.structural import StructuralValidator

    # Роутинг по типу задачи (лендинг → web-агент, код → CLI, ...): при явном
    # override берём его, иначе EXECUTOR_TASK_ROUTES / активный backend.
    backend_name = resolve_backend_name_for_task_type(
        task_type, override=backend_override)
    backend = get_backend(backend_name)
    submission = TaskSubmission(
        tz_markdown=tz_markdown,
        task_type=task_type,
        metadata={"user_id": user_id, "source": "meeting_pipeline"},
    )
    return await run_feedback_loop(
        submission=submission,
        backend=backend,
        validators=[CodeValidator(), StructuralValidator()],
        max_iterations=max_iterations,
    )


# === EventBus-подписчик ==================================================

async def handle_meeting_ended(event: Any) -> None:
    """EventHandler для `meeting_ended`. Никогда не raises (контракт).

    P1 #7: интегрирован Wave 1 dedup. До запуска тяжёлого pipeline
    проверяем processed_meetings ledger по (org_id, source, external_id)
    или content_hash. Если эпизод уже был обработан (например, тот же
    Zoom-meeting прилетел через другого участника той же org) — skip,
    не валим pipeline для idempotent webhook'ов.
    """
    try:
        payload = getattr(event, "payload", None) or {}
        user_id = getattr(event, "user_id", "") or payload.get("user_id", "")
        meeting_id = str(
            payload.get("meeting_id")
            or payload.get("id")
            or getattr(event, "id", "")
            or "unknown"
        )
        transcript = (
            payload.get("transcript")
            or payload.get("transcript_text")
            or payload.get("text")
            or ""
        )
        meeting_context = payload.get("meeting_context") or payload.get("context") or {}
        cfg = _coerce_config(payload)

        from backend.core.observability import audit_log

        # --- Wave 1 dedup (P1 #7): защита webhook flow от дублей --------
        # Этот flow обходит scheduled knowledge_sync — без проверки здесь
        # один Zoom-meeting от двух участников создаёт два прогона
        # pipeline'а и два набора Decision/Task в графе.
        dedup_skip_reason: Optional[str] = None
        try:
            from backend.core.ingest import dedup as _dedup
            from backend.core.ingest.membership import get_org_for_user
            from backend.core.ingest import meeting_upsert as _upsert
            from backend.db.supabase_client import get_supabase_client

            # build_dedup_key умеет извлекать source/external_id из любых
            # типичных названий полей (zoom_meeting_id, gcal_event_id, ...).
            key = _dedup.build_dedup_key(payload, transcript or "")
            org_id = get_org_for_user(user_id) if user_id else None
            if key["external_id"] or key["content_hash"]:
                sb = get_supabase_client()
                existing = await _upsert.find_completed_episode(
                    sb,
                    org_id=org_id,
                    source=key["source"],
                    external_id=key["external_id"],
                    content_hash=key["content_hash"],
                )
                if existing:
                    dedup_skip_reason = (
                        f"already processed as meeting_id={existing.get('meeting_id')}"
                    )
        except Exception as dedup_exc:
            # Dedup best-effort: если Supabase недоступен — продолжаем
            # processing (как было до P1 #7), просто лог warning.
            logger.debug("webhook dedup check failed (continuing): %s", dedup_exc)

        if dedup_skip_reason:
            logger.info(
                "meeting_pipeline skip (Wave 1 dedup): meeting=%s reason=%s",
                meeting_id, dedup_skip_reason,
            )
            return
        # ----------------------------------------------------------------

        res = await run_meeting_pipeline(
            transcript=transcript,
            meeting_id=meeting_id,
            user_id=user_id,
            meeting_context=meeting_context if isinstance(meeting_context, dict) else {},
            config=cfg,
            audit=audit_log.emit,
        )
        logger.info(
            "meeting_pipeline done: meeting=%s status=%s tasks=%d/%d",
            meeting_id, res.status, res.tasks_processed, res.tasks_extracted,
        )

        # --- Mark в ledger после успешного pipeline ---------------------
        # Это закрывает loop: следующий webhook с тем же external_id
        # увидит запись и пропустит.
        try:
            from backend.core.ingest import dedup as _dedup
            from backend.core.ingest.membership import get_org_for_user
            from backend.core.ingest import meeting_upsert as _upsert
            from backend.db.supabase_client import get_supabase_client

            key = _dedup.build_dedup_key(payload, transcript or "")
            org_id = get_org_for_user(user_id) if user_id else None
            if user_id and meeting_id != "unknown":
                sb = get_supabase_client()
                await _upsert.mark_completion(
                    sb,
                    user_id=user_id, meeting_id=meeting_id,
                    org_id=org_id,
                    source=key["source"],
                    external_id=key["external_id"],
                    content_hash=key["content_hash"],
                    subscription_id=None,  # webhook не имеет subscription
                    status=_upsert.STATUS_COMPLETED,
                )
        except Exception as mark_exc:
            logger.debug("webhook ledger mark failed (best-effort): %s", mark_exc)
    except Exception as exc:  # абсолютная гарантия: handler не валит bus
        logger.error("meeting_pipeline handler crashed: %s", exc)


# === Идемпотентная регистрация ==========================================

_REGISTERED = False


def register_meeting_pipeline(bus: Any = None) -> bool:
    """Подписать handle_meeting_ended на `meeting_ended`. Идемпотентно.

    Returns True если зарегистрировали (False если уже было).
    Безопасно вызывать многократно (напр. из webhook-роута лениво).
    """
    global _REGISTERED
    if _REGISTERED:
        return False
    try:
        if bus is None:
            from backend.core.automations.event_bus import get_event_bus
            bus = get_event_bus()
        from backend.core.automations.event_bus import EventType
        bus.subscribe(EventType.MEETING_ENDED, handle_meeting_ended)
        _REGISTERED = True
        logger.info("meeting_pipeline: subscribed to meeting_ended")
        return True
    except Exception as exc:
        logger.error("meeting_pipeline: registration failed: %s", exc)
        return False


def _reset_registration_for_tests() -> None:
    global _REGISTERED
    _REGISTERED = False


__all__ = [
    "MeetingPipelineResult",
    "PipelineConfig",
    "TaskPipelineRecord",
    "handle_meeting_ended",
    "register_meeting_pipeline",
    "run_meeting_pipeline",
]

# ── Дневной кап авто-исполнений (runaway-защита режима auto) ────────────

def _auto_cap_allow() -> bool:
    """True, если сегодняшний лимит авто-исполнений ещё не исчерпан.
    Счётчик — файл data/handoffs/auto_counter.json {date, count};
    инкремент при разрешении. Never-raise (сбой учёта → разрешаем)."""
    import json as _json
    import os as _os
    from datetime import datetime, timezone
    from pathlib import Path
    try:
        cap = int(_os.getenv("VIBE_AUTO_DAILY_CAP", "10"))
    except ValueError:
        cap = 10
    if cap <= 0:
        return True          # 0/отрицательный = без лимита (осознанно)
    try:
        p = Path("data") / "handoffs" / "auto_counter.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            st = _json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            st = {}
        if st.get("date") != today:
            st = {"date": today, "count": 0}
        if st["count"] >= cap:
            return False
        st["count"] += 1
        p.write_text(_json.dumps(st), encoding="utf-8")
        return True
    except Exception:
        logger.debug("auto cap check failed — allow", exc_info=True)
        return True

