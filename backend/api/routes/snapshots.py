"""
TESSENT BRAIN - Snapshot Routes
Эндпоинты для работы со слепками состояния сущностей
"""
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Optional

import structlog
from litestar import Router, get, post
from backend.core.auth.user_guard import enforce_user_id_matches_token
from litestar.params import Parameter
from pydantic import BaseModel, Field

from backend.core.sleep.snapshot_generator import SnapshotPeriod

if TYPE_CHECKING:
    from backend.core.sleep.snapshot_generator import SnapshotGenerator

logger = structlog.get_logger(__name__)

# Кеш генераторов по user_id
_generators_cache: "dict[str, SnapshotGenerator]" = {}

def _graph_is_stale(graph_builder) -> bool:
    """True, если у кэшированного генератора граф — устаревший NetworkX-фолбэк.

    merged_graph_view_for_user при недоступном Neo4j молча отдаёт NetworkX,
    загруженный из файла data/graphs/<uid>.json. Если генератор впервые
    создался в такой момент (Neo4j ещё поднимался при старте), он навсегда
    кэшировал фолбэк-граф, и список людей/team360 не видит ни слияний дедупа
    (они пишутся в Neo4j), ни свежих узлов. Считаем граф протухшим, если
    система настроена на Neo4j, а бэкенд у графа — networkx/нет соединения.
    """
    import os as _os
    if _os.environ.get("USE_NETWORKX", "").lower() in ("true", "1", "yes"):
        return False  # NetworkX — штатный режим, фолбэка нет
    if graph_builder is None:
        return True
    return (getattr(graph_builder, "backend", None) != "neo4j"
            or not getattr(graph_builder, "connected", False))


async def _get_generator(user_id: str):
    """Получить или создать генератор снимков для конкретного пользователя"""
    global _generators_cache

    from backend.core.sleep.snapshot_generator import SnapshotGenerator
    from backend.core.store.graph_view import merged_graph_view_for_user
    from backend.core.store.tenant_paths import vector_index_path_for_user
    from backend.core.store.vector_indexer import VectorIndexer

    cached = _generators_cache.get(user_id)
    if cached is not None and not _graph_is_stale(getattr(cached, "graph", None)):
        return cached
    if cached is not None:
        # Протухший фолбэк-граф: пересоздаём граф вживую (Neo4j теперь доступен),
        # переиспользуя уже прогретый vector_indexer (модель эмбеддингов).
        try:
            fresh = await merged_graph_view_for_user(user_id, use_networkx=None)
            if not _graph_is_stale(fresh):
                cached.graph = fresh
                logger.info("♻️ refreshed stale graph for generator user=%s", user_id)
                return cached
        except Exception as e:
            logger.error(f"Failed to refresh graph for user {user_id}: {e}")

    try:
        graph_builder = await merged_graph_view_for_user(user_id, use_networkx=None)
    except Exception as e:
        logger.error(f"Failed to load federated graph for user {user_id}: {e}")
        graph_builder = None

    # Холодная загрузка sentence-transformers внутри HTTP-запроса роняет
    # воркер granian на Windows (тот же краш, что был в редакторе знаний).
    # Векторный индекс подключаем ТОЛЬКО если модель эмбеддингов уже
    # прогрета (ночной консолидацией/ингестом); иначе граф-only — все
    # снапшот-эндпоинты это переживают.
    vector_indexer = None
    try:
        from backend.core.store.vector_indexer import (
            _EMBEDDING_MODEL_CACHE as _vi_cache,
        )
    except Exception:
        _vi_cache = None
    if _vi_cache:
        vector_indexer = VectorIndexer(
            storage_path=vector_index_path_for_user(user_id),
            namespace=user_id,
        )
        try:
            await vector_indexer.connect()
        except Exception as e:
            logger.error(f"Failed to connect vector indexer for user {user_id}: {e}")
    else:
        logger.info("snapshots: embedding model cold — graph-only generator "
                    "(без падения воркера)")

    generator = SnapshotGenerator(graph_builder, vector_indexer)
    _generators_cache[user_id] = generator
    return generator

# Типы слепков
SnapshotType = Literal["project", "person", "team", "department", "company"]


# === Response Models ===

class ProjectSnapshot(BaseModel):
    """Слепок состояния проекта"""
    project_id: str
    name: str
    generated_at: datetime

    # Статус
    status: str
    health_score: float = Field(..., ge=0, le=1)

    # Метрики
    metrics: dict[str, Any] = Field(default_factory=dict)

    # Задачи
    tasks: dict[str, Any] = Field(default_factory=dict)

    # Люди
    team: list[dict[str, Any]] = Field(default_factory=list)

    # Проблемы и риски
    problems: list[dict[str, Any]] = Field(default_factory=list)
    risks: list[dict[str, Any]] = Field(default_factory=list)

    # Решения
    recent_decisions: list[dict[str, Any]] = Field(default_factory=list)

    # Аномалии
    anomalies: list[dict[str, Any]] = Field(default_factory=list)

    # Рекомендации
    recommendations: list[str] = Field(default_factory=list)


class PersonSnapshot(BaseModel):
    """Слепок состояния сотрудника"""
    person_id: str
    name: str
    generated_at: datetime

    # Роль и отдел
    role: str | None = None
    department: str | None = None

    # Метрики активности
    activity_metrics: dict[str, Any] = Field(default_factory=dict)

    # Проекты
    projects: list[dict[str, Any]] = Field(default_factory=list)

    # Задачи
    tasks: dict[str, Any] = Field(default_factory=dict)

    # Коммуникации
    communication_patterns: dict[str, Any] = Field(default_factory=dict)

    # Тренды
    trends: list[dict[str, Any]] = Field(default_factory=list)


# === Route Handlers ===


@get("/project/{project_id:str}")
async def get_project_snapshot(
    project_id: str,
    user_id: str = Parameter(query="user_id", required=True),
    regenerate: bool = False,
) -> dict[str, Any]:
    """
    Получить слепок состояния проекта

    Слепок включает:
    - Общий статус и health score
    - Метрики (задачи, бюджет, сроки)
    - Команда проекта
    - Активные проблемы и риски
    - Недавние решения
    - Обнаруженные аномалии
    - Рекомендации

    Args:
        project_id: ID проекта
        user_id: ID пользователя
        regenerate: Принудительно пересоздать слепок

    Returns:
        Структурированный слепок проекта
    """
    logger.info("📸 Generating project snapshot", project_id=project_id, user_id=user_id)

    try:
        generator = await _get_generator(user_id)
        snapshot = await generator.create_detailed_project_snapshot(project_id)

        if "error" in snapshot:
            return {
                "project_id": project_id,
                "error": snapshot["error"],
                "message": "Project not found in knowledge graph"
            }

        return snapshot

    except Exception as e:
        logger.error(f"Error generating project snapshot: {e}")
        return {
            "project_id": project_id,
            "error": str(e),
            "message": "Failed to generate snapshot"
        }


