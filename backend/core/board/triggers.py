# -*- coding: utf-8 -*-
"""Триггеры процесс-досок по расписанию (P3 BOARD_ROADMAP).

Планировщик раз в N минут находит процесс-доски с trigger-узлом
`trigger_type=schedule` и, если с прошлого запуска прошёл интервал
(`last_triggered_at`, миграция 265), прогоняет их через process_engine.
Дедуп по last_triggered_at → НЕ гоняем заново раньше интервала (защита от
runaway-расхода). ВЫКЛ по умолчанию (BOARD_PROCESS_TRIGGERS=on). Никогда не raises.

Чистые `schedule_interval` / `is_due` тестируются без БД.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def triggers_enabled() -> bool:
    return os.getenv("BOARD_PROCESS_TRIGGERS", "on").strip().lower() in ("1", "on", "true", "yes")


def _parse_iso(s: Any) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def schedule_spec(graph: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Спека schedule-триггера доски, иначе None.

    {"kind": "interval", "minutes": N}            — каждые N минут (мин. 5);
    {"kind": "daily", "time": "HH:MM"}            — раз в день (UTC);
    {"kind": "weekly", "weekday": 0-6, "time": …} — раз в неделю (0=Пн, UTC)."""
    def _hhmm(raw: Any) -> str:
        try:
            h, m = str(raw or "09:00").strip().split(":")
            return f"{int(h) % 24:02d}:{int(m) % 60:02d}"
        except (ValueError, TypeError):
            return "09:00"

    for n in (graph or {}).get("nodes") or []:
        if str(n.get("type")) == "trigger":
            d = n.get("data") or {}
            # Выключатель автоматизации: enabled=false → расписание не тикает
            # (раньше галочка была только у событий, расписание нельзя было
            # выключить, не удаляя доску).
            if d.get("enabled") is False:
                return None
            if str(d.get("trigger_type")) == "schedule":
                kind = str(d.get("schedule_kind") or "interval")
                if kind == "daily":
                    return {"kind": "daily", "time": _hhmm(d.get("daily_time"))}
                if kind == "weekly":
                    try:
                        wd = int(d.get("weekday") or 0) % 7
                    except (ValueError, TypeError):
                        wd = 0
                    return {"kind": "weekly", "weekday": wd,
                            "time": _hhmm(d.get("daily_time"))}
                try:
                    return {"kind": "interval",
                            "minutes": max(5, int(d.get("interval_min") or 60))}
                except (ValueError, TypeError):
                    return {"kind": "interval", "minutes": 60}
    return None


def schedule_interval(graph: Dict[str, Any]) -> Optional[int]:
    """Совместимость: интервал (мин) interval-триггера, иначе None."""
    spec = schedule_spec(graph)
    if spec and spec["kind"] == "interval":
        return int(spec["minutes"])
    return None


def is_due(interval_min: int, last_iso: Any, now: datetime) -> bool:
    """Пора ли запускать (interval): нет прошлого запуска ИЛИ прошёл интервал."""
    last = _parse_iso(last_iso)
    if last is None:
        return True
    return (now - last) >= timedelta(minutes=int(interval_min))


def is_due_spec(spec: Dict[str, Any], last_iso: Any, now: datetime) -> bool:
    """Пора ли запускать по спеке.

    interval — как is_due; daily — сегодня (UTC) наступило время ЧЧ:ММ и
    сегодняшний запуск ещё не делался; weekly — сегодня нужный день недели,
    время наступило и на этой неделе ещё не бегали."""
    if not spec:
        return False
    if spec.get("kind") in ("daily", "weekly"):
        try:
            h, m = str(spec.get("time") or "09:00").split(":")
            target = now.replace(hour=int(h), minute=int(m),
                                 second=0, microsecond=0)
        except (ValueError, TypeError):
            target = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if spec.get("kind") == "weekly":
            try:
                wd = int(spec.get("weekday") or 0) % 7
            except (ValueError, TypeError):
                wd = 0
            if now.weekday() != wd or now < target:
                return False
            last = _parse_iso(last_iso)
            return last is None or last.astimezone(timezone.utc) < target
        if now < target:
            return False
        last = _parse_iso(last_iso)
        return last is None or last.astimezone(timezone.utc).date() < now.date()
    return is_due(int(spec.get("minutes") or 60), last_iso, now)


async def run_due_process_boards(now: Optional[datetime] = None) -> Dict[str, Any]:
    """Пройти по процесс-доскам с schedule-триггером и запустить назревшие.
    Best-effort. {enabled, scanned, ran}."""
    if not triggers_enabled():
        return {"enabled": False, "scanned": 0, "ran": 0}
    now = now or datetime.now(timezone.utc)
    try:
        from backend.db.supabase_client import get_supabase_client
        sb = get_supabase_client()
        rows = await sb._request(
            "GET", "/rest/v1/board_workflows",
            params={"kind": "eq.process",
                    "select": "id,user_id,graph,last_triggered_at", "limit": "500"}) or []
    except Exception as e:
        logger.warning("board triggers: list failed: %s", e)
        return {"enabled": True, "scanned": 0, "ran": 0}

    from backend.core.board.process_engine import run_process_board
    ran = 0
    for b in rows:
        spec = schedule_spec(b.get("graph") or {})
        if spec is None:
            continue
        if not is_due_spec(spec, b.get("last_triggered_at"), now):
            continue
        try:
            await run_process_board(b.get("graph") or {}, user_id=b.get("user_id"))
            await sb._request(
                "PATCH", "/rest/v1/board_workflows",
                params={"id": f"eq.{b.get('id')}"},
                json_data={"last_triggered_at": now.isoformat()})
            ran += 1
        except Exception as e:
            logger.warning("board trigger run failed for %s: %s", b.get("id"), e)

    if ran:
        logger.info("🔁 board triggers: запущено %d процесс-досок по расписанию", ran)
    return {"enabled": True, "scanned": len(rows), "ran": ran}


__all__ = ["run_due_process_boards", "schedule_spec", "schedule_interval",
           "is_due", "is_due_spec", "triggers_enabled"]
