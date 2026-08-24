"""
Business Wisdom Engine — API Routes

Эндпоинты для взаимодействия с системой корпоративной мудрости.

Консистентность с архитектурой Tessbrain:
- user_id как query-параметр (как везде в системе)
- Litestar Router/get/post паттерны
- structlog логирование
- Lazy инициализация через get_wisdom_engine()

Endpoints:
  POST /wisdom/process-meeting   — обработать транскрипт
  POST /wisdom/link-outcome      — привязать исход вручную
  POST /wisdom/query             — запросить мудрость
  GET  /wisdom/decisions         — список Decision Events
  GET  /wisdom/decisions/{id}    — детали одного решения
  GET  /wisdom/patterns          — список паттернов (Wisdom Index)
  GET  /wisdom/patterns/{id}     — детали паттерна
  POST /wisdom/sync              — синхронизировать из встреч Tessbrain
  POST /wisdom/recluster         — полная перекластеризация
  GET  /wisdom/stats             — статистика
"""
from __future__ import annotations

from typing import Any, Optional

import structlog
from litestar import Router, get, post
from backend.core.auth.user_guard import enforce_user_id_matches_token
from litestar.params import Parameter
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from backend.core.wisdom.bwe_models import (
    BWEDecisionEvent,
    BWEOutcomeEvent,
    BWEWisdomPattern,
    OutcomeEventCreate,
    ProcessMeetingRequest,
    ProcessMeetingResult,
    WisdomQuery,
    WisdomResponse,
)
from backend.core.wisdom.wisdom_engine import get_wisdom_engine

logger = structlog.get_logger(__name__)


# ─────────────────────────────────────────────
# Дополнительные схемы
# ─────────────────────────────────────────────

class SyncRequest(BaseModel):
    user_id: str
    company_vitek: int = Field(default=1, ge=1, le=4)
    limit: int = Field(default=30, ge=1, le=200)


class ReclusterRequest(BaseModel):
    user_id: str


class WisdomStats(BaseModel):
    total_decisions: int
    decisions_with_outcomes: int
    outcome_coverage_pct: float
    total_patterns: int
    patterns_by_confidence: dict[str, int]
    top_tension_types: list[dict[str, Any]]


# ─────────────────────────────────────────────
# Handlers
# ─────────────────────────────────────────────

@post("/process-meeting")
async def process_meeting(data: ProcessMeetingRequest) -> ProcessMeetingResult:
    """
    Обработать транскрипт встречи через Business Wisdom Engine.

    Идемпотентен: повторная отправка того же meeting_id пропускается.

    Что происходит:
    1. LLM извлекает Decision Events (типы ситуаций, не конкретику)
    2. Генерируются батчевые эмбеддинги (один API-вызов)
    3. Если is_retrospective=True — ищутся упоминания прошлых исходов
    4. Инкрементальная кластеризация в паттерны (только новые решения)
    5. Qdrant индекс обновляется
    """
    engine = await get_wisdom_engine()
    return await engine.process_meeting(
        user_id=data.user_id,
        transcript=data.transcript,
        meeting_id=data.meeting_id,
        meeting_source=data.meeting_source,
        company_vitek=data.company_vitek,
        market_context=data.market_context or "",
        is_retrospective=data.is_retrospective,
    )


@post("/link-outcome")
async def link_outcome(data: OutcomeEventCreate) -> dict[str, Any]:
    """
    Вручную привязать исход к Decision Event (замыкание петли).

    Идемпотентен: повторная привязка того же типа исхода к тому же решению игнорируется.

    outcome_type:
    - success: решение сработало
    - failure: решение не сработало
    - partial: смешанный результат
    - unknown: результат не оценён
    """
    engine = await get_wisdom_engine()
    outcome = await engine.link_outcome(
        user_id=data.user_id,
        decision_event_id=data.decision_event_id,
        outcome_type=data.outcome_type,
        outcome_description=data.outcome_description,
        causal_factors=data.causal_factors,
        delay_days=data.delay_days,
        impact_magnitude=data.impact_magnitude,
        meeting_id=data.meeting_id,
    )
    if outcome:
        return {"status": "linked", "outcome": outcome.model_dump(mode="json")}
    return {
        "status": "skipped",
        "message": (
            "Исход не создан: либо Decision Event не найден, "
            "либо исход с таким типом уже зарегистрирован для этого решения."
        ),
    }


