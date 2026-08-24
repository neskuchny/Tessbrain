"""Persona data models — стабильный контракт для Mini Tess и UI.

Изменения в этих структурах ломают интеграцию Mini Tess → меняем
через явный versioning и MINI_TESS_INTEGRATION.md.

Все Optional + дефолты: Persona строится постепенно по мере накопления
данных. Mini Tess получает то что есть; недозаполненные поля = None.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


# ───────────────────────────────────────────────────────────────
# Provenance — обязательный инвариант из Persona-спеки
# ───────────────────────────────────────────────────────────────


class ProvenanceSource(str, enum.Enum):
    """Откуда узнали данное поле Persona."""

    CAPTURE_AGENT = "capture_agent"  # извлечено одним из 32 capture-агентов
    MANUAL = "manual"  # founder/пользователь ввёл/перезаписал через UI
    MINI_TESS_INPUT = "mini_tess_input"  # пришло из Mini Tess (push)
    INFERRED = "inferred"  # выведено агрегатором из других полей
    EXTERNAL = "external"  # импортировано из внешнего сервиса (LinkedIn и т.п.)


@dataclass
class PersonaProvenanceEntry:
    """Один источник для одного поля Persona.

    field_path: dotted-path, например "communication_cognitive.metaphor_domains"
    source_type: какой пайплайн дал данные
    agent_or_actor: имя capture-агента / "user@email" / "mini_tess"
    """

    field_path: str
    source_type: ProvenanceSource
    agent_or_actor: str
    meeting_ids: list[str] = field(default_factory=list)
    extracted_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    confidence: float = 1.0  # 0.0-1.0
    raw_evidence: Optional[str] = None  # короткая цитата/snippet для audit


@dataclass
class PersonaProvenance:
    """Контейнер: field_path → последняя запись (current source-of-truth).

    Историю (все revisions) храним в отдельной таблице persona_field_history,
    здесь только actual для быстрого чтения.
    """

    entries: dict[str, PersonaProvenanceEntry] = field(default_factory=dict)

    def record(self, entry: PersonaProvenanceEntry) -> None:
        self.entries[entry.field_path] = entry

    def get(self, field_path: str) -> Optional[PersonaProvenanceEntry]:
        return self.entries.get(field_path)


# ───────────────────────────────────────────────────────────────
# Подфункция 1: коммуникативно-когнитивный профиль
# ───────────────────────────────────────────────────────────────


class CommunicationStyle(str, enum.Enum):
    DIRECT_CONCISE = "direct_concise"  # коротко, по делу
    DETAILED_ANALYTICAL = "detailed_analytical"  # развёрнуто с обоснованием
    NARRATIVE = "narrative"  # рассказ с примерами
    SOCRATIC = "socratic"  # вопросами
    DEFAULT = "default"


class AbstractionLevel(str, enum.Enum):
    CONCRETE = "concrete"  # любит цифры и факты
    BALANCED = "balanced"
    ABSTRACT = "abstract"  # любит концепции и метафоры


@dataclass
class CommunicationCognitive:
    """Подфункция 1: как с этим человеком разговаривать."""

    preferred_style: CommunicationStyle = CommunicationStyle.DEFAULT
    vocabulary_register: Optional[str] = None  # "tech_business", "academic", "casual"
    metaphor_domains: list[str] = field(default_factory=list)
    # ↑ примеры: ["sports", "engineering", "war", "music"] — какие источники
    # сравнений человек реально использует. Заполняется communication_style_agent.

    abstraction_level: AbstractionLevel = AbstractionLevel.BALANCED
    preferred_language: str = "ru"

    # Примеры реальных фраз/паттернов из встреч — для context при rewriter'е
    speech_patterns: list[dict[str, Any]] = field(default_factory=list)
    # каждый элемент: {"phrase": "...", "meeting_id": "...", "confidence": 0.9}


# ───────────────────────────────────────────────────────────────
# Подфункция 2: поведенческий профиль
# ───────────────────────────────────────────────────────────────


class ChannelPreference(str, enum.Enum):
    TELEGRAM = "telegram"
    VOICE_NOTES = "voice_notes"
    EMAIL_LONG = "email_long"
    EMAIL_SHORT = "email_short"
    SLACK = "slack"
    IN_PERSON = "in_person"
    CALL = "call"


@dataclass
class BehavioralProfile:
    """Подфункция 2: рабочий ритм, каналы, response patterns."""

    peak_hours_utc: list[int] = field(default_factory=list)  # часы 0-23 наивысшей продуктивности
    low_load_windows_utc: list[int] = field(default_factory=list)  # окна для "сигналов"

    avg_meetings_per_day: float = 0.0
    meeting_load_p95: float = 0.0  # 95-й процентиль за последний месяц

    channel_preferences: list[ChannelPreference] = field(default_factory=list)
    # ↑ упорядочено по предпочтительности: первое = primary

    response_latency_p50_min: Optional[int] = None  # обычное время ответа
    context_switching_tolerance: Optional[str] = None  # "low" / "medium" / "high"

    # Тайминговая фильтрация — используется Reactive engine (Phase C)
    do_not_disturb_windows_utc: list[dict[str, Any]] = field(default_factory=list)
    # ↑ [{"start_hour": 22, "end_hour": 8, "weekdays": [0,1,2,3,4]}, ...]


# ───────────────────────────────────────────────────────────────
# Подфункция 3: профиль развития
# ───────────────────────────────────────────────────────────────


@dataclass
class DevelopmentProfile:
    """Подфункция 3: куда движется, чему учится, какие цели."""

    current_projects: list[dict[str, Any]] = field(default_factory=list)
    # [{"id": "...", "name": "...", "focus_score": 0.85, "role": "..."}]

    active_goals: list[str] = field(default_factory=list)
    recent_learning: list[str] = field(default_factory=list)  # топики которые изучает
    # ↑ извлекается из chunks/transcripts через personal_interests_agent

    decision_style: Optional[str] = None
    # "data_driven" / "intuitive" / "consensus_seeking" / "deliberative"


# ───────────────────────────────────────────────────────────────
# Подфункция 4: сильные и слабые стороны
# ───────────────────────────────────────────────────────────────


@dataclass
class AreaAssessment:
    area: str  # "strategy", "sales", "technical_review", "hr_legal", ...
    confidence: float  # 0.0-1.0
    evidence_meeting_ids: list[str] = field(default_factory=list)


@dataclass
class StrengthsWeaknesses:
    """Подфункция 4: где силён, где слаб, какие blind spots."""

    strong: list[AreaAssessment] = field(default_factory=list)
    developing: list[AreaAssessment] = field(default_factory=list)
    blind_spots: list[AreaAssessment] = field(default_factory=list)
    # ↑ blind_spot = область которой избегает / не упоминает / признаёт незнание


# ───────────────────────────────────────────────────────────────
# Подфункция 5: профили клиентов (для Mini Tess)
# ───────────────────────────────────────────────────────────────


@dataclass
class ClientInteraction:
    date: str  # ISO
    channel: str  # call / email / meeting / message
    summary: str
    sentiment: Optional[float] = None  # -1.0 ... +1.0


@dataclass
class ClientArchetype:
    """Один клиент founder'а — данные накапливает Mini Tess, пушит сюда.

    Объединяется с графом: Company + Person + Pattern entities появляются
    автоматически при ингестии.
    """

    client_id: str  # external id из Mini Tess либо UUID
    name: str
    archetype: Optional[str] = None
    # ↑ стандартизованный enum в будущем; пока строка, согласуется с Mini Tess
    industry: Optional[str] = None
    deal_stage: Optional[str] = None
    # ↑ "discovery" / "negotiation" / "won" / "lost" / "stale"

    interactions: list[ClientInteraction] = field(default_factory=list)
    founder_notes: Optional[str] = None
    extracted_features: dict[str, Any] = field(default_factory=dict)
    # Гибкое поле: {"decision_style": "data_driven", "budget_signal": "high", ...}


# ───────────────────────────────────────────────────────────────
# Top-level Persona
# ───────────────────────────────────────────────────────────────


@dataclass
class Persona:
    """Aggregated Persona для одного user_id.

    Контракт стабилизируется в Phase A.1. Изменения требуют bump
    `schema_version` и обновления MINI_TESS_INTEGRATION.md.
    """

    user_id: str
    schema_version: str = "1.0"

    # Подфункции 1-4 — Tessbrain сам строит из capture-агентов
    communication_cognitive: CommunicationCognitive = field(
        default_factory=CommunicationCognitive
    )
    behavioral: BehavioralProfile = field(default_factory=BehavioralProfile)
    development: DevelopmentProfile = field(default_factory=DevelopmentProfile)
    strengths_weaknesses: StrengthsWeaknesses = field(
        default_factory=StrengthsWeaknesses
    )

    # Подфункция 5 — push from Mini Tess
    client_profiles: list[ClientArchetype] = field(default_factory=list)

    # Расширенные vendor-секции, которых нет в типизированном контракте
    # (Phase A.5 / Mini Tess B-эпик). Free-form JSONB-backed. Слот
    # extended["cogni"] формализован в core/cogni/dimensions.py (CogniLayer
    # Ф1.1): витки по доменам / световой конус / эквалайзер / фазовое состояние
    # со схемой, нормализацией и аксессорами. to_public_dict нормализует его на
    # выходе. extended["biography"] (roles_history/milestones) — пока narrative.
    extended: dict[str, Any] = field(default_factory=dict)

    # Provenance + metadata
    provenance: PersonaProvenance = field(default_factory=PersonaProvenance)
    source_meetings_count: int = 0
    confidence: float = 0.0  # глобальная уверенность профиля (function of count + agreement)

    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def _public_extended(self) -> dict[str, Any]:
        """Копия extended с нормализованным слотом ``cogni`` (CogniLayer Ф1.1).

        Наружу всегда идёт валидная схема когнитивных измерений, даже если
        внутри лежал частично заполненный dict — нормализация клампит диапазоны
        и отбрасывает мусор. Остальные vendor-секции extended копируются как есть.
        """
        out = {k: v for k, v in self.extended.items()}
        if "cogni" in out:
            try:
                from backend.core.cogni.dimensions import normalize_cogni
                out["cogni"] = normalize_cogni(out["cogni"])
            except Exception:  # never-raise: сериализация не должна падать
                pass
        return out

    def to_public_dict(self) -> dict[str, Any]:
        """Сериализация для API-ответа Mini Tess / web UI.

        Возвращает структуру совместимую с контрактом в MINI_TESS_INTEGRATION.md.
        Все enum'ы → string values. Provenance собирается компактным dict'ом.
        """
        return {
            "user_id": self.user_id,
            "schema_version": self.schema_version,
            "version": self.updated_at,
            "last_updated": self.updated_at,
            "source_meetings": self.source_meetings_count,
            "confidence": self.confidence,
            "communication_cognitive": {
                "preferred_style": self.communication_cognitive.preferred_style.value,
                "vocabulary_register": self.communication_cognitive.vocabulary_register,
                "metaphor_domains": list(self.communication_cognitive.metaphor_domains),
                "abstraction_level": self.communication_cognitive.abstraction_level.value,
                "preferred_language": self.communication_cognitive.preferred_language,
                "examples": list(self.communication_cognitive.speech_patterns),
            },
            "behavioral": {
                "work_rhythm": {
                    "peak_hours_utc": list(self.behavioral.peak_hours_utc),
                    "low_load_windows": list(self.behavioral.low_load_windows_utc),
                    "avg_meetings_per_day": self.behavioral.avg_meetings_per_day,
                },
                "channel_preferences": [c.value for c in self.behavioral.channel_preferences],
                "response_latency_p50_min": self.behavioral.response_latency_p50_min,
                "context_switching_tolerance": self.behavioral.context_switching_tolerance,
                "do_not_disturb_windows_utc": list(self.behavioral.do_not_disturb_windows_utc),
            },
            "development": {
                "current_projects": list(self.development.current_projects),
                "active_goals": list(self.development.active_goals),
                "recent_learning": list(self.development.recent_learning),
                "decision_style": self.development.decision_style,
            },
            "strengths_weaknesses": {
                "strong": [
                    {"area": a.area, "confidence": a.confidence}
                    for a in self.strengths_weaknesses.strong
                ],
                "developing": [
                    {"area": a.area, "confidence": a.confidence}
                    for a in self.strengths_weaknesses.developing
                ],
                "blind_spots": [
                    {"area": a.area, "evidence": a.evidence_meeting_ids}
                    for a in self.strengths_weaknesses.blind_spots
                ],
            },
            "client_profiles": {
                "archetypes": [
                    {
                        "client_id": c.client_id,
                        "name": c.name,
                        "archetype": c.archetype,
                        "industry": c.industry,
                        "deal_stage": c.deal_stage,
                        "founder_notes": c.founder_notes,
                        "extracted_features": dict(c.extracted_features),
                    }
                    for c in self.client_profiles
                ],
            },
            "extended": self._public_extended(),
            "provenance": {
                field_path: {
                    "source_type": entry.source_type.value,
                    "agent_or_actor": entry.agent_or_actor,
                    "meeting_ids": list(entry.meeting_ids),
                    "extracted_at": entry.extracted_at,
                    "confidence": entry.confidence,
                }
                for field_path, entry in self.provenance.entries.items()
            },
        }
