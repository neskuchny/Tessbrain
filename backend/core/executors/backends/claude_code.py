"""Claude Code CLI executor (W19).

Запускает `claude` CLI в headless-mode (`-p` / `--print`), скармливает ТЗ
как input, забирает stdout + созданные файлы.

Дизайн:
- Subprocess в asyncio с timeout. Лог пишется в `working_dir/.claude.log`.
- ТЗ → передаётся через stdin (claude умеет `--print` с piped input).
- working_dir создаётся уникальным per-task (`/tmp/tessent-task-{id}/`).
- artifacts собираются как список файлов в working_dir после
  завершения (минус .claude.log).
- НЕ поддерживаем streaming — для async-пользы caller сам polls
  `get_status` / получает webhook.
- При timeout помечаем status=FAILED, error_message="timeout".

Конфигурация:
- `CLAUDE_CODE_CLI_PATH` env (default: `claude`) — путь к бинарю
- `CLAUDE_CODE_WORKING_ROOT` env (default: `/tmp`) — где создавать
  working_dirs
- `ANTHROPIC_API_KEY` env — пробрасывается в subprocess

В enterprise mode этот backend НЕ должен быть включён (validator в
registry это проверит — Claude API внешний).
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import Optional

from backend.core.executors.base import (
    ExecutorBackend,
    ExecutorError,
    TaskHandle,
    TaskResult,
    TaskStatus,
    TaskSubmission,
)
from backend.core.executors.store import (
    get_handle,
    save_handle,
    save_result,
    update_status,
)

logger = logging.getLogger(__name__)


_DEFAULT_CLI = "claude"
_DEFAULT_WORKING_ROOT = "/tmp"
_LOG_FILENAME = ".claude.log"
_RESULT_FILENAME = ".claude.result.json"
_MAX_LOG_EXCERPT = 4000


def _resolve_cli_path() -> str:
    return os.environ.get("CLAUDE_CODE_CLI_PATH", _DEFAULT_CLI).strip() or _DEFAULT_CLI


def _resolve_working_root() -> Path:
    root = os.environ.get("CLAUDE_CODE_WORKING_ROOT", _DEFAULT_WORKING_ROOT)
    return Path(root).expanduser()


def _build_working_dir(handle_id: str, override: Optional[str]) -> Path:
    if override:
        return Path(override).expanduser()
    return _resolve_working_root() / f"tessent-task-{handle_id}"


def _collect_artifacts(working_dir: Path, exclude: set[str]) -> list[dict[str, str]]:
    """Список файлов в working_dir, отсортированный по имени."""
    out: list[dict[str, str]] = []
    if not working_dir.exists():
        return out
    for path in sorted(working_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(working_dir))
        if rel in exclude or rel.startswith("."):
            continue
        out.append({
            "name": rel,
            "path": str(path),
            "kind": "file",
        })
    return out


def _read_log_excerpt(log_path: Path) -> str:
    if not log_path.exists():
        return ""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"[log read failed: {exc}]"
    if len(text) <= _MAX_LOG_EXCERPT:
        return text
    return "...\n" + text[-_MAX_LOG_EXCERPT:]


class ClaudeCodeCLIExecutor(ExecutorBackend):
    name = "claude_code_cli"

    def __init__(self, *, cli_path: Optional[str] = None) -> None:
        self.cli_path = cli_path or _resolve_cli_path()

    def _ensure_cli(self) -> None:
        if shutil.which(self.cli_path) is None:
            raise ExecutorError(
                f"Claude CLI not found: {self.cli_path!r}. "
                "Set CLAUDE_CODE_CLI_PATH env or install `claude`."
            )

    def cli_flags(self) -> list[str]:
        """CLI-флаги headless-режима. Наследники (cursor) переопределяют:
        у cursor-agent нет --output-format json (claude-специфика)."""
        return ["-p", "--output-format", "json"]

    async def submit(self, submission: TaskSubmission) -> TaskHandle:
        self._ensure_cli()

        handle = TaskHandle.new(
            backend=self.name,
            user_id=submission.metadata.get("user_id"),
            tenant_id=submission.metadata.get("tenant_id"),
            task_type=submission.task_type,
        )

        working_dir = _build_working_dir(handle.id, submission.working_dir)
        working_dir.mkdir(parents=True, exist_ok=True)
        handle.working_dir = str(working_dir)

        # Сохраняем ТЗ в файл — пригодится для retries и для логов.
        tz_path = working_dir / "TZ.md"
        tz_path.write_text(submission.tz_markdown, encoding="utf-8")

        await save_handle(handle)

        # Запускаем subprocess в фоне.
        # `claude -p "<prompt>"` — print mode, читает prompt и завершается.
        # Если TZ длинное — лучше передать через stdin.
        log_path = working_dir / _LOG_FILENAME
        # Не блокируем submit — schedule task и возвращаем handle.
        asyncio.create_task(self._run(
            handle=handle,
            tz_markdown=submission.tz_markdown,
            working_dir=working_dir,
            log_path=log_path,
            timeout=submission.timeout_seconds,
            callback_url=submission.callback_url,
        ))

        return handle

    async def _run(
        self,
        *,
        handle: TaskHandle,
        tz_markdown: str,
        working_dir: Path,
        log_path: Path,
        timeout: int,
        callback_url: Optional[str],
    ) -> None:
        """Outer-task: запускает subprocess, ждёт finish, сохраняет результат."""
        await update_status(handle.id, TaskStatus.RUNNING)
        result: TaskResult
        try:
            result = await self._spawn_and_wait(
                handle=handle,
                tz_markdown=tz_markdown,
                working_dir=working_dir,
                log_path=log_path,
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            result = TaskResult(
                handle_id=handle.id,
                status=TaskStatus.FAILED,
                success=False,
                summary="claude code CLI exceeded timeout",
                error_message=f"timeout after {timeout}s",
                logs_excerpt=_read_log_excerpt(log_path),
            )
        except Exception as exc:
            logger.exception("ClaudeCodeCLI: subprocess failed")
            result = TaskResult(
                handle_id=handle.id,
                status=TaskStatus.FAILED,
                success=False,
                summary="claude code CLI errored",
                error_message=str(exc),
                logs_excerpt=_read_log_excerpt(log_path),
            )

        await save_result(result)
        await update_status(handle.id, result.status)

        if callback_url:
            await _post_callback(callback_url, handle.id, result)

    async def _spawn_and_wait(
        self,
        *,
        handle: TaskHandle,
        tz_markdown: str,
        working_dir: Path,
        log_path: Path,
        timeout: int,
    ) -> TaskResult:
        """Запустить `claude` subprocess и собрать результат."""
        env = os.environ.copy()
        # API-key явно пробрасываем (если есть в parent env, не теряется).
        cmd = [self.cli_path, *self.cli_flags()]

        with log_path.open("wb") as log_file:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=log_file,
                cwd=str(working_dir),
                env=env,
            )
            handle.backend_ref = str(proc.pid)
            await save_handle(handle)

            try:
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(input=tz_markdown.encode("utf-8")),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                with _suppress():
                    proc.kill()
                raise

        return_code = proc.returncode if proc.returncode is not None else -1
        stdout_text = (stdout or b"").decode("utf-8", errors="replace")

        # Save stdout как result-файл.
        if stdout_text.strip():
            (working_dir / _RESULT_FILENAME).write_text(stdout_text, encoding="utf-8")

        artifacts = _collect_artifacts(
            working_dir,
            exclude={_LOG_FILENAME, "TZ.md"},
        )

        success = return_code == 0
        status = TaskStatus.DONE if success else TaskStatus.FAILED

        return TaskResult(
            handle_id=handle.id,
            status=status,
            success=success,
            summary=(
                f"claude CLI finished rc={return_code}, {len(artifacts)} artifacts"
            ),
            artifacts=artifacts,
            logs_excerpt=_read_log_excerpt(log_path),
            error_message=None if success else f"non-zero return code: {return_code}",
            raw={"return_code": return_code, "stdout_length": len(stdout_text)},
        )

    async def get_status(self, handle: TaskHandle) -> TaskStatus:
        # Status обновляется фоновой task'ой; читаем из store.
        fresh = await get_handle(handle.id)
        return fresh.status if fresh else handle.status

    async def get_result(self, handle: TaskHandle) -> Optional[TaskResult]:
        from backend.core.executors.store import get_result as load_result
        raw = await load_result(handle.id)
        if not raw:
            return None
        return TaskResult(
            handle_id=raw["handle_id"],
            status=TaskStatus(raw.get("status", TaskStatus.UNKNOWN.value)),
            success=bool(raw.get("success", False)),
            finished_at=raw.get("finished_at", ""),
            summary=raw.get("summary", ""),
            artifacts=list(raw.get("artifacts") or []),
            logs_excerpt=raw.get("logs_excerpt", ""),
            error_message=raw.get("error_message"),
        )

    async def cancel(self, handle: TaskHandle) -> bool:
        if not handle.backend_ref:
            return False
        try:
            pid = int(handle.backend_ref)
        except (TypeError, ValueError):
            return False
        with _suppress():
            os.kill(pid, 15)  # SIGTERM
        await update_status(handle.id, TaskStatus.CANCELLED)
        return True


# === helpers =============================================================

class _suppress:
    """Контекст-менеджер чтобы проглатывать ошибки cleanup-операций."""
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc, tb):
        return True  # suppress all


async def _post_callback(url: str, handle_id: str, result: TaskResult) -> None:
    """Best-effort POST на callback_url с результатом."""
    try:
        import httpx
    except ImportError:
        logger.debug("post_callback: httpx not installed")
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(url, json={
                "handle_id": handle_id,
                "result": result.to_dict(),
            })
    except Exception as exc:
        logger.warning("post_callback failed: %s", exc)


__all__ = ["ClaudeCodeCLIExecutor"]
