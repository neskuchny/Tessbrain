# -*- coding: utf-8 -*-
"""Память наблюдателя (per-user): что смотрел, что говорил, как отреагировали.

data/observer/state/<uid>.json:
{
  "last_cycle_at": епоха-секунды последнего цикла,
  "current_front": {"front_id", "title", "ts"} — что агент смотрит сейчас
      (обновляется КАЖДЫЙ цикл, даже когда он решил молчать — присутствие
      на главной живёт именно отсюда),
  "observations": [ {id, front_id, format, hook, signal, meeting_ids,
                     ts, reaction} ... ]  (кап 100, свежие в конце)
}

reaction ∈ {null, "accepted", "declined", "ignored"} — ставится в Ф3
кнопками; ранжирование штрафует фронты с отказами.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MAX_OBSERVATIONS = 100


def _path(user_id: str) -> str:
    import os
    safe = "".join(c for c in str(user_id or "anon")
                   if c.isalnum() or c == "-")[:40] or "anon"
    d = os.path.join("data", "observer", "state")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{safe}.json")


def load_state(user_id: str) -> Dict[str, Any]:
    import os
    try:
        p = _path(user_id)
        if not os.path.exists(p):
            return {}
        with open(p, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        logger.debug("observer state load failed", exc_info=True)
        return {}


def _save(user_id: str, state: Dict[str, Any]) -> None:
    try:
        obs = state.get("observations") or []
        if len(obs) > _MAX_OBSERVATIONS:
            state["observations"] = obs[-_MAX_OBSERVATIONS:]
        from backend.core.store.tenant_io import atomic_write_json
        atomic_write_json(_path(user_id), state)
    except Exception:
        logger.debug("observer state save failed", exc_info=True)


def mark_cycle(user_id: str, front_id: str, front_title: str) -> None:
    """Зафиксировать цикл: агент «посмотрел» на фронт (даже если промолчал)."""
    st = load_state(user_id)
    st["last_cycle_at"] = int(time.time())
    st["current_front"] = {"front_id": front_id, "title": front_title,
                           "ts": int(time.time())}
    _save(user_id, st)


def add_observation(user_id: str, *, front_id: str, fmt: str, hook: str,
                    signal: str, meeting_ids: List[str],
                    score: float) -> str:
    """Записать высказанное наблюдение. Возвращает его id."""
    st = load_state(user_id)
    oid = uuid.uuid4().hex[:12]
    st.setdefault("observations", []).append({
        "id": oid, "front_id": front_id, "format": fmt,
        "hook": str(hook)[:600], "signal": str(signal)[:600],
        "meeting_ids": [str(m) for m in (meeting_ids or [])][:5],
        "score": round(float(score or 0), 3),
        "ts": int(time.time()), "reaction": None,
    })
    _save(user_id, st)
    return oid


def set_reaction(user_id: str, observation_id: str, reaction: str) -> bool:
    """Реакция человека на наблюдение (Ф3: кнопки). accepted/declined/ignored."""
    if reaction not in ("accepted", "declined", "ignored"):
        return False
    st = load_state(user_id)
    for o in st.get("observations") or []:
        if o.get("id") == observation_id:
            o["reaction"] = reaction
            _save(user_id, st)
            return True
    return False


def recent_observations(user_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    obs = load_state(user_id).get("observations") or []
    return list(obs)[-max(1, limit):][::-1]  # свежие первыми


def set_board(user_id: str, observation_id: str, board_id: str,
              board_name: str) -> bool:
    """Доска-автоматизация, собранная из наблюдения («руки» агента)."""
    st = load_state(user_id)
    for o in st.get("observations") or []:
        if o.get("id") == observation_id:
            o["board_id"] = str(board_id)
            o["board_name"] = str(board_name)[:200]
            _save(user_id, st)
            return True
    return False


def set_report(user_id: str, observation_id: str, report: str) -> bool:
    """Полный разбор наблюдения (кэш: повторная кнопка не платит LLM)."""
    st = load_state(user_id)
    for o in st.get("observations") or []:
        if o.get("id") == observation_id:
            o["report"] = str(report)[:12000]
            _save(user_id, st)
            return True
    return False


def get_observation(user_id: str, observation_id: str) -> Optional[Dict[str, Any]]:
    for o in load_state(user_id).get("observations") or []:
        if o.get("id") == observation_id:
            return o
    return None


def set_outcome(user_id: str, observation_id: str, note: str,
                status: str) -> bool:
    """Итог наблюдения (Ф4): applied / worsened / unchanged / unclear."""
    if status not in ("applied", "worsened", "unchanged", "unclear"):
        return False
    st = load_state(user_id)
    for o in st.get("observations") or []:
        if o.get("id") == observation_id:
            o["outcome_note"] = str(note)[:400]
            o["outcome_status"] = status
            o["outcome_ts"] = int(time.time())
            _save(user_id, st)
            return True
    return False


# ── поручения («проследи за …») ─────────────────────────────────────────

_MAX_WATCH = 20


def add_watch(user_id: str, text: str) -> Optional[str]:
    """Поручение наблюдателю. Возвращает id (None — пусто/переполнено)."""
    text = str(text or "").strip()[:300]
    if not text:
        return None
    st = load_state(user_id)
    watch = st.setdefault("watch", [])
    if len(watch) >= _MAX_WATCH:
        return None
    wid = uuid.uuid4().hex[:10]
    watch.append({"id": wid, "text": text, "ts": int(time.time())})
    _save(user_id, st)
    return wid


def remove_watch(user_id: str, watch_id: str) -> bool:
    st = load_state(user_id)
    watch = st.get("watch") or []
    kept = [w for w in watch if w.get("id") != watch_id]
    if len(kept) == len(watch):
        return False
    st["watch"] = kept
    _save(user_id, st)
    return True


def list_watch(user_id: str) -> List[Dict[str, Any]]:
    return list(load_state(user_id).get("watch") or [])
