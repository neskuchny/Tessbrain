"""
BWE Models — модели данных Business Wisdom Engine

SQLAlchemy ORM для PostgreSQL + Pydantic v2 схемы для API.

Принципы:
- server_default для JSONB полей (избегаем mutable defaults)
- timezone-aware datetime везде
- встроенная дедупликация по meeting_id
- строгая типизация tension_type через список допустимых значений
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from backend.db.postgres import Base

# Допустимые типы бизнес-напряжений (для нормализации)
TENSION_TYPES = Literal[
    "growth_vs_margin",
    "speed_vs_quality",
    "innovation_vs_stability",
    "cost_vs_growth",
    "build_vs_buy",
    "hire_vs_outsource",
    "centralize_vs_decentralize",
    "short_term_vs_long_term",
    "revenue_vs_retention",
    "other",
]


# ─────────────────────────────────────────────
# SQLAlchemy ORM Models
# ─────────────────────────────────────────────

class BWEDecisionEvent(Base):
    """
    Слой 1: Решение, принятое на встрече.
    Извлекается LLM-пайплайном из транскрипта.
    """
    __tablename__ = "bwe_decision_events"
    __table_args__ = (
        # Дедупликация: один meeting_id → одна запись о решении (по порядку)
        # Уникальность по (user_id, meeting_id, situation) — защита от повторной обработки
        UniqueConstraint("user_id", "meeting_id", "decision", name="uq_bwe_de_user_meeting_decision"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String(255), nullable=False, index=True)
    meeting_id = Column(String(255), nullable=True, index=True)
    meeting_source = Column(String(500), nullable=True)

    # Тип ситуации (нормализованный, не конкретика)
    situation = Column(Text, nullable=False)
    tension_type = Column(String(50), nullable=True, index=True)
    options_considered = Column(JSON, nullable=True, server_default=text("'[]'::jsonb"))
    decision = Column(Text, nullable=False)
    rationale = Column(Text, nullable=True)

    # Контекст компании в момент решения
    company_vitek = Column(Integer, default=1)  # 1–4: стартап/рост/зрелость/экспансия
    market_context = Column(Text, nullable=True)
    decision_confidence = Column(String(20), default="medium")  # low/medium/high
    # Phase 2 D: эпистемика источника (observed/inferred/claimed/predicted).
    # Default claimed — консервативно (миграция 252). observed весит выше при синтезе.
    epistemic_type = Column(String(20), default="claimed")
    # D1 observer-effect: ожидаемый на момент решения исход
    # (success/failure/uncertain). Сравнение с фактическим даёт drift. Default
    # uncertain — нет ожидания, нет вклада в дрейф (миграция 253).
    predicted_outcome = Column(String(20), default="uncertain")
    # B1 bi-temporal (миграция 254): до КОГДА контекст решения истинен.
    # NULL = бессрочно. created_at = known_from (когда МЫ узнали).
    valid_until = Column(DateTime(timezone=True), nullable=True)

    # JEPA-encoded вектор ситуации (1536-dim OpenAI embedding)
    situation_embedding = Column(JSON, nullable=True)

    # Ссылка на паттерн (устанавливается при кластеризации)
    pattern_id = Column(String(36), nullable=True, index=True)

    outcome_linked = Column(Boolean, default=False, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    outcomes = relationship("BWEOutcomeEvent", back_populates="decision", cascade="all, delete-orphan")


class BWEOutcomeEvent(Base):
    """
    Слой 2: Исход принятого решения.
    Извлекается из ретроспективных встреч и метрик.
    """
    __tablename__ = "bwe_outcome_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String(255), nullable=False, index=True)
    decision_event_id = Column(UUID(as_uuid=True), ForeignKey("bwe_decision_events.id", ondelete="CASCADE"), nullable=False)
    meeting_id = Column(String(255), nullable=True, index=True)

    outcome_type = Column(String(20), nullable=False)  # success/failure/partial/unknown
    outcome_description = Column(Text, nullable=False)
    delay_days = Column(Integer, nullable=True)
    causal_factors = Column(JSON, nullable=True, server_default=text("'[]'::jsonb"))
    impact_magnitude = Column(Float, default=0.5)  # 0.0–1.0

    # JEPA-encoded вектор исхода
    outcome_embedding = Column(JSON, nullable=True)

    source_type = Column(String(30), default="retrospective")  # retrospective/metric/inline

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    decision = relationship("BWEDecisionEvent", back_populates="outcomes")


class BWEWisdomPattern(Base):
    """
    Слой 4: Wisdom Index — паттерн бизнес-ситуаций.
    Кластер похожих Decision Events с распределением исходов.
    """
    __tablename__ = "bwe_wisdom_patterns"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String(255), nullable=False, index=True)
    pattern_name = Column(String(200), nullable=False)

    # Центроид кластера в latent space
    prototype_vector = Column(JSON, nullable=False)

    # Список UUID Decision Events, образующих паттерн
    instances = Column(JSON, nullable=True, server_default=text("'[]'::jsonb"))

    # Распределение исходов: {"success": 3, "failure": 1, "partial": 0, "unknown": 0}
    outcome_distribution = Column(JSON, nullable=True, server_default=text("'{}'::jsonb"))

    # Чувствительность к витку компании
    vitek_sensitivity = Column(Text, nullable=True)

    # Что отличает успех от провала
    context_differentiators = Column(JSON, nullable=True, server_default=text("'[]'::jsonb"))

    # Уверенность: low (<3 случаев), medium (3-9), high (10+)
    confidence = Column(String(20), default="low")

    # Флаг: нужно ли пересчитать differentiators (после добавления новых исходов)
    needs_analysis_update = Column(Boolean, default=False)

    # ── Business Crystal (Understanding Layer, Module A) ──────────────────
    # Все поля nullable / с дефолтом → обратносовместимо (миграция 250).
    # «Оси» кристалла НЕ новая таксономия: композируют существующие
    # tension_type / company_vitek / KnowledgeCategory. Денормализованы сюда
    # ради быстрого фильтра поиска (search_with_axis).
    #
    # Денормализованный доминирующий tension_type связанных Decision Events.
    primary_tension = Column(String(100), nullable=True)
    # Границы применимости по фазе компании (company_vitek 1..4): если
    # кристалл валиден только на [min..max] витках — это not_law по фазе.
    vitek_phase_min = Column(Integer, nullable=True)
    vitek_phase_max = Column(Integer, nullable=True)
    # Категория знания из существующего KnowledgeCategory enum (если применимо).
    knowledge_category = Column(String(50), nullable=True)
    # Реально новое из ARC-методологии (причинно-логическое обогащение):
    # not_law — текстовые границы применимости («НЕ всегда, а при условии Z»).
    not_law = Column(JSON, nullable=True, server_default=text("'[]'::jsonb"))
    # open_holes — что схема ещё не покрывает (честные пробелы).
    open_holes = Column(JSON, nullable=True, server_default=text("'[]'::jsonb"))
    # confidence_score — численная асимметричная уверенность (≠ строковый
    # confidence выше). NULL = ещё не считалась → fallback на строковый.
    confidence_score = Column(Float, nullable=True)
    # Provenance: id эпизодов Graphiti, на которых кристалл основан.
    evidence_episode_ids = Column(JSON, nullable=True, server_default=text("'[]'::jsonb"))

    # ── Crystal Phase 1 (порт из Tessbrain, миграция 251) ───────────────────
    # A. Якорь свежести для time-decay confidence_score. Ставится в момент,
    # когда исходы меняют распределение. NULL → не угасать (back-compat).
    last_calibrated_at = Column(DateTime(timezone=True), nullable=True)
    # B. Версионированная история переформулировок ["v1: …","v2: …"]
    # (аналог concept_crystallizer.evolution_log из Tessbrain).
    evolution_log = Column(JSON, nullable=True, server_default=text("'[]'::jsonb"))
    # B2 retrieval-as-learning (миграция 254): сколько раз кристалл выдан в
    # запросах → сопротивление decay (часто полезный забывается медленнее).
    access_count = Column(Integer, nullable=True, server_default=text("0"))
    last_accessed_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


# ─────────────────────────────────────────────
# Pydantic v2 Schemas (API contracts)
# ─────────────────────────────────────────────

class DecisionEventCreate(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: str
    meeting_id: Optional[str] = None
    meeting_source: Optional[str] = None
    situation: str
    tension_type: Optional[str] = None
    options_considered: Optional[list[str]] = None
    decision: str
    rationale: Optional[str] = None
    company_vitek: int = Field(default=1, ge=1, le=4)
    market_context: Optional[str] = None
    decision_confidence: str = Field(default="medium")
    epistemic_type: str = Field(default="claimed")
    predicted_outcome: str = Field(default="uncertain")


class DecisionEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    meeting_id: Optional[str]
    meeting_source: Optional[str]
    situation: str
    tension_type: Optional[str]
    options_considered: Optional[list[str]]
    decision: str
    rationale: Optional[str]
    company_vitek: int
    market_context: Optional[str]
    decision_confidence: str
    epistemic_type: str = "claimed"
    predicted_outcome: str = "uncertain"
    pattern_id: Optional[str]
    outcome_linked: bool
    created_at: datetime


class OutcomeEventCreate(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: str
    decision_event_id: str
    meeting_id: Optional[str] = None
    outcome_type: str = Field(..., pattern="^(success|failure|partial|unknown)$")
    outcome_description: str
    delay_days: Optional[int] = None
    causal_factors: Optional[list[str]] = None
    impact_magnitude: float = Field(default=0.5, ge=0.0, le=1.0)
    source_type: str = Field(default="retrospective")


class OutcomeEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    decision_event_id: str
    meeting_id: Optional[str]
    outcome_type: str
    outcome_description: str
    delay_days: Optional[int]
    causal_factors: Optional[list[str]]
    impact_magnitude: float
    source_type: str
    created_at: datetime


class WisdomPatternRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    pattern_name: str
    instances: list[str]
    outcome_distribution: dict[str, int]
    vitek_sensitivity: Optional[str]
    context_differentiators: Optional[list[str]]
    confidence: str
    created_at: datetime
    updated_at: datetime


class SimilarPatternResult(BaseModel):
    pattern_id: str
    pattern_name: str
    similarity_score: float
    outcome_distribution: dict[str, int]
    confidence: str
    context_differentiators: Optional[list[str]] = None
    vitek_sensitivity: Optional[str] = None
    total_cases: int
    recent_decisions: list[dict[str, Any]] = Field(default_factory=list)
    # Crystal-поля (Module A). Optional с дефолтами → обратносовместимо:
    # существующие потребители ответа их просто не читают.
    primary_tension: Optional[str] = None
    vitek_phase_min: Optional[int] = None
    vitek_phase_max: Optional[int] = None
    confidence_score: Optional[float] = None
    not_law: list[str] = Field(default_factory=list)
    open_holes: list[str] = Field(default_factory=list)
    # Module C veto-gate: чем условия запроса отличаются от тех, на которых
    # кристалл валидирован (фаза/tension/not_law). Пусто = расхождений нет.
    distinguishing_axes: list[str] = Field(default_factory=list)


class WisdomQuery(BaseModel):
    user_id: str
    situation: str
    tension_type: Optional[str] = None
    company_vitek: int = Field(default=1, ge=1, le=4)
    market_context: Optional[str] = None
    max_patterns: int = Field(default=5, ge=1, le=20)


class WisdomResponse(BaseModel):
    query_situation: str
    similar_patterns: list[SimilarPatternResult]
    wisdom_narrative: str
    overall_confidence: str  # low/medium/high
    total_cases_analyzed: int
    recommendation: Optional[str] = None


class ProcessMeetingRequest(BaseModel):
    user_id: str
    transcript: str
    meeting_id: Optional[str] = None
    meeting_source: Optional[str] = None
    company_vitek: int = Field(default=1, ge=1, le=4)
    market_context: Optional[str] = None
    is_retrospective: bool = False


class ProcessMeetingResult(BaseModel):
    meeting_id: Optional[str]
    decisions_extracted: int
    outcomes_linked: int
    patterns_updated: int
    skipped_duplicates: int = 0
    decision_events: list[DecisionEventRead] = Field(default_factory=list)
    outcome_events: list[OutcomeEventRead] = Field(default_factory=list)
