# -*- coding: utf-8 -*-
"""
Temporal API Routes - эндпоинты для работы с temporal knowledge graph.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from litestar import Controller, delete, get, post
from backend.core.auth.user_guard import enforce_user_id_matches_token
from litestar.exceptions import HTTPException
from litestar.params import Parameter
from pydantic import BaseModel

from ...core.temporal import TemporalManager, get_temporal_manager

logger = logging.getLogger(__name__)


def _own_tenants(user_id: str) -> set:
    """Свои tenant-ы (user + организации + коллеги) для read-side фильтра."""
    try:
        from backend.core.store.tenant_scope import allowed_tenants
        allowed = allowed_tenants(user_id)
        return allowed or {user_id}
    except Exception:
        return {user_id}


def _is_foreign_record(data: Any, allowed: set) -> bool:
    """True, если запись несёт ЧУЖОЙ штамп tenant_id.

    Анти-протечка для панели «Эволюция объекта»: до фикса кросс-tenant
    резолва в per-tenant temporal-стор попадали свойства чужих узлов (общий
    Neo4j). Записи без штампа (None/'') — свои: CREATE-ветка knowledge_sync
    tenant_id в data не пишет."""
    if not isinstance(data, dict):
        return False
    t = data.get("tenant_id")
    return t not in (None, "") and str(t) not in allowed


class AddEpisodeRequest(BaseModel):
    """Запрос на добавление эпизода"""
    name: str
    content: str
    episode_type: str = "text"  # text, message, json
    source_description: str = ""
    reference_time: Optional[str] = None  # ISO format
    group_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class AddMeetingEpisodeRequest(BaseModel):
    """Запрос на добавление встречи"""
    meeting_id: str
    title: str
    transcript: str
    meeting_time: str  # ISO format
    project_id: Optional[str] = None
    participants: Optional[List[str]] = None


class AddChatEpisodeRequest(BaseModel):
    """Запрос на добавление чата"""
    session_id: str
    user_id: str
    messages: List[Dict[str, str]]
    chat_time: Optional[str] = None


class TemporalSearchRequest(BaseModel):
    """Запрос на temporal поиск"""
    query: str
    group_ids: Optional[List[str]] = None
    time_range_days: int = 30
    limit: int = 10


class TemporalController(Controller):
    """Контроллер для работы с temporal данными"""

    guards = [enforce_user_id_matches_token]
    path = "/temporal"
    tags = ["Temporal"]

    async def _get_manager(self) -> TemporalManager:
        """Получить менеджер temporal данных"""
        try:
            manager = get_temporal_manager()
            await manager.initialize()
            return manager
        except Exception as e:
            logger.error(f"Failed to get temporal manager: {e}")
            raise HTTPException(
                status_code=503,
                detail="Temporal service unavailable"
            )

    # === SQLite-backed эволюция (без Graphiti/Neo4j) ===
    # /entities и /entity/{id}/versions читают TemporalTracker (per-tenant SQLite),
    # который заполняется при индексации встреч. В отличие от /episodes,/timeline,
    # /entity/{name}/history (Graphiti) — работают БЕЗ Neo4j.

    @get("/entities")
    async def list_versioned_entities(
        self,
        user_id: str = Parameter(query="user_id", required=True),
        entity_type: Optional[str] = Parameter(query="entity_type", default=None),
        limit: int = Parameter(query="limit", default=200, ge=1, le=1000),
    ) -> Dict[str, Any]:
        """Список версионируемых сущностей (для панели «Эволюция объекта»).

        versions>1 = объект реально трансформировался во времени."""
        try:
            from ...core.temporal.temporal_tracker import get_temporal_tracker
            tracker = get_temporal_tracker(user_id)
            entities = await tracker.list_entities(entity_type=entity_type, limit=limit)
            # Read-side анти-протечка: скрываем сущности с чужим штампом
            # tenant_id (записаны в стор до фикса кросс-tenant резолва).
            allowed = _own_tenants(user_id)
            before = len(entities)
            entities = [e for e in entities if not _is_foreign_record(e, allowed)]
            if len(entities) != before:
                logger.warning(
                    "temporal/entities: скрыто %d чужих записей (user %s)",
                    before - len(entities), user_id)
            return {"status": "success", "count": len(entities), "entities": entities}
        except Exception as e:
            logger.error(f"temporal/entities failed: {e}")
            return {"status": "error", "message": str(e), "entities": []}

    @get("/entity/{entity_id:str}/versions")
    async def get_entity_versions(
        self,
        entity_id: str,
        user_id: str = Parameter(query="user_id", required=True),
        limit: int = Parameter(query="limit", default=50, ge=1, le=200),
    ) -> Dict[str, Any]:
        """Полная история версий + timeline-события сущности (SQLite-трекер)."""
        try:
            from ...core.temporal.temporal_tracker import get_temporal_tracker
            tracker = get_temporal_tracker(user_id)
            history = await tracker.get_version_history(entity_id, limit=limit)
            # Read-side анти-протечка: версии со свойствами чужого узла
            # (data.tenant_id ≠ свой) не отдаём. Если ВСЯ история чужая —
            # объект не наш, прячем и timeline.
            allowed = _own_tenants(user_id)
            before = len(history)
            history = [v for v in history
                       if not _is_foreign_record(v.get("data"), allowed)]
            if before and not history:
                logger.warning(
                    "temporal/versions: сущность %s целиком чужая, скрыта "
                    "(user %s)", entity_id, user_id)
                return {"status": "success", "entity_id": entity_id,
                        "versions_count": 0, "versions": [], "timeline": []}
            if len(history) != before:
                logger.warning(
                    "temporal/versions: скрыто %d чужих версий %s (user %s)",
                    before - len(history), entity_id, user_id)
            timeline = []
            try:
                timeline = await tracker.get_entity_timeline(entity_id, limit=limit)
            except Exception:
                pass
            return {"status": "success", "entity_id": entity_id,
                    "versions_count": len(history),
                    "versions": history, "timeline": timeline}
        except Exception as e:
            logger.error(f"temporal/entity versions failed: {e}")
            return {"status": "error", "message": str(e),
                    "versions": [], "timeline": []}

    @post("/episodes")
    async def add_episode(self, data: AddEpisodeRequest) -> Dict[str, Any]:
        """
        Добавить эпизод в temporal graph.

        Graphiti автоматически извлечёт сущности и связи.
        """
        manager = await self._get_manager()

        ref_time = None
        if data.reference_time:
            ref_time = datetime.fromisoformat(data.reference_time.replace('Z', '+00:00'))

        result = await manager.graphiti.add_episode(
            name=data.name,
            content=data.content,
            episode_type=data.episode_type,
            source_description=data.source_description,
            reference_time=ref_time,
            group_id=data.group_id,
            metadata=data.metadata
        )

        return result

    @post("/episodes/meeting")
    async def add_meeting_episode(self, data: AddMeetingEpisodeRequest) -> Dict[str, Any]:
        """
        Добавить встречу как temporal episode.
        """
        manager = await self._get_manager()

        meeting_time = datetime.fromisoformat(data.meeting_time.replace('Z', '+00:00'))

        result = await manager.add_meeting_episode(
            meeting_id=data.meeting_id,
            title=data.title,
            transcript=data.transcript,
            meeting_time=meeting_time,
            project_id=data.project_id,
            participants=data.participants
        )

        return result

    @post("/episodes/chat")
    async def add_chat_episode(self, data: AddChatEpisodeRequest) -> Dict[str, Any]:
        """
        Добавить чат как temporal episode.
        """
        manager = await self._get_manager()

        chat_time = None
        if data.chat_time:
            chat_time = datetime.fromisoformat(data.chat_time.replace('Z', '+00:00'))

        result = await manager.add_chat_episode(
            session_id=data.session_id,
            user_id=data.user_id,
            messages=data.messages,
            chat_time=chat_time
        )

        return result

    @get("/episodes")
    async def get_episodes(
        self,
        group_id: Optional[str] = None,
        episode_type: Optional[str] = None,
        limit: int = Parameter(default=20, ge=1, le=100)
    ) -> Dict[str, Any]:
        """
        Получить последние эпизоды.
        """
        manager = await self._get_manager()

        group_ids = [group_id] if group_id else None

        episodes = await manager.graphiti.retrieve_episodes(
            last_n=limit,
            group_ids=group_ids,
            episode_type=episode_type
        )

        return {
            "status": "success",
            "count": len(episodes),
            "episodes": episodes
        }

    @get("/timeline/{group_id:str}")
    async def get_timeline(
        self,
        group_id: str,
        days_back: int = Parameter(default=30, ge=1, le=365),
        limit: int = Parameter(default=50, ge=1, le=200)
    ) -> Dict[str, Any]:
        """
        Получить timeline событий для группы (проекта/пользователя).
        """
        manager = await self._get_manager()

        timeline = await manager.get_timeline(
            group_id=group_id,
            days_back=days_back,
            limit=limit
        )

        return {
            "status": "success",
            "group_id": group_id,
            "days_back": days_back,
            "events_count": len(timeline),
            "timeline": timeline
        }

    @post("/search")
    async def temporal_search(self, data: TemporalSearchRequest) -> Dict[str, Any]:
        """
        Temporal поиск с учётом времени.
        """
        manager = await self._get_manager()

        result = await manager.search_temporal(
            query=data.query,
            group_ids=data.group_ids,
            time_range_days=data.time_range_days,
            limit=data.limit
        )

        return {
            "status": "success",
            **result
        }

    @get("/entity/{entity_name:str}/history")
    async def get_entity_history(
        self,
        entity_name: str,
        group_id: Optional[str] = None,
        limit: int = Parameter(default=20, ge=1, le=100)
    ) -> Dict[str, Any]:
        """
        Получить историю упоминаний сущности.
        """
        manager = await self._get_manager()

        group_ids = [group_id] if group_id else None

        history = await manager.get_entity_history(
            entity_name=entity_name,
            group_ids=group_ids,
            limit=limit
        )

        return {
            "status": "success",
            "entity": entity_name,
            "mentions_count": len(history),
            "history": history
        }

    @delete("/episodes/{episode_uuid:str}", status_code=200)
    async def delete_episode(self, episode_uuid: str) -> Dict[str, Any]:
        """
        Удалить эпизод.
        """
        manager = await self._get_manager()

        success = await manager.graphiti.delete_episode(episode_uuid)

        if not success:
            raise HTTPException(status_code=500, detail="Failed to delete episode")

        return {
            "status": "success",
            "deleted": episode_uuid
        }

    @get("/stats")
    async def get_stats(self) -> Dict[str, Any]:
        """
        Получить статистику temporal системы.
        """
        manager = await self._get_manager()

        stats = manager.graphiti.get_stats()

        return {
            "status": "success",
            "stats": stats
        }

    @get("/compare")
    async def temporal_compare(
        self,
        user_id: str = Parameter(query="user_id", required=True),
        entity_type: str = Parameter(query="entity_type", required=True),
        entity_id: str = Parameter(query="entity_id", required=True),
        dates: str = Parameter(
            query="dates", required=True,
            description="Comma-separated ISO dates, например "
                        "2026-05-15,2026-06-01,2026-06-20"),
        include_drift: bool = Parameter(query="include_drift", default=False),
    ) -> Dict[str, Any]:
        """Три (или больше) временных среза одного объекта рядом — для
        ответа на вопрос «как было вчера, как сейчас, как было месяц
        назад, и где управленческая ошибка».

        issue #112 follow-up по запросу Сергея. Композирует:
          - TemporalTracker.get_version_at_date() — snapshot на дату
          - diff_snapshots() — поля added/removed/changed между соседними
          - compute_observer_drift() — если include_drift=true, где
            система переоценивала исходы решений
        """
        try:
            from ...core.temporal.compare import compare_temporal_slices
            dates_list = [d.strip() for d in dates.split(",") if d.strip()]
            if not dates_list:
                return {"error": "dates parameter is empty or invalid"}
            return await compare_temporal_slices(
                user_id=user_id,
                entity_type=entity_type,
                entity_id=entity_id,
                dates=dates_list,
                include_drift=include_drift,
            )
        except Exception as e:
            logger.error(f"temporal/compare failed: {e}")
            return {"error": str(e)}

    @get("/company-state")
    async def company_state(
        self,
        user_id: str = Parameter(query="user_id", required=True),
        date: str = Parameter(
            query="date", required=True,
            description="ISO-дата среза, например 2026-03-15. Без времени "
                        "означает конец дня."),
        entity_types: Optional[str] = Parameter(
            query="entity_types", default=None,
            description="Фильтр через запятую: person,project,decision,task"),
        limit: int = Parameter(query="limit", default=500, ge=1, le=2000),
    ) -> Dict[str, Any]:
        """Состояние КОМПАНИИ на дату — последняя версия каждой сущности на
        этот момент, сгруппированная по типу.

        В отличие от /compare (один объект по entity_id) это срез целиком:
        кто работал, какие проекты шли, что было решено к этой дате.
        Собирается из уже накопленных версий, без новых таблиц и без LLM.
        """
        try:
            from ...core.temporal.compare import get_company_state_at_date
            types = ([t.strip() for t in entity_types.split(",") if t.strip()]
                     if entity_types else None)
            return await get_company_state_at_date(
                user_id=user_id, date=date, entity_types=types,
                limit=limit, allowed_tenants=_own_tenants(user_id),
            )
        except Exception as e:
            logger.error(f"temporal/company-state failed: {e}")
            return {"error": str(e)}

    @get("/company-compare")
    async def company_compare(
        self,
        user_id: str = Parameter(query="user_id", required=True),
        dates: str = Parameter(
            query="dates", required=True,
            description="Даты через запятую: 2026-03-15,2026-05-01,2026-08-13"),
        entity_types: Optional[str] = Parameter(
            query="entity_types", default=None),
        limit: int = Parameter(query="limit", default=500, ge=1, le=2000),
    ) -> Dict[str, Any]:
        """Несколько срезов компании рядом с посчитанной разницей.

        Отвечает на «как было на 15 марта, на 1 мая и сегодня»: что
        появилось, что изменилось, что исчезло между датами.
        """
        try:
            from ...core.temporal.compare import compare_company_states
            dates_list = [d.strip() for d in dates.split(",") if d.strip()]
            if not dates_list:
                return {"error": "dates parameter is empty or invalid"}
            types = ([t.strip() for t in entity_types.split(",") if t.strip()]
                     if entity_types else None)
            return await compare_company_states(
                user_id=user_id, dates=dates_list, entity_types=types,
                limit=limit, allowed_tenants=_own_tenants(user_id),
            )
        except Exception as e:
            logger.error(f"temporal/company-compare failed: {e}")
            return {"error": str(e)}


# Router для подключения к app
router = TemporalController