@get("/person/{person_id:str}")
async def get_person_snapshot(
    person_id: str,
    user_id: str = Parameter(query="user_id", required=True),
    regenerate: bool = False,
) -> dict[str, Any]:
    """
    Получить слепок состояния сотрудника

    Args:
        person_id: ID или имя сотрудника
        user_id: ID пользователя
        regenerate: Принудительно пересоздать слепок

    Returns:
        Структурированный слепок сотрудника
    """
    logger.info("📸 Generating person snapshot", person_id=person_id, user_id=user_id)

    try:
        generator = await _get_generator(user_id)
        profile = await generator.create_person_profile(person_id)

        if "error" in profile:
            return {
                "person_id": person_id,
                "error": profile["error"],
                "message": "Person not found in knowledge graph"
            }

        # Профиль человека — чувствительное чтение: службе безопасности
        # заказчика важно «кто на кого смотрел», а раньше журналировались
        # только изменения. Повторы в пределах короткого окна не пишутся.
        from backend.core.observability.read_audit import record_read
        await record_read(user_id, resource_type="person_profile",
                          resource_id=person_id)

        return profile

    except Exception as e:
        logger.error(f"Error generating person snapshot: {e}")
        return {
            "person_id": person_id,
            "error": str(e),
            "message": "Failed to generate snapshot"
        }


@get("/people")
async def get_all_people_snapshots(
    user_id: str = Parameter(query="user_id", required=True),
) -> dict[str, Any]:
    """
    Получить профили всех сотрудников

    Args:
        user_id: ID пользователя

    Returns:
        Список профилей всех сотрудников
    """
    logger.info("📸 Generating all people snapshots", user_id=user_id)

    try:
        generator = await _get_generator(user_id)
        profiles = await generator.get_all_people_profiles(tenant_id=user_id)

        return {
            "total": len(profiles),
            "profiles": profiles
        }

    except Exception as e:
        logger.error(f"Error generating people snapshots: {e}")
        return {
            "total": 0,
            "profiles": [],
            "error": str(e)
        }


@get("/team360")
async def get_team_360(
    user_id: str = Parameter(query="user_id", required=True),
    limit: int = Parameter(query="limit", default=30),
) -> dict[str, Any]:
    """360-обзор команды: агрегированные метрики по каждому сотруднику в одном
    сравнительном наборе (вклад, решения, задачи, встречи, психопрофиль,
    достижения/вызовы, зоны роста).

    Грунтуется на тех же PersonSnapshot, что и карточка сотрудника — никаких
    отдельных «оценок», только атрибутированные данные. Масштабируемо: берём
    top-N по вовлечённости (агентство может иметь тысячи внешних контактов —
    360 строится по внутренней команде, не по всему графу).
    """
    logger.info("📸 Building team 360 review", user_id=user_id, limit=limit)
    limit = max(1, min(int(limit or 30), 60))

    try:
        from backend.core.sleep.enhanced_snapshot import get_enhanced_snapshot_generator
        from backend.core.store.graph_view import merged_graph_view_for_user

        graph_builder = await merged_graph_view_for_user(user_id, use_networkx=None)
        generator = get_enhanced_snapshot_generator(graph_builder, user_id=user_id)
        generator.user_id = user_id

        # Лёгкий список людей (отсортирован по вовлечённости) → берём верх.
        light = await generator.get_all_people_profiles(tenant_id=user_id)
        candidates = [p for p in light if p.get("person_id") or p.get("id")][:limit]

        rows: list[dict[str, Any]] = []
        for p in candidates:
            pid = p.get("person_id") or p.get("id")
            try:
                snap = await generator.get_person_snapshot(pid, force_regenerate=False)
            except Exception as e:
                logger.debug(f"team360: snapshot failed for {pid}: {e}")
                snap = None
            if not snap:
                continue
            psych = getattr(snap, "psychological", None) or {}
            rows.append({
                "person_id": pid,
                "name": getattr(snap, "name", p.get("name", "")),
                "role": getattr(snap, "role", p.get("role", "")) or p.get("role", ""),
                "department": getattr(snap, "department", "") or p.get("department", ""),
                "collaboration_score": getattr(snap, "collaboration_score", 0) or 0,
                "decisions_made": getattr(snap, "decisions_made", 0) or 0,
                "tasks_completed": getattr(snap, "tasks_completed_week", 0) or 0,
                "tasks_in_progress": getattr(snap, "tasks_in_progress", 0) or 0,
                "meetings": getattr(snap, "meetings_participated", 0) or 0,
                "achievements_count": len(getattr(snap, "recent_achievements", []) or []),
                "challenges_count": len(getattr(snap, "current_challenges", []) or []),
                "personality_type": psych.get("personality_type", "") if isinstance(psych, dict) else "",
                "team_role": psych.get("team_role", "") if isinstance(psych, dict) else "",
                "strengths": (psych.get("strengths") or [])[:3] if isinstance(psych, dict) else [],
                "areas_for_improvement": (getattr(snap, "areas_for_improvement", []) or [])[:3],
            })

        await graph_builder.close(save=False)

        # Сводка по команде (для шапки отчёта).
        n = len(rows)
        avg = lambda key: round(sum(r.get(key, 0) for r in rows) / n, 1) if n else 0
        rows.sort(key=lambda r: r.get("collaboration_score", 0), reverse=True)
        summary = {
            "people_count": n,
            "avg_collaboration": avg("collaboration_score"),
            "total_decisions": sum(r.get("decisions_made", 0) for r in rows),
            "total_tasks_completed": sum(r.get("tasks_completed", 0) for r in rows),
            "with_psych_profile": sum(1 for r in rows if r.get("personality_type")),
            "top_contributors": [r["name"] for r in rows[:3]],
        }
        return {"status": "ok", "summary": summary, "people": rows}

    except Exception as e:
        logger.error(f"Error building team 360: {e}")
        return {"status": "error", "message": str(e), "people": [], "summary": {}}


