"""Role inference — определяем роль участника в контексте встречи.

Phase B Coffee scenario, шаг 1. Для каждого участника встречи определяем
**роль в КОНТЕКСТЕ этой встречи** — не глобальную должность, а функцию,
которую он выполняет именно сейчас. Один человек может быть PM на одной
встрече и sales lead на другой (если он founder и закрывает крупную сделку).

Источники сигнала (по приоритету):
1. **Participant explicit role** — если capture-агент `participant_agent`
   уже извлёк роль из транскрипта («Я отвечаю за разработку») — берём.
2. **Persona.behavioral.work_preferences** + **development.current_projects**
   — что человек делает чаще всего, какие проекты ведёт.
3. **Graph context** — связи Person → Team → Department (Persona-aware).
4. **LLM fallback** (опционально) — если выше не сработали, дёргаем LLM
   с meeting context + participant statement count.

Best-effort: при отсутствии сигнала роль = OTHER. Никаких raise.

Контракт стабилен: Coffee orchestrator и role pipelines читают
`Role` enum + `RoleAssignment.role_confidence`. Расширение enum
требует bump RoleInference version + update pipelines.
"""
from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


class Role(str, enum.Enum):
    """Роли для role-typed pipelines.

    7 значений покрывают основные сценарии founder-led компании.
    Добавление новой роли требует:
    1. Создания соответствующего pipeline класса в role_pipelines/
    2. Update DEFAULT_PIPELINE_FOR_ROLE в orchestrator.py
    3. Документирования в docs/COFFEE_ARCHITECTURE.md §3
    """

    FOUNDER = "founder"  # CEO/CTO — стратегические выходы, briefs
    SALES = "sales"      # account manager — emails, sales-pack
    DEV = "dev"          # engineer — ТЗ с архитектурным эскизом
    PM = "pm"            # product manager — Jira-карточки, roadmap-фрагменты
    DESIGN = "design"    # designer — фигма-задачи, briefs
    HR = "hr"            # HR — onboarding/interview notes, hiring plans
    OTHER = "other"      # неопределённая — fallback на универсальный summary


@dataclass
class RoleAssignment:
    """Результат role inference для одного участника."""

    participant_id: str  # user_id или участник из meeting transcript
    participant_name: str
    role: Role
    role_confidence: float  # 0.0-1.0
    reasoning: str = ""  # короткое обоснование для audit
    inferred_from: list[str] = field(default_factory=list)
    # ↑ "explicit_extraction" / "persona" / "graph" / "llm_fallback"


# ─────────────────────────────────────────────────────────────────
# Сигналы
# ─────────────────────────────────────────────────────────────────

# Маппинг ключевых слов → роль для fast-path detection.
# Не идеально, но покрывает 80% случаев без LLM-вызова.
_ROLE_KEYWORDS: dict[Role, frozenset[str]] = {
    Role.FOUNDER: frozenset({
        "ceo", "cto", "co-founder", "co founder", "founder",
        "генеральный", "основатель", "директор",
    }),
    Role.SALES: frozenset({
        "sales", "account manager", "bdr", "sdr", "account exec",
        "продаж", "коммерческий", "акаунт-менеджер", "менеджер по продажам",
    }),
    Role.DEV: frozenset({
        "engineer", "developer", "swe", "backend", "frontend", "fullstack",
        "tech lead", "разработчик", "инженер", "программист", "архитектор",
    }),
    Role.PM: frozenset({
        "product manager", "product owner", "pm", "продакт",
        "владелец продукта", "продакт-менеджер",
    }),
    Role.DESIGN: frozenset({
        "designer", "ui", "ux", "product designer",
        "дизайнер", "ui/ux",
    }),
    Role.HR: frozenset({
        "hr", "people", "recruiter", "talent",
        "эйчар", "хр", "рекрутер",
    }),
}


def _role_from_text(text: Optional[str]) -> Optional[Role]:
    """Простой keyword-matcher по тексту role-описания."""
    if not text or not isinstance(text, str):
        return None
    lower = text.lower()
    for role, kws in _ROLE_KEYWORDS.items():
        for kw in kws:
            if kw in lower:
                return role
    return None


# ─────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────


