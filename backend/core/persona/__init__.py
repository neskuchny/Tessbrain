"""Persona service — agregated longitudinal profile founder'а / сотрудника.

Phase A видения (`docs/tessent_core/Persona-спека.md`): 5 подфункций
коммуникативно-когнитивного + поведенческого + развития + сильных-слабых +
клиентов (последнее — для Mini Tess).

Архитектура:
- PersonaStore — read/write один longitudinal-документ per user_id с provenance
- PersonaAggregator — собирает данные из существующих 32 capture-агентов
  (communication_style_agent, psychological_analyzer, work_preferences_agent,
  personal_interests_agent, и пр.) → суммирует в подфункции Persona
- ClientArchetypeIngester — принимает push от Mini Tess (подфункция 5),
  сохраняет в Persona + создаёт сущности Person/Company/Pattern в графе

Phase A.1 — этот skeleton. Реализация подфункций 1-4 — A.1 → A.2.
Подфункция 5 (клиенты) — после Phase A; делается совместно с Mini Tess.

Privacy-инварианты (3 из спеки):
1. Provenance-tracking: каждое поле знает source (capture_agent / manual /
   mini_tess_input / inferred) + meeting_ids
2. Context-filtered output: при запросе передаётся audience-context, мы
   фильтруем что нельзя показывать (например HR-инфо не для всех)
3. User-visible audit: founder через UI может посмотреть provenance и
   override любое поле (manual takes priority)
"""
from backend.core.persona.models import (
    AbstractionLevel,
    AreaAssessment,
    BehavioralProfile,
    ChannelPreference,
    ClientArchetype,
    ClientInteraction,
    CommunicationCognitive,
    CommunicationStyle,
    DevelopmentProfile,
    Persona,
    PersonaProvenance,
    PersonaProvenanceEntry,
    ProvenanceSource,
    StrengthsWeaknesses,
)
from backend.core.persona.store import PersonaStore, get_persona_store

__all__ = [
    "AbstractionLevel",
    "AreaAssessment",
    "BehavioralProfile",
    "ChannelPreference",
    "ClientArchetype",
    "ClientInteraction",
    "CommunicationCognitive",
    "CommunicationStyle",
    "DevelopmentProfile",
    "Persona",
    "PersonaProvenance",
    "PersonaProvenanceEntry",
    "PersonaStore",
    "ProvenanceSource",
    "StrengthsWeaknesses",
    "get_persona_store",
]
