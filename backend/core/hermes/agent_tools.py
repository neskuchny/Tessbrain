# -*- coding: utf-8 -*-
"""Hermes agentic tools (P12f) — skill_manage / delegate / execute_code.

Закрывает #3 карты («выполнить любую задачу / гибкость»):
- skill_manage  — агент САМ создаёт/правит/удаляет навыки (реально,
  через P12b SkillStore). Это «учится как действовать» из запроса.
- delegate_to_agent — делегирование другому агенту (brain/mark/...).
  Исполнитель ИНЪЕКТИРУЕМ; по умолчанию НЕ сконфигурирован → честная
  безопасная заглушка (слот открыт, петли/латентность не тащим).
- execute_code — слот под исполнение кода. Runner ИНЪЕКТИРУЕМ; по
  умолчанию None → НЕ исполняет ничего, возвращает «нужен approval +
  sandbox». Реальный sandbox — отдельная фаза.

ВСЁ это идёт через approval-gate (P12e): skill_manage=WRITE,
execute_code/delete=DANGEROUS — без явного allow в enforce блокируется.

Дизайн (как P0–P12e): чистый stdlib, инъектируемый, never-raises,
fail-safe.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Optional

from .skill_store import SkillDoc, SkillStore

logger = logging.getLogger(__name__)

# delegate_fn(target, query) -> str   (инъектируем; None = не сконфигурирован)
DelegateFn = Callable[[str, str], Awaitable[str]]
# code_runner(code, lang) -> str      (инъектируем sandbox; None = выкл)
CodeRunner = Callable[[str, str], Awaitable[str]]

_ALLOWED_DELEGATE = ("brain", "mark", "automation")
_AGENT_TOOLS = {"skill_manage", "delegate_to_agent", "execute_code"}


def agent_tool_defs() -> list[dict]:
    """OpenAI-style определения. skill_manage реальный; delegate/
    execute_code — слоты (поведение зависит от инъекций)."""
    return [
        {
            "name": "skill_manage",
            "description": (
                "Создать/исправить/удалить ВЫУЧЕННЫЙ навык. "
                "action: create|patch|delete. Для create — поля навыка; "
                "для patch — name+old+new; для delete — name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["create", "patch", "delete"]},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "category": {"type": "string"},
                    "procedure": {"type": "string"},
                    "pitfalls": {"type": "string"},
                    "verification": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                },
                "required": ["action", "name"],
            },
        },
        {
            "name": "delegate_to_agent",
            "description": (
                "Делегировать подзадачу другому агенту "
                f"({'/'.join(_ALLOWED_DELEGATE)}). Используй, если задача "
                "вне твоей зоны. Возвращает ответ агента."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string",
                               "enum": list(_ALLOWED_DELEGATE)},
                    "query": {"type": "string"},
                },
                "required": ["target", "query"],
            },
        },
        {
            "name": "execute_code",
            "description": (
                "Выполнить код в изолированной среде (если "
                "сконфигурирована). Требует approval."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "lang": {"type": "string", "default": "python"},
                },
                "required": ["code"],
            },
        },
    ]


def is_agent_tool(name: str) -> bool:
    return name in _AGENT_TOOLS


def _err(msg: str, **extra) -> str:
    return json.dumps({"error": msg, **extra}, ensure_ascii=False)


def execute_skill_manage(
    args: dict,
    *,
    store: SkillStore,
    user_id: str,
) -> str:
    """Реальное управление навыками через P12b. Никогда не raises."""
    try:
        a = args or {}
        action = str(a.get("action", "")).lower()
        name = str(a.get("name", "")).strip()
        if not name:
            return _err("skill name required")
        if action == "create":
            doc = SkillDoc(
                name=name,
                description=str(a.get("description", "") or ""),
                category=str(a.get("category", "general") or "general"),
                procedure=str(a.get("procedure", "") or ""),
                pitfalls=str(a.get("pitfalls", "") or ""),
                verification=str(a.get("verification", "") or ""),
                when_to_use=str(a.get("when_to_use", "") or ""),
            )
            ok = store.create(user_id, doc)
            return json.dumps({"ok": ok, "action": "create", "name": name},
                              ensure_ascii=False)
        if action == "patch":
            ok = store.patch(user_id, name, str(a.get("old", "")),
                             str(a.get("new", "")))
            return json.dumps({"ok": ok, "action": "patch", "name": name},
                              ensure_ascii=False)
        if action == "delete":
            ok = store.delete(user_id, name)
            return json.dumps({"ok": ok, "action": "delete", "name": name},
                              ensure_ascii=False)
        return _err(f"unknown skill_manage action '{action}'")
    except Exception as exc:
        logger.debug("execute_skill_manage failed: %s", exc)
        return _err("skill_manage failed")


async def execute_delegate(
    args: dict,
    *,
    delegate_fn: Optional[DelegateFn] = None,
    depth: int = 0,
    max_depth: int = 1,
) -> str:
    """Делегировать подзадачу. Bounded по глубине (анти-цикл).
    delegate_fn=None → честная заглушка. Никогда не raises."""
    try:
        a = args or {}
        target = str(a.get("target", "")).lower()
        query = str(a.get("query", "")).strip()
        if target not in _ALLOWED_DELEGATE:
            return _err("invalid delegate target",
                        allowed=list(_ALLOWED_DELEGATE))
        if not query:
            return _err("delegate query required")
        if depth >= max_depth:
            return _err("delegation depth limit reached")
        if delegate_fn is None:
            return json.dumps(
                {"error": "delegation_not_configured",
                 "hint": "решай задачу доступными инструментами"},
                ensure_ascii=False,
            )
        out = await delegate_fn(target, query)
        return out if isinstance(out, str) else json.dumps(
            {"result": out}, ensure_ascii=False, default=str)
    except Exception as exc:
        logger.debug("execute_delegate failed: %s", exc)
        return _err("delegate failed")


async def execute_code_tool(
    args: dict,
    *,
    runner: Optional[CodeRunner] = None,
) -> str:
    """Слот исполнения кода. runner=None → НЕ исполняет (безопасно).
    Никогда не raises."""
    try:
        a = args or {}
        code = str(a.get("code", "") or "")
        lang = str(a.get("lang", "python") or "python")
        if not code.strip():
            return _err("code required")
        if runner is None:
            return json.dumps(
                {"error": "code_execution_not_configured",
                 "hint": ("Исполнение кода не включено (нет sandbox). "
                          "Используй другие инструменты.")},
                ensure_ascii=False,
            )
        out = await runner(code, lang)
        return out if isinstance(out, str) else json.dumps(
            {"result": out}, ensure_ascii=False, default=str)
    except Exception as exc:
        logger.debug("execute_code_tool failed: %s", exc)
        return _err("execute_code failed")


async def execute_agent_tool(
    name: str,
    args: dict,
    *,
    store: Optional[SkillStore] = None,
    user_id: str = "",
    delegate_fn: Optional[DelegateFn] = None,
    code_runner: Optional[CodeRunner] = None,
    depth: int = 0,
) -> str:
    """Единый диспетчер агентных инструментов. Никогда не raises."""
    try:
        if name == "skill_manage":
            if store is None:
                return _err("skill store unavailable")
            return execute_skill_manage(args or {}, store=store,
                                        user_id=user_id)
        if name == "delegate_to_agent":
            return await execute_delegate(args or {},
                                          delegate_fn=delegate_fn,
                                          depth=depth)
        if name == "execute_code":
            return await execute_code_tool(args or {}, runner=code_runner)
        return _err(f"unknown agent tool '{name}'")
    except Exception as exc:
        logger.debug("execute_agent_tool failed: %s", exc)
        return _err("agent tool failed")


__all__ = [
    "DelegateFn",
    "CodeRunner",
    "agent_tool_defs",
    "is_agent_tool",
    "execute_skill_manage",
    "execute_delegate",
    "execute_code_tool",
    "execute_agent_tool",
]
