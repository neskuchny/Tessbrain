# -*- coding: utf-8 -*-
"""Hermes approval-gate (P12e) — политика безопасного исполнения tool-call.

ПРОБЕЛ (карта переноса, #9): у Hermes есть command-approval перед
опасными действиями. У нас RBAC/MAC есть, но gate'а на сам tool-call
нет — а без него нельзя безопасно дать агенту execute_code/write.
Это safety-примитив, который РАЗБЛОКИРУЕТ расширение инструментов.

Механика: классифицируем tool-call (SAFE/WRITE/DANGEROUS) по имени и
аргументам, затем по политике решаем allow/deny. В неинтерактивном
API «approve» = явный allow-list/уровень (живого подтверждения в петле
нет — поэтому default fail-safe: опасное без явного allow → блок с
понятным observation, агент в ReAct это видит и адаптируется).

Дизайн (как P0–P12d): чистый stdlib, детерминированный, инъектируемый
(policy, audit), never-raises. `guard_executor` оборачивает любой
tool-executor — нулевой риск для существующих путей при mode=off.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class RiskLevel(IntEnum):
    SAFE = 0          # чтение/поиск/просмотр
    WRITE = 1         # создание/изменение/отправка
    DANGEROUS = 2     # удаление/исполнение кода/терминал/деньги


# консервативные эвристики (порядок: dangerous → write → safe)
_DANGEROUS = re.compile(
    r"(delete|remove|drop|destroy|exec|execute_code|terminal|shell|"
    r"run_command|bash|sudo|deploy|payment|charge|transfer|wipe|"
    r"truncate|force|\bcode\b|\bcommand\b|\bcmd\b|\bscript\b)",
    re.IGNORECASE,
)
_WRITE = re.compile(
    r"(create|update|edit|write|send|post|patch|add|upload|schedule|"
    r"assign|comment|merge|push|move|rename|set_|put_|manage|delegate)",
    re.IGNORECASE,
)


@dataclass
class ApprovalPolicy:
    """off — пропускать всё (дефолт, поведение не меняется).
    observe — пропускать, но логировать «заблокировал бы».
    enforce — блокировать опасное без явного allow."""
    mode: str = "off"                           # off|observe|enforce
    auto_allow_level: int = RiskLevel.SAFE      # ≤ этого — авто-allow
    allow: set[str] = field(default_factory=set)   # форс-allow по имени
    deny: set[str] = field(default_factory=set)    # форс-deny по имени


@dataclass
class ApprovalDecision:
    allowed: bool
    level: int
    reason: str
    observation: str = ""   # что вернуть агенту вместо исполнения

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed, "level": int(self.level),
            "reason": self.reason,
        }


def classify_tool(tool_name: str, args: Any) -> tuple[RiskLevel, str]:
    """Эвристическая классификация. Никогда не raises."""
    try:
        name = str(tool_name or "")
        blob = name
        if isinstance(args, dict):
            # имена аргументов тоже сигнал (command=, code=, path=)
            blob += " " + " ".join(str(k) for k in args.keys())
        if _DANGEROUS.search(blob):
            return RiskLevel.DANGEROUS, "matched dangerous pattern"
        if _WRITE.search(blob):
            return RiskLevel.WRITE, "matched write pattern"
        return RiskLevel.SAFE, "no write/dangerous signal"
    except Exception as exc:
        logger.debug("classify_tool failed → DANGEROUS (fail-safe): %s", exc)
        return RiskLevel.DANGEROUS, "classification error (fail-safe)"


def decide(
    tool_name: str,
    args: Any,
    *,
    policy: ApprovalPolicy,
) -> ApprovalDecision:
    """Решение по политике. Никогда не raises. Fail-safe: при сомнении
    в enforce — deny."""
    level, why = classify_tool(tool_name, args)
    name = str(tool_name or "")

    if policy.mode == "off":
        return ApprovalDecision(True, level, "approval off")

    if name in policy.deny:
        d = ApprovalDecision(False, level, f"deny-list: {name}")
        d.observation = _blocked_obs(name, d.reason)
        return d

    allowed = (
        name in policy.allow
        or int(level) <= int(policy.auto_allow_level)
    )

    if policy.mode == "observe":
        if not allowed:
            logger.info(
                "approval(observe): would BLOCK %s (level=%s, %s)",
                name, int(level), why,
            )
        return ApprovalDecision(True, level, f"observe ({why})")

    # enforce
    if allowed:
        return ApprovalDecision(True, level, f"auto-allow ({why})")
    d = ApprovalDecision(
        False, level, f"requires approval: {name} (level {int(level)})",
    )
    d.observation = _blocked_obs(name, d.reason)
    return d


def _blocked_obs(name: str, reason: str) -> str:
    return json.dumps(
        {
            "error": "approval_required",
            "tool": name,
            "reason": reason,
            "hint": (
                "Этот инструмент требует одобрения и НЕ был выполнен. "
                "Выбери безопасную альтернативу или заверши задачу."
            ),
        },
        ensure_ascii=False,
    )


def policy_from_env() -> ApprovalPolicy:
    """env-конфиг. DEFAULT mode=off (careful rollout — поведение не
    меняется, пока ops не включит). Никогда не raises."""
    try:
        mode = (os.getenv("HERMES_APPROVAL_MODE", "off") or "off").strip().lower()
        if mode not in ("off", "observe", "enforce"):
            mode = "off"
        allow = {
            x.strip() for x in
            (os.getenv("HERMES_APPROVAL_ALLOW", "") or "").split(",")
            if x.strip()
        }
        deny = {
            x.strip() for x in
            (os.getenv("HERMES_APPROVAL_DENY", "") or "").split(",")
            if x.strip()
        }
        return ApprovalPolicy(mode=mode, allow=allow, deny=deny)
    except Exception as exc:
        logger.debug("policy_from_env failed → safe default off: %s", exc)
        return ApprovalPolicy()


ToolExecutor = Callable[..., Awaitable[str]]
AuditFn = Callable[..., Awaitable[None]]


def guard_executor(
    executor: ToolExecutor,
    *,
    policy: ApprovalPolicy,
    audit: Optional[AuditFn] = None,
) -> ToolExecutor:
    """Обернуть tool-executor проверкой approval. Контракт исходного
    executor сохранён: async (tool_name, args) -> str. Никогда не
    raises (ошибка проверки → fail-safe: в enforce deny, иначе pass)."""

    async def _guarded(tool_name: str, args: dict) -> str:
        try:
            d = decide(tool_name, args, policy=policy)
        except Exception as exc:
            logger.debug("guard decide failed: %s", exc)
            d = ApprovalDecision(
                policy.mode != "enforce", RiskLevel.DANGEROUS,
                "guard error",
            )
            if not d.allowed:
                d.observation = _blocked_obs(str(tool_name), d.reason)
        if not d.allowed:
            if audit is not None:
                try:
                    await audit(event="approval.blocked",
                                detail={"tool": tool_name,
                                        "reason": d.reason})
                except Exception:
                    pass
            return d.observation or _blocked_obs(str(tool_name), d.reason)
        return await executor(tool_name, args)

    return _guarded


__all__ = [
    "RiskLevel",
    "ApprovalPolicy",
    "ApprovalDecision",
    "classify_tool",
    "decide",
    "policy_from_env",
    "guard_executor",
]
