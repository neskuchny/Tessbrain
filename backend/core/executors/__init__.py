"""Executor adapter API (W19).

Пишет TZ → отдаёт исполнителю → собирает результат.
Backends: noop / claude_code_cli / cursor_cli / openhands.
"""
from backend.core.executors.base import (
    ExecutorBackend,
    ExecutorError,
    TaskHandle,
    TaskResult,
    TaskStatus,
    TaskSubmission,
)

__all__ = [
    "ExecutorBackend",
    "ExecutorError",
    "TaskHandle",
    "TaskResult",
    "TaskStatus",
    "TaskSubmission",
]
