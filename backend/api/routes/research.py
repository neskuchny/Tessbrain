# -*- coding: utf-8 -*-
"""Batch Research API — исследование по всему корпусу встреч.

POST /research/start {question} → {research_id} (фоновая задача, минуты)
GET  /research/{id}             → статус/прогресс/отчёт
GET  /research/                 → последние исследования юзера
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from litestar import Controller, Response, get, post
from backend.core.auth.user_guard import enforce_user_id_matches_token
from litestar.params import Parameter

logger = logging.getLogger(__name__)


class ResearchController(Controller):
    guards = [enforce_user_id_matches_token]
    path = "/research"
    tags = ["research"]

    @post("/start")
    async def start(
        self,
        data: Dict[str, Any],
        user_id: str = Parameter(query="user_id", required=True),
    ) -> Dict[str, Any]:
        question = str((data or {}).get("question") or "").strip()
        if len(question) < 8:
            return {"ok": False, "error": "Сформулируйте вопрос исследования"}
        mode = str((data or {}).get("mode") or "research")
        depth = str((data or {}).get("depth") or "standard")
        from backend.core.research.batch_research import start_research
        rid = start_research(user_id, question, mode=mode, depth=depth)
        logger.info(f"🔬 Research started: {rid} for {user_id[:8]}")
        return {"ok": True, "research_id": rid}

    @get("/{research_id:str}")
    async def status(
        self,
        research_id: str,
        user_id: str = Parameter(query="user_id", required=True),
    ) -> Dict[str, Any]:
        from backend.core.research.batch_research import load_state
        state = load_state(user_id, research_id)
        if not state:
            return {"ok": False, "error": "not_found"}
        return {"ok": True, **state}

    @get("/{research_id:str}/report")
    async def report_html(
        self,
        research_id: str,
        user_id: str = Parameter(query="user_id", required=True),
    ) -> Response:
        """HTML-отчёт: открывается кнопкой из чата прямо в браузере."""
        from pathlib import Path
        f = Path("data") / "research" / user_id / f"{research_id}.html"
        if not f.exists():
            return Response(content="Отчёт не найден", media_type="text/plain",
                            status_code=404)
        return Response(content=f.read_text(encoding="utf-8"),
                        media_type="text/html")

    @get("/")
    async def list_all(
        self,
        user_id: str = Parameter(query="user_id", required=True),
    ) -> Dict[str, Any]:
        from backend.core.research.batch_research import list_research
        return {"items": list_research(user_id)}


router = ResearchController
