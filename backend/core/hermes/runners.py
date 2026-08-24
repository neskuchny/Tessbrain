# -*- coding: utf-8 -*-
"""Hermes runners (P12i-IND) — индустриализация слотов P12f.

delegate_to_agent / execute_code в P12f были безопасными слотами
(исполнители = None). Здесь — РЕАЛЬНЫЕ исполнители поверх уже
существующей продовой инфраструктуры, без сырого exec:

- execute_code → `core/executors` (OpenHands/ClaudeCode/Cursor/Noop
  через get_active_backend; default Noop = безопасно). НЕ subprocess.
- delegate_to_agent → инъектируемый answer_fn (в проде — наш
  reasoning/LLM-роутер; в тесте — фейк). bounded в P12f по глубине.

Дизайн (как весь P12): инъектируемо, never-raises, bounded по
времени (не вешаем ReAct-ход). Careful rollout: подключаются только
при env HERMES_REAL_EXECUTION (иначе слоты остаются заглушками).
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Optional

logger = logging.getLogger(__name__)

# answer_fn(query) -> str   (в проде reasoning/LLM; в тесте фейк)
AnswerFn = Callable[[str], Awaitable[str]]


def make_executor_code_runner(
    *,
    backend: Any = None,
    max_wait_s: float = 30.0,
    poll_s: float = 2.0,
) -> Callable[[str, str], Awaitable[str]]:
    """Вернуть async (code, lang) -> str поверх core/executors.

    backend инъектируем (тест → фейк); по умолчанию
    get_active_backend() — Noop, если OpenHands/CLI не сконфигурены
    (безопасно). Никогда не raises; bounded ожидание результата."""

    async def _runner(code: str, lang: str) -> str:
        try:
            try:
                from backend.core.executors.base import TaskSubmission
            except Exception:
                from dataclasses import dataclass, field

                @dataclass
                class TaskSubmission:  # минимальный fallback-контракт
                    tz_markdown: str = ""
                    task_type: Optional[str] = None
                    timeout_seconds: int = 3600
                    metadata: dict = field(default_factory=dict)
            be = backend
            if be is None:
                from backend.core.executors.registry import (
                    get_active_backend,
                )
                be = get_active_backend()

            tz = (
                f"Execute the following {lang} code in an isolated "
                f"environment and return stdout/result.\n\n```{lang}\n"
                f"{code}\n```"
            )
            handle = await be.submit(TaskSubmission(
                tz_markdown=tz, task_type="code_exec",
                timeout_seconds=int(max_wait_s) + 10,
                metadata={"hermes": "execute_code", "lang": lang},
            ))
            waited = 0.0
            while waited < max_wait_s:
                res = await be.get_result(handle)
                if res is not None:
                    return json.dumps(
                        {
                            "backend": getattr(be, "name", "?"),
                            "success": bool(getattr(res, "success", False)),
                            "summary": getattr(res, "summary", "")[:1500],
                            "logs": getattr(res, "logs_excerpt", "")[:1500],
                            "error": getattr(res, "error_message", "")[:500],
                        },
                        ensure_ascii=False,
                    )
                await asyncio.sleep(poll_s)
                waited += poll_s
            return json.dumps(
                {"backend": getattr(be, "name", "?"),
                 "status": "pending",
                 "handle": getattr(handle, "id", ""),
                 "hint": "задача отправлена, результат ещё не готов"},
                ensure_ascii=False,
            )
        except Exception as exc:
            logger.debug("code runner failed (safe): %s", exc)
            return json.dumps({"error": "code execution failed"},
                              ensure_ascii=False)

    return _runner


def make_brain_delegate(
    answer_fn: Optional[AnswerFn],
    *,
    supported: tuple[str, ...] = ("brain",),
) -> Callable[[str, str], Awaitable[str]]:
    """Вернуть delegate_fn(target, query) -> str. Реально обслуживает
    только `supported` цели (по умолчанию brain) через answer_fn;
    остальные → честный отказ. Никогда не raises."""

    async def _delegate(target: str, query: str) -> str:
        try:
            t = (target or "").lower()
            if t not in supported or answer_fn is None:
                return json.dumps(
                    {"error": "delegate target not available",
                     "supported": list(supported)},
                    ensure_ascii=False,
                )
            out = await answer_fn(query)
            return out if isinstance(out, str) else json.dumps(
                {"result": out}, ensure_ascii=False, default=str)
        except Exception as exc:
            logger.debug("brain delegate failed (safe): %s", exc)
            return json.dumps({"error": "delegate failed"},
                              ensure_ascii=False)

    return _delegate


__all__ = [
    "AnswerFn",
    "make_executor_code_runner",
    "make_brain_delegate",
]
