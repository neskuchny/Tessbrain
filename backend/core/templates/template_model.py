# -*- coding: utf-8 -*-
"""
Template Model - Модели данных для шаблонов и workflows.

Шаблон (WorkflowTemplate) определяет:
- Последовательность шагов (агентов)
- Триггеры для автоматического запуска
- Параметры и переменные
- Формат вывода
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class TemplateCategory(str, Enum):
    """Категории шаблонов."""
    ANALYSIS = "analysis"  # Анализ данных
    GENERATION = "generation"  # Генерация контента
    INTEGRATION = "integration"  # Интеграции
    REPORT = "report"  # Отчёты
    AUTOMATION = "automation"  # Автоматизации
    EXTRACTION = "extraction"  # Извлечение данных


class TemplateVisibility(str, Enum):
    """Видимость шаблона."""
    PRIVATE = "private"  # Только создатель
    ORGANIZATION = "organization"  # Вся организация
    PUBLIC = "public"  # Публичный


class ExecutionStatus(str, Enum):
    """Статус выполнения."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TemplateStep:
    """
    Шаг шаблона - вызов агента.

    Пример:
    ```
    step = TemplateStep(
        step_id="step_1",
        agent="IntentAgent",
        action="classify_intent",
        params={"extract_entities": True},
        output_variable="intent_result"
    )
    ```
    """
    step_id: str
    agent: str  # Имя агента
    action: str  # Действие агента

    # Параметры (могут содержать переменные ${step1.result})
    params: Dict[str, Any] = field(default_factory=dict)

    # Имя переменной для результата
    output_variable: str = ""

    # Условие выполнения (JavaScript-подобное выражение)
    condition: Optional[str] = None

    # Таймаут в секундах
    timeout_seconds: int = 60

    # Порядок выполнения
    order: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "agent": self.agent,
            "action": self.action,
            "params": self.params,
            "output_variable": self.output_variable,
            "condition": self.condition,
            "timeout_seconds": self.timeout_seconds,
            "order": self.order
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TemplateStep":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class WorkflowTemplate:
    """
    Шаблон workflow.

    Пример:
    ```
    template = WorkflowTemplate(
        template_id="project_analysis",
        name="Анализ проекта",
        description="Глубокий анализ проекта с выявлением рисков",
        category=TemplateCategory.ANALYSIS,
        trigger_patterns=["анализ проекта (.*)", "что с проектом (.*)"],
        steps=[
            TemplateStep(
                step_id="step_1",
                agent="IntentAgent",
                action="classify_intent",
                params={"extract_entities": True}
            ),
            TemplateStep(
                step_id="step_2",
                agent="ResearcherAgent",
                action="deep_research",
                params={"entity": "${step_1.entity}", "depth": "deep"}
            )
        ]
    )
    ```
    """
    template_id: str
    name: str
    description: str

    # Категория и видимость
    category: TemplateCategory = TemplateCategory.ANALYSIS
    visibility: TemplateVisibility = TemplateVisibility.PRIVATE

    # Триггеры (regex паттерны)
    trigger_patterns: List[str] = field(default_factory=list)

    # Шаги выполнения
    steps: List[TemplateStep] = field(default_factory=list)

    # Входные параметры (описание)
    input_schema: Dict[str, Any] = field(default_factory=dict)

    # Формат вывода
    output_format: str = "markdown"  # markdown, json, table, html
    output_schema: Dict[str, Any] = field(default_factory=dict)

    # Опции
    show_sources: bool = True
    show_anomalies: bool = True
    show_chain: bool = False  # Показывать цепочку агентов

    # Владелец
    created_by: Optional[str] = None
    organization_id: Optional[str] = None

    # Статистика
    usage_count: int = 0
    last_used_at: Optional[str] = None
    avg_execution_time_ms: int = 0

    # Метаданные
    tags: List[str] = field(default_factory=list)
    icon: str = "📋"
    color: str = "#3B82F6"

    # Временные метки
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "template_id": self.template_id,
            "name": self.name,
            "description": self.description,
            "category": self.category.value if isinstance(self.category, TemplateCategory) else self.category,
            "visibility": self.visibility.value if isinstance(self.visibility, TemplateVisibility) else self.visibility,
            "trigger_patterns": self.trigger_patterns,
            "steps": [s.to_dict() for s in self.steps],
            "input_schema": self.input_schema,
            "output_format": self.output_format,
            "output_schema": self.output_schema,
            "show_sources": self.show_sources,
            "show_anomalies": self.show_anomalies,
            "show_chain": self.show_chain,
            "created_by": self.created_by,
            "organization_id": self.organization_id,
            "usage_count": self.usage_count,
            "last_used_at": self.last_used_at,
            "avg_execution_time_ms": self.avg_execution_time_ms,
            "tags": self.tags,
            "icon": self.icon,
            "color": self.color,
            "created_at": self.created_at,
            "updated_at": self.updated_at
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkflowTemplate":
        # Преобразуем enums
        if "category" in data and isinstance(data["category"], str):
            try:
                data["category"] = TemplateCategory(data["category"])
            except ValueError:
                data["category"] = TemplateCategory.ANALYSIS

        if "visibility" in data and isinstance(data["visibility"], str):
            try:
                data["visibility"] = TemplateVisibility(data["visibility"])
            except ValueError:
                data["visibility"] = TemplateVisibility.PRIVATE

        # Преобразуем steps
        if "steps" in data and isinstance(data["steps"], list):
            data["steps"] = [
                TemplateStep.from_dict(s) if isinstance(s, dict) else s
                for s in data["steps"]
            ]

        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def matches_query(self, query: str) -> Optional[Dict[str, str]]:
        """
        Проверяет, соответствует ли запрос триггерам шаблона.

        Returns:
            Dict с захваченными группами или None
        """
        import re

        query_lower = query.lower().strip()

        for pattern in self.trigger_patterns:
            try:
                match = re.match(pattern.lower(), query_lower)
                if match:
                    return {
                        "pattern": pattern,
                        "groups": match.groups(),
                        "groupdict": match.groupdict()
                    }
            except re.error:
                continue

        return None


