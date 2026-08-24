# -*- coding: utf-8 -*-
"""API наблюдателя — присутствие на главной + ручной прогон.

GET  /observer/status — что агент смотрит сейчас, последние наблюдения,
     включён ли вообще (виджет честно показывает «выключен»).
POST /observer/run    — «осмотреться сейчас»: ручной цикл (force),
     работает и при выключенном фоновом цикле — чтобы можно было увидеть
     агента в деле до включения флага. Требует снапшоты встреч.
POST /observer/reaction — реакция на наблюдение (accepted/declined/
     ignored) — топливо для ротации и (Ф3) калибровки.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from litestar import Controller, get, post
from litestar.params import Parameter

from backend.core.auth.user_guard import enforce_user_id_matches_token

logger = logging.getLogger(__name__)


class ObserverController(Controller):
    guards = [enforce_user_id_matches_token]
    path = "/observer"
    tags = ["observer"]

    @get("/status")
    async def status(
        self,
        user_id: str = Parameter(query="user_id", required=True),
    ) -> Dict[str, Any]:
        from backend.core.observer.agent import observer_enabled
        from backend.core.observer.fronts import FRONTS
        from backend.core.observer.meeting_snapshot import (
            list_snapshot_records,
            snapshots_enabled,
        )
        from backend.core.observer.state import (
            list_watch,
            load_state,
            recent_observations,
        )
        st = load_state(user_id)
        return {
            "enabled": observer_enabled(),
            "snapshots_enabled": snapshots_enabled(),
            "snapshots_count": len(list_snapshot_records(user_id, limit=200)),
            "current_front": st.get("current_front"),
            "last_cycle_at": st.get("last_cycle_at"),
            "observations": recent_observations(user_id, limit=8),
            "watch": list_watch(user_id),
            "fronts": [{"id": f.id, "title": f.title} for f in FRONTS],
        }

    @post("/run")
    async def run_now(
        self,
        user_id: str = Parameter(query="user_id", required=True),
    ) -> Dict[str, Any]:
        from backend.core.observer.agent import run_observer_cycle
        try:
            res = await run_observer_cycle(user_id, force=True)
        except Exception as e:
            logger.warning("observer manual run failed: %s", e)
            return {"status": "error", "error": str(e)[:300]}
        return res

    @post("/reaction")
    async def reaction(
        self,
        data: Dict[str, Any],
        user_id: str = Parameter(query="user_id", required=True),
    ) -> Dict[str, Any]:
        from backend.core.observer.state import set_reaction
        ok = set_reaction(user_id, str((data or {}).get("observation_id") or ""),
                          str((data or {}).get("reaction") or ""))
        return {"ok": ok}

    @post("/watch")
    async def watch_add(
        self,
        data: Dict[str, Any],
        user_id: str = Parameter(query="user_id", required=True),
    ) -> Dict[str, Any]:
        """Поручение «проследи за …» — персональный фронт в ротации."""
        from backend.core.observer.state import add_watch, list_watch
        wid = add_watch(user_id, str((data or {}).get("text") or ""))
        return {"ok": wid is not None, "watch_id": wid,
                "watch": list_watch(user_id)}

    @post("/expand")
    async def expand(
        self,
        data: Dict[str, Any],
        user_id: str = Parameter(query="user_id", required=True),
    ) -> Dict[str, Any]:
        """«Разобрать подробнее»: полный разбор наблюдения (кэшируется)."""
        from backend.core.observer.agent import expand_observation
        try:
            return await expand_observation(
                user_id, str((data or {}).get("observation_id") or ""))
        except Exception as e:
            logger.warning("observer expand failed: %s", e)
            return {"status": "error", "error": str(e)[:300]}

    @post("/make-board")
    async def make_board(
        self,
        data: Dict[str, Any],
        user_id: str = Parameter(query="user_id", required=True),
    ) -> Dict[str, Any]:
        """«Собрать доску»: автоматизация из наблюдения (черновик, триггер
        выключен — человек включает сам)."""
        from backend.core.observer.agent import propose_board
        try:
            return await propose_board(
                user_id, str((data or {}).get("observation_id") or ""))
        except Exception as e:
            logger.warning("observer make-board failed: %s", e)
            return {"status": "error", "error": str(e)[:300]}

    @post("/watch/remove")
    async def watch_remove(
        self,
        data: Dict[str, Any],
        user_id: str = Parameter(query="user_id", required=True),
    ) -> Dict[str, Any]:
        from backend.core.observer.state import list_watch, remove_watch
        ok = remove_watch(user_id, str((data or {}).get("watch_id") or ""))
        return {"ok": ok, "watch": list_watch(user_id)}


router = ObserverController
