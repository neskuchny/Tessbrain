# -*- coding: utf-8 -*-
"""
TESSENT BRAIN - Night Processor
===============================

Ночной процессор — "спящий мозг" системы.

Работает в фоновом режиме (обычно ночью) и выполняет:
1. Глубокий анализ графа знаний
2. Поиск скрытых связей между сущностями
3. Генерация инсайтов и рекомендаций
4. Обновление кластеров и категорий
5. Переоценка важности узлов
6. Предсказание рисков и возможностей

Это аналог "консолидации памяти" во время сна у человека.
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from backend.core.llm.usage_tracker import UsageContext

logger = logging.getLogger(__name__)


class InsightType(str, Enum):
    """Типы инсайтов"""
    CONNECTION = "connection"       # Скрытая связь между сущностями
    PATTERN = "pattern"             # Обнаруженный паттерн
    RECOMMENDATION = "recommendation"  # Рекомендация
    RISK = "risk"                   # Потенциальный риск
    OPPORTUNITY = "opportunity"     # Возможность
    ANOMALY = "anomaly"             # Аномалия
    TREND = "trend"                 # Тренд


class InsightPriority(str, Enum):
    """Приоритет инсайта"""
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class NightInsight:
    """Инсайт, обнаруженный ночным процессором"""
    insight_id: str
    insight_type: InsightType
    priority: InsightPriority

    # Описание
    title: str
    description: str

    # Связанные сущности
    entities: List[Dict[str, str]] = field(default_factory=list)

    # Доказательства / обоснование
    evidence: List[str] = field(default_factory=list)

    # Рекомендуемые действия
    actions: List[str] = field(default_factory=list)

    # Уверенность (0-1)
    confidence: float = 0.5

    # Временные метки
    discovered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: Optional[datetime] = None

    # Статус
    is_read: bool = False
    is_actionable: bool = True

    # Метаданные
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "insight_id": self.insight_id,
            "insight_type": self.insight_type.value,
            "priority": self.priority.value,
            "title": self.title,
            "description": self.description,
            "entities": self.entities,
            "evidence": self.evidence,
            "actions": self.actions,
            "confidence": self.confidence,
            "discovered_at": self.discovered_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "is_read": self.is_read,
            "is_actionable": self.is_actionable,
            "metadata": self.metadata
        }


@dataclass
class NightProcessingResult:
    """Результат ночной обработки"""
    session_id: str
    started_at: datetime
    completed_at: datetime

    # Найденные инсайты
    insights: List[NightInsight] = field(default_factory=list)

    # Статистика
    nodes_analyzed: int = 0
    edges_analyzed: int = 0
    patterns_found: int = 0
    connections_discovered: int = 0

    # Ошибки
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "duration_seconds": (self.completed_at - self.started_at).total_seconds(),
            "insights": [i.to_dict() for i in self.insights],
            "stats": {
                "nodes_analyzed": self.nodes_analyzed,
                "edges_analyzed": self.edges_analyzed,
                "patterns_found": self.patterns_found,
                "connections_discovered": self.connections_discovered,
                "total_insights": len(self.insights)
            },
            "errors": self.errors
        }


class NightProcessor:
    """
    Ночной процессор для глубокого анализа данных.

    Запускается периодически (обычно ночью) и выполняет
    ресурсоёмкие операции анализа.

    Пример использования:
    ```python
    processor = NightProcessor(graph_builder=graph, llm_router=llm)

    # Запустить полную ночную обработку
    result = await processor.run_night_processing()

    # Получить инсайты
    insights = result.insights
    ```
    """

    def __init__(
        self,
        graph_builder=None,
        vector_indexer=None,
        llm_router=None,
        entity_resolver=None,
        user_id: Optional[str] = None,
    ):
        """
        Args:
            graph_builder: GraphBuilder для доступа к графу
            vector_indexer: VectorIndexer для семантического анализа
            llm_router: LLM роутер для генерации инсайтов
            entity_resolver: EntityResolver для дедупликации
            user_id: per-user namespace для чтения EventLog в team-misalignment.
                ИСПРАВЛЕНИЕ аудита F2: раньше читался несуществующий
                graph.user_id → детектор раскола ВСЕГДА был пуст в проде.
        """
        self.graph = graph_builder
        self.vectors = vector_indexer
        self.llm = llm_router
        self.resolver = entity_resolver
        self.user_id = user_id

        # История обработок
        self._processing_history: List[NightProcessingResult] = []

        # Кеш инсайтов
        self._insights_cache: List[NightInsight] = []

        # Счётчик сессий
        self._session_counter = 0

    def _generate_session_id(self) -> str:
        """Генерировать ID сессии"""
        self._session_counter += 1
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        return f"night_{timestamp}_{self._session_counter}"

    def _generate_insight_id(self, insight_type: InsightType) -> str:
        """Генерировать ID инсайта"""
        import hashlib
        timestamp = datetime.now(timezone.utc).isoformat()
        raw = f"{insight_type.value}_{timestamp}"
        return hashlib.md5(raw.encode()).hexdigest()[:10]

    async def run_night_processing(self) -> NightProcessingResult:
        """
        Запустить полную ночную обработку.

        Выполняет все виды анализа последовательно.
        """
        session_id = self._generate_session_id()
        started_at = datetime.now(timezone.utc)

        logger.info(f"🌙 Starting night processing session: {session_id}")

        insights: List[NightInsight] = []
        errors: List[str] = []
        stats = {
            "nodes_analyzed": 0,
            "edges_analyzed": 0,
            "patterns_found": 0,
            "connections_discovered": 0
        }

        # Получаем граф
        graph_data = None
        if self.graph:
            if hasattr(self.graph, 'nx_graph') and self.graph.nx_graph:
                graph_data = self.graph.nx_graph

        if not graph_data:
            errors.append("No graph available for analysis")
            return NightProcessingResult(
                session_id=session_id,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
                insights=[],
                errors=errors
            )

        stats["nodes_analyzed"] = graph_data.number_of_nodes()
        stats["edges_analyzed"] = graph_data.number_of_edges()

        # 1. Поиск скрытых связей
        try:
            connection_insights = await self._find_hidden_connections(graph_data)
            insights.extend(connection_insights)
            stats["connections_discovered"] = len(connection_insights)
        except Exception as e:
            errors.append(f"Connection analysis failed: {e}")
            logger.error(f"Connection analysis error: {e}")

        # 2. Обнаружение паттернов
        try:
            pattern_insights = await self._detect_patterns(graph_data)
            insights.extend(pattern_insights)
            stats["patterns_found"] = len(pattern_insights)
        except Exception as e:
            errors.append(f"Pattern detection failed: {e}")
            logger.error(f"Pattern detection error: {e}")

        # 3. Анализ активности
        try:
            activity_insights = await self._analyze_activity(graph_data)
            insights.extend(activity_insights)
        except Exception as e:
            errors.append(f"Activity analysis failed: {e}")
            logger.error(f"Activity analysis error: {e}")

        # 3a. Раскол между командами (CAPABILITY_READINESS №7 проактивно):
        # позиции команд из событий → межкомандные пары → LLM-судья →
        # RISK-инсайты в этот же stream. Best-effort, не роняет ночь.
        try:
            misalignment_insights = await self._detect_team_misalignments()
            insights.extend(misalignment_insights)
        except Exception as e:
            errors.append(f"Team misalignment detection failed: {e}")
            logger.error(f"Team misalignment error: {e}")

        # 4. Генерация рекомендаций через LLM
        if self.llm:
            try:
                llm_insights = await self._generate_llm_insights(graph_data, insights)
                insights.extend(llm_insights)
            except Exception as e:
                errors.append(f"LLM insight generation failed: {e}")
                logger.error(f"LLM insight error: {e}")

        # 5. Дедупликация сущностей
        if self.resolver:
            try:
                dedup_insights = await self._run_deduplication()
                insights.extend(dedup_insights)
            except Exception as e:
                errors.append(f"Deduplication failed: {e}")
                logger.error(f"Deduplication error: {e}")

        completed_at = datetime.now(timezone.utc)

        result = NightProcessingResult(
            session_id=session_id,
            started_at=started_at,
            completed_at=completed_at,
            insights=insights,
            nodes_analyzed=stats["nodes_analyzed"],
            edges_analyzed=stats["edges_analyzed"],
            patterns_found=stats["patterns_found"],
            connections_discovered=stats["connections_discovered"],
            errors=errors
        )

        # Сохраняем в историю
        self._processing_history.append(result)
        self._insights_cache.extend(insights)

        duration = (completed_at - started_at).total_seconds()
        logger.info(f"🌙 Night processing completed in {duration:.1f}s. Found {len(insights)} insights.")

        return result

    async def _find_hidden_connections(self, graph_data) -> List[NightInsight]:
        """
        Найти скрытые связи между сущностями.

        Ищет людей, которые работают над похожими темами,
        но не связаны напрямую.
        """
        insights = []

        # Собираем людей и их проекты/темы
        person_topics: Dict[str, set] = {}
        person_names: Dict[str, str] = {}

        for node_id, data in graph_data.nodes(data=True):
            if data.get("_label") == "Person":
                person_id = node_id
                person_names[person_id] = data.get("name", node_id)
                topics = set()

                # Собираем темы через связи
                for _, target, _ in graph_data.out_edges(node_id, data=True):
                    target_data = graph_data.nodes[target]
                    label = target_data.get("_label", "")

                    if label == "Project":
                        topics.add(f"project:{target_data.get('name', target)}")
                    elif label == "Topic":
                        topics.add(f"topic:{target_data.get('name', target)}")
                    elif label == "Technology":
                        topics.add(f"tech:{target_data.get('name', target)}")

                if topics:
                    person_topics[person_id] = topics

        # Ищем людей с пересекающимися интересами, но без прямой связи
        persons = list(person_topics.keys())
        for i, p1 in enumerate(persons):
            for p2 in persons[i+1:]:
                # Проверяем есть ли прямая связь
                has_direct = graph_data.has_edge(p1, p2) or graph_data.has_edge(p2, p1)

                if not has_direct:
                    # Проверяем пересечение тем
                    common_topics = person_topics[p1] & person_topics[p2]

                    if len(common_topics) >= 2:  # Минимум 2 общие темы
                        insight = NightInsight(
                            insight_id=self._generate_insight_id(InsightType.CONNECTION),
                            insight_type=InsightType.CONNECTION,
                            priority=InsightPriority.MEDIUM,
                            title=f"Потенциальная связь: {person_names[p1]} и {person_names[p2]}",
                            description="Эти люди работают над похожими темами, но не взаимодействуют напрямую.",
                            entities=[
                                {"type": "person", "id": p1, "name": person_names[p1]},
                                {"type": "person", "id": p2, "name": person_names[p2]}
                            ],
                            evidence=[f"Общие темы: {', '.join(common_topics)}"],
                            actions=[
                                f"Рассмотреть возможность сотрудничества между {person_names[p1]} и {person_names[p2]}",
                                "Организовать встречу для обмена опытом"
                            ],
                            confidence=min(len(common_topics) * 0.2, 0.9)
                        )
                        insights.append(insight)

        return insights

    async def _detect_patterns(self, graph_data) -> List[NightInsight]:
        """
        Обнаружить паттерны в данных.

        Ищет:
        - Часто встречающиеся комбинации
        - Цепочки связей
        - Кластеры активности
        """
        insights = []

        # Паттерн 1: Люди, которые часто встречаются вместе
        meeting_participants: Dict[str, set] = {}

        for node_id, data in graph_data.nodes(data=True):
            if data.get("_label") == "Meeting":
                participants = set()

                # Входящие связи (участники)
                for source, _, _ in graph_data.in_edges(node_id, data=True):
                    source_data = graph_data.nodes[source]
                    if source_data.get("_label") == "Person":
                        participants.add(source_data.get("name", source))

                if len(participants) >= 2:
                    meeting_participants[node_id] = participants

        # Считаем частоту пар
        pair_counts: Dict[Tuple[str, str], int] = {}
        for participants in meeting_participants.values():
            participants_list = sorted(participants)
            for i, p1 in enumerate(participants_list):
                for p2 in participants_list[i+1:]:
                    pair = (p1, p2)
                    pair_counts[pair] = pair_counts.get(pair, 0) + 1

        # Находим частые пары (>= 3 встреч вместе)
        frequent_pairs = [(pair, count) for pair, count in pair_counts.items() if count >= 3]

        if frequent_pairs:
            # Сортируем по частоте
            frequent_pairs.sort(key=lambda x: -x[1])

            top_pairs = frequent_pairs[:5]
            pairs_text = ", ".join([f"{p[0]} и {p[1]} ({c} встреч)" for (p, c) in top_pairs])

            insight = NightInsight(
                insight_id=self._generate_insight_id(InsightType.PATTERN),
                insight_type=InsightType.PATTERN,
                priority=InsightPriority.LOW,
                title="Обнаружены устойчивые рабочие связи",
                description=f"Найдены люди, которые регулярно встречаются вместе: {pairs_text}",
                entities=[
                    {"type": "pair", "names": list(pair), "meetings": count}
                    for pair, count in top_pairs
                ],
                evidence=[f"Анализ {len(meeting_participants)} встреч"],
                actions=["Рассмотреть формализацию этих рабочих групп"],
                confidence=0.8
            )
            insights.append(insight)

        # Паттерн 2: Проекты без активности
        inactive_projects = []
        for node_id, data in graph_data.nodes(data=True):
            if data.get("_label") == "Project":
                # Считаем связи
                in_degree = graph_data.in_degree(node_id)
                out_degree = graph_data.out_degree(node_id)

                if in_degree + out_degree < 3:  # Мало связей
                    inactive_projects.append(data.get("name", node_id))

        if inactive_projects:
            insight = NightInsight(
                insight_id=self._generate_insight_id(InsightType.PATTERN),
                insight_type=InsightType.PATTERN,
                priority=InsightPriority.MEDIUM,
                title="Проекты с низкой активностью",
                description=f"Обнаружены проекты с минимальной активностью: {', '.join(inactive_projects[:5])}",
                entities=[{"type": "project", "name": p} for p in inactive_projects[:5]],
                evidence=["Анализ связей в графе знаний"],
                actions=["Проверить статус этих проектов", "Рассмотреть закрытие или реактивацию"],
                confidence=0.7
            )
            insights.append(insight)

        return insights

    async def _analyze_activity(self, graph_data) -> List[NightInsight]:
        """
        Анализ активности в системе.

        Ищет:
        - Самых активных участников
        - Тренды активности
        - Узкие места
        """
        insights = []

        # Подсчёт активности по людям
        person_activity: Dict[str, Dict] = {}

        for node_id, data in graph_data.nodes(data=True):
            if data.get("_label") == "Person":
                name = data.get("name", node_id)

                meetings = 0
                tasks = 0
                decisions = 0

                for _, target, _edge_data in graph_data.out_edges(node_id, data=True):
                    target_data = graph_data.nodes[target]
                    label = target_data.get("_label", "")

                    if label == "Meeting":
                        meetings += 1
                    elif label == "Task":
                        tasks += 1
                    elif label == "Decision":
                        decisions += 1

                person_activity[name] = {
                    "meetings": meetings,
                    "tasks": tasks,
                    "decisions": decisions,
                    "total": meetings + tasks + decisions * 2
                }

        if person_activity:
            # Топ активных
            sorted_activity = sorted(
                person_activity.items(),
                key=lambda x: -x[1]["total"]
            )

            top_active = sorted_activity[:5]

            if top_active and top_active[0][1]["total"] > 5:
                insight = NightInsight(
                    insight_id=self._generate_insight_id(InsightType.TREND),
                    insight_type=InsightType.TREND,
                    priority=InsightPriority.LOW,
                    title="Самые активные участники",
                    description=f"Лидеры по активности: {', '.join([p[0] for p in top_active])}",
                    entities=[
                        {"type": "person", "name": name, "activity": stats}
                        for name, stats in top_active
                    ],
                    evidence=["Анализ участия во встречах, задачах и решениях"],
                    actions=[],
                    confidence=0.9
                )
                insights.append(insight)

            # Проверяем дисбаланс нагрузки
            if len(sorted_activity) >= 3:
                top_total = sorted_activity[0][1]["total"]
                avg_total = sum(p[1]["total"] for p in sorted_activity) / len(sorted_activity)

                if top_total > avg_total * 3:  # Топ участник в 3 раза активнее среднего
                    insight = NightInsight(
                        insight_id=self._generate_insight_id(InsightType.RISK),
                        insight_type=InsightType.RISK,
                        priority=InsightPriority.MEDIUM,
                        title="Дисбаланс нагрузки",
                        description=f"{sorted_activity[0][0]} значительно активнее остальных. Возможен риск выгорания или зависимости от одного человека.",
                        entities=[{"type": "person", "name": sorted_activity[0][0]}],
                        evidence=[f"Активность {sorted_activity[0][0]}: {top_total}, средняя: {avg_total:.1f}"],
                        actions=[
                            "Рассмотреть перераспределение задач",
                            "Проверить нагрузку ключевых сотрудников"
                        ],
                        confidence=0.75
                    )
                    insights.append(insight)

        return insights

    async def _detect_team_misalignments(self) -> List[NightInsight]:
        """C-glue (CAPABILITY_READINESS №7 проактивно): межкомандный раскол.

        Конвейер:
          события decision_made → glue (positions_from_event_log + карта
          person→team из графа) → team_misalignment.detect (скелет-пары +
          LLM-судья) → NightInsight'ы со статусом=conflict.
        Best-effort на каждом шаге; нет данных/нет ключей/нет графа → пусто.
        """
        out: List[NightInsight] = []
        try:
            from backend.core.think.team_misalignment import detect_team_misalignment
            from backend.core.think.team_positions_glue import positions_from_event_log
        except Exception as exc:
            logger.debug("team_misalignment unavailable: %s", exc)
            return out

        # Карта person→team из графа (best-effort; без графа — выходим)
        person_to_team = await self._build_person_to_team_map()
        if not person_to_team:
            logger.debug("team misalignment: empty person→team map, skipping")
            return out

        # Источник событий — best-effort: per-user EventLog по user_id.
        # F2-fix: берём self.user_id (передаётся в конструктор), с запасным
        # чтением атрибута графа для обратной совместимости.
        events: List[dict] = []
        try:
            uid = (self.user_id
                   or getattr(self.graph, "user_id", None)
                   or getattr(self.graph, "_user_id", None))
            if uid:
                from backend.core.memory.event_log import EventLog
                from backend.core.store.tenant_paths import event_log_path_for_user
                store = EventLog(persist_path=event_log_path_for_user(uid))
                events = [e if isinstance(e, dict) else e.to_dict()
                          for e in store.events(kind="decision_made")]
        except Exception as exc:
            logger.debug("EventLog read for misalignment skipped: %s", exc)

        if not events:
            return out

        positions, stats = positions_from_event_log(events, person_to_team)
        logger.info("Team misalignment: %d positions resolved, %d unresolved",
                    stats["resolved"], stats["unresolved"])
        if len(positions) < 2:
            return out

        # LLM-судья опционален: без него получаем 'unverified' пары — в инсайты
        # такое не шлём (CRITICAL должны быть верифицированы)
        if self.llm is None:
            return out

        def _judge_sync(system: str, user: str) -> str:
            # team_misalignment ждёт sync-callable; LLMRouter — async.
            # В ночи мы уже в async-контексте → берём существующий loop через
            # asyncio.run_coroutine_threadsafe нельзя, поэтому делаем blocking
            # call через nest_asyncio-стиль НЕ нужен: достаточно вызвать
            # сам детектор в thread'е (ниже через to_thread).
            import asyncio as _aio
            return _aio.run(self.llm.generate(
                prompt=user, system_prompt=system, temperature=0.0, max_tokens=600)) or ""

        import asyncio as _aio
        try:
            findings = await _aio.to_thread(
                detect_team_misalignment, positions, _judge_sync,
                min_topic_overlap=1)
        except Exception as exc:
            logger.error("team_misalignment.detect failed: %s", exc)
            return out

        for f in findings:
            if f.status != "conflict":
                continue
            iid = self._generate_insight_id(InsightType.RISK)
            severity_priority = {
                "high": InsightPriority.CRITICAL,
                "medium": InsightPriority.HIGH,
                "low": InsightPriority.MEDIUM,
            }.get(f.severity, InsightPriority.HIGH)
            out.append(NightInsight(
                insight_id=iid,
                insight_type=InsightType.RISK,
                priority=severity_priority,
                title=f"Раскол между командами: {f.team_a} ↔ {f.team_b}",
                description=(f"«{f.statement_a}» (команда {f.team_a}) против "
                             f"«{f.statement_b}» (команда {f.team_b}). "
                             f"{f.explanation}"),
                entities=[{"type": "team", "name": f.team_a},
                          {"type": "team", "name": f.team_b}],
                evidence=list(f.sources),
                actions=[f"Согласовать позиции команд {f.team_a} и {f.team_b}",
                         "Поднять на встрече руководителей"],
                confidence=0.7,
                metadata={"topic_overlap": list(f.topic_overlap),
                          "severity": f.severity},
            ))
        return out

    async def _build_person_to_team_map(self) -> Dict[str, str]:
        """Карта person→team из графа: Person→Team через MEMBER_OF.
        Best-effort: при отсутствии графа/связей возвращает {}."""
        result: Dict[str, str] = {}
        if not self.graph:
            return result
        try:
            persons = await self.graph.get_all_nodes_async(label="Person")
        except Exception:
            return result
        for p in persons or []:
            pid = p.get("id") or ""
            pname = p.get("name") or ""
            if not pid:
                continue
            try:
                rels = await self.graph.get_node_relationships(pid)
            except Exception:
                continue
            for rel in (rels.get("outgoing", []) + rels.get("incoming", [])):
                target_id = rel.get("target") or rel.get("source") or ""
                if not target_id:
                    continue
                try:
                    other = await self.graph.get_node_by_id(target_id)
                except Exception:
                    continue
                if not other or other.get("_label") != "Team":
                    continue
                tname = other.get("name") or ""
                if not tname:
                    continue
                for key in (pname, pid):
                    if key:
                        result[key] = tname
                break  # одна команда на person — берём первую
        return result

    async def _generate_llm_insights(
        self,
        graph_data,
        existing_insights: List[NightInsight]
    ) -> List[NightInsight]:
        """
        Генерация инсайтов с помощью LLM.

        Отправляет сводку данных в LLM для генерации
        высокоуровневых рекомендаций.
        """
        insights = []

        if not self.llm:
            return insights

        # Собираем сводку для LLM
        summary = {
            "total_nodes": graph_data.number_of_nodes(),
            "total_edges": graph_data.number_of_edges(),
            "persons": [],
            "projects": [],
            "recent_decisions": []
        }

        for _node_id, data in graph_data.nodes(data=True):
            label = data.get("_label", "")
            if label == "Person":
                summary["persons"].append(data.get("name", ""))
            elif label == "Project":
                summary["projects"].append(data.get("name", ""))
            elif label == "Decision":
                summary["recent_decisions"].append(data.get("summary", "")[:100])

        # Ограничиваем для промпта
        summary["persons"] = summary["persons"][:20]
        summary["projects"] = summary["projects"][:10]
        summary["recent_decisions"] = summary["recent_decisions"][:5]

        # Существующие инсайты для контекста
        existing_summary = [
            f"- {i.title}: {i.description[:100]}"
            for i in existing_insights[:5]
        ]

        prompt = f"""Проанализируй данные компании и дай 1-2 стратегических рекомендации.

