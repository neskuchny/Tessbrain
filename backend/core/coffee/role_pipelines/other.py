"""Fallback pipeline для ролей без специфичного генератора (OTHER, DESIGN, HR).

Отдаёт универсальный personal summary встречи на основе персонализированных
hint'ов из Persona (если есть). Никакого role-specific шаблона.
"""
from __future__ import annotations

from typing import Any, Optional

from backend.core.coffee.role_inference import Role, RoleAssignment
from backend.core.coffee.role_pipelines.base import RolePipeline, RolePipelineOutput


_OTHER_SYSTEM_PROMPT = """\
Ты ассистент. По итогам встречи готовишь персональное summary для одного
участника. Формат markdown:

## Что обсудили
1 параграф

## Что касается тебя лично
2-4 буллета — то, что повлияет на этого конкретного участника

## Что делать дальше
2-4 буллета конкретных действий

Без эмодзи. Язык русский. ≤350 слов."""


class OtherPipeline(RolePipeline):
    role = Role.OTHER

    async def generate(
        self,
        *,
        role_assignment: RoleAssignment,
        meeting_data: dict[str, Any],
        persona: Optional[Any] = None,
        llm_router: Any = None,
    ) -> RolePipelineOutput:
        title = meeting_data.get("title") or "встречи"
        summary = (meeting_data.get("summary") or "")[:1500]
        decisions = meeting_data.get("decisions") or []

        decisions_str = "\n".join(
            f"- {d.get('text') if isinstance(d, dict) else d}"
            for d in decisions[:5]
        ) or "(нет решений)"

        prompt = (
            f"Подготовь персональное summary встречи.\n\n"
            f"Встреча: {title!r}\n"
            f"Участник: {role_assignment.participant_name!r}\n\n"
            f"Summary: {summary!r}\n\n"
            f"Принятые решения:\n{decisions_str}\n"
        )

        text = await self._safe_llm_call(
            llm_router=llm_router,
            prompt=prompt,
            system_prompt=_OTHER_SYSTEM_PROMPT,
            max_tokens=1200,
            temperature=0.3,
            fallback_text=(
                f"## Что обсудили\nВстреча '{title}'. "
                f"Полное summary в источнике.\n\n"
                "## Что касается тебя лично\n- См. transcript\n\n"
                "## Что делать дальше\n- Свериться с автором встречи"
            ),
        )

        channel_hints = self._persona_channel_hints(persona)
        return RolePipelineOutput(
            artifact_type="summary",
            role=Role.OTHER,
            participant_id=role_assignment.participant_id,
            participant_name=role_assignment.participant_name,
            title=f"Summary: {title}",
            content_markdown=text,
            content_structured={"language": "ru"},
            recommended_channels=channel_hints or ["telegram"],
            recommended_executor=None,
        )


__all__ = ["OtherPipeline"]
