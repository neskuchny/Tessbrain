# -*- coding: utf-8 -*-
"""Отчёт по сотруднику: встречи + задачи + загрузка (клиентский запрос).

Сводит УЖЕ существующие источники в один взгляд на человека:
- встречи — PersonSnapshot (граф: рёбра PARTICIPATED_IN → recent_meetings,
  meetings_participated, роль на встрече);
- задачи — граф (Task, извлечённые из встреч) + задачники (Trello/Jira/
  YouGile через collect_tasks_from_trackers), фильтр по assignee;
- ПРОСРОЧКА — вычисляется здесь (deadline vs сегодня): раньше «overdue»
  существовал только если статус буквально так назывался;
- активность — collaboration_score / decisions из снапшота.

Детерминированная сборка из реальных данных (без LLM): чего нет — честно
пропускается или помечается. Рендер в HTML/PDF — существующим экспортом.

Известные ограничения (дорожная карта, не скрываем):
- «проведённые» vs «посещённые» встречи не различаются (organizer-флага нет
  в PARTICIPATED_IN);
- длительность встреч не хранится;
- «переносит задачу» — нет детектора сдвига дедлайнов (есть только
  cross-meeting упоминания);
- CRM-сделки по менеджеру не подтянуты (коннекторы отдают owner_id, но
  маппинга owner→Person нет).
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DATE_PATTERNS = (
    ("%Y-%m-%d", re.compile(r"\b(\d{4}-\d{2}-\d{2})")),
    ("%d.%m.%Y", re.compile(r"\b(\d{2}\.\d{2}\.\d{4})")),
    ("%d/%m/%Y", re.compile(r"\b(\d{2}/\d{2}/\d{4})")),
)


def _parse_deadline(raw: Any) -> Optional[date]:
    """Дедлайны в источниках — свободные строки; выцепляем дату best-effort.
    Не распарсилось → None (никогда не гадаем)."""
    s = str(raw or "").strip()
    if not s:
        return None
    for fmt, rx in _DATE_PATTERNS:
        m = rx.search(s)
        if m:
            try:
                return datetime.strptime(m.group(1), fmt).date()
            except ValueError:
                continue
    return None


def _assigned_to(task: Dict[str, Any], person: str) -> bool:
    """Задача назначена на человека? Совпадение по подстроке имени в обе
    стороны, без регистра (в источниках имя пишут по-разному)."""
    p = person.strip().lower()
    if not p:
        return False
    who = str(task.get("assignee") or task.get("assignee_name")
              or task.get("owner") or "").strip().lower()
    if not who:
        return False
    return p in who or who in p


async def build_employee_report(user_id: str, person: str,
                                days: int = 30) -> Dict[str, Any]:
    """Собрать отчёт по сотруднику. person — имя или person_id."""
    from backend.core.tasks.task_analysis import (
        _bucket,
        collect_tasks,
        collect_tasks_from_trackers,
    )

    today = date.today()
    since = today - timedelta(days=max(1, days))

    # ── Встречи (граф, PARTICIPATED_IN) ────────────────────────────────
    snap = None
    try:
        from backend.core.sleep.enhanced_snapshot import (
            get_enhanced_snapshot_generator,
        )
        gen = get_enhanced_snapshot_generator(user_id=user_id)
        snap = await gen.get_person_snapshot(person)
    except Exception as e:
        logger.warning(f"employee report: person snapshot failed: {e}")

    display_name = getattr(snap, "name", "") or person
    meetings_total = int(getattr(snap, "meetings_participated", 0) or 0)
    recent_meetings = list(getattr(snap, "recent_meetings", []) or [])
    meetings_in_period = []
    for m in recent_meetings:
        d = _parse_deadline((m or {}).get("date"))
        if d is None or d >= since:      # дата не заполнена → показываем
            meetings_in_period.append(m)

    # ── Задачи (граф + задачники) ──────────────────────────────────────
    tasks: List[Dict[str, Any]] = []
    try:
        tasks += await collect_tasks(user_id)
    except Exception as e:
        logger.warning(f"employee report: graph tasks failed: {e}")
    try:
        tasks += await collect_tasks_from_trackers(user_id)
    except Exception as e:
        logger.warning(f"employee report: tracker tasks failed: {e}")
    mine = [t for t in tasks if _assigned_to(t, display_name)
            or _assigned_to(t, person)]

    # История дедлайнов: фиксируем текущие значения (по ВСЕМ задачам —
    # история общая) и вычисляем переносы для задач этого человека.
    postponed: List[Dict[str, Any]] = []
    try:
        from backend.core.reports.deadline_tracker import (
            postponed_tasks,
            record_deadlines,
        )
        record_deadlines(user_id, tasks)
        postponed = postponed_tasks(user_id, mine)
    except Exception as e:
        logger.warning(f"employee report: deadline tracker failed: {e}")

    buckets: Dict[str, List[Dict[str, Any]]] = {
        "done": [], "in_progress": [], "blocked": [], "todo": []}
    overdue: List[Dict[str, Any]] = []
    for t in mine:
        b = _bucket(str(t.get("status") or ""))
        buckets.setdefault(b, []).append(t)
        dl = _parse_deadline(t.get("deadline") or t.get("due_date"))
        if dl and dl < today and b != "done":
            overdue.append({**t, "_deadline": dl.isoformat()})

    # ── Markdown ───────────────────────────────────────────────────────
    role = getattr(snap, "role", "") or ""
    dept = getattr(snap, "department", "") or ""
    lines = [f"# Отчёт по сотруднику: {display_name}", ""]
    sub = " · ".join(x for x in (role, dept) if x)
    if sub:
        lines += [f"*{sub}*", ""]
    lines += [
        f"**Период:** последние {days} дней (до {today.isoformat()})", "",
        "## Встречи",
        f"- Участие во встречах (всего в графе): **{meetings_total}**",
    ]
    if meetings_in_period:
        lines.append(f"- Недавние встречи ({len(meetings_in_period)}):")
        for m in meetings_in_period[:10]:
            t_ = (m or {}).get("title") or "—"
            d_ = (m or {}).get("date") or ""
            r_ = (m or {}).get("role_in_meeting") or ""
            lines.append("  - " + " · ".join(x for x in (t_, d_, r_) if x))
    lines += [
        "",
        "## Задачи",
        "| Статус | Кол-во |",
        "|---|---|",
        f"| ✅ Выполнено | {len(buckets['done'])} |",
        f"| 🔄 В работе | {len(buckets['in_progress'])} |",
        f"| ⛔ Заблокировано | {len(buckets['blocked'])} |",
        f"| 📋 К выполнению | {len(buckets['todo'])} |",
        f"| ⏰ **Просрочено (дедлайн < сегодня)** | **{len(overdue)}** |",
        f"| 🔁 Переносились (сдвиг дедлайна) | {len(postponed)} |",
        "",
    ]
    if postponed:
        lines.append("### 🔁 Задачи с переносом срока")
        for t in postponed[:10]:
            lines.append(f"- **{t.get('title') or '—'}** — переносов: "
                         f"{t['_shifts']} ({t['_from']} → {t['_to']}) · "
                         f"{t.get('source') or t.get('tracker') or 'граф'}")
        lines.append("")
    if overdue:
        lines.append("### ⏰ Просроченные задачи")
        for t in overdue[:15]:
            lines.append(f"- **{t.get('title') or '—'}** — дедлайн "
                         f"{t['_deadline']} · источник: "
                         f"{t.get('source') or t.get('tracker') or 'граф'}")
        lines.append("")
    for key, label in (("in_progress", "🔄 В работе"),
                       ("blocked", "⛔ Заблокировано"),
                       ("done", "✅ Выполнено (последние)")):
        if buckets[key]:
            lines.append(f"### {label}")
            for t in buckets[key][:10]:
                dl = t.get("deadline") or t.get("due_date") or ""
                lines.append(f"- {t.get('title') or '—'}"
                             + (f" · до {dl}" if dl else "")
                             + f" · {t.get('source') or t.get('tracker') or 'граф'}")
            lines.append("")

    # ── CRM-нагрузка (маппинг owner→человек, все настроенные провайдеры) ─
    crm: Dict[str, Any] = {}
    try:
        from backend.core.reports.crm_workload import crm_workload_for_person
        crm = await crm_workload_for_person(user_id, display_name) or {}
    except Exception as e:
        logger.warning(f"employee report: crm workload failed: {e}")
    if crm.get("providers"):
        lines += [
            "## CRM",
            "| Показатель | Значение |",
            "|---|---|",
            f"| 💼 Сделки в работе | {crm['deals_open']} |",
            f"| ✅ Сделки закрыты | {crm['deals_done']} |",
            f"| 💰 Сумма по сделкам | {round(crm['deals_value']):,} |".replace(",", " "),
            f"| 📋 CRM-задачи открыто | {crm['crm_tasks_open']} |",
            f"| ⏰ CRM-задачи просрочено | {crm['crm_tasks_overdue']} |",
            "",
        ]
        for p in crm["providers"]:
            lines.append(f"- {p['provider']}: сделки {p['deals_open']} в работе / "
                         f"{p['deals_done']} закрыто · задачи {p['tasks_open']} "
                         f"(просрочено {p['tasks_overdue']})")
        lines.append("")
    elif crm.get("notes"):
        lines += ["## CRM", *[f"- {n}" for n in crm["notes"]], ""]

    ach = list(getattr(snap, "recent_achievements", []) or [])
    chal = list(getattr(snap, "current_challenges", []) or [])
    if ach:
        lines += ["## Недавние достижения"] + [f"- {a}" for a in ach[:7]] + [""]
    if chal:
        lines += ["## Текущие сложности"] + [f"- {c}" for c in chal[:7]] + [""]
    score = getattr(snap, "collaboration_score", 0) or 0
    decisions = getattr(snap, "decisions_made", 0) or 0
    lines += [
        "## Активность",
        f"- Решений на встречах: {decisions}",
        f"- Индекс вовлечённости (встречи+задачи+решения+связность): "
        f"{round(float(score))}/100",
        "",
        "## Ограничения данных (честно)",
        "- «Проведённые» и «посещённые» встречи пока не различаются "
        "(организатор не приходит из источника).",
        "- Переносы дедлайнов видны с момента внедрения истории и только "
        "между замерами (отчёт/анализ задач).",
        "- Длительность встреч не приходит из источника — оценка объёма "
        "в часах недоступна.",
    ]
    if not crm.get("providers"):
        lines.append("- CRM-активность не подключена (нужен маппинг "
                     "owner→человек и env-ключи провайдера).")
    lines += [
        "",
        "---",
        "*Сформировано из графа знаний, задачников и снапшотов — только "
        "реальные данные, без домыслов.*",
    ]

    return {
        "person": display_name,
        "markdown": "\n".join(lines),
        "stats": {
            "meetings_total": meetings_total,
            "tasks_done": len(buckets["done"]),
            "tasks_in_progress": len(buckets["in_progress"]),
            "tasks_blocked": len(buckets["blocked"]),
            "tasks_todo": len(buckets["todo"]),
            "tasks_overdue": len(overdue),
            "tasks_postponed": len(postponed),
            "decisions": decisions,
            "activity_score": round(float(score)),
            "crm_deals_open": crm.get("deals_open", 0),
            "crm_deals_done": crm.get("deals_done", 0),
            "crm_deals_value": crm.get("deals_value", 0),
            "crm_tasks_overdue": crm.get("crm_tasks_overdue", 0),
        },
        "overdue": overdue,
        "postponed": postponed,
        "crm": crm,
    }
