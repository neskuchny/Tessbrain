# -*- coding: utf-8 -*-
"""Уроки исполнения → плейбуки (Self-Grown, перенос идеи из OpenOPC).

OpenOPC атрибутирует исход каждой работы исполнителю и превращает
повторяющиеся уроки в переиспользуемые playbooks. У нас исходы уже есть —
вердикты LLM-судьи по каждому handoff (done / needs_work, чего не хватило).
Этот модуль:

1. **append_lesson** — фиксирует урок при завершении handoff: задача,
   исполнитель (агент), вердикт, чего не хватило, сколько попыток. Пишется
   автоматически из task_analysis (best-effort, не ломает исполнение).
2. **crystallize_playbooks** — LLM смотрит на накопленные уроки и предлагает
   черновики плейбуков ТОЛЬКО там, где паттерн повторился (≥3 схожих урока);
   мало данных → честный пустой список. Ничего не сохраняет сам.
3. **accept_playbook** — human gate: плейбук сохраняется только по явному
   действию пользователя (как сшивка «это я» и каскад целей).

Хранение — per-user JSON (file_lock + atomic), паттерн остальных примитивов.
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MAX_LESSONS = 200
_MAX_PLAYBOOKS = 50
_MIN_PATTERN = 3  # минимум схожих уроков для плейбука — меньше это не паттерн


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(user_id: str) -> str:
    from backend.core.store.tenant_paths import _DATA_ROOT, _require_uuid
    user_id = _require_uuid(user_id, "user_id")
    d = _DATA_ROOT / "lessons"
    d.mkdir(parents=True, exist_ok=True)
    return str(d / f"{user_id}.json")


def _load(user_id: str) -> Dict[str, Any]:
    try:
        p = _path(user_id)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return {"lessons": d.get("lessons", []),
                        "playbooks": d.get("playbooks", [])}
    except Exception:
        logger.debug("lessons load failed", exc_info=True)
    return {"lessons": [], "playbooks": []}


def _save(user_id: str, data: Dict[str, Any]) -> None:
    try:
        from backend.core.store.tenant_io import atomic_write_json
        atomic_write_json(_path(user_id), {
            "lessons": data.get("lessons", [])[-_MAX_LESSONS:],
            "playbooks": data.get("playbooks", [])[-_MAX_PLAYBOOKS:],
        })
    except Exception:
        logger.warning("lessons save failed", exc_info=True)


def append_lesson(user_id: str, *, task_title: str, agent: str,
                  verdict: str, missing: Optional[List[str]] = None,
                  summary: str = "", attempt: int = 1,
                  handoff_id: str = "", kind: str = "") -> None:
    """Зафиксировать урок исполнения (best-effort, never-raise)."""
    try:
        from backend.core.store.tenant_io import file_lock
        p = _path(user_id)
        with file_lock(p):
            data = _load(user_id)
            data["lessons"].append({
                "id": uuid.uuid4().hex[:8],
                "ts": _now(),
                "task_title": str(task_title or "")[:200],
                "agent": str(agent or "")[:40],
                "verdict": str(verdict or "")[:20],
                "missing": [str(m)[:200] for m in (missing or [])[:8]],
                "summary": str(summary or "")[:400],
                "attempt": int(attempt or 1),
                "handoff_id": str(handoff_id or "")[:16],
                "kind": str(kind or "")[:20],
            })
            _save(user_id, data)
    except Exception:
        logger.debug("append_lesson skipped", exc_info=True)


def list_lessons(user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    return list(reversed(_load(user_id).get("lessons", [])))[:max(1, limit)]


def list_playbooks(user_id: str) -> List[Dict[str, Any]]:
    return list(reversed(_load(user_id).get("playbooks", [])))


def _crystallize_prompt(lessons: List[Dict[str, Any]]) -> str:
    lines = []
    for l in lessons:
        miss = ("; не хватило: " + ", ".join(l.get("missing") or [])) \
            if l.get("missing") else ""
        lines.append(
            f"- [{l.get('ts', '')[:10]}] «{l.get('task_title')}» · "
            f"исполнитель {l.get('agent') or '—'} · вердикт {l.get('verdict')} · "
            f"попытка {l.get('attempt', 1)}{miss}")
    return (
        "Ты выращиваешь ПЛЕЙБУКИ из уроков исполнения задач AI-агентами. "
        "Плейбук — короткая переиспользуемая инструкция «как в этой компании "
        "делать такие задачи, чтобы приняли с первого раза».\n\n"
        "ЖЁСТКИЕ ПРАВИЛА:\n"
        "1. Плейбук предлагай ТОЛЬКО когда паттерн ПОВТОРИЛСЯ: минимум 3 "
        "схожих урока (одинаковый тип задач или одинаковая причина "
        "доработки). Один случай — не паттерн.\n"
        "2. Каждый шаг/подводный камень — только из фактов уроков ниже "
        "(что реально не хватало, что реально приводило к переделке). "
        "Не выдумывай общих советов из головы.\n"
        "3. Мало данных или паттернов нет — верни []. Пустой массив — "
        "валидный и честный ответ.\n"
        "4. evidence — id уроков, на которых основан плейбук.\n\n"
        "УРОКИ:\n" + "\n".join(lines) +
        "\n\nОтвет — ТОЛЬКО валидный JSON-массив:\n"
        '[{"title": "...", "when_to_use": "...", "steps": ["..."], '
        '"pitfalls": ["..."], "evidence": ["id1", "id2", "id3"]}]'
    )


async def crystallize_playbooks(user_id: str) -> Dict[str, Any]:
    """Черновики плейбуков из уроков (НИЧЕГО не сохраняет — human gate)."""
    data = _load(user_id)
    lessons = data.get("lessons", [])[-60:]
    if len(lessons) < _MIN_PATTERN:
        return {"status": "success", "drafts": [],
                "note": (f"уроков пока {len(lessons)} — паттерну нужно минимум "
                         f"{_MIN_PATTERN} схожих; продолжайте делегировать задачи")}
    try:
        from backend.core.llm.router import LLMRouter, ModelTier
        raw = await LLMRouter().generate(
            prompt=_crystallize_prompt(lessons),
            model_tier=ModelTier.STANDARD, max_tokens=1600)
    except Exception as e:  # noqa: BLE001
        logger.warning("playbook crystallize LLM failed: %s", e)
        return {"status": "error", "drafts": [],
                "note": "LLM недоступна — попробуйте позже"}

    drafts: List[Dict[str, Any]] = []
    try:
        t = str(raw or "").strip()
        if "```" in t:
            t = t.split("```")[1].lstrip("json").strip()
        m = re.search(r"\[.*\]", t, re.DOTALL)
        items = json.loads(m.group(0) if m else t)
        known_ids = {l.get("id") for l in lessons}
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict) or not str(it.get("title") or "").strip():
                continue
            evidence = [e for e in (it.get("evidence") or []) if e in known_ids]
            if len(evidence) < _MIN_PATTERN:
                continue  # паттерн не подтверждён реальными уроками — отбрасываем
            drafts.append({
                "title": str(it["title"])[:200],
                "when_to_use": str(it.get("when_to_use") or "")[:300],
                "steps": [str(s)[:300] for s in (it.get("steps") or [])[:10]],
                "pitfalls": [str(p)[:300] for p in (it.get("pitfalls") or [])[:8]],
                "evidence": evidence[:10],
            })
    except Exception:
        logger.debug("playbook parse failed", exc_info=True)
    note = "" if drafts else "повторяющихся паттернов в уроках пока не видно"
    return {"status": "success", "drafts": drafts, "note": note}


def accept_playbook(user_id: str, draft: Dict[str, Any]) -> Dict[str, Any]:
    """Сохранить плейбук — ТОЛЬКО по явному действию пользователя."""
    title = str((draft or {}).get("title") or "").strip()
    if not title:
        return {"status": "error", "message": "у плейбука нет названия"}
    try:
        from backend.core.store.tenant_io import file_lock
        p = _path(user_id)
        with file_lock(p):
            data = _load(user_id)
            pb = {
                "id": uuid.uuid4().hex[:8],
                "accepted_at": _now(),
                "title": title[:200],
                "when_to_use": str(draft.get("when_to_use") or "")[:300],
                "steps": [str(s)[:300] for s in (draft.get("steps") or [])[:10]],
                "pitfalls": [str(x)[:300] for x in (draft.get("pitfalls") or [])[:8]],
                "evidence": [str(e)[:16] for e in (draft.get("evidence") or [])[:10]],
            }
            data["playbooks"].append(pb)
            _save(user_id, data)
        return {"status": "success", "playbook": pb}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "message": str(e)}


async def dictate_playbook(user_id: str, text: str) -> Dict[str, Any]:
    """«Наговорил паттерн один раз → система применяет всегда».

    Надиктовка (голосовая расшифровка или текст) → LLM формует плейбук того
    же формата, что и кристаллизованные из уроков → сохраняем СРАЗУ принятым
    (source=dictated): здесь диктует сам человек — он и есть human gate.
    Дальше плейбук автоматически подмешивается в каждое новое ТЗ через
    playbooks_context_block. НЕ дублирует иной функционал: отдельного
    «создания регламентов» в системе нет — это расширение Self-Grown."""
    raw = (text or "").strip()
    if len(raw) < 20:
        return {"status": "error",
                "message": "надиктуйте правило подробнее (минимум пара фраз)"}
    prompt = (
        "Сформуй из надиктованного экспертом правила ПЛЕЙБУК. Верни СТРОГИЙ "
        "JSON: {\"title\": \"короткое название правила\", "
        "\"when_to_use\": \"когда применять\", "
        "\"steps\": [\"шаг/принцип\"], \"pitfalls\": [\"чего не делать\"]}. "
        "Только то, что сказал эксперт, ничего не выдумывай. По-русски. "
        "Только JSON.\n\nНАДИКТОВКА:\n" + raw[:6000])
    obj = None
    try:
        from backend.core.llm.workload_policy import generate_for_workload
        out = await generate_for_workload(user_id, "search_deep_synthesis", prompt)
        if not out:
            from backend.core.llm.router import get_llm_router
            out = await get_llm_router().generate(prompt, personalize=False)
        import json as _json
        import re as _re
        m = _re.search(r"\{.*\}", out or "", _re.DOTALL)
        obj = _json.loads(m.group(0)) if m else None
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "message": f"формовка правила: {e}"}
    if not obj or not obj.get("title"):
        return {"status": "error",
                "message": "не удалось разобрать правило — переформулируйте"}
    obj["evidence"] = ["dictated"]
    res = accept_playbook(user_id, obj)
    if res.get("status") == "success":
        res["playbook"]["source"] = "dictated"
    return res


def playbooks_context_block(user_id: str, *, limit: int = 5) -> str:
    """Принятые плейбуки строкой для промпта генерации ТЗ — чтобы новые
    задачи сразу учитывали выученное (замыкание Self-Grown)."""
    pbs = list_playbooks(user_id)[:max(1, limit)]
    if not pbs:
        return ""
    lines = ["=== ПЛЕЙБУКИ КОМПАНИИ (выучены из прошлых задач) ==="]
    for pb in pbs:
        steps = "; ".join(pb.get("steps") or [])
        lines.append(f"• {pb['title']} — {pb.get('when_to_use', '')}. "
                     f"Шаги: {steps}"[:500])
    return "\n".join(lines)
