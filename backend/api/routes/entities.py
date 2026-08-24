"""
TESSENT BRAIN - Entity Routes
Эндпоинты для работы с сущностями графа знаний
"""
from typing import Any, Literal, Optional

import structlog
from litestar import Router, get, post
from litestar.params import Parameter
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)

# Типы сущностей
EntityType = Literal[
    "person", "project", "meeting", "task",
    "problem", "decision", "team", "client", "product"
]


# === Request/Response Models ===

class EntityCreateRequest(BaseModel):
    """Запрос на создание сущности"""
    entity_type: EntityType
    name: str
    properties: dict[str, Any] = Field(default_factory=dict)
    aliases: list[str] = Field(default_factory=list)


class EntityUpdateRequest(BaseModel):
    """Запрос на обновление сущности"""
    name: str | None = None
    properties: dict[str, Any] | None = None
    aliases: list[str] | None = None


class EntityResponse(BaseModel):
    """Ответ с данными сущности"""
    id: str
    entity_type: str
    name: str
    properties: dict[str, Any]
    aliases: list[str]
    relationships: list[dict[str, Any]] = Field(default_factory=list)


class EntitySearchRequest(BaseModel):
    """Запрос на поиск сущностей"""
    query: str
    entity_types: list[EntityType] | None = None
    limit: int = Field(20, ge=1, le=100)
    include_relationships: bool = False


# === Route Handlers ===

