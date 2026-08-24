"""
TESSENT BRAIN - Qdrant Client
Клиент для работы с векторной базой данных Qdrant
"""
from typing import Any

import structlog
from qdrant_client import QdrantClient as QdrantBaseClient
from qdrant_client.http import models

from backend.config import settings

logger = structlog.get_logger(__name__)

# Имя поля в payload, по которому фильтруем tenant. Меняйте только централизованно.
TENANT_PAYLOAD_KEY = "tenant_id"


def _current_tenant_id() -> str | None:
    """Прочитать tenant_id из единого источника контекста.

    Импорт ленивый: storage-слой не должен жёстко зависеть от observability
    при тестах/cli-сценариях. Если модуль недоступен — возвращаем None.
    """
    try:
        from backend.core.observability.tenant_context import get_current_tenant
        return get_current_tenant()
    except Exception:
        return None


def _enforce_tenant_or_none(operation: str) -> str | None:
    """Достать текущий tenant_id; в strict-режиме отсутствие = ошибка операции."""
    tenant = _current_tenant_id()
    if tenant is None and settings.multitenant_storage_strict:
        raise RuntimeError(
            f"Qdrant.{operation}: tenant_id required in multitenant_storage_strict mode "
            "but no tenant is set in current context",
        )
    return tenant


