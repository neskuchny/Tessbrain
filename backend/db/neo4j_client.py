"""
TESSENT BRAIN - Neo4j Client
Асинхронный клиент для работы с графовой базой данных Neo4j

Multi-tenant контракт (Phase 7c):
- На `create_node` и `create_relationship` автоматически добавляется
  property `tenant_id` из контекста, если в properties её ещё нет.
- На `find_node` и `get_subgraph` Cypher автоматически дополняется
  фильтром `n.tenant_id = $_tenant`. Кэллер может явно передать
  `tenant_id` в properties — тогда явный приоритет.
- Для произвольных Cypher (`execute_query`) ответственность за фильтр
  на стороне кэллера — у нас слишком много свободных запросов в коде,
  чтобы их безопасно автоматизировать без AST-парсера. Хелпер
  `tenant_filter_clause()` помогает добавить фильтр единообразно.
"""
from typing import Any

import structlog
from neo4j import AsyncDriver, AsyncGraphDatabase

from backend.config import settings

logger = structlog.get_logger(__name__)

# Имя property, по которому фильтруется тенант. Меняется только централизованно.
TENANT_PROPERTY = "tenant_id"


def _current_tenant_id() -> str | None:
    """Прочитать tenant_id из единого источника контекста (lazy import)."""
    try:
        from backend.core.observability.tenant_context import get_current_tenant
        return get_current_tenant()
    except Exception:
        return None


def _enforce_tenant_or_none(operation: str) -> str | None:
    tenant = _current_tenant_id()
    if tenant is None and settings.multitenant_storage_strict:
        raise RuntimeError(
            f"Neo4j.{operation}: tenant_id required in multitenant_storage_strict mode "
            "but no tenant is set in current context",
        )
    return tenant


def tenant_filter_clause(node_alias: str = "n", *, param_name: str = "_tenant") -> str:
    """Вернуть AND-условие для произвольного Cypher запроса.

    Использование:
        query = f"MATCH (n:Person) WHERE {tenant_filter_clause('n')} RETURN n"
        params = {**user_params, **tenant_filter_params()}
    Если tenant_id в контексте отсутствует — возвращается `1=1` (no-op).
    """
    if _current_tenant_id() is None:
        return "1=1"
    return f"({node_alias}.{TENANT_PROPERTY} IS NULL OR {node_alias}.{TENANT_PROPERTY} = ${param_name})"


def tenant_filter_params(*, param_name: str = "_tenant") -> dict[str, Any]:
    """Параметры для tenant-фильтра. Возвращает пустой dict, если tenant отсутствует."""
    tenant = _current_tenant_id()
    if tenant is None:
        return {}
    return {param_name: tenant}


