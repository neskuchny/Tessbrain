# -*- coding: utf-8 -*-
"""Генерация через ПОДПИСКУ CLI-агента (claude/codex/gemini/qwen) как
LLM-движок уровня «Премиум».

Экономика: подписка = фикс-цена за редкие тяжёлые вызовы (исследования,
отчёты, разбор документов, СИМА, пакетная синхронизация «1 вызов вместо 30» —
tiered extraction). Семантика окружения — как у vibe-tasking исполнителя:
ANTHROPIC_API_KEY вычищается (CLI работает по login-подписке),
TESSENT_HANDOFF_SHARED_CLI_LOGIN даёт общий логин. Параллельность ограничена
семафором (2) — не съедаем лимит подписки пачкой фоновых задач. Сбой/таймаут →
вызывающий (router/workload_policy) откатывается на API-цепочку, ничего
не падает.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_AGENTS = {"claude": ["claude", "-p", "--output-format", "text"],
           "codex": ["codex", "exec"],
           "gemini": ["gemini", "-p"],
           "qwen": ["qwen", "-p"],
           "kimi": ["kimi", "-p"]}
_SEM = asyncio.Semaphore(2)
_TIMEOUT_S = int(os.getenv("TESSENT_CLI_LLM_TIMEOUT", "900"))


class SubscriptionCliCaller:
    """Совместим с контрактом caller'ов workload_policy/router:
    .generate(prompt, **kw) -> str, .aclose()."""

    def __init__(self, agent: str = "claude"):
        self.agent = (agent or "claude").strip().lower()
        self.enabled = self.agent in _AGENTS

    async def generate(self, prompt: str, **_kw) -> str:
        if not self.enabled:
            raise RuntimeError(f"CLI-агент не поддержан: {self.agent}")
        cmd = list(_AGENTS[self.agent])
        env = dict(os.environ)
        # Подписочный режим: ключ API убираем, чтобы CLI использовал login.
        env.pop("ANTHROPIC_API_KEY", None)
        async with _SEM:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=env)
            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(prompt.encode("utf-8")),
                    timeout=_TIMEOUT_S)
            except asyncio.TimeoutError:
                proc.kill()
                raise RuntimeError(
                    f"CLI {self.agent}: таймаут {_TIMEOUT_S}s")
        if proc.returncode != 0:
            raise RuntimeError(
                f"CLI {self.agent} rc={proc.returncode}: "
                + (err or b"").decode("utf-8", "replace")[:300])
        text = (out or b"").decode("utf-8", "replace").strip()
        if not text:
            raise RuntimeError(f"CLI {self.agent}: пустой ответ")
        return text

    async def aclose(self) -> None:
        return None


__all__ = ["SubscriptionCliCaller"]
