# -*- coding: utf-8 -*-
"""
Logical Form Planner - планирование шагов рассуждения.

Преобразует QueryIntent в план выполнения:
- Определяет последовательность шагов
- Генерирует запросы к графу
- Определяет источники данных для каждого шага
"""
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from .query_analyzer import QueryIntent, QueryType

logger = logging.getLogger(__name__)


class StepType(Enum):
    """Типы шагов в плане рассуждения"""
    # Поиск
    GRAPH_QUERY = "graph_query"           # Запрос к графу
    VECTOR_SEARCH = "vector_search"       # Векторный поиск
    HYBRID_SEARCH = "hybrid_search"       # Гибридный поиск

    # Обработка
    AGGREGATE = "aggregate"               # Агрегация данных
    FILTER = "filter"                     # Фильтрация
    SORT = "sort"                         # Сортировка

    # Анализ
    ANALYZE = "analyze"                   # LLM анализ
    COMPARE = "compare"                   # Сравнение
    DETECT_ANOMALY = "detect_anomaly"     # Поиск аномалий

    # Синтез
    SYNTHESIZE = "synthesize"             # Синтез ответа
    VERIFY = "verify"                     # Верификация


class SearchStrategy(str, Enum):
    """Стратегии поиска"""
    QUICK = "quick"        # Быстрый (1-2 сек) - только векторный поиск
    STANDARD = "standard"  # Стандартный (3-5 сек) - вектор + граф (1-2 хопа)
    DEEP = "deep"          # Глубокий (15-30 сек) - многошаговый анализ

@dataclass
class ReasoningStep:
    """Шаг в плане рассуждения"""
    step_id: str
    step_type: StepType
    description: str

    # Параметры шага
    params: Dict[str, Any] = field(default_factory=dict)

    # Зависимости от других шагов
    depends_on: List[str] = field(default_factory=list)

    # Результат (заполняется при выполнении)
    result: Optional[Any] = None
    status: str = "pending"  # pending, running, completed, failed

    # Метаданные
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ReasoningPlan:
    """План рассуждения"""
    plan_id: str
    query_intent: QueryIntent

    # Шаги плана
    steps: List[ReasoningStep] = field(default_factory=list)

    # Текущий статус
    status: str = "created"  # created, executing, completed, failed

    # Результаты
    final_result: Optional[Dict[str, Any]] = None

    # Метаданные
    created_at: str = ""
    completed_at: Optional[str] = None

    def get_next_steps(self) -> List[ReasoningStep]:
        """Получить следующие шаги для выполнения"""
        ready_steps = []

        for step in self.steps:
            if step.status != "pending":
                continue

            # Проверяем зависимости
            deps_completed = all(
                self._get_step(dep_id).status == "completed"
                for dep_id in step.depends_on
                if self._get_step(dep_id)
            )

            if deps_completed:
                ready_steps.append(step)

        return ready_steps

    def _get_step(self, step_id: str) -> Optional[ReasoningStep]:
        """Получить шаг по ID"""
        for step in self.steps:
            if step.step_id == step_id:
                return step
        return None

    def is_completed(self) -> bool:
        """Проверить завершён ли план"""
        return all(step.status in ("completed", "failed") for step in self.steps)


