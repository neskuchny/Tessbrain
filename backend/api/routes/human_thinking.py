# -*- coding: utf-8 -*-
"""
ADD-AS-SKILL «думать как человек» — эндпоинты, чтобы скилл можно было запускать
из интерфейса.

GET  /api/v1/think/modes       — какие режимы есть и какой блок дисциплины
                                  навешивается (для рендера в UI, без LLM).
POST /api/v1/think/run         — прогнать произвольный промпт ПОД дисциплиной
                                  выбранного режима и вернуть ответ LLM +
                                  применённый блок (прозрачность).

За флагом human_thinking_enabled (default False). Скелет здесь — детерминированная
композиция дисциплины (human_thinking.with_thinking_discipline); LLM работает ПОД
ней. Сбой LLM не роняет ручку — возвращаем заземлённую ошибку.
"""
from __future__ import annotations

from typing import Optional

from litestar import Router, get, post
from litestar.exceptions import HTTPException
from pydantic import BaseModel, Field

_VALID_MODES = ("interpret", "specify", "diagnose")


class ThinkRunRequest(BaseModel):
    prompt: str = Field(..., description="Вопрос/данные, над которыми думать")
    mode: str = Field("diagnose", description="interpret | specify | diagnose")
    system_prompt: Optional[str] = Field(
        None, description="База system-prompt; дисциплина навесится в конец"
    )
    temperature: float = 0.3
    max_tokens: int = 1200


def _ensure_enabled() -> None:
    """Скилл доступен по ЯВНОМУ выбору пользователя (он сам вызвал /think/*).
    Это его осознанный opt-in, поэтому проверяем отдельный флаг доступности
    скилла (default True), а НЕ авто-флаг `human_thinking_enabled` (тот про
    автоприменение во всех пайплайнах, default False). Так выбор скилла РАБОТАЕТ,
    даже когда автоприменение выключено по Gate-0."""
    from backend.config import settings as _settings
    if not getattr(_settings, "human_thinking_skill_enabled", True):
        raise HTTPException(status_code=403, detail="human_thinking skill disabled")


@get("/modes")
async def list_modes() -> dict:
    """Режимы дисциплины и их блоки — чтобы UI показал, что именно навешивается."""
    _ensure_enabled()
    from backend.core.think.human_thinking import disciplines_for, thinking_block

    return {
        "modes": [
            {
                "mode": m,
                "disciplines": disciplines_for(m),
                "block": thinking_block(m),
            }
            for m in _VALID_MODES
        ]
    }


@post("/run")
async def run_thinking(data: ThinkRunRequest) -> dict:
    """Прогнать промпт ПОД дисциплиной режима и вернуть ответ LLM + применённый блок."""
    _ensure_enabled()

    mode = data.mode if data.mode in _VALID_MODES else "diagnose"
    from backend.core.think.human_thinking import thinking_block, with_thinking_discipline

    base_system = data.system_prompt or (
        "Ты — сильный бизнес-аналитик. Отвечай по существу, без воды."
    )
    system_prompt = with_thinking_discipline(base_system, mode)

    answer: Optional[str] = None
    error: Optional[str] = None
    try:
        from backend.core.llm.router import get_llm_router

        llm = get_llm_router()
        answer = await llm.generate(
            prompt=data.prompt,
            system_prompt=system_prompt,
            temperature=data.temperature,
            max_tokens=data.max_tokens,
        )
    except Exception as e:  # сбой LLM не роняет ручку — честная заземлённая ошибка
        error = f"LLM недоступен: {e}"

    return {
        "mode": mode,
        "applied_discipline": thinking_block(mode),
        "system_prompt": system_prompt,
        "answer": answer,
        "error": error,
    }


router = Router(
    path="/think",
    route_handlers=[list_modes, run_thinking],
    tags=["Human Thinking"],
)
