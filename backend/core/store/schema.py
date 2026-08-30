# -*- coding: utf-8 -*-
"""
TESSENT BRAIN - Graph Schema Definition & Validation
Определяет онтологию графа знаний и правила валидации.
"""
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

# ==============================================================================
# SCHEMA DEFINITION
# ==============================================================================

# ==============================================================================
# ACCESS LEVELS
# ==============================================================================
ACCESS_LEVELS = {
    1: "PUBLIC",        # Видно всем (включая внешних)
    2: "INTERNAL",      # Видно всем сотрудникам
    3: "CONFIDENTIAL",  # Видно определённым ролям/группам
    4: "RESTRICTED",    # Видно конкретным людям
    5: "TOP_SECRET",    # Видно только CEO/совет директоров
}

# Базовые поля, которые ДОЛЖНЫ быть на каждом узле
BASE_NODE_PROPS = [
    "access_level",     # int 1-5
    "access_groups",    # list[str] — группы доступа
    "tenant_id",        # str — ID компании (multi-tenant)
    "user_id",          # str — кто создал
    "confidence",       # float 0.0-1.0
    "source_meeting_id",# str — из какой встречи
    "created_at",       # str — ISO datetime
    "updated_at",       # str — ISO datetime
]

# Базовые поля для связей
BASE_EDGE_PROPS = [
    "description",       # str — осмысленное описание связи
    "confidence",        # float 0.0-1.0
    "weight",            # float — сила связи
    "co_occurrences",    # int — сколько раз совместно упоминались
    "source_meeting_id", # str — из какой встречи
    "valid_from",        # str — когда связь стала актуальной
    "valid_until",       # str — когда перестала (null = текущая)
    "known_from",        # str — когда мы узнали
    "created_at",        # str — ISO datetime
]

