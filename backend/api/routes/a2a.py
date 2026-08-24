# -*- coding: utf-8 -*-
"""
A2A API — внешние агенты опрашивают мозг и наоборот (CAPABILITY_READINESS C13).

Контекст: ag2_a2a_integration.py содержит полный фреймворк (TessentA2AServer/
Client/Orchestrator, setup_tessent_ecosystem с Mark001 + MeetFlow), но в routes
не подключён и при старте не вызывается. Endpoints не открыты — внешним агентам
не к чему стучаться. Этот модуль закрывает дыру.

Endpoints (все за флагом enable_a2a_api → 403 при OFF):
  GET  /a2a/agents          — карточки подключённых агентов
  POST /a2a/route           — route_by_message → call (мозг сам выбирает агента)
  POST /a2a/call/{name}     — явный вызов конкретного агента

Дизайн (как везде):
- ЛЕНИВАЯ инициализация оркестратора (singleton) — не блокируем старт сервиса;
  если A2A не используется, накладные расходы нулевые.
- Импорт ag2_a2a_integration тянет autogen, в тестах его может не быть —
  делаем через _load_orchestrator() best-effort: ImportError → 503.
- Чистый I/O-слой, бизнес-логика в готовом TessentA2AOrchestrator.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from litestar import Controller, get, post
from litestar.exceptions import HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Singleton — заводится при первом обращении, не при старте app.
_orchestrator_cache: list = []
_orchestrator_lock_init: list = []


class RouteRequest(BaseModel):
    message: str
    default_agent: Optional[str] = None


class CallRequest(BaseModel):
    message: str


def _flag_or_403() -> None:
    """Проверка флага enable_a2a_api. 403 при OFF."""
    try:
        from backend.core.config.feature_flags import get_feature_flags
        if not get_feature_flags().enable_a2a_api:
            raise HTTPException(status_code=403,
                                detail="A2A API выключен (enable_a2a_api)")
    except HTTPException:
        raise
    except Exception as e:
        # отсутствует флаг / другая ошибка конфига → честная 503
        raise HTTPException(status_code=503,
                            detail=f"feature flags недоступны: {e}") from e


async def _get_orchestrator():
    """Лениво поднимает оркестратор + экосистему. Кэшируется в _orchestrator_cache.

    Любая ошибка импорта (нет autogen / нет внешних сервисов) → HTTPException 503,
    но это не падение сервиса — другие routes продолжают работать.
    """
    if _orchestrator_cache:
        return _orchestrator_cache[0]
    try:
        from backend.core.think.ag2_a2a_integration import (
            setup_tessent_ecosystem,
        )
    except Exception as e:
        logger.warning("A2A unavailable: %s", e)
        raise HTTPException(status_code=503,
                            detail=f"A2A framework недоступен: {e}") from e
    try:
        orch = await setup_tessent_ecosystem()
    except Exception as e:
        logger.warning("A2A setup failed: %s", e)
        raise HTTPException(status_code=503,
                            detail=f"A2A экосистема не настроена: {e}") from e
    _orchestrator_cache.append(orch)
    return orch


def _agent_card_to_dict(card) -> Dict[str, Any]:
    """AgentCardInfo → JSON. Поля могут отличаться по версиям ag2."""
    return {
        "name": getattr(card, "name", "") or getattr(card, "agent_name", ""),
        "description": getattr(card, "description", ""),
        "url": getattr(card, "url", ""),
        "version": getattr(card, "version", ""),
        "tools": list(getattr(card, "tools", []) or []),
    }


class A2AController(Controller):
    path = "/a2a"
    tags = ["a2a"]

    @get("/agents")
    async def list_agents(self) -> Dict[str, Any]:
        """Карточки подключённых агентов экосистемы."""
        _flag_or_403()
        orch = await _get_orchestrator()
        try:
            cards = orch.client.list_agents()
        except Exception as e:
            logger.error("A2A list_agents failed: %s", e)
            raise HTTPException(status_code=502, detail=str(e)) from e
        return {"agents": [_agent_card_to_dict(c) for c in (cards or [])]}

    @post("/route")
    async def route_message(self, data: RouteRequest) -> Dict[str, Any]:
        """Мозг сам выбирает агента по ключевым словам и зовёт его."""
        _flag_or_403()
        if not (data.message or "").strip():
            raise HTTPException(status_code=400, detail="пустой message")
        orch = await _get_orchestrator()
        try:
            result = await orch.call_routed(
                message=data.message, default_agent=data.default_agent)
        except Exception as e:
            logger.error("A2A call_routed failed: %s", e)
            raise HTTPException(status_code=502, detail=str(e)) from e
        return {
            "success": bool(getattr(result, "success", False)),
            "agent_name": getattr(result, "agent_name", "") or "",
            "response": getattr(result, "response", "") or "",
            "error": getattr(result, "error", None),
            "duration_ms": getattr(result, "duration_ms", None),
        }

    @post("/call/{name:str}")
    async def call_agent(self, name: str, data: CallRequest) -> Dict[str, Any]:
        """Явный вызов конкретного агента по имени."""
        _flag_or_403()
        if not (data.message or "").strip():
            raise HTTPException(status_code=400, detail="пустой message")
        orch = await _get_orchestrator()
        try:
            result = await orch.client.call(name, data.message)
        except Exception as e:
            logger.error("A2A call failed: %s", e)
            raise HTTPException(status_code=502, detail=str(e)) from e
        return {
            "success": bool(getattr(result, "success", False)),
            "agent_name": getattr(result, "agent_name", "") or name,
            "response": getattr(result, "response", "") or "",
            "error": getattr(result, "error", None),
            "duration_ms": getattr(result, "duration_ms", None),
        }


router = A2AController
