# -*- coding: utf-8 -*-
"""Пульс-отчёт руководителю — сводка сигналов, а не список задач.

Аксиома A4 (docs/EXECUTION_PULSE.md): руководитель не будет читать список.
Отчёт ведёт с того, что требует вмешательства, остальное — счётчиками.
Детеминированный (ни одного LLM-вызова): каждая строка — факт реестра,
выдумывать здесь нечему. Сохраняется в историю отчётов; доставка — тем же
notify_user (Telegram + email), что и советы Гида.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_MAX_LINES = 7   # строк на секцию в сообщении; остальное — счётчиком


def _tr(key: str, locale: Any = None, **vars: Any) -> str:
    """Строка отчёта на языке получателя. Отчёт собирается шаблонами в коде
    (ни одного LLM-вызова), поэтому переводится каталогами бэкенда, а не
    инструкцией модели. Ночная джоба запроса не имеет — язык приходит
    параметром, см. build_pulse_report."""
    from backend.core.i18n import Locale, t as _t
    loc = Locale.from_string(locale) if isinstance(locale, str) else locale
    return _t(f"pulse.{key}", locale=loc, **vars)


def _line(t: Dict[str, Any], note: str = "", locale: Any = None) -> str:
    title = str(t.get("title") or _tr("untitled", locale)).strip()[:90]
    owner = str(t.get("assignee") or t.get("assignee_name") or "").strip()
    bits = [f"• {title}"]
    if owner:
        bits.append(f"— {owner}")
    if note:
        bits.append(f"({note})")
    return " ".join(bits)


def _section(title: str, rows: List[str], total: int,
             max_lines: int = _MAX_LINES, locale: Any = None) -> str:
    if not total:
        return ""
    head = f"**{title} ({total})**"
    body = "\n".join(rows[:max_lines])
    more = ("\n" + _tr("more", locale, count=total - max_lines)
            if total > max_lines else "")
    return f"{head}\n{body}{more}"


def compose_markdown(analysis: Dict[str, Any], *,
                     postponed: List[Dict[str, Any]] | None = None,
                     goals: Dict[str, Any] | None = None,
                     max_lines: int = _MAX_LINES,
                     locale: Any = None) -> str:
    """locale — язык получателя отчёта (код или Locale); None = язык
    запроса/дефолт стенда."""
    s = analysis["signals"]
    parts: List[str] = ["## " + _tr("heading", locale)]

    # 1. Требует вмешательства — самое главное, первым
    urgent: List[str] = []
    for t in s["overdue"]:
        urgent.append(_line(t, _tr("note_overdue", locale,
                                   days=t.get("_overdue_days", "?")), locale))
    for t in s["blocked"]:
        urgent.append(_line(t, _tr("note_blocked", locale), locale))
    sec = _section(_tr("sec_urgent", locale),
                   urgent, len(s["overdue"]) + len(s["blocked"]),
                   max_lines=max_lines, locale=locale)
    if sec:
        parts.append(sec)

    # 2. Дыры постановки: без срока / без владельца
    holes = ([_line(t, _tr("note_no_deadline", locale), locale)
              for t in s["no_deadline"]]
             + [_line(t, _tr("note_no_owner", locale), locale)
                for t in s["no_owner"]])
    sec = _section(_tr("sec_holes", locale),
                   holes, len(s["no_deadline"]) + len(s["no_owner"]),
                   max_lines=max_lines, locale=locale)
    if sec:
        parts.append(sec)

    # 3. Висит без движения
    sec = _section(_tr("sec_stale", locale),
                   [_line(t, _tr("note_idle", locale,
                                 days=t.get("_idle_days", "?")), locale)
                    for t in s["stale"]], len(s["stale"]),
                   max_lines=max_lines, locale=locale)
    if sec:
        parts.append(sec)

    # 4. Скоро дедлайн
    sec = _section(_tr("sec_deadline_soon", locale),
                   [_line(t, locale=locale) for t in s["deadline_soon"]],
                   len(s["deadline_soon"]), max_lines=max_lines, locale=locale)
    if sec:
        parts.append(sec)

    # 4б. Сверка встреча↔задачник: прозвучало на встрече, в трекер не попало
    if s.get("not_in_tracker"):
        parts.append(_section(
            _tr("sec_not_in_tracker", locale),
            [_line(t, _tr("note_not_in_tracker", locale), locale)
             for t in s["not_in_tracker"]], len(s["not_in_tracker"]),
            max_lines=max_lines, locale=locale))

    # 5. Переносы сроков (история deadline_tracker)
    if postponed:
        rows = [_line(t, _tr("note_postponed", locale,
                             frm=t.get("_from", "?"), to=t.get("_to", "?")),
                      locale) for t in postponed]
        parts.append(_section(_tr("sec_postponed", locale), rows,
                              len(postponed), max_lines=max_lines,
                              locale=locale))

    # 6. Отложено сознательно — не трогаем, но видно
    if s["deferred"]:
        parts.append(_section(_tr("sec_deferred", locale),
                              [_line(t, locale=locale) for t in s["deferred"]],
                              len(s["deferred"]), max_lines=max_lines,
                              locale=locale))

    # 7. Перегруз
    if analysis["overloaded"]:
        rows = [_tr("row_overloaded", locale, owner=o["owner"], open=o["open"])
                for o in analysis["overloaded"]]
        parts.append(_section(_tr("sec_overloaded", locale), rows,
                              len(analysis["overloaded"]),
                              max_lines=max_lines, locale=locale))

    # 8. Цели недели (если модуль целей ведётся)
    if goals and goals.get("goals_total"):
        parts.append(
            _tr("goals", locale, forward=goals.get("moved_forward", 0),
                stalled=goals.get("stalled", 0),
                regressed=goals.get("regressed", 0))
            + (_tr("goals_blocked", locale,
                   blocked=goals.get("blocked_goals", 0))
               if goals.get("blocked_goals") else ""))

    # 9. Цифры
    c = analysis["counts"]
    parts.append(_tr("totals", locale, open=analysis["open_total"],
                     done=analysis["done_total"], overdue=c["overdue"],
                     no_deadline=c["no_deadline"], no_owner=c["no_owner"],
                     stale=c["stale"]))

    if len(parts) == 2 and analysis["open_total"] == 0:
        parts.append(_tr("empty", locale))
    return "\n\n".join(p for p in parts if p)


async def build_pulse_report(user_id: str, *, deliver: bool = False,
                             full: bool = False) -> Dict[str, Any]:
    """Собрать пульс-отчёт: реестр → детекторы → markdown → история отчётов.

    deliver=True — дополнительно отправить владельцу (TG/email через
    notify_user). full=True — БЕЗ обрезки секций («…и ещё N»): полный
    список задач каждой секции (для просмотра в UI; доставка и история
    остаются компактными — руководитель не будет читать список в TG)."""
    from backend.core.pulse.detectors import detect_signals
    from backend.core.pulse.ledger import build_ledger

    ledger = await build_ledger(user_id)
    # сверка встреча↔задачник имеет смысл, только когда трекер реально
    # отдаёт задачи — иначе «не в трекере» было бы у всех подряд
    trackers = any(str(t.get("tracker") or "").strip()
                   for t in ledger["tasks"])
    analysis = detect_signals(ledger["tasks"], trackers_configured=trackers)

    postponed: List[Dict[str, Any]] = []
    try:
        from backend.core.reports.deadline_tracker import postponed_tasks
        postponed = postponed_tasks(user_id, ledger["tasks"]) or []
    except Exception:
        logger.debug("pulse: переносы сроков пропущены", exc_info=True)

    goals: Dict[str, Any] = {}
    try:
        from backend.core.goals.goal_tracker import weekly_review
        goals = await weekly_review(user_id, with_llm=False) or {}
    except Exception:
        logger.debug("pulse: обзор целей пропущен", exc_info=True)

    # язык получателя: локаль запроса (UI) → сохранённый язык человека →
    # дефолт стенда. Ночная джоба запроса не имеет — иначе англоязычный
    # руководитель получал бы русский пульс в Telegram
    from backend.core.llm.lang import resolve_answer_lang
    lang = await resolve_answer_lang(user_id)
    md = compose_markdown(analysis, postponed=postponed, goals=goals,
                          max_lines=10_000 if full else _MAX_LINES,
                          locale=lang)

    report_id = str(uuid.uuid4())
    try:
        from backend.core.reports.methodology_service import (
            report_store_for_user,
        )
        report_store_for_user(user_id).add_report({
            "id": report_id,
            "report_type": "execution_pulse",
            "title": _tr("title", lang),
            "icon": "🫀",
            "content_text": md,
            "summary": md.split("\n\n")[-1][:400],
            "context_key": "execution_pulse",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        logger.warning("pulse: отчёт не сохранился в историю", exc_info=True)

    delivered = {}
    if deliver:
        try:
            from backend.core.help.advice_push import notify_user
            delivered = await notify_user(
                user_id, subject=_tr("title", lang), text=md)
        except Exception:
            logger.warning("pulse: доставка не удалась", exc_info=True)

    return {"status": "success", "id": report_id, "markdown": md,
            "counts": analysis["counts"], "open_total": analysis["open_total"],
            "done_total": analysis["done_total"],
            "events": ledger["events"], "ledger_stats": ledger["stats"],
            "delivered": delivered}