class LogicalFormPlanner:
    """
    Планировщик логических форм.

    Создаёт план рассуждения на основе QueryIntent.

    Пример:
    ```python
    planner = LogicalFormPlanner()

    intent = QueryIntent(query_type=QueryType.PROJECT_STATUS, ...)
    plan = planner.create_plan(intent)

    for step in plan.steps:
        logger.info(f"{step.step_id}: {step.description}")
    ```
    """

    # Шаблоны планов для разных типов запросов (STANDARD стратегия)
    STANDARD_TEMPLATES = {
        QueryType.PROJECT_STATUS: [
            {
                "step_type": StepType.GRAPH_QUERY,
                "description": "Найти проект в графе",
                "params": {
                    "node_type": "Entity",
                    "filter": {"entity_type": "project"},
                    "match_field": "name"
                }
            },
            {
                "step_type": StepType.GRAPH_QUERY,
                "description": "Получить связанные задачи",
                "params": {
                    "query_template": "tasks_for_project",
                    "relationship": "MENTIONED_IN"
                },
                "depends_on": ["step_1"]
            },
            {
                "step_type": StepType.GRAPH_QUERY,
                "description": "Получить связанные решения",
                "params": {
                    "query_template": "decisions_for_project",
                    "relationship": "MENTIONED_IN"
                },
                "depends_on": ["step_1"]
            },
            {
                "step_type": StepType.GRAPH_QUERY,
                "description": "Получить участников",
                "params": {
                    "query_template": "participants_for_project"
                },
                "depends_on": ["step_1"]
            },
            {
                "step_type": StepType.VECTOR_SEARCH,
                "description": "Найти релевантные фрагменты встреч",
                "params": {
                    "collections": ["tessent_meetings", "tessent_decisions"],
                    "limit": 5
                },
                "depends_on": ["step_1"]
            },
            {
                "step_type": StepType.AGGREGATE,
                "description": "Агрегировать данные о проекте",
                "params": {
                    "aggregate_type": "project_summary"
                },
                "depends_on": ["step_2", "step_3", "step_4", "step_5"]
            },
            {
                "step_type": StepType.SYNTHESIZE,
                "description": "Сформировать ответ о статусе проекта",
                "params": {
                    "template": "project_status_response"
                },
                "depends_on": ["step_6"]
            }
        ],

        QueryType.PERSON_STATUS: [
            {
                "step_type": StepType.GRAPH_QUERY,
                "description": "Найти человека и весь его контекст",
                "params": {
                    "node_type": "Person",
                    "query_subtype": "person_context",
                    "include_relationships": True
                }
            },
            {
                "step_type": StepType.AGGREGATE,
                "description": "Агрегировать данные о человеке",
                "params": {
                    "aggregate_type": "person_summary"
                },
                "depends_on": ["step_1"]
            },
            {
                "step_type": StepType.SYNTHESIZE,
                "description": "Сформировать ответ о человеке",
                "params": {
                    "template": "person_status_response"
                },
                "depends_on": ["step_2"]
            }
        ],

        QueryType.TASK_STATUS: [
            {
                "step_type": StepType.GRAPH_QUERY,
                "description": "Получить все задачи из графа",
                "params": {
                    "node_type": "Task",
                    "query_subtype": "all_tasks",
                    "limit": 50
                }
            },
            {
                "step_type": StepType.AGGREGATE,
                "description": "Агрегировать данные о задачах",
                "params": {
                    "aggregate_type": "group_by_type"
                },
                "depends_on": ["step_1"]
            },
            {
                "step_type": StepType.SYNTHESIZE,
                "description": "Сформировать ответ о задачах",
                "params": {
                    "template": "tasks_list_response"
                },
                "depends_on": ["step_2"]
            }
        ],

        QueryType.SEARCH_DECISIONS: [
            {
                "step_type": StepType.HYBRID_SEARCH,
                "description": "Поиск решений",
                "params": {
                    "node_type": "Decision",
                    "collections": ["tessent_decisions"],
                    "limit": 10
                }
            },
            {
                "step_type": StepType.GRAPH_QUERY,
                "description": "Получить контекст решений",
                "params": {
                    "query_template": "decision_context",
                    "include_relationships": True
                },
                "depends_on": ["step_1"]
            },
            {
                "step_type": StepType.SORT,
                "description": "Отсортировать по релевантности",
                "params": {
                    "sort_by": "relevance",
                    "order": "desc"
                },
                "depends_on": ["step_2"]
            },
            {
                "step_type": StepType.SYNTHESIZE,
                "description": "Сформировать список решений",
                "params": {
                    "template": "decisions_list_response"
                },
                "depends_on": ["step_3"]
            }
        ],

        QueryType.SEARCH_TASKS: [
            {
                "step_type": StepType.GRAPH_QUERY,
                "description": "Получить все задачи из графа",
                "params": {
                    "node_type": "Task",
                    "query_subtype": "all_tasks",
                    "limit": 50
                }
            },
            {
                "step_type": StepType.AGGREGATE,
                "description": "Агрегировать данные о задачах",
                "params": {
                    "aggregate_type": "group_by_type"
                },
                "depends_on": ["step_1"]
            },
            {
                "step_type": StepType.SYNTHESIZE,
                "description": "Сформировать список задач",
                "params": {
                    "template": "tasks_list_response"
                },
                "depends_on": ["step_2"]
            }
        ],

        QueryType.COMPARE: [
            {
                "step_type": StepType.GRAPH_QUERY,
                "description": "Найти первую сущность",
                "params": {
                    "entity_index": 0
                }
            },
            {
                "step_type": StepType.GRAPH_QUERY,
                "description": "Найти вторую сущность",
                "params": {
                    "entity_index": 1
                }
            },
            {
                "step_type": StepType.GRAPH_QUERY,
                "description": "Получить данные первой сущности",
                "params": {
                    "include_relationships": True
                },
                "depends_on": ["step_1"]
            },
            {
                "step_type": StepType.GRAPH_QUERY,
                "description": "Получить данные второй сущности",
                "params": {
                    "include_relationships": True
                },
                "depends_on": ["step_2"]
            },
            {
                "step_type": StepType.COMPARE,
                "description": "Сравнить сущности",
                "params": {
                    "compare_type": "detailed"
                },
                "depends_on": ["step_3", "step_4"]
            },
            {
                "step_type": StepType.SYNTHESIZE,
                "description": "Сформировать сравнительный отчёт",
                "params": {
                    "template": "comparison_response"
                },
                "depends_on": ["step_5"]
            }
        ],

        QueryType.ANOMALY: [
            {
                "step_type": StepType.GRAPH_QUERY,
                "description": "Получить все задачи",
                "params": {
                    "node_type": "Task"
                }
            },
            {
                "step_type": StepType.GRAPH_QUERY,
                "description": "Получить все решения",
                "params": {
                    "node_type": "Decision"
                }
            },
            {
                "step_type": StepType.DETECT_ANOMALY,
                "description": "Анализ задач на аномалии",
                "params": {
                    "anomaly_types": ["overdue", "stuck", "unassigned"]
                },
                "depends_on": ["step_1"]
            },
            {
                "step_type": StepType.DETECT_ANOMALY,
                "description": "Анализ решений на аномалии",
                "params": {
                    "anomaly_types": ["no_implementation", "conflicting"]
                },
                "depends_on": ["step_2"]
            },
            {
                "step_type": StepType.AGGREGATE,
                "description": "Агрегировать аномалии",
                "params": {
                    "aggregate_type": "anomaly_report"
                },
                "depends_on": ["step_3", "step_4"]
            },
            {
                "step_type": StepType.SYNTHESIZE,
                "description": "Сформировать отчёт об аномалиях",
                "params": {
                    "template": "anomaly_report_response"
                },
                "depends_on": ["step_5"]
            }
        ],

        QueryType.CHANGE: [
            {
                "step_type": StepType.HYBRID_SEARCH,
                "description": "Найти факты и снапшоты об изменениях",
                "params": {
                    "node_type": "Entity",
                    "limit": 12
                }
            },
            {
                "step_type": StepType.VECTOR_SEARCH,
                "description": "Найти текстовые свидетельства изменений",
                "params": {
                    "collections": ["tessent_snapshots", "tessent_propositions", "tessent_topics", "tessent_reports", "tessent_meetings"],
                    "limit": 12
                }
            },
            {
                "step_type": StepType.AGGREGATE,
                "description": "Собрать сводку изменений",
                "params": {
                    "aggregate_type": "overall_summary"
                },
                "depends_on": ["step_1", "step_2"]
            },
            {
                "step_type": StepType.SYNTHESIZE,
                "description": "Сформировать ответ по изменениям",
                "params": {
                    "template": "change_analysis_response"
                },
                "depends_on": ["step_3"]
            }
        ],

        QueryType.AUDIT: [
            {
                "step_type": StepType.GRAPH_QUERY,
                "description": "Получить задачи и обязательства для аудита",
                "params": {
                    "node_type": "Task",
                    "query_subtype": "all_tasks",
                    "limit": 50
                }
            },
            {
                "step_type": StepType.GRAPH_QUERY,
                "description": "Получить решения и связанные артефакты",
                "params": {
                    "node_type": "Decision",
                    "limit": 30
                }
            },
            {
                "step_type": StepType.VECTOR_SEARCH,
                "description": "Найти supporting evidence для аудита",
                "params": {
                    "collections": ["tessent_snapshots", "tessent_reports", "tessent_propositions", "tessent_meetings"],
                    "limit": 12
                }
            },
            {
                "step_type": StepType.DETECT_ANOMALY,
                "description": "Найти риски и нарушения",
                "params": {
                    "anomaly_types": ["overdue", "stuck", "unassigned", "conflicting", "no_implementation"]
                },
                "depends_on": ["step_1", "step_2"]
            },
            {
                "step_type": StepType.AGGREGATE,
                "description": "Собрать аудиторскую картину",
                "params": {
                    "aggregate_type": "anomaly_report"
                },
                "depends_on": ["step_1", "step_2", "step_3", "step_4"]
            },
            {
                "step_type": StepType.SYNTHESIZE,
                "description": "Сформировать аудиторский отчёт",
                "params": {
                    "template": "audit_response"
                },
                "depends_on": ["step_5"]
            }
        ],

        QueryType.DISCOVERY: [
            {
                "step_type": StepType.HYBRID_SEARCH,
                "description": "Найти слабосвязанные, неожиданные и скрытые сигналы",
                "params": {
                    "node_type": "Entity",
                    "limit": 15
                }
            },
            {
                "step_type": StepType.VECTOR_SEARCH,
                "description": "Найти редкие, но релевантные текстовые свидетельства",
                "params": {
                    "collections": ["tessent_propositions", "tessent_topics", "tessent_reports", "tessent_meetings"],
                    "limit": 15
                }
            },
            {
                "step_type": StepType.ANALYZE,
                "description": "Выделить неожиданные инсайты и скрытые возможности",
                "params": {
                    "analyze_type": "discovery"
                },
                "depends_on": ["step_1", "step_2"]
            },
            {
                "step_type": StepType.SYNTHESIZE,
                "description": "Сформировать discovery-ответ",
                "params": {
                    "template": "discovery_response"
                },
                "depends_on": ["step_3"]
            }
        ],

        QueryType.SUMMARY: [
            {
                "step_type": StepType.GRAPH_QUERY,
                "description": "Получить статистику по узлам",
                "params": {
                    "query_template": "graph_statistics"
                }
            },
            {
                "step_type": StepType.GRAPH_QUERY,
                "description": "Получить последние встречи",
                "params": {
                    "node_type": "Meeting",
                    "limit": 5,
                    "sort_by": "created_at"
                }
            },
            {
                "step_type": StepType.GRAPH_QUERY,
                "description": "Получить активные задачи",
                "params": {
                    "node_type": "Task",
                    "filter": {"status": "new"}
                }
            },
            {
                "step_type": StepType.AGGREGATE,
                "description": "Агрегировать общую картину",
                "params": {
                    "aggregate_type": "overall_summary"
                },
                "depends_on": ["step_1", "step_2", "step_3"]
            },
            {
                "step_type": StepType.SYNTHESIZE,
                "description": "Сформировать общий обзор",
                "params": {
                    "template": "summary_response"
                },
                "depends_on": ["step_4"]
            }
        ],

        QueryType.GENERAL: [
            {
                "step_type": StepType.GRAPH_QUERY,
                "description": "Поиск в графе знаний",
                "params": {
                    "node_type": "Entity",
                    "limit": 20
                }
            },
            {
                "step_type": StepType.VECTOR_SEARCH,
                "description": "Семантический поиск",
                "params": {
                    "limit": 10
                }
            },
            {
                "step_type": StepType.ANALYZE,
                "description": "LLM анализ результатов",
                "params": {
                    "analyze_type": "general"
                },
                "depends_on": ["step_1", "step_2"]
            },
            {
                "step_type": StepType.SYNTHESIZE,
                "description": "Сформировать ответ",
                "params": {
                    "template": "general_response"
                },
                "depends_on": ["step_3"]
            }
        ]
    }

    # Шаблоны для QUICK стратегии (только векторный поиск)
    QUICK_TEMPLATES = {
        QueryType.GENERAL: [
            {
                "step_type": StepType.VECTOR_SEARCH,
                "description": "Быстрый поиск по базе знаний",
                "params": {
                    "limit": 5,
                    "collections": ["tessent_meetings", "tessent_decisions", "tessent_tasks"]
                }
            },
            {
                "step_type": StepType.SYNTHESIZE,
                "description": "Сформировать краткий ответ",
                "params": {
                    "template": "quick_response"
                },
                "depends_on": ["step_1"]
            }
        ]
    }

    def __init__(self):
        self._plan_counter = 0

    def create_plan(self, intent: QueryIntent, strategy: SearchStrategy = SearchStrategy.STANDARD) -> ReasoningPlan:
        """
        Создать план рассуждения для запроса.

        Args:
            intent: Намерение пользователя
            strategy: Стратегия поиска (quick/standard/deep)

        Returns:
            ReasoningPlan с шагами выполнения
        """
        self._plan_counter += 1
        plan_id = f"plan_{self._plan_counter}_{datetime.now().strftime('%H%M%S')}"

        # Выбираем набор шаблонов
        if strategy == SearchStrategy.QUICK:
            templates = self.QUICK_TEMPLATES
        else:
            # Для DEEP пока используем STANDARD + (в будущем расширим)
            templates = self.STANDARD_TEMPLATES

        # Получаем шаблон плана
        # Если для конкретного типа нет шаблона в QUICK, используем GENERAL из QUICK
        # Если стратегии STANDARD, ищем конкретный тип, иначе GENERAL

        template = templates.get(intent.query_type)

        if not template:
            # Fallback logic
            if strategy == SearchStrategy.QUICK:
                template = self.QUICK_TEMPLATES[QueryType.GENERAL]
            else:
                template = self.STANDARD_TEMPLATES.get(QueryType.GENERAL)

        # Создаём шаги
        steps = []
        for i, step_template in enumerate(template, 1):
            step_id = f"step_{i}"

            # Копируем параметры
            params = dict(step_template.get("params", {}))

            # Добавляем сущности из intent
            if intent.entities:
                params["entities"] = intent.entities

                # Если есть entity_index — берём конкретную сущность
                if "entity_index" in params:
                    idx = params["entity_index"]
                    if idx < len(intent.entities):
                        params["target_entity"] = intent.entities[idx]

            # Добавляем запрос
            params["query"] = intent.original_query
            params["normalized_query"] = intent.normalized_query
            params["keywords"] = intent.keywords

            # Добавляем временной контекст
            if intent.time_range:
                params["time_range"] = intent.time_range

            step = ReasoningStep(
                step_id=step_id,
                step_type=step_template["step_type"],
                description=step_template["description"],
                params=params,
                depends_on=step_template.get("depends_on", [])
            )

            steps.append(step)

        plan = ReasoningPlan(
            plan_id=plan_id,
            query_intent=intent,
            steps=steps,
            status="created",
            created_at=datetime.now(timezone.utc).isoformat()
        )

        logger.info(
            f"Created plan {plan_id} with {len(steps)} steps "
            f"for query type {intent.query_type.value}"
        )

        return plan

    async def create_deep_plan(self, intent: QueryIntent, llm) -> ReasoningPlan:
        """
        Динамическое создание глубокого плана с помощью LLM.
        """
        self._plan_counter += 1
        plan_id = f"plan_{self._plan_counter}_deep_{datetime.now().strftime('%H%M%S')}"

        entities_names = [e.get('name') for e in intent.entities]

        prompt = f"""Ты — архитектор поисковых стратегий Tessbrain.
Твоя задача — составить пошаговый план поиска информации для ответа на СЛОЖНЫЙ вопрос.

ВОПРОС: "{intent.original_query}"
ТИП ЗАПРОСА: {intent.query_type.value}
СУЩНОСТИ: {entities_names}

ДОСТУПНЫЕ ТИПЫ ШАГОВ (step_type):
1. graph_query: Поиск узлов и связей в графе (проекты, люди, задачи, встречи, решения). Параметры: node_type, limit.
2. vector_search: Семантический поиск по тексту встреч. Параметры: query (если нужно уточнить), limit.
3. aggregate: Агрегация данных (подсчет, группировка).
4. detect_anomaly: Поиск аномалий (риски, просрочки).
5. compare: Сравнение сущностей.
6. synthesize: (ОБЯЗАТЕЛЬНО ПОСЛЕДНИЙ) Формирование финального ответа.

ПРАВИЛА:
1. Разбей вопрос на логические шаги. Если вопрос "Почему проект стоит?", сначала найди статус проекта, потом задачи, потом последние встречи и проблемы.
2. Создай от 3 до 7 шагов.
3. Указывай зависимости (depends_on).

ОТВЕТЬ ТОЛЬКО JSON МАССИВОМ:
[
  {{
    "step_id": "step_1",
    "step_type": "graph_query",
    "description": "Найти проект...",
    "params": {{"node_type": "Project", "filter": {{"name": "..."}}}}
  }},
  {{
    "step_id": "step_2",
    "step_type": "vector_search",
    "description": "Найти обсуждения проблем...",
    "params": {{"query": "проблемы с..."}},
    "depends_on": ["step_1"]
  }}
]"""

        try:
            logger.info("🧠 Generating dynamic DEEP plan...")
            response = await llm.generate(prompt=prompt, temperature=0.2, max_tokens=1000)

            # Парсим JSON
            json_match = re.search(r'\[[\s\S]*\]', response)
            if not json_match:
                logger.warning("No JSON found in LLM response for plan")
                return self.create_plan(intent, strategy=SearchStrategy.STANDARD)

            steps_data = json.loads(json_match.group())

            steps = []
            for s_data in steps_data:
                # Конвертируем строку типа в Enum
                try:
                    s_type = StepType(s_data["step_type"])
                except ValueError:
                    s_type = StepType.GRAPH_QUERY # Fallback

                step = ReasoningStep(
                    step_id=s_data.get("step_id", f"step_{len(steps)+1}"),
                    step_type=s_type,
                    description=s_data.get("description", ""),
                    params=s_data.get("params", {}),
                    depends_on=s_data.get("depends_on", [])
                )
                steps.append(step)

            # Если нет synthesize шага - добавляем
            if not any(s.step_type == StepType.SYNTHESIZE for s in steps):
                steps.append(ReasoningStep(
                    step_id=f"step_{len(steps)+1}",
                    step_type=StepType.SYNTHESIZE,
                    description="Синтез ответа",
                    params={"template": "general_response"},
                    depends_on=[s.step_id for s in steps]
                ))

            plan = ReasoningPlan(
                plan_id=plan_id,
                query_intent=intent,
                steps=steps,
                status="created",
                created_at=datetime.now().isoformat()
            )

            logger.info(f"✅ Dynamic plan created with {len(steps)} steps")
            return plan

        except Exception as e:
            logger.error(f"❌ Failed to generate dynamic plan: {e}")
            return self.create_plan(intent, strategy=SearchStrategy.STANDARD)

    def optimize_plan(self, plan: ReasoningPlan) -> ReasoningPlan:
        """
        Оптимизировать план (удалить избыточные шаги, параллелизовать).

        Args:
            plan: Исходный план

        Returns:
            Оптимизированный план
        """
        # Находим шаги без зависимостей — можно выполнять параллельно
        parallel_groups = []
        current_group = []

        for step in plan.steps:
            if not step.depends_on:
                current_group.append(step)
            else:
                if current_group:
                    parallel_groups.append(current_group)
                    current_group = []

        if current_group:
            parallel_groups.append(current_group)

        # Помечаем параллельные группы
        for i, group in enumerate(parallel_groups):
            for step in group:
                step.metadata["parallel_group"] = i

        return plan

    def estimate_cost(self, plan: ReasoningPlan) -> Dict[str, Any]:
        """
        Оценить стоимость выполнения плана.

        Args:
            plan: План

        Returns:
            Оценка стоимости
        """
        cost = {
            "graph_queries": 0,
            "vector_searches": 0,
            "llm_calls": 0,
            "estimated_time_ms": 0
        }

        for step in plan.steps:
            if step.step_type in (StepType.GRAPH_QUERY,):
                cost["graph_queries"] += 1
                cost["estimated_time_ms"] += 50
            elif step.step_type in (StepType.VECTOR_SEARCH, StepType.HYBRID_SEARCH):
                cost["vector_searches"] += 1
                cost["estimated_time_ms"] += 100
            elif step.step_type in (StepType.ANALYZE, StepType.SYNTHESIZE):
                cost["llm_calls"] += 1
                cost["estimated_time_ms"] += 500
            else:
                cost["estimated_time_ms"] += 20

        return cost

