"""Codex CLI executor (Phase B.3).

OpenAI Codex CLI как ExecutorBackend. Бо́льшая часть логики переиспользует
ClaudeCodeCLIExecutor — мы только переопределяем cli_path resolver и
команду запуска (codex использует другую сигнатуру).

Конфигурация:
- `CODEX_CLI_PATH` env (default: `codex`) — путь к бинарю
- `CODEX_WORKING_ROOT` env (default: same as Claude) — где working_dirs
- `OPENAI_API_KEY` env — или subscription auth через `codex login`
- Codex CLI поддерживает `codex exec <prompt>` для headless mode
- Output format: `--output-format json` для парсинга artifacts

Note: Codex CLI продолжает развиваться, флаги могут меняться. Проверь
актуальные через `codex exec --help` если возникают проблемы.

Регистрация: backend/core/executors/registry.py добавлен 'codex_cli' в
_KNOWN_BACKENDS + ветка в get_backend().
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import Optional

from backend.core.executors.backends.claude_code import (
    ClaudeCodeCLIExecutor,
    _collect_artifacts,
    _read_log_excerpt,
)
from backend.core.executors.base import (
    ExecutorError,
    TaskHandle,
    TaskResult,
    TaskStatus,
)

logger = logging.getLogger(__name__)


def _resolve_codex_cli_path() -> str:
    """Возвращает путь к codex CLI. Default 'codex' резолвится через PATH."""
    return os.getenv("CODEX_CLI_PATH", "codex").strip() or "codex"


class CodexCLIExecutor(ClaudeCodeCLIExecutor):
    """Codex executor — наследует от ClaudeCodeCLIExecutor.

    Переопределяет только две вещи:
    1. _resolve_cli_path → CODEX_CLI_PATH вместо CLAUDE_CODE_CLI_PATH
    2. _spawn_and_wait → команда `codex exec ...` вместо `claude -p ...`
    """

    name = "codex_cli"

    def __init__(self, *, cli_path: Optional[str] = None) -> None:
        # НЕ зовём super().__init__() с тем cli_path, потому что Claude
        # резолвит свой; мы хотим CODEX_CLI_PATH.
        self.cli_path = cli_path or _resolve_codex_cli_path()

    def _ensure_cli(self) -> None:
        if shutil.which(self.cli_path) is None:
            raise ExecutorError(
                f"Codex CLI not found: {self.cli_path!r}. "
                "Set CODEX_CLI_PATH env or install `codex` "
                "(see docs/CLI_BRIDGE_SETUP.md)."
            )

    async def _spawn_and_wait(
        self,
        *,
        handle: TaskHandle,
        tz_markdown: str,
        working_dir: Path,
        log_path: Path,
        timeout: int,
    ) -> TaskResult:
        """Запустить `codex exec` subprocess и собрать результат.

        Отличия от Claude:
        - команда: `codex exec` вместо `claude -p`
        - аргумент prompt через stdin (--input - / pipe)
        - output format text (codex JSON format может различаться,
          в Phase B.3.1 уточним под актуальную версию)
        """
        env = os.environ.copy()
        cmd = [self.cli_path, "exec"]
        # TODO Phase B.3.1: добавить --output-format json когда стабилизируется
        # contract codex CLI. Сейчас работаем с plain text output.

        with log_path.open("wb") as log_file:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=log_file,
                cwd=str(working_dir),
                env=env,
            )
            try:
                stdout_b, _ = await asyncio.wait_for(
                    proc.communicate(input=tz_markdown.encode("utf-8")),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                raise

        stdout_text = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
        rc = proc.returncode or 0
        # stdout пишем в result-файл, чтобы он попал в artifacts (как у Claude).
        if stdout_text.strip():
            try:
                (working_dir / "codex_result.txt").write_text(
                    stdout_text, encoding="utf-8")
            except Exception as exc:
                logger.debug("CodexCLI: result write failed: %s", exc)

        if rc != 0:
            # TaskResult НЕ имеет поля output_text — хвост stdout идёт в raw.
            return TaskResult(
                handle_id=handle.id,
                status=TaskStatus.FAILED,
                success=False,
                summary=f"codex exec returned {rc}",
                error_message=f"codex exec returned non-zero: {rc}",
                logs_excerpt=_read_log_excerpt(log_path),
                raw={"return_code": rc, "stdout_tail": stdout_text[-2000:]},
            )

        # Artifacts: всё что codex создал в working_dir (минус log/TZ) —
        # единый хелпер отдаёт list[dict] нужной формы.
        artifacts = _collect_artifacts(
            working_dir, exclude={".codex.log", ".claude.log", "TZ.md"})

        return TaskResult(
            handle_id=handle.id,
            status=TaskStatus.DONE,
            success=True,
            summary=f"codex exec completed; {len(artifacts)} artifacts",
            artifacts=artifacts,
            logs_excerpt=_read_log_excerpt(log_path),
            raw={"return_code": rc, "stdout_length": len(stdout_text)},
        )


__all__ = ["CodexCLIExecutor"]
