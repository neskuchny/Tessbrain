# -*- coding: utf-8 -*-
"""Маршрут доставки результатов Vibe Tasking по умолчанию (per-user).

«Результаты всегда дублировать в YouGile-колонку X / в amoCRM» — задаётся
один раз в панели очереди; после завершения задачи БЕЗ привязки к трекеру
результат уходит по этому маршруту автоматически (deliver_handoff_result).
Пусто (дефолт) → поведение прежнее, ничего никуда не шлётся само.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _path(user_id: str) -> str:
    safe = "".join(c for c in str(user_id or "anon")
                   if c.isalnum() or c == "-")[:40] or "anon"
    d = os.path.join("data", "vibe_delivery")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{safe}.json")


def get_route(user_id: str) -> Optional[Dict[str, Any]]:
    """Сохранённый маршрут-цель (формат target из deliver_handoff_result)
    или None."""
    try:
        p = _path(user_id)
        if not os.path.exists(p):
            return None
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        target = data.get("target")
        return target if isinstance(target, dict) and target.get("kind") else None
    except Exception:
        logger.debug("delivery route read failed", exc_info=True)
        return None


def set_route(user_id: str, target: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Сохранить/очистить маршрут. target=None|{} → очистка."""
    p = _path(user_id)
    if not target or not isinstance(target, dict) or not target.get("kind"):
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            logger.debug("delivery route clear failed", exc_info=True)
        return {"status": "success", "target": None}
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"target": target}, f, ensure_ascii=False, indent=2)
    return {"status": "success", "target": target}


__all__ = ["get_route", "set_route"]