# Разрешенные типы узлов и их обязательные/опциональные свойства
NODE_SCHEMA = {
    # ═══ ЛЮДИ И ОРГАНИЗАЦИЯ ═══
    "Meeting": {
        "description": "Встреча или событие",
        "required_props": ["meeting_id"],
        "optional_props": ["title", "date", "project_id", "processing_timestamp", "summary", "tags"]
    },
    # Узел-связка кросс-встречного анализа. Его создаёт
    # cross_meeting_analyzer._create_graph_links, читает роут cross_meeting
    # по _label == "MeetingLink" — но в схеме тип отсутствовал, и в STRICT
    # валидатор отвергал узел, а в warn засорял лог. Меряно на живом
    # прогоне: «unknown object type 'MeetingLink'» на каждой связи.
    "MeetingLink": {
        "description": "Связь между встречами (кросс-встречный анализ)",
        "required_props": ["source_meeting", "target_meeting"],
        "optional_props": ["relevance_score", "link_type", "common_participants",
                           "common_projects", "common_topics",
                           "temporal_distance_days", "created_at"]
    },
    "Person": {
        "description": "Участник встречи или сотрудник",
        "required_props": ["name"],
        "optional_props": ["role", "department", "email", "normalized_name", "aliases",
                           "external_ids", "meeting_ids", "meeting_titles", "competencies"]
    },
    "Role": {
        "description": "Должностная роль (PM, CTO, маркетолог...)",
        "required_props": ["role_id", "name"],
        "optional_props": ["description", "department", "level"]
    },
    "Team": {
        "description": "Команда или отдел",
        "required_props": ["team_id", "name"],
        "optional_props": ["description", "department", "lead_id", "member_count"]
    },
    "Department": {
        "description": "Департамент",
        "required_props": ["department_id", "name"],
        "optional_props": ["description", "head_id"]
    },

    # ═══ ПРОЕКТЫ И ПРОДУКТЫ ═══
    "Project": {
        "description": "Проект",
        # project_id → optional: часть Project-узлов создаётся из statuses без id.
        "required_props": ["name"],
        "optional_props": ["project_id", "description", "status", "health_score", "progress",
                           "start_date", "end_date", "budget"]
    },
    "Product": {
        "description": "Продукт или услуга",
        "required_props": ["product_id", "name"],
        "optional_props": ["description", "price", "features", "target_audience",
                           "pricing_model", "status"]
    },
    "Milestone": {
        "description": "Веха проекта",
        "required_props": ["milestone_id", "name"],
        "optional_props": ["description", "deadline", "status", "project_id"]
    },
    "Risk": {
        "description": "Риск",
        "required_props": ["risk_id", "description"],
        "optional_props": ["probability", "impact", "mitigation", "status", "project_id"]
    },

    # ═══ СОБЫТИЯ И РЕШЕНИЯ ═══
    "Task": {
        "description": "Задача или поручение",
        "required_props": ["title"],
        "optional_props": ["task_id", "status", "priority", "deadline", "description", "assignee"]
    },
    "Decision": {
        "description": "Принятое решение",
        "required_props": ["summary"],
        "optional_props": ["decision_id", "category", "status", "confidence", "details", "rationale"]
    },
    "Commitment": {
        "description": "Обязательство или обещание",
        "required_props": ["commitment_id", "description"],
        "optional_props": ["person_id", "deadline", "status", "meeting_id"]
    },

    # ═══ ЗНАНИЯ ═══
    "Entity": {
        "description": "Именованная сущность (технология, концепция, клиент, партнёр)",
        # entity_id → optional: causal-extractor создаёт Entity-узлы cause/effect
        # без entity_id (узел сохраняется штатно). name+entity_type остаются.
        "required_props": ["name", "entity_type"],
        "optional_props": ["entity_id", "description", "importance"]
    },
    "KPI": {
        "description": "Ключевой показатель эффективности",
        "required_props": ["name"],
        "optional_props": ["kpi_id", "current_value", "target_value", "unit", "trend",
                           "numeric_value", "period", "kpi_type", "category"]
    },
    "Knowledge": {
        "description": "Извлеченное знание (инсайт, кейс, методология и ещё 14 категорий)",
        "required_props": ["name", "category", "content"],
        "optional_props": ["confidence", "importance", "tags", "source_chunk_id"]
    },
    "Principle": {
        "description": "Выведенный принцип или мудрость (DIKW)",
        "required_props": ["principle_id", "statement"],
        "optional_props": ["domain", "confidence", "source"]
    },
    "Pattern": {
        "description": "Обнаруженный повторяющийся паттерн (Tier 3+)",
        "required_props": ["pattern_id", "description"],
        "optional_props": ["frequency", "examples", "confidence", "domain"]
    },
    "Contradiction": {
        "description": "Противоречие между фактами (Tier 3+)",
        "required_props": [],
        "optional_props": ["contradiction_id", "new_fact", "old_fact", "contradiction_type",
                           "resolution", "resolution_suggestion"]
    },
    "Question": {
        "description": "Открытый вопрос без ответа",
        "required_props": ["question_id", "text"],
        "optional_props": ["context", "asked_by", "asked_at", "meeting_id", "status"]
    },
    "Goal": {
        "description": "Цель организации/проекта/команды/человека (GoalsTrackingAgent)",
        "required_props": ["goal_id", "goal_statement"],
        "optional_props": ["level", "entity_name", "status", "timeline",
                           "success_criteria", "confidence", "why_reason",
                           "progress_percentage", "metrics_quantitative",
                           "metrics_qualitative", "meeting_id"]
    },
    "ConflictRisk": {
        "description": "Выявленный риск конфликта между людьми/целями",
        "required_props": ["conflict_id", "description"],
        "optional_props": ["conflict_type", "risk_level", "probability",
                           "parties_involved", "root_causes", "impact",
                           "resolution_suggestions", "severity", "meeting_id"]
    },
    "Scenario": {
        "description": "Смоделированный сценарий развития (FuturePredictor)",
        "required_props": ["scenario_id", "name"],
        "optional_props": ["scenario_type", "probability", "description",
                           "time_horizon", "key_events", "consequences",
                           "recommended_actions", "affected_projects", "meeting_id"]
    },
    "Prediction": {
        "description": "Предсказание исхода проекта/задачи (FuturePredictor)",
        "required_props": ["prediction_id", "subject"],
        "optional_props": ["success_probability", "completion_date", "key_risks",
                           "blockers", "recommendations", "meeting_id"]
    },

    # ═══ ИДЕИ / МНЕНИЯ / ПСИХОПРОФИЛИ ═══
    "Idea": {
        "description": "Идея/предложение, высказанное на встрече (IdeaAgent)",
        "required_props": ["name"],
        "optional_props": ["summary", "category", "author", "novelty",
                           "meeting_id", "user_id"]
    },
    "Opinion": {
        "description": "Мнение участника с тональностью (OpinionAgent)",
        "required_props": ["name"],
        "optional_props": ["summary", "topic", "category", "sentiment", "author",
                           "meeting_id", "user_id"]
    },
    "PsychologicalProfile": {
        "description": "Психологический профиль участника (4 модели)",
        "required_props": [],
        "optional_props": ["person_id", "person_name", "big_five", "disc", "mbti",
                           "values", "meeting_id", "user_id"]
    },
    "PsychInsight": {
        "description": "Психоаналитика встречи: мотивы / групповая динамика / "
                       "прогноз поведения / рекомендация (kind различает). "
                       "Мотивы и прогнозы sensitive, в поиск не индексируются",
        "required_props": ["kind"],
        "optional_props": ["name", "participant", "sensitive", "access_level",
                           "meeting_id", "user_id", "created_at", "source_agent"]
    },

    # ═══ РЕГЛАМЕНТЫ И ШАБЛОНЫ ═══
    "Regulation": {
        "description": "Регламент, правило, политика",
        # name → optional: экстрактор кладёт title; жёсткое required давало
        # лавину WARN-варнингов, хотя узел сохраняется штатно.
        "required_props": [],
        "optional_props": ["name", "title", "description", "content", "category", "source_meeting_id"]
    },
    "Template": {
        "description": "Шаблон, форма, структура",
        "required_props": [],
        "optional_props": ["name", "title", "description", "content", "category", "source_meeting_id"]
    },

    # ═══ СТРУКТУРНЫЕ ═══
    "Chunk": {
        "description": "Фрагмент текста (транскрипта)",
        "required_props": ["chunk_id", "text", "source_meeting_id"],
        "optional_props": ["index", "char_start", "char_end"]
    },
    "DocumentChunk": {
        "description": "Фрагмент документа",
        "required_props": ["chunk_id", "document_id"],
        "optional_props": ["text", "chunk_index"]
    },
    "Snapshot": {
        "description": "Узел-ссылка на снэпшот сущности",
        "required_props": ["snapshot_id", "snapshot_type", "entity_id"],
        "optional_props": ["snippet", "version", "hierarchy_level", "parent_snapshot_id"]
    },
    "Report": {
        "description": "Периодический отчёт (weekly/monthly/quarterly/yearly)",
        "required_props": ["report_id", "period_type", "period_start"],
        "optional_props": ["period_end", "scope", "scope_id", "summary",
                           "snippet", "meeting_ids"]
    },
}

