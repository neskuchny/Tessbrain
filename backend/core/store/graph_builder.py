# -*- coding: utf-8 -*-
"""
Graph Builder - построение графа знаний из извлечённых сущностей.

Поддерживает два бэкенда:
1. Neo4j (персистентный граф) - для продакшена
2. NetworkX (in-memory граф) - для разработки/тестирования без Docker

Типы узлов:
- Meeting - встреча
- Person - участник
- Decision - решение
- Task - задача
- Entity - сущность (проект, организация, технология, документ, событие, идея)
- KPI - метрика/показатель

Типы связей:
- PARTICIPATED_IN - участник → встреча
- MADE_DECISION - участник → решение
- ASSIGNED_TO - задача → участник
- MENTIONED_IN - сущность → встреча
- RELATED_TO - сущность ↔ сущность
- DEPENDS_ON - задача → задача
- CREATED_FROM - решение/задача → встреча
"""
import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from .schema import get_schema_validator

logger = logging.getLogger(__name__)


def _strict_tenant_default() -> bool:
    """TENANT_STRICT_DEFAULT=on → дефолтные чтения графа НЕ отдают
    NULL-легаси узлы (гигиена мульти-аккаунта). По умолчанию ВЫКЛ."""
    return os.getenv("TENANT_STRICT_DEFAULT", "").strip().lower() in (
        "on", "1", "true", "yes")

# Neo4j шлёт уведомления-WARNING вида «label/property does not exist» (когда
# запрашиваем новые метки Goal/Scenario или свойства total_mentions до того,
# как появятся первые узлы) и INFO «index already exists». Это не ошибки, а
# шум, заливающий логи. Поднимаем порог их логгера до ERROR.
logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)

# Индексы Neo4j раньше создавались на КАЖДОЕ connect() (28 CREATE INDEX
# IF NOT EXISTS), а GraphBuilder открывается заново на каждую запись —
# десятки раз в секунду под нагрузкой синка. Это грузит Neo4j и засоряет
# лог ("✅ Neo4j indexes created" сотни раз). Создаём индексы ОДИН раз на
# процесс на каждый Neo4j URI (они идемпотентны — после первого раза смысла
# гонять нет до рестарта).
_INDEXES_CREATED_FOR_URI: set[str] = set()

# Общий AsyncDriver на процесс: (uri, user) → driver. Драйвер держит свой
# connection-пул и рассчитан на переиспользование всем процессом; создавать
# его на каждый запрос — дорогой анти-паттерн (см. _connect_neo4j).
_SHARED_DRIVERS: dict = {}

# Карта lowercase-тип → фактическая метка узла в графе. Редактор знаний и
# агенты передают тип в нижнем регистре ("project"), а метки в Neo4j
# капитализованы ("Project") — без нормализации MATCH (n:project) даёт
# «label does not exist» и пустой результат.
_SEARCH_LABEL_MAP = {
    "person": "Person", "people": "Person",
    "project": "Project", "product": "Product",
    "team": "Team", "department": "Department",
    "meeting": "Meeting", "task": "Task", "decision": "Decision",
    "kpi": "KPI", "role": "Role", "milestone": "Milestone", "risk": "Risk",
    "idea": "Idea", "opinion": "Opinion", "principle": "Principle",
    "contradiction": "Contradiction", "regulation": "Regulation",
    "company": "Entity", "organization": "Entity", "feature": "Entity",
    "entity": "Entity", "technology": "Entity", "concept": "Entity",
}

# Проверяем доступность Neo4j
try:
    from neo4j import AsyncDriver, AsyncGraphDatabase
    NEO4J_AVAILABLE = True
except ImportError:
    NEO4J_AVAILABLE = False
    logger.info("neo4j не установлен - используется NetworkX fallback")

# NetworkX всегда доступен
try:
    import networkx as nx
    NETWORKX_AVAILABLE = True
except ImportError:
    NETWORKX_AVAILABLE = False
    logger.warning("networkx не установлен. pip install networkx")


