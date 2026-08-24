# -*- coding: utf-8 -*-
"""
Data Bus Models — Модели данных шины.

Хранение в Supabase (PostgreSQL). Таблицы создаются через миграцию.
"""
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ConsumerType(str, Enum):
    """Типы потребителей шины данных."""
    INVESTOR = "investor"          # Инвестор
    CONTRACTOR = "contractor"      # Подрядчик (дизайн, разработка)
    PARTNER = "partner"            # Партнёр
    AI_AGENT = "ai_agent"          # AI-агент внешней компании
    INTERNAL_DEPT = "internal_dept"  # Внутренний отдел
    AUDITOR = "auditor"            # Аудитор
    CONSULTANT = "consultant"      # Консалтинговая компания
    HR_PARTNER = "hr_partner"      # HR-партнёр


@dataclass
class SharingPolicy:
    """
    Политика доступа к данным.

    Определяет ЧТО видит потребитель, КОГДА, и СКОЛЬКО может запрашивать.
    """
    id: str
    tenant_id: str
    name: str                                    # "Инвестор — только чтение"
    description: str = ""

    # ЧТО видно: типы узлов графа знаний
    allowed_node_types: List[str] = field(default_factory=list)
    # ["Project", "Milestone", "KPI", "Product", "Report"]

    blocked_node_types: List[str] = field(default_factory=list)
    # ["Person", "Decision", "Commitment"]

    # Максимальный уровень доступа (1=PUBLIC, 2=INTERNAL, 3=CONFIDENTIAL, 4=RESTRICTED, 5=TOP_SECRET)
    max_access_level: int = 1

    # Ограничение по проектам ("*" = все, или список конкретных)
    allowed_projects: List[str] = field(default_factory=lambda: ["*"])

    # Фильтрация полей внутри узлов
    # {"Person": ["name", "role"], "KPI": ["name", "trend"]}
    field_filters: Dict[str, List[str]] = field(default_factory=dict)

    # Паттерны редактирования (замены)
    # [{"pattern": "\\d+ руб", "replace": "[СУММА СКРЫТА]"}]
    redaction_patterns: List[Dict[str, str]] = field(default_factory=list)

    # Ограничения по запросам
    max_queries_per_day: int = 100
    max_queries_per_hour: int = 20
    max_results_per_query: int = 50

    # Аудит
    audit_required: bool = True
    notify_admin: bool = False

    # СРЕЗ ДАННЫХ (как фильтры чата, но наружу): открыть потребителю не всю
    # базу, а конкретные встречи и/или папки. Пусто = измерение не
    # ограничено. Заполнено = ЖЁСТКАЯ граница: результаты вне среза
    # отсекаются до редакции и синтеза (встречи по методологии — ученикам,
    # встречи по проекту клиента — клиенту).
    allowed_meeting_ids: List[str] = field(default_factory=list)
    allowed_folders: List[str] = field(default_factory=list)

    # Промпт владельца данных: «как отвечать этому потребителю» — роль, тон,
    # правила («ты — ассистент методологии КПД, отвечай по шагам...»).
    answer_prompt: str = ""

    # РАСШИРЕННЫЙ ПОИСК (opt-in владельца — тратит ЕГО токены на запросы
    # потребителя): (а) разведка — по сводке компании уточняются поисковые
    # запросы (сводка наружу НЕ попадает, только навигация), (б) дочитывание
    # встреч среза целиком, когда чанков мало. False = только дешёвый поиск.
    allow_deep_search: bool = False

    # Двунаправленность (P3 cross-org federation).
    # ПО УМОЛЧАНИЮ False — все существующие шаблоны/консьюмеры остаются
    # read-only. Запись разрешается только явно через custom_policy.
    allow_ingest: bool = False
    ingest_max_items_per_request: int = 100

    # Статус
    is_active: bool = True
    created_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SharingPolicy":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class DataBusConsumer:
    """
    Потребитель шины данных.

    Это может быть внешняя компания (инвестор, подрядчик),
    внутренний отдел, или AI-агент партнёра.
    """
    id: str
    tenant_id: str
    name: str                                    # "НордИнвест"
    consumer_type: ConsumerType                  # investor, contractor, ...
    contact_email: str = ""

    # Аутентификация
    api_key_hash: str = ""                       # SHA-256 hash
    api_key_prefix: str = ""                     # "tdb_inv_a3f8..." для визуального опознания

    # Политика доступа
    sharing_policy_id: str = ""                  # FK → SharingPolicy
    sharing_policy: Optional[SharingPolicy] = field(default=None, repr=False)

    # К каким проектам есть доступ (дополнительно к policy.allowed_projects)
    allowed_project_ids: List[str] = field(default_factory=list)

    # Статус
    is_active: bool = True
    created_at: str = ""
    expires_at: Optional[str] = None
    last_access_at: Optional[str] = None

    # Метаданные
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["consumer_type"] = self.consumer_type.value if isinstance(self.consumer_type, ConsumerType) else self.consumer_type
        # Не включаем policy объект — он подгружается отдельно
        d.pop("sharing_policy", None)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DataBusConsumer":
        data = dict(data)  # copy
        ct = data.get("consumer_type", "partner")
        if isinstance(ct, str):
            data["consumer_type"] = ConsumerType(ct)
        data.pop("sharing_policy", None)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    @property
    def is_expired(self) -> bool:
        if not self.expires_at:
            return False
        return self.expires_at < datetime.now(timezone.utc).isoformat()