@get("/duplicate-candidates")
async def get_duplicate_candidates(
    user_id: str = Parameter(query="user_id", required=True),
    threshold: float = Parameter(query="threshold", default=0.80),
) -> dict[str, Any]:
    """Превью дублей людей БЕЗ слияния — content-aware кластеры.

    LLM группирует записи одного человека ОДНИМ вызовом, учитывая активность
    (проекты/коллеги/отдел), а не только имя: Катя/Екатерина/Катя Пустовалова
    в одну группу, но «Максим Белухин» и «Максим Фельдман» — раздельно (разные
    люди). Граф НЕ меняется. Для каждой группы: участники, confidence, причина
    и tier (auto — сольётся при консолидации; review — предложат подтвердить).
    """
    try:
        from backend.core.store.entity_resolver import EntityResolver
        from backend.core.store.graph_view import merged_graph_view_for_user

        graph_builder = await merged_graph_view_for_user(user_id, use_networkx=None)
        try:
            resolver = EntityResolver(graph_builder=graph_builder)
            clusters = await resolver.cluster_duplicates_llm(
                entity_type="person", tenant_id=user_id)
        finally:
            await graph_builder.close(save=False)

        _auto = resolver.llm_auto_merge_confidence
        out = []
        for c in clusters:
            conf = c.get("confidence", 0.0)
            out.append({
                "member_ids": c.get("member_ids", []),
                "members": c.get("member_names", []),
                "canonical": c.get("canonical", ""),
                "confidence": round(conf, 2),
                "reason": c.get("reason", ""),
                "tier": "auto" if conf >= _auto else ("review" if conf >= 0.55 else "low"),
            })
        # Сначала те, что сольются авто, потом review — так нагляднее.
        out.sort(key=lambda x: x["confidence"], reverse=True)
        return {"status": "ok", "count": len(out), "clusters": out}
    except Exception as e:
        logger.error(f"duplicate-candidates failed: {e}")
        return {"status": "error", "message": str(e), "clusters": []}


@get("/team/{team_id:str}")
async def get_team_snapshot(
    team_id: str,
    user_id: str = Parameter(query="user_id", required=True),
    format: str = Parameter(query="format", default="json"),
) -> dict[str, Any]:
    """
    Слепок состояния команды (lead, members, velocity, health, проекты,
    дельты). Логика — EnhancedSnapshotGenerator.get_team_snapshot (была
    написана ранее, endpoint был заглушкой — подключено,
    docs/CAPABILITY_READINESS.md, быстрая победа B3).
    """
    logger.info("📸 Getting team snapshot", team_id=team_id, user_id=user_id)
    try:
        from backend.core.sleep.enhanced_snapshot import get_enhanced_snapshot_generator
        from backend.core.store.graph_view import merged_graph_view_for_user

        # Wave 2.4: federated read — team-снимок включает promoted org-узлы
        # (коллеги/проекты, поднятые в org-граф), а не только personal-граф.
        graph_builder = await merged_graph_view_for_user(user_id, use_networkx=None)
        generator = get_enhanced_snapshot_generator(graph_builder, user_id=user_id)
        generator.user_id = user_id
        snapshot = await generator.get_team_snapshot(team_id, force_regenerate=True)
        await graph_builder.close(save=False)

        if not snapshot:
            return {"team_id": team_id, "found": False,
                    "message": "Команда не найдена в графе знаний"}
        if format == "text":
            return {"team_id": team_id, "found": True, "format": "text",
                    "text": snapshot.to_text()}
        return {"team_id": team_id, "found": True, "snapshot": snapshot.to_dict()}
    except Exception as e:
        logger.error(f"Error getting team snapshot: {e}")
        return {"team_id": team_id, "found": False, "error": str(e)}


@get("/department/{department_id:str}")
async def get_department_snapshot(
    department_id: str,
    user_id: str = Parameter(query="user_id", required=True),
    format: str = Parameter(query="format", default="json"),
) -> dict[str, Any]:
    """
    Слепок отдела (head, teams, key people, проекты, KPI, риски, блокеры).
    Логика существовала (get_department_snapshot) — endpoint добавлен
    (docs/CAPABILITY_READINESS.md, быстрая победа B3).
    """
    logger.info("📸 Getting department snapshot",
                department_id=department_id, user_id=user_id)
    try:
        from backend.core.sleep.enhanced_snapshot import get_enhanced_snapshot_generator
        from backend.core.store.graph_view import merged_graph_view_for_user

        # Wave 2.4: federated read — снимок отдела включает promoted org-узлы,
        # а не только personal-граф (иначе head/teams/people были бы неполными).
        graph_builder = await merged_graph_view_for_user(user_id, use_networkx=None)
        generator = get_enhanced_snapshot_generator(graph_builder, user_id=user_id)
        generator.user_id = user_id
        snapshot = await generator.get_department_snapshot(
            department_id, force_regenerate=True)
        await graph_builder.close(save=False)

        if not snapshot:
            return {"department_id": department_id, "found": False,
                    "message": "Отдел не найден в графе знаний"}
        if format == "text":
            return {"department_id": department_id, "found": True, "format": "text",
                    "text": snapshot.to_text()}
        return {"department_id": department_id, "found": True,
                "snapshot": snapshot.to_dict()}
    except Exception as e:
        logger.error(f"Error getting department snapshot: {e}")
        return {"department_id": department_id, "found": False, "error": str(e)}


@get("/company")
async def get_company_snapshot(
    user_id: str = Parameter(query="user_id", required=True),
) -> dict[str, Any]:
    """
    Получить слепок состояния всей компании

    Args:
        user_id: ID пользователя

    Returns:
        Агрегированный слепок компании
    """
    logger.info("📸 Generating company snapshot", user_id=user_id)

    try:
        generator = await _get_generator(user_id)
        snapshot = await generator.create_organization_snapshot(period=SnapshotPeriod.WEEKLY)

        if snapshot:
            result = snapshot.to_dict()
            # Добавляем текстовое представление для LLM
            result["text_summary"] = await generator.get_current_snapshot_text()
            return result

    except Exception as e:
        logger.error(f"Error generating snapshot: {e}")

    return {
        "generated_at": datetime.utcnow().isoformat(),
        "projects": {
            "total": 0,
            "active": 0,
            "at_risk": 0,
            "completed": 0,
        },
        "people": {
            "total": 0,
            "active": 0,
        },
        "tasks": {
            "total": 0,
            "completed": 0,
            "overdue": 0,
        },
        "health_indicators": {
            "overall_health": 0.0,
            "project_health": 0.0,
            "team_health": 0.0,
        },
        "top_risks": [],
        "top_opportunities": [],
        "recent_anomalies": [],
        "message": "Snapshot generation failed or returned empty",
    }


