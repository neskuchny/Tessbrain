# -*- coding: utf-8 -*-
"""
Хранилище обратной связи пользователей (проблемы/пожелания) для разработчиков.

Primary — Supabase-таблица feedback_tickets (миграция 268): тикеты видны
разработчикам в облаке, конкурентные записи безопасны. Fallback — прежний
append-JSONL `data/feedback/feedback.jsonl` (Supabase недоступен/не настроен).
Публичное API — async; JSONL-реализации остались как _jsonl_*. Никогда не raises.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

VALID_KINDS = ("problem", "wish", "confusion")


def _dir() -> Path:
    env = os.getenv("TESSENT_FEEDBACK_DIR")
    base = Path(env) if env else (
        Path(__file__).resolve().parents[2] / "data" / "feedback")
    base.mkdir(parents=True, exist_ok=True)
    return base


def _path() -> Path:
    return _dir() / "feedback.jsonl"


def _jsonl_add(*, user_id: Optional[str], kind: str, message: str,
                 area: Optional[list[str]] = None, triage: str = "",
                 context: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Записать тикет обратной связи. Возвращает сохранённую запись."""
    rec = {
        "id": uuid.uuid4().hex[:12],
        "ts": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id or "",
        "kind": kind if kind in VALID_KINDS else "problem",
        "message": (message or "").strip()[:4000],
        "area": area or [],
        "triage": (triage or "")[:2000],
        "context": context or {},
        "status": "open",
        "resolution": "",
    }
    try:
        with _path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("feedback add failed: %s", e)
    return rec


def _jsonl_list(*, since_days: int = 0, limit: int = 500) -> list[dict[str, Any]]:
    """Прочитать тикеты (опц. за последние N дней). Свежие — последними."""
    p = _path()
    if not p.exists():
        return []
    cutoff = None
    if since_days and since_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    out: list[dict[str, Any]] = []
    try:
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if cutoff is not None:
                    try:
                        ts = datetime.fromisoformat(rec.get("ts", ""))
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        if ts < cutoff:
                            continue
                    except Exception:
                        pass
                out.append(rec)
    except Exception as e:
        logger.warning("feedback list failed: %s", e)
    return out[-limit:]


def _jsonl_set_status(feedback_id: str, status: str, *,
               resolution: str = "") -> Optional[dict[str, Any]]:
    """Обновить статус тикета (напр. resolved) + заметку. Перезаписывает JSONL
    (файл маленький, dev-facing). Возвращает обновлённую запись или None."""
    p = _path()
    if not p.exists():
        return None
    rows: list[dict[str, Any]] = []
    updated: Optional[dict[str, Any]] = None
    try:
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("id") == feedback_id:
                    rec["status"] = status
                    if resolution:
                        rec["resolution"] = resolution[:2000]
                    rec["resolved_at"] = datetime.now(timezone.utc).isoformat()
                    updated = rec
                rows.append(rec)
        if updated is not None:
            tmp = p.with_suffix(".jsonl.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            tmp.replace(p)
    except Exception as e:
        logger.warning("feedback set_status failed: %s", e)
    return updated


def _jsonl_get(feedback_id: str) -> Optional[dict[str, Any]]:
    for r in _jsonl_list(limit=100000):
        if r.get("id") == feedback_id:
            return r
    return None


def stats() -> dict[str, int]:
    items = _jsonl_list(limit=100000)
    by_kind: dict[str, int] = {}
    for r in items:
        by_kind[r.get("kind", "problem")] = by_kind.get(r.get("kind", "problem"), 0) + 1
    return {"total": len(items), **by_kind}




# ── Async API: Supabase primary, JSONL fallback ─────────────────────────

_TBL = "/rest/v1/feedback_tickets"


def _sb():
    from backend.db.supabase_client import get_supabase_client
    return get_supabase_client()


async def add_feedback(*, user_id: Optional[str], kind: str, message: str,
                       area: Optional[list[str]] = None, triage: str = "",
                       context: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    rec = {
        "id": uuid.uuid4().hex[:12],
        "ts": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id or "",
        "kind": kind if kind in VALID_KINDS else "problem",
        "message": (message or "").strip()[:4000],
        "area": area or [],
        "triage": (triage or "")[:2000],
        "context": context or {},
        "status": "open",
        "resolution": "",
    }
    try:
        await _sb()._request("POST", _TBL, json_data=rec,
                             headers={"Prefer": "return=representation"})
        return rec
    except Exception as e:
        logger.warning("feedback → Supabase failed, JSONL fallback: %s", e)
    return _jsonl_add(user_id=user_id, kind=kind, message=message,
                      area=area, triage=triage, context=context)


async def list_feedback(*, since_days: int = 0,
                        limit: int = 500) -> list[dict[str, Any]]:
    try:
        params = {"order": "ts.asc", "limit": str(limit)}
        if since_days and since_days > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
            params["ts"] = f"gte.{cutoff.isoformat()}"
        rows = await _sb()._request("GET", _TBL, params=params)
        if isinstance(rows, list):
            return rows
    except Exception as e:
        logger.debug("feedback list → Supabase failed, JSONL fallback: %s", e)
    return _jsonl_list(since_days=since_days, limit=limit)


async def set_status(feedback_id: str, status: str, *,
                     resolution: str = "") -> Optional[dict[str, Any]]:
    patch = {"status": status,
             "resolved_at": datetime.now(timezone.utc).isoformat()}
    if resolution:
        patch["resolution"] = resolution[:2000]
    try:
        rows = await _sb()._request(
            "PATCH", _TBL, params={"id": f"eq.{feedback_id}"},
            json_data=patch, headers={"Prefer": "return=representation"})
        if rows:
            return rows[0]
    except Exception as e:
        logger.debug("feedback set_status → Supabase failed: %s", e)
    return _jsonl_set_status(feedback_id, status, resolution=resolution)


async def get_feedback(feedback_id: str) -> Optional[dict[str, Any]]:
    try:
        rows = await _sb()._request("GET", _TBL,
                                    params={"id": f"eq.{feedback_id}"})
        if rows:
            return rows[0]
    except Exception as e:
        logger.debug("feedback get → Supabase failed: %s", e)
    return _jsonl_get(feedback_id)


def support_health() -> dict[str, Any]:
    """Что настроено для цикла обратной связи (для чек-листа владельца)."""
    import os as _os
    return {
        "telegram_token_set": bool(_os.getenv("TELEGRAM_BOT_TOKEN", "").strip()),
        "support_chat_id_set": bool(
            _os.getenv("TESSENT_SUPPORT_CHAT_ID", "").strip()),
        "jsonl_fallback_path": str(_path()),
    }

__all__ = ["add_feedback", "list_feedback", "get_feedback", "set_status",
           "stats", "support_health", "VALID_KINDS"]
