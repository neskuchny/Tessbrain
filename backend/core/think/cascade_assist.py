# -*- coding: utf-8 -*-
"""Каскад-ассистент: раскрыть цель компании в цели отделов (CogniLayer Ф3).

Сверка каскада (org_sync.check_cascade) находит цели компании БЕЗ
отдела-владельца. Этот модуль помогает закрыть разрыв: LLM предлагает
черновики целей отделам, человек подтверждает — только после явного
«Принять» цель создаётся в goal_tracker (level=department,
parent_id=цель компании). Авто-записи нет: каскад — управленческое решение,
система лишь готовит заготовку (тот же human-gate, что у сшивки «это я»
и черновиков трансляции стратегии).

Дисциплина фактов:
- предлагать можно ТОЛЬКО отделам, которые реально существуют в орге
  (membership + отделы с целями) — чужой/выдуманный отдел отбрасывается кодом;
- числа, которых нет в цели компании, не выдаются за факт: код сверяет цифры
  предложения с текстом цели и принудительно помечает draft_numbers=true;
- нерелевантному отделу — ничего: пустой список предложений валиден.

Never-raise: сбой LLM → пустые предложения с честной note.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MAX_DEPTS = 12
_MAX_EXISTING_GOALS = 30


def _norm(s: Any) -> str:
    return str(s or "").strip().lower()


def _numbers(text: str) -> set:
    """Числовые токены текста (для проверки «число из источника»)."""
    return set(re.findall(r"\d+(?:[.,]\d+)?", str(text or "")))


def _propose_prompt(goal_text: str, departments: List[str],
                    existing: Dict[str, List[str]]) -> str:
    existing_lines = []
    for d, goals in existing.items():
        for g in goals[:5]:
            existing_lines.append(f"- [{d}] {g}")
    existing_block = ("\n".join(existing_lines[:_MAX_EXISTING_GOALS])
                      if existing_lines else "— (целей у отделов пока нет)")
    return (
        "Ты помогаешь руководителю раскрыть цель компании в цели отделов "
        "(каскад). Работаешь ТОЛЬКО с данными ниже.\n\n"
        f"ЦЕЛЬ КОМПАНИИ: {goal_text}\n\n"
        f"ОТДЕЛЫ КОМПАНИИ (других не существует): {', '.join(departments)}\n\n"
        f"УЖЕ СУЩЕСТВУЮЩИЕ ЦЕЛИ ОТДЕЛОВ (не дублируй их):\n{existing_block}\n\n"
        "ПРАВИЛА:\n"
        "1. Предлагай цель ТОЛЬКО тем отделам, чей вклад в цель компании "
        "очевиден из ЕЁ ФОРМУЛИРОВКИ. Отдел нерелевантен — пропусти его. "
        "Пустой массив — валидный ответ.\n"
        "2. НЕ выдумывай числа: используй числа из цели компании или "
        "формулируй без чисел. Если предлагаешь разбивку числа по отделам — "
        "это черновик, поставь draft_numbers: true.\n"
        "3. Максимум ОДНА цель на отдел. Формула: глагол-направление + что "
        "именно меняет этот отдел (+ за счёт чего).\n"
        "4. rationale — одно предложение, почему этот отдел, опираясь на "
        "слова цели компании. Не приписывай отделам обязательств, которых "
        "из цели не следует.\n\n"
        "Ответ — ТОЛЬКО валидный JSON-массив:\n"
        '[{"department": "...", "title": "...", "rationale": "...", '
        '"draft_numbers": false}]'
    )


def _parse_proposals(raw: str, departments: List[str],
                     goal_text: str) -> List[Dict[str, Any]]:
    """Валидация ответа LLM: только существующие отделы, непустой title,
    ≤1 цели на отдел, числа не из цели → принудительно draft_numbers."""
    try:
        t = str(raw or "").strip()
        if "```" in t:
            t = t.split("```")[1].lstrip("json").strip()
        m = re.search(r"\[.*\]", t, re.DOTALL)
        items = json.loads(m.group(0) if m else t)
    except Exception:
        logger.debug("cascade parse failed", exc_info=True)
        return []
    allowed = {_norm(d): d for d in departments}
    src_numbers = _numbers(goal_text)
    out: List[Dict[str, Any]] = []
    seen_depts: set = set()
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        dept = allowed.get(_norm(it.get("department")))
        title = str(it.get("title") or "").strip()
        if not dept or not title or dept in seen_depts:
            continue
        seen_depts.add(dept)
        draft_numbers = bool(it.get("draft_numbers"))
        if _numbers(title) - src_numbers:
            draft_numbers = True  # числа не из цели компании — только черновик
        out.append({
            "department": dept,
            "title": title[:300],
            "rationale": str(it.get("rationale") or "").strip()[:400],
            "draft_numbers": draft_numbers,
        })
    return out


async def _org_departments(user_id: str,
                           existing: Dict[str, List[str]]) -> List[str]:
    """Реальные отделы: участники орга + отделы, у которых уже есть цели."""
    depts: Dict[str, str] = {}
    try:
        from backend.core.ingest import membership
        org_id = membership.get_org_for_user(user_id)
        for m in (membership.list_members(org_id) if org_id else []):
            d = str(m.get("department") or "").strip()
            if d:
                depts.setdefault(_norm(d), d)
    except Exception:
        logger.debug("cascade: membership depts skipped", exc_info=True)
    for d in existing:
        if _norm(d) not in ("", "без отдела"):
            depts.setdefault(_norm(d), d)
    return sorted(depts.values())[:_MAX_DEPTS]


async def propose_department_goals(
    user_id: str, *, goal_id: Optional[str] = None,
    goal_text: Optional[str] = None,
) -> Dict[str, Any]:
    """Черновики целей отделов для цели компании. НИЧЕГО не записывает."""
    out: Dict[str, Any] = {"goal_id": goal_id, "goal": "", "proposals": [],
                           "note": ""}
    # цель: по id из трекера либо произвольным текстом
    goal = None
    try:
        from backend.core.goals.goal_tracker import goal_store_for_user
        store = goal_store_for_user(user_id)
        if goal_id:
            goal = store.get_goal(goal_id)
        existing_goals = store.list_goals(status="active")
    except Exception:
        logger.debug("cascade: goal store skipped", exc_info=True)
        existing_goals = []
    text = (str(goal.get("title") or "") + " "
            + str(goal.get("description") or "")).strip() if goal else \
        str(goal_text or "").strip()
    if not text:
        out["note"] = "цель не найдена: передайте goal_id из трекера или goal_text"
        return out
    out["goal"] = text[:400]

    existing: Dict[str, List[str]] = {}
    for g in existing_goals:
        if g.get("level") == "department":
            existing.setdefault(str(g.get("department") or "без отдела"),
                                []).append(str(g.get("title") or ""))

    departments = await _org_departments(user_id, existing)
    if not departments:
        out["note"] = ("в компании нет отделов: назначьте участникам отделы "
                       "во вкладке «Организация» — тогда каскад заработает")
        return out

    try:
        from backend.core.llm.router import LLMRouter, ModelTier
        raw = await LLMRouter().generate(
            prompt=_propose_prompt(text, departments, existing),
            model_tier=ModelTier.STANDARD, max_tokens=1200)
    except Exception as e:  # noqa: BLE001
        logger.warning("cascade propose LLM failed: %s", e)
        out["note"] = "LLM недоступна — попробуйте позже"
        return out

    out["proposals"] = _parse_proposals(raw, departments, text)
    if not out["proposals"]:
        out["note"] = ("предложений нет: либо цель не раскладывается на эти "
                       "отделы, либо ответ модели не прошёл валидацию")
    return out


def accept_proposal(
    user_id: str, *, department: str, title: str,
    company_goal_id: str = "", rationale: str = "",
) -> Dict[str, Any]:
    """Принять черновик: создать department-цель в goal_tracker (human gate —
    вызывается ТОЛЬКО по явному действию руководителя в UI)."""
    department = str(department or "").strip()
    title = str(title or "").strip()
    if not (department and title):
        return {"ok": False, "error": "department и title обязательны"}
    try:
        from backend.core.goals.goal_tracker import goal_store_for_user
        store = goal_store_for_user(user_id)
        goal = store.add_goal(
            title=title, level="department", department=department,
            description=rationale or "", parent_id=company_goal_id or "",
        )
        return {"ok": True, "goal_id": goal.get("id")}
    except Exception as e:  # noqa: BLE001
        logger.warning("cascade accept failed: %s", e)
        return {"ok": False, "error": str(e)}