@post("/query")
async def query_wisdom(data: WisdomQuery) -> WisdomResponse:
    """
    Запросить мудрость по новой бизнес-ситуации.

    BWE ищет ПОХОЖИЕ СИТУАЦИИ (не похожий текст).
    "Конкурент снизил цену" и "ценовое давление" — одна ситуация.

    Возвращает:
    - wisdom_narrative — ответ как опытный консультант
    - similar_patterns — похожие паттерны с распределением исходов
    - overall_confidence — уверенность (low/medium/high)
    - total_cases_analyzed — сколько прошлых случаев задействовано
    """
    engine = await get_wisdom_engine()
    return await engine.query(wisdom_query=data)


@get("/observer-drift")
async def observer_drift(
    user_id: str = Parameter(query="user_id", required=True),
    tension_type: Optional[str] = Parameter(query="tension_type", default=None),
) -> dict[str, Any]:
    """
    D1 observer-effect: дрейф «ожидание на встрече → факт».

    Сравнивает predicted_outcome (каким встреча видела исход) с фактическим
    outcome_type. drift_index ∈ [−1,1]:
      + систематическая переоценка (обещали успех, по факту провал) —
        сигнатура observer-effect (встречи как перформанс, не отражение реальности);
      − системный пессимизм; 0 — калибровано / нет данных.
    Опциональный фильтр по оси tension_type.
    """
    engine = await get_wisdom_engine()
    return await engine.compute_observer_drift(user_id=user_id, tension_type=tension_type)


@get("/intelligence-health")
async def intelligence_health(
    user_id: str = Parameter(query="user_id", required=True),
) -> dict[str, Any]:
    """
    Tier 3 G: режим здоровья корпуса знаний (frozen/healthy/stressed/chaotic)
    по притоку решений за 14 дней. Возвращает режим, амплитуду и рекомендацию
    политики (что делать в этом режиме).
    """
    engine = await get_wisdom_engine()
    return await engine.compute_intelligence_health(user_id=user_id)