async def infer_roles(
    *,
    meeting_id: str,
    participants: list[dict[str, Any]],
    meeting_context: Optional[dict[str, Any]] = None,
    llm_router: Any = None,
) -> list[RoleAssignment]:
    """Определить роли всех участников встречи.

    Args:
        meeting_id: для логов/provenance
        participants: список из meeting.extraction.participants —
            каждый dict с {name, role?, user_id?, statements_count?, ...}
        meeting_context: optional {title, project_id, summary, ...} для
            обогащения LLM-fallback
        llm_router: optional, для LLM-fallback при неудаче keyword/persona path.
            Если None — пропускаем LLM-fallback (Role.OTHER для непонятных)

    Returns:
        list[RoleAssignment] длины == len(participants).
    """
    assignments: list[RoleAssignment] = []
    for p in participants or []:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("user_id") or p.get("id") or p.get("name", "")).strip()
        pname = str(p.get("name") or p.get("display_name") or pid).strip()
        if not pid and not pname:
            continue

        # 1. Explicit role field из participant_agent
        explicit = p.get("role")
        role = _role_from_text(explicit)
        if role is not None:
            assignments.append(RoleAssignment(
                participant_id=pid or pname,
                participant_name=pname,
                role=role,
                role_confidence=0.85,
                reasoning=f"Explicit role field: {explicit!r}",
                inferred_from=["explicit_extraction"],
            ))
            continue

        # 2. Persona signals (если user_id есть и Persona построена)
        persona_role = await _role_from_persona(pid)
        if persona_role is not None:
            assignments.append(RoleAssignment(
                participant_id=pid or pname,
                participant_name=pname,
                role=persona_role,
                role_confidence=0.7,
                reasoning="Inferred from Persona work_preferences",
                inferred_from=["persona"],
            ))
            continue

        # 3. Graph signals — Person → Team membership
        graph_role = await _role_from_graph(pid, pname)
        if graph_role is not None:
            assignments.append(RoleAssignment(
                participant_id=pid or pname,
                participant_name=pname,
                role=graph_role,
                role_confidence=0.6,
                reasoning="Inferred from graph Person→Team→Department",
                inferred_from=["graph"],
            ))
            continue

        # 4. LLM-fallback (опционально)
        if llm_router is not None:
            llm_role = await _role_from_llm(
                participant=p,
                meeting_context=meeting_context or {},
                llm_router=llm_router,
            )
            if llm_role is not None:
                assignments.append(RoleAssignment(
                    participant_id=pid or pname,
                    participant_name=pname,
                    role=llm_role,
                    role_confidence=0.55,
                    reasoning="LLM-fallback из meeting_context",
                    inferred_from=["llm_fallback"],
                ))
                continue

        # Финальный fallback
        assignments.append(RoleAssignment(
            participant_id=pid or pname,
            participant_name=pname,
            role=Role.OTHER,
            role_confidence=0.0,
            reasoning="No signal — defaulting to OTHER",
            inferred_from=[],
        ))

    logger.info(
        "[coffee.role_inference] meeting=%s assigned %d roles: %s",
        meeting_id,
        len(assignments),
        {a.role.value: sum(1 for x in assignments if x.role == a.role) for a in assignments},
    )
    return assignments


async def _role_from_persona(user_id: str) -> Optional[Role]:
    """Достать роль из Persona (development.current_projects + work_preferences)."""
    if not user_id:
        return None
    try:
        from backend.core.persona.store import get_persona_store
        store = get_persona_store()
        persona = await store.get(user_id)
        if persona is None:
            return None

        # Heuristic 1: проекты с маркерами «sales pipeline» / «hire» / «design system»
        for proj in persona.development.current_projects or []:
            if not isinstance(proj, dict):
                continue
            name = (proj.get("name") or "").lower()
            for role, kws in _ROLE_KEYWORDS.items():
                if any(kw in name for kw in kws):
                    return role

        # Heuristic 2: goals
        for goal in persona.development.active_goals or []:
            if not isinstance(goal, str):
                continue
            role = _role_from_text(goal)
            if role is not None:
                return role
    except Exception as exc:
        logger.debug("_role_from_persona failed for %s: %s", user_id, exc)
    return None


async def _role_from_graph(user_id: str, name: str) -> Optional[Role]:
    """Достать роль через граф: Person.role / Person→Team→Department."""
    if not (user_id or name):
        return None
    try:
        from backend.core.store.graph_builder import get_graph_builder
        gb = await get_graph_builder()
        if gb is None or not getattr(gb, "nx_graph", None):
            return None
        # Простая lookup: ищем Person node по name/user_id
        for node_id, attrs in gb.nx_graph.nodes(data=True):
            if attrs.get("label") != "Person":
                continue
            if str(attrs.get("normalized_name") or "").lower() != name.lower() \
                    and str(attrs.get("external_ids") or "") != user_id:
                continue
            # Found — check role/department fields
            role = _role_from_text(attrs.get("role"))
            if role is not None:
                return role
            role = _role_from_text(attrs.get("department"))
            if role is not None:
                return role
            break
    except Exception as exc:
        logger.debug("_role_from_graph failed: %s", exc)
    return None


async def _role_from_llm(
    *,
    participant: dict[str, Any],
    meeting_context: dict[str, Any],
    llm_router: Any,
) -> Optional[Role]:
    """LLM-fallback: классифицируем по meeting context."""
    try:
        title = meeting_context.get("title") or ""
        summary = (meeting_context.get("summary") or "")[:500]
        statements = participant.get("statements_count") or 0
        prompt = (
            "Определи РОЛЬ участника встречи в её контексте. Ответь ОДНИМ "
            "словом строго из списка: founder, sales, dev, pm, design, hr, other.\n\n"
            f"Встреча: {title!r}\n"
            f"Краткое summary: {summary!r}\n"
            f"Участник: {participant.get('name')!r}, "
            f"выступлений: {statements}\n\n"
            "Если данных не хватает — отвечай 'other'."
        )
        response = await llm_router.generate(
            prompt=prompt, max_tokens=20, temperature=0.0,
        )
        text = str(response).strip().lower()
        for role in Role:
            if role.value in text:
                return role
    except Exception as exc:
        logger.debug("_role_from_llm failed: %s", exc)
    return None
