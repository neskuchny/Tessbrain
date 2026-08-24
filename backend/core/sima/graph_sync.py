"""
SIMA Graph Sync — синхронизация SIMA проектов/блоков/связей в Neo4j Knowledge Graph.
При создании/обновлении/удалении объектов SIMA они отражаются в графе знаний Tessbrain.
"""
import structlog

from backend.db.sima_client import get_sima_db

logger = structlog.get_logger(__name__)


async def _get_neo4j():
    """Получить клиент Neo4j (ленивая инициализация)."""
    try:
        from backend.db.neo4j_client import get_neo4j_client
        return await get_neo4j_client()
    except Exception as e:
        logger.warning("Neo4j not available for SIMA sync", error=str(e))
        return None


async def sync_project_to_graph(project_id: str, user_id: str) -> bool:
    """Синхронизировать SIMA проект и его блоки/связи в Neo4j."""
    neo4j = await _get_neo4j()
    if not neo4j:
        return False

    db = await get_sima_db()
    project = await db.get_project(project_id, user_id)
    if not project:
        return False

    try:
        # Создаём/обновляем узел проекта
        await neo4j.execute_query(
            """
            MERGE (p:SimaProject {id: $id})
            SET p.name = $name,
                p.description = $description,
                p.goal = $goal,
                p.user_id = $user_id,
                p.target_audience = $target_audience,
                p.updated_at = datetime()
            """,
            {
                "id": project_id,
                "name": project.get("name", ""),
                "description": project.get("description", ""),
                "goal": project.get("goal", ""),
                "user_id": user_id,
                "target_audience": project.get("target_audience", ""),
            }
        )

        # Синхронизируем блоки
        blocks = project.get("blocks", [])
        for block in blocks:
            await neo4j.execute_query(
                """
                MERGE (b:SimaBlock {id: $id})
                SET b.name = $name,
                    b.label = $label,
                    b.type = $type,
                    b.layer = $layer,
                    b.description = $description,
                    b.goal = $goal,
                    b.business_value = $business_value,
                    b.user_id = $user_id,
                    b.project_id = $project_id
                WITH b
                MATCH (p:SimaProject {id: $project_id})
                MERGE (p)-[:HAS_BLOCK]->(b)
                """,
                {
                    "id": block.get("id"),
                    "name": block.get("name", ""),
                    "label": block.get("label", ""),
                    "type": block.get("type", "feature"),
                    "layer": block.get("layer", "mvp"),
                    "description": block.get("description", ""),
                    "goal": block.get("goal", ""),
                    "business_value": block.get("business_value", ""),
                    "user_id": user_id,
                    "project_id": project_id,
                }
            )

        # Синхронизируем связи
        connections = project.get("connections", [])
        for conn in connections:
            await neo4j.execute_query(
                """
                MATCH (from:SimaBlock {id: $from_id})
                MATCH (to:SimaBlock {id: $to_id})
                MERGE (from)-[r:SIMA_CONNECTS {id: $id}]->(to)
                SET r.type = $type,
                    r.label = $label,
                    r.description = $description,
                    r.data_description = $data_description,
                    r.business_meaning = $business_meaning
                """,
                {
                    "id": conn.get("id"),
                    "from_id": conn.get("from_block_id"),
                    "to_id": conn.get("to_block_id"),
                    "type": conn.get("type", "data_flow"),
                    "label": conn.get("label", ""),
                    "description": conn.get("description", ""),
                    "data_description": conn.get("data_description", ""),
                    "business_meaning": conn.get("business_meaning", ""),
                }
            )

        logger.info("SIMA project synced to Neo4j",
                     project_id=project_id, blocks=len(blocks), connections=len(connections))
        return True

    except Exception as e:
        logger.error("Failed to sync SIMA project to Neo4j", error=str(e), project_id=project_id)
        return False


async def remove_project_from_graph(project_id: str) -> bool:
    """Удалить SIMA проект и все его данные из Neo4j."""
    neo4j = await _get_neo4j()
    if not neo4j:
        return False

    try:
        # Удаляем все связи и блоки проекта, затем сам проект
        await neo4j.execute_query(
            """
            MATCH (p:SimaProject {id: $id})-[:HAS_BLOCK]->(b:SimaBlock)
            OPTIONAL MATCH (b)-[r:SIMA_CONNECTS]-()
            DELETE r
            """,
            {"id": project_id}
        )
        await neo4j.execute_query(
            """
            MATCH (p:SimaProject {id: $id})-[hb:HAS_BLOCK]->(b:SimaBlock)
            DELETE hb, b
            """,
            {"id": project_id}
        )
        await neo4j.execute_query(
            "MATCH (p:SimaProject {id: $id}) DELETE p",
            {"id": project_id}
        )

        logger.info("SIMA project removed from Neo4j", project_id=project_id)
        return True
    except Exception as e:
        logger.error("Failed to remove SIMA project from Neo4j", error=str(e))
        return False


async def search_sima_in_graph(user_id: str, query: str, limit: int = 20) -> list[dict]:
    """Поиск SIMA сущностей в графе знаний (для использования в Brain)."""
    neo4j = await _get_neo4j()
    if not neo4j:
        return []

    try:
        result = await neo4j.execute_query(
            """
            MATCH (b:SimaBlock)
            WHERE b.user_id = $user_id
            AND (b.name =~ $pattern OR b.description =~ $pattern OR b.label =~ $pattern)
            OPTIONAL MATCH (p:SimaProject)-[:HAS_BLOCK]->(b)
            RETURN b.id as block_id, b.name as block_name, b.label as block_label,
                   b.type as block_type, b.description as block_description,
                   p.name as project_name, p.id as project_id
            LIMIT $limit
            """,
            {
                "user_id": user_id,
                "pattern": f"(?i).*{query}.*",
                "limit": limit,
            }
        )
        return [dict(r) for r in result] if result else []
    except Exception as e:
        logger.error("SIMA graph search failed", error=str(e))
        return []