@get("/decisions")
async def list_decisions(
    user_id: str = Parameter(query="user_id", required=True),
    limit: int = Parameter(query="limit", default=50),
    offset: int = Parameter(query="offset", default=0),
    tension_type: Optional[str] = Parameter(query="tension_type", default=None),
    with_outcomes_only: bool = Parameter(query="with_outcomes_only", default=False),
) -> dict[str, Any]:
    """
    Список Decision Events.

    Фильтры: tension_type, with_outcomes_only.
    """
    engine = await get_wisdom_engine()
    async with engine.session_factory() as session:
        q = select(BWEDecisionEvent).where(BWEDecisionEvent.user_id == user_id)
        if with_outcomes_only:
            q = q.where(BWEDecisionEvent.outcome_linked)
        if tension_type:
            q = q.where(BWEDecisionEvent.tension_type == tension_type)
        q = q.order_by(BWEDecisionEvent.created_at.desc()).limit(limit).offset(offset)

        result = await session.execute(q)
        decisions = result.scalars().all()

        total = await session.scalar(
            select(func.count(BWEDecisionEvent.id))
            .where(BWEDecisionEvent.user_id == user_id)
        )

    return {
        "total": total or 0,
        "offset": offset,
        "limit": limit,
        "items": [
            {
                "id": str(d.id),
                "situation": d.situation,
                "tension_type": d.tension_type,
                "decision": d.decision,
                "decision_confidence": d.decision_confidence,
                "company_vitek": d.company_vitek,
                "outcome_linked": d.outcome_linked,
                "pattern_id": d.pattern_id,
                "meeting_source": d.meeting_source,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in decisions
        ],
    }


@get("/decisions/{decision_id:str}")
async def get_decision(
    decision_id: str,
    user_id: str = Parameter(query="user_id", required=True),
) -> dict[str, Any]:
    """Детали Decision Event с привязанными исходами."""
    engine = await get_wisdom_engine()
    async with engine.session_factory() as session:
        result = await session.execute(
            select(BWEDecisionEvent)
            .where(
                BWEDecisionEvent.id == decision_id,
                BWEDecisionEvent.user_id == user_id,
            )
        )
        dec = result.scalar_one_or_none()
        if not dec:
            return {"error": "Decision Event не найден"}

        outcome_result = await session.execute(
            select(BWEOutcomeEvent)
            .where(BWEOutcomeEvent.decision_event_id == dec.id)
        )
        outcomes = outcome_result.scalars().all()

    return {
        "id": str(dec.id),
        "user_id": dec.user_id,
        "meeting_id": dec.meeting_id,
        "meeting_source": dec.meeting_source,
        "situation": dec.situation,
        "tension_type": dec.tension_type,
        "options_considered": dec.options_considered or [],
        "decision": dec.decision,
        "rationale": dec.rationale,
        "company_vitek": dec.company_vitek,
        "market_context": dec.market_context,
        "decision_confidence": dec.decision_confidence,
        "pattern_id": dec.pattern_id,
        "outcome_linked": dec.outcome_linked,
        "created_at": dec.created_at.isoformat() if dec.created_at else None,
        "outcomes": [
            {
                "id": str(o.id),
                "outcome_type": o.outcome_type,
                "outcome_description": o.outcome_description,
                "delay_days": o.delay_days,
                "causal_factors": o.causal_factors or [],
                "impact_magnitude": o.impact_magnitude,
                "source_type": o.source_type,
                "created_at": o.created_at.isoformat() if o.created_at else None,
            }
            for o in outcomes
        ],
    }


@get("/patterns")
async def list_patterns(
    user_id: str = Parameter(query="user_id", required=True),
    confidence: Optional[str] = Parameter(query="confidence", default=None),
) -> dict[str, Any]:
    """
    Список паттернов Wisdom Index.

    confidence фильтр: low | medium | high
    """
    engine = await get_wisdom_engine()
    async with engine.session_factory() as session:
        q = select(BWEWisdomPattern).where(BWEWisdomPattern.user_id == user_id)
        if confidence and confidence in ("low", "medium", "high"):
            q = q.where(BWEWisdomPattern.confidence == confidence)
        q = q.order_by(BWEWisdomPattern.updated_at.desc())
        result = await session.execute(q)
        patterns = result.scalars().all()

    return {
        "total": len(patterns),
        "items": [
            {
                "id": str(p.id),
                "pattern_name": p.pattern_name,
                "total_cases": len(p.instances or []),
                "outcome_distribution": p.outcome_distribution or {},
                "confidence": p.confidence,
                "context_differentiators": p.context_differentiators or [],
                "vitek_sensitivity": p.vitek_sensitivity,
                "needs_analysis_update": p.needs_analysis_update,
                "updated_at": p.updated_at.isoformat() if p.updated_at else None,
            }
            for p in patterns
        ],
    }


@get("/patterns/{pattern_id:str}")
async def get_pattern(
    pattern_id: str,
    user_id: str = Parameter(query="user_id", required=True),
) -> dict[str, Any]:
    """Детали паттерна с примерами решений."""
    engine = await get_wisdom_engine()
    async with engine.session_factory() as session:
        details = await engine.index.get_pattern_details(
            session=session,
            pattern_uuid=pattern_id,
            user_id=user_id,
            max_recent_decisions=5,
        )
    return details or {"error": "Паттерн не найден"}


@post("/sync")
async def sync_from_meetings(data: SyncRequest) -> dict[str, Any]:
    """
    Загрузить встречи из Tessbrain и обработать через BWE.

    Используется для первоначальной загрузки и периодической синхронизации.
    Идемпотентен: повторная обработка тех же meeting_id игнорируется.
    """
    engine = await get_wisdom_engine()
    meetings = await _load_meetings_from_tessent(user_id=data.user_id, limit=data.limit)

    if not meetings:
        return {
            "status": "no_meetings",
            "message": "Встречи не найдены. Проверьте что данные загружены в Tessbrain.",
            "processed": 0,
        }

    result = await engine.sync_patterns_from_existing_meetings(
        user_id=data.user_id,
        meeting_transcripts=meetings,
        company_vitek=data.company_vitek,
    )

    return {
        "status": "ok",
        "processed_meetings": result["processed_meetings"],
        "total_decisions_extracted": result["total_decisions"],
        "total_outcomes_linked": result["total_outcomes"],
    }


@post("/recluster")
async def recluster(data: ReclusterRequest) -> dict[str, Any]:
    """
    Полная перекластеризация паттернов пользователя.

    Используется если изменились пороги кластеризации или качество паттернов
    неудовлетворительно. ДОРОГАЯ операция — выполнять не чаще раза в неделю.
    """
    engine = await get_wisdom_engine()
    async with engine.session_factory() as session:
        try:
            patterns = await engine.encoder.full_recluster(
                session=session,
                user_id=data.user_id,
            )
            await session.commit()
            # Полная синхронизация Qdrant (включая удаление orphans)
            await engine.index.sync_all_patterns(session=session, user_id=data.user_id)
        except Exception as e:
            await session.rollback()
            logger.error(f"[BWE Recluster] Failed for user {data.user_id}: {e}")
            return {"status": "error", "message": str(e)}

    return {
        "status": "ok",
        "patterns_created": len(patterns),
        "message": f"Перекластеризация завершена. Создано {len(patterns)} паттернов.",
    }


@get("/stats")
async def get_stats(
    user_id: str = Parameter(query="user_id", required=True),
) -> WisdomStats:
    """
    Статистика Business Wisdom Engine.

    Ключевые метрики:
    - outcome_coverage_pct: процент решений с зафиксированными исходами (цель: > 60%)
    - patterns_by_confidence: сколько паттернов достигли medium/high уверенности
    """
    engine = await get_wisdom_engine()
    async with engine.session_factory() as session:
        total_dec = await session.scalar(
            select(func.count(BWEDecisionEvent.id))
            .where(BWEDecisionEvent.user_id == user_id)
        ) or 0

        dec_with_outcomes = await session.scalar(
            select(func.count(BWEDecisionEvent.id))
            .where(
                BWEDecisionEvent.user_id == user_id,
                BWEDecisionEvent.outcome_linked,
            )
        ) or 0

        pattern_confs = await session.execute(
            select(BWEWisdomPattern.confidence)
            .where(BWEWisdomPattern.user_id == user_id)
        )
        confidences = [r[0] for r in pattern_confs]

        tension_result = await session.execute(
            select(BWEDecisionEvent.tension_type, func.count(BWEDecisionEvent.id).label("cnt"))
            .where(
                BWEDecisionEvent.user_id == user_id,
                BWEDecisionEvent.tension_type.isnot(None),
            )
            .group_by(BWEDecisionEvent.tension_type)
            .order_by(func.count(BWEDecisionEvent.id).desc())
            .limit(5)
        )

    coverage = round(dec_with_outcomes / total_dec * 100, 1) if total_dec > 0 else 0.0

    return WisdomStats(
        total_decisions=total_dec,
        decisions_with_outcomes=dec_with_outcomes,
        outcome_coverage_pct=coverage,
        total_patterns=len(confidences),
        patterns_by_confidence={
            "low": confidences.count("low"),
            "medium": confidences.count("medium"),
            "high": confidences.count("high"),
        },
        top_tension_types=[
            {"tension_type": row[0], "count": row[1]}
            for row in tension_result
        ],
    )


# ─────────────────────────────────────────────
# Helper: загрузка встреч из Tessbrain
# ─────────────────────────────────────────────

async def _load_meetings_from_tessent(user_id: str, limit: int = 30) -> list[dict]:
    """
    Загрузить транскрипты из Tessbrain (Supabase → Neo4j fallback).
    """
    meetings: list[dict] = []

    # Попытка 1: Supabase
    try:
        from backend.config import settings
        if settings.supabase_url and settings.supabase_key:
            from supabase import create_client
            sb = create_client(settings.supabase_url, settings.supabase_key)
            rows = (
                sb.table("meetings")
                .select("id, transcript, title, created_at")
                .eq("user_id", user_id)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            ).data or []
            for row in rows:
                text = row.get("transcript") or row.get("text") or ""
                if text:
                    title = (row.get("title") or "").lower()
                    meetings.append({
                        "id": row.get("id"),
                        "text": text,
                        "source": row.get("title", ""),
                        "is_retrospective": any(k in title for k in ("ретро", "retrospect", "разбор", "постмортем")),
                    })
            if meetings:
                logger.info(f"[BWESync] Loaded {len(meetings)} meetings from Supabase")
                return meetings
    except Exception as e:
        logger.debug(f"[BWESync] Supabase load failed: {e}")

    # Попытка 2: Neo4j Episodes
    try:
        from backend.db.neo4j_client import get_neo4j
        neo = await get_neo4j()
        if neo and neo.driver:
            rows = neo.run(
                """
                MATCH (ep:Episode {user_id: $user_id})
                WHERE coalesce(ep.raw_text, ep.text, '') <> ''
                RETURN ep.id AS id,
                       coalesce(ep.raw_text, ep.text, '') AS text,
                       ep.source AS source
                ORDER BY ep.created_at DESC LIMIT $limit
                """,
                user_id=user_id,
                limit=limit,
            )
            for row in rows:
                source = (row.get("source") or "").lower()
                meetings.append({
                    "id": row.get("id"),
                    "text": row.get("text", ""),
                    "source": row.get("source", ""),
                    "is_retrospective": any(k in source for k in ("ретро", "retrospect", "разбор")),
                })
            if meetings:
                logger.info(f"[BWESync] Loaded {len(meetings)} episodes from Neo4j")
    except Exception as e:
        logger.debug(f"[BWESync] Neo4j load failed: {e}")

    return meetings


@get("/principles")
async def list_principles(
    user_id: str = Parameter(query="user_id", required=True),
    limit: int = Parameter(query="limit", default=100),
) -> dict[str, Any]:
    """Синтезированные принципы (Wisdom) из графа — с доказательной базой.

    Раньше создавались ночью, но экрана для них не было. Возвращаем
    statement/domain/confidence/evidence_count/evidence/needs_validation.
    """
    import json as _json

    from backend.core.store.graph_builder import GraphBuilder
    from backend.core.store.tenant_paths import graph_path_for_user

    items: list[dict[str, Any]] = []
    try:
        gb = GraphBuilder(use_networkx=None, graph_storage_path=graph_path_for_user(user_id))
        await gb.connect()
        nodes = await gb.find_nodes_by_label("Principle", limit=limit, tenant_id=user_id)
        await gb.close(save=False)
        for n in nodes:
            ev = n.get("evidence")
            if isinstance(ev, str):
                try:
                    ev = _json.loads(ev)
                except (ValueError, TypeError):
                    ev = [ev] if ev else []
            items.append({
                "statement": n.get("statement") or "",
                "domain": n.get("domain") or "",
                "confidence": n.get("confidence") or 0,
                "evidence_count": n.get("evidence_count") or 0,
                "evidence": ev or [],
                "needs_validation": bool(n.get("needs_validation")),
                "source": n.get("source") or "",
                "created_at": n.get("created_at") or "",
            })
        # самые уверенные/подтверждённые — выше
        items.sort(key=lambda x: (x.get("confidence") or 0, x.get("evidence_count") or 0), reverse=True)
    except Exception as e:
        logger.error(f"list_principles failed: {e}")
        return {"total": 0, "items": [], "error": str(e)}
    return {"total": len(items), "items": items}


@get("/rules")
async def list_rules(
    user_id: str = Parameter(query="user_id", required=True),
) -> dict[str, Any]:
    """Rule Book компании (регламенты → правила), который чат подмешивает в
    system-prompt. Раньше наполнялся ночью, но просмотра не было."""
    try:
        from backend.core.memory.rule_book import get_rule_book_service
        book = get_rule_book_service().load(user_id)
        data = book.to_dict()
        rules = data.get("rules", []) if isinstance(data, dict) else []
        return {"total": len(rules), "rules": rules, "book": data}
    except Exception as e:
        logger.error(f"list_rules failed: {e}")
        return {"total": 0, "rules": [], "error": str(e)}


# ─────────────────────────────────────────────
# Router
# ─────────────────────────────────────────────

router = Router(
    path="/wisdom",
    guards=[enforce_user_id_matches_token],
    route_handlers=[
        process_meeting,
        link_outcome,
        query_wisdom,
        observer_drift,
        intelligence_health,
        list_decisions,
        get_decision,
        list_patterns,
        get_pattern,
        sync_from_meetings,
        recluster,
        get_stats,
        list_principles,
        list_rules,
    ],
    tags=["Business Wisdom Engine"],
)
