# -*- coding: utf-8 -*-
"""Выгрузка собственного слепка — сотрудник забирает свои данные себе.

Зачем. Слепок человека до сих пор нельзя было забрать: GDPR-экспорт отдаёт
профиль настроек, датасет для ИИ-копии выгружается от лица владельца
тенанта, а сам сотрудник — субъект всех этих данных — не имел ни одной
кнопки «скачать своё». Это первый шаг сценария переносимого слепка:
при увольнении человек уносит картину своего роста, а не резюме.

Что входит (по решению владельца продукта): как работал над проектами,
что получалось и что шло с трудом, опыт, достижения и результаты,
психологический профиль, отчёт по сотруднику, RPG-портрет.

Что НЕ входит — «совсем конкретные значения»: суммы денег, финансовые
показатели компании. Слепок построен из встреч, где звучали бюджеты и
цифры клиентов; это данные компании, а не человека. Поэтому все денежные
суммы затираются, а финансовые KPI не включаются вовсе. Затирание здесь
уместно (в отличие от защиты по грифу): это вежливая чистка собственной
выгрузки, а не барьер против злоумышленника.

Формат версионируется (`format`), чтобы будущая принимающая сторона могла
понять, что перед ней. Подписи пока нет — это честно указано в самой
выгрузке: без подписи документ подтверждает содержание, но не происхождение.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

EXPORT_FORMAT = "tessent-person-export/v1"

# Денежная сумма: число + (масштаб и/или валюта) в любом порядке рядом.
# «2,5 млн ₽», «1 200 000 руб», «$50k», «бюджет 500 тыс».
_MONEY_RX = re.compile(
    r"(?:[$€₽]\s*)?\d[\d\s  '’.,]*\d?\s*(?:млрд|млн|тыс(?:\.|яч\w*)?|k\b|K\b)?\s*"
    r"(?:₽|руб\w*|\brub\b|\$|\busd\b|доллар\w*|€|\beur\b|евро)"
    r"|(?:[$€₽])\s*\d[\d\s  '’.,]*\d?\s*(?:млрд|млн|тыс\.?|k|K)?",
    re.IGNORECASE,
)
_REDACTED = "[сумма скрыта]"


def redact_money(text: Any) -> str:
    """Затереть денежные суммы в свободном тексте.

    Числа без валюты рядом (проценты, счёт задач, дедлайны) не трогаем:
    «закрыл 14 задач» и «вырос на 20%» — это результаты человека, а не
    финансы компании.
    """
    s = str(text or "")
    if not s:
        return s
    return _MONEY_RX.sub(_REDACTED, s)


def _redact_deep(value: Any) -> Any:
    """Рекурсивно затереть суммы во всех строках структуры."""
    if isinstance(value, str):
        return redact_money(value)
    if isinstance(value, dict):
        return {k: _redact_deep(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_deep(v) for v in value]
    return value


def _clean_list(items: Any, limit: int = 20) -> List[Any]:
    out = []
    for it in items or []:
        if it in (None, "", {}):
            continue
        out.append(_redact_deep(it))
        if len(out) >= limit:
            break
    return out


async def build_self_export(
    *,
    user_id: str,
    person_id: str,
    include_report: bool = True,
    include_portrait: bool = True,
) -> Dict[str, Any]:
    """Собрать выгрузку слепка для самого человека.

    Каждый блок best-effort: недоступный источник даёт честную пометку в
    `sections_missing`, а не роняет выгрузку целиком — человек забирает то,
    что есть, и видит, чего нет.
    """
    now = datetime.now(timezone.utc).isoformat()
    export: Dict[str, Any] = {
        "format": EXPORT_FORMAT,
        "generated_at": now,
        "person_id": person_id,
        "disclaimer": (
            "Выгрузка собрана автоматически из рабочих встреч и задач. "
            "Денежные суммы и финансовые показатели компании затёрты — "
            "это данные компании, а не человека. Документ не подписан "
            "криптографически: он подтверждает содержание, но не "
            "происхождение."
        ),
        "sections_missing": [],
    }

    # ── Карточка человека: проекты, достижения, сложности, психопрофиль ──
    snap = None
    try:
        from backend.core.sleep.enhanced_snapshot import (
            get_enhanced_snapshot_generator,
        )
        gen = get_enhanced_snapshot_generator(user_id=user_id)
        snap = await gen.get_person_snapshot(person_id)
    except Exception as exc:
        logger.warning("self_export: snapshot unavailable: %s", exc)
    if snap is None:
        export["sections_missing"].append("snapshot")
    else:
        export["name"] = getattr(snap, "name", "") or ""
        export["role"] = redact_money(getattr(snap, "role", "") or "")
        export["department"] = getattr(snap, "department", "") or ""
        # Как работал над проектами
        export["projects"] = _clean_list(getattr(snap, "current_projects", None))
        export["responsibilities"] = _clean_list(
            getattr(snap, "responsibilities", None))
        # Что получалось и что шло с трудом
        export["achievements"] = _clean_list(
            getattr(snap, "recent_achievements", None))
        export["challenges"] = _clean_list(
            getattr(snap, "current_challenges", None))
        export["strengths"] = _clean_list(getattr(snap, "strengths", None))
        export["growth_areas"] = _clean_list(
            getattr(snap, "areas_for_improvement", None))
        # Результаты — счётные, без денег
        export["activity"] = {
            "decisions_made": getattr(snap, "decisions_made", 0) or 0,
            "meetings_participated": getattr(snap, "meetings_participated", 0) or 0,
            "tasks_completed_week": getattr(snap, "tasks_completed_week", 0) or 0,
            "collaboration_score": getattr(snap, "collaboration_score", 0) or 0,
        }
        export["activity_summary"] = redact_money(
            getattr(snap, "activity_summary", "") or "")
        # Психологический профиль (из встреч, с потолком уверенности)
        psych = getattr(snap, "psychological", None) or {}
        if isinstance(psych, dict) and psych:
            export["psychological_profile"] = _redact_deep(psych)
        else:
            export["sections_missing"].append("psychological_profile")
        # Опыт: решения и идеи человека (тексты, не суммы)
        export["decisions"] = _clean_list(getattr(snap, "decisions", None))
        export["ideas"] = _clean_list(getattr(snap, "ideas", None))

    # ── Отчёт по сотруднику (детерминированный) ──────────────────────────
    if include_report and snap is not None and export.get("name"):
        try:
            from backend.core.reports.employee_report import build_employee_report
            report = await build_employee_report(user_id, export["name"])
            text = report.get("markdown") if isinstance(report, dict) else report
            export["employee_report"] = redact_money(str(text or ""))
        except Exception as exc:
            logger.warning("self_export: employee report unavailable: %s", exc)
            export["sections_missing"].append("employee_report")
    elif include_report:
        export["sections_missing"].append("employee_report")

    # ── RPG-портрет (LLM, best-effort) ───────────────────────────────────
    if include_portrait and snap is not None:
        try:
            from backend.core.board.character_portrait import extract_character_data
            source = "\n".join(filter(None, [
                export.get("activity_summary", ""),
                "Достижения: " + "; ".join(map(str, export.get("achievements", []))),
                "Сложности: " + "; ".join(map(str, export.get("challenges", []))),
            ]))
            data = await extract_character_data(user_id, source)
            if data:
                # Своя выгрузка — sensitive-фильтр аудитории не нужен:
                # человек имеет право видеть о себе всё, включая «на грани
                # перегрузки». Вырезать это из ЕГО ЖЕ данных было бы цензурой.
                export["rpg_portrait"] = _redact_deep(data)
            else:
                export["sections_missing"].append("rpg_portrait")
        except Exception as exc:
            logger.warning("self_export: portrait unavailable: %s", exc)
            export["sections_missing"].append("rpg_portrait")
    elif include_portrait:
        export["sections_missing"].append("rpg_portrait")

    # Финансовые KPI не включаются вовсе — даже затёртыми. Отсутствие
    # раздела надёжнее регулярного выражения.
    return export
