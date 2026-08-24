"""Concrete executor backends (W19)."""
from backend.core.executors.backends.claude_code import ClaudeCodeCLIExecutor
from backend.core.executors.backends.cursor import CursorCLIExecutor
from backend.core.executors.backends.noop import NoopExecutor
from backend.core.executors.backends.openhands import OpenHandsExecutor

__all__ = [
    "ClaudeCodeCLIExecutor",
    "CursorCLIExecutor",
    "NoopExecutor",
    "OpenHandsExecutor",
]