class QdrantVectorClient:
    """Клиент для работы с Qdrant.

    Multi-tenant контракт:
    - На каждом upsert payload автоматически дополняется `tenant_id` из
      текущего контекста, если он есть. Уже-существующий tenant_id в payload
      не перезаписывается (оставляем admin-операциям свободу).
    - На каждом search автоматически добавляется must-фильтр по tenant_id.
      Запросы без tenant'а в context работают по-старому (single-tenant) или
      падают, если включён `multitenant_storage_strict`.
    - Delete проводится только в пределах tenant'а: даже если клиент знает ID
      чужой точки, она не будет удалена.
    """

    def __init__(self):
        self.client: QdrantBaseClient | None = None
        self.collection_name = settings.qdrant_collection
        self._initialized = False
        self.vector_dim = settings.vector_embedding_dimension

    async def initialize(self) -> bool:
        """Инициализация подключения и создание коллекции"""
        try:
            self.client = QdrantBaseClient(
                host=settings.qdrant_host,
                port=settings.qdrant_port,
                api_key=settings.qdrant_api_key or None,
                https=settings.qdrant_https,
                timeout=30,
            )

            # Создаем коллекцию если не существует
            collections = self.client.get_collections().collections
            collection_names = [c.name for c in collections]

            if self.collection_name not in collection_names:
                self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=models.VectorParams(
                        size=self.vector_dim,
                        distance=models.Distance.COSINE,
                    ),
                )
                logger.info(
                    "✅ Qdrant collection created",
                    collection=self.collection_name,
                    vector_dim=self.vector_dim,
                )
            else:
                try:
                    collection_info = self.client.get_collection(self.collection_name)
                    vectors_cfg = collection_info.config.params.vectors
                    existing_dim = getattr(vectors_cfg, "size", None)
                    if existing_dim and existing_dim != self.vector_dim:
                        logger.warning(
                            "⚠️ Qdrant collection dimension mismatch",
                            collection=self.collection_name,
                            expected_dim=self.vector_dim,
                            actual_dim=existing_dim,
                        )
                except Exception as mismatch_error:
                    logger.debug(
                        "Could not inspect Qdrant collection config",
                        collection=self.collection_name,
                        error=str(mismatch_error),
                    )

            # Индекс по tenant_id ускоряет каждый search, фильтрующий по нему.
            try:
                self.client.create_payload_index(
                    collection_name=self.collection_name,
                    field_name=TENANT_PAYLOAD_KEY,
                    field_schema=models.PayloadSchemaType.KEYWORD,
                )
            except Exception:
                pass

            self._initialized = True
            logger.info(
                "✅ Qdrant initialized",
                host=settings.qdrant_host,
                port=settings.qdrant_port,
                vector_dim=self.vector_dim,
            )
            return True

        except Exception as e:
            logger.error("❌ Qdrant initialization failed", error=str(e))
            return False

    async def upsert(
        self,
        points: list[dict[str, Any]],
        collection: str | None = None,
    ) -> bool:
        """Добавить/обновить точки. tenant_id из контекста автоматически
        дописывается в payload, если в payload его ещё нет.
        """
        if not self.client:
            return False

        tenant = _enforce_tenant_or_none("upsert")

        try:
            qdrant_points: list[models.PointStruct] = []
            for p in points:
                payload = dict(p.get("payload") or {})
                if tenant is not None and TENANT_PAYLOAD_KEY not in payload:
                    payload[TENANT_PAYLOAD_KEY] = tenant
                qdrant_points.append(
                    models.PointStruct(
                        id=p["id"],
                        vector=p["vector"],
                        payload=payload,
                    ),
                )

            self.client.upsert(
                collection_name=collection or self.collection_name,
                points=qdrant_points,
            )
            return True
        except Exception as e:
            logger.error("Qdrant upsert error", error=str(e))
            return False

    async def search(
        self,
        query_vector: list[float],
        limit: int = 10,
        score_threshold: float = 0.7,
        filter_conditions: dict | None = None,
        collection: str | None = None,
    ) -> list[dict[str, Any]]:
        """Поиск с автоматическим must-фильтром по tenant_id из контекста.

        Если caller передал свой `tenant_id` в filter_conditions — он имеет
        приоритет (для админских/cross-tenant операций).
        """
        if not self.client:
            return []

        tenant = _enforce_tenant_or_none("search")

        try:
            # Объединяем явные фильтры пользователя и tenant-фильтр.
            merged_conditions: dict[str, Any] = {}
            if filter_conditions:
                merged_conditions.update(filter_conditions)
            if tenant is not None and TENANT_PAYLOAD_KEY not in merged_conditions:
                merged_conditions[TENANT_PAYLOAD_KEY] = tenant

            qdrant_filter = None
            if merged_conditions:
                qdrant_filter = models.Filter(
                    must=[
                        models.FieldCondition(
                            key=key,
                            match=models.MatchValue(value=value),
                        )
                        for key, value in merged_conditions.items()
                    ],
                )

            results = self.client.search(
                collection_name=collection or self.collection_name,
                query_vector=query_vector,
                limit=limit,
                score_threshold=score_threshold,
                query_filter=qdrant_filter,
            )

            return [
                {
                    "id": str(r.id),
                    "score": r.score,
                    "payload": r.payload,
                }
                for r in results
            ]
        except Exception as e:
            logger.error("Qdrant search error", error=str(e))
            return []

    async def delete(
        self,
        ids: list[str],
        collection: str | None = None,
    ) -> bool:
        """Удалить точки. В multi-tenant режиме удаление ограничено tenant'ом
        текущего контекста: даже если клиент знает чужой ID, он не сработает.
        """
        if not self.client:
            return False

        tenant = _enforce_tenant_or_none("delete")

        try:
            if tenant is not None:
                # Filter-based delete: tenant_id == current AND id in ids.
                self.client.delete(
                    collection_name=collection or self.collection_name,
                    points_selector=models.FilterSelector(
                        filter=models.Filter(
                            must=[
                                models.FieldCondition(
                                    key=TENANT_PAYLOAD_KEY,
                                    match=models.MatchValue(value=tenant),
                                ),
                                models.HasIdCondition(has_id=ids),
                            ],
                        ),
                    ),
                )
            else:
                self.client.delete(
                    collection_name=collection or self.collection_name,
                    points_selector=models.PointIdsList(points=ids),
                )
            return True
        except Exception as e:
            logger.error("Qdrant delete error", error=str(e))
            return False

    async def health_check(self) -> bool:
        """Проверка здоровья подключения"""
        try:
            if self.client:
                self.client.get_collections()
                return True
            return False
        except Exception:
            return False

    async def close(self):
        """Закрытие подключения"""
        if self.client:
            self.client.close()
            logger.info("Qdrant connection closed")


# Singleton instance
_qdrant_client: QdrantVectorClient | None = None


async def get_qdrant() -> QdrantVectorClient:
    """Получить клиент Qdrant"""
    global _qdrant_client
    if _qdrant_client is None:
        _qdrant_client = QdrantVectorClient()
        await _qdrant_client.initialize()
    return _qdrant_client
