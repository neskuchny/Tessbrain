# -*- coding: utf-8 -*-
"""Hermes skill-runtime (P12c) — скиллы как инструменты петли +
автосоздание из успешных траекторий.

Поверх P12b (skill_store): тут (1) tool-определения и диспетчер, чтобы
agent_oc-петля могла СПРАШИВАТЬ скиллы (progressive disclosure:
skills_list → skill_view), и (2) триггер + инъектируемый драфтер,
который из успешной траектории делает SkillDoc и сохраняет — это и
есть «агент учится навыкам» как у Hermes.

Дизайн (как P0–P12b): чистый, инъектируемый (store, llm_json_call,
clock), never-raises, bounded. Без тяжёлых backend.core.* импортов
(юнит-тест без графа/БД/сети).
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Optional

from .skill_store import SkillDoc, SkillStore

logger = logging.getLogger(__name__)

# async (prompt:str) -> str|dict   (реальный LLM в проде, фейк в тесте)
LLMJsonCall = Callable[[str], Awaitable[Any]]

_AUTOLEARN_MIN_STEPS = 5  # как Hermes: «сложная задача» = 5+ tool-вызовов


# === скиллы как инструменты петли ======================================

def skill_tool_defs() -> list[dict]:
    """OpenAI-style определения read-инструментов для ReAct-петли."""
    return [
        {
            "name": "skills_list",
            "description": (
                "Список доступных выученных навыков (краткие карточки: "
                "name/description/category). Вызови ПЕРВЫМ, если задача "
                "похожа на ранее решённую."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "skill_view",
            "description": (
                "Полный навык по имени (When to Use / Procedure / "
                "Pitfalls / Verification). Используй процедуру как план."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "имя навыка"}
                },
                "required": ["name"],
            },
        },
    ]


_SKILL_TOOL_NAMES = {"skills_list", "skill_view"}


def is_skill_tool(name: str) -> bool:
    return name in _SKILL_TOOL_NAMES


def build_skill_nudge(
    *,
    skills_enabled: bool,
    agent_tools_enabled: bool = False,
    lessons_enabled: bool = False,
) -> str:
    """Системная преамбула-nudge (как у Hermes): объясняет агенту,
    КОГДА смотреть/создавать навыки и учитывать уроки. Без неё
    подключённые инструменты простаивают — модель про них не знает.
    Эмитит только релевантные строки. '' если ничего не включено.
    Не raises."""
    if not skills_enabled:
        return ""
    lines = ["[Память навыков]"]
    lines.append(
        "- Если задача похожа на ранее решённую — СНАЧАЛА вызови "
        "`skills_list`, при совпадении `skill_view` и следуй "
        "Procedure (учитывая Pitfalls/Verification)."
    )
    if agent_tools_enabled:
        lines.append(
            "- После успешного решения нетривиальной задачи (5+ "
            "шагов, либо ошибка→обход, либо ты исправил подход) — "
            "сохрани навык, вызвав ИНСТРУМЕНТ skill_manage в "
            "стандартном формате: "
            '{"action": "call", "tool": "skill_manage", '
            '"args": {"action": "create", "name": "...", '
            '"description": "...", "procedure": "..."}}. '
            "После сохранения сразу заверши задачу через "
            '{"action": "done", "answer": "..."} '
            "с полезным ответом пользователю — не оставляй вывод сырым."
        )
    else:
        lines.append(
            "- Полезные многошаговые решения система сохранит в навыки "
            "автоматически — действуй чётко и проверяемо."
        )
    if lessons_enabled:
        lines.append(
            "- Учитывай блок «Уроки из прошлого опыта» ниже: не "
            "повторяй отмеченные ⚠️ провалы."
        )
    return "\n".join(lines)


def execute_skill_tool(
    name: str,
    args: dict,
    *,
    store: SkillStore,
    user_id: str,
) -> str:
    """Диспетчер skill-инструментов → JSON-строка (контракт как у
    oc_execute_tool). Никогда не raises."""
    try:
        if name == "skills_list":
            metas = store.list(user_id)
            return json.dumps(
                {"skills": [m.to_dict() for m in metas], "count": len(metas)},
                ensure_ascii=False,
            )
        if name == "skill_view":
            sk = store.view(user_id, str((args or {}).get("name", "")))
            if sk is None:
                return json.dumps({"error": "skill not found"},
                                  ensure_ascii=False)
            return json.dumps(
                {
                    "name": sk.name, "description": sk.description,
                    "category": sk.category, "version": sk.version,
                    "when_to_use": sk.when_to_use, "procedure": sk.procedure,
                    "pitfalls": sk.pitfalls, "verification": sk.verification,
                },
                ensure_ascii=False,
            )
        return json.dumps({"error": f"unknown skill tool '{name}'"},
                          ensure_ascii=False)
    except Exception as exc:
        logger.debug("execute_skill_tool failed: %s", exc)
        return json.dumps({"error": "skill tool failed"}, ensure_ascii=False)


# === автосоздание из траектории ========================================

def _norm_steps(steps: Any) -> list[dict]:
    """ReActStep|dict → [{tool,args,thought}], 'done' отброшен. Безопасно."""
    out: list[dict] = []
    if not isinstance(steps, (list, tuple)):
        return out
    for s in steps:
        tool = getattr(s, "tool", None)
        args = getattr(s, "args", None)
        thought = getattr(s, "thought", None)
        if tool is None and isinstance(s, dict):
            tool, args, thought = s.get("tool"), s.get("args"), s.get("thought")
        if not tool or tool == "done":
            continue
        out.append({
            "tool": str(tool),
            "args": args if isinstance(args, dict) else {},
            "thought": str(thought or ""),
        })
    return out


_ERR_RE = __import__("re").compile(
    r'("error"|\berror\b|\bfailed\b|traceback|exception|not found|'
    r'denied|timeout|approval_required)', __import__("re").IGNORECASE)
_CORRECTION_RE = __import__("re").compile(
    r'(\bнет,|\bне так\b|\bне то\b|исправь|переделай|вместо этого|'
    r'на самом деле|\bwrong\b|\bno,|\bnot that\b|\binstead\b|'
    r'\bactually\b|\bredo\b|fix that)', __import__("re").IGNORECASE)


def _had_error_recovery(steps: Any) -> bool:
    """Был ли в траектории шаг с ошибкой (observation/thought/raw),
    т.е. «упёрся → нашёл путь» (Hermes-триггер). Не raises."""
    try:
        seq = steps if isinstance(steps, (list, tuple)) else []
        for s in seq:
            tool = getattr(s, "tool", None)
            if tool is None and isinstance(s, dict):
                tool = s.get("tool")
            if tool == "done":
                continue
            for attr in ("observation", "thought", "raw_text"):
                v = getattr(s, attr, None)
                if v is None and isinstance(s, dict):
                    v = s.get(attr)
                if v and _ERR_RE.search(str(v)):
                    return True
        return False
    except Exception:
        return False


def _looks_like_correction(user_message: str) -> bool:
    try:
        return bool(_CORRECTION_RE.search(str(user_message or "")))
    except Exception:
        return False


def should_propose_skill(
    steps: Any, *, success: bool, user_message: str = "",
) -> tuple[bool, str]:
    """Эвристика Hermes (4 триггера, дословно по их докам): успешная
    задача И один из:
      • сложная (≥5 значимых tool-шагов)
      • нетривиальный multi-tool workflow (≥3 шага, ≥2 инструмента)
      • ошибка/тупик → нашёл рабочий путь (error-recovery)
      • пользователь поправил подход (user-correction)
    Не raises."""
    if not success:
        return False, "trajectory not successful"
    norm = _norm_steps(steps)
    if len(norm) >= _AUTOLEARN_MIN_STEPS:
        return True, f"complex task ({len(norm)} tool steps)"
    tools_used = {s["tool"] for s in norm}
    if len(norm) >= 3 and len(tools_used) >= 2:
        return True, f"non-trivial workflow ({len(tools_used)} tools)"
    if norm and _had_error_recovery(steps):
        return True, "error-recovery (hit error, found working path)"
    if norm and _looks_like_correction(user_message):
        return True, "user-correction of approach"
    return False, "trivial trajectory"


def _build_draft_prompt(user_message: str, norm_steps: list[dict]) -> str:
    plan = "\n".join(
        f"{i}. {s['tool']}({', '.join(f'{k}={v}' for k, v in list(s['args'].items())[:3])})"
        for i, s in enumerate(norm_steps, 1)
    )
    return (
        "Из успешно решённой задачи извлеки ПЕРЕИСПОЛЬЗУЕМЫЙ навык. "
        "Верни ТОЛЬКО JSON: {\"name\":\"kebab-slug\",\"description\":"
        "\"одна строка\",\"category\":\"slug\",\"tags\":[..],"
        "\"when_to_use\":\"..\",\"procedure\":\"нумерованные шаги\","
        "\"pitfalls\":\"что не так пошло/риски\",\"verification\":"
        "\"как проверить успех\"}.\n"
        f"Задача пользователя:\n{user_message}\n\n"
        f"Выполненные шаги:\n{plan}\n"
    )


def draft_skill_from_trajectory_sync(raw: Any) -> Optional[SkillDoc]:
    """Распарсить ответ LLM (str|dict) в SkillDoc. Не raises.
    Валидно только если есть name и procedure."""
    obj: Any = raw
    if isinstance(raw, str):
        txt = raw.strip()
        if "```" in txt:
            parts = txt.split("```")
            if len(parts) >= 2:
                cand = parts[1]
                if cand.lstrip().lower().startswith("json"):
                    cand = cand.lstrip()[4:]
                txt = cand.strip()
        try:
            obj = json.loads(txt)
        except Exception:
            return None
    if not isinstance(obj, dict):
        return None
    name = str(obj.get("name", "") or "").strip()
    procedure = str(obj.get("procedure", "") or "").strip()
    if not name or not procedure:
        return None
    tags = obj.get("tags")
    return SkillDoc(
        name=name,
        description=str(obj.get("description", "") or ""),
        category=str(obj.get("category", "general") or "general"),
        tags=[str(t) for t in tags] if isinstance(tags, list) else [],
        when_to_use=str(obj.get("when_to_use", "") or ""),
        procedure=procedure,
        pitfalls=str(obj.get("pitfalls", "") or ""),
        verification=str(obj.get("verification", "") or ""),
    )


async def maybe_autolearn(
    user_message: str,
    steps: Any,
    success: bool,
    *,
    store: SkillStore,
    user_id: str,
    llm_json_call: LLMJsonCall,
    enabled: bool = False,
) -> Optional[str]:
    """Если включено и траектория достойна — сделать и сохранить скилл.
    Возвращает имя созданного скилла или None. НИКОГДА не raises
    (автообучение не должно ронять ответ агента)."""
    if not enabled:
        return None
    try:
        ok, _why = should_propose_skill(
            steps, success=success, user_message=user_message)
        if not ok:
            return None
        norm = _norm_steps(steps)
        prompt = _build_draft_prompt(user_message, norm)
        raw = await llm_json_call(prompt)
        doc = draft_skill_from_trajectory_sync(raw)
        if doc is None:
            return None
        # не плодим дубликаты — обновляем существующий (create перетрёт,
        # created_at сохранится из P12b)
        if store.create(user_id, doc):
            logger.info("hermes.autolearn: skill saved '%s'", doc.name)
            return doc.name
        return None
    except Exception as exc:
        logger.debug("hermes.autolearn failed (safe): %s", exc)
        return None


__all__ = [
    "LLMJsonCall",
    "skill_tool_defs",
    "is_skill_tool",
    "build_skill_nudge",
    "execute_skill_tool",
    "should_propose_skill",
    "draft_skill_from_trajectory_sync",
    "maybe_autolearn",
]