@get("/")
async def list_entities(
    entity_type: EntityType | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """
    Получить список сущностей

    Args:
        entity_type: Фильтр по типу
        limit: Максимальное количество
        offset: Смещение

    Returns:
        Список сущностей
    """
    # TODO: Запрос к Neo4j
    return {
        "entities": [],
        "total": 0,
        "limit": limit,
        "offset": offset,
        "entity_type": entity_type,
    }


@get("/{entity_id:str}")
async def get_entity(entity_id: str) -> dict[str, Any]:
    """
    Получить сущность по ID

    Args:
        entity_id: ID сущности в Neo4j

    Returns:
        Полные данные сущности с связями
    """
    from backend.db import get_neo4j

    neo4j = await get_neo4j()

    # Получаем подграф от сущности
    subgraph = await neo4j.get_subgraph(entity_id, depth=1)

    if not subgraph["nodes"]:
        return {"error": "Entity not found", "entity_id": entity_id}

    # Находим основную сущность
    main_entity = None
    for node in subgraph["nodes"]:
        if node["id"] == entity_id:
            main_entity = node
            break

    if not main_entity:
        return {"error": "Entity not found", "entity_id": entity_id}

    return {
        "id": main_entity["id"],
        "labels": main_entity["labels"],
        "properties": main_entity["properties"],
        "relationships": subgraph["relationships"],
        "connected_nodes": [n for n in subgraph["nodes"] if n["id"] != entity_id],
    }


@post("/")
async def create_entity(data: EntityCreateRequest) -> dict[str, Any]:
    """
    Создать новую сущность

    Args:
        data: Данные сущности

    Returns:
        Созданная сущность с ID
    """
    from backend.db import get_neo4j

    neo4j = await get_neo4j()

    # Маппинг типов на метки Neo4j
    label_map = {
        "person": "Person",
        "project": "Project",
        "meeting": "Meeting",
        "task": "Task",
        "problem": "Problem",
        "decision": "Decision",
        "team": "Team",
        "client": "Client",
        "product": "Product",
    }

    label = label_map.get(data.entity_type, "Entity")

    properties = {
        "name": data.name,
        "aliases": data.aliases,
        **data.properties,
    }

    entity_id = await neo4j.create_node(label, properties)

    if entity_id:
        logger.info(
            "✅ Entity created",
            entity_id=entity_id,
            entity_type=data.entity_type,
            name=data.name,
        )
        return {
            "id": entity_id,
            "entity_type": data.entity_type,
            "name": data.name,
            "properties": properties,
            "status": "created",
        }

    return {"error": "Failed to create entity"}


@post("/search")
async def search_entities(data: EntitySearchRequest) -> dict[str, Any]:
    """
    Поиск сущностей по запросу

    Использует:
    - Векторный поиск в Qdrant для семантического поиска
    - Cypher запросы в Neo4j для точного поиска

    Args:
        data: Параметры поиска

    Returns:
        Найденные сущности с релевантностью
    """
    # TODO: Реализовать гибридный поиск
    # 1. Векторный поиск в Qdrant
    # 2. Cypher поиск в Neo4j
    # 3. Объединение результатов (RRF)

    return {
        "query": data.query,
        "results": [],
        "total": 0,
        "search_type": "hybrid",
    }


@get("/{entity_id:str}/graph")
async def get_entity_graph(
    entity_id: str,
    depth: int = 2,
) -> dict[str, Any]:
    """
    Получить подграф сущности

    Args:
        entity_id: ID сущности
        depth: Глубина обхода (1-5)

    Returns:
        Подграф с узлами и связями для визуализации
    """
    from backend.db import get_neo4j

    depth = min(max(depth, 1), 5)  # Ограничиваем 1-5

    neo4j = await get_neo4j()
    subgraph = await neo4j.get_subgraph(entity_id, depth=depth)

    return {
        "entity_id": entity_id,
        "depth": depth,
        "nodes": subgraph["nodes"],
        "edges": subgraph["relationships"],
        "stats": {
            "node_count": len(subgraph["nodes"]),
            "edge_count": len(subgraph["relationships"]),
        },
    }


def _trusted_uid(authorization: Optional[str], user_id: Optional[str]) -> Optional[str]:
    """Анти-IDOR (паттерн documents): токен валиден, но user_id чужой → None
    (отказ). Без токена (внутренние вызовы) — как раньше, доверяем user_id."""
    try:
        from backend.core.auth.service_token import trusted_user_id
        headers = {"authorization": authorization} if authorization else {}
        uid, src = trusted_user_id(headers, user_id or "")
        if src != "unverified" and uid:
            return uid
    except PermissionError:
        logger.warning("entities: токен валиден, но запрошен чужой user_id "
                       f"(req={str(user_id)[:8]}…) — отказ")
        return None
    except Exception:
        logger.debug("entities: trusted_user_id unavailable", exc_info=True)
    return user_id


@get("/graph")
async def get_full_graph(
    limit: int = 500,
    user_id: str | None = None,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """
    Получить полный граф знаний для визуализации.

    Args:
        limit: Максимальное количество узлов
        user_id: ID пользователя для фильтрации

    Returns:
        Граф в формате для vis.js / d3.js
    """
    # Анти-IDOR: user_id обязан совпадать с токеном (иначе стейлный/чужой id
    # из localStorage подтягивал ЧУЖОЙ граф — «данные из другого аккаунта»)
    user_id = _trusted_uid(authorization, user_id)
    try:
        from backend.core.store.graph_builder import GraphBuilder
        from backend.core.store.tenant_paths import graph_path_for_user

        # Требуем user_id, чтобы не было утечек между пользователями.
        # nodes/edges обязательны и в отказе — фронт читает .nodes без guard'а
        if not user_id:
            return {"error": "Authentication required", "nodes": [], "edges": []}

        # Цвета для типов
        color_map = {
            "Person": "#4CAF50",      # Зелёный
            "Project": "#2196F3",     # Синий
            "Product": "#03A9F4",     # Светло-синий
            "Meeting": "#FF9800",     # Оранжевый
            "Task": "#9C27B0",        # Фиолетовый
            "Decision": "#F44336",    # Красный
            "Entity": "#607D8B",      # Серый
            "Topic": "#00BCD4",       # Голубой
            "Technology": "#795548",  # Коричневый
            "KPI": "#FFEB3B",         # Жёлтый
            "Regulation": "#E91E63",  # Розовый
            "Template": "#3F51B5",    # Индиго
            "Episodic": "#009688",    # Бирюзовый
            # New types (Step 1)
            "Knowledge": "#8BC34A",   # Светло-зелёный
            "Principle": "#FF5722",   # Глубокий оранж
            "Team": "#00BCD4",        # Голубой
            "Department": "#009688",  # Бирюзовый
            "Role": "#CDDC39",        # Лайм
            "Milestone": "#FFC107",   # Янтарный
            "Risk": "#F44336",        # Красный
            "Commitment": "#673AB7",  # Глубокий фиолет
            "Contradiction": "#FF1744",# Ярко-красный
            "Pattern": "#00E676",     # Ярко-зелёный
            "Question": "#FFD740",    # Жёлтый
            "Chunk": "#90A4AE",       # Серо-голубой
        }

        # Загружаем per-user граф (изоляция на уровне файлов/путей)
        graph_builder = GraphBuilder(
            use_networkx=None,  # Auto-detect backend
            graph_storage_path=graph_path_for_user(user_id),
        )
        await graph_builder.connect()

        # ФЕДЕРАТИВНО: кроме personal (user_id) включаем узлы ОРГ-тенанта (scope)
        # — люди/отделы промоутнуты туда, иначе общий/3D граф пуст, хотя данные
        # есть. scope = org пользователя (или он сам). Cross-tenant невозможен.
        from backend.core.ingest.membership import get_org_for_user
        scope = get_org_for_user(user_id) or user_id

        nodes = []
        edges = []
        node_ids_set = set()

        # Проверяем, используется ли Neo4j
        if graph_builder.driver:
            # Получаем данные из Neo4j напрямую (синхронный драйвер для визуализации)
            from neo4j import GraphDatabase
            sync_driver = GraphDatabase.driver(graph_builder.uri, auth=(graph_builder.user, graph_builder.password))
            with sync_driver.session() as session:
                # Получаем узлы (фильтруем по user_id или берём все если user_id не указан в узле)
                # СТРОГИЙ tenant (никакого NULL-легаси) + ПРИОРИТЕТ бизнес-ядра:
                # раньше `MATCH (n) ... LIMIT` брал ПРОИЗВОЛЬНЫЕ узлы — граф
                # «крутился вокруг случайной встречи», люди попадали выборочно.
                # Теперь квоты по типам: сначала структура компании (люди,
                # отделы, проекты, клиенты, решения), встречи/задачи — добором.
                _quota = {
                    "Person": int(limit * 0.35), "Department": 30, "Team": 30,
                    "Project": 40, "Product": 25, "Entity": int(limit * 0.15),
                    "Decision": int(limit * 0.12), "Task": int(limit * 0.12),
                    "Meeting": int(limit * 0.10),
                }
                _rows: list = []
                _seen_ids: set = set()
                for _lbl, _cap in _quota.items():
                    if _cap <= 0:
                        continue
                    _r = session.run(f'''
                        MATCH (n:{_lbl})
                        WHERE n.user_id = $user_id OR n.tenant_id = $user_id
                           OR n.tenant_id = $scope OR n.user_id = $scope
                        RETURN
                            elementId(n) as id,
                            labels(n) as labels,
                            properties(n) as props
                        LIMIT $lim
                    ''', user_id=user_id, scope=scope, lim=_cap)
                    for _rec in _r:
                        if _rec["id"] not in _seen_ids:
                            _seen_ids.add(_rec["id"])
                            _rows.append(_rec)
                result = _rows

                def serialize_value(v):
                    """Convert Neo4j types to JSON-serializable types"""
                    if v is None:
                        return None
                    if isinstance(v, (str, int, float, bool)):
                        return v
                    if isinstance(v, (list, tuple)):
                        return [serialize_value(x) for x in v]
                    if isinstance(v, dict):
                        return {k: serialize_value(val) for k, val in v.items()}
                    return str(v)

                for record in result:
                    props = dict(record["props"])
                    # Use elementId as unique identifier to avoid duplicates
                    element_id = str(record["id"])
                    labels = list(record["labels"])

                    # Skip if already added (deduplication)
                    if element_id in node_ids_set:
                        continue

                    label = labels[0] if labels else "Entity"
                    name = props.get("name") or props.get("title") or props.get("text", element_id)[:50]

                    # Serialize properties
                    safe_props = {k: serialize_value(v) for k, v in props.items() if not str(k).startswith("_")}

                    nodes.append({
                        "id": element_id,
                        "label": str(name)[:30] + "..." if len(str(name)) > 30 else str(name),
                        "title": f"{label}: {name}",
                        "group": label,
                        "color": color_map.get(label, "#607D8B"),
                        "properties": safe_props
                    })
                    node_ids_set.add(element_id)

                # Получаем связи (используем elementId для консистентности с узлами)
                # Строгий tenant и для рёбер (NULL-узлы чужого легаси не берём).
                result = session.run('''
                    MATCH (a)-[r]->(b)
                    WHERE (a.user_id = $user_id OR a.tenant_id = $user_id OR a.tenant_id = $scope OR a.user_id = $scope)
                      AND (b.user_id = $user_id OR b.tenant_id = $user_id OR b.tenant_id = $scope OR b.user_id = $scope)
                    RETURN
                        elementId(a) as from_id,
                        elementId(b) as to_id,
                        type(r) as rel_type
                    LIMIT $limit
                ''', user_id=user_id, scope=scope, limit=limit * 2)

                for record in result:
                    from_id = str(record["from_id"])
                    to_id = str(record["to_id"])
                    if from_id in node_ids_set and to_id in node_ids_set:
                        edges.append({
                            "from": from_id,
                            "to": to_id,
                            "label": record["rel_type"],
                            "arrows": "to",
                        })

            sync_driver.close()

        elif graph_builder.nx_graph:
            # Fallback на NetworkX (федеративно: свои + орг-scope, legacy-NULL)
            for node_id, data in list(graph_builder.nx_graph.nodes(data=True)):
                node_owner = data.get("user_id") or data.get("tenant_id") or data.get("owner_id")
                if node_owner and node_owner not in (user_id, scope):
                    continue

                if len(nodes) >= limit:
                    break

                label = data.get("_label", data.get("label", "Entity"))
                name = data.get("name", data.get("title", node_id))

                nodes.append({
                    "id": node_id,
                    "label": name[:30] + "..." if len(str(name)) > 30 else str(name),
                    "title": f"{label}: {name}",
                    "group": label,
                    "color": color_map.get(label, "#607D8B"),
                    "properties": {k: v for k, v in data.items() if not k.startswith("_")}
                })
                node_ids_set.add(node_id)

            for source, target, data in graph_builder.nx_graph.edges(data=True):
                if source in node_ids_set and target in node_ids_set:
                    rel_type = data.get("type", data.get("_type", "RELATED"))
                    edges.append({
                        "from": source,
                        "to": target,
                        "label": rel_type,
                        "arrows": "to",
                    })

        await graph_builder.close()

        return {
            "nodes": nodes,
            "edges": edges,
            "stats": {
                "node_count": len(nodes),
                "edge_count": len(edges),
            },
            "color_legend": color_map,
            "user_id": user_id,
            "backend": "neo4j" if graph_builder.driver else "networkx"
        }

    except Exception as e:
        logger.error(f"Error loading graph: {e}")
        import traceback
        traceback.print_exc()
        return {
            "nodes": [],
            "edges": [],
            "error": str(e)
        }


@get("/org-graph")
async def get_org_graph(
    user_id: str | None = None,
    limit: int = 300,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Оргструктура «кто кем является»: только люди/команды/отделы и связи
    подчинения/членства/руководства (REPORTS_TO / MEMBER_OF / LEADS).

    Отдельный от /graph фокус-вид — чтобы видеть оргсхему, а не весь граф
    вперемешку со встречами/задачами. Backend-agnostic (Neo4j + NetworkX).
    """
    # Анти-IDOR: оргсхема отдавалась по ЛЮБОМУ user_id из query без сверки с
    # токеном — стейлный id из localStorage показывал чужую оргсхему
    user_id = _trusted_uid(authorization, user_id)
    if not user_id:
        return {"error": "Authentication required", "nodes": [], "edges": []}
    try:
        color_map = {"Person": "#4CAF50", "Team": "#00BCD4", "Department": "#009688"}
        org_edges = {"REPORTS_TO", "MEMBER_OF", "LEADS"}

        # ФЕДЕРАТИВНЫЙ вид (personal ∪ org, RBAC) — тот же, что у снепшотов.
        # Раньше читали ТОЛЬКО personal-граф со strict-tenant → оргсхема-граф был
        # пуст, хотя люди/отделы промоутнуты в орг-граф (их видит «Компания»).
        from backend.core.ingest.membership import get_org_for_user
        from backend.core.store.graph_view import merged_graph_view_for_user
        scope = get_org_for_user(user_id) or user_id
        gb = await merged_graph_view_for_user(user_id, use_networkx=None)

        nodes: list[dict[str, Any]] = []
        node_ids: set[str] = set()
        _nx = getattr(gb, "nx_graph", None)
        for label in ("Person", "Team", "Department"):
            if _nx is not None:
                # NetworkX merged: узлы уже отфильтрованы RBAC при merge
                found = [{"id": nid, **data} for nid, data in _nx.nodes(data=True)
                         if data.get("_label") == label][:limit]
            else:
                try:
                    # Neo4j: tenant_id=None → контекст (org_or_user), не strict →
                    # видны и промоутнутые орг-узлы, и legacy. Cross-tenant нельзя.
                    found = await gb.find_nodes_by_label(
                        label, limit=limit, tenant_id=None, strict_tenant=False)
                except Exception:
                    found = []
            for n in found:
                nid = n.get("id")
                if not nid or nid in node_ids:
                    continue
                node_ids.add(nid)
                nm = n.get("name") or n.get("title") or nid
                nodes.append({
                    "id": nid,
                    "label": str(nm)[:30],
                    "title": f"{label}: {nm}",
                    "group": label,
                    "color": color_map.get(label, "#607D8B"),
                    "properties": {k: v for k, v in n.items() if not str(k).startswith("_")},
                })

        edges: list[dict[str, Any]] = []
        seen_edges: set[tuple] = set()

        def _add_edge(f: Any, t: Any, rt: Any) -> None:
            if not (f and t and rt):
                return
            if f in node_ids and t in node_ids and (f, t, rt) not in seen_edges:
                seen_edges.add((f, t, rt))
                edges.append({"from": f, "to": t, "label": rt, "arrows": "to"})

        # Рёбра ОДНИМ запросом. Раньше — get_node_relationships на КАЖДЫЙ
        # узел (N+1): на аккаунте с сотнями людей это сотни последовательных
        # Neo4j-запросов и минуты ожидания — «оргсхема просто висит».
        batch_ok = False
        try:
            if getattr(gb, "driver", None):
                async with gb.driver.session() as session:
                    res = await session.run(
                        "MATCH (a)-[r]->(b) WHERE type(r) IN $types "
                        "AND (a.tenant_id = $scope OR a.user_id = $scope OR a.user_id = $uid OR a.tenant_id = $uid) "
                        "AND (b.tenant_id = $scope OR b.user_id = $scope OR b.user_id = $uid OR b.tenant_id = $uid) "
                        "RETURN a.id AS f, b.id AS t, type(r) AS rt "
                        "LIMIT 8000",
                        {"types": sorted(org_edges), "uid": user_id, "scope": scope})
                    rows = await res.data()
                for row in rows or []:
                    _add_edge(row.get("f"), row.get("t"), row.get("rt"))
                batch_ok = True
            elif getattr(gb, "nx_graph", None) is not None:
                for src, tgt, d in gb.nx_graph.edges(data=True):
                    rt = str(d.get("type") or d.get("_type") or "").upper()
                    if rt in org_edges:
                        _add_edge(src, tgt, rt)
                batch_ok = True
        except Exception:
            logger.warning("org-graph: batch edge query failed — fallback N+1",
                           exc_info=True)

        if not batch_ok:
            # Страховочный старый путь (ограничен, чтобы не висеть минутами)
            for nid in list(node_ids)[:150]:
                try:
                    rels = await gb.get_node_relationships(nid)
                except Exception:
                    continue
                for e in rels.get("outgoing", []):
                    if e.get("type") in org_edges:
                        _add_edge(nid, e.get("target"), e.get("type"))

        await gb.close(save=False)
        return {
            "nodes": nodes,
            "edges": edges,
            "stats": {"node_count": len(nodes), "edge_count": len(edges)},
            "color_legend": color_map,
            "user_id": user_id,
        }
    except Exception as e:
        logger.error(f"Error loading org graph: {e}")
        return {"nodes": [], "edges": [], "error": str(e)}


@get("/by-label")
async def get_nodes_by_label(
    label: str,
    user_id: str | None = None,
    limit: int = 50,
    category: str | None = None,
) -> dict[str, Any]:
    """Список узлов заданной метки с полными свойствами (для секций UI:
    Goal/ConflictRisk/Scenario/Prediction/Knowledge и т.п.). Опциональный
    фильтр по полю category (напр. Knowledge category=marketing — наработки
    Mark). Backend-agnostic, tenant-safe.
    """
    if not user_id:
        return {"error": "Authentication required", "items": []}
    # whitelist меток (чтобы не дёргать произвольное)
    allowed = {"Goal", "ConflictRisk", "Scenario", "Prediction", "KPI",
               "Principle", "Contradiction", "Risk", "Knowledge"}
    if label not in allowed:
        return {"error": f"label must be one of {sorted(allowed)}", "items": []}
    try:
        from backend.core.store.graph_builder import GraphBuilder
        from backend.core.store.tenant_paths import graph_path_for_user
        gb = GraphBuilder(use_networkx=None, graph_storage_path=graph_path_for_user(user_id))
        await gb.connect()
        nodes = await gb.find_nodes_by_label(label, limit=max(limit, 200), tenant_id=user_id)
        await gb.close(save=False)
        # чистим служебные поля + фильтр по категории
        items = []
        seen_stmt: set = set()
        for n in nodes:
            if category and n.get("category") != category:
                continue
            # Goal: старые «цели»-обрывки транскрипта (status=unclear,
            # entity_name=«Сотрудник»/«Задача») уже записаны в граф до фикса
            # goals_tracking_agent — прячем их из UI. И дедуп по формулировке
            # («Завершить проект: Практикум КПД» показывался дважды).
            if label == "Goal":
                _st = (n.get("status") or n.get("current_status") or "").lower()
                if _st == "unclear":
                    continue
                _stmt = (n.get("goal_statement") or "").strip().lower()
                if _stmt:
                    if _stmt in seen_stmt:
                        continue
                    seen_stmt.add(_stmt)
            items.append({k: v for k, v in n.items() if not str(k).startswith("_")})
        # свежие выше
        items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        page = items[:limit]
        # Метка свежести: без неё факт девятимесячной давности выглядит в
        # списке так же уверенно, как вчерашний. Пороги — общие с decay.
        from backend.core.store.freshness import annotate
        annotate(page, entity_type=label)
        # Чтение узлов уровня CONFIDENTIAL и выше — одной агрегированной
        # записью на выдачу, а не строкой на узел.
        from backend.core.observability.read_audit import record_sensitive_nodes
        await record_sensitive_nodes(user_id, page,
                                     resource_type=f"graph_{label.lower()}")
        return {"total": len(items), "items": page}
    except Exception as e:
        logger.error(f"by-label({label}) failed: {e}")
        return {"total": 0, "items": [], "error": str(e)}


@get("/most-mentioned")
async def get_most_mentioned(
    user_id: str | None = None,
    limit: int = 15,
    by: str = "mentions",
) -> dict[str, Any]:
    """Лента сущностей: by=mentions — «в фокусе» (total_mentions); by=salience —
    «горячее» (emotional_salience, требует flag enable_emotional_salience).

    Считается на узлах при ингесте, но наружу не выводилось. Backend-agnostic.
    """
    if not user_id:
        return {"error": "Authentication required", "items": []}
    prop = "emotional_salience" if by == "salience" else "total_mentions"
    try:
        from backend.core.store.graph_builder import GraphBuilder
        from backend.core.store.tenant_paths import graph_path_for_user

        emoji = {"Person": "👤", "Project": "📁", "Product": "📦", "Team": "👥",
                 "Department": "🏢", "Entity": "🔷", "KPI": "📊", "Decision": "✅"}
        gb = GraphBuilder(use_networkx=None, graph_storage_path=graph_path_for_user(user_id))
        await gb.connect()
        items: list[dict[str, Any]] = []
        if gb.driver:
            async with gb.driver.session() as session:
                res = await session.run(
                    f"""
                    MATCH (n)
                    WHERE coalesce(n.{prop}, 0) > 0
                      AND (n.user_id = $uid OR n.tenant_id = $uid)
                      AND coalesce(n.name, n.title, '') <> ''
                    RETURN coalesce(n.name, n.title) AS name,
                           labels(n) AS labels,
                           n.{prop} AS score
                    ORDER BY n.{prop} DESC LIMIT $lim
                    """,
                    {"uid": user_id, "lim": limit},
                )
                for rec in await res.data():
                    labels = rec.get("labels") or []
                    label = labels[0] if labels else "Entity"
                    items.append({
                        "name": rec.get("name"),
                        "label": label,
                        "emoji": emoji.get(label, "•"),
                        "score": rec.get("score") or 0,
                        "mentions": rec.get("score") or 0,
                    })
        elif gb.nx_graph is not None:
            cand = []
            for nid, d in gb.nx_graph.nodes(data=True):
                m = d.get(prop, 0) or 0
                nm = d.get("name") or d.get("title") or ""
                if m > 0 and nm and (d.get("user_id") in (None, "", user_id)):
                    label = d.get("_label", "Entity")
                    cand.append({"name": nm, "label": label, "emoji": emoji.get(label, "•"), "score": m, "mentions": m})
            cand.sort(key=lambda x: x["score"], reverse=True)
            items = cand[:limit]
        await gb.close(save=False)
        return {"total": len(items), "items": items}
    except Exception as e:
        logger.error(f"most-mentioned failed: {e}")
        return {"total": 0, "items": [], "error": str(e)}


@post("/relationship")
async def create_relationship(
    from_id: str,
    to_id: str,
    rel_type: str,
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Создать связь между сущностями

    Args:
        from_id: ID исходной сущности
        to_id: ID целевой сущности
        rel_type: Тип связи (PARTICIPATED_IN, OWNS_TASK, etc.)
        properties: Свойства связи

    Returns:
        Статус создания
    """
    from backend.db import get_neo4j

    neo4j = await get_neo4j()
    success = await neo4j.create_relationship(from_id, to_id, rel_type, properties)

    if success:
        logger.info(
            "✅ Relationship created",
            from_id=from_id,
            to_id=to_id,
            rel_type=rel_type,
        )
        return {
            "status": "created",
            "from_id": from_id,
            "to_id": to_id,
            "rel_type": rel_type,
        }

    return {"error": "Failed to create relationship"}


# Router
class RenameRequest(BaseModel):
    node_id: str
    name: str


@post("/rename")
async def rename_entity(
    data: RenameRequest,
    user_id: str | None = None,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Переименовать узел графа (сотрудник/отдел) — сохраняется в БД.

    Анти-IDOR: user_id обязан совпадать с токеном; узел обязан принадлежать
    этому тенанту (иначе можно было бы переименовать чужой узел в shared-Neo4j).
    """
    user_id = _trusted_uid(authorization, user_id)
    if not user_id:
        return {"status": "error", "message": "Authentication required"}
    nid = (data.node_id or "").strip()
    name = (data.name or "").strip()
    if not nid or not name:
        return {"status": "error", "message": "node_id и name обязательны"}
    try:
        from backend.core.store.graph_view import writer_graph_for_user
        gb = await writer_graph_for_user(user_id)
        try:
            node = await gb.get_node_by_id(nid)
            if not node:
                return {"status": "error", "message": "узел не найден"}
            owner = node.get("user_id") or node.get("tenant_id") or ""
            if owner and owner != user_id:
                logger.warning("rename: чужой узел (req=%s…) — отказ", str(user_id)[:8])
                return {"status": "error", "message": "нет доступа к этому узлу"}
            ok = await gb.update_node(nid, {"name": name, "label": name})
            if ok:
                await gb.save()
        finally:
            await gb.close(save=False)
        return {"status": "success" if ok else "error", "node_id": nid, "name": name}
    except Exception as e:
        logger.error(f"rename_entity failed: {e}")
        return {"status": "error", "message": str(e)}


router = Router(
    path="/entities",
    route_handlers=[
        list_entities,
        get_entity,
        create_entity,
        search_entities,
        get_full_graph,
        get_org_graph,
        get_most_mentioned,
        get_nodes_by_label,
        get_entity_graph,
        create_relationship,
        rename_entity,
    ],
    tags=["Entities"],
)



