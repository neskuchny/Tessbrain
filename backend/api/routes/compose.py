# -*- coding: utf-8 -*-
"""
Compose API — универсальный композитор для ВНЕШНИХ потребителей (mini tess /
телеграм-бот / агенты): произвольная задача → план → досье → артефакт →
опц. оценка (docs/CAPABILITY_READINESS.md).

Зачем отдельный endpoint: бот живёт в другом сервисе (alfa_asynk_meetflow) и
зовёт мозг по HTTP — ему нужен один вызов «сделай задачу из контекста компании»
без знания внутренних слоёв.

За флагом enable_composer_api (default OFF → 403). Память планов (PlanMemory,
per-user) подключена: повторные задачи делаются запомненным способом, исходы
оценок копятся.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import anyio
from litestar import Controller, Request, post
from litestar.exceptions import HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ComposeRequest(BaseModel):
    user_id: str
    request: str                                  # произвольная задача
    entities: Optional[List[Dict[str, str]]] = None   # [{"name","type"}] — опц.
    company_context: str = ""                     # принципы/позиционирование — опц.
    model_tier: str = "standard"


class ComposeResponse(BaseModel):
    artifact_text: str
    schema_name: str
    plan_source: str                              # planner|memory|memory+planner|caller
    entities: List[Dict[str, str]]
    data_gaps: List[str]
    dossier_coverage: Dict[str, Any]
    evaluation: Optional[Dict[str, Any]] = None   # {decision, score, summary}
    memory_record_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


def _make_sync_llm(loop, model_tier: str):
    """Sync-мост к LLMRouter, привязанный к ОДНОМУ event loop.

    КРИТИЧНО (аудит F1): LLMRouter держит loop-привязанный httpx-пул внутри
    AsyncOpenAI (создаётся при первом запросе). Если каждый llm()-вызов делать
    через отдельный asyncio.run (новый loop, старый закрыт), второй вызов даёт
    'Event loop is closed' — multi-step compose (план+генерация+оценка+критерии)
    ломается на on-prem (LOCAL/OpenAI default). Решение: ВСЕ generate() гоняем
    на ОДНОМ переданном loop через run_until_complete; router один на запрос."""
    from backend.core.llm.router import LLMRouter, ModelTier
    router = LLMRouter()
    tier = ModelTier.PREMIUM if model_tier == "premium" else ModelTier.STANDARD

    def llm(system: str, user: str) -> str:
        return loop.run_until_complete(router.generate(
            prompt=user, system_prompt=system,
            temperature=0.2, max_tokens=1500, model_tier=tier)) or ""
    return llm


async def _company_style_brief() -> str:
    """Фирменный стиль из РЕГЛАМЕНТОВ компании (Cowork-перенос C3):
    база знаний о компании живёт в регламентах (template registry, куда
    их извлекает regulation_extractor) — ищем регламент про стиль/
    оформление/брендбук и отдаём текст композитору. Нет регламента →
    пустая строка (стиль не выдумываем)."""
    try:
        from backend.core.templates import get_template_registry
        registry = get_template_registry()
        for query in ("фирменный стиль", "брендбук", "оформление КП",
                      "стиль документов"):
            try:
                found = registry.search(query=query, limit=3)
                if hasattr(found, "__await__"):
                    found = await found
            except Exception:
                continue
            for t in found or []:
                d = t.to_dict() if hasattr(t, "to_dict") else dict(t)
                text = (d.get("content") or d.get("body")
                        or d.get("description") or "")
                title = str(d.get("title") or d.get("name") or "")
                low = (title + " " + str(text)[:200]).lower()
                if text and any(w in low for w in
                                ("стиль", "бренд", "оформлен")):
                    return (f"\n\nФИРМЕННЫЙ СТИЛЬ (из регламента "
                            f"«{title}») — оформляй артефакт по нему:\n"
                            f"{str(text)[:2000]}")
    except Exception:
        logger.debug("style brief unavailable", exc_info=True)
    return ""


def _run_compose(data: ComposeRequest):
    """Весь sync-конвейер (вызывается в worker-потоке через anyio.to_thread).

    Управляет жизненным циклом одного event loop на весь запрос (см.
    _make_sync_llm) — иначе loop-привязанный AsyncOpenAI ломается между вызовами.
    """
    from backend.core.artifacts import composer as cp
    from backend.core.memory.dossier_glue import dossier_for_user
    from backend.core.memory.plan_memory import PlanMemory
    from backend.core.store.tenant_paths import plan_memory_path_for_user

    plan_memory = None
    try:
        plan_memory = PlanMemory(persist_path=plan_memory_path_for_user(data.user_id))
    except Exception as e:
        logger.debug("plan memory unavailable: %s", e)  # best-effort

    def dossier_fn(name: str, etype: str):
        return dossier_for_user(data.user_id, name, entity_type=etype)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        # C3: фирменный стиль из регламентов компании — аддитивно к
        # company_context; нет регламента → контекст прежний
        company_context = data.company_context
        try:
            style = loop.run_until_complete(_company_style_brief())
            if style:
                company_context = (company_context or "") + style
        except Exception:
            logger.debug("style brief skipped", exc_info=True)
        return cp.compose(
            data.request,
            dossier_fn=dossier_fn,
            llm=_make_sync_llm(loop, data.model_tier),
            company_context=company_context,
            plan_memory=plan_memory,
            entities=data.entities,
        )
    finally:
        try:
            loop.close()
        finally:
            asyncio.set_event_loop(None)


class ComposeController(Controller):
    path = "/compose"
    tags = ["compose"]

    @post("/")
    async def compose_task(self, data: ComposeRequest,
                           request: Request) -> ComposeResponse:
        """Произвольная задача из контекста компании — один вызов для ботов."""
        from backend.core.config.feature_flags import get_feature_flags
        if not get_feature_flags().enable_composer_api:
            raise HTTPException(status_code=403,
                                detail="composer API выключен (enable_composer_api)")
        if not (data.request or "").strip():
            raise HTTPException(status_code=400, detail="пустой request")

        # S1: при enable_service_auth — ТРЕБУЕМ валидный service-JWT, user_id
        # берём из claim токена (а не из произвольного body). Закрывает доступ
        # к чужим данным по подставленному user_id.
        from backend.core.auth.service_token import resolve_service_user_id
        try:
            trusted_uid, _src = resolve_service_user_id(
                request.headers, data.user_id)
            data = data.model_copy(update={"user_id": trusted_uid})
        except PermissionError as e:
            raise HTTPException(status_code=401, detail=str(e)) from e

        try:
            res = await anyio.to_thread.run_sync(_run_compose, data)
        except Exception as e:
            # PlanValidationError и прочее — честная ошибка наружу
            raise HTTPException(status_code=422, detail=f"compose failed: {e}") from e

        evaluation = None
        if res.evaluation is not None:
            evaluation = {"decision": res.evaluation.decision,
                          "score": res.evaluation.score,
                          "summary": res.evaluation.to_text()[:1500]}
        return ComposeResponse(
            artifact_text=res.artifact.text,
            schema_name=res.artifact.schema_name,
            plan_source=res.plan_source,
            entities=[dict(e) for e in res.plan.entities],
            data_gaps=list(res.artifact.data_gaps),
            dossier_coverage=res.dossier_coverage,
            evaluation=evaluation,
            memory_record_id=res.memory_record_id,
            metadata={"reason": res.plan.reason, "generated_at": res.generated_at},
        )


router = ComposeController
