"""
Wisdom Index — Слой 4 BWE

Векторный индекс паттернов на Qdrant + PostgreSQL metadata.

Исправления vs v1:
- Qdrant payload filter по user_id (не Python-level фильтрация)
- Корректное удаление orphan-паттернов при синхронизации
- Убран unused _cosine_similarity
- Безопасная конвертация UUID → int (коллизионно-устойчивая)
- Переиспользует qdrant_client из аргументов (не создаёт свой)
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from qdrant_client import QdrantClient
from qdrant_client.http import models as qdrant_models
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.wisdom.bwe_models import BWEDecisionEvent, BWEWisdomPattern

logger = logging.getLogger(__name__)


class WisdomIndexUnavailable(RuntimeError):
    """Индекс паттернов недоступен (Qdrant не отвечает).

    Отдельный тип, чтобы вызывающий мог отличить «мы не смогли посмотреть»
    от «мы посмотрели, там пусто». Разница принципиальная: во втором случае
    честно сказать «ситуация нового типа», в первом — нельзя.
    """

BWE_QDRANT_COLLECTION = "bwe_wisdom_patterns"


class WisdomIndex:
    """
    Векторный индекс Wisdom Patterns.

    Хранилище:
    - Qdrant: prototype_vector (для быстрого cosine поиска)
    - PostgreSQL: полные данные паттернов (metadata, instances, distribution)

    Qdrant фильтрует по user_id через payload filter — корректная мультиарендность.
    """

    def __init__(self, qdrant_client: QdrantClient, embedding_dim: int = 1536):
        self.qdrant = qdrant_client
        self.embedding_dim = embedding_dim
        self._collection_ready = False

    async def ensure_collection(self) -> bool:
        """Создать Qdrant коллекцию если не существует."""
        if self._collection_ready:
            return True
        try:
            collections = self.qdrant.get_collections().collections
            names = [c.name for c in collections]
            if BWE_QDRANT_COLLECTION not in names:
                self.qdrant.create_collection(
                    collection_name=BWE_QDRANT_COLLECTION,
                    vectors_config=qdrant_models.VectorParams(
                        size=self.embedding_dim,
                        distance=qdrant_models.Distance.COSINE,
                    ),
                )
                # Индекс по user_id для быстрой фильтрации
                self.qdrant.create_payload_index(
                    collection_name=BWE_QDRANT_COLLECTION,
                    field_name="user_id",
                    field_schema=qdrant_models.PayloadSchemaType.KEYWORD,
                )
                logger.info(f"[WisdomIndex] Created Qdrant collection '{BWE_QDRANT_COLLECTION}'")
            self._collection_ready = True
            return True
        except Exception as e:
            logger.error(f"[WisdomIndex] Failed to ensure collection: {e}")
            return False

    async def index_pattern(self, pattern: BWEWisdomPattern) -> bool:
        """
        Добавить/обновить паттерн в Qdrant.
        Вызывается после кластеризации.
        """
        if not await self.ensure_collection():
            return False
        if not pattern.prototype_vector or not pattern.id:
            return False

        try:
            point_id = self._uuid_to_uint64(str(pattern.id))
            self.qdrant.upsert(
                collection_name=BWE_QDRANT_COLLECTION,
                points=[
                    qdrant_models.PointStruct(
                        id=point_id,
                        vector=pattern.prototype_vector,
                        payload={
                            "pattern_uuid": str(pattern.id),
                            "user_id": pattern.user_id,
                            "pattern_name": pattern.pattern_name,
                            "confidence": pattern.confidence,
                            "outcome_distribution": pattern.outcome_distribution or {},
                            "total_cases": len(pattern.instances or []),
                        },
                    )
                ],
            )
            return True
        except Exception as e:
            logger.error(f"[WisdomIndex] Failed to index pattern {pattern.id}: {e}")
            return False

    async def search_similar_patterns(
        self,
        query_embedding: list[float],
        user_id: str,
        limit: int = 5,
        min_score: float = 0.50,
    ) -> list[dict[str, Any]]:
        """
        Найти похожие паттерны через Qdrant с payload filter по user_id.

        Фильтрация на стороне Qdrant (не Python) — правильная мультиарендность.
        """
        if not await self.ensure_collection():
            return []

        try:
            results = self.qdrant.search(
                collection_name=BWE_QDRANT_COLLECTION,
                query_vector=query_embedding,
                limit=limit,
                score_threshold=min_score,
                with_payload=True,
                query_filter=qdrant_models.Filter(
                    must=[
                        qdrant_models.FieldCondition(
                            key="user_id",
                            match=qdrant_models.MatchValue(value=user_id),
                        )
                    ]
                ),
            )
            return [
                {
                    "pattern_uuid": r.payload.get("pattern_uuid"),
                    "pattern_name": r.payload.get("pattern_name", "unknown"),
                    "score": r.score,
                    "confidence": r.payload.get("confidence", "low"),
                    "outcome_distribution": r.payload.get("outcome_distribution", {}),
                    "total_cases": r.payload.get("total_cases", 0),
                }
                for r in results
                if r.payload.get("pattern_uuid")
            ]
        except Exception as e:
            # Сбой поиска — НЕ то же самое, что «похожих паттернов нет».
            # Раньше здесь возвращался пустой список, и вызывающий отвечал
            # «в архиве компании нет похожих паттернов» — утверждение о
            # содержимом архива, к которому не было доступа. Поднимаем
            # исключение: пусть вызывающий скажет «архив недоступен».
            logger.error(f"[WisdomIndex] Qdrant search failed: {e}")
            raise WisdomIndexUnavailable(str(e)) from e

    async def get_pattern_details(
        self,
        session: AsyncSession,
        pattern_uuid: str,
        user_id: str,
        max_recent_decisions: int = 3,
    ) -> Optional[dict[str, Any]]:
        """
        Детальная информация о паттерне из PostgreSQL.
        Включает недавние примеры решений.
        """
        result = await session.execute(
            select(BWEWisdomPattern)
            .where(
                BWEWisdomPattern.id == pattern_uuid,
                BWEWisdomPattern.user_id == user_id,
            )
        )
        pattern = result.scalar_one_or_none()
        if not pattern:
            return None

        # Недавние решения (последние по порядку в instances)
        instances = pattern.instances or []
        recent_ids = instances[-max_recent_decisions:]  # последние добавленные
        recent_decisions = []
        if recent_ids:
            dec_result = await session.execute(
                select(BWEDecisionEvent)
                .where(BWEDecisionEvent.id.in_(recent_ids))
                .order_by(BWEDecisionEvent.created_at.desc())
            )
            for dec in dec_result.scalars().all():
                recent_decisions.append({
                    "situation": dec.situation,
                    "decision": dec.decision,
                    "outcome_linked": dec.outcome_linked,
                    "company_vitek": dec.company_vitek,
                    "created_at": dec.created_at.isoformat() if dec.created_at else None,
                })

        return {
            "id": str(pattern.id),
            "pattern_name": pattern.pattern_name,
            "instances": instances,
            "total_cases": len(instances),
            "outcome_distribution": pattern.outcome_distribution or {},
            "vitek_sensitivity": pattern.vitek_sensitivity,
            "context_differentiators": pattern.context_differentiators or [],
            "confidence": pattern.confidence,
            "recent_decisions": recent_decisions,
            "created_at": pattern.created_at.isoformat() if pattern.created_at else None,
            "updated_at": pattern.updated_at.isoformat() if pattern.updated_at else None,
            # Crystal-поля (Module A). Аддитивно: getattr с дефолтом, чтобы
            # код не падал на паттернах, созданных до миграции 250.
            "primary_tension": getattr(pattern, "primary_tension", None),
            "vitek_phase_min": getattr(pattern, "vitek_phase_min", None),
            "vitek_phase_max": getattr(pattern, "vitek_phase_max", None),
            "knowledge_category": getattr(pattern, "knowledge_category", None),
            "confidence_score": getattr(pattern, "confidence_score", None),
            "not_law": getattr(pattern, "not_law", None) or [],
            "open_holes": getattr(pattern, "open_holes", None) or [],
            # Crystal Phase 1: якорь свежести (для time-decay на чтении) +
            # история эволюции. getattr-с-дефолтом → безопасно до миграции 251.
            "last_calibrated_at": (
                lc.isoformat() if (lc := getattr(pattern, "last_calibrated_at", None)) else None
            ),
            "evolution_log": getattr(pattern, "evolution_log", None) or [],
            # B2: сигнал утилиты для сопротивления decay.
            "access_count": getattr(pattern, "access_count", 0) or 0,
        }

    async def sync_all_patterns(
        self,
        session: AsyncSession,
        user_id: str,
    ) -> int:
        """
        Полная синхронизация PG → Qdrant.

        Удаляет из Qdrant паттерны которых больше нет в PostgreSQL.
        Добавляет/обновляет все существующие.
        """
        if not await self.ensure_collection():
            return 0

        # Текущие паттерны в PostgreSQL
        pg_result = await session.execute(
            select(BWEWisdomPattern)
            .where(BWEWisdomPattern.user_id == user_id)
        )
        pg_patterns = pg_result.scalars().all()
        pg_ids = {str(p.id) for p in pg_patterns}

        # Текущие точки в Qdrant для этого пользователя
        try:
            qdrant_points, _ = self.qdrant.scroll(
                collection_name=BWE_QDRANT_COLLECTION,
                scroll_filter=qdrant_models.Filter(
                    must=[
                        qdrant_models.FieldCondition(
                            key="user_id",
                            match=qdrant_models.MatchValue(value=user_id),
                        )
                    ]
                ),
                with_payload=True,
                limit=10000,
            )
            qdrant_uuids = {p.payload.get("pattern_uuid") for p in qdrant_points if p.payload}
        except Exception as e:
            logger.warning(f"[WisdomIndex] Qdrant scroll failed: {e}")
            qdrant_uuids = set()

        # Удаляем orphan паттерны из Qdrant
        orphans = qdrant_uuids - pg_ids
        if orphans:
            orphan_qdrant_ids = [self._uuid_to_uint64(uid) for uid in orphans if uid]
            try:
                self.qdrant.delete(
                    collection_name=BWE_QDRANT_COLLECTION,
                    points_selector=qdrant_models.PointIdsList(points=orphan_qdrant_ids),
                )
                logger.info(f"[WisdomIndex] Deleted {len(orphans)} orphan patterns from Qdrant")
            except Exception as e:
                logger.warning(f"[WisdomIndex] Failed to delete orphans: {e}")

        # Индексируем все PG паттерны
        synced = 0
        for pattern in pg_patterns:
            if await self.index_pattern(pattern):
                synced += 1

        logger.info(f"[WisdomIndex] Synced {synced}/{len(pg_patterns)} patterns for user {user_id}")
        return synced

    async def remove_pattern(self, pattern_uuid: str) -> bool:
        """Удалить паттерн из Qdrant по UUID."""
        if not await self.ensure_collection():
            return False
        try:
            point_id = self._uuid_to_uint64(pattern_uuid)
            self.qdrant.delete(
                collection_name=BWE_QDRANT_COLLECTION,
                points_selector=qdrant_models.PointIdsList(points=[point_id]),
            )
            return True
        except Exception as e:
            logger.debug(f"[WisdomIndex] Failed to remove pattern {pattern_uuid}: {e}")
            return False

    @staticmethod
    def _uuid_to_uint64(uuid_str: str) -> int:
        """
        Безопасная конвертация UUID в uint64 для Qdrant.

        Берём старшие 64 бита UUID (первые 16 hex символов).
        Вероятность коллизии: ~1/(2^63) при < миллиарда точек.
        """
        try:
            uid = uuid.UUID(uuid_str)
            return uid.int >> 64  # старшие 64 бита
        except Exception:
            return abs(hash(uuid_str)) % (2 ** 63)