@post("/generate")
async def generate_snapshot(
    entity_type: SnapshotType,
    entity_id: str | None = None,
) -> dict[str, Any]:
    """
    Принудительно сгенерировать слепок

    Args:
        entity_type: Тип сущности
        entity_id: ID сущности (для company не требуется)

    Returns:
        Статус генерации
    """
    logger.info(
        "📸 Snapshot generation requested",
        entity_type=entity_type,
        entity_id=entity_id,
    )

    # TODO: Запустить фоновую задачу генерации

    return {
        "status": "queued",
        "entity_type": entity_type,
        "entity_id": entity_id,
        "message": "Snapshot generation queued (not yet implemented)",
    }


# ============================================================================
# ENHANCED SNAPSHOTS (новая система)
# ============================================================================

@get("/enhanced/company")
async def get_enhanced_company_snapshot(
    user_id: str = Parameter(query="user_id", required=True),
    format: str = Parameter(query="format", default="json"),  # json или text
    regenerate: bool = Parameter(query="regenerate", default=False),
) -> dict[str, Any]:
    """
    Получить расширенный снапшот компании.

    Включает 10 разделов:
    - Базовая информация и миссия
    - Структура (отделы, команды)
    - Текущее состояние и приоритеты
    - Активные проекты
    - Цели и стратегия
    - SWOT-анализ
    - Достижения и проблемы
    - Технологии и инструменты
    - Ключевые метрики
    - Тренды

    Args:
        user_id: ID пользователя
        format: Формат ответа (json/text)
    """
    logger.info("📸 Getting enhanced company snapshot", user_id=user_id)

    try:
        from backend.core.sleep.enhanced_snapshot import get_enhanced_snapshot_generator
        from backend.core.store.graph_view import merged_graph_view_for_user

        # Получаем граф пользователя
        # Wave 2.4: federated read — snapshot включает promoted org-узлы.
        graph_builder = await merged_graph_view_for_user(user_id, use_networkx=None)

        generator = get_enhanced_snapshot_generator(graph_builder, user_id=user_id)
        generator.user_id = user_id
        # regenerate=true — пересобрать из ТЕКУЩЕГО графа пользователя: старый
        # закешированный снапшот мог быть сгенерирован до анти-IDOR guard'ов
        # из загрязнённых данных («чужая компания» в своём аккаунте)
        snapshot = await generator.get_company_snapshot(force_regenerate=bool(regenerate))

        await graph_builder.close(save=False)

        if format == "text":
            return {
                "format": "text",
                "content": snapshot.to_text(),
                "last_updated": snapshot.last_updated,
            }

        return {
            "format": "json",
            "snapshot": snapshot.to_dict(),
        }

    except Exception as e:
        logger.error(f"Error getting enhanced company snapshot: {e}")
        return {
            "error": str(e),
            "message": "Failed to get enhanced company snapshot"
        }


async def _fetch_entity_timeline(user_id: str, entity_id: str, limit: int = 100) -> list:
    """Реальный таймлайн сущности из TemporalTracker (per-user SQLite).

    Это ЖИВОЙ темпоральный слой: события (created/participated/decided/…)
    пишутся на каждой обработанной встрече. graphiti тут ни при чём — он
    мёртвый код. Best-effort: ошибки → пустой список.
    """
    try:
        from backend.core.temporal.temporal_tracker import get_temporal_tracker
        tracker = get_temporal_tracker(user_id)
        events = await tracker.get_entity_timeline(entity_id, limit=limit)
        # Чистим до компактного вида для UI (хронология по убыванию даты).
        out = []
        for ev in events or []:
            out.append({
                "event_type": ev.get("event_type"),
                "entity_type": ev.get("entity_type"),
                "event_time": ev.get("event_time"),
                "meeting_id": ev.get("meeting_id"),
                "description": ev.get("description"),
                "event_data": ev.get("event_data") or {},
            })
        return out
    except Exception as e:
        logger.warning(f"timeline fetch failed for {entity_id}: {e}")
        return []


def _synthetic_timeline(snap: dict) -> list:
    """Собрать таймлайн ПРЯМО из данных снапшота (граф), когда TemporalTracker
    пуст (на Neo4j id событий = UUID и не совпадали с запросом → история была
    пустой). Источники: встречи (с датами), решения, недавние решения проекта.
    """
    out = []
    for m in (snap.get("recent_meetings") or []):
        d = m.get("date") or ""
        title = m.get("title") or "встреча"
        out.append({
            "event_type": "participated", "entity_type": "meeting",
            "event_time": d, "meeting_id": m.get("meeting_id"),
            "description": f"Встреча: {title}",
        })
    for dec in (snap.get("decisions") or []):
        s = dec.get("summary") or ""
        if s:
            out.append({
                "event_type": "decided", "entity_type": "decision",
                "event_time": dec.get("date") or "", "description": f"Решение: {s}",
            })
    for dec in (snap.get("recent_decisions") or []):
        s = dec.get("decision") or dec.get("summary") or ""
        if s:
            out.append({
                "event_type": "decided", "entity_type": "decision",
                "event_time": dec.get("date") or "", "description": f"Решение: {s}",
            })
    out = [e for e in out if (e.get("description") or "").strip()]
    out.sort(key=lambda e: e.get("event_time") or "", reverse=True)
    return out[:40]


class SnapshotOverrideRequest(BaseModel):
    """Ручная правка поля снапшота (переживает ре-генерацию)."""
    field: str = Field(..., description="Имя поля снапшота, напр. 'founder' или 'role'")
    value: Any = Field(..., description="Новое значение (любого JSON-типа)")


@get("/entity/{entity_id:str}/timeline")
async def get_entity_timeline_route(
    entity_id: str,
    user_id: str = Parameter(query="user_id", required=True),
    limit: int = Parameter(query="limit", default=100),
) -> dict[str, Any]:
    """История сущности (человек/проект/решение/задача) во времени.

    Источник — TemporalTracker (живые данные), не graphiti. Отвечает на
    «как менялось / когда что происходило» по конкретной сущности.
    """
    timeline = await _fetch_entity_timeline(user_id, entity_id, limit=limit)
    return {"entity_id": entity_id, "count": len(timeline), "timeline": timeline}