# Разрешенные связи: SOURCE_LABEL -> RELATIONSHIP -> TARGET_LABEL
RELATIONSHIP_SCHEMA = {
    # ═══ ОРГАНИЗАЦИОННЫЕ ═══
    "PARTICIPATED_IN": [
        {"from": "Person", "to": "Meeting"}
    ],
    # Рёбра кросс-встречных связей (см. MeetingLink в NODE_SCHEMA):
    # Meeting --HAS_LINK--> MeetingLink --LINKS_TO--> Meeting.
    "HAS_LINK": [
        {"from": "Meeting", "to": "MeetingLink"}
    ],
    "LINKS_TO": [
        {"from": "MeetingLink", "to": "Meeting"}
    ],
    "HOLDS_ROLE": [
        {"from": "Person", "to": "Role"}
    ],
    "MEMBER_OF": [
        {"from": "Person", "to": "Team"},
        {"from": "Person", "to": "Department"},
        {"from": "Team", "to": "Department"},
    ],
    "REPORTS_TO": [
        {"from": "Person", "to": "Person"}
    ],
    "LEADS": [
        {"from": "Person", "to": "Team"},
        {"from": "Person", "to": "Department"},
    ],

    # ═══ ПРОЕКТНЫЕ ═══
    "ASSIGNED_TO": [
        {"from": "Task", "to": "Person"},
        {"from": "Person", "to": "Task"},  # пайплайн пишет в обе стороны
    ],
    "BELONGS_TO": [
        {"from": "Task", "to": "Project"},
        {"from": "Milestone", "to": "Project"},
        {"from": "Risk", "to": "Project"},
        {"from": "KPI", "to": "Project"},
    ],
    "HAS_MILESTONE": [
        {"from": "Project", "to": "Milestone"}
    ],
    "HAS_RISK": [
        {"from": "Project", "to": "Risk"}
    ],
    "DEPENDS_ON": [
        {"from": "Task", "to": "Task"},
        {"from": "Project", "to": "Project"},
    ],

    # ═══ РЕШЕНИЯ ═══
    "MADE_DECISION": [
        {"from": "Person", "to": "Decision"}
    ],
    "CREATED_FROM": [
        {"from": "Decision", "to": "Meeting"},
        {"from": "Task", "to": "Meeting"}
    ],
    "IMPLEMENTS": [
        {"from": "Task", "to": "Decision"}
    ],
    "SUPERSEDES": [
        {"from": "Decision", "to": "Decision"}
    ],
    "COMMITTED_TO": [
        {"from": "Person", "to": "Commitment"}
    ],

    # ═══ ЦЕЛИ / ПРОГНОЗЫ / РИСКИ КОНФЛИКТОВ ═══
    "HAS_GOAL": [
        {"from": "Meeting", "to": "Goal"},
        {"from": "Project", "to": "Goal"},
        {"from": "Person", "to": "Goal"},
        {"from": "Department", "to": "Goal"},
    ],
    "HAS_CONFLICT_RISK": [
        {"from": "Meeting", "to": "ConflictRisk"},
    ],
    "AT_RISK_OF": [
        {"from": "Person", "to": "ConflictRisk"},
    ],
    "PREDICTS": [
        {"from": "Meeting", "to": "Scenario"},
        {"from": "Meeting", "to": "Prediction"},
    ],

    # ═══ ЗНАНИЯ ═══
    "MENTIONED_IN": [
        {"from": "Entity", "to": "Meeting"},
        {"from": "Entity", "to": "Chunk"},
        {"from": "Knowledge", "to": "Chunk"},
        {"from": "Task", "to": "Chunk"},
        {"from": "Decision", "to": "Chunk"},
    ],
    "MENTIONS": [
        # Встреча может упоминать сущность любого типа (Entity/Project/Product/
        # Person/Team/Department/Role/KPI/…) — не плодим whack-a-mole-варнинги.
        {"from": "Meeting", "to": "*"},
    ],
    "EXTRACTED_FROM": [
        {"from": "Knowledge", "to": "Meeting"},
        {"from": "Principle", "to": "Meeting"},
        {"from": "Regulation", "to": "Meeting"},
        {"from": "Template", "to": "Meeting"},
        {"from": "Entity", "to": "Meeting"},
    ],
    "REPORTED_IN": [
        {"from": "KPI", "to": "Meeting"}
    ],
    "RELATED_TO": [
        # Hebbian co-occurrence связывает ЛЮБЫЕ совместно упомянутые узлы
        # (Person/Project/Product/Entity/…), поэтому пара произвольная.
        {"from": "*", "to": "*"},
    ],
    "EVOLVED_INTO": [
        # Ночная консолидация связывает «нити идей» по хронологии
        # (nightly_consolidation: идея с ранней встречи → её развитие на
        # поздней). Тип писался в граф, но отсутствовал в схеме — каждая
        # ночь спамила «unknown relationship type 'EVOLVED_INTO'».
        {"from": "Idea", "to": "Idea"},
        {"from": "Entity", "to": "Entity"},
    ],

    # ═══ СТРУКТУРНЫЕ ═══
    "PART_OF": [
        {"from": "Chunk", "to": "Meeting"},
        {"from": "DocumentChunk", "to": "Document"},
        {"from": "Product", "to": "Project"},
    ],
    "DERIVED_FROM": [
        {"from": "Principle", "to": "Meeting"},
        {"from": "Principle", "to": "Decision"},
        {"from": "Principle", "to": "Knowledge"},
        {"from": "Pattern", "to": "Knowledge"},
    ],
    "HAS_SNAPSHOT": [
        {"from": "*", "to": "Snapshot"},   # Любой узел может иметь снэпшот
    ],
    "FOLLOW_UP_TO": [
        {"from": "Meeting", "to": "Meeting"}
    ],

    # ═══ ПРИЧИННОСТЬ (Tier 3+) ═══
    "CAUSED_BY": [
        {"from": "Decision", "to": "Decision"},
        {"from": "Risk", "to": "Decision"},
        {"from": "*", "to": "*"},  # Любая причинно-следственная связь
    ],
    "LED_TO": [
        {"from": "Decision", "to": "*"},
        {"from": "*", "to": "*"},
    ],
    "CONTRADICTS": [
        {"from": "*", "to": "*"},  # Любые два узла могут противоречить
    ],
    "SUPPORTS": [
        {"from": "*", "to": "*"},
    ],

    # ═══ ИДЕИ / МНЕНИЯ / РЕШЕНИЯ / ПСИХОПРОФИЛИ ═══
    "DECIDED": [
        {"from": "Person", "to": "Decision"},
    ],
    "HAS_IDEA": [
        {"from": "Meeting", "to": "Idea"},
    ],
    "PROPOSED": [
        {"from": "Person", "to": "Idea"},
    ],
    "HAS_OPINION": [
        {"from": "Meeting", "to": "Opinion"},
    ],
    "EXPRESSED": [
        {"from": "Person", "to": "Opinion"},
    ],
    "HAS_PROFILE": [
        {"from": "Person", "to": "PsychologicalProfile"},
    ],
    "HAS_PSYCH_INSIGHT": [
        {"from": "Meeting", "to": "PsychInsight"},
        {"from": "Person", "to": "PsychInsight"},
    ],
    "HAS_CONTRADICTION": [
        {"from": "Meeting", "to": "Contradiction"},
        {"from": "*", "to": "Contradiction"},
    ],
    "INVOLVED_IN": [
        {"from": "*", "to": "*"},
    ],

    # ═══ LEGACY / ОБРАТНАЯ СОВМЕСТИМОСТЬ ═══
    "HAS_DECISION": [
        {"from": "Meeting", "to": "Decision"}
    ],
    "HAS_TASK": [
        {"from": "Meeting", "to": "Task"}
    ],
    "HAS_KPI": [
        {"from": "Meeting", "to": "KPI"}
    ],
    "HAS_REGULATION": [
        {"from": "Meeting", "to": "Knowledge"},
        {"from": "Meeting", "to": "Regulation"},
    ],
    "HAS_TEMPLATE": [
        {"from": "Meeting", "to": "Knowledge"},
        {"from": "Meeting", "to": "Template"},
    ],
    "CONTAINS": [
        {"from": "Meeting", "to": "*"}
    ],
}

