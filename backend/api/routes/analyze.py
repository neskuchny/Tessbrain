"""
TESSENT BRAIN - Analysis Routes
Эндпоинты для анализа встреч и документов
"""
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

import structlog
from litestar import Router, get, post
from litestar.exceptions import NotAuthorizedException
from litestar.params import Parameter
from litestar.exceptions import HTTPException
from litestar.status_codes import HTTP_202_ACCEPTED
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)


# === Request/Response Models ===

class MeetingIngestRequest(BaseModel):
    """Запрос на загрузку встречи"""
    transcript: str = Field(..., description="Текст транскрипта встречи")
    title: str | None = Field(None, description="Название встречи")
    meeting_date: datetime | None = Field(None, description="Дата встречи")
    participants: list[str] | None = Field(None, description="Список участников (имена)")
    # issue #112 T-4: structured attendees (ground truth «кто был на встрече»).
    # Каждый элемент: {name, email, role}. Источник любой — manual / CRM /
    # Google Calendar. Используется для T-5 validation assignee ∈ attendees.
    attendees: list[dict[str, Any]] | None = Field(
        None, description="Структурированные участники: [{name, email, role}]")
    calendar_event_id: str | None = Field(
        None, description="ID события в Google Calendar для авто-загрузки attendees")
    project_id: str | None = Field(None, description="ID связанного проекта")
    # ПЕРЕПРОВЕРКА: поле отсутствовало, а код читал data.user_id → AttributeError
    # ломал /ingest/meeting целиком (tiered-роутинг + cross-meeting его ждут)
    user_id: str | None = Field(None, description="ID пользователя (tenant)")
    metadata: dict[str, Any] | None = Field(None, description="Дополнительные метаданные")


class MeetingIngestResponse(BaseModel):
    """Ответ на загрузку встречи"""
    meeting_id: str
    status: str
    message: str
    entities_extracted: int = 0
    processing_time_ms: int = 0


class DeepAnalyzeRequest(BaseModel):
    """Запрос на глубокий анализ"""
    query: str = Field(..., description="Вопрос или запрос на анализ")
    context: dict[str, Any] | None = Field(None, description="Дополнительный контекст")
    depth: int = Field(2, ge=1, le=5, description="Глубина анализа (1-5)")
    include_anomalies: bool = Field(True, description="Включить детекцию аномалий")
    include_recommendations: bool = Field(True, description="Включить рекомендации")
    user_id: str | None = Field(None, description="ID пользователя (для tenant-aware поиска)")


class DeepAnalyzeResponse(BaseModel):
    """Ответ на глубокий анализ"""
    query: str
    answer: str
    reasoning_path: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    anomalies: list[dict[str, Any]] | None = None
    recommendations: list[str] | None = None
    confidence: float
    sources: list[str]
    processing_time_ms: int


# === Route Handlers ===