@post("/enhanced/company/override")
async def set_company_snapshot_override(
    data: SnapshotOverrideRequest,
    user_id: str = Parameter(query="user_id", required=True),
) -> dict[str, Any]:
    """Ручная правка поля снапшота КОМПАНИИ (напр. исправить CEO/founder).

    Правка хранится отдельно и накладывается поверх при каждой ре-генерации —
    поэтому больше не перетирается авто-генерацией.
    """
    try:
        from backend.core.sleep.enhanced_snapshot import get_enhanced_snapshot_generator
        from backend.core.store.graph_view import merged_graph_view_for_user

        graph_builder = await merged_graph_view_for_user(user_id, use_networkx=None)
        generator = get_enhanced_snapshot_generator(graph_builder, user_id=user_id)
        generator.user_id = user_id
        generator.set_company_override(data.field, data.value)
        snapshot = await generator.get_company_snapshot()
        await graph_builder.close(save=False)
        return {"status": "ok", "field": data.field, "snapshot": snapshot.to_dict()}
    except Exception as e:
        logger.error(f"Error setting company override: {e}")
        return {"status": "error", "message": str(e)}


@post("/enhanced/person/{person_id:str}/override")
async def set_person_snapshot_override(
    person_id: str,
    data: SnapshotOverrideRequest,
    user_id: str = Parameter(query="user_id", required=True),
) -> dict[str, Any]:
    """Ручная правка поля снапшота ЧЕЛОВЕКА (напр. role/department)."""
    try:
        from backend.core.sleep.enhanced_snapshot import get_enhanced_snapshot_generator
        from backend.core.store.graph_view import merged_graph_view_for_user

        graph_builder = await merged_graph_view_for_user(user_id, use_networkx=None)
        generator = get_enhanced_snapshot_generator(graph_builder, user_id=user_id)
        generator.user_id = user_id
        generator.set_person_override(person_id, data.field, data.value)
        snapshot = await generator.get_person_snapshot(person_id)
        await graph_builder.close(save=False)
        return {
            "status": "ok", "field": data.field,
            "snapshot": snapshot.to_dict() if snapshot else None,
        }
    except Exception as e:
        logger.error(f"Error setting person override: {e}")
        return {"status": "error", "message": str(e)}


class PersonClassificationRequest(BaseModel):
    """Ручная классификация человека (переживает ре-генерацию)."""
    classification: str = Field(
        ...,
        description="employee | partner | external | candidate | client",
    )


class EntityDeleteRequest(BaseModel):
    """Удаление сущности прямо из снепшота (ошибка распознавания и т.п.)."""
    entity_id: Optional[str] = Field(None, description="id узла графа (если известен)")
    name: str = Field(..., description="имя сущности («Ишка»)")
    label: Optional[str] = Field(None, description="метка узла (Person/Product/…)")
    comment: Optional[str] = Field(None, description="почему («это ИИшка, не человек»)")


@post("/enhanced/entity/delete")
async def delete_snapshot_entity_route(
    data: EntityDeleteRequest,
    user_id: str = Parameter(query="user_id", required=True),
) -> dict[str, Any]:
    """Удалить сущность из графа + tombstone «не пересоздавать».

    Кейс: транскрибация услышала «ИИшка» как «Ишка» → появился «сотрудник».
    Удаление переживает пересборку памяти: create_node пропускает имена из
    tombstone-памяти. Возвращает свежий снепшот компании."""
    name = (data.name or "").strip()
    if not name:
        return {"status": "error", "message": "name required"}
    try:
        from backend.core.store.entity_tombstones import add_tombstone
        from backend.core.store.graph_view import merged_graph_view_for_user

        # Удаляем на ЗАПИСЫВАЕМОМ персональном графе. Раньше удаление шло на
        # merged-view: в NetworkX-режиме его save заблокирован (read-only),
        # и узел молча возвращался после перезапуска — «удалил, а он снова».
        from backend.core.store.graph_builder import GraphBuilder
        from backend.core.store.tenant_paths import graph_path_for_user
        gb_w = GraphBuilder(use_networkx=None,
                            graph_storage_path=graph_path_for_user(user_id))
        await gb_w.connect()
        removed_ids: list[str] = []
        try:
            # по id — прямое удаление; иначе точные совпадения имени по меткам
            if data.entity_id:
                if await gb_w.remove_node(data.entity_id):
                    removed_ids.append(data.entity_id)
            else:
                labels = [data.label] if data.label else [
                    "Person", "Entity", "Product", "Team", "Department"]
                nname = name.lower()
                for lb in labels:
                    try:
                        found = await gb_w.find_nodes_by_label(
                            lb, limit=500, tenant_id=user_id, strict_tenant=True)
                    except Exception:
                        found = []
                    for n in found:
                        nm = str(n.get("name") or n.get("title") or "").strip().lower()
                        if nm == nname and n.get("id"):
                            if await gb_w.remove_node(n["id"]):
                                removed_ids.append(n["id"])
        finally:
            await gb_w.close(save=True)

        # tombstone пишем в любом случае: даже если узла уже нет, память
        # «не пересоздавать» должна остаться. Он же скрывает копии из
        # org-графа/старых снапшотов (read-фильтр в генерации).
        add_tombstone(user_id, name, data.label, data.comment)

        # свежий снепшот компании (без удалённой сущности) — по merged-view,
        # как его читают остальные роуты
        snapshot = None
        gb = await merged_graph_view_for_user(user_id, use_networkx=None)
        try:
            from backend.core.sleep.enhanced_snapshot import get_enhanced_snapshot_generator
            generator = get_enhanced_snapshot_generator(gb, user_id=user_id)
            generator.user_id = user_id
            snapshot = await generator.get_company_snapshot(force_regenerate=True)
        except Exception as e:
            logger.warning(f"snapshot regenerate after delete failed: {e}")
        await gb.close(save=False)

        logger.info(f"⚰️ Entity deleted from snapshot: «{name}» "
                    f"(nodes removed: {len(removed_ids)}, user={user_id[:8]})")
        return {
            "status": "ok",
            "name": name,
            "removed_node_ids": removed_ids,
            "tombstoned": True,
            "snapshot": snapshot.to_dict() if snapshot else None,
        }
    except Exception as e:
        logger.error(f"Error deleting snapshot entity: {e}")
        return {"status": "error", "message": str(e)}


