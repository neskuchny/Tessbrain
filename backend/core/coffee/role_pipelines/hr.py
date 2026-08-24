"""HR pipeline — interview-pack / onboarding notes / hiring plan (Phase B.2).

Для HR-специалистов. После встречи где обсуждались кандидаты, найм,
onboarding или 1:1 с сотрудником — готовит:
- Краткий профиль человека/роли
- Вопросы для следующего интервью (если найм)
- Action items для onboarding (если новенький)
- Risk flags для team-health (если 1:1)

Output:
- artifact_type = "hr_pack"
- recommended_channels = ["email_draft", "telegram"] (для HR важна почта)
- recommended_executor = None — для человека
"""
from __future__ import annotations

from typing import Any, Optional

from backend.core.coffee.role_inference import Role, RoleAssignment
from backend.core.coffee.role_pipelines.base import RolePipeline, RolePipelineOutput


_HR_SYSTEM_PROMPT = """\
Ты ассистент HR-специалиста. По итогам встречи готовишь HR-pack —
короткий документ. Формат markdown строго:

# <Тема>

## Тип встречи
Один из: интервью кандидата / onboarding / 1:1 с сотрудником / другое

## Краткое summary
1 параграф

## Ключевые наблюдения
3-6 буллетов: сильные/слабые стороны, риски, договорённости

## Следующие действия
- [ ] действие 1 (срок)
- [ ] действие 2 (срок)

## Вопросы для следующей встречи
3-5 коротких вопросов

Без эмодзи. Язык русский. Конфиденциально по умолчанию. ≤450 слов."""


class HRPipeline(RolePipeline):
    role = Role.HR

    async def generate(
        self,
        *,
        role_assignment: RoleAssignment,
        meeting_data: dict[str, Any],
        persona: Optional[Any] = None,
        llm_router: Any = None,
    ) -> RolePipelineOutput:
        title = meeting_data.get("title") or "HR-встреча"
        summary = (meeting_data.get("summary") or "")[:2000]
        decisions = meeting_data.get("decisions") or []
        tasks = meeting_data.get("tasks") or []
        participants = meeting_data.get("participants") or []

        decisions_str = "\n".join(
            f"- {d.get('text') if isinstance(d, dict) else d}"
            for d in decisions[:6]
        ) or "(нет решений)"
        tasks_str = "\n".join(
            f"- {t.get('title') if isinstance(t, dict) else t}"
            for t in tasks[:6]
        ) or "(нет задач)"

        # Кратко перечислим, кто ещё был на встрече кроме самого HR'а
        others = [
            p.get("name") or p.get("user_id") or "?"
            for p in participants
            if isinstance(p, dict)
            and str(p.get("user_id") or p.get("id") or "") != role_assignment.participant_id
        ]
        others_str = ", ".join(o for o in others if o) or "(только HR)"

        prompt = (
            f"Сгенерируй HR-pack по итогам встречи.\n\n"
            f"Встреча: {title!r}\n"
            f"Другие участники: {others_str}\n\n"
            f"Summary: {summary!r}\n\n"
            f"Принятые решения:\n{decisions_str}\n\n"
            f"Задачи:\n{tasks_str}\n"
        )

        text = await self._safe_llm_call(
            llm_router=llm_router,
            prompt=prompt,
            system_prompt=_HR_SYSTEM_PROMPT,
            max_tokens=1500,
            temperature=0.3,
            fallback_text=(
                f"# {title}\n\n"
                "## Тип встречи\nдругое\n\n"
                "## Краткое summary\nПодробности в источнике встречи.\n\n"
                "## Следующие действия\n- [ ] Свериться с автором встречи\n\n"
                "## Вопросы для следующей встречи\n- Какие договорённости в фокусе?"
            ),
        )

        channel_hints = self._persona_channel_hints(persona)
        if "email_long" in channel_hints or "email_short" in channel_hints:
            ordered = ["email_draft", "telegram"]
        else:
            ordered = ["telegram", "email_draft"]

        return RolePipelineOutput(
            artifact_type="hr_pack",
            role=Role.HR,
            participant_id=role_assignment.participant_id,
            participant_name=role_assignment.participant_name,
            title=f"HR-pack: {title}",
            content_markdown=text,
            content_structured={
                "language": "ru",
                "confidential": True,
            },
            recommended_channels=ordered,
            recommended_executor=None,
        )


__all__ = ["HRPipeline"]