@post("/ingest/meeting", status_code=HTTP_202_ACCEPTED)
async def ingest_meeting(data: MeetingIngestRequest,
                         authorization: Optional[str] = Parameter(header="Authorization", default=None)) -> MeetingIngestResponse:
    from backend.core.auth.user_guard import resolve_user_or_none
    _u = resolve_user_or_none(authorization, data.user_id, scope="analyze/ingest")
    if authorization and _u is None:
        raise NotAuthorizedException(detail="user_id does not match token")
    if _u:
        data.user_id = _u
    """Загрузить встречу и извлечь сущности через CaptureOrchestrator.

    Реальный pipeline:
    1. CaptureOrchestrator.process_meeting → 20+ агентов извлечения
       (decisions, tasks, participants, KPI, opinions, ideas, etc.)
    2. Результат хранит сущности и связи в памяти оркестратора;
       персистентность графа/векторов делает следующий слой (knowledge_sync,
       вызывается отдельно).
    """
    start_time = datetime.now(timezone.utc)
    meeting_id = str(uuid4())

    logger.info(
        "📝 Ingesting meeting",
        meeting_id=meeting_id,
        title=data.title,
        transcript_length=len(data.transcript),
    )

    try:
        from backend.core.capture import get_capture_orchestrator
    except Exception as exc:
        logger.error("Capture orchestrator unavailable: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Capture orchestrator failed to load",
        )

    orchestrator = get_capture_orchestrator()
    meeting_context = {
        "title": data.title,
        "meeting_date": data.meeting_date.isoformat() if data.meeting_date else None,
        "participants": data.participants or [],
        "project_id": data.project_id,
        # user_id → tiered-роутинг модели (ЛК) + cross-meeting links
        "user_id": data.user_id,
        **(data.metadata or {}),
    }
    # issue #112 T-4: добавляем structured attendees + calendar_event_id.
    # resolve_attendees мерджит все источники (structured / GCal / participants)
    # в единый список ground-truth — для T-5 validation в graph_builder.
    if data.attendees:
        meeting_context["attendees"] = data.attendees
    if data.calendar_event_id:
        meeting_context["calendar_event_id"] = data.calendar_event_id
    try:
        from backend.core.transcripts import resolve_attendees
        resolved = await resolve_attendees(meeting_context)
        if resolved:
            # Кладём нормализованный список — orchestrator/graph_builder
            # используют его как ground truth.
            meeting_context["resolved_attendees"] = resolved
    except Exception as exc:
        logger.warning("attendees resolution failed (non-fatal): %s", exc)

    result = await orchestrator.process_meeting(
        transcript=data.transcript,
        meeting_id=meeting_id,
        meeting_context=meeting_context,
    )

    entities_extracted = sum(
        len(result.get(k, []))
        for k in ("decisions", "tasks", "participants", "ideas", "opinions", "kpis")
        if isinstance(result.get(k), list)
    )

    # Авто-свежесть снапшотов: после инжеста кэш устарел — иначе страница
    # «Компания» показывает старое до кнопки «Пересобрать».
    if entities_extracted and data.user_id:
        try:
            from backend.core.sleep.enhanced_snapshot import invalidate_snapshot_cache
            invalidate_snapshot_cache(data.user_id)
        except Exception:
            logger.warning("snapshot invalidate after ingest skipped", exc_info=True)

    processing_time = int((datetime.now(timezone.utc) - start_time).total_seconds() * 1000)
    return MeetingIngestResponse(
        meeting_id=meeting_id,
        status="processed",
        message=f"Meeting processed by {len(result)} agents",
        entities_extracted=entities_extracted,
        processing_time_ms=processing_time,
    )


@post("/deep-analyze")
async def deep_analyze(data: DeepAnalyzeRequest,
                       authorization: Optional[str] = Parameter(header="Authorization", default=None)) -> DeepAnalyzeResponse:
    from backend.core.auth.user_guard import resolve_user_or_none
    _u = resolve_user_or_none(authorization, data.user_id, scope="analyze/deep_analyze")
    if authorization and _u is None:
        raise NotAuthorizedException(detail="user_id does not match token")
    if _u:
        data.user_id = _u
    """Глубокий анализ через ReasoningEngine + Hybrid Search.

    Текущая реализация (Phase 5):
    1. Hybrid search по гибридному оркестратору (vector + BM25 + graph + RRF).
    2. ReasoningEngine собирает evidence и формирует ответ.
    3. Если KAG-engine ещё не сконфигурирован для tenant'а — возвращаем
       честный 503 с конкретным сообщением вместо «not implemented» заглушки.
    """
    start_time = datetime.now(timezone.utc)

    logger.info("🔍 Deep analysis request", query=data.query[:100], depth=data.depth)

    try:
        from backend.core.search.hybrid_search_orchestrator import (
            get_hybrid_search_orchestrator,
        )
        from backend.core.think.reasoning_engine import ReasoningEngine
    except Exception as exc:
        logger.error("KAG components failed to import: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Reasoning components are not available",
        )

    user_id = data.user_id or "default"
    orchestrator = get_hybrid_search_orchestrator(user_id=user_id)
    try:
        search_result = await orchestrator.search(
            query=data.query,
            top_k=max(5, data.depth * 5),
        )
    except Exception as exc:
        logger.exception("Hybrid search failed")
        raise HTTPException(status_code=500, detail=f"Hybrid search failed: {exc}")

    # ReasoningEngine принимает search-результаты и возвращает структурированный ответ.
    try:
        reasoner = ReasoningEngine()
        answer = await reasoner.reason(
            query=data.query,
            search_results=search_result if isinstance(search_result, list) else search_result.get("results", []),
            depth=data.depth,
        )
    except (NotImplementedError, AttributeError) as exc:
        logger.warning("ReasoningEngine surface incomplete: %s", exc)
        # Деградируем: отдаём результаты поиска как evidence без synthesis.
        answer_text = (
            f"Hybrid search returned {len(search_result) if isinstance(search_result, list) else '?'}"
            f" candidates; reasoning synthesis is not yet enabled in this build."
        )
        processing_time = int((datetime.now(timezone.utc) - start_time).total_seconds() * 1000)
        evidence = search_result if isinstance(search_result, list) else search_result.get("results", [])
        return DeepAnalyzeResponse(
            query=data.query,
            answer=answer_text,
            reasoning_path=[
                {"step": 1, "action": "hybrid_search", "result": f"candidates={len(evidence)}"},
            ],
            evidence=evidence[:10],
            anomalies=[] if data.include_anomalies else None,
            recommendations=[] if data.include_recommendations else None,
            confidence=0.3,
            sources=[str(e.get("source", "")) for e in evidence[:5]] if evidence else [],
            processing_time_ms=processing_time,
        )

    processing_time = int((datetime.now(timezone.utc) - start_time).total_seconds() * 1000)
    return DeepAnalyzeResponse(
        query=data.query,
        answer=str(answer.get("answer", "")) if isinstance(answer, dict) else str(answer),
        reasoning_path=answer.get("reasoning_path", []) if isinstance(answer, dict) else [],
        evidence=answer.get("evidence", []) if isinstance(answer, dict) else [],
        anomalies=(answer.get("anomalies") if isinstance(answer, dict) else None) if data.include_anomalies else None,
        recommendations=(answer.get("recommendations") if isinstance(answer, dict) else None) if data.include_recommendations else None,
        confidence=float(answer.get("confidence", 0.5)) if isinstance(answer, dict) else 0.5,
        sources=answer.get("sources", []) if isinstance(answer, dict) else [],
        processing_time_ms=processing_time,
    )