@post("/enhanced/person/{person_id:str}/classification")
async def set_person_classification_route(
    person_id: str,
    data: PersonClassificationRequest,
    user_id: str = Parameter(query="user_id", required=True),
) -> dict[str, Any]:
    """Ручная классификация человека — напр. перевести из «внешних» в
    «сотрудники» (employee). Переживает ре-генерацию снапшота и влияет на
    состав key_people/related_people на странице «Компания».
    """
    allowed = {"employee", "management", "partner", "external",
               "candidate", "client"}
    cls = (data.classification or "").strip().lower()
    if cls not in allowed:
        return {"status": "error", "message": f"classification must be one of {sorted(allowed)}"}
    try:
        from backend.core.sleep.enhanced_snapshot import get_enhanced_snapshot_generator
        from backend.core.store.graph_view import merged_graph_view_for_user

        graph_builder = await merged_graph_view_for_user(user_id, use_networkx=None)
        generator = get_enhanced_snapshot_generator(graph_builder, user_id=user_id)
        generator.user_id = user_id
        generator.set_person_classification(person_id, cls)
        snapshot = await generator.get_company_snapshot(force_regenerate=True)
        await graph_builder.close(save=False)
        return {
            "status": "ok",
            "person_id": person_id,
            "classification": cls,
            "snapshot": snapshot.to_dict() if snapshot else None,
        }
    except Exception as e:
        logger.error(f"Error setting person classification: {e}")
        return {"status": "error", "message": str(e)}


@get("/enhanced/person/{person_id:str}")
async def get_enhanced_person_snapshot(
    person_id: str,
    user_id: str = Parameter(query="user_id", required=True),
    format: str = Parameter(query="format", default="json"),
    name: str = Parameter(query="name", default=""),
) -> dict[str, Any]:
    """
    Получить расширенный снапшот сотрудника.

    Включает 8 разделов:
    - Базовая информация
    - Иерархия (руководитель, коллеги)
    - Компетенции и навыки
    - Текущие проекты и задачи
    - Взаимодействие с коллегами
    - Производительность
    - Сильные стороны и области для улучшения
    - Достижения и вызовы

    Args:
        person_id: ID сотрудника
        user_id: ID пользователя
        format: Формат ответа (json/text)
    """
    logger.info("📸 Getting enhanced person snapshot", person_id=person_id, user_id=user_id)

    try:
        from backend.core.sleep.enhanced_snapshot import get_enhanced_snapshot_generator
        from backend.core.store.graph_view import merged_graph_view_for_user

        # Wave 2.4: federated read — snapshot включает promoted org-узлы.
        graph_builder = await merged_graph_view_for_user(user_id, use_networkx=None)

        # Передаём graph_builder — singleton обновит граф
        generator = get_enhanced_snapshot_generator(graph_builder, user_id=user_id)
        generator.user_id = user_id

        # force_regenerate=True чтобы всегда искать в текущем графе
        snapshot = await generator.get_person_snapshot(person_id, force_regenerate=True)

        # Синтетический id из иерархии/оргсхемы (p:…, person_…) может не быть
        # узлом графа — но ИМЯ человека резолвится штатно. Фолбэк по имени,
        # которое прислал UI, прежде чем честно сдаться.
        if not snapshot and name.strip() and name.strip() != person_id:
            snapshot = await generator.get_person_snapshot(
                name.strip(), force_regenerate=False)

        await graph_builder.close(save=False)

        if not snapshot:
            return {
                "person_id": person_id,
                "error": "Person not found",
                "message": f"Сотрудник '{person_id}' не найден в графе знаний. Запустите синхронизацию.",
            }

        if format == "text":
            return {
                "format": "text",
                "content": snapshot.to_text(),
                "last_updated": snapshot.last_updated,
            }

        # Снепшот = таймлайн: реальная история из TemporalTracker; если пусто
        # (на Neo4j id событий не совпадали) — собираем из данных графа.
        _snap_dict = snapshot.to_dict()
        timeline = await _fetch_entity_timeline(user_id, person_id)
        if not timeline:
            timeline = _synthetic_timeline(_snap_dict)

        return {
            "format": "json",
            "snapshot": _snap_dict,
            "timeline": timeline,
        }

    except Exception as e:
        logger.error(f"Error getting enhanced person snapshot: {e}")
        return {
            "person_id": person_id,
            "error": str(e),
        }


@get("/enhanced/project/{project_id:str}")
async def get_enhanced_project_snapshot(
    project_id: str,
    user_id: str = Parameter(query="user_id", required=True),
    format: str = Parameter(query="format", default="json"),
) -> dict[str, Any]:
    """
    Получить расширенный снапшот проекта.

    Включает 8 разделов:
    - Базовая информация и статус
    - Команда проекта
    - Прогресс и milestones
    - Задачи (выполнено/в работе/заблокировано)
    - Риски и блокеры
    - Недавние решения
    - Метрики здоровья
    - Сроки

    Args:
        project_id: ID проекта
        user_id: ID пользователя
        format: Формат ответа (json/text)
    """
    logger.info("📸 Getting enhanced project snapshot", project_id=project_id, user_id=user_id)

    try:
        from backend.core.sleep.enhanced_snapshot import get_enhanced_snapshot_generator
        from backend.core.store.graph_view import merged_graph_view_for_user

        # Wave 2.4: federated read — snapshot включает promoted org-узлы.
        graph_builder = await merged_graph_view_for_user(user_id, use_networkx=None)

        # Передаём graph_builder — singleton обновит граф
        generator = get_enhanced_snapshot_generator(graph_builder, user_id=user_id)
        generator.user_id = user_id

        # force_regenerate=True чтобы всегда искать в текущем графе
        snapshot = await generator.get_project_snapshot(project_id, force_regenerate=True)

        await graph_builder.close(save=False)

        if not snapshot:
            return {
                "project_id": project_id,
                "error": "Project not found",
                "message": f"Проект '{project_id}' не найден в графе знаний. Запустите синхронизацию.",
            }

        if format == "text":
            return {
                "format": "text",
                "content": snapshot.to_text(),
                "last_updated": snapshot.last_updated,
            }

        # Снепшот = таймлайн: история проекта из TemporalTracker; фолбэк — из
        # данных графа (решения проекта), если TemporalTracker пуст.
        _snap_dict = snapshot.to_dict()
        timeline = await _fetch_entity_timeline(user_id, project_id)
        if not timeline:
            timeline = _synthetic_timeline(_snap_dict)

        return {
            "format": "json",
            "snapshot": _snap_dict,
            "timeline": timeline,
        }

    except Exception as e:
        logger.error(f"Error getting enhanced project snapshot: {e}")
        return {
            "project_id": project_id,
            "error": str(e),
        }


