# -*- coding: utf-8 -*-
"""Итоги встречи: какие задачи были выполнены и зачем.

Агрегирует handoff-записи пользователя, привязанные к встрече
(source.kind == "meeting"), в один читаемый markdown-отчёт:
задача → владелец → статус → вердикт судьи → созданные файлы → «зачем»
(цель из ТЗ). Дальше отчёт рендерится существующим экспортом
(HTML/PDF/DOCX/PPTX) — тот же путь, что у «Готового документа».

Детерминированная сборка из реальных записей (без LLM): ничего не
придумывает, поля отсутствуют → строка пропускается.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_STATUS_RU = {
    "done": "✅ выполнена",
    "failed": "❌ ошибка исполнения",
    "running": "⏳ выполняется",
    "pending_confirmation": "🕐 ждёт подтверждения",
    "rejected": "🚫 отклонена",
}


def _goal_from_spec(rec: Dict[str, Any]) -> str:
    """«Зачем»: краткая цель из ТЗ. Берём первый содержательный абзац
    (после заголовков/служебных строк), обрезаем до ~280 символов."""
    spec = rec.get("spec_text") or ""
    if not spec and rec.get("spec_file"):
        try:
            with open(rec["spec_file"], "r", encoding="utf-8") as f:
                spec = f.read()
        except OSError:
            spec = ""
    # директиву-обёртку исполнителя не считаем целью
    if "===== ТЗ =====" in spec:
        spec = spec.split("===== ТЗ =====", 1)[1]
    for para in re.split(r"\n\s*\n", spec):
        p = para.strip()
        if not p or p.startswith("#") or p.startswith("**Дата"):
            continue
        p = re.sub(r"^\*\*Задача:\*\*\s*", "", p)
        p = re.sub(r"\s+", " ", p)
        if len(p) >= 20:
            return p[:280] + ("…" if len(p) > 280 else "")
    return ""


def build_meeting_summary(user_id: str, meeting_id: str = "",
                          meeting_title: str = "") -> Dict[str, Any]:
    """Собрать сводку по задачам встречи из handoff-записей.

    meeting_id ИЛИ meeting_title — что есть у вызывающего (совпадение по id
    точное, по названию — регистронезависимое). Возвращает
    {markdown, stats, tasks}; встреча без handoff'ов → пустой отчёт с
    честной пометкой.
    """
    from backend.core.tasks.handoff_store import HandoffStore

    store = HandoffStore(user_id)
    # list() режет spec_text → для «зачем» берём полные записи через get()
    rows = store.list(limit=200)
    matched: List[Dict[str, Any]] = []
    title_seen = meeting_title
    for r in rows:
        src = r.get("source") or {}
        if not (isinstance(src, dict) and src.get("kind") == "meeting"):
            continue
        if meeting_id and str(src.get("meeting_id") or "") != str(meeting_id):
            continue
        if not meeting_id and meeting_title and \
                (src.get("meeting_title") or "").strip().lower() != \
                meeting_title.strip().lower():
            continue
        full = store.get(r.get("id") or "") or r
        matched.append(full)
        title_seen = src.get("meeting_title") or title_seen

    tasks: List[Dict[str, Any]] = []
    done = failed = in_progress = 0
    for rec in matched:
        st = rec.get("status") or ""
        if st == "done":
            done += 1
        elif st in ("failed", "rejected"):
            failed += 1
        else:
            in_progress += 1
        tasks.append({
            "title": rec.get("task_title") or "(без названия)",
            "assignee": rec.get("assignee") or "",
            "agent": rec.get("agent") or "",
            "status": st,
            "status_ru": _STATUS_RU.get(st, st),
            "verdict": rec.get("verify_verdict") or "",
            "verify_summary": rec.get("verify_summary") or "",
            "goal": _goal_from_spec(rec),
            "files": [a.get("name") for a in (rec.get("artifacts") or [])],
            "tracker": rec.get("tracker") or "",
            "handoff_id": rec.get("id") or "",
        })

    header = f"# Итоги встречи{f' «{title_seen}»' if title_seen else ''}"
    lines = [header, ""]
    if not tasks:
        lines.append("_По этой встрече нет задач, отданных исполнителям._")
    else:
        lines += [
            f"**Задач в работе исполнителей:** {len(tasks)} · "
            f"выполнено: {done} · с ошибкой/отклонено: {failed} · "
            f"в процессе: {in_progress}",
            "",
            "| Задача | Владелец | Статус | Вердикт | Файлы |",
            "|---|---|---|---|---|",
        ]
        for t in tasks:
            lines.append(
                f"| {t['title']} | {t['assignee'] or '—'} | {t['status_ru']} "
                f"| {t['verdict'] or '—'} | "
                f"{', '.join(t['files']) or '—'} |")
        lines.append("")
        lines.append("## Подробно: что и зачем делалось")
        for i, t in enumerate(tasks, 1):
            lines.append(f"### {i}. {t['title']}")
            if t["goal"]:
                lines.append(f"**Зачем:** {t['goal']}")
            if t["assignee"]:
                lines.append(f"**Владелец:** {t['assignee']}")
            lines.append(f"**Статус:** {t['status_ru']}"
                         + (f" · агент: {t['agent']}" if t["agent"] else ""))
            if t["verify_summary"]:
                lines.append(f"**Проверка:** {t['verify_summary']}")
            if t["files"]:
                lines.append("**Готовые файлы:** " + ", ".join(t["files"]))
            if t["tracker"]:
                lines.append(f"**Задачник:** {t['tracker']}")
            lines.append("")

    return {
        "meeting_id": meeting_id,
        "meeting_title": title_seen,
        "markdown": "\n".join(lines),
        "stats": {"total": len(tasks), "done": done, "failed": failed,
                  "in_progress": in_progress},
        "tasks": tasks,
    }