@get("/meetings")
async def list_meetings(
    limit: int = 20,
    offset: int = 0,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Список встреч из Postgres.

    Контракт ответа стабилен — даже если внутренняя схема таблицы поменяется,
    клиенты получат одинаковый JSON.
    """
    try:
        from sqlalchemy import select, text

        from backend.db.postgres import get_postgres
    except Exception:
        return {"meetings": [], "total": 0, "limit": limit, "offset": offset, "error": "db_unavailable"}

    pg = await get_postgres()
    async with pg.session() as session:
        where = "WHERE project_id = :pid" if project_id else ""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if project_id:
            params["pid"] = project_id
        try:
            count_row = await session.execute(
                text(f"SELECT COUNT(*) FROM meetings {where}"),
                params,
            )
            total = int(count_row.scalar() or 0)
            list_rows = await session.execute(
                text(
                    "SELECT id, title, project_id, meeting_date, transcript_length "
                    f"FROM meetings {where} ORDER BY meeting_date DESC NULLS LAST "
                    "LIMIT :limit OFFSET :offset"
                ),
                params,
            )
            rows = [dict(r._mapping) for r in list_rows.fetchall()]
        except Exception as exc:
            logger.warning("list_meetings: query failed (table may not exist yet): %s", exc)
            return {"meetings": [], "total": 0, "limit": limit, "offset": offset}

    return {"meetings": rows, "total": total, "limit": limit, "offset": offset}


@get("/meetings/{meeting_id:str}")
async def get_meeting(meeting_id: str) -> dict[str, Any]:
    """Детали одной встречи + извлечённые сущности из Neo4j."""
    try:
        from sqlalchemy import text

        from backend.db.neo4j_client import get_neo4j
        from backend.db.postgres import get_postgres
    except Exception:
        raise HTTPException(status_code=503, detail="Storage layer unavailable")

    pg = await get_postgres()
    async with pg.session() as session:
        try:
            row = await session.execute(
                text("SELECT * FROM meetings WHERE id = :id"),
                {"id": meeting_id},
            )
            meeting = row.fetchone()
        except Exception as exc:
            logger.warning("get_meeting: pg query failed: %s", exc)
            meeting = None
    if not meeting:
        raise HTTPException(status_code=404, detail=f"Meeting {meeting_id} not found")

    # Сущности связанные со встречей в графе.
    entities: list[dict[str, Any]] = []
    try:
        neo4j = await get_neo4j()
        result = await neo4j.execute_query(
            "MATCH (e)-[:EXTRACTED_FROM]->(m {id: $mid}) RETURN e LIMIT 100",
            {"mid": meeting_id},
        )
        entities = [dict(r["e"]) for r in (result or [])]
    except Exception as exc:
        logger.debug("get_meeting: neo4j entities lookup failed: %s", exc)

    return {
        "meeting_id": meeting_id,
        "status": "ok",
        "data": dict(meeting._mapping),
        "entities": entities,
    }


# Router
router = Router(
    path="/analyze",
    route_handlers=[ingest_meeting, deep_analyze, list_meetings, get_meeting],
    tags=["Analysis"],
)