# ═══ Клиенты: карточки из ночной консолидации ═══

@get("/clients")
async def get_client_snapshots(
    user_id: str = Parameter(query="user_id", required=True),
) -> dict[str, Any]:
    """Карточки клиентов (B2B): статус/договорённости/риски/следующий шаг.
    Пишутся ночным шагом _update_client_snapshots; здесь читаем готовые."""
    import json as _json

    from backend.core.store.tenant_paths import snapshots_dir_for_user
    cards: list[dict] = []
    try:
        cdir = snapshots_dir_for_user(user_id) / "clients"
        if cdir.exists():
            for f in cdir.glob("*.json"):
                try:
                    cards.append(_json.loads(f.read_text(encoding="utf-8")))
                except Exception:
                    continue
        cards.sort(key=lambda c: c.get("degree", 0), reverse=True)
    except Exception as e:
        logger.error(f"client snapshots read failed: {e}")
        return {"cards": [], "error": str(e)}
    return {"cards": cards, "total": len(cards)}


# ═══ Timeline: история версий снапшота + сторилайн ═══

_TL_DIRS = {"person": "persons", "project": "projects", "product": "products",
            "department": "departments", "team": "teams", "company": "company"}


def _tl_point(data: dict, entity_type: str) -> dict:
    """Компактная точка таймлайна из полного снапшота (для ленты)."""
    return {
        "version": data.get("version", 1),
        "date": data.get("last_updated", ""),
        "delta_summary": data.get("delta_summary", ""),
        "status": data.get("status", "") or data.get("current_status", ""),
        "health_score": data.get("health_score", None),
        "progress": data.get("progress", None),
        "tasks_completed": data.get("tasks_completed",
                                    data.get("tasks_completed_week", None)),
        "name": data.get("name", ""),
    }


