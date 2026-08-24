# -*- coding: utf-8 -*-
"""
Wisdom → критерии приёмки ТЗ (CAPABILITY_READINESS C9).

ЗАЧЕМ: критерий «хорошо для ЭТОЙ компании» зависит от того, как компания
исторически решала похожие задачи (паттерны успеха в WisdomEngine). Сейчас
spec_generator._generate_acceptance_criteria даёт «общие» критерии без
привязки к опыту. Этот модуль обогащает их строками из готового
`build_wisdom_brief` (он уже умеет собирать «опыт + границы not_law +
калибровку оптимизма»).

ДИЗАЙН (скелет в коде, БЕЗ доп. LLM-вызова):
- enrich_criteria_with_wisdom — чистая функция, тестируется без инфры;
- получает brief как СПИСОК СТРОК (build_wisdom_brief возвращает именно
  такой формат) и встраивает в JSON-критерии как новый блок
  `company_wisdom`: рекомендация → soft criterion, границы → hard rules,
  калибровка → assumption-checker;
- старые поля не трогаем (functional/quality/business/user/sign_off) —
  только добавляем секцию.
- ЧЕСТНОСТЬ §1: пустой brief → criteria возвращаем как есть (не выдумываем).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def _classify_line(line: str) -> Optional[str]:
    """Грубая, но детерминированная классификация строки brief'а
    по маркерам, которые ставит build_wisdom_brief сам."""
    if not line:
        return None
    if "💡 ОПЫТ КОМПАНИИ" in line:
        return "soft"      # рекомендация на основе опыта
    if "⚠️ ГРАНИЦА" in line:
        return "hard"      # not_law границы — обязательны к проверке
    if "⚠️ КАЛИБРОВКА" in line:
        return "calibration"
    # неизвестный маркер: считаем мягким (без ужесточения)
    return "soft"


def _strip_marker(line: str) -> str:
    """Убрать эмодзи-метку из начала, оставить смысл."""
    for marker in ("💡 ОПЫТ КОМПАНИИ:", "⚠️ ГРАНИЦА (из прошлых решений):",
                   "⚠️ КАЛИБРОВКА:"):
        if line.startswith(marker):
            return line[len(marker):].strip()
    return line.strip()


def enrich_criteria_with_wisdom(
    criteria: Optional[Dict[str, Any]],
    wisdom_brief: Optional[List[str]],
) -> Dict[str, Any]:
    """Вернуть копию criteria с добавленной секцией `company_wisdom`.

    company_wisdom: {
        "soft_criteria":    [ "...", ... ],   # «учитывать опыт: ...»
        "hard_rules":       [ "...", ... ],   # not_law границы — обязательны
        "calibration_notes":[ "...", ... ],   # «закладывать консервативные оценки»
    }
    Пустой brief / отсутствующий → criteria возвращается без секции.
    """
    base: Dict[str, Any] = dict(criteria or {})
    if not wisdom_brief:
        return base

    soft, hard, calib = [], [], []
    for line in wisdom_brief:
        if not isinstance(line, str) or not line.strip():
            continue
        kind = _classify_line(line)
        clean = _strip_marker(line)
        if not clean:
            continue
        if kind == "soft":
            soft.append(clean)
        elif kind == "hard":
            hard.append(clean)
        elif kind == "calibration":
            calib.append(clean)

    if not (soft or hard or calib):
        return base

    base["company_wisdom"] = {
        "soft_criteria": soft,
        "hard_rules": hard,
        "calibration_notes": calib,
        "_note": ("Критерии «хорошо для ЭТОЙ компании» из опыта прошлых решений. "
                  "soft — учесть; hard — соблюсти обязательно; calibration — "
                  "осторожность в оценках."),
    }
    return base


async def fetch_wisdom_brief_safe(user_id: str, situation: str) -> List[str]:
    """Безопасный фасад над WisdomEngine.build_wisdom_brief — нужен врезке в
    spec_generator. Любая ошибка → []."""
    if not user_id or not situation:
        return []
    try:
        from backend.core.wisdom.wisdom_engine import get_wisdom_engine
        engine = await get_wisdom_engine()
        return await engine.build_wisdom_brief(
            user_id=user_id, situation=situation[:1000]) or []
    except Exception:
        return []