@dataclass
class DataChannel:
    """
    Канал доставки данных потребителю.

    Потребитель может получать данные через API, webhook,
    Telegram бот, Slack бот и т.д.
    """
    id: str
    consumer_id: str
    channel_type: str                            # "api", "webhook", "telegram_bot", "slack_bot"

    # Для webhook push-уведомлений
    endpoint_url: Optional[str] = None

    # Для чат-ботов
    chat_id: Optional[str] = None

    # Подписки на события
    subscriptions: List[str] = field(default_factory=list)
    # ["milestone_reached", "kpi_changed", "task_completed"]

    is_active: bool = True
    created_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DataChannel":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class DataBusAuditEntry:
    """Запись аудита шины данных."""
    id: str
    timestamp: str
    consumer_id: str
    consumer_name: str
    channel_type: str                            # "api", "telegram", "webhook"

    # Запрос
    query_type: str                              # "search", "snapshot", "subscribe"
    query_text: Optional[str] = None
    query_params: Dict[str, Any] = field(default_factory=dict)

    # Результат
    results_count: int = 0
    redacted_count: int = 0
    policy_applied: str = ""

    # Мета
    ip_address: Optional[str] = None
    response_time_ms: int = 0
    status_code: int = 200
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DataBusAuditEntry":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# ============================================================
# Предустановленные политики доступа (шаблоны)
# ============================================================

POLICY_TEMPLATES = {
    "investor_readonly": SharingPolicy(
        id="tpl_investor_readonly",
        tenant_id="",
        name="Инвестор — только чтение",
        description="Инвестор видит общий статус проектов, вехи, публичные KPI и продукты. "
                    "Не видит персональные данные, решения, зарплаты.",
        allowed_node_types=["Project", "Milestone", "KPI", "Product", "Report", "Entity"],
        blocked_node_types=["Person", "Decision", "Commitment", "Task", "Contradiction", "Risk"],
        max_access_level=1,
        allowed_projects=["*"],
        field_filters={
            "KPI": ["name", "trend", "category", "period"],
            "Project": ["name", "status", "progress", "health_score", "description"],
            "Product": ["name", "description", "features", "target_audience"],
        },
        redaction_patterns=[
            {"pattern": r"\d+[\s]*(руб|₽|\$|€|USD|RUB|EUR)", "replace": "[СУММА СКРЫТА]"},
            {"pattern": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}", "replace": "[EMAIL]"},
            {"pattern": r"\+?\d{1,3}[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{2}[-.\s]?\d{2}", "replace": "[ТЕЛЕФОН]"},
        ],
        max_queries_per_day=50,
        max_queries_per_hour=10,
        max_results_per_query=20,
        audit_required=True,
        notify_admin=True,
    ),

    "contractor_project": SharingPolicy(
        id="tpl_contractor_project",
        tenant_id="",
        name="Подрядчик — задачи проекта",
        description="Подрядчик видит задачи и вехи своих проектов. "
                    "Не видит другие проекты, KPI, финансы, персональные данные.",
        allowed_node_types=["Task", "Milestone", "Meeting", "Entity"],
        blocked_node_types=["Person", "KPI", "Decision", "Risk", "Commitment", "Contradiction"],
        max_access_level=2,
        allowed_projects=[],  # Заполняется при создании
        field_filters={
            "Task": ["title", "description", "status", "priority", "deadline"],
            "Meeting": ["title", "date", "summary"],
            "Milestone": ["name", "status", "deadline"],
        },
        redaction_patterns=[
            {"pattern": r"\d+[\s]*(руб|₽|\$|€)", "replace": "[СКРЫТО]"},
        ],
        max_queries_per_day=100,
        max_queries_per_hour=20,
        max_results_per_query=50,
    ),

    "ai_agent_analysis": SharingPolicy(
        id="tpl_ai_agent_analysis",
        tenant_id="",
        name="AI-агент — анализ",
        description="AI-агент партнёрской компании может анализировать структуру, "
                    "отделы, проекты, продукты и публичные KPI.",
        allowed_node_types=[
            "Team", "Department", "Project", "Product", "KPI",
            "Milestone", "Report", "Entity", "Knowledge",
        ],
        blocked_node_types=["Person", "Commitment", "Contradiction", "Risk"],
        max_access_level=2,
        allowed_projects=["*"],
        field_filters={
            "KPI": ["name", "numeric_value", "unit", "trend", "category", "period"],
        },
        max_queries_per_day=500,
        max_queries_per_hour=50,
        max_results_per_query=100,
    ),

    "internal_department": SharingPolicy(
        id="tpl_internal_department",
        tenant_id="",
        name="Внутренний отдел",
        description="Внутренний отдел видит все типы данных, ограничен access_level.",
        allowed_node_types=["*"],
        blocked_node_types=[],
        max_access_level=3,
        allowed_projects=["*"],
        max_queries_per_day=1000,
        max_queries_per_hour=100,
        max_results_per_query=200,
    ),

    "partner_public": SharingPolicy(
        id="tpl_partner_public",
        tenant_id="",
        name="Партнёр — public",
        description="Партнёр видит только публичные данные о продуктах и проектах.",
        allowed_node_types=["Product", "Project", "Milestone", "Entity"],
        blocked_node_types=["Person", "Decision", "KPI", "Task", "Risk", "Commitment"],
        max_access_level=1,
        allowed_projects=["*"],
        field_filters={
            "Product": ["name", "description", "features"],
            "Project": ["name", "status", "description"],
        },
        redaction_patterns=[
            {"pattern": r"\d+[\s]*(руб|₽|\$|€|USD|RUB)", "replace": "[СУММА СКРЫТА]"},
            {"pattern": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}", "replace": "[EMAIL]"},
        ],
        max_queries_per_day=30,
        max_queries_per_hour=5,
        max_results_per_query=10,
    ),
}
