"""Cursor CLI executor (W19).

Cursor поставляет `cursor-agent` CLI (background-mode). Тот же subprocess
pattern что у Claude Code, только другой бинарь.

Конфигурация:
- `CURSOR_CLI_PATH` env (default: `cursor-agent`)
- `CURSOR_API_KEY` env — пробрасывается

В enterprise mode НЕ должен быть включён (Cursor managed).
"""
from __future__ import annotations

import os
from typing import Optional

from backend.core.executors.backends.claude_code import ClaudeCodeCLIExecutor

_DEFAULT_CLI = "cursor-agent"


class CursorCLIExecutor(ClaudeCodeCLIExecutor):
    """Subprocess-исполнитель для Cursor.

    Наследуем от Claude Code — единственная разница в:
    - имени backend
    - дефолте CLI path
    - command-line flags
    """
    name = "cursor_cli"

    def __init__(self, *, cli_path: Optional[str] = None) -> None:
        super().__init__(
            cli_path=cli_path or os.environ.get("CURSOR_CLI_PATH", _DEFAULT_CLI),
        )

    def cli_flags(self) -> list[str]:
        # cursor-agent понимает -p (print), но НЕ понимает
        # --output-format json (это флаг claude) — прогон падал на старте.
        return ["-p"]


__all__ = ["CursorCLIExecutor"]