@dataclass
class TemplateExecution:
    """
    Запись о выполнении шаблона.
    """
    execution_id: str
    template_id: str

    # Входные данные
    query: str
    input_params: Dict[str, Any] = field(default_factory=dict)

    # Статус
    status: ExecutionStatus = ExecutionStatus.PENDING

    # Результаты шагов
    step_results: Dict[str, Any] = field(default_factory=dict)

    # Финальный результат
    result: Optional[str] = None
    result_data: Dict[str, Any] = field(default_factory=dict)

    # Ошибка (если есть)
    error: Optional[str] = None

    # Метрики
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_ms: int = 0

    # Пользователь
    user_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "template_id": self.template_id,
            "query": self.query,
            "input_params": self.input_params,
            "status": self.status.value if isinstance(self.status, ExecutionStatus) else self.status,
            "step_results": self.step_results,
            "result": self.result,
            "result_data": self.result_data,
            "error": self.error,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "user_id": self.user_id
        }


# ============================================
# ГОТОВЫЕ ШАБЛОНЫ (Built-in Templates)
# ============================================

BUILTIN_TEMPLATES = [
    WorkflowTemplate(
        template_id="project_analysis",
        name="Анализ проекта",
        description="Глубокий анализ проекта с выявлением рисков, статуса и рекомендаций",
        category=TemplateCategory.ANALYSIS,
        visibility=TemplateVisibility.PUBLIC,
        trigger_patterns=[
            r"анализ проекта (.*)",
            r"что с проектом (.*)",
            r"статус проекта (.*)",
            r"как дела с проектом (.*)"
        ],
        steps=[
            TemplateStep(
                step_id="step_1",
                agent="IntentAgent",
                action="classify_intent",
                params={"extract_entities": True},
                output_variable="intent"
            ),
            TemplateStep(
                step_id="step_2",
                agent="ResearcherAgent",
                action="deep_research",
                params={
                    "entity": "${step_1.entity}",
                    "entity_type": "project",
                    "include_anomalies": True,
                    "depth": "deep"
                },
                output_variable="research"
            ),
            TemplateStep(
                step_id="step_3",
                agent="SynthesizerAgent",
                action="generate_report",
                params={
                    "research": "${step_2}",
                    "format": "markdown",
                    "include_recommendations": True
                },
                output_variable="report"
            )
        ],
        icon="📊",
        color="#10B981",
        tags=["проект", "анализ", "статус"]
    ),

    WorkflowTemplate(
        template_id="person_profile",
        name="Профиль сотрудника",
        description="Анализ активности и профиля сотрудника",
        category=TemplateCategory.ANALYSIS,
        visibility=TemplateVisibility.PUBLIC,
        trigger_patterns=[
            r"кто такой (.*)",
            r"профиль (.*)",
            r"что известно о (.*)",
            r"как работает (.*)"
        ],
        steps=[
            TemplateStep(
                step_id="step_1",
                agent="IntentAgent",
                action="classify_intent",
                params={"extract_entities": True, "entity_type": "person"},
                output_variable="intent"
            ),
            TemplateStep(
                step_id="step_2",
                agent="PersonProfileAgent",
                action="build_profile",
                params={"person_name": "${step_1.entity}"},
                output_variable="profile"
            )
        ],
        icon="👤",
        color="#8B5CF6",
        tags=["сотрудник", "профиль", "человек"]
    ),

    WorkflowTemplate(
        template_id="weekly_report",
        name="Еженедельный отчёт",
        description="Автоматический отчёт по активности за неделю",
        category=TemplateCategory.REPORT,
        visibility=TemplateVisibility.PUBLIC,
        trigger_patterns=[
            r"отчёт за неделю",
            r"недельный отчёт",
            r"сводка за неделю",
            r"что было на этой неделе"
        ],
        steps=[
            TemplateStep(
                step_id="step_1",
                agent="ReportGenerator",
                action="generate_weekly",
                params={"include_meetings": True, "include_decisions": True},
                output_variable="report"
            )
        ],
        icon="📝",
        color="#F59E0B",
        tags=["отчёт", "неделя", "сводка"]
    ),

    WorkflowTemplate(
        template_id="risk_analysis",
        name="Анализ рисков",
        description="Выявление и анализ рисков проекта или компании",
        category=TemplateCategory.ANALYSIS,
        visibility=TemplateVisibility.PUBLIC,
        trigger_patterns=[
            r"риски проекта (.*)",
            r"какие риски (.*)",
            r"анализ рисков (.*)",
            r"что может пойти не так"
        ],
        steps=[
            TemplateStep(
                step_id="step_1",
                agent="FuturePredictor",
                action="predict_risks",
                params={"depth": "deep", "time_horizon": "3_months"},
                output_variable="risks"
            ),
            TemplateStep(
                step_id="step_2",
                agent="SynthesizerAgent",
                action="format_risks",
                params={"risks": "${step_1}", "format": "markdown"},
                output_variable="report"
            )
        ],
        icon="⚠️",
        color="#EF4444",
        tags=["риски", "анализ", "прогноз"]
    ),

    WorkflowTemplate(
        template_id="regulation_extraction",
        name="Извлечение регламентов",
        description="Извлечение регламентов и правил из встреч",
        category=TemplateCategory.EXTRACTION,
        visibility=TemplateVisibility.PUBLIC,
        trigger_patterns=[
            r"регламенты из встречи (.*)",
            r"какие правила (.*)",
            r"извлеки регламенты (.*)",
            r"политики из встречи (.*)"
        ],
        steps=[
            TemplateStep(
                step_id="step_1",
                agent="RegulationExtractor",
                action="extract_regulations",
                params={"meeting_id": "${input.meeting_id}"},
                output_variable="regulations"
            ),
            TemplateStep(
                step_id="step_2",
                agent="SynthesizerAgent",
                action="format_regulations",
                params={"regulations": "${step_1}", "format": "markdown"},
                output_variable="report"
            )
        ],
        icon="📋",
        color="#6366F1",
        tags=["регламент", "правила", "политика"]
    ),

    WorkflowTemplate(
        template_id="template_extraction",
        name="Извлечение шаблонов",
        description="Извлечение чек-листов и шаблонов из встреч",
        category=TemplateCategory.EXTRACTION,
        visibility=TemplateVisibility.PUBLIC,
        trigger_patterns=[
            r"шаблоны из встречи (.*)",
            r"чек-листы из встречи (.*)",
            r"извлеки шаблоны (.*)"
        ],
        steps=[
            TemplateStep(
                step_id="step_1",
                agent="TemplateExtractor",
                action="extract_templates",
                params={"meeting_id": "${input.meeting_id}"},
                output_variable="templates"
            )
        ],
        icon="📄",
        color="#14B8A6",
        tags=["шаблон", "чек-лист", "структура"]
    ),

    WorkflowTemplate(
        template_id="meeting_summary",
        name="Краткое содержание встречи",
        description="Генерация краткого содержания встречи",
        category=TemplateCategory.REPORT,
        visibility=TemplateVisibility.PUBLIC,
        trigger_patterns=[
            r"о чём была встреча (.*)",
            r"краткое содержание встречи (.*)",
            r"summary встречи (.*)",
            r"расскажи о встрече (.*)"
        ],
        steps=[
            TemplateStep(
                step_id="step_1",
                agent="MeetingSummarizer",
                action="summarize",
                params={"meeting_id": "${input.meeting_id}", "detail_level": "standard"},
                output_variable="summary"
            )
        ],
        icon="📝",
        color="#0EA5E9",
        tags=["встреча", "summary", "краткое"]
    )
]