class Neo4jClient:
    """Асинхронный клиент Neo4j"""

    def __init__(self):
        self.driver: AsyncDriver | None = None
        self._initialized = False

    async def initialize(self) -> bool:
        """Инициализация подключения"""
        try:
            self.driver = AsyncGraphDatabase.driver(
                settings.neo4j_uri,
                auth=(settings.neo4j_user, settings.neo4j_password),
            )
            # Проверка подключения
            async with self.driver.session() as session:
                await session.run("RETURN 1")
                # Индекс по tenant_id ускоряет каждый фильтр.
                # Не падаем, если индекс уже есть.
                try:
                    await session.run(
                        f"CREATE INDEX tenant_id_idx IF NOT EXISTS "
                        f"FOR (n) ON (n.{TENANT_PROPERTY})",
                    )
                except Exception:
                    pass

            self._initialized = True
            logger.info("✅ Neo4j initialized", uri=settings.neo4j_uri)
            return True
        except Exception as e:
            logger.error("❌ Neo4j initialization failed", error=str(e))
            return False

    async def execute_query(
        self,
        query: str,
        parameters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Выполнить произвольный Cypher запрос.

        ВАЖНО: Cypher не модифицируется автоматически — каллеру нужно
        самому добавить tenant-фильтр через `tenant_filter_clause()`
        для multi-tenant сценариев. Это сделано осознанно: в коде много
        широких MATCH-обходов, где автоматическая инжекция WHERE рискует
        сломать pattern-matching.
        """
        if not self.driver:
            return []

        try:
            async with self.driver.session() as session:
                result = await session.run(query, parameters or {})
                records = await result.data()
                return records
        except Exception as e:
            logger.error("Neo4j query error", query=query[:100], error=str(e))
            return []

    async def create_node(
        self,
        label: str,
        properties: dict[str, Any],
    ) -> str | None:
        """Создать узел. tenant_id из контекста автодобавляется в properties,
        если его там ещё нет."""
        tenant = _enforce_tenant_or_none("create_node")
        props = dict(properties)
        if tenant is not None and TENANT_PROPERTY not in props:
            props[TENANT_PROPERTY] = tenant
        query = f"""
        CREATE (n:{label} $props)
        RETURN elementId(n) as id
        """
        results = await self.execute_query(query, {"props": props})
        if results:
            return results[0].get("id")
        return None

    async def create_relationship(
        self,
        from_id: str,
        to_id: str,
        rel_type: str,
        properties: dict[str, Any] | None = None,
    ) -> bool:
        """Создать связь между узлами; обе стороны должны принадлежать
        текущему tenant'у (если он задан) — защита от cross-tenant связей."""
        tenant = _enforce_tenant_or_none("create_relationship")
        props = dict(properties or {})
        if tenant is not None and TENANT_PROPERTY not in props:
            props[TENANT_PROPERTY] = tenant

        if tenant is not None:
            # WHERE гарантирует, что узлы принадлежат текущему tenant'у.
            query = f"""
            MATCH (a), (b)
            WHERE elementId(a) = $from_id AND elementId(b) = $to_id
              AND coalesce(a.{TENANT_PROPERTY}, $tenant) = $tenant
              AND coalesce(b.{TENANT_PROPERTY}, $tenant) = $tenant
            CREATE (a)-[r:{rel_type} $props]->(b)
            RETURN type(r) as type
            """
            params = {"from_id": from_id, "to_id": to_id, "props": props, "tenant": tenant}
        else:
            query = f"""
            MATCH (a), (b)
            WHERE elementId(a) = $from_id AND elementId(b) = $to_id
            CREATE (a)-[r:{rel_type} $props]->(b)
            RETURN type(r) as type
            """
            params = {"from_id": from_id, "to_id": to_id, "props": props}

        results = await self.execute_query(query, params)
        return len(results) > 0

    async def find_node(
        self,
        label: str,
        properties: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Найти узел по свойствам, отфильтровав по текущему tenant_id."""
        tenant = _enforce_tenant_or_none("find_node")
        where_clauses = [f"n.{k} = ${k}" for k in properties.keys()]
        params = dict(properties)
        if tenant is not None and TENANT_PROPERTY not in properties:
            where_clauses.append(f"(n.{TENANT_PROPERTY} IS NULL OR n.{TENANT_PROPERTY} = $_tenant)")
            params["_tenant"] = tenant
        where_str = " AND ".join(where_clauses) if where_clauses else "1=1"

        query = f"""
        MATCH (n:{label})
        WHERE {where_str}
        RETURN n, elementId(n) as id
        LIMIT 1
        """
        results = await self.execute_query(query, params)
        if results:
            return {"id": results[0]["id"], **dict(results[0]["n"])}
        return None

    async def get_subgraph(
        self,
        node_id: str,
        depth: int = 2,
    ) -> dict[str, Any]:
        """Подграф от узла. Все промежуточные узлы должны принадлежать
        текущему tenant'у (если он задан) — иначе обход остановится."""
        tenant = _enforce_tenant_or_none("get_subgraph")
        if tenant is not None:
            # Ограничиваем обход: каждый узел в пути должен принадлежать tenant'у.
            query = f"""
            MATCH path = (n)-[*1..{depth}]-(m)
            WHERE elementId(n) = $node_id
              AND ALL(x IN nodes(path) WHERE
                  x.{TENANT_PROPERTY} IS NULL OR x.{TENANT_PROPERTY} = $_tenant)
            RETURN nodes(path) as nodes, relationships(path) as rels
            """
            params = {"node_id": node_id, "_tenant": tenant}
        else:
            query = f"""
            MATCH path = (n)-[*1..{depth}]-(m)
            WHERE elementId(n) = $node_id
            RETURN nodes(path) as nodes, relationships(path) as rels
            """
            params = {"node_id": node_id}

        results = await self.execute_query(query, params)

        nodes = {}
        relationships = []

        for record in results:
            for node in record.get("nodes", []):
                node_id = node.element_id
                if node_id not in nodes:
                    nodes[node_id] = {
                        "id": node_id,
                        "labels": list(node.labels),
                        "properties": dict(node),
                    }

            for rel in record.get("rels", []):
                relationships.append({
                    "id": rel.element_id,
                    "type": rel.type,
                    "start": rel.start_node.element_id,
                    "end": rel.end_node.element_id,
                    "properties": dict(rel),
                })

        return {
            "nodes": list(nodes.values()),
            "relationships": relationships,
        }

    async def health_check(self) -> bool:
        """Проверка здоровья подключения"""
        try:
            if self.driver:
                async with self.driver.session() as session:
                    await session.run("RETURN 1")
                return True
            return False
        except Exception:
            return False

    async def close(self):
        """Закрытие подключения"""
        if self.driver:
            await self.driver.close()
            logger.info("Neo4j connection closed")


# Singleton instance
_neo4j_client: Neo4jClient | None = None


async def get_neo4j() -> Neo4jClient:
    """Получить клиент Neo4j"""
    global _neo4j_client
    if _neo4j_client is None:
        _neo4j_client = Neo4jClient()
        await _neo4j_client.initialize()
    return _neo4j_client