Данные:
- Людей в системе: {len(summary['persons'])} ({', '.join(summary['persons'][:10])})
- Проектов: {len(summary['projects'])} ({', '.join(summary['projects'][:5])})
- Недавние решения: {len(summary['recent_decisions'])}

Уже обнаруженные инсайты:
{chr(10).join(existing_summary) if existing_summary else 'Нет'}

Дай 1-2 стратегических рекомендации для улучшения работы компании — СТРОГО на
основе данных выше. Каждая рекомендация должна опираться на конкретный факт/
сигнал из этих данных (упомяни, на чём она основана). Не выдумывай проблемы,
метрики и общие советы «из учебника», не привязанные к этой компании. Если данных
мало для содержательной рекомендации — верни пустой массив [].
Формат ответа - JSON массив (плейсхолдеры — формат, не данные):
[{{"title": "Заголовок", "description": "Описание", "priority": "high/medium/low", "actions": ["действие1", "действие2"]}}]"""

        try:
            # Трекинг использования LLM для ночного процессора
            async with UsageContext(agent_mode="night_processor", request_type="strategic_recommendations"):
                response = await self.llm.generate(
                    prompt=prompt,
                    temperature=0.7,
                    max_tokens=500
                )

            text = response.get("text", "") if isinstance(response, dict) else str(response)

            # Парсим JSON
            import re
            json_match = re.search(r'\[.*\]', text, re.DOTALL)
            if json_match:
                recommendations = json.loads(json_match.group())

                for rec in recommendations[:2]:
                    priority_map = {
                        "high": InsightPriority.HIGH,
                        "medium": InsightPriority.MEDIUM,
                        "low": InsightPriority.LOW
                    }

                    insight = NightInsight(
                        insight_id=self._generate_insight_id(InsightType.RECOMMENDATION),
                        insight_type=InsightType.RECOMMENDATION,
                        priority=priority_map.get(rec.get("priority", "medium"), InsightPriority.MEDIUM),
                        title=rec.get("title", "Рекомендация"),
                        description=rec.get("description", ""),
                        entities=[],
                        evidence=["Анализ LLM на основе данных графа знаний"],
                        actions=rec.get("actions", []),
                        confidence=0.6,
                        metadata={"source": "llm"}
                    )
                    insights.append(insight)

        except Exception as e:
            logger.error(f"LLM insight generation error: {e}")

        return insights

    async def _run_deduplication(self) -> List[NightInsight]:
        """Запустить дедупликацию и создать инсайты о найденных дубликатах"""
        insights = []

        if not self.resolver:
            return insights

        try:
            # Используем LLM-based поиск дубликатов
            duplicates = await self.resolver.find_duplicates_llm(
                entity_type="Person",
                batch_size=10
            )

            if duplicates:
                for dup in duplicates:
                    e1 = dup.get("entity1", {})
                    e2 = dup.get("entity2", {})
                    confidence = dup.get("confidence", 0)
                    reasoning = dup.get("reasoning", "")

                    insight = NightInsight(
                        insight_id=self._generate_insight_id(InsightType.ANOMALY),
                        insight_type=InsightType.ANOMALY,
                        priority=InsightPriority.MEDIUM if confidence > 0.8 else InsightPriority.LOW,
                        title=f"Возможный дубликат: {e1.get('name')} = {e2.get('name')}",
                        description=f"LLM определил, что это может быть один и тот же человек. {reasoning}",
                        entities=[
                            {"type": "person", "id": e1.get("id"), "name": e1.get("name")},
                            {"type": "person", "id": e2.get("id"), "name": e2.get("name")}
                        ],
                        evidence=[f"LLM confidence: {confidence:.0%}", reasoning],
                        actions=["Подтвердить слияние сущностей", "Проверить данные вручную"],
                        confidence=confidence
                    )
                    insights.append(insight)

        except Exception as e:
            logger.error(f"Deduplication error: {e}")

        return insights

    def get_unread_insights(self) -> List[NightInsight]:
        """Получить непрочитанные инсайты"""
        return [i for i in self._insights_cache if not i.is_read]

    def mark_insight_read(self, insight_id: str) -> bool:
        """Отметить инсайт как прочитанный"""
        for insight in self._insights_cache:
            if insight.insight_id == insight_id:
                insight.is_read = True
                return True
        return False

    def get_insights_by_priority(self, priority: InsightPriority) -> List[NightInsight]:
        """Получить инсайты по приоритету"""
        return [i for i in self._insights_cache if i.priority == priority]

    def get_processing_history(self) -> List[Dict[str, Any]]:
        """Получить историю обработок"""
        return [r.to_dict() for r in self._processing_history]

    def get_stats(self) -> Dict[str, Any]:
        """Получить статистику процессора"""
        return {
            "total_sessions": len(self._processing_history),
            "total_insights": len(self._insights_cache),
            "unread_insights": len(self.get_unread_insights()),
            "insights_by_type": {
                t.value: len([i for i in self._insights_cache if i.insight_type == t])
                for t in InsightType
            }
        }