class GraphBuilder:
    """
    Строитель графа знаний.

    Преобразует извлечённые сущности в узлы и связи.
    Поддерживает Neo4j и NetworkX бэкенды.

    Пример использования:
    ```python
    # С NetworkX (без Docker)
    builder = GraphBuilder(use_networkx=True)
    await builder.connect()

    # С Neo4j (с Docker)
    builder = GraphBuilder(use_networkx=False)
    await builder.connect()

    # Сохранить результаты обработки встречи
    stats = await builder.save_meeting_results(
        meeting_id="meeting_123",
        results=orchestrator_results
    )

    logger.info(f"Создано узлов: {stats['nodes_created']}")
    logger.info(f"Создано связей: {stats['relationships_created']}")

    # Экспорт графа (только NetworkX)
    builder.export_graph("graph_backup.json")
    ```
    """

    def __init__(
        self,
        uri: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        use_networkx: Optional[bool] = None,
        graph_storage_path: Optional[str] = None
    ):
        """
        Args:
            uri: URI Neo4j (bolt://localhost:7687)
            user: Пользователь Neo4j
            password: Пароль Neo4j
            use_networkx: Принудительно использовать NetworkX (None = авто)
            graph_storage_path: Путь для сохранения NetworkX графа
        """
        self.uri = uri or os.environ.get("NEO4J_URI", "bolt://localhost:7687")
        self.user = user or os.environ.get("NEO4J_USER", "neo4j")
        self.password = password or os.environ.get("NEO4J_PASSWORD", "neo4j_secret")

        # Определяем бэкенд.
        #
        # Принимаем ОБА имени переменной: USE_NETWORKX и
        # USE_NETWORKX_GRAPH. Второе — то, которым пользуются
        # core/gdpr/org_cascade.py и docs/NEO4J_MIGRATION.md («процедура
        # отката на файловый граф»), а читалось здесь только первое.
        # То есть документированный откат не работал: человек выставлял
        # переменную из инструкции, а граф продолжал ломиться в Neo4j.
        if use_networkx is None:
            env_use_nx = (os.environ.get("USE_NETWORKX")
                          or os.environ.get("USE_NETWORKX_GRAPH")
                          or "").strip().lower()
            if env_use_nx in ("true", "1", "yes", "on"):
                self.use_networkx = True
            elif env_use_nx in ("false", "0", "no", "off"):
                # Явное «нет» уважаем; если Neo4j при этом недоступен,
                # _connect_neo4j всё равно переключится на NetworkX — но
                # это будет аварийная деградация с записью в лог, а не
                # тихий выбор бэкенда за спиной у настройки.
                self.use_networkx = not NEO4J_AVAILABLE
            else:
                self.use_networkx = not NEO4J_AVAILABLE
        else:
            self.use_networkx = use_networkx

        # Neo4j
        self.driver: Optional[AsyncDriver] = None
        self._driver_shared = False  # True, если driver взят из _SHARED_DRIVERS

        # NetworkX
        self.nx_graph: Optional[nx.MultiDiGraph] = None
        self.graph_storage_path = graph_storage_path or os.environ.get(
            "GRAPH_STORAGE_PATH",
            "data/knowledge_graph.json"
        )
        self._node_index: Dict[Tuple[str, str], str] = {}  # (label, key) -> node_id

        self.connected = False
        self.backend = "networkx" if self.use_networkx else "neo4j"

        # Статистика
        self.stats = {
            "nodes_created": 0,
            "relationships_created": 0,
            "errors": 0
        }

    async def connect(self) -> bool:
        """Подключиться к хранилищу графа (Neo4j или NetworkX)"""

        if self.use_networkx:
            return await self._connect_networkx()
        else:
            return await self._connect_neo4j()

    async def _connect_networkx(self) -> bool:
        """Инициализировать NetworkX граф"""
        if not NETWORKX_AVAILABLE:
            logger.error("❌ NetworkX не установлен")
            return False

        try:
            # Создаём направленный мультиграф (поддерживает несколько связей между узлами)
            self.nx_graph = nx.MultiDiGraph()

            # Пробуем загрузить существующий граф
            storage_path = Path(self.graph_storage_path)
            if storage_path.exists():
                self._load_graph_from_file(storage_path)
                logger.info(f"✅ Loaded existing graph from {storage_path}")
            else:
                # Создаём директорию если нужно
                storage_path.parent.mkdir(parents=True, exist_ok=True)
                logger.info(f"✅ Created new NetworkX graph (will save to {storage_path})")

            self.connected = True
            self.backend = "networkx"
            logger.info("✅ GraphBuilder connected (NetworkX in-memory)")
            logger.info(f"   📊 Nodes: {self.nx_graph.number_of_nodes()}, Edges: {self.nx_graph.number_of_edges()}")

            return True

        except Exception as e:
            logger.error(f"❌ Failed to initialize NetworkX: {e}")
            self.connected = False
            return False

    async def _connect_neo4j(self) -> bool:
        """Подключиться к Neo4j"""
        if not NEO4J_AVAILABLE:
            logger.warning("⚠️ Neo4j SDK не установлен, переключаюсь на NetworkX")
            self.use_networkx = True
            return await self._connect_networkx()

        try:
            # ОБЩИЙ драйвер на процесс (per URI+user). Раньше каждый запрос/шаг
            # создавал НОВЫЙ AsyncDriver со своим connection-пулом и закрывал его
            # (в логах бесконечные «connected/closed») — на нагрузке это churn
            # соединений и лишняя латентность. Драйвер neo4j потокобезопасен и
            # рассчитан на переиспользование; сессии по-прежнему короткоживущие.
            _key = (self.uri, self.user)
            drv = _SHARED_DRIVERS.get(_key)
            if drv is None:
                drv = AsyncGraphDatabase.driver(
                    self.uri,
                    auth=(self.user, self.password)
                )
                _SHARED_DRIVERS[_key] = drv
            self.driver = drv
            self._driver_shared = True

            # Проверяем подключение
            try:
                async with self.driver.session() as session:
                    await session.run("RETURN 1")
            except Exception:
                # Мёртвый кэшированный драйвер (Neo4j перезапускался) —
                # выбрасываем из кэша и пробуем один раз с нуля.
                _SHARED_DRIVERS.pop(_key, None)
                try:
                    await drv.close()
                except Exception:
                    logger.debug("stale driver close failed", exc_info=True)
                drv = AsyncGraphDatabase.driver(
                    self.uri, auth=(self.user, self.password)
                )
                async with drv.session() as session:
                    await session.run("RETURN 1")
                _SHARED_DRIVERS[_key] = drv
                self.driver = drv

            self.connected = True
            self.backend = "neo4j"
            logger.debug(f"GraphBuilder connected to Neo4j: {self.uri}")

            # Создаём индексы один раз на процесс на каждый URI (см. коммент
            # к _INDEXES_CREATED_FOR_URI). На повторных connect — пропускаем.
            if self.uri not in _INDEXES_CREATED_FOR_URI:
                await self._create_indexes()

            return True

        except Exception as e:
            logger.warning(f"⚠️ Neo4j недоступен ({e}), переключаюсь на NetworkX")
            self.use_networkx = True
            return await self._connect_networkx()

    async def _create_indexes(self):
        """Создание индексов для оптимизации запросов"""
        indexes = [
            # Оригинальные типы
            "CREATE INDEX IF NOT EXISTS FOR (m:Meeting) ON (m.meeting_id)",
            "CREATE INDEX IF NOT EXISTS FOR (p:Person) ON (p.name)",
            "CREATE INDEX IF NOT EXISTS FOR (d:Decision) ON (d.decision_id)",
            "CREATE INDEX IF NOT EXISTS FOR (t:Task) ON (t.task_id)",
            "CREATE INDEX IF NOT EXISTS FOR (e:Entity) ON (e.entity_id)",
            "CREATE INDEX IF NOT EXISTS FOR (k:KPI) ON (k.kpi_id)",
            "CREATE INDEX IF NOT EXISTS FOR (c:Chunk) ON (c.chunk_id)",
            "CREATE INDEX IF NOT EXISTS FOR (pr:Principle) ON (pr.principle_id)",
            # ═══ Step 1: Новые типы ═══
            "CREATE INDEX IF NOT EXISTS FOR (r:Role) ON (r.role_id)",
            "CREATE INDEX IF NOT EXISTS FOR (tm:Team) ON (tm.team_id)",
            "CREATE INDEX IF NOT EXISTS FOR (dp:Department) ON (dp.department_id)",
            "CREATE INDEX IF NOT EXISTS FOR (pj:Project) ON (pj.project_id)",
            "CREATE INDEX IF NOT EXISTS FOR (pd:Product) ON (pd.product_id)",
            "CREATE INDEX IF NOT EXISTS FOR (ms:Milestone) ON (ms.milestone_id)",
            "CREATE INDEX IF NOT EXISTS FOR (rk:Risk) ON (rk.risk_id)",
            "CREATE INDEX IF NOT EXISTS FOR (cm:Commitment) ON (cm.commitment_id)",
            "CREATE INDEX IF NOT EXISTS FOR (pt:Pattern) ON (pt.pattern_id)",
            "CREATE INDEX IF NOT EXISTS FOR (ct:Contradiction) ON (ct.contradiction_id)",
            "CREATE INDEX IF NOT EXISTS FOR (q:Question) ON (q.question_id)",
            "CREATE INDEX IF NOT EXISTS FOR (sn:Snapshot) ON (sn.snapshot_id)",
            "CREATE INDEX IF NOT EXISTS FOR (rp:Report) ON (rp.report_id)",
            # ═══ Access control indexes ═══
            "CREATE INDEX IF NOT EXISTS FOR (n:Person) ON (n.access_level)",
            "CREATE INDEX IF NOT EXISTS FOR (n:Meeting) ON (n.tenant_id)",
        ]

        try:
            async with self.driver.session() as session:
                for index_query in indexes:
                    await session.run(index_query)
            _INDEXES_CREATED_FOR_URI.add(self.uri)
            logger.info("✅ Neo4j indexes created")
        except Exception as e:
            logger.warning(f"⚠️ Failed to create indexes: {e}")

    async def close(self, save: bool = False):
        """Закрыть подключение и опционально сохранить граф

        Args:
            save: Сохранить граф в файл (по умолчанию False чтобы не перезаписать данные)
        """
        if self.use_networkx:
            # Сохраняем граф только если явно запрошено
            if save:
                self._save_graph_to_file()
                logger.info("NetworkX graph saved and closed")
            else:
                logger.info("NetworkX graph closed (not saved)")
            self.nx_graph = None
        elif self.driver:
            # Общий драйвер НЕ закрываем — им пользуются другие вызовы в этом
            # процессе (закрытие роняло бы их с "driver closed"). Он живёт до
            # конца процесса; свои соединения драйвер пулит сам.
            if not getattr(self, "_driver_shared", False):
                await self.driver.close()
                logger.info("Neo4j connection closed")
            self.driver = None

        self.connected = False

    async def save(self) -> None:
        """
        Backward compatible async API.
        Исторически часть кода вызывала `await graph_builder.save()`.
        Для NetworkX это эквивалент `save_graph()`. Для Neo4j нет отдельного save.
        """
        try:
            if self.use_networkx:
                self._save_graph_to_file()
        except Exception as e:
            logger.error(f"❌ Failed to save graph: {e}")

    def _load_graph_from_file(self, path: Path):
        """Загрузить граф из JSON файла"""
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            self.nx_graph = nx.MultiDiGraph()

            # Восстанавливаем узлы
            for node in data.get("nodes", []):
                node_id = node.pop("id")
                self.nx_graph.add_node(node_id, **node)

                # Восстанавливаем индекс
                label = node.get("_label", "")
                key = node.get("_key", "")
                if label and key:
                    self._node_index[(label, key)] = node_id

            # Восстанавливаем связи
            for edge in data.get("edges", []):
                self.nx_graph.add_edge(
                    edge["source"],
                    edge["target"],
                    key=edge.get("key", 0),
                    **edge.get("data", {})
                )

        except Exception as e:
            logger.warning(f"⚠️ Could not load graph: {e}")
            self.nx_graph = nx.MultiDiGraph()

    def _stable_node_id(self, label: str, match_props: Dict[str, Any]) -> str:
        """
        Генерирует стабильный node id из label + match props.
        Это нужно, чтобы GraphBuilder работал одинаково на NetworkX и Neo4j:
        - все внутренние ссылки строятся по нодовым id
        - при перезапусках id не «прыгают»
        """
        # Стабильная сериализация match_props
        items = sorted((str(k), str(v)) for k, v in match_props.items())
        basis = f"{label}|" + "|".join([f"{k}={v}" for k, v in items])
        return str(uuid.uuid5(uuid.NAMESPACE_URL, basis))

    def get_all_nodes(self, *args, **kwargs) -> List[Dict[str, Any]]:
        """
        Backward compatible sync API.
        - NetworkX: returns immediately
        - Neo4j: if called inside an event loop -> instruct to use await get_all_nodes_async()
        """
        if self.use_networkx:
            label = kwargs.get("label")
            limit = kwargs.get("limit")
            tenant_id = kwargs.get("tenant_id")
            return self._get_all_nodes_networkx(
                label=label, limit=limit, tenant_id=tenant_id)

        # Neo4j backend: sync access is unsafe inside an async server loop.
        try:
            asyncio.get_running_loop()
            raise RuntimeError("GraphBuilder.get_all_nodes() must not be used with Neo4j inside an event loop. Use `await get_all_nodes_async()`.")
        except RuntimeError as e:
            # No running loop -> we can run it
            if "no running event loop" in str(e).lower():
                return asyncio.run(self.get_all_nodes_async(**kwargs))
            raise

    def _get_all_nodes_networkx(
        self,
        label: Optional[str] = None,
        limit: Optional[int] = None,
        tenant_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not self.nx_graph:
            return []
        tenant = self._resolve_tenant_for_query(tenant_id)
        nodes: List[Dict[str, Any]] = []
        for node_id, attrs in self.nx_graph.nodes(data=True):
            if label and attrs.get("_label") != label:
                continue
            if not self._tenant_networkx_pass(attrs, tenant):
                continue
            nodes.append({"id": node_id, **attrs})
            if limit and len(nodes) >= limit:
                break
        return nodes

    async def get_all_nodes_async(
        self,
        label: Optional[str] = None,
        limit: int = 5000,
        tenant_id: Optional[str] = None,
        strict_tenant: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Unified async read API for both backends.
        Returns nodes in a shape compatible with NetworkX nodes:
        { id, _label, ...properties }

        tenant_id (или текущий tenant_context) фильтрует выдачу —
        cross-tenant утечки недопустимы. См. tests/test_cross_tenant_isolation.

        strict_tenant=True: при заданном tenant возвращаем ТОЛЬКО его узлы, без
        legacy-NULL. Нужно для снапшотов компании/команды: узлы без tenant-штампа
        (созданные другим аккаунтом) иначе протекают («чужой человек в команде»).
        """
        tenant = self._resolve_tenant_for_query(tenant_id)
        if self.use_networkx:
            nodes = self._get_all_nodes_networkx(
                label=label, limit=limit, tenant_id=tenant)
            if strict_tenant and tenant:
                nodes = [n for n in nodes if n.get("tenant_id") == tenant]
            return nodes

        if not self.driver:
            return []

        try:
            if strict_tenant and tenant:
                tenant_clause = "n.tenant_id = $tenant"
            else:
                tenant_clause = self._tenant_cypher_clause("n")
            query = f"""
            MATCH (n)
            WHERE ($label IS NULL OR $label IN labels(n))
              AND {tenant_clause}
            RETURN n, labels(n) as labels
            LIMIT $limit
            """
            async with self.driver.session() as session:
                result = await session.run(
                    query,
                    {"label": label, "limit": limit, "tenant": tenant},
                )
                rows = await result.data()
                out: List[Dict[str, Any]] = []
                for row in rows:
                    node = row.get("n")
                    labels = row.get("labels") or []
                    props = dict(node) if node else {}
                    # Ensure stable id exists
                    node_id = props.get("id")
                    if not node_id:
                        # Fallback: not expected, but keep something usable
                        node_id = str(uuid.uuid4())
                        props["id"] = node_id
                    props["_label"] = labels[0] if labels else props.get("_label", "unknown")
                    out.append(props)
                return out
        except Exception as e:
            logger.error(f"❌ Failed to get_all_nodes_async from Neo4j: {e}")
            return []

    def _save_graph_to_file(self):
        """Сохранить граф в JSON файл"""
        if not self.nx_graph:
            return

        try:
            path = Path(self.graph_storage_path)
            path.parent.mkdir(parents=True, exist_ok=True)

            data = {
                "nodes": [],
                "edges": [],
                "metadata": {
                    "saved_at": datetime.now(timezone.utc).isoformat(),
                    "node_count": self.nx_graph.number_of_nodes(),
                    "edge_count": self.nx_graph.number_of_edges()
                }
            }

            # Сохраняем узлы
            for node_id, attrs in self.nx_graph.nodes(data=True):
                node_data = {"id": node_id, **attrs}
                data["nodes"].append(node_data)

            # Сохраняем связи
            for source, target, key, attrs in self.nx_graph.edges(keys=True, data=True):
                edge_data = {
                    "source": source,
                    "target": target,
                    "key": key,
                    "data": attrs
                }
                data["edges"].append(edge_data)

            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            logger.info(f"✅ Graph saved to {path}")

        except Exception as e:
            logger.error(f"❌ Failed to save graph: {e}")

    async def save_meeting_results(
        self,
        meeting_id: str,
        results: Dict[str, Any],
        meeting_metadata: Optional[Dict[str, Any]] = None,
        access_group: str = "public"
    ) -> Dict[str, Any]:
        """
        Сохранить результаты обработки встречи в граф.

        Args:
            meeting_id: ID встречи
            results: Результаты от CaptureOrchestrator
            meeting_metadata: Дополнительные метаданные встречи
            access_group: Группа доступа (RBAC)

        Returns:
            Статистика сохранения
        """
        if not self.connected:
            logger.warning("⚠️ Not connected to Neo4j")
            return {"error": "Not connected", "nodes_created": 0, "relationships_created": 0}

        self.stats = {"nodes_created": 0, "relationships_created": 0, "errors": 0}

        # issue #112 T-2: tenant_id для изоляции Person identity между
        # тенантами. Источники в порядке приоритета:
        #   1) явно в meeting_metadata["tenant_id"]
        #   2) observability tenant_context (если запрос пришёл через middleware)
        #   3) None — legacy fallback (warning в _create_participants)
        tenant_id = (meeting_metadata or {}).get("tenant_id")
        if not tenant_id:
            try:
                from backend.core.observability.tenant_context import get_current_tenant
                tenant_id = get_current_tenant()
            except Exception:
                tenant_id = None

        # issue #112 T-5: ground-truth attendees для validation assignee.
        # Источник — meeting_metadata['resolved_attendees'] (его наполняет
        # analyze.py через resolve_attendees) или 'attendees'/'participants'.
        # Если список пуст — validation no-op (не блокируем без ground truth).
        attendees = (
            (meeting_metadata or {}).get("resolved_attendees")
            or (meeting_metadata or {}).get("attendees")
            or []
        )

        # Active learning (issue #112 продолжение): learned aliases из
        # истории fix-исправлений. Если assignee не в attendees напрямую,
        # но человек раньше правил 'X → Y' и Y в attendees — резолвим через
        # алиас (medium confidence), без повторного review.
        learned_aliases: Dict[str, str] = {}
        try:
            from backend.core.store.corrections_store import get_corrections_store
            learned_aliases = get_corrections_store().get_learned_aliases(tenant_id)
        except Exception:
            learned_aliases = {}

        try:
            # 1. Создаём узел Meeting
            meeting_node_id = await self._create_meeting_node(meeting_id, results, meeting_metadata, access_group)

            if not meeting_node_id:
                return {"error": "Failed to create meeting node", **self.stats}

            # 2. Создаём узлы участников и связи (с tenant-aware MERGE)
            participant_ids = await self._create_participants(
                results.get("participants", []),
                meeting_node_id,
                access_group,
                tenant_id=tenant_id,
            )

            # 3. Создаём узлы решений и связи (с T-5 attendee validation)
            decision_ids = await self._create_decisions(
                results.get("decisions", []),
                meeting_node_id,
                participant_ids,
                access_group,
                attendees=attendees,
                learned_aliases=learned_aliases,
            )

            # 4. Создаём узлы задач и связи (с T-5 attendee validation)
            task_ids = await self._create_tasks(
                results.get("tasks", []),
                meeting_node_id,
                participant_ids,
                decision_ids,
                access_group,
                attendees=attendees,
                learned_aliases=learned_aliases,
            )

            # 5. Создаём узлы сущностей и связи
            entity_ids = await self._create_entities(
                results.get("entities", []),
                meeting_node_id,
                access_group
            )

            # 6. Создаём узлы KPI и связи
            kpi_ids = await self._create_kpis(
                results.get("kpis", []),
                meeting_node_id,
                access_group
            )

            # 7. Создаём cross-entity связи
            await self._create_cross_references(results, {
                "meeting": meeting_node_id,
                "participants": participant_ids,
                "decisions": decision_ids,
                "tasks": task_ids,
                "entities": entity_ids,
                "kpis": kpi_ids
            })

            logger.info(
                f"✅ Meeting {meeting_id} saved to graph: "
                f"{self.stats['nodes_created']} nodes, "
                f"{self.stats['relationships_created']} relationships"
            )

            return {
                "meeting_id": meeting_id,
                "meeting_node_id": meeting_node_id,
                **self.stats
            }

        except Exception as e:
            logger.error(f"❌ Error saving meeting to graph: {e}")
            self.stats["errors"] += 1
            return {"error": str(e), **self.stats}

    async def _create_meeting_node(
        self,
        meeting_id: str,
        results: Dict[str, Any],
        metadata: Optional[Dict[str, Any]],
        access_group: str
    ) -> Optional[str]:
        """Создать узел Meeting"""
        summary = results.get("summary", {})

        props = {
            "meeting_id": meeting_id,
            "title": metadata.get("title", "") if metadata else "",
            "date": metadata.get("date", "") if metadata else "",
            "project_id": metadata.get("project_id", "") if metadata else "",
            "processing_timestamp": results.get("processing_timestamp", ""),
            "total_entities": summary.get("total_entities", 0),
            "decisions_count": summary.get("decisions_count", 0),
            "tasks_count": summary.get("tasks_count", 0),
            "participants_count": summary.get("participants_count", 0),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "access_group": access_group # NEW
        }

        return await self._merge_node("Meeting", "meeting_id", meeting_id, props)

    async def _create_participants(
        self,
        participants: List[Dict[str, Any]],
        meeting_node_id: str,
        access_group: str,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, str]:
        """Создать узлы участников и связи с встречей.

        issue #112 T-2: email-first identity (защита от 'два Максима →
        один node'). tenant_id передаётся для изоляции — Максим из
        тенанта A и Максим из тенанта B теперь РАЗНЫЕ ноды.

        Стратегия MERGE для Person:
          1. Если participant имеет email → MERGE по (tenant_id, lower(email))
             Это самый надёжный сигнал identity.
          2. Иначе MERGE по (tenant_id, normalized_name)
             С collision-detection: если найден существующий Person с
             тем же именем но РАЗНОЙ ролью/department — это сигнал
             что это разные люди. Создаём новый node с suffix
             '<name>#2', '<name>#3', и т.д. + warning в логи + флаг
             needs_review для админа.

        Backward-compat: если tenant_id=None — поведение как раньше
        (legacy путь), но в логи WARNING — это значит что вызов не
        прошёл через tenant-aware orchestrator (нужно поправить).
        """
        participant_ids = {}

        if tenant_id is None:
            logger.warning(
                "graph_builder._create_participants: tenant_id is None — "
                "Person MERGE collisions are possible. См. issue #112 T-2."
            )

        for p in participants:
            name = p.get("name", "")
            if not name:
                continue

            # Junk-guard НА ИНГЕСТЕ: не создаём Person-узлы из составных
            # («Александр и Екатерина»), generic-меток («Все участники»),
            # дикторов («Speaker 1») и тайм-фрагментов («Катя 1:11:57») —
            # раньше они плодились в графе, а view-фильтры лишь прятали их.
            try:
                from backend.core.sleep.enhanced_snapshot import _is_person_junk
                if _is_person_junk(name):
                    logger.debug(f"participant skipped as junk: {name!r}")
                    continue
            except ImportError:
                pass

            email = (p.get("email") or "").strip().lower() or None
            normalized_name = p.get("normalized_name") or name

            props = {
                "name": name,
                "normalized_name": normalized_name,
                "role": p.get("role", ""),
                "department": p.get("department", ""),
                "activity_level": p.get("activity_level", "medium"),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "access_group": "public" if access_group == "public" else access_group,
                "tenant_id": tenant_id or "",
            }
            if email:
                props["email"] = email

            # issue #112 T-2: email-first идентификация
            additional_match: Dict[str, Any] = {}
            if tenant_id:
                additional_match["tenant_id"] = tenant_id

            if email:
                # Самый надёжный путь: MERGE по (tenant_id, email).
                # Никакой collision-detection не нужен — email уникален.
                node_id = await self._merge_node(
                    "Person", "email", email, props,
                    additional_match=additional_match,
                )
            else:
                # Fallback: MERGE по (tenant_id, normalized_name) с
                # collision detection.
                collision = await self._detect_person_collision(
                    normalized_name=normalized_name,
                    incoming_role=p.get("role", ""),
                    incoming_department=p.get("department", ""),
                    tenant_id=tenant_id,
                )
                if collision:
                    # Существующий Person с тем же именем но разной ролью —
                    # высокая вероятность что это РАЗНЫЙ человек. Создаём
                    # отдельный node с suffix.
                    suffix = collision.get("next_suffix", 2)
                    disambig_name = f"{normalized_name}#{suffix}"
                    props["normalized_name"] = disambig_name
                    props["disambiguation_suffix"] = suffix
                    props["needs_review"] = True
                    props["review_reason"] = (
                        f"name collision with existing Person '{normalized_name}' "
                        f"(role: {collision.get('existing_role', '?')} vs "
                        f"{p.get('role', '?')})"
                    )
                    logger.warning(
                        "Person identity collision: tenant=%s name=%r "
                        "(existing role=%r, incoming role=%r) → new node '%s'. "
                        "issue #112 T-2",
                        tenant_id, normalized_name,
                        collision.get("existing_role"),
                        p.get("role"), disambig_name,
                    )
                    node_id = await self._merge_node(
                        "Person", "normalized_name", disambig_name, props,
                        additional_match=additional_match,
                    )
                else:
                    node_id = await self._merge_node(
                        "Person", "normalized_name", normalized_name, props,
                        additional_match=additional_match,
                    )

            if node_id:
                participant_ids[p.get("participant_id", name)] = node_id

                # Создаём связь PARTICIPATED_IN
                await self._create_relationship(
                    node_id, meeting_node_id,
                    "PARTICIPATED_IN",
                    {
                        "role": p.get("role", ""),
                        "activity_level": p.get("activity_level", "medium"),
                        "sentiment": p.get("sentiment", "neutral")
                    }
                )

        return participant_ids

    def _enqueue_correction(
        self,
        *,
        entity_type: str,
        entity_id: str,
        original_attribution: Optional[str],
        original_confidence: str,
        review_reason: str,
        title: str,
    ) -> None:
        """issue #112 T-8: положить low-confidence attribution в review
        queue. Best-effort — не ломает создание узла если store недоступен.

        tenant_id берём из tenant_context (тот же источник, что для Person
        MERGE в T-2)."""
        try:
            tenant_id = None
            try:
                from backend.core.observability.tenant_context import get_current_tenant
                tenant_id = get_current_tenant()
            except Exception:
                tenant_id = None
            from backend.core.store.corrections_store import get_corrections_store
            get_corrections_store().enqueue(
                entity_type=entity_type,
                entity_id=entity_id,
                tenant_id=tenant_id,
                original_attribution=original_attribution,
                original_confidence=original_confidence,
                review_reason=review_reason,
                title=title,
            )
        except Exception as e:
            logger.debug("enqueue_correction skipped (non-fatal): %s", e)

    @staticmethod
    def _resolve_tenant_for_query(explicit: Optional[str]) -> Optional[str]:
        """issue #112 RLS hotfix (wave 2): единая точка получения tenant_id
        для read-запросов в Neo4j. Приоритет:
          1) явный параметр от caller (он знает контекст)
          2) observability.tenant_context (через request middleware)
          3) None — fallback: caller получит только legacy (tenant_id IS NULL),
             но никогда чужие данные. Это safe-by-default.
        """
        if explicit:
            return explicit
        try:
            from backend.core.observability.tenant_context import (
                get_current_tenant,
            )
            return get_current_tenant()
        except Exception:
            return None

    @staticmethod
    def _tenant_cypher_clause(alias: str = "n", param: str = "tenant") -> str:
        """Возвращает Cypher-фрагмент tenant-фильтра для read-запроса.

        Используется паттерн: `(n.tenant_id IS NULL OR ($tenant IS NOT NULL
        AND n.tenant_id = $tenant))`. То есть:
          - tenant_id=None → видны только legacy узлы (без tenant)
          - tenant_id=X → видны legacy + узлы тенанта X
          - Никогда не пускаем cross-tenant.

        alias — имя ноды/ребра в MATCH (например 'n', 'a', 'r').
        param — имя $-параметра в запросе.

        Гигиена мульти-аккаунта: `TENANT_STRICT_DEFAULT=on` убирает ветку
        NULL-легаси и из ДЕФОЛТНЫХ чтений (остаточная течь: узлы без
        tenant_id были видны всем). По умолчанию ВЫКЛ — старые инсталляции
        с легаси-данными не теряют их молча; включать после бэкфилла
        tenant_id.
        """
        if _strict_tenant_default():
            return f"(${param} IS NOT NULL AND {alias}.tenant_id = ${param})"
        return (f"({alias}.tenant_id IS NULL "
                f"OR (${param} IS NOT NULL AND {alias}.tenant_id = ${param}))")

    @staticmethod
    def _tenant_networkx_pass(node_or_edge_data: Dict[str, Any],
                              tenant_id: Optional[str]) -> bool:
        """NetworkX вариант _tenant_cypher_clause: True если запись
        принадлежит этому тенанту или legacy (NULL)."""
        t = node_or_edge_data.get("tenant_id")
        if t in (None, ""):
            # legacy: видна, если не включён строгий дефолт (см. clause выше)
            return not _strict_tenant_default()
        if tenant_id is None:
            return False  # без контекста — только legacy
        return t == tenant_id

    @staticmethod
    def _validate_attendance(
        name: str,
        email: Optional[str],
        attendees: Optional[List[Dict[str, Any]]],
        learned_aliases: Optional[Dict[str, str]],
    ) -> tuple:
        """issue #112 T-5 + active learning: проверить, был ли name среди
        attendees, с учётом learned aliases.

        Возвращает (in_attendees: bool, resolved_via_alias: Optional[str]):
          - (True, None): name прямо в attendees → OK
          - (True, 'Максим Иванов'): name не в attendees, но learned alias
            'name → Максим Иванов', и Максим в attendees → OK через alias
          - (False, None): нет ни прямого матча, ни alias → review

        Если attendees пуст — (True, None) (нет ground truth, не блокируем).
        """
        from backend.core.transcripts import is_assignee_in_attendees

        if not attendees:
            return (True, None)

        # 1. Прямой матч
        if is_assignee_in_attendees(name, email, attendees):
            return (True, None)

        # 2. Active learning: проверим learned alias
        if learned_aliases and name:
            alias_target = learned_aliases.get(name.strip().lower())
            if alias_target and is_assignee_in_attendees(
                    alias_target, None, attendees):
                return (True, alias_target)

        return (False, None)

    @staticmethod
    def _resolve_assignee_to_participant(
        assignee_name: str,
        participant_ids: Dict[str, str],
    ) -> Optional[tuple]:
        """Найти participant_id для assignee_name (issue #112 T-3).

        ДО правки: 'if assignee_name.lower() in pid.lower()' — substring
        match, который ломался на 'max' ⊂ 'maxwell' и 'иван' ⊂ 'петр иванов'.

        Стратегия (consrervative, безопасная):
          1. **Exact match** (case-insensitive normalized): 'Иван' ==
             'иван' ✓, но 'Иван' ≠ 'Иван Петров' (требуем точного
             совпадения по всему имени).
          2. **First-name + last-name exact**: 'Иван Петров' ↔ 'Петров Иван'
             совпадают (token set check).
          3. **Edit distance ≤ 2** на нормализованных формах — только
             если incoming имя длиннее 4 символов (защита от 'Ян' ≈ 'Ян2').
             Возвращает confidence='medium'.

        Возвращает:
            None — если матча нет (caller НЕ создаёт ребро, лог-warning)
            (participant_id, confidence: 'high'|'medium') — если матч найден
        """
        if not assignee_name or not assignee_name.strip():
            return None

        target = assignee_name.strip().lower()
        target_tokens = set(target.split())

        # 1. Exact match по полному имени (lower)
        for pid in participant_ids.keys():
            if pid.lower() == target:
                return (participant_ids[pid], "high")

        # 2. Token-set match: 'Иван Петров' ↔ 'Петров Иван'
        # Только если есть >=2 токенов (защита от 'Иван' ↔ 'Иван Петров')
        if len(target_tokens) >= 2:
            for pid in participant_ids.keys():
                pid_tokens = set(pid.lower().split())
                if len(pid_tokens) >= 2 and pid_tokens == target_tokens:
                    return (participant_ids[pid], "high")

        # 3. Edit distance fallback (medium confidence)
        # Только если имя достаточно длинное, чтобы не путать 'max' с 'maxs'
        if len(target) >= 4:
            for pid in participant_ids.keys():
                pid_lower = pid.lower()
                if abs(len(pid_lower) - len(target)) > 2:
                    continue
                d = GraphBuilder._levenshtein(pid_lower, target)
                if d <= 2 and d * 4 <= len(target):
                    # д ≤ 2 И составляет не более 25% длины — безопасно
                    return (participant_ids[pid], "medium")

        return None

    @staticmethod
    def _levenshtein(a: str, b: str) -> int:
        """Простой O(n*m) Levenshtein distance. Достаточно для коротких
        имён (~ 30 символов max). Без внешних deps."""
        if a == b:
            return 0
        if not a:
            return len(b)
        if not b:
            return len(a)
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a, 1):
            curr = [i] + [0] * len(b)
            for j, cb in enumerate(b, 1):
                cost = 0 if ca == cb else 1
                curr[j] = min(
                    curr[j - 1] + 1,         # insert
                    prev[j] + 1,             # delete
                    prev[j - 1] + cost,      # substitute
                )
            prev = curr
        return prev[-1]

    async def _detect_person_collision(
        self,
        *,
        normalized_name: str,
        incoming_role: str,
        incoming_department: str,
        tenant_id: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """Обнаружить ситуацию 'тот же name, но разная роль/отдел'.

        Возвращает {existing_role, existing_department, next_suffix}
        если найдена коллизия, иначе None.

        Используем для disambiguation: 'Максим (Sales)' vs 'Максим (Dev)'
        не должны сливаться в один Person node.

        Логика:
          - Ищем существующих Person с тем же normalized_name + tenant_id
          - Если найден И его role/department отличается от incoming —
            это коллизия
          - Если incoming поля пусты (LLM не определил) — НЕ коллизия
            (можем смело merge'ить)
        """
        # Если incoming role и department пусты — нет основания считать
        # это разным человеком. Merge'им как обычно.
        if not incoming_role.strip() and not incoming_department.strip():
            return None

        try:
            if self.use_networkx:
                return self._detect_person_collision_networkx(
                    normalized_name, incoming_role, incoming_department, tenant_id)
            return await self._detect_person_collision_neo4j(
                normalized_name, incoming_role, incoming_department, tenant_id)
        except Exception as e:
            logger.warning("collision detection failed (non-fatal): %s", e)
            return None

    def _detect_person_collision_networkx(
        self, normalized_name: str, incoming_role: str,
        incoming_department: str, tenant_id: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """NetworkX вариант collision detection."""
        existing_with_name = []
        for nid, data in self.nx_graph.nodes(data=True):
            if data.get("_label") != "Person":
                continue
            base_name = (data.get("normalized_name") or "").split("#")[0]
            if base_name != normalized_name:
                continue
            if tenant_id and data.get("tenant_id") != tenant_id:
                continue
            existing_with_name.append(data)

        if not existing_with_name:
            return None

        # Ищем хотя бы один существующий Person с расхождением role/department
        for existing in existing_with_name:
            existing_role = (existing.get("role") or "").strip()
            existing_dept = (existing.get("department") or "").strip()
            role_conflict = (existing_role and incoming_role
                             and existing_role.lower() != incoming_role.lower())
            dept_conflict = (existing_dept and incoming_department
                             and existing_dept.lower() != incoming_department.lower())
            if role_conflict or dept_conflict:
                # Найдём максимальный suffix → next_suffix = max+1
                max_suffix = 1
                for n in existing_with_name:
                    s = n.get("disambiguation_suffix")
                    if isinstance(s, int) and s > max_suffix:
                        max_suffix = s
                return {
                    "existing_role": existing_role,
                    "existing_department": existing_dept,
                    "next_suffix": max_suffix + 1,
                }
        return None

    async def _detect_person_collision_neo4j(
        self, normalized_name: str, incoming_role: str,
        incoming_department: str, tenant_id: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """Neo4j вариант collision detection."""
        # Ищем Person с тем же base-именем (без #N suffix) в этом тенанте
        query = """
        MATCH (n:Person)
        WHERE (n.normalized_name = $name OR n.normalized_name STARTS WITH $name + '#')
          AND ($tenant IS NULL OR n.tenant_id = $tenant)
        RETURN n.role AS role, n.department AS department,
               n.disambiguation_suffix AS suffix
        """
        params = {"name": normalized_name, "tenant": tenant_id}
        try:
            async with self.driver.session() as session:
                result = await session.run(query, params)
                rows = [dict(r) async for r in result]
        except Exception:
            return None
        if not rows:
            return None
        max_suffix = 1
        collision_row = None
        for r in rows:
            existing_role = (r.get("role") or "").strip()
            existing_dept = (r.get("department") or "").strip()
            role_conflict = (existing_role and incoming_role
                             and existing_role.lower() != incoming_role.lower())
            dept_conflict = (existing_dept and incoming_department
                             and existing_dept.lower() != incoming_department.lower())
            if role_conflict or dept_conflict:
                collision_row = r
            s = r.get("suffix")
            if isinstance(s, int) and s > max_suffix:
                max_suffix = s
        if collision_row is None:
            return None
        return {
            "existing_role": (collision_row.get("role") or "").strip(),
            "existing_department": (collision_row.get("department") or "").strip(),
            "next_suffix": max_suffix + 1,
        }

    async def _create_decisions(
        self,
        decisions: List[Dict[str, Any]],
        meeting_node_id: str,
        participant_ids: Dict[str, str],
        access_group: str,
        attendees: Optional[List[Dict[str, Any]]] = None,
        learned_aliases: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        """Создать узлы решений.

        issue #112 T-5: attendees — ground truth «кто был на встрече». Если
        decision_maker не входит в attendees → понижаем confidence + флаг
        review (нельзя приписать решение тому, кого не было).
        learned_aliases (active learning): если maker не в attendees, но
        раньше правился 'X → Y' и Y в attendees — резолвим через alias."""
        decision_ids = {}

        for d in decisions:
            decision_id = d.get("decision_id", "")
            if not decision_id:
                continue

            # issue #112 T-7: attribution полей, выставленных decision_extractor
            # (T-1.b). Если LLM их не выставил — defaults безопасные (low/null),
            # чтобы UI мог показывать «требует проверки» и не считать legacy
            # legacy записи 100%-уверенными.
            attr_speaker = d.get("decision_maker_speaker")
            attr_resolved = d.get("decision_maker_resolved")
            attr_conf = (d.get("attribution_confidence") or "low").lower().strip()
            if attr_conf not in ("low", "medium", "high"):
                attr_conf = "low"
            # System-side guarantee: если LLM не определил maker — обязательно low
            if not attr_speaker and not attr_resolved:
                attr_conf = "low"

            # issue #112 T-5 + active learning: validation decision_maker ∈
            # attendees. Если resolved-имя есть, но человека не было на встрече
            # → проверяем learned alias, иначе downgrade + флаг.
            review_reason = ""
            if attr_resolved and attendees:
                in_att, via_alias = self._validate_attendance(
                    attr_resolved, None, attendees, learned_aliases)
                if not in_att:
                    attr_conf = "low"
                    review_reason = (
                        f"decision_maker '{attr_resolved}' not in meeting "
                        f"attendees")
                    logger.warning(
                        "Decision T-5: maker=%r not in attendees → "
                        "confidence=low + review. decision_id=%s",
                        attr_resolved, decision_id,
                    )
                elif via_alias:
                    # Резолвили через learned alias — обновляем resolved-имя,
                    # confidence не ниже medium (история подтверждает маппинг)
                    attr_resolved = via_alias
                    if attr_conf == "low":
                        attr_conf = "medium"
                    logger.info(
                        "Decision active-learning: maker resolved via alias "
                        "→ %r (decision_id=%s)", via_alias, decision_id)

            props = {
                "decision_id": decision_id,
                "summary": d.get("decision_summary", ""),
                "category": d.get("decision_category", "operational"),
                "details": d.get("details", ""),
                "reasoning": d.get("reasoning", ""),
                "urgency": d.get("business_impact", {}).get("urgency", "medium"),
                "risk_level": d.get("business_impact", {}).get("risk_level", "low"),
                "confidence": d.get("confidence", 0.8),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "access_group": access_group, # NEW
                # T-7: атрибуционные поля
                "decision_maker_speaker": attr_speaker or "",
                "decision_maker_resolved": attr_resolved or "",
                "attribution_confidence": attr_conf,
                "needs_human_review": (attr_conf == "low"),
                # T-5: причина review (если decision_maker не в attendees)
                "review_reason": review_reason,
            }

            node_id = await self._merge_node("Decision", "decision_id", decision_id, props)

            if node_id:
                decision_ids[decision_id] = node_id

                # issue #112 T-8: если решение требует review — в очередь
                if props["needs_human_review"]:
                    self._enqueue_correction(
                        entity_type="decision", entity_id=decision_id,
                        original_attribution=attr_resolved or attr_speaker,
                        original_confidence=attr_conf,
                        review_reason=review_reason or "low attribution confidence",
                        title=props["summary"][:120],
                    )

                # Связь с встречей
                await self._create_relationship(
                    node_id, meeting_node_id,
                    "CREATED_FROM",
                    {"type": "decision"}
                )

                # Связи с ответственными
                responsible = d.get("responsible", {})
                primary = responsible.get("primary", "")

                if primary:
                    # issue #112 T-3: exact + token-set + edit-distance
                    # вместо опасного substring match (где 'max' ⊂ 'maxwell').
                    match = self._resolve_assignee_to_participant(
                        primary, participant_ids)
                    if match is not None:
                        pnode_id, attr_confidence = match
                        await self._create_relationship(
                            pnode_id, node_id,
                            "MADE_DECISION",
                            {
                                "role": "primary",
                                "attribution_confidence": attr_confidence,
                            }
                        )
                    else:
                        logger.warning(
                            "Decision attribution skip: primary=%r not "
                            "found in participants %s. issue #112 T-3 — "
                            "не создаём ребро молча.",
                            primary, list(participant_ids.keys()),
                        )

        return decision_ids

    async def _reconcile_completed_task(
        self,
        *,
        title: str,
        assignee_name: str,
        participant_ids: Dict[str, str],
        exclude_task_id: str,
        meeting_node_id: str,
    ) -> Optional[str]:
        """Межвстречный трекинг завершения задач.

        Когда текущая встреча помечает задачу как done/completed, ищем СРЕДИ
        уже существующих ОТКРЫТЫХ задач того же исполнителя ту, чьё название
        похоже, и помечаем ЕЁ done — вместо того чтобы плодить новый «done»-узел
        (из-за чего у всех было 0 выполненных: задачи создавались как new и
        статус никогда не менялся).

        Скоуп поиска — задачи, привязанные к участнику-исполнителю (ASSIGNED_TO),
        поэтому tenant-safe и дёшево. Возвращает node_id обновлённой задачи или
        None (тогда вызывающий создаёт узел как обычно). Best-effort: любые
        ошибки — не критичны для пайплайна.
        """
        from difflib import SequenceMatcher

        if not (title or "").strip() or not (assignee_name or "").strip():
            return None
        match = self._resolve_assignee_to_participant(assignee_name, participant_ids)
        if match is None:
            return None
        pnode_id, _conf = match

        rels = await self.get_node_relationships(pnode_id)
        t_norm = title.strip().lower()
        best_id, best_score = None, 0.0
        _DONE = ("done", "completed", "выполнено")

        for rel in rels.get("incoming", []):
            if (rel.get("type") or "").upper() != "ASSIGNED_TO":
                continue
            task_node_id = rel.get("source")
            if not task_node_id:
                continue
            node = await self.get_node_by_id(task_node_id)
            if not node:
                continue
            if node.get("task_id") == exclude_task_id:
                continue
            if str(node.get("status", "")).lower() in _DONE:
                continue
            cand_title = str(node.get("title", "")).strip().lower()
            if not cand_title:
                continue
            score = SequenceMatcher(None, t_norm, cand_title).ratio()
            if score > best_score:
                best_score, best_id = score, task_node_id

        if best_id and best_score >= 0.7:
            await self.update_node(best_id, {
                "status": "done",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "completed_via_meeting": meeting_node_id,
            })
            logger.info(
                "Task cross-meeting completion: %r → done (match %.2f, node=%s)",
                title[:60], best_score, best_id,
            )
            return best_id
        return None

    async def _create_tasks(
        self,
        tasks: List[Dict[str, Any]],
        meeting_node_id: str,
        participant_ids: Dict[str, str],
        decision_ids: Dict[str, str],
        access_group: str,
        attendees: Optional[List[Dict[str, Any]]] = None,
        learned_aliases: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        """Создать узлы задач.

        issue #112 T-5: attendees — ground truth. Если assignee не входит в
        attendees → понижаем confidence + review (нельзя назначить задачу
        тому, кого не было на встрече).
        learned_aliases (active learning): если assignee не в attendees, но
        раньше правился 'X → Y' и Y в attendees — резолвим через alias."""
        task_ids = {}

        for t in tasks:
            task_id = t.get("task_id", "")
            if not task_id:
                continue

            deadline = t.get("deadline", {})

            # issue #112 T-7: атрибуция из task_agent (T-1.c).
            # assignee_speaker — Speaker N из диаризации (если был)
            # assignment_confidence — low|medium|high
            # needs_human_review — bool, выставляется extractор'ом или нашей
            # защитой от self-overconfidence ниже
            assignee = t.get("assignee") or {}
            assignee_name = (
                assignee.get("name", "") if isinstance(assignee, dict)
                else str(assignee)
            )
            assignee_email = (
                assignee.get("email") if isinstance(assignee, dict) else None
            )
            assign_speaker = t.get("assignee_speaker")
            assign_conf = (t.get("assignment_confidence") or "low").lower().strip()
            if assign_conf not in ("low", "medium", "high"):
                assign_conf = "low"
            needs_review = bool(t.get("needs_human_review", False))
            # System-side: если нет ни speaker, ни name — обязательно low+review
            if not assign_speaker and not assignee_name:
                assign_conf = "low"
                needs_review = True

            # issue #112 T-5 + active learning: validation assignee ∈
            # attendees. Если assignee не в attendees напрямую, проверяем
            # learned alias; иначе понижаем confidence + флаг review.
            task_review_reason = ""
            if assignee_name and attendees:
                in_att, via_alias = self._validate_attendance(
                    assignee_name, assignee_email, attendees, learned_aliases)
                if not in_att:
                    assign_conf = "low"
                    needs_review = True
                    task_review_reason = (
                        f"assignee '{assignee_name}' not in meeting attendees")
                    logger.warning(
                        "Task T-5: assignee=%r not in attendees → "
                        "confidence=low + review. task_id=%s",
                        assignee_name, task_id,
                    )
                elif via_alias:
                    # Резолвили через learned alias — обновляем имя, confidence
                    # не ниже medium (история подтверждает маппинг)
                    assignee_name = via_alias
                    if assign_conf == "low":
                        assign_conf = "medium"
                    logger.info(
                        "Task active-learning: assignee resolved via alias "
                        "→ %r (task_id=%s)", via_alias, task_id)

            props = {
                "task_id": task_id,
                "title": t.get("title", ""),
                "description": t.get("description", ""),
                "task_type": t.get("task_type", "action"),
                "priority": t.get("priority", "medium"),
                "status": t.get("status", "new"),
                "deadline": deadline.get("date", "") if isinstance(deadline, dict) else str(deadline),
                "confidence": t.get("confidence", 0.8),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "access_group": access_group, # NEW
                # T-7: атрибуционные поля
                "assignee_name": assignee_name or "",
                "assignee_speaker": assign_speaker or "",
                "assignment_confidence": assign_conf,
                "needs_human_review": needs_review,
                # T-5: причина review (если assignee не в attendees)
                "review_reason": task_review_reason,
            }

            # Межвстречный трекинг: если задача завершена — попробуем пометить
            # done уже существующую открытую задачу того же исполнителя, а не
            # создавать дубль. Иначе создаём/мёрджим узел как обычно.
            reconciled_id = None
            if str(props["status"]).lower() in ("done", "completed", "выполнено"):
                try:
                    reconciled_id = await self._reconcile_completed_task(
                        title=props["title"],
                        assignee_name=assignee_name,
                        participant_ids=participant_ids,
                        exclude_task_id=task_id,
                        meeting_node_id=meeting_node_id,
                    )
                except Exception:
                    logger.debug("cross-meeting task reconcile failed", exc_info=True)

            if reconciled_id:
                node_id = reconciled_id
            else:
                node_id = await self._merge_node("Task", "task_id", task_id, props)

            if node_id:
                task_ids[task_id] = node_id

                # issue #112 T-8: если задача требует review — в очередь
                if props["needs_human_review"]:
                    self._enqueue_correction(
                        entity_type="task", entity_id=task_id,
                        original_attribution=assignee_name or assign_speaker,
                        original_confidence=assign_conf,
                        review_reason=task_review_reason or "low assignment confidence",
                        title=props["title"][:120],
                    )

                # Связь с встречей
                await self._create_relationship(
                    node_id, meeting_node_id,
                    "CREATED_FROM",
                    {"type": "task"}
                )

                # Связь с исполнителем
                assignee = t.get("assignee", {})
                assignee_name = assignee.get("name", "") if isinstance(assignee, dict) else str(assignee)

                if assignee_name:
                    # issue #112 T-3: exact + token-set + edit-distance
                    # вместо substring match ('max' ⊂ 'maxwell smart').
                    match = self._resolve_assignee_to_participant(
                        assignee_name, participant_ids)
                    if match is not None:
                        pnode_id, attr_confidence = match
                        await self._create_relationship(
                            node_id, pnode_id,
                            "ASSIGNED_TO",
                            {
                                "role": assignee.get("role", "") if isinstance(assignee, dict) else "",
                                "attribution_confidence": attr_confidence,
                            }
                        )
                    else:
                        logger.warning(
                            "Task attribution skip: assignee=%r not found "
                            "in participants %s. issue #112 T-3 — не "
                            "создаём silent ребро.",
                            assignee_name, list(participant_ids.keys()),
                        )

                # Связь с решением
                related_decision = t.get("related_decision", "")
                if related_decision and related_decision in decision_ids:
                    await self._create_relationship(
                        node_id, decision_ids[related_decision],
                        "IMPLEMENTS",
                        {}
                    )

        return task_ids

    async def _create_entities(
        self,
        entities: List[Dict[str, Any]],
        meeting_node_id: str,
        access_group: str
    ) -> Dict[str, str]:
        """Создать узлы сущностей (проекты, организации, технологии и т.д.)"""
        entity_ids = {}

        for e in entities:
            entity_id = e.get("entity_id", "")
            name = e.get("name", "")

            if not entity_id or not name:
                continue

            entity_type = e.get("entity_type", "idea")

            props = {
                "entity_id": entity_id,
                "name": name,
                "entity_type": entity_type,
                "description": e.get("description", ""),
                "importance": e.get("importance", "medium"),
                "confidence": e.get("confidence", 0.8),
                "attributes": str(e.get("attributes", {})),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "access_group": access_group # NEW
            }

            # Используем составной ключ: тип + имя
            node_id = await self._merge_node(
                "Entity",
                "name",
                name,
                props,
                additional_match={"entity_type": entity_type}
            )

            if node_id:
                entity_ids[entity_id] = node_id

                # Связь с встречей
                await self._create_relationship(
                    node_id, meeting_node_id,
                    "MENTIONED_IN",
                    {"entity_type": entity_type, "importance": e.get("importance", "medium")}
                )

        return entity_ids

    async def _create_kpis(
        self,
        kpis: List[Dict[str, Any]],
        meeting_node_id: str,
        access_group: str
    ) -> Dict[str, str]:
        """Создать узлы KPI"""
        kpi_ids = {}

        for k in kpis:
            kpi_id = k.get("kpi_id", "")
            name = k.get("name", "")

            if not kpi_id or not name:
                continue

            value = k.get("value", {})
            trend = k.get("trend", {})

            props = {
                "kpi_id": kpi_id,
                "name": name,
                "category": k.get("category", "operational"),
                "current_value": value.get("current") if isinstance(value, dict) else value,
                "previous_value": value.get("previous") if isinstance(value, dict) else None,
                "target_value": value.get("target") if isinstance(value, dict) else None,
                "unit": value.get("unit", "") if isinstance(value, dict) else "",
                "trend_direction": trend.get("direction", "stable") if isinstance(trend, dict) else "",
                "change_percent": trend.get("change_percent") if isinstance(trend, dict) else None,
                "confidence": k.get("confidence", 0.8),
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "access_group": access_group # NEW
            }

            node_id = await self._merge_node("KPI", "name", name, props)

            if node_id:
                kpi_ids[kpi_id] = node_id

                # Связь с встречей
                await self._create_relationship(
                    node_id, meeting_node_id,
                    "REPORTED_IN",
                    {"category": k.get("category", "operational")}
                )

        return kpi_ids

    async def _create_cross_references(
        self,
        results: Dict[str, Any],
        node_ids: Dict[str, Any]
    ):
        """Создать перекрёстные связи между сущностями"""
        cross_insights = results.get("cross_insights", {})

        # Связи решений с задачами
        for link in cross_insights.get("decision_task_links", []):
            decision_id = link.get("decision_id")
            task_id = link.get("task_id")

            if decision_id and task_id:
                decision_node = node_ids.get("decisions", {}).get(decision_id)
                task_node = node_ids.get("tasks", {}).get(task_id)

                if decision_node and task_node:
                    await self._create_relationship(
                        task_node, decision_node,
                        "IMPLEMENTS",
                        {"link_type": link.get("link_type", "creates")}
                    )

        # Связи участников с задачами
        for resp in cross_insights.get("participant_responsibilities", []):
            participant_id = resp.get("participant_id")
            task_ids_list = resp.get("task_ids", [])

            participant_node = node_ids.get("participants", {}).get(participant_id)
            if participant_node:
                for tid in task_ids_list:
                    task_node = node_ids.get("tasks", {}).get(tid)
                    if task_node:
                        await self._create_relationship(
                            task_node, participant_node,
                            "ASSIGNED_TO",
                            {"source": "cross_analysis"}
                        )

    async def _merge_node(
        self,
        label: str,
        key_field: str,
        key_value: Any,
        properties: Dict[str, Any],
        additional_match: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """
        MERGE узла (создать если не существует, обновить если существует).

        Args:
            label: Метка узла
            key_field: Поле для поиска
            key_value: Значение ключа
            properties: Свойства узла
            additional_match: Дополнительные условия для MATCH
        """
        if self.use_networkx:
            return self._merge_node_networkx(label, key_field, key_value, properties, additional_match)
        else:
            return await self._merge_node_neo4j(label, key_field, key_value, properties, additional_match)

    def _merge_node_networkx(
        self,
        label: str,
        key_field: str,
        key_value: Any,
        properties: Dict[str, Any],
        additional_match: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """MERGE узла в NetworkX"""
        # VALIDATION
        validator = get_schema_validator()
        if not validator.validate_node(label, properties):
            return None

        try:
            # Создаём ключ для индекса
            index_key = (label, str(key_value))
            if additional_match:
                # Добавляем дополнительные условия в ключ
                extra = "_".join(f"{k}={v}" for k, v in sorted(additional_match.items()))
                index_key = (label, f"{key_value}_{extra}")

            # Проверяем существует ли узел
            if index_key in self._node_index:
                node_id = self._node_index[index_key]
                # Обновляем свойства
                self.nx_graph.nodes[node_id].update(properties)
            else:
                # Создаём новый узел
                node_id = f"{label}_{uuid.uuid4().hex[:8]}"

                # Добавляем метаданные
                node_props = {
                    **properties,
                    "_label": label,
                    "_key": str(key_value),
                    "_created_at": datetime.now(timezone.utc).isoformat()
                }

                # НОВОЕ: Добавляем поля для весов если флаг включён
                try:
                    from backend.core.config import flags
                    if flags.enable_graph_weights:
                        node_props.setdefault("weight", 1.0)
                        node_props.setdefault("access_count", 0)
                        node_props.setdefault("last_accessed", None)

                    if flags.enable_frequency_tracking:
                        node_props.setdefault("total_mentions", 0)
                        node_props.setdefault("frequency_score", 0.0)
                        node_props.setdefault("last_mentioned_in", None)
                        node_props.setdefault("last_mentioned_at", None)

                    if flags.enable_emotional_salience:
                        node_props.setdefault("emotional_salience", 0.0)
                except ImportError:
                    pass  # Флаги ещё не настроены

                self.nx_graph.add_node(node_id, **node_props)
                self._node_index[index_key] = node_id
                self.stats["nodes_created"] += 1

            return node_id

        except Exception as e:
            logger.error(f"❌ Error creating {label} node in NetworkX: {e}")
            self.stats["errors"] += 1
            return None

    async def _merge_node_neo4j(
        self,
        label: str,
        key_field: str,
        key_value: Any,
        properties: Dict[str, Any],
        additional_match: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """MERGE узла в Neo4j"""
        # VALIDATION
        validator = get_schema_validator()
        if not validator.validate_node(label, properties):
            return None

        try:
            # Строим условие MATCH
            match_props = {key_field: key_value}
            if additional_match:
                match_props.update(additional_match)

            match_str = ", ".join([f"{k}: ${k}" for k in match_props.keys()])

            node_id = self._stable_node_id(label, match_props)
            # FIX: ON MATCH — не затираем непустые поля пустыми значениями
            # ON CREATE — устанавливаем все свойства
            # ON MATCH — обновляем только непустые
            props_set_parts = []
            for pk in properties.keys():
                props_set_parts.append(
                    f"n.{pk} = CASE WHEN $props.{pk} IS NOT NULL AND $props.{pk} <> '' THEN $props.{pk} "
                    f"ELSE coalesce(n.{pk}, $props.{pk}) END"
                )
            on_match_set = ", ".join(props_set_parts) if props_set_parts else "n.updated_at = datetime()"

            query = f"""
            MERGE (n:{label} {{{match_str}}})
            ON CREATE SET n.id = $node_id, n += $props
            ON MATCH SET {on_match_set}, n.updated_at = datetime()
            RETURN n.id as id
            """

            params = {**match_props, "props": properties, "node_id": node_id}

            async with self.driver.session() as session:
                result = await session.run(query, params)
                record = await result.single()

                if record:
                    self.stats["nodes_created"] += 1
                    return record["id"]

        except Exception as e:
            logger.error(f"❌ Error creating {label} node in Neo4j: {e}")
            self.stats["errors"] += 1

        return None

    def _apply_edge_lineage(
        self,
        props: Dict[str, Any],
        from_id: str,
        to_id: str,
        rel_type: str,
        serialize: bool,
    ) -> Dict[str, Any]:
        """P8: env-gated lineage на связь. Единая точка для обоих
        бэкендов. Никогда не роняет создание связи.

        serialize=True (Neo4j) — `_lineage` сериализуется в JSON
        string, т.к. Neo4j не поддерживает вложенные maps в
        properties; networkx хранит dict как есть.
        """
        try:
            from backend.core.lineage.lineage import (
                LINEAGE_KEY,
                lineage_enabled_from_env,
                stamp_props,
            )

            if not lineage_enabled_from_env():
                return props

            stamped = stamp_props(
                props,
                created_by=str(props.get("user_id", "system")),
                pipeline_id=str(props.get("source_meeting_id", "")) or "adhoc",
                stage=f"create_relationship:{rel_type}",
                op="create",
                note=f"{from_id}->{to_id}",
            )
            if serialize and isinstance(stamped.get(LINEAGE_KEY), dict):
                import json as _json
                stamped[LINEAGE_KEY] = _json.dumps(
                    stamped[LINEAGE_KEY], ensure_ascii=False
                )
            return stamped
        except Exception as _lex:
            logger.debug(f"edge lineage skipped: {_lex}")
            return props

    async def _create_relationship(
        self,
        from_id: str,
        to_id: str,
        rel_type: str,
        properties: Dict[str, Any]
    ) -> bool:
        """Создать связь между узлами"""
        if self.use_networkx:
            return self._create_relationship_networkx(from_id, to_id, rel_type, properties)
        else:
            return await self._create_relationship_neo4j(from_id, to_id, rel_type, properties)

    def _create_relationship_networkx(
        self,
        from_id: str,
        to_id: str,
        rel_type: str,
        properties: Dict[str, Any]
    ) -> bool:
        """Создать связь в NetworkX"""
        try:
            if not self.nx_graph.has_node(from_id) or not self.nx_graph.has_node(to_id):
                logger.warning(f"⚠️ Nodes not found: {from_id} -> {to_id}")
                return False

            # VALIDATION
            from_node = self.nx_graph.nodes[from_id]
            to_node = self.nx_graph.nodes[to_id]
            from_label = from_node.get("_label", "Unknown")
            to_label = to_node.get("_label", "Unknown")

            validator = get_schema_validator()
            if not validator.validate_relationship(from_label, to_label, rel_type):
                return False

            # Проверяем существует ли уже такая связь
            existing_edge = None
            if self.nx_graph.has_edge(from_id, to_id):
                for key, attrs in self.nx_graph[from_id][to_id].items():
                    if attrs.get("_type") == rel_type:
                        existing_edge = (key, attrs)
                        break

            # FIX: ВСЕГДА дедуплицируем (не только при enable_graph_weights)
            # Если связь уже существует — обновляем свойства и усиливаем
            if existing_edge:
                key, attrs = existing_edge
                # Усиливаем вес
                attrs["weight"] = attrs.get("weight", 1.0) + 0.1
                attrs["co_occurrences"] = attrs.get("co_occurrences", 1) + 1
                attrs["last_strengthened"] = datetime.now(timezone.utc).isoformat()
                # Обновляем свойства (но не затираем непустые пустыми)
                for pk, pv in properties.items():
                    if pv or not attrs.get(pk):  # Пишем если новое непустое ИЛИ старое пустое
                        attrs[pk] = pv
                self.stats["relationships_created"] += 1
                return True

            # Новая связь — добавляем с весами и bi-temporal полями
            _now_iso = datetime.now(timezone.utc).isoformat()
            edge_props = {
                **properties,
                "_type": rel_type,
                "_created_at": _now_iso,
                "weight": properties.get("weight", 1.0),
                "co_occurrences": 1,
                "last_strengthened": None,
                # ═══ Step 2: Bi-temporal fields ═══
                "valid_from": properties.get("valid_from", None),    # Когда факт стал актуальным
                "valid_until": properties.get("valid_until", None),  # Когда перестал (null=текущий)
                "known_from": properties.get("known_from", _now_iso),# Когда мы узнали
                "known_until": None,                                  # Когда мы узнали что это неверно
            }
            # Гарантируем что description не пустой если передан
            edge_props.setdefault("description", "")
            edge_props.setdefault("confidence", 0.8)

            edge_props = self._apply_edge_lineage(
                edge_props, from_id, to_id, rel_type, serialize=False
            )

            self.nx_graph.add_edge(from_id, to_id, **edge_props)
            self.stats["relationships_created"] += 1
            return True

        except Exception as e:
            logger.error(f"❌ Error creating relationship {rel_type} in NetworkX: {e}")
            self.stats["errors"] += 1
            return False

    async def _create_relationship_neo4j(
        self,
        from_id: str,
        to_id: str,
        rel_type: str,
        properties: Dict[str, Any]
    ) -> bool:
        """Создать связь в Neo4j"""
        try:
            # VALIDATION (Need to fetch labels first)
            validator = get_schema_validator()
            # Оптимизация: проверяем только если валидатор включен (пока он всегда включен, но не строгий)

            async with self.driver.session() as session:
                # Получаем лейблы узлов
                check_query = """
                MATCH (a), (b)
                WHERE a.id = $from_id AND b.id = $to_id
                RETURN labels(a) as source_labels, labels(b) as target_labels
                """
                check_result = await session.run(check_query, {"from_id": from_id, "to_id": to_id})
                record = await check_result.single()

                if not record:
                    logger.warning(f"⚠️ Nodes not found for relationship: {from_id} -> {to_id}")
                    return False

                source_labels = record["source_labels"]
                target_labels = record["target_labels"]

                # Берем первый лейбл (обычно основной)
                from_label = source_labels[0] if source_labels else "Unknown"
                to_label = target_labels[0] if target_labels else "Unknown"

                if not validator.validate_relationship(from_label, to_label, rel_type):
                    return False

                # ═══ Step 2: Bi-temporal + weight fields для Neo4j ═══
                _now_iso = datetime.now(timezone.utc).isoformat()
                enriched_props = {
                    **properties,
                    "weight": properties.get("weight", 1.0),
                    "co_occurrences": 1,
                    "valid_from": properties.get("valid_from", None),
                    "valid_until": properties.get("valid_until", None),
                    "known_from": properties.get("known_from", _now_iso),
                    "created_at": properties.get("created_at", _now_iso),
                }
                enriched_props.setdefault("description", "")
                enriched_props.setdefault("confidence", 0.8)

                enriched_props = self._apply_edge_lineage(
                    enriched_props, from_id, to_id, rel_type, serialize=True
                )

                # Создаем связь (MERGE для дедупликации, ON MATCH для усиления)
                query = f"""
                MATCH (a), (b)
                WHERE a.id = $from_id AND b.id = $to_id
                MERGE (a)-[r:{rel_type}]->(b)
                ON CREATE SET r += $props
                ON MATCH SET r.weight = coalesce(r.weight, 1.0) + 0.1,
                             r.co_occurrences = coalesce(r.co_occurrences, 0) + 1,
                             r.last_strengthened = $now
                RETURN type(r) as type
                """

                result = await session.run(query, {
                    "from_id": from_id,
                    "to_id": to_id,
                    "props": enriched_props,
                    "now": _now_iso,
                })
                record = await result.single()

                if record:
                    self.stats["relationships_created"] += 1
                    return True

        except Exception as e:
            logger.error(f"❌ Error creating relationship {rel_type} in Neo4j: {e}")
            self.stats["errors"] += 1

        return False

    def get_stats(self) -> Dict[str, int]:
        """Получить статистику"""
        return self.stats.copy()

    def reset_stats(self):
        """Сбросить статистику"""
        self.stats = {"nodes_created": 0, "relationships_created": 0, "errors": 0}

    def get_graph_info(self) -> Dict[str, Any]:
        """Получить информацию о графе"""
        info = {
            "backend": self.backend,
            "connected": self.connected,
            "stats": self.stats.copy()
        }

        if self.use_networkx and self.nx_graph:
            info.update({
                "total_nodes": self.nx_graph.number_of_nodes(),
                "total_edges": self.nx_graph.number_of_edges(),
                "storage_path": self.graph_storage_path
            })

            # Подсчёт по типам узлов
            label_counts = {}
            for _, attrs in self.nx_graph.nodes(data=True):
                label = attrs.get("_label", "unknown")
                label_counts[label] = label_counts.get(label, 0) + 1
            info["nodes_by_type"] = label_counts

            # Подсчёт по типам связей
            edge_counts = {}
            for _, _, attrs in self.nx_graph.edges(data=True):
                rel_type = attrs.get("_type", "unknown")
                edge_counts[rel_type] = edge_counts.get(rel_type, 0) + 1
            info["edges_by_type"] = edge_counts

        return info

    def save_graph(self):
        """Принудительно сохранить граф (только NetworkX)"""
        if self.use_networkx:
            self._save_graph_to_file()

    def export_graph(self, output_path: str, format: str = "json") -> bool:
        """
        Экспортировать граф в файл.

        Args:
            output_path: Путь к файлу
            format: Формат (json, gexf, graphml)
        """
        if not self.use_networkx or not self.nx_graph:
            logger.warning("⚠️ Export available only for NetworkX backend")
            return False

        try:
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)

            if format == "json":
                self._save_graph_to_file()
                # Копируем в указанный путь
                import shutil
                shutil.copy(self.graph_storage_path, output_path)
            elif format == "gexf":
                nx.write_gexf(self.nx_graph, output_path)
            elif format == "graphml":
                nx.write_graphml(self.nx_graph, output_path)
            else:
                logger.error(f"❌ Unknown format: {format}")
                return False

            logger.info(f"✅ Graph exported to {output_path}")
            return True

        except Exception as e:
            logger.error(f"❌ Failed to export graph: {e}")
            return False

    async def create_principle(
        self,
        principle_id: str,
        statement: str,
        domain: str,
        confidence: float = 0.5,
        access_group: str = "public",
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Создать узел Principle (Wisdom layer).

        Args:
            principle_id: ID принципа
            statement: Формулировка принципа
            domain: Область знаний (management, engineering, sales...)
            confidence: Уверенность (0.0 - 1.0)
            access_group: Группа доступа
            metadata: Дополнительные метаданные

        Returns:
            True если создано
        """
        props = {
            "principle_id": principle_id,
            "statement": statement,
            "domain": domain,
            "confidence": confidence,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "access_group": access_group,
            **(metadata or {})
        }

        return await self._merge_node("Principle", "principle_id", principle_id, props)

    async def create_node(
        self,
        node_id: str,
        label: str,
        properties: Dict[str, Any],
        access_group: str = "public"
    ) -> bool:
        """
        Создать узел в графе.

        Args:
            node_id: Уникальный ID узла
            label: Метка узла (Person, Task, Decision, etc.)
            properties: Свойства узла
            access_group: Группа доступа (RBAC)

        Returns:
            True если узел создан
        """
        # VALIDATION
        validator = get_schema_validator()
        if not validator.validate_node(label, properties):
            return False

        # TOMBSTONE: пользователь удалил сущность из снепшота («Ишка» ≠
        # сотрудник) — не пересоздаём её при следующем извлечении. Never-raise.
        try:
            from backend.core.store.entity_tombstones import is_tombstoned
            _tid = properties.get("tenant_id") or properties.get("user_id") or ""
            _nm = properties.get("name") or properties.get("title") or ""
            if _tid and _nm and is_tombstoned(str(_tid), str(_nm), label):
                logger.info(f"⚰️ create_node пропущен (tombstone): {label} «{_nm}»")
                return False
        except Exception:
            pass

        try:
            # ═══ STEP 1: Базовые поля на каждом узле ═══
            properties["access_group"] = access_group
            properties.setdefault("access_level", 2)  # INTERNAL по умолчанию
            properties.setdefault("access_groups", [])
            properties.setdefault("tenant_id", properties.get("user_id", ""))
            properties.setdefault("confidence", 0.8)
            properties.setdefault("created_at", datetime.now(timezone.utc).isoformat())
            properties.setdefault("updated_at", datetime.now(timezone.utc).isoformat())

            # ═══ STEP 1b: data lineage (P7) ═══
            # env-gated (DATA_LINEAGE_ENABLED, default OFF) → пока не
            # включат, props не меняются. Обёрнуто — не может уронить
            # создание узла.
            try:
                from backend.core.lineage.lineage import (
                    lineage_enabled_from_env,
                    stamp_props,
                )

                if lineage_enabled_from_env():
                    properties = stamp_props(
                        properties,
                        created_by=str(properties.get("user_id", "system")),
                        pipeline_id=str(
                            properties.get("source_meeting_id", "")
                        ) or "adhoc",
                        stage=f"create_node:{label}",
                        op="create",
                    )
            except Exception as _lex:
                logger.debug(f"lineage stamp skipped: {_lex}")

            if self.use_networkx:
                self.nx_graph.add_node(node_id, _label=label, **properties)
                self._modified = True
                logger.debug(f"Created node {node_id} with label {label}")
                return True
            else:
                # Neo4j — MERGE по id для идемпотентности
                async with self.driver.session() as session:
                    # Neo4j не поддерживает dict/nested как property values.
                    # Сериализуем dict → JSON string, оставляем list/primitive как есть.
                    safe_props = {}
                    for k, v in properties.items():
                        if v is None:
                            continue
                        if isinstance(v, dict):
                            # Neo4j не поддерживает вложенные maps — сериализуем в JSON
                            import json as _json
                            safe_props[k] = _json.dumps(v, ensure_ascii=False)
                        elif isinstance(v, list):
                            # Neo4j поддерживает списки примитивов — фильтруем не-примитивы
                            clean_list = []
                            for item in v:
                                if isinstance(item, (str, int, float, bool)):
                                    clean_list.append(item)
                                elif isinstance(item, dict):
                                    import json as _json
                                    clean_list.append(_json.dumps(item, ensure_ascii=False))
                                else:
                                    clean_list.append(str(item))
                            safe_props[k] = clean_list
                        else:
                            safe_props[k] = v

                    # ON MATCH: обновляем только непустые скалярные поля
                    set_parts = []
                    for k, v in safe_props.items():
                        if isinstance(v, list):
                            # Списки: всегда перезаписываем (merge не имеет смысла)
                            set_parts.append(f"n.{k} = ${k}")
                        elif isinstance(v, (int, float)):
                            set_parts.append(f"n.{k} = ${k}")
                        else:
                            # Строки: обновляем только если новое значение непустое
                            set_parts.append(
                                f"n.{k} = CASE WHEN ${k} IS NOT NULL AND ${k} <> '' THEN ${k} ELSE coalesce(n.{k}, ${k}) END"
                            )
                    on_match_set = ", ".join(set_parts) if set_parts else "n.updated_at = $updated_at"

                    query = (
                        f"MERGE (n:{label} {{id: $node_id}}) "
                        f"ON CREATE SET n += $props "
                        f"ON MATCH SET {on_match_set} "
                        f"RETURN n"
                    )
                    await session.run(query, {
                        "node_id": node_id,
                        "props": {**safe_props, "id": node_id},
                        **safe_props,
                    })
                    return True
        except Exception as e:
            logger.error(f"❌ Failed to create node {node_id}: {e}")
            return False

    def get_node_data(self, node_id: str) -> dict | None:
        """
        Получить данные узла по ID.
        Returns: dict с properties или None если узел не найден.
        """
        try:
            if self.use_networkx:
                if self.nx_graph and self.nx_graph.has_node(node_id):
                    return dict(self.nx_graph.nodes[node_id])
                return None
            else:
                # Для Neo4j — синхронно не можем, возвращаем None
                # (вызывающий код должен использовать async version)
                return None
        except Exception:
            return None

    async def create_relationship(
        self,
        from_id: str,
        to_id: str,
        rel_type: str,
        properties: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Создать связь между узлами (публичный API).

        Args:
            from_id: ID исходного узла
            to_id: ID целевого узла
            rel_type: Тип связи (EXTRACTED_FROM, ASSIGNED_TO, etc.)
            properties: Свойства связи

        Returns:
            True если связь создана
        """
        return await self._create_relationship(
            from_id=from_id,
            to_id=to_id,
            rel_type=rel_type,
            properties=properties or {}
        )

    async def update_node(
        self,
        node_id: str,
        properties: Dict[str, Any]
    ) -> bool:
        """
        Обновить свойства узла.

        Args:
            node_id: ID узла
            properties: Новые свойства

        Returns:
            True если узел обновлён
        """
        try:
            if self.use_networkx:
                if self.nx_graph.has_node(node_id):
                    for key, value in properties.items():
                        self.nx_graph.nodes[node_id][key] = value
                    self._modified = True
                    return True
                return False
            else:
                # Neo4j
                async with self.driver.session() as session:
                    set_clause = ", ".join([f"n.{k} = ${k}" for k in properties.keys()])
                    query = f"MATCH (n) WHERE n.id = $node_id SET {set_clause} RETURN n"
                    result = await session.run(query, {"node_id": node_id, **properties})
                    return await result.single() is not None
        except Exception as e:
            logger.error(f"❌ Failed to update node {node_id}: {e}")
            return False

    async def get_node_by_id(
        self,
        node_id: str,
        tenant_id: Optional[Union[str, Sequence[str]]] = None,
        strict_tenant: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Получить узел по ID.

        tenant_id (str или список своих tenant-ов) — анти-утечка: узел чужого
        тенанта не возвращается (None). Legacy-узлы без штампа проходят, если
        не strict_tenant. None (дефолт) — поведение прежнее, без фильтра.
        """
        tenants: Optional[List[str]] = None
        if tenant_id:
            tenants = ([tenant_id] if isinstance(tenant_id, str)
                       else [str(t) for t in tenant_id if t])
        if self.use_networkx:
            if self.nx_graph.has_node(node_id):
                attrs = self.nx_graph.nodes[node_id]
                if tenants is not None:
                    t = attrs.get("tenant_id")
                    if t in (None, ""):
                        if strict_tenant:
                            return None
                    elif str(t) not in tenants:
                        return None
                return {"id": node_id, **attrs}
            return None
        else:
            # Neo4j query
            try:
                clause = ""
                params: Dict[str, Any] = {"id": node_id}
                if tenants is not None:
                    if strict_tenant:
                        clause = " AND n.tenant_id IN $tenants"
                    else:
                        clause = (" AND (n.tenant_id IS NULL OR n.tenant_id = ''"
                                  " OR n.tenant_id IN $tenants)")
                    params["tenants"] = tenants
                async with self.driver.session() as session:
                    result = await session.run(
                        f"MATCH (n) WHERE n.id = $id{clause} "
                        "RETURN n, labels(n) as labels",
                        params
                    )
                    record = await result.single()
                    if record:
                        props = dict(record["n"])
                        labels = record.get("labels") or []
                        props["_label"] = labels[0] if labels else props.get("_label", "unknown")
                        return props
            except Exception as e:
                logger.error(f"❌ Error getting node: {e}")
            return None

    async def find_nodes_by_label(
        self, label: str, limit: int = 100,
        tenant_id: Optional[str] = None,
        strict_tenant: bool = False,
    ) -> List[Dict[str, Any]]:
        """Найти узлы по метке.

        issue #112 RLS hotfix: фильтрация по tenant_id (Neo4j RLS не имеет —
        изоляция на уровне Cypher-запроса). Источники tenant_id в порядке:
          1. явный параметр (caller знает контекст);
          2. observability.tenant_context (если запрос через middleware);
          3. None — fallback: возвращаются ТОЛЬКО узлы с tenant_id IS NULL
             (legacy данные до multi-tenant). Это безопасный default —
             узлы тенанта B никогда не попадают в результат.

        strict_tenant=True: когда tenant_id задан, возвращаем ТОЛЬКО узлы этого
        тенанта, БЕЗ legacy-NULL. Нужно для списков людей/снапшотов: узлы без
        tenant-штампа (созданные другим аккаунтом) иначе протекают во все
        аккаунты («чужой человек в моей команде»).
        """
        if tenant_id is None:
            try:
                from backend.core.observability.tenant_context import (
                    get_current_tenant,
                )
                tenant_id = get_current_tenant()
            except Exception:
                tenant_id = None

        if self.use_networkx:
            nodes = []
            for node_id, attrs in self.nx_graph.nodes(data=True):
                if attrs.get("_label") != label:
                    continue
                node_tenant = attrs.get("tenant_id")
                # Без явного tenant_id — только legacy (NULL); с тенантом —
                # его записи + legacy. Никогда не пускаем кросс-тенант.
                if tenant_id is None:
                    if node_tenant not in (None, ""):
                        continue
                elif strict_tenant:
                    # Строго свой тенант — без legacy-NULL (анти-утечка).
                    if node_tenant != tenant_id:
                        continue
                else:
                    if node_tenant not in (None, "", tenant_id):
                        continue
                nodes.append({"id": node_id, **attrs})
                if len(nodes) >= limit:
                    break
            return nodes
        else:
            # Neo4j query с tenant_id фильтрацией
            try:
                if strict_tenant and tenant_id:
                    # Строго свой тенант — без legacy-NULL (анти-утечка людей
                    # из чужих аккаунтов).
                    where_clause = "WHERE n.tenant_id = $tenant"
                else:
                    where_clause = (
                        "WHERE (n.tenant_id IS NULL OR n.tenant_id = '') "
                        "   OR ($tenant IS NOT NULL AND n.tenant_id = $tenant) "
                    )
                async with self.driver.session() as session:
                    result = await session.run(
                        f"MATCH (n:{label}) {where_clause} RETURN n LIMIT $limit",
                        {"limit": limit, "tenant": tenant_id},
                    )
                    records = await result.data()
                    return [dict(r["n"]) for r in records]
            except Exception as e:
                logger.error(f"❌ Error finding nodes: {e}")
            return []

    async def get_node_relationships(self, node_id: str) -> Dict[str, List[Dict[str, Any]]]:
        """Получить все связи узла"""
        result = {"incoming": [], "outgoing": []}

        if self.use_networkx:
            # Исходящие связи
            for _, target, attrs in self.nx_graph.out_edges(node_id, data=True):
                result["outgoing"].append({
                    "target": target,
                    "type": attrs.get("_type", ""),
                    **attrs
                })

            # Входящие связи
            for source, _, attrs in self.nx_graph.in_edges(node_id, data=True):
                result["incoming"].append({
                    "source": source,
                    "type": attrs.get("_type", ""),
                    **attrs
                })
        else:
            # Neo4j query
            try:
                async with self.driver.session() as session:
                    # Исходящие
                    out_result = await session.run(
                        """
                        MATCH (n)-[r]->(m)
                        WHERE n.id = $id
                        RETURN type(r) as type, m.id as target, properties(r) as props
                        """,
                        {"id": node_id}
                    )
                    for record in await out_result.data():
                        result["outgoing"].append({
                            "target": record["target"],
                            "type": record["type"],
                            **record["props"]
                        })

                    # Входящие
                    in_result = await session.run(
                        """
                        MATCH (m)-[r]->(n)
                        WHERE n.id = $id
                        RETURN type(r) as type, m.id as source, properties(r) as props
                        """,
                        {"id": node_id}
                    )
                    for record in await in_result.data():
                        result["incoming"].append({
                            "source": record["source"],
                            "type": record["type"],
                            **record["props"]
                        })
            except Exception as e:
                logger.error(f"❌ Error getting relationships: {e}")

        return result

    async def get_node_chunks(self, node_id: str) -> List[str]:
        """
        Получить текст чанков, связанных с узлом.

        Args:
            node_id: ID узла

        Returns:
            Список текстов чанков
        """
        chunks = []
        try:
            if self.use_networkx:
                if not self.nx_graph.has_node(node_id):
                    return []

                # Ищем связи MENTIONED_IN -> Chunk
                for _, target, attrs in self.nx_graph.out_edges(node_id, data=True):
                    if attrs.get("_type") == "MENTIONED_IN":
                        chunk_node = self.nx_graph.nodes[target]
                        if chunk_node.get("_label") == "Chunk":
                            chunks.append(chunk_node.get("text", ""))
            else:
                # Neo4j
                async with self.driver.session() as session:
                    query = """
                    MATCH (n)-[:MENTIONED_IN]->(c:Chunk)
                    WHERE n.id = $node_id
                    RETURN c.text as text
                    """
                    result = await session.run(query, {"node_id": node_id})
                    records = await result.data()
                    chunks = [r["text"] for r in records if r.get("text")]

        except Exception as e:
            logger.error(f"❌ Failed to get node chunks for {node_id}: {e}")

        return chunks

    # ============================================================
    # НОВЫЕ МЕТОДЫ: Работа с весами (enable_graph_weights)
    # ============================================================

    async def update_node_weight(
        self,
        node_id: str,
        weight_delta: float = 0.0,
        set_weight: Optional[float] = None,
        update_access: bool = True
    ) -> bool:
        """
        Обновить вес узла.

        Args:
            node_id: ID узла
            weight_delta: Изменение веса (добавляется к текущему)
            set_weight: Установить абсолютное значение веса (игнорирует delta)
            update_access: Обновить access_count и last_accessed

        Returns:
            True если обновлено успешно
        """
        try:
            from backend.core.config import flags
            if not flags.enable_graph_weights:
                return True  # Молча пропускаем если флаг выключен

            if self.use_networkx:
                if not self.nx_graph.has_node(node_id):
                    return False

                node = self.nx_graph.nodes[node_id]

                # Обновляем вес
                if set_weight is not None:
                    node["weight"] = max(0.0, set_weight)
                else:
                    current_weight = node.get("weight", 1.0)
                    node["weight"] = max(0.0, current_weight + weight_delta)

                # Обновляем access_count и last_accessed
                if update_access:
                    node["access_count"] = node.get("access_count", 0) + 1
                    node["last_accessed"] = datetime.now(timezone.utc).isoformat()

                return True
            else:
                # Neo4j
                async with self.driver.session() as session:
                    if set_weight is not None:
                        query = """
                        MATCH (n) WHERE n.id = $node_id
                        SET n.weight = $weight,
                            n.access_count = COALESCE(n.access_count, 0) + 1,
                            n.last_accessed = datetime()
                        RETURN n
                        """
                        result = await session.run(query, {"node_id": node_id, "weight": max(0.0, set_weight)})
                    else:
                        query = """
                        MATCH (n) WHERE n.id = $node_id
                        SET n.weight = COALESCE(n.weight, 1.0) + $delta,
                            n.access_count = COALESCE(n.access_count, 0) + 1,
                            n.last_accessed = datetime()
                        RETURN n
                        """
                        result = await session.run(query, {"node_id": node_id, "delta": weight_delta})
                    return await result.single() is not None

        except Exception as e:
            logger.error(f"❌ Failed to update node weight {node_id}: {e}")
            return False

    async def update_edge_weight(
        self,
        from_id: str,
        to_id: str,
        rel_type: str,
        weight_delta: float = 0.0,
        set_weight: Optional[float] = None,
        increment_co_occurrences: bool = True
    ) -> bool:
        """
        Обновить вес ребра.

        Args:
            from_id: ID исходного узла
            to_id: ID целевого узла
            rel_type: Тип связи
            weight_delta: Изменение веса
            set_weight: Установить абсолютное значение
            increment_co_occurrences: Увеличить счётчик совместных упоминаний

        Returns:
            True если обновлено успешно
        """
        try:
            from backend.core.config import flags
            if not flags.enable_graph_weights:
                return True

            if self.use_networkx:
                if not self.nx_graph.has_edge(from_id, to_id):
                    return False

                # Получаем первое ребро нужного типа
                for _key, attrs in self.nx_graph[from_id][to_id].items():
                    if attrs.get("_type") == rel_type:
                        # Обновляем вес
                        if set_weight is not None:
                            attrs["weight"] = max(0.0, set_weight)
                        else:
                            current_weight = attrs.get("weight", 1.0)
                            attrs["weight"] = max(0.0, current_weight + weight_delta)

                        # Обновляем co_occurrences
                        if increment_co_occurrences:
                            attrs["co_occurrences"] = attrs.get("co_occurrences", 1) + 1

                        attrs["last_strengthened"] = datetime.now(timezone.utc).isoformat()
                        return True

                return False
            else:
                # Neo4j
                async with self.driver.session() as session:
                    if set_weight is not None:
                        query = f"""
                        MATCH (a)-[r:{rel_type}]->(b)
                        WHERE a.id = $from_id AND b.id = $to_id
                        SET r.weight = $weight,
                            r.co_occurrences = COALESCE(r.co_occurrences, 1) + 1,
                            r.last_strengthened = datetime()
                        RETURN r
                        """
                        result = await session.run(query, {
                            "from_id": from_id, "to_id": to_id, "weight": max(0.0, set_weight)
                        })
                    else:
                        query = f"""
                        MATCH (a)-[r:{rel_type}]->(b)
                        WHERE a.id = $from_id AND b.id = $to_id
                        SET r.weight = COALESCE(r.weight, 1.0) + $delta,
                            r.co_occurrences = COALESCE(r.co_occurrences, 1) + 1,
                            r.last_strengthened = datetime()
                        RETURN r
                        """
                        result = await session.run(query, {
                            "from_id": from_id, "to_id": to_id, "delta": weight_delta
                        })
                    return await result.single() is not None

        except Exception as e:
            logger.error(f"❌ Failed to update edge weight {from_id}->{to_id}: {e}")
            return False

    async def increment_node_mentions(
        self,
        node_id: str,
        meeting_id: str,
        count: int = 1
    ) -> bool:
        """
        Увеличить счётчик упоминаний узла.

        Args:
            node_id: ID узла
            meeting_id: ID встречи (для отслеживания источника)
            count: Количество упоминаний

        Returns:
            True если обновлено успешно
        """
        try:
            from backend.core.config import flags
            if not flags.enable_frequency_tracking:
                return True

            if self.use_networkx:
                if not self.nx_graph.has_node(node_id):
                    return False

                node = self.nx_graph.nodes[node_id]

                # Увеличиваем total_mentions
                node["total_mentions"] = node.get("total_mentions", 0) + count

                # Обновляем frequency_score (логарифмическая шкала)
                import math
                total = node["total_mentions"]
                node["frequency_score"] = math.log1p(total) / 10.0  # Нормализация

                # Сохраняем последнюю встречу
                node["last_mentioned_in"] = meeting_id
                node["last_mentioned_at"] = datetime.now(timezone.utc).isoformat()

                return True
            else:
                # Neo4j
                async with self.driver.session() as session:
                    query = """
                    MATCH (n) WHERE n.id = $node_id
                    SET n.total_mentions = COALESCE(n.total_mentions, 0) + $count,
                        n.frequency_score = log(COALESCE(n.total_mentions, 0) + $count + 1) / 10.0,
                        n.last_mentioned_in = $meeting_id,
                        n.last_mentioned_at = datetime()
                    RETURN n
                    """
                    result = await session.run(query, {
                        "node_id": node_id, "count": count, "meeting_id": meeting_id
                    })
                    return await result.single() is not None

        except Exception as e:
            logger.error(f"❌ Failed to increment mentions for {node_id}: {e}")
            return False

    async def update_emotional_salience(
        self,
        node_id: str,
        salience_delta: float = 0.0,
        set_salience: Optional[float] = None
    ) -> bool:
        """
        Обновить эмоциональную значимость узла.

        Args:
            node_id: ID узла
            salience_delta: Изменение значимости
            set_salience: Установить абсолютное значение (0.0 - 1.0)

        Returns:
            True если обновлено успешно
        """
        try:
            from backend.core.config import flags
            if not flags.enable_emotional_salience:
                return True

            if self.use_networkx:
                if not self.nx_graph.has_node(node_id):
                    return False

                node = self.nx_graph.nodes[node_id]

                if set_salience is not None:
                    node["emotional_salience"] = max(0.0, min(1.0, set_salience))
                else:
                    current = node.get("emotional_salience", 0.0)
                    node["emotional_salience"] = max(0.0, min(1.0, current + salience_delta))

                return True
            else:
                # Neo4j
                async with self.driver.session() as session:
                    if set_salience is not None:
                        value = max(0.0, min(1.0, set_salience))
                        query = """
                        MATCH (n) WHERE n.id = $node_id
                        SET n.emotional_salience = $value
                        RETURN n
                        """
                        result = await session.run(query, {"node_id": node_id, "value": value})
                    else:
                        query = """
                        MATCH (n) WHERE n.id = $node_id
                        SET n.emotional_salience =
                            CASE
                                WHEN COALESCE(n.emotional_salience, 0.0) + $delta < 0 THEN 0
                                WHEN COALESCE(n.emotional_salience, 0.0) + $delta > 1 THEN 1
                                ELSE COALESCE(n.emotional_salience, 0.0) + $delta
                            END
                        RETURN n
                        """
                        result = await session.run(query, {"node_id": node_id, "delta": salience_delta})
                    return await result.single() is not None

        except Exception as e:
            logger.error(f"❌ Failed to update emotional salience for {node_id}: {e}")
            return False

    def get_top_weighted_nodes(
        self,
        label: Optional[str] = None,
        limit: int = 20,
        tenant_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Получить узлы с наибольшим весом.

        Args:
            label: Фильтр по метке (опционально)
            limit: Максимальное количество узлов
            tenant_id: tenant фильтр (или текущий tenant_context)

        Returns:
            Список узлов отсортированных по весу
        """
        if not self.use_networkx or not self.nx_graph:
            return []

        tenant = self._resolve_tenant_for_query(tenant_id)
        nodes = []
        for node_id, attrs in self.nx_graph.nodes(data=True):
            if label and attrs.get("_label") != label:
                continue
            if not self._tenant_networkx_pass(attrs, tenant):
                continue

            nodes.append({
                "id": node_id,
                "weight": attrs.get("weight", 1.0),
                "frequency_score": attrs.get("frequency_score", 0.0),
                "emotional_salience": attrs.get("emotional_salience", 0.0),
                "total_mentions": attrs.get("total_mentions", 0),
                "name": attrs.get("name", ""),
                "_label": attrs.get("_label", ""),
            })

        # Сортируем по весу (убывание)
        nodes.sort(key=lambda x: x["weight"], reverse=True)

        return nodes[:limit]

    def get_strongest_edges(
        self,
        node_id: str,
        limit: int = 10,
        direction: str = "both",  # "in", "out", "both"
        tenant_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Получить самые сильные связи узла.

        Args:
            node_id: ID узла
            limit: Максимальное количество связей
            direction: Направление связей
            tenant_id: tenant фильтр (или текущий tenant_context)

        Returns:
            Список связей отсортированных по весу
        """
        if not self.use_networkx or not self.nx_graph:
            return []

        if not self.nx_graph.has_node(node_id):
            return []

        tenant = self._resolve_tenant_for_query(tenant_id)
        node_attrs = self.nx_graph.nodes[node_id]
        # Если сам узел чужого тенанта — наружу ничего не возвращаем
        if not self._tenant_networkx_pass(node_attrs, tenant):
            return []

        edges = []

        # Исходящие связи
        if direction in ("out", "both"):
            for _, target, attrs in self.nx_graph.out_edges(node_id, data=True):
                target_attrs = self.nx_graph.nodes.get(target, {})
                if not self._tenant_networkx_pass(attrs, tenant):
                    continue
                if not self._tenant_networkx_pass(target_attrs, tenant):
                    continue
                edges.append({
                    "source": node_id,
                    "target": target,
                    "direction": "out",
                    "type": attrs.get("_type", ""),
                    "weight": attrs.get("weight", 1.0),
                    "co_occurrences": attrs.get("co_occurrences", 1),
                })

        # Входящие связи
        if direction in ("in", "both"):
            for source, _, attrs in self.nx_graph.in_edges(node_id, data=True):
                source_attrs = self.nx_graph.nodes.get(source, {})
                if not self._tenant_networkx_pass(attrs, tenant):
                    continue
                if not self._tenant_networkx_pass(source_attrs, tenant):
                    continue
                edges.append({
                    "source": source,
                    "target": node_id,
                    "direction": "in",
                    "type": attrs.get("_type", ""),
                    "weight": attrs.get("weight", 1.0),
                    "co_occurrences": attrs.get("co_occurrences", 1),
                })

        # Сортируем по весу
        edges.sort(key=lambda x: x["weight"], reverse=True)

        return edges[:limit]

    def get_node_with_weights(self, node_id: str) -> Optional[Dict[str, Any]]:
        """
        Получить узел со всеми полями весов.

        Args:
            node_id: ID узла

        Returns:
            Узел с полями weight, frequency_score, emotional_salience и т.д.
        """
        if not self.use_networkx or not self.nx_graph:
            return None

        if not self.nx_graph.has_node(node_id):
            return None

        attrs = self.nx_graph.nodes[node_id]

        return {
            "id": node_id,
            "name": attrs.get("name", ""),
            "_label": attrs.get("_label", ""),
            # Поля весов
            "weight": attrs.get("weight", 1.0),
            "access_count": attrs.get("access_count", 0),
            "last_accessed": attrs.get("last_accessed"),
            # Частота упоминаний
            "total_mentions": attrs.get("total_mentions", 0),
            "frequency_score": attrs.get("frequency_score", 0.0),
            "last_mentioned_in": attrs.get("last_mentioned_in"),
            "last_mentioned_at": attrs.get("last_mentioned_at"),
            # Эмоциональная значимость
            "emotional_salience": attrs.get("emotional_salience", 0.0),
            # Остальные атрибуты
            **{k: v for k, v in attrs.items() if k not in [
                "weight", "access_count", "last_accessed",
                "total_mentions", "frequency_score", "last_mentioned_in", "last_mentioned_at",
                "emotional_salience", "name", "_label", "id"
            ]}
        }


    # ═══════════════════════════════════════════════════════════
    # STEP 2: TEMPORAL QUERY METHODS
    # ═══════════════════════════════════════════════════════════

    async def query_at_time(
        self,
        timestamp: str,
        node_type: str | None = None,
        user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> list:
        """
        Вернуть связи, актуальные на определённый момент времени.

        issue #112 RLS hotfix (wave 2): tenant_id берётся из аргумента или
        observability.tenant_context. Без tenant'а — возвращаются только
        legacy связи (tenant_id IS NULL), никогда чужие данные.

        Args:
            timestamp: ISO datetime string
            node_type: Фильтр по типу узла (опционально)
            user_id: Фильтр по пользователю
            tenant_id: Фильтр по тенанту (None → safe default из ctx или legacy)
        """
        tenant = self._resolve_tenant_for_query(tenant_id)
        results = []

        if self.use_networkx:
            for u, v, _key, data in self.nx_graph.edges(keys=True, data=True):
                vf = data.get("valid_from")
                vu = data.get("valid_until")

                # Связь актуальна если: valid_from <= timestamp И (valid_until null ИЛИ valid_until > timestamp)
                if vf and vf > timestamp:
                    continue  # Ещё не началось
                if vu and vu <= timestamp:
                    continue  # Уже закончилось

                # issue #112 RLS: tenant-фильтр (по ребру И по обоим узлам)
                if not self._tenant_networkx_pass(data, tenant):
                    continue
                u_data = self.nx_graph.nodes.get(u, {})
                v_data = self.nx_graph.nodes.get(v, {})
                if not self._tenant_networkx_pass(u_data, tenant):
                    continue
                if not self._tenant_networkx_pass(v_data, tenant):
                    continue

                # Фильтр по типу узла
                if node_type:
                    u_label = u_data.get("_label", "")
                    v_label = v_data.get("_label", "")
                    if node_type not in (u_label, v_label):
                        continue

                # Фильтр по user_id
                if user_id:
                    if data.get("user_id") != user_id and data.get("source_meeting_id", "").split("_")[0] != user_id:
                        if u_data.get("user_id") != user_id:
                            continue

                results.append({
                    "from_id": u,
                    "to_id": v,
                    "rel_type": data.get("_type", ""),
                    "properties": {k: v2 for k, v2 in data.items() if not k.startswith("_")},
                })
        else:
            # Neo4j
            async with self.driver.session() as session:
                tenant_clause = self._tenant_cypher_clause("a")
                tenant_clause_b = self._tenant_cypher_clause("b")
                tenant_clause_r = self._tenant_cypher_clause("r")
                query = f"""
                MATCH (a)-[r]->(b)
                WHERE (r.valid_from IS NULL OR r.valid_from <= $ts)
                  AND (r.valid_until IS NULL OR r.valid_until > $ts)
                  AND {tenant_clause}
                  AND {tenant_clause_b}
                  AND {tenant_clause_r}
                """
                if node_type:
                    query += f" AND ('{node_type}' IN labels(a) OR '{node_type}' IN labels(b))"
                if user_id:
                    query += " AND (a.user_id = $uid OR b.user_id = $uid)"
                query += " RETURN a.id as from_id, b.id as to_id, type(r) as rel_type, properties(r) as props LIMIT 1000"

                params = {"ts": timestamp, "tenant": tenant}
                if user_id:
                    params["uid"] = user_id
                result = await session.run(query, params)
                async for record in result:
                    results.append({
                        "from_id": record["from_id"],
                        "to_id": record["to_id"],
                        "rel_type": record["rel_type"],
                        "properties": dict(record["props"]) if record["props"] else {},
                    })

        return results

    async def query_changes_between(
        self,
        start: str,
        end: str,
        user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> list:
        """
        Вернуть связи, о которых мы узнали между двумя датами.

        issue #112 RLS hotfix (wave 2): tenant_id обязательный фильтр для
        изоляции (берётся из аргумента или ctx; без него — только legacy).
        """
        tenant = self._resolve_tenant_for_query(tenant_id)
        results = []

        if self.use_networkx:
            for u, v, _key, data in self.nx_graph.edges(keys=True, data=True):
                kf = data.get("known_from", data.get("_created_at", ""))
                if not kf:
                    continue
                if kf < start or kf > end:
                    continue

                # issue #112 RLS: tenant-фильтр (ребро + оба узла)
                if not self._tenant_networkx_pass(data, tenant):
                    continue
                u_data = self.nx_graph.nodes.get(u, {})
                v_data = self.nx_graph.nodes.get(v, {})
                if not self._tenant_networkx_pass(u_data, tenant):
                    continue
                if not self._tenant_networkx_pass(v_data, tenant):
                    continue

                if user_id:
                    if u_data.get("user_id") != user_id:
                        continue

                results.append({
                    "from_id": u,
                    "to_id": v,
                    "rel_type": data.get("_type", ""),
                    "properties": {k: v2 for k, v2 in data.items() if not k.startswith("_")},
                })
        else:
            async with self.driver.session() as session:
                tenant_clause = self._tenant_cypher_clause("a")
                tenant_clause_b = self._tenant_cypher_clause("b")
                tenant_clause_r = self._tenant_cypher_clause("r")
                query = f"""
                MATCH (a)-[r]->(b)
                WHERE r.known_from >= $start AND r.known_from <= $end
                  AND {tenant_clause}
                  AND {tenant_clause_b}
                  AND {tenant_clause_r}
                """
                if user_id:
                    query += " AND (a.user_id = $uid OR b.user_id = $uid)"
                query += " RETURN a.id as from_id, b.id as to_id, type(r) as rel_type, properties(r) as props ORDER BY r.known_from LIMIT 1000"

                params = {"start": start, "end": end, "tenant": tenant}
                if user_id:
                    params["uid"] = user_id
                result = await session.run(query, params)
                async for record in result:
                    results.append({
                        "from_id": record["from_id"],
                        "to_id": record["to_id"],
                        "rel_type": record["rel_type"],
                        "properties": dict(record["props"]) if record["props"] else {},
                    })

        return results

    # ============================================================
    # Knowledge Editor API: search, remove, merge, add/remove edge
    # ============================================================

    async def search_nodes(
        self,
        query: str,
        node_type: Optional[str] = None,
        limit: int = 10,
        tenant_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Поиск узлов по имени (CONTAINS, case-insensitive).
        Используется Knowledge Editor для нахождения сущностей.

        issue #112 RLS hotfix (wave 2): tenant_id фильтр обязателен —
        без него Knowledge Editor любого юзера видел узлы всех тенантов.
        Источники: явный параметр → ctx → None (только legacy NULL).
        """
        tenant = self._resolve_tenant_for_query(tenant_id)
        query_lower = query.lower().strip()
        results: List[Dict[str, Any]] = []

        if self.use_networkx:
            for nid, attrs in self.nx_graph.nodes(data=True):
                name = (attrs.get("name") or "").lower()
                label = attrs.get("_label", "")
                if node_type and label.lower() != node_type.lower():
                    continue
                # issue #112 RLS tenant-фильтр
                if not self._tenant_networkx_pass(attrs, tenant):
                    continue
                if query_lower in name or query_lower in nid.lower():
                    results.append({"id": nid, **attrs})
                    if len(results) >= limit:
                        break
        else:
            try:
                async with self.driver.session() as session:
                    tenant_clause = self._tenant_cypher_clause("n")
                    # Нормализуем тип к фактической метке графа: редактор/агенты
                    # шлют lowercase ("project"), а в Neo4j метки капитализованы
                    # ("Project") → MATCH (n:project) давал warning «label does
                    # not exist» и пустой результат. Капитализация невалидных
                    # типов (KPI и т.п.) — через карту.
                    norm_label = _SEARCH_LABEL_MAP.get(
                        (node_type or "").lower(), (node_type or "").strip()
                    ) if node_type else None
                    if norm_label:
                        cypher = (
                            f"MATCH (n:`{norm_label}`) "
                            "WHERE (toLower(n.name) CONTAINS $q OR toLower(n.id) CONTAINS $q) "
                            f"AND {tenant_clause} "
                            "RETURN n, labels(n) AS labels LIMIT $lim"
                        )
                    else:
                        cypher = (
                            "MATCH (n) "
                            "WHERE (toLower(n.name) CONTAINS $q OR toLower(n.id) CONTAINS $q) "
                            f"AND {tenant_clause} "
                            "RETURN n, labels(n) AS labels LIMIT $lim"
                        )
                    res = await session.run(
                        cypher,
                        {"q": query_lower, "lim": limit, "tenant": tenant})
                    for record in await res.data():
                        props = dict(record["n"])
                        labels = record.get("labels") or []
                        props["_label"] = labels[0] if labels else props.get("_label", "unknown")
                        results.append(props)
            except Exception as e:
                logger.error(f"❌ search_nodes error: {e}")

        return results

    async def remove_node(self, node_id: str) -> bool:
        """
        Удалить узел и все его связи.
        """
        try:
            if self.use_networkx:
                if self.nx_graph.has_node(node_id):
                    self.nx_graph.remove_node(node_id)
                    self._modified = True
                    return True
                return False
            else:
                async with self.driver.session() as session:
                    result = await session.run(
                        "MATCH (n) WHERE n.id = $id DETACH DELETE n RETURN count(n) AS cnt",
                        {"id": node_id},
                    )
                    record = await result.single()
                    return record and record["cnt"] > 0
        except Exception as e:
            logger.error(f"❌ remove_node error for {node_id}: {e}")
            return False

    async def add_edge(
        self,
        source_id: str,
        target_id: str,
        relationship_type: str = "RELATES_TO",
        properties: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Alias для create_relationship (совместимость с nl_editor).
        """
        return await self.create_relationship(
            from_id=source_id,
            to_id=target_id,
            rel_type=relationship_type,
            properties=properties,
        )

    async def remove_edge(
        self,
        source_id: str,
        target_id: str,
        rel_type: Optional[str] = None,
    ) -> bool:
        """
        Удалить связь между узлами.
        Если rel_type задан — удаляет только указанный тип, иначе все связи.
        """
        try:
            if self.use_networkx:
                if self.nx_graph.has_edge(source_id, target_id):
                    self.nx_graph.remove_edge(source_id, target_id)
                    self._modified = True
                    return True
                return False
            else:
                async with self.driver.session() as session:
                    if rel_type:
                        cypher = (
                            f"MATCH (a)-[r:{rel_type}]->(b) "
                            "WHERE a.id = $src AND b.id = $tgt "
                            "DELETE r RETURN count(r) AS cnt"
                        )
                    else:
                        cypher = (
                            "MATCH (a)-[r]->(b) "
                            "WHERE a.id = $src AND b.id = $tgt "
                            "DELETE r RETURN count(r) AS cnt"
                        )
                    result = await session.run(cypher, {"src": source_id, "tgt": target_id})
                    record = await result.single()
                    return record and record["cnt"] > 0
        except Exception as e:
            logger.error(f"❌ remove_edge error {source_id}->{target_id}: {e}")
            return False

    async def merge_nodes(
        self,
        keep_id: str,
        merge_id: str,
    ) -> bool:
        """
        Объединить два узла: перенести все связи merge_id → keep_id, удалить merge_id.
        Свойства merge_id добавляются к keep_id если у keep_id они пусты.
        """
        try:
            if self.use_networkx:
                if not self.nx_graph.has_node(keep_id) or not self.nx_graph.has_node(merge_id):
                    return False

                keep_attrs = self.nx_graph.nodes[keep_id]
                merge_attrs = self.nx_graph.nodes[merge_id]

                # Переносим свойства (не перезаписываем непустые)
                for k, v in merge_attrs.items():
                    if k.startswith("_"):
                        continue
                    existing = keep_attrs.get(k)
                    if not existing and v:
                        keep_attrs[k] = v

                # Aliases
                keep_aliases = keep_attrs.get("aliases", [])
                merge_name = merge_attrs.get("name", merge_id)
                if merge_name and merge_name not in keep_aliases:
                    if isinstance(keep_aliases, list):
                        keep_aliases.append(merge_name)
                    else:
                        keep_aliases = [merge_name]
                    keep_attrs["aliases"] = keep_aliases

                # Переносим исходящие связи
                for _, target, data in list(self.nx_graph.out_edges(merge_id, data=True)):
                    if target != keep_id and not self.nx_graph.has_edge(keep_id, target):
                        self.nx_graph.add_edge(keep_id, target, **data)

                # Переносим входящие связи
                for source, _, data in list(self.nx_graph.in_edges(merge_id, data=True)):
                    if source != keep_id and not self.nx_graph.has_edge(source, keep_id):
                        self.nx_graph.add_edge(source, keep_id, **data)

                self.nx_graph.remove_node(merge_id)
                self._modified = True
                return True
            else:
                # Neo4j
                async with self.driver.session() as session:
                    # 1. Получаем свойства merge_id
                    res = await session.run(
                        "MATCH (n) WHERE n.id = $id RETURN properties(n) AS props",
                        {"id": merge_id},
                    )
                    merge_rec = await res.single()
                    if not merge_rec:
                        return False

                    merge_props = dict(merge_rec["props"])

                    # 2. Обновляем keep_id свойствами из merge_id (пустые → заполняем)
                    res2 = await session.run(
                        "MATCH (n) WHERE n.id = $id RETURN properties(n) AS props",
                        {"id": keep_id},
                    )
                    keep_rec = await res2.single()
                    if not keep_rec:
                        return False

                    keep_props = dict(keep_rec["props"])
                    updates = {}
                    for k, v in merge_props.items():
                        if k in ("id",) or k.startswith("_"):
                            continue
                        existing = keep_props.get(k)
                        if (not existing or existing in ("", None, [], {})) and v:
                            updates[k] = v

                    # Добавляем alias
                    merge_name = merge_props.get("name", merge_id)
                    aliases_raw = keep_props.get("aliases", "[]")
                    import json as _json
                    if isinstance(aliases_raw, str):
                        try:
                            aliases = _json.loads(aliases_raw)
                        except (ValueError, TypeError):
                            aliases = [aliases_raw] if aliases_raw else []
                    elif isinstance(aliases_raw, list):
                        aliases = aliases_raw
                    else:
                        aliases = []
                    if merge_name and merge_name not in aliases:
                        aliases.append(merge_name)
                    updates["aliases"] = _json.dumps(aliases, ensure_ascii=False)

                    if updates:
                        set_parts = ", ".join(f"a.{k} = ${k}" for k in updates)
                        await session.run(
                            f"MATCH (a) WHERE a.id = $keep SET {set_parts}",
                            {"keep": keep_id, **updates},
                        )

                    # 3. Переносим исходящие связи merge → keep
                    await session.run(
                        """
                        MATCH (old)-[r]->(target)
                        WHERE old.id = $merge AND target.id <> $keep
                        WITH old, r, target, type(r) AS rt, properties(r) AS rp
                        MERGE (keep)-[nr:RELATES_TO]->(target)
                        SET nr = rp
                        DELETE r
                        """,
                        {"merge": merge_id, "keep": keep_id},
                    )

                    # 4. Переносим входящие связи
                    await session.run(
                        """
                        MATCH (source)-[r]->(old)
                        WHERE old.id = $merge AND source.id <> $keep
                        WITH source, r, old, type(r) AS rt, properties(r) AS rp
                        MERGE (source)-[nr:RELATES_TO]->(keep)
                        SET nr = rp
                        DELETE r
                        """,
                        {"merge": merge_id, "keep": keep_id},
                    )

                    # 5. Удаляем merge_id
                    await session.run(
                        "MATCH (n) WHERE n.id = $id DETACH DELETE n",
                        {"id": merge_id},
                    )

                    return True
        except Exception as e:
            logger.error(f"❌ merge_nodes error {keep_id}+{merge_id}: {e}")
            return False


# Singleton
_global_builder: Optional[GraphBuilder] = None


async def get_graph_builder() -> GraphBuilder:
    """Получить глобальный экземпляр GraphBuilder"""
    global _global_builder
    if _global_builder is None:
        _global_builder = GraphBuilder()
        await _global_builder.connect()
    return _global_builder


def reset_global_builder():
    """Сбросить глобальный builder"""
    global _global_builder
    _global_builder = None