class SchemaValidator:
    """Валидатор графовой схемы."""

    def __init__(self, strict_mode: bool = False):
        self.strict_mode = strict_mode
        self.known_nodes = set(NODE_SCHEMA.keys())
        self.known_relationships = set(RELATIONSHIP_SCHEMA.keys())

    def _effective_mode(self):
        """P6: явный strict_mode → STRICT; иначе режим из env
        `ONTOLOGY_EXTRACTION_MODE` (default WARN). Позволяет ops
        включить глобальный STRICT для extraction без правки кода,
        не меняя дефолтное поведение."""
        from backend.core.ontology.sdk import (
            EnforcementMode,
            extraction_mode_from_env,
        )

        if self.strict_mode:
            return EnforcementMode.STRICT
        return extraction_mode_from_env()

    def validate_node(self, label: str, properties: Dict[str, Any]) -> bool:
        """
        Проверяет, соответствует ли узел схеме.

        P4: делегирует в Ontology SDK (единый источник правды). Сохраняет
        bool-контракт и нестрогое поведение по умолчанию, поэтому все
        существующие точки записи graph_builder проходят через SDK без
        изменений. Ленивый импорт — избегаем циклической зависимости
        (sdk.py импортит schema.py).
        """
        from backend.core.ontology.sdk import validate_object

        mode = self._effective_mode()
        res = validate_object(label, properties or {}, mode=mode)
        for w in res.warnings:
            logger.warning(f"⚠️ Schema Warning: {w}")
        for v in res.violations:
            logger.error(f"❌ Schema Violation: {v}")
        return res.ok

    def validate_relationship(self, from_label: str, to_label: str, rel_type: str) -> bool:
        """Проверяет, разрешена ли связь. P4: делегирует в Ontology SDK."""
        from backend.core.ontology.sdk import (
            validate_relationship as _vr,
        )

        mode = self._effective_mode()
        res = _vr(from_label, to_label, rel_type, mode=mode)
        for w in res.warnings:
            logger.warning(f"⚠️ Schema Warning: {w}")
        for v in res.violations:
            logger.error(f"❌ Schema Violation: {v}")
        return res.ok

# Глобальный инстанс
_validator = SchemaValidator(strict_mode=False) # Пока не строгий режим, чтобы не сломать существующее

def get_schema_validator() -> SchemaValidator:
    return _validator

