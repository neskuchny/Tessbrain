# -*- coding: utf-8 -*-
"""Хранилище приостановленных прогонов досок — человек-в-цикле (§C).

Когда доска доходит до узла «Отправить и дождаться ответа», прогон
приостанавливается: сюда кладётся снимок (граф + resume_state + чат ожидания +
таймаут). Когда человек отвечает в Telegram — по chat_id находим ждущий прогон и
продолжаем его. Таймаут-жнец добивает просроченные (ветка «не ответили»).

Файловое хранилище, один JSON на user_id (как handoff_store). Never-raise. Всё
работает только за флагом ENABLE_BOARD_WAIT (гейт — на стороне вызывающего).
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

WAITING = "waiting"
DONE = "done"
EXPIRED = "expired"


def _path(user_id: str) -> Optional[str]:
    try:
        from backend.core.store.tenant_paths import _DATA_ROOT
        d = _DATA_ROOT / "board_waits"
        d.mkdir(parents=True, exist_ok=True)
        safe = "".join(c for c in str(user_id) if c.isalnum() or c in "-_")[:64]
        return str(d / f"{safe}.json") if safe else None
    except Exception:
        logger.debug("wait path failed", exc_info=True)
        return None


def _load(user_id: str) -> Dict[str, Any]:
    p = _path(user_id)
    if not p or not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        logger.debug("wait load failed", exc_info=True)
        return {}


def _save(user_id: str, data: Dict[str, Any]) -> None:
    p = _path(user_id)
    if not p:
        return
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        logger.warning("wait save failed", exc_info=True)


def create_wait(user_id: str, *, board_id: Optional[str], chat_id: str,
                nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]],
                resume_state: Dict[str, Any], wait_info: Dict[str, Any],
                now: Optional[float] = None) -> Optional[str]:
    """Сохранить приостановленный прогон. Возврат — wait_id (или None)."""
    if not user_id or not chat_id:
        return None
    ts = float(now if now is not None else time.time())
    timeout_min = 0
    try:
        timeout_min = max(0, int((wait_info or {}).get("timeout_min") or 0))
    except (TypeError, ValueError):
        timeout_min = 0
    wid = "wait_" + uuid.uuid4().hex[:14]
    store = _load(user_id)
    store[wid] = {
        "wait_id": wid, "user_id": user_id, "board_id": board_id,
        "chat_id": str(chat_id), "status": WAITING,
        "nodes": nodes, "edges": edges, "resume_state": resume_state,
        "wait": wait_info or {}, "created_at": ts,
        "expires_at": (ts + timeout_min * 60) if timeout_min else 0,
    }
    _save(user_id, store)
    return wid


def find_waiting_for_chat(user_id: str, chat_id: str) -> Optional[Dict[str, Any]]:
    """Самый свежий ждущий прогон этого пользователя для этого chat_id."""
    if not user_id or not chat_id:
        return None
    store = _load(user_id)
    cand = [w for w in store.values()
            if isinstance(w, dict) and w.get("status") == WAITING
            and str(w.get("chat_id")) == str(chat_id)]
    if not cand:
        return None
    cand.sort(key=lambda w: float(w.get("created_at") or 0), reverse=True)
    return cand[0]


def mark(user_id: str, wait_id: str, status: str) -> None:
    """Пометить прогон (done/expired), чтобы не резюмировать повторно."""
    store = _load(user_id)
    w = store.get(wait_id)
    if isinstance(w, dict):
        w["status"] = status
        _save(user_id, store)


def due_expired(user_id: str, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Ждущие прогоны с истёкшим таймаутом (для жнеца). Не меняет статус."""
    ts = float(now if now is not None else time.time())
    out = []
    for w in _load(user_id).values():
        if (isinstance(w, dict) and w.get("status") == WAITING
                and float(w.get("expires_at") or 0) and ts >= float(w["expires_at"])):
            out.append(w)
    return out


def _all_user_ids() -> List[str]:
    """id всех пользователей, у кого есть файл ожиданий (для жнеца)."""
    try:
        from backend.core.store.tenant_paths import _DATA_ROOT
        d = _DATA_ROOT / "board_waits"
        if not d.is_dir():
            return []
        return [p.stem for p in d.glob("*.json")]
    except Exception:
        logger.debug("wait list users failed", exc_info=True)
        return []


def due_expired_all(now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Просроченные ждущие прогоны ВСЕХ пользователей (для планировщика)."""
    out: List[Dict[str, Any]] = []
    for uid in _all_user_ids():
        out.extend(due_expired(uid, now=now))
    return out


__all__ = [
    "DONE",
    "EXPIRED",
    "WAITING",
    "create_wait",
    "due_expired",
    "due_expired_all",
    "find_waiting_for_chat",
    "mark",
]