@get("/timeline/{entity_type:str}/{entity_id:str}")
async def get_snapshot_timeline(
    entity_type: str,
    entity_id: str,
    user_id: str = Parameter(query="user_id", required=True),
    storyline: bool = Parameter(query="storyline", default=False),
) -> dict[str, Any]:
    """Таймлайн сущности: точки-версии снапшота (дата + что изменилось +
    ключевые метрики) и, по запросу, LLM-сторилайн «как всё менялось».
    Версии уже пишутся на диск (до 10 на сущность) — здесь их читаем.
    """
    import json as _json

    from backend.core.store.tenant_paths import snapshots_dir_for_user

    if entity_type not in _TL_DIRS:
        return {"error": f"entity_type must be one of {sorted(_TL_DIRS)}", "points": []}
    base = snapshots_dir_for_user(user_id)
    tdir = _TL_DIRS[entity_type]
    if entity_type == "company":
        versions_dir = base / "versions" / "company"
        current_file = base / "company_snapshot.json"
    else:
        versions_dir = base / "versions" / tdir / entity_id
        current_file = base / tdir / f"{entity_id}.json"

    points: list[dict] = []
    try:
        if versions_dir.exists():
            vfiles = sorted(versions_dir.glob("v*.json"),
                            key=lambda p: int(p.stem[1:]) if p.stem[1:].isdigit() else 0)
            for vf in vfiles:
                try:
                    points.append(_tl_point(_json.loads(vf.read_text(encoding="utf-8")), entity_type))
                except Exception:
                    continue
        if current_file.exists():
            cur = _tl_point(_json.loads(current_file.read_text(encoding="utf-8")), entity_type)
            cur["is_current"] = True
            points.append(cur)
    except Exception as e:
        logger.error(f"timeline read failed: {e}")
        return {"points": [], "error": str(e)}

    # Будущее (Scenario/Prediction) + события (встречи/решения сущности) из графа.
    future: list[dict] = []
    events: list[dict] = []
    try:
        ent_name = (points[-1].get("name") if points else "") or ""
        from backend.core.store.graph_view import merged_graph_view_for_user
        gb = await merged_graph_view_for_user(user_id, use_networkx=None)
        try:
            if ent_name:
                for label in ("Scenario", "Prediction"):
                    for n in await gb.find_nodes_by_label(label, limit=200, tenant_id=user_id):
                        blob = f"{n.get('name', '')} {n.get('description', '')}"
                        if ent_name.lower() in blob.lower():
                            future.append({
                                "title": n.get("name", ""),
                                "description": (n.get("description") or "")[:200],
                                "probability": n.get("probability", n.get("success_probability", None)),
                                "horizon": n.get("timeframe", n.get("horizon", "")),
                                "kind": label.lower(),
                            })
            # События: соседние Meeting/Decision узла сущности — точки-факты
            # между версиями («что происходило», а не только «что изменилось»).
            if entity_type in ("person", "project") and entity_id:
                # Анти-протечка (общий Neo4j): соседей читаем только из своих
                # tenant-ов (user + организации + коллеги) и legacy-узлов.
                try:
                    from backend.core.store.tenant_scope import allowed_tenants
                    _own = sorted(allowed_tenants(user_id)) or None
                except Exception:
                    _own = None
                rels = await gb.get_node_relationships(entity_id)
                seen_ev: set = set()
                for e in (rels.get("outgoing") or []) + (rels.get("incoming") or []):
                    other = e.get("target") or e.get("source")
                    if not other or other == entity_id or other in seen_ev:
                        continue
                    seen_ev.add(other)
                    n = await gb.get_node_by_id(other, tenant_id=_own)
                    if not n:
                        continue
                    lbl = n.get("_label", "")
                    if lbl == "Meeting":
                        events.append({
                            "kind": "meeting",
                            "date": n.get("date") or n.get("created_at") or "",
                            "title": (n.get("title") or n.get("name") or "Встреча")[:120],
                        })
                    elif lbl == "Decision":
                        events.append({
                            "kind": "decision",
                            "date": n.get("date") or n.get("created_at") or "",
                            "title": (n.get("text") or n.get("title") or n.get("name") or "Решение")[:120],
                        })
                    if len(events) >= 60:
                        break
                events = sorted([e for e in events if e["date"]],
                                key=lambda x: x["date"])[-30:]
        finally:
            await gb.close(save=False)
    except Exception:
        logger.debug("timeline future/events scan skipped", exc_info=True)

    result: dict[str, Any] = {
        "entity_type": entity_type, "entity_id": entity_id,
        "points": points, "future": future[:6], "events": events,
    }

    # Сторилайн: LLM-нарратив «как менялось» по дельтам версий. Кэш на диске
    # по номеру последней версии — не жжём вызов на каждый открытый таймлайн.
    if storyline and points:
        latest_v = points[-1].get("version", 1)
        cache_file = (versions_dir if versions_dir.exists() else base) / f".storyline_v{latest_v}.json"
        try:
            if cache_file.exists():
                result["storyline"] = _json.loads(cache_file.read_text(encoding="utf-8")).get("text", "")
            else:
                from backend.core.llm.router import get_llm_router
                llm = get_llm_router()
                lines = [
                    f"{p.get('date', '')[:10]} (v{p.get('version')}): "
                    f"{p.get('delta_summary') or 'без изменений'}"
                    + (f"; статус {p['status']}" if p.get('status') else "")
                    for p in points
                ]
                prompt = (
                    f"История изменений «{points[-1].get('name', entity_id)}» по версиям. "
                    "Напиши связный сторилайн (4-7 предложений): как всё развивалось, "
                    "ключевые повороты, где сейчас. По фактам, без воды, русский язык.\n\n"
                    + "\n".join(lines[-10:])
                )
                resp = await llm.generate(prompt=prompt, temperature=0.3, max_tokens=400)
                text = (resp.get("text", "") if isinstance(resp, dict) else str(resp or "")).strip()
                result["storyline"] = text
                try:
                    cache_file.parent.mkdir(parents=True, exist_ok=True)
                    cache_file.write_text(_json.dumps({"text": text}, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    logger.debug("storyline cache write skipped", exc_info=True)
        except Exception as e:
            logger.warning(f"storyline generation failed: {e}")
            result["storyline"] = ""

    return result


# ═══ Step 3: Hierarchy + Delta endpoints ═══

@get("/hierarchy")
async def get_snapshot_hierarchy(
    user_id: str = Parameter(query="user_id", required=True),
) -> dict[str, Any]:
    """Получить иерархию снэпшотов с snippets для навигации."""
    try:
        from backend.core.sleep.enhanced_snapshot import EnhancedSnapshotGenerator
        from backend.core.store.graph_view import merged_graph_view_for_user
        from backend.core.store.tenant_paths import snapshots_dir_for_user

        # Create fresh generator with proper graph and storage.
        # Wave 2.4: federated read view.
        graph_builder = await merged_graph_view_for_user(user_id, use_networkx=None)

        from backend.core.llm.router import get_llm_router
        # ВАЖНО: per-user storage. Раньше генератор создавался с дефолтной
        # ГЛОБАЛЬНОЙ data/snapshots — __init__._load_snapshots() поднимал с
        # диска сотни старых person-снапшотов ВСЕХ тенантов (в навигации
        # «345 чел.», продукты другой компании), игнорируя чистый per-user
        # каталог, который пишет консолидация.
        generator = EnhancedSnapshotGenerator(
            graph_builder=graph_builder,
            llm_router=get_llm_router(),
            storage_path=str(snapshots_dir_for_user(user_id)),
        )
        generator.user_id = user_id

        # force_regenerate=False: берём снапшот, сгенерированный консолидацией.
        # Раньше True — LLM-вызов на КАЖДЫЙ просмотр вкладки (медленно, а при
        # сбое LLM-провайдера ещё и портил name/products fallback-моделью).
        await generator.get_company_snapshot(force_regenerate=False)

        hierarchy = generator.get_snapshot_hierarchy()

        await graph_builder.close(save=False)

        return {"status": "ok", "hierarchy": hierarchy}

    except Exception as e:
        logger.error(f"Error getting snapshot hierarchy: {e}")
        import traceback
        return {"status": "error", "message": str(e), "trace": traceback.format_exc()}


@get("/delta/{snapshot_type:str}")
async def get_snapshot_delta(
    snapshot_type: str,
    user_id: str = Parameter(query="user_id", required=True),
    entity_id: str = Parameter(query="entity_id", default="company"),
) -> dict[str, Any]:
    """Получить delta (что изменилось) для снэпшота."""
    try:
        from backend.core.sleep.enhanced_snapshot import get_enhanced_snapshot_generator
        from backend.core.store.graph_view import merged_graph_view_for_user

        # Wave 2.4: federated read view. Legacy `user_graph_path` атрибут
        # больше не нужен — merged_graph_view_for_user сам resolve'ит путь
        # к personal-графу и догружает org-узлы для членов команды.
        graph_builder = await merged_graph_view_for_user(user_id, use_networkx=None)

        generator = get_enhanced_snapshot_generator(graph_builder, user_id=user_id)
        generator.user_id = user_id

        if snapshot_type == "company":
            snapshot = await generator.get_company_snapshot()
            return {
                "status": "ok",
                "snapshot_type": "company",
                "version": snapshot.version,
                "delta_summary": snapshot.delta_summary,
                "delta_details": snapshot.delta_details,
                "last_updated": snapshot.last_updated,
            }
        elif snapshot_type == "person":
            snapshot = await generator.get_person_snapshot(entity_id)
            if snapshot:
                return {
                    "status": "ok",
                    "snapshot_type": "person",
                    "entity_id": entity_id,
                    "version": getattr(snapshot, 'version', 1),
                    "delta_summary": getattr(snapshot, 'delta_summary', ''),
                    "last_updated": getattr(snapshot, 'last_updated', ''),
                }

        await graph_builder.close(save=False)
        return {"status": "error", "message": f"Snapshot not found: {snapshot_type}/{entity_id}"}

    except Exception as e:
        logger.error(f"Error getting snapshot delta: {e}")
        return {"status": "error", "message": str(e)}


# Router
router = Router(
    path="/snapshots",
    guards=[enforce_user_id_matches_token],
    route_handlers=[
        get_project_snapshot,
        get_person_snapshot,
        get_all_people_snapshots,
        get_team_360,
        get_duplicate_candidates,
        get_team_snapshot,
        get_department_snapshot,
        get_company_snapshot,
        generate_snapshot,
        # Enhanced snapshots
        get_enhanced_company_snapshot,
        get_entity_timeline_route,
        set_company_snapshot_override,
        set_person_snapshot_override,
        set_person_classification_route,
        delete_snapshot_entity_route,
        get_enhanced_person_snapshot,
        get_enhanced_project_snapshot,
        # Step 3: Hierarchy + Delta
        get_snapshot_hierarchy,
        get_snapshot_timeline,
        get_client_snapshots,
        get_snapshot_delta,
    ],
    tags=["Snapshots"],
)



