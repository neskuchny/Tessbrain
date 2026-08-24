"""
SIMA Vector Sync — индексация SIMA контента в Qdrant для семантического поиска.
Блоки, нарративы и описания проектов индексируются для использования в Brain поиске.
"""
import structlog

from backend.config import settings
from backend.db.sima_client import get_sima_db

logger = structlog.get_logger(__name__)


async def _get_qdrant():
    """Получить клиент Qdrant."""
    try:
        from qdrant_client import AsyncQdrantClient
        return AsyncQdrantClient(
            host=settings.qdrant_host,
            port=settings.qdrant_port,
            api_key=settings.qdrant_api_key or None,
            https=settings.qdrant_https,
        )
    except Exception as e:
        logger.warning("Qdrant not available for SIMA sync", error=str(e))
        return None


async def _get_embedder():
    """Получить функцию генерации эмбеддингов."""
    try:
        from backend.core.indexing.vector_indexer import get_vector_indexer
        indexer = await get_vector_indexer()
        return indexer
    except Exception as e:
        logger.warning("Vector indexer not available", error=str(e))
        return None


SIMA_COLLECTION = "tessent_sima"


async def _ensure_collection(qdrant) -> bool:
    """Создаёт коллекцию SIMA в Qdrant если не существует."""
    try:
        collections = await qdrant.get_collections()
        names = [c.name for c in collections.collections]
        if SIMA_COLLECTION not in names:
            from qdrant_client.models import Distance, VectorParams
            await qdrant.create_collection(
                collection_name=SIMA_COLLECTION,
                vectors_config=VectorParams(
                    size=settings.local_embedding_dimension,
                    distance=Distance.COSINE,
                ),
            )
            logger.info(f"Created Qdrant collection: {SIMA_COLLECTION}")
        return True
    except Exception as e:
        logger.error("Failed to ensure SIMA collection", error=str(e))
        return False


async def index_project(project_id: str, user_id: str) -> int:
    """Индексировать все блоки и нарративы SIMA проекта в Qdrant."""
    qdrant = await _get_qdrant()
    if not qdrant:
        return 0

    indexer = await _get_embedder()
    if not indexer:
        return 0

    if not await _ensure_collection(qdrant):
        return 0

    db = await get_sima_db()
    project = await db.get_project(project_id, user_id)
    if not project:
        return 0

    points = []
    blocks = project.get("blocks", [])

    for block in blocks:
        # Собираем текст для индексации
        text_parts = [
            f"Блок: {block.get('name', '')} ({block.get('label', '')})",
            f"Тип: {block.get('type', '')}, Слой: {block.get('layer', '')}",
        ]
        if block.get("description"):
            text_parts.append(f"Описание: {block['description']}")
        if block.get("goal"):
            text_parts.append(f"Цель: {block['goal']}")
        if block.get("business_value"):
            text_parts.append(f"Бизнес-ценность: {block['business_value']}")
        if block.get("user_benefit"):
            text_parts.append(f"Польза: {block['user_benefit']}")
        if block.get("implementation"):
            text_parts.append(f"Реализация: {block['implementation']}")
        if block.get("block_narrative"):
            text_parts.append(f"Нарратив: {block['block_narrative']}")

        text = "\n".join(text_parts)
        if len(text) < 20:
            continue

        try:
            embedding = await indexer.embed_text(text)
            if embedding:
                from qdrant_client.models import PointStruct
                points.append(PointStruct(
                    id=block["id"].replace("-", "")[:32],  # Qdrant ID
                    vector=embedding,
                    payload={
                        "type": "sima_block",
                        "block_id": block["id"],
                        "block_name": block.get("name", ""),
                        "block_label": block.get("label", ""),
                        "block_type": block.get("type", ""),
                        "project_id": project_id,
                        "project_name": project.get("name", ""),
                        "user_id": user_id,
                        "text": text[:2000],
                    },
                ))
        except Exception as e:
            logger.warning("Failed to embed SIMA block", block_id=block.get("id"), error=str(e))

    # Индексируем нарратив проекта
    narrative_parts = []
    for field in ["narrative_problem", "narrative_solution", "narrative_how_it_works", "narrative_product_map", "narrative_decisions"]:
        if project.get(field):
            narrative_parts.append(project[field])

    if narrative_parts:
        narrative_text = f"Проект: {project.get('name', '')}\n" + "\n\n".join(narrative_parts)
        try:
            embedding = await indexer.embed_text(narrative_text[:3000])
            if embedding:
                from qdrant_client.models import PointStruct
                points.append(PointStruct(
                    id=project_id.replace("-", "")[:32],
                    vector=embedding,
                    payload={
                        "type": "sima_narrative",
                        "project_id": project_id,
                        "project_name": project.get("name", ""),
                        "user_id": user_id,
                        "text": narrative_text[:2000],
                    },
                ))
        except Exception as e:
            logger.warning("Failed to embed SIMA narrative", error=str(e))

    # Загружаем в Qdrant
    if points:
        try:
            await qdrant.upsert(collection_name=SIMA_COLLECTION, points=points)
            logger.info("SIMA vectors indexed", project_id=project_id, points=len(points))
        except Exception as e:
            logger.error("Failed to upsert SIMA vectors", error=str(e))
            return 0

    return len(points)


async def remove_project_vectors(project_id: str) -> bool:
    """Удалить векторы SIMA проекта из Qdrant."""
    qdrant = await _get_qdrant()
    if not qdrant:
        return False

    try:
        from qdrant_client.models import FieldCondition, Filter, MatchValue
        await qdrant.delete(
            collection_name=SIMA_COLLECTION,
            points_selector=Filter(
                must=[FieldCondition(key="project_id", match=MatchValue(value=project_id))]
            ),
        )
        logger.info("SIMA vectors removed", project_id=project_id)
        return True
    except Exception as e:
        logger.error("Failed to remove SIMA vectors", error=str(e))
        return False


async def search_sima_vectors(user_id: str, query: str, limit: int = 10) -> list[dict]:
    """Семантический поиск по SIMA данным в Qdrant (для Brain)."""
    qdrant = await _get_qdrant()
    if not qdrant:
        return []

    indexer = await _get_embedder()
    if not indexer:
        return []

    try:
        query_embedding = await indexer.embed_text(query)
        if not query_embedding:
            return []

        from qdrant_client.models import FieldCondition, Filter, MatchValue
        results = await qdrant.search(
            collection_name=SIMA_COLLECTION,
            query_vector=query_embedding,
            query_filter=Filter(
                must=[FieldCondition(key="user_id", match=MatchValue(value=user_id))]
            ),
            limit=limit,
        )

        return [
            {
                "score": hit.score,
                "type": hit.payload.get("type"),
                "block_name": hit.payload.get("block_name"),
                "project_name": hit.payload.get("project_name"),
                "text": hit.payload.get("text", "")[:500],
                **hit.payload,
            }
            for hit in results
        ]
    except Exception as e:
        logger.error("SIMA vector search failed", error=str(e))
        return []
