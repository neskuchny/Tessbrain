"""Code artifacts validator (W21).

Запускается на artifacts которые выглядят как код. Выполняет static checks:
- ruff / pyflakes (Python)
- tsc / eslint (Node)
- shellcheck (.sh)

Все проверки optional — если бинаря нет, validator skip'ает шаг и
не понижает score за это.

Дизайн:
- subprocess в asyncio с timeout
- НЕ запускаем тесты (`pytest`/`vitest`) автоматически — это требует
  install dependencies, slow, и часто требует env. Caller решает
  отдельно.
- artifacts с content и `name`, оканчивающимся на .py/.ts/... парсятся
  по группам, прогоняются через инструменты в режиме «текст на stdin
  или временный файл»
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from backend.core.validator.base import (
    Issue,
    IssueSeverity,
    Validator,
    ValidatorReport,
)

logger = logging.getLogger(__name__)


_CODE_EXTS = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".sh": "shell",
}


def _detect_language(name: str) -> str | None:
    name = name.lower()
    for ext, lang in _CODE_EXTS.items():
        if name.endswith(ext):
            return lang
    return None


async def _run(cmd: list[str], cwd: str, timeout: float = 30.0) -> tuple[int, str]:
    """Запустить cmd, вернуть (rc, combined_output)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return (proc.returncode if proc.returncode is not None else -1,
                (stdout or b"").decode("utf-8", errors="replace"))
    except asyncio.TimeoutError:
        return -1, f"[timeout after {timeout}s]"
    except FileNotFoundError:
        return -127, "[binary not found]"
    except Exception as exc:
        return -1, f"[unexpected error: {exc}]"


class CodeValidator(Validator):
    name = "code"

    def __init__(self, *, timeout_per_check: float = 30.0) -> None:
        self.timeout_per_check = timeout_per_check

    async def validate(
        self,
        *,
        task_description: str,
        tz_markdown: str,
        artifacts: list[dict[str, Any]],
        **_kwargs: Any,
    ) -> ValidatorReport:
        report = ValidatorReport(validator=self.name, score=10.0)

        # Собираем code artifacts.
        code_artifacts: list[tuple[str, str, str]] = []  # (name, lang, content)
        for a in artifacts or []:
            if not isinstance(a, dict):
                continue
            name = a.get("name") or ""
            content = a.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            lang = _detect_language(name)
            if lang:
                code_artifacts.append((name, lang, content))

        if not code_artifacts:
            # Не code-задача → нейтральный score (validator не отбирает баллы).
            report.score = 10.0
            report.summary = "no code artifacts to validate"
            report.metadata = {"checked_files": 0}
            return report

        deductions = 0.0
        checked_files = 0
        skipped_no_tool = 0

        with tempfile.TemporaryDirectory(prefix="tessent-code-validate-") as tmpdir:
            for name, lang, content in code_artifacts:
                # Безопасное относительное имя в tmpdir.
                rel_name = os.path.basename(name) or "untitled"
                file_path = Path(tmpdir) / rel_name
                try:
                    file_path.write_text(content, encoding="utf-8")
                except OSError as exc:
                    logger.debug("code validator write failed: %s", exc)
                    continue

                checked_files += 1

                if lang == "python":
                    issues, deduct, skipped = await self._check_python(file_path, tmpdir)
                elif lang in {"typescript", "javascript"}:
                    issues, deduct, skipped = await self._check_node(file_path, tmpdir)
                elif lang == "shell":
                    issues, deduct, skipped = await self._check_shell(file_path, tmpdir)
                else:
                    issues, deduct, skipped = [], 0.0, False

                if skipped:
                    skipped_no_tool += 1
                report.issues.extend(issues)
                deductions += deduct

        report.score = max(0.0, 10.0 - deductions)
        report.summary = (
            f"checked {checked_files} code files; "
            f"{report.error_count} errors, {report.warning_count} warnings; "
            f"skipped {skipped_no_tool} (tools missing)"
        )
        report.metadata = {
            "checked_files": checked_files,
            "skipped_no_tool": skipped_no_tool,
            "deductions": round(deductions, 2),
        }
        return report

    async def _check_python(
        self, file_path: Path, cwd: str,
    ) -> tuple[list[Issue], float, bool]:
        issues: list[Issue] = []

        # Сначала syntax (py_compile через asyncio).
        rc, output = await _run(
            ["python", "-c", f"import py_compile; py_compile.compile({str(file_path)!r}, doraise=True)"],
            cwd=cwd, timeout=self.timeout_per_check,
        )
        if rc != 0:
            issues.append(Issue(
                severity=IssueSeverity.ERROR,
                category="syntax_error",
                message=f"Python syntax error in {file_path.name}",
                location=file_path.name,
                fix_hint=output[:300],
            ))
            return issues, 3.0, False

        # ruff если есть.
        if shutil.which("ruff") is None:
            return issues, 0.0, True

        rc, output = await _run(
            ["ruff", "check", "--no-cache", "--output-format=concise", str(file_path)],
            cwd=cwd, timeout=self.timeout_per_check,
        )
        if rc != 0 and output.strip():
            # Ruff вернёт ненулевой code только если нашёл issues.
            lines = [line for line in output.split("\n") if line.strip()][:20]
            for line in lines:
                issues.append(Issue(
                    severity=IssueSeverity.WARNING,
                    category="lint",
                    message=line[:200],
                    location=file_path.name,
                ))
        deduct = 0.5 * min(len(issues), 4)
        return issues, deduct, False

    async def _check_node(
        self, file_path: Path, cwd: str,
    ) -> tuple[list[Issue], float, bool]:
        # node --check для basic syntax.
        if shutil.which("node") is None:
            return [], 0.0, True
        rc, output = await _run(
            ["node", "--check", str(file_path)],
            cwd=cwd, timeout=self.timeout_per_check,
        )
        issues: list[Issue] = []
        if rc != 0 and ".tsx" not in file_path.name and ".ts" not in file_path.name:
            issues.append(Issue(
                severity=IssueSeverity.WARNING,
                category="syntax_warning",
                message=f"node --check non-zero on {file_path.name}",
                location=file_path.name,
                fix_hint=output[:300],
            ))
        # Для TS-файлов: tsc нужен contextual setup, скипаем (тяжёлая deps).
        return issues, 0.5 * len(issues), False

    async def _check_shell(
        self, file_path: Path, cwd: str,
    ) -> tuple[list[Issue], float, bool]:
        if shutil.which("shellcheck") is None:
            return [], 0.0, True
        rc, output = await _run(
            ["shellcheck", "-f", "tty", str(file_path)],
            cwd=cwd, timeout=self.timeout_per_check,
        )
        issues: list[Issue] = []
        if rc != 0 and output.strip():
            lines = [line for line in output.split("\n") if line.strip()][:10]
            for line in lines:
                issues.append(Issue(
                    severity=IssueSeverity.WARNING,
                    category="shell_lint",
                    message=line[:200],
                    location=file_path.name,
                ))
        deduct = 0.3 * min(len(issues), 5)
        return issues, deduct, False


__all__ = ["CodeValidator"]
