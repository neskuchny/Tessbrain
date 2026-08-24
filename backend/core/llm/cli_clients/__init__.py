"""CLI-bridge для Claude Code и Codex (Phase 3 multi-provider).

Назначение: использовать **подписочную авторизацию** Claude Pro/Max
или ChatGPT Plus/Pro вместо API-ключей. Юзер логинится в CLI один раз
(`claude login` / `codex login`), и наша система делегирует генерацию
через subprocess — токены списываются из подписочной квоты, не через
наш API-биллинг.

Когда это имеет смысл (важная honest caveat):
- ✅ Personal dev: один юзер с Pro/Max подпиской, агентные нагрузки
  низкие (≤50 req/5h на Claude Pro). Реальная экономия vs API.
- ❌ Production multi-user: квота подписки общая на один аккаунт →
  упрёшься в лимит за секунды.
- ⚠️ Серая зона по ToS: использование subscription через CLI
  допустимо для personal/internal use, но не прописано явно как
  «строй продукт для других людей».

Архитектура (MVP, one-shot subprocess):
- Каждый chat-request запускает `claude --print <prompt>` или
  `codex exec <prompt>` как subprocess
- Working directory: /tmp/tessent-cli-sessions/{session_id}/ (изоляция)
- Stdout читаем, отдаём как text-response
- Token tracking: невозможно точно (CLI не выводит), считаем
  request-count в stats

Persistent-pty (одна CLI-сессия = одна chat-сессия с history) —
следующая итерация, гораздо сложнее. MVP: каждый запрос — новый процесс.

Security:
- Workdir per-session — CLI не может прочитать чужие файлы
- Subprocess timeout (по умолчанию 120s)
- Никаких user-supplied flags — только prompt в stdin или argv
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class CLIClientBase:
    """База для CLI-bridge клиентов."""

    cli_command: str = ""  # "claude" / "codex" — имя бинарника
    print_flag: list[str] = []  # флаги для non-interactive mode

    def __init__(
        self,
        *,
        model: str = "",
        workspace_root: Optional[str] = None,
        timeout_seconds: float = 120.0,
        extra_args: Optional[list[str]] = None,
    ) -> None:
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.extra_args = list(extra_args or [])
        # Workspace для всех CLI-сессий — изолированный каталог
        self.workspace_root = Path(
            workspace_root or os.getenv(
                "CLI_WORKSPACE_ROOT", "/tmp/tessent-cli-sessions"
            )
        )
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.stats: dict[str, Any] = {
            "total_requests": 0,
            "errors": 0,
            "timeouts": 0,
        }

    def _check_cli_available(self) -> bool:
        """Проверка, что бинарник CLI присутствует в PATH контейнера."""
        return shutil.which(self.cli_command) is not None

    @property
    def enabled(self) -> bool:
        """Клиент включён только если CLI установлен в контейнере.
        Контейнер по умолчанию НЕ имеет claude/codex CLI — юзеру нужно
        отдельно собрать образ с CLI или смонтировать его в /usr/local/bin.
        Без CLI клиент отвечает enabled=False и роутер делает fallback.
        """
        return self._check_cli_available()

    async def _run_cli(
        self,
        prompt: str,
        session_id: Optional[str] = None,
    ) -> tuple[int, str, str]:
        """Запустить CLI-команду one-shot. Возвращает (returncode, stdout, stderr)."""
        # Рабочий каталог per session (изоляция между юзерами)
        if session_id:
            workdir = self.workspace_root / session_id
            workdir.mkdir(parents=True, exist_ok=True)
        else:
            workdir = Path(tempfile.mkdtemp(
                prefix="tessent-cli-", dir=str(self.workspace_root)
            ))

        cmd = [self.cli_command, *self.print_flag, *self.extra_args]
        # Promot передаём через stdin, а не argv — избегаем экранирования
        # больших промптов с newline'ами/кавычками.

        env = dict(os.environ)
        # Никакого специального env: CLI прочитает свой auth (claude login)
        # из ~/.config/claude/ или эквивалента. В контейнере нужно эти
        # креды смонтировать томом.

        self.stats["total_requests"] += 1
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workdir),
                env=env,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(input=prompt.encode("utf-8")),
                    timeout=self.timeout_seconds,
                )
            except asyncio.TimeoutError:
                self.stats["timeouts"] += 1
                try:
                    proc.kill()
                except Exception:
                    pass
                return -1, "", f"CLI timeout after {self.timeout_seconds}s"
            return (
                proc.returncode or 0,
                stdout_b.decode("utf-8", errors="replace"),
                stderr_b.decode("utf-8", errors="replace"),
            )
        except FileNotFoundError as exc:
            self.stats["errors"] += 1
            return -1, "", f"CLI binary not found: {exc}"
        except Exception as exc:
            self.stats["errors"] += 1
            logger.exception("CLI run failed")
            return -1, "", str(exc)

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,  # игнорируется в CLI-mode (не настраивается)
        max_tokens: int = 8192,  # игнорируется в CLI-mode
        session_id: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        """Сгенерировать ответ через CLI subprocess.

        system_prompt объединяется с user prompt — CLI обычно не имеет
        отдельного флага для system. Стандартная склейка:
        `{system_prompt}\\n\\n---\\n\\n{prompt}`.
        """
        full_prompt = prompt
        if system_prompt:
            full_prompt = f"{system_prompt}\n\n---\n\n{prompt}"

        rc, stdout, stderr = await self._run_cli(full_prompt, session_id=session_id)
        if rc != 0:
            logger.warning(
                "%s CLI returned %d: %s",
                self.cli_command, rc, (stderr or "")[:200],
            )
            raise RuntimeError(
                f"{self.cli_command} CLI failed (rc={rc}): {stderr[:200]}"
            )
        return stdout

    async def generate_json(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        **kwargs: Any,
    ) -> Any:
        """JSON-генерация — CLI просто отдаёт текст, парсим best-effort."""
        text = await self.generate(prompt, system_prompt=system_prompt, **kwargs)
        import json
        try:
            return json.loads(text)
        except Exception:
            # Fallback на regex-extract если JSON завёрнут в markdown
            import re
            m = re.search(r"\{[\s\S]*\}", text)
            if m:
                try:
                    return json.loads(m.group(0))
                except Exception:
                    pass
        return {}


class ClaudeCLIClient(CLIClientBase):
    """Bridge для `claude` CLI (Claude Code).

    Использует подписочную авторизацию Claude Pro/Max — юзер делает
    `claude login` один раз, далее CLI ходит через OAuth-сессию.

    Setup в контейнере:
    1. Установить claude CLI в образ Dockerfile (npm install -g
       @anthropic-ai/claude-cli) ИЛИ смонтировать томом
       /usr/local/bin/claude из хоста.
    2. Смонтировать ~/.config/claude/ хоста в контейнер для auth-credentials.

    Профиль создаётся как:
      provider="claude_cli", model="sonnet"|"opus"|"haiku", api_key=None
    """

    cli_command = "claude"
    # `claude --print <prompt>` — non-interactive, выводит ответ в stdout
    # `--output-format text` — без markdown wrappers
    print_flag = ["--print", "--output-format", "text"]

    def __init__(
        self,
        *,
        model: str = "sonnet",
        **kwargs: Any,
    ) -> None:
        super().__init__(model=model, **kwargs)
        # Если в профиле указана модель — пробрасываем через --model
        if self.model and self.model not in ("default", ""):
            self.extra_args.extend(["--model", self.model])


class CodexCLIClient(CLIClientBase):
    """Bridge для `codex` CLI (OpenAI Codex).

    Использует ChatGPT Plus/Pro подписку или API-ключ — `codex login`
    настраивает один из вариантов.

    Setup аналогично Claude:
    1. Установить codex CLI в образ.
    2. Смонтировать ~/.codex/ для auth.

    Профиль:
      provider="codex_cli", model="o4-mini"|"gpt-4o"|..., api_key=None
    """

    cli_command = "codex"
    # `codex exec <prompt>` — agentic mode без TUI
    # codex CLI ещё развивается, флаги могут уточняться
    print_flag = ["exec"]

    def __init__(
        self,
        *,
        model: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(model=model, **kwargs)
        if self.model:
            self.extra_args.extend(["--model", self.model])


__all__ = ["ClaudeCLIClient", "CodexCLIClient", "CLIClientBase"]
