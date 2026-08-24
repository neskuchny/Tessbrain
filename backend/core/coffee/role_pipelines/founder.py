"""Founder pipeline — стратегический бриф + 3-5 вопросов на подумать.

Для CEO/CTO/co-founder'ов. После стратегической встречи (партнёрство,
big-bet, инвестор-pitch, hire decision) генерирует:
- 1-page бриф с ключевыми выводами и risks
- 3-5 «вопросов на подумать» — open questions требующих личной рефлексии

Channel preference: Telegram pinned message (быстро прочесть в дороге).
"""
from __future__ import annotations

from typing import Any, Optional

from backend.core.coffee.role_inference import Role, RoleAssignment
from backend.core.coffee.role_pipelines.base import RolePipeline, RolePipelineOutput


_FOUNDER_SYSTEM_PROMPT = """\
Ты strategic advisor founder'а. По итогам встречи готовишь СТРАТЕГИЧЕСКИЙ
БРИФ. Формат markdown строго:

## Ключевые выводы
3-5 буллетов. Не пересказ, а вытяжка смысла.

## Что это меняет
1-2 параграфа: какие предыдущие решения/предположения теперь под вопросом

## Risks
3-5 буллетов. Конкретные, не общие («рынок может упасть» — не риск).

## Open questions
3-5 буллетов в формате вопросов. Эти вопросы founder задаст себе сам в
свободные 15 минут вечером — потенциал для важных insight'ов.

Без жаргона. Никаких эмодзи. Язык русский.
Бриф должен быть таким, чтобы founder прочёл за 2 минуты."""


class FounderPipeline(RolePipeline):
    role = Role.FOUNDER

    async def generate(
        self,
        *,
        role_assignment: RoleAssignment,
        meeting_data: dict[str, Any],
        persona: Optional[Any] = None,
        llm_router: Any = None,
    ) -> RolePipelineOutput:
        title = meeting_data.get("title") or "встречи"
        summary = (meeting_data.get("summary") or "")[:2000]
        decisions = meeting_data.get("decisions") or []
        opinions = meeting_data.get("opinions") or []

        decisions_str = "\n".join(
            f"- {d.get('text') if isinstance(d, dict) else d}"
            for d in decisions[:8]
        ) or "(нет извлечённых решений)"
        opinions_str = "\n".join(
            f"- {o.get('text') if isinstance(o, dict) else o}"
            for o in opinions[:8]
        ) or "(нет извлечённых мнений)"

        # Дополнительный контекст: текущие goals из Persona — чтобы бриф
        # учитывал стратегические приоритеты founder'а
        persona_goals_str = ""
        if persona is not None:
            try:
                goals = persona.development.active_goals or []
                if goals:
                    persona_goals_str = "\nТекущие стратегические цели:\n" + \
                        "\n".join(f"- {g}" for g in goals[:5])
            except Exception:
                pass

        prompt = (
            f"Сгенерируй стратегический бриф по итогам встречи.\n\n"
            f"Встреча: {title!r}\n\n"
            f"Summary: {summary!r}\n\n"
            f"Принятые решения:\n{decisions_str}\n\n"
            f"Высказанные мнения:\n{opinions_str}\n"
            f"{persona_goals_str}"
        )

        text = await self._safe_llm_call(
            llm_router=llm_router,
            prompt=prompt,
            system_prompt=_FOUNDER_SYSTEM_PROMPT,
            max_tokens=2000,
            temperature=0.35,
            fallback_text=(
                f"## Ключевые выводы\n- По встрече '{title}' зафиксированы решения\n"
                "- Полный транскрипт в источнике\n\n"
                "## Open questions\n- Что я понял по встрече иначе чем команда?\n"
                "- Какое одно действие в течение 24 ч изменит ход дальнейших событий?"
            ),
        )

        channel_hints = self._persona_channel_hints(persona)
        return RolePipelineOutput(
            artifact_type="strategic_brief",
            role=Role.FOUNDER,
            participant_id=role_assignment.participant_id,
            participant_name=role_assignment.participant_name,
            title=f"Стратегический бриф: {title}",
            content_markdown=text,
            content_structured={
                "brief_markdown": text,
                "language": "ru",
            },
            recommended_channels=(
                # Founder обычно хочет видеть бриф быстро в telegram
                ["telegram"] + [c for c in channel_hints if c != "telegram"]
            ),
            recommended_executor=None,
        )


__all__ = ["FounderPipeline"]
