# -*- coding: utf-8 -*-
"""«Пульс» (роадмап R-4.2): ритм компании на дистанции.

Ловит то, что человек внутри не видит:
- РЕЦИДИВЫ: одна и та же тема/тип срыва всплывает раз за разом на протяжении
  нескольких недель (и, возможно, с регулярной каденцией — «каждые ~7 дней»).
- СЕЗОННОСТЬ по дню недели: встречи/задачи скапливаются в один день.

Источник — timestamped-узлы графа (Meeting/Task) с их датами и заголовками.
Ключ рецидива = главное ключевое слово заголовка. Без LLM. Best-effort:
пустой граф → пусто, никогда не raises.

Чистый `_compute_pulse(events)` (события `{key, ts}`) юнит-тестируется без БД.
"""
from __future__ import annotations

import logging
import statistics
from datetime import date, datetime
from typing import Any, Optional

from backend.core.help.org_health import _keywords  # переиспользуем разбор слов

logger = logging.getLogger(__name__)

_RECUR_MIN = 3         # ≥ столько повторов темы → рецидив
_RECUR_WEEKS = 2       # ... разбросанных по ≥ стольким разным неделям
_SEASON_MIN = 5        # ≥ столько событий всего → есть смысл искать сезонность
_SEASON_SHARE = 0.45   # ≥ такая доля в одном дне недели → пик
_REGULAR_TOL = 0.5     # разброс интервалов < 50% среднего → «регулярно»

_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг",
             "пятница", "суббота", "воскресенье"]

_DATE_FIELDS = ("date", "meeting_date", "start_time", "created_at",
                "timestamp", "deadline", "due_date")


def _parse_day(ts: Any) -> Optional[date]:
    s = str(ts or "")[:10]
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _compute_pulse(events: list[dict[str, Any]], *,
                   today: Optional[date] = None) -> dict[str, Any]:
    """Рецидивы + сезонность по списку событий `{key, ts}`."""
    parsed: list[tuple[str, date]] = []
    for e in events or []:
        d = _parse_day(e.get("ts"))
        key = str(e.get("key") or "").strip()
        if d and key:
            parsed.append((key, d))
    if not parsed:
        return {"recurring": [], "seasonality": [], "summary": {}}

    # ── Рецидивы: группируем по ключу ──────────────────────────────────────
    by_key: dict[str, list[date]] = {}
    for key, d in parsed:
        by_key.setdefault(key, []).append(d)

    recurring: list[dict[str, Any]] = []
    for key, days in by_key.items():
        days = sorted(days)
        weeks = {(d.isocalendar()[0], d.isocalendar()[1]) for d in days}
        if len(days) < _RECUR_MIN or len(weeks) < _RECUR_WEEKS:
            continue
        gaps = [(days[i + 1] - days[i]).days for i in range(len(days) - 1)]
        avg_gap = round(statistics.mean(gaps), 1) if gaps else 0.0
        regular = (
            len(gaps) >= 2 and avg_gap > 0
            and (statistics.pstdev(gaps) / avg_gap) < _REGULAR_TOL
        )
        recurring.append({
            "key": key,
            "count": len(days),
            "weeks": len(weeks),
            "avg_gap_days": avg_gap,
            "regular": regular,
            "last": days[-1].isoformat(),
        })
    recurring.sort(key=lambda x: (-x["count"], -x["weeks"]))

    # ── Сезонность по дню недели (по всем событиям) ────────────────────────
    seasonality: list[dict[str, Any]] = []
    all_days = [d for _k, d in parsed]
    if len(all_days) >= _SEASON_MIN:
        hist = [0] * 7
        for d in all_days:
            hist[d.weekday()] += 1
        total = len(all_days)
        peak_wd = max(range(7), key=lambda i: hist[i])
        share = hist[peak_wd] / total
        if share >= _SEASON_SHARE:
            seasonality.append({
                "weekday": _WEEKDAYS[peak_wd],
                "share": round(share, 2),
                "count": hist[peak_wd],
                "scope": "все события",
            })

    return {
        "recurring": recurring,
        "seasonality": seasonality,
        "summary": {"recurring": len(recurring), "seasonal": len(seasonality)},
    }


def _events_from_graph(g: Any) -> list[dict[str, Any]]:
    """Собрать события `{key, ts, label}` из timestamped-узлов графа."""
    events: list[dict[str, Any]] = []
    if g is None:
        return events
    for _nid, d in g.nodes(data=True):
        label = str(d.get("_label") or d.get("label") or "")
        if label not in ("Meeting", "Task"):
            continue
        ts = None
        for f in _DATE_FIELDS:
            v = str(d.get(f) or "")[:10]
            if v:
                ts = v
                break
        if not ts:
            continue
        title = d.get("title") or d.get("name") or d.get("topic") or ""
        kws = _keywords([title], top=1)
        key = kws[0] if kws else ("встреча" if label == "Meeting" else "задача")
        events.append({"key": key, "ts": ts, "label": label})
    return events


async def detect_pulse(user_id: str) -> dict[str, Any]:
    """R-4.2: пульс по графу пользователя. Пусто, если графа/событий нет."""
    from backend.core.help.org_health import _close_graph, _open_graph
    g = await _open_graph(user_id)
    if g is None:
        return {"recurring": [], "seasonality": [], "summary": {}}
    try:
        return _compute_pulse(_events_from_graph(g[1]))
    finally:
        await _close_graph(g[0])


__all__ = ["detect_pulse", "_compute_pulse", "_events_from_graph"]
