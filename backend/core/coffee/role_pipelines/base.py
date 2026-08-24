"""RolePipeline base — единый контракт для всех role-typed генераторов.

Phase B Coffee scenario, шаг 2 фундамент.

Контракт RolePipeline:
- generate(role_assignment, meeting_data, persona, llm_router?) → RolePipelineOutput
- artifact_type: имя типа артефакта для UI/routing
  (email_draft / tz_document / jira_cards / strategic_brief / summary)
- recommended_channels: какие каналы доставки логично для этого артефакта
  (используется delivery.pick_channels как hint)
- recommended_executor: какой Executor (claude_code_cli / cursor_cli / codex / None)
  если артефакт — задача для AI-исполнителя

Все pipelines best-effort: при ошибке LLM возвращают RolePipelineOutput с
success=False и text fallback («Не удалось сгенерировать черновик; полный
транскрипт в источнике»). Coffee orchestrator продолжает с другими ролями.
"""
from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from backend.core.coffee.role_inference import Role, RoleAssignment

logger = logging.getLogger(__name__)


@dataclass
class RolePipelineOutput:
    """Результат генерации одного артефакта."""

    artifact_type: str  # "email_draft" / "tz_document" / "jira_cards" / "strategic_brief" / "summary"
    role: Role
    participant_id: str
    participant_name: str
    title: str
    content_markdown: str  # человекочитаемый body
    content_structured: dict[str, Any] = field(default_factory=dict)
    # ↑ Specific-for-artifact data: для jira_cards — список задач,
    # для email_draft — subject+body+recipients_guess, для tz — sections

    recommended_channels: list[str] = field(default_factory=list)
    # ↑ "telegram" / "email" / "notion" / "claude_code" / "cursor" / ...
    recommended_executor: Optional[str] = None
    # ↑ None если артефакт — для человека; имя executor backend если для AI

    success: bool = True
    error_message: str = ""
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    generation_cost_usd: float = 0.0
    generation_duration_ms: int = 0


class RolePipeline(abc.ABC):
    """Базовый класс для всех role-typed генераторов."""

    role: Role = Role.OTHER  # переопределяется в подклассах

    @abc.abstractmethod
    async def generate(
        self,
        *,
        role_assignment: RoleAssignment,
        meeting_data: dict[str, Any],
        persona: Optional[Any] = None,
        llm_router: Any = None,
    ) -> RolePipelineOutput:
        """Сгенерировать артефакт для одного участника.

        Args:
            role_assignment: результат RoleInference
            meeting_data: {
                "id": meeting_id,
                "title": "...",
                "transcript": "...",  # может быть None если streaming
                "summary": "...",
                "participants": [...],
                "tasks": [...],         # из capture-agents (опционально)
                "decisions": [...],
                "key_topics": [...],
            }
            persona: backend.core.persona.Persona текущего participant'а;
                None если не построена
            llm_router: для генерации текста; None — pipeline должен дать
                deterministic fallback без LLM
        """

    async def _safe_llm_call(
        self,
        *,
        llm_router: Any,
        prompt: str,
        system_prompt: Optional[str] = None,
        max_tokens: int = 1500,
        temperature: float = 0.3,
        fallback_text: str = "",
    ) -> str:
        """Wrapper для LLM-вызова с fallback. Never-raise."""
        if llm_router is None:
            return fallback_text
        try:
            return await llm_router.generate(
                prompt=prompt,
                system_prompt=system_prompt,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as exc:
            logger.warning(
                "[coffee.%s] LLM call failed: %s",
                self.role.value, exc,
            )
            return fallback_text

    def _persona_channel_hints(self, persona: Optional[Any]) -> list[str]:
        """Подсказать список каналов из Persona.behavioral.channel_preferences."""
        if persona is None:
            return []
        try:
            return [
                c.value if hasattr(c, "value") else str(c)
                for c in (persona.behavioral.channel_preferences or [])
            ]
        except Exception:
            return []


__all__ = ["RolePipeline", "RolePipelineOutput"]
