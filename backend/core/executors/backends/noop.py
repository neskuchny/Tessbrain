"""Noop executor — для тестов, dry-run, demo (W19).

Вместо реального исполнителя — сразу записывает «task done» с echo-style
артефактом «вот ваше ТЗ, типа я бы его выполнил».

Полезен для:
- юнит-тестов adapter API
- dev/preview когда не хочется тратить токены
- smoke-test продакшн-окружения
"""
from __future__ import annotations

from backend.core.executors.base import (
    ExecutorBackend,
    TaskHandle,
    TaskResult,
    TaskStatus,
    TaskSubmission,
)
from backend.core.executors.store import save_handle, save_result


class NoopExecutor(ExecutorBackend):
    name = "noop"

    async def submit(self, submission: TaskSubmission) -> TaskHandle:
        handle = TaskHandle.new(
            backend=self.name,
            user_id=submission.metadata.get("user_id"),
            tenant_id=submission.metadata.get("tenant_id"),
            working_dir=submission.working_dir,
            task_type=submission.task_type,
        )
        # Сразу же помечаем done, сохраняем result с echo.
        handle.status = TaskStatus.DONE
        await save_handle(handle)
        result = TaskResult(
            handle_id=handle.id,
            status=TaskStatus.DONE,
            success=True,
            summary=f"noop executor accepted task ({len(submission.tz_markdown)} chars)",
            artifacts=[{
                "name": "echo_tz.md",
                "kind": "text",
                "content": submission.tz_markdown,
            }],
            logs_excerpt="noop: no real work performed",
            raw={"task_type": submission.task_type},
        )
        await save_result(result)
        return handle

    async def get_status(self, handle: TaskHandle) -> TaskStatus:
        return handle.status if handle.status != TaskStatus.QUEUED else TaskStatus.DONE

    async def get_result(self, handle: TaskHandle) -> TaskResult | None:
        from backend.core.executors.store import get_result
        raw = await get_result(handle.id)
        if not raw:
            return None
        return TaskResult(
            handle_id=raw["handle_id"],
            status=TaskStatus(raw["status"]),
            success=bool(raw["success"]),
            finished_at=raw.get("finished_at", ""),
            summary=raw.get("summary", ""),
            artifacts=list(raw.get("artifacts") or []),
            logs_excerpt=raw.get("logs_excerpt", ""),
            error_message=raw.get("error_message"),
        )

    async def cancel(self, handle: TaskHandle) -> bool:
        return True


__all__ = ["NoopExecutor"]
