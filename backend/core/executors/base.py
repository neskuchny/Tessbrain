"""Base abstraction для executor backends (W19).

Контракт един для всех backends:
- `submit(submission)` → TaskHandle (с unique id и backend-specific meta)
- `get_status(handle)` → TaskStatus (queued/running/done/failed/cancelled)
- `get_result(handle)` → TaskResult (artifacts + logs + success)
- `cancel(handle)` → bool

Backends:
- Noop — для тестов и preview
- ClaudeCodeCLI — subprocess к локально установленному `claude` CLI
- CursorCLI — subprocess к `cursor-agent` (где есть)
- OpenHands — HTTP к internal OpenHands runtime (W20)
- Goose — HTTP к Goose runtime (опционально)

Дизайн:
- async-first
- никаких raises на «нормальных» ошибках исполнителя — всё попадает в
  `TaskResult.success=False` + `error_message`
- ExecutorError — только для кейсов где adapter сам сломался
  (нет CLI бинарника, недоступен endpoint)
"""
from __future__ import annotations

import abc
import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


class TaskStatus(str, enum.Enum):
    """Lifecycle статусы task'а."""
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ExecutorError(RuntimeError):
    """Сам adapter сломан (нет CLI / недоступный endpoint / config issue)."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_handle_id() -> str:
    return f"task_{uuid.uuid4().hex[:16]}"


@dataclass
class TaskSubmission:
    """Что отправляем executor'у. Минимум — ТЗ в Markdown.

    `task_type` (опц) — landing/api/... — backend может использовать как
    подсказку. `working_dir` — где ему развернуть workspace (для CLI).
    `metadata` — произвольный словарь, передаётся как есть в executor logs.
    """
    tz_markdown: str
    task_type: Optional[str] = None
    working_dir: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    callback_url: Optional[str] = None      # webhook куда executor вернёт результат
    timeout_seconds: int = 3600              # дефолт 1ч на задачу

    def to_dict(self) -> dict[str, Any]:
        return {
            "tz_markdown_length": len(self.tz_markdown),
            "task_type": self.task_type,
            "working_dir": self.working_dir,
            "metadata": dict(self.metadata),
            "callback_url": self.callback_url,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass
class TaskHandle:
    """Handle на запущенную задачу. Сериализуется в БД/Redis."""
    id: str
    backend: str                       # имя backend'а (noop/claude_code_cli/...)
    submitted_at: str                  # ISO
    status: TaskStatus = TaskStatus.QUEUED
    backend_ref: Optional[str] = None  # process_id / external job id / ...
    user_id: Optional[str] = None
    tenant_id: Optional[str] = None
    # working_dir фиксируем чтобы потом collect артефакты
    working_dir: Optional[str] = None
    # task_type сохраняем для фильтрации/листинга
    task_type: Optional[str] = None

    @classmethod
    def new(
        cls,
        *,
        backend: str,
        user_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        working_dir: Optional[str] = None,
        task_type: Optional[str] = None,
    ) -> "TaskHandle":
        return cls(
            id=_new_handle_id(),
            backend=backend,
            submitted_at=_utc_now_iso(),
            status=TaskStatus.QUEUED,
            user_id=user_id,
            tenant_id=tenant_id,
            working_dir=working_dir,
            task_type=task_type,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "backend": self.backend,
            "submitted_at": self.submitted_at,
            "status": self.status.value,
            "backend_ref": self.backend_ref,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "working_dir": self.working_dir,
            "task_type": self.task_type,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskHandle":
        status = d.get("status", TaskStatus.UNKNOWN.value)
        try:
            status_enum = TaskStatus(status)
        except ValueError:
            status_enum = TaskStatus.UNKNOWN
        return cls(
            id=str(d["id"]),
            backend=str(d["backend"]),
            submitted_at=str(d.get("submitted_at") or _utc_now_iso()),
            status=status_enum,
            backend_ref=d.get("backend_ref"),
            user_id=d.get("user_id"),
            tenant_id=d.get("tenant_id"),
            working_dir=d.get("working_dir"),
            task_type=d.get("task_type"),
        )


@dataclass
class TaskResult:
    """Финальный результат задачи."""
    handle_id: str
    status: TaskStatus                  # должен быть DONE/FAILED/CANCELLED
    success: bool
    finished_at: str = field(default_factory=_utc_now_iso)
    summary: str = ""                   # короткое описание что сделал executor
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    # Каждый artifact: {"name": "...", "path"|"url"|"content": ..., "kind": "file|url|text"}
    logs_excerpt: str = ""              # последние ~1000 символов output
    error_message: Optional[str] = None # если success=False
    raw: dict[str, Any] = field(default_factory=dict)  # backend-specific output

    def to_dict(self) -> dict[str, Any]:
        return {
            "handle_id": self.handle_id,
            "status": self.status.value,
            "success": self.success,
            "finished_at": self.finished_at,
            "summary": self.summary,
            "artifacts": list(self.artifacts),
            "logs_excerpt": self.logs_excerpt,
            "error_message": self.error_message,
        }


class ExecutorBackend(abc.ABC):
    """Абстрактный интерфейс executor backend'а."""

    name: str = "abstract"

    @abc.abstractmethod
    async def submit(self, submission: TaskSubmission) -> TaskHandle:
        """Отправить задачу. Возвращает handle, обычно сразу."""

    @abc.abstractmethod
    async def get_status(self, handle: TaskHandle) -> TaskStatus:
        """Запросить текущий статус."""

    @abc.abstractmethod
    async def get_result(self, handle: TaskHandle) -> Optional[TaskResult]:
        """Вернуть результат (если задача завершена). None если ещё running."""

    async def cancel(self, handle: TaskHandle) -> bool:
        """Отменить задачу. По умолчанию — not implemented (backend override)."""
        return False


__all__ = [
    "ExecutorBackend",
    "ExecutorError",
    "TaskHandle",
    "TaskResult",
    "TaskStatus",
    "TaskSubmission",
]
