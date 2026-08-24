# -*- coding: utf-8 -*-
"""
ADD-AS-SKILL readiness / Q0 — эндпоинт, чтобы скилл включался по ЯВНОМУ выбору.

POST /api/v1/readiness/assess — оценить процесс на готовность к автоматизации:
Q0 (активировать уже купленное) / фильтр «Лучше руками?» / квадрант. Принимает
СТРУКТУРНЫЕ атрибуты процесса (детерминированно) ИЛИ описание процесса (тогда
LLM извлекает атрибуты, потом тот же детерминированный гейт).

Почему так (фикс после Gate-0): прошлый текстовый путь искал сигналы в ТЕКСТЕ
рекомендации и пропускал их — сигналы живут в ОПИСАНИИ ПРОЦЕССА. Здесь гейт
читает правильный вход (атрибуты процесса), поэтому работает по сути.

За флагом readiness_skill_enabled (default True) — скилл доступен по выбору.
"""
from __future__ import annotations

import json
from typing import Optional

from litestar import Router, post
from litestar.exceptions import HTTPException
from pydantic import BaseModel, Field


class ReadinessRequest(BaseModel):
    # Путь 1 — структурные атрибуты (если известны пайплайну/пользователю).
    frequency: Optional[str] = Field(None, description="daily|weekly|monthly|quarterly|yearly|rare")
    needs_judgment: Optional[bool] = None
    has_owner: Optional[bool] = None
    volatile: Optional[bool] = None
    already_owned: Optional[bool] = None
    value_high: Optional[bool] = None
    complexity_high: Optional[bool] = None
    # Путь 2 — свободное описание процесса; атрибуты извлечёт LLM.
    process_description: Optional[str] = None


def _ensure_enabled() -> None:
    from backend.config import settings as _settings
    if not getattr(_settings, "readiness_skill_enabled", True):
        raise HTTPException(status_code=403, detail="readiness skill disabled")


_EXTRACT_SYSTEM = (
    "Ты извлекаешь СТРУКТУРНЫЕ атрибуты процесса для оценки готовности к "
    "автоматизации. Только факты из описания, не выдумывай. Верни строго JSON с "
    "ключами: frequency (daily|weekly|monthly|quarterly|yearly|rare), "
    "needs_judgment (bool), has_owner (bool), volatile (bool — меняется каждые "
    "2–3 мес), already_owned (bool — инструмент уже куплен), value_high (bool), "
    "complexity_high (bool). Неизвестно — ставь null."
)


async def _extract_attributes(description: str) -> dict:
    """LLM извлекает атрибуты из описания процесса. Сбой → {} (гейт возьмёт дефолты)."""
    try:
        from backend.core.llm.router import get_llm_router
        llm = get_llm_router()
        raw = await llm.generate(
            prompt=f"Описание процесса:\n{description}\n\nИзвлеки атрибуты JSON.",
            system_prompt=_EXTRACT_SYSTEM, temperature=0.0, max_tokens=300,
        )
        start, end = raw.find("{"), raw.rfind("}")
        return json.loads(raw[start:end + 1]) if start >= 0 and end > start else {}
    except Exception:
        return {}


def _kwargs_from(data: ReadinessRequest, extracted: dict) -> dict:
    """Слить явные атрибуты запроса с извлечёнными (явные имеют приоритет).
    Только переданные не-None значения → остальное берёт дефолт assess_process."""
    out: dict = {}
    for k in ("frequency", "needs_judgment", "has_owner", "volatile",
              "already_owned", "value_high", "complexity_high"):
        v = getattr(data, k, None)
        if v is None:
            v = extracted.get(k)
        if v is not None:
            out[k] = v
    return out


@post("/assess")
async def assess_readiness(data: ReadinessRequest) -> dict:
    """Оценить готовность процесса к автоматизации (Q0 / руками / квадрант)."""
    _ensure_enabled()
    from backend.core.think.readiness_gates import assess_process

    extracted: dict = {}
    if data.process_description and not any(
        getattr(data, k) is not None for k in (
            "frequency", "needs_judgment", "has_owner", "volatile",
            "already_owned", "value_high", "complexity_high")
    ):
        extracted = await _extract_attributes(data.process_description)

    kwargs = _kwargs_from(data, extracted)
    result = assess_process(**kwargs)
    return {"assessment": result, "used_attributes": kwargs, "extracted": extracted}


router = Router(path="/readiness", route_handlers=[assess_readiness], tags=["Readiness / Q0"])
