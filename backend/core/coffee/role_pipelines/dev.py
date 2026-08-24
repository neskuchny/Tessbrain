"""Dev pipeline — ТЗ с архитектурным эскизом, готовое для AI-исполнителя.

Для разработчиков: после встречи где обсуждалась новая фича / баг / refactor —
генерирует структурированное ТЗ, которое можно сразу передать Claude Code / Cursor.

Output:
- artifact_type = "tz_document"
- recommended_executor = claude_code_cli / cursor_cli / codex_cli
  (выбор по Persona.development.preferred_executor; в Phase A это поле
  не заполняется автоматически — fallback на claude_code_cli)
"""
from __future__ import annotations

from typing import Any, Optional

from backend.core.coffee.role_inference import Role, RoleAssignment
from backend.core.coffee.role_pipelines.base import RolePipeline, RolePipelineOutput


_DEV_SYSTEM_PROMPT = """\
Ты архитектор задаёт ТЗ для inженера. Формат markdown строго:

# <Заголовок задачи>

## Контекст
1-2 параграфа: почему это нужно, на основе встречи

## Цель
Одна формулировка измеримой цели

## Архитектурный эскиз
- Какие сущности/модули затронуты
- Какие интерфейсы меняются
- Точки расширения / risk-ы

## Шаги реализации
Нумерованный список 3-7 шагов

## Acceptance criteria
- [ ] критерий 1
- [ ] критерий 2

## Open questions
Что НЕ решено и требует прояснения у автора задачи

Без эмодзи. Язык русский."""


class DevPipeline(RolePipeline):
    role = Role.DEV

    async def generate(
        self,
        *,
        role_assignment: RoleAssignment,
        meeting_data: dict[str, Any],
        persona: Optional[Any] = None,
        llm_router: Any = None,
    ) -> RolePipelineOutput:
        title = meeting_data.get("title") or "техническая задача"
        summary = (meeting_data.get("summary") or "")[:2000]
        decisions = meeting_data.get("decisions") or []
        tasks = meeting_data.get("tasks") or []
        key_topics = meeting_data.get("key_topics") or []

        decisions_str = "\n".join(
            f"- {d.get('text') if isinstance(d, dict) else d}"
            for d in decisions[:8]
        ) or "(нет решений)"
        tasks_str = "\n".join(
            f"- {t.get('title') if isinstance(t, dict) else t}"
            for t in tasks[:8]
        ) or "(нет задач)"
        topics_str = ", ".join(str(t) for t in key_topics[:6]) or "(не выделены)"

        prompt = (
            f"Сгенерируй ТЗ по итогам технического обсуждения.\n\n"
            f"Встреча: {title!r}\n\n"
            f"Summary: {summary!r}\n\n"
            f"Ключевые темы: {topics_str}\n\n"
            f"Принятые решения:\n{decisions_str}\n\n"
            f"Назначенные задачи:\n{tasks_str}\n"
        )

        text = await self._safe_llm_call(
            llm_router=llm_router,
            prompt=prompt,
            system_prompt=_DEV_SYSTEM_PROMPT,
            max_tokens=2500,
            temperature=0.2,
            fallback_text=(
                f"# {title}\n\n"
                "## Контекст\nПо итогам встречи требуется ТЗ. "
                "Полный транскрипт см. в источнике.\n\n"
                "## Шаги реализации\n1. Уточнить требования у автора\n"
                "2. Декомпозировать на под-задачи\n3. Реализовать\n\n"
                "## Open questions\n- Полный scope требует разбора"
            ),
        )

        channel_hints = self._persona_channel_hints(persona)
        # Default executor — claude_code_cli, потому что он лучше всех в коде
        # и работает с subscription auth. Можно перебить через
        # Persona.development.preferred_executor (поле появится в A.5).
        preferred_executor = "claude_code_cli"
        if persona is not None:
            try:
                pref = getattr(persona.development, "preferred_executor", None)
                if pref:
                    preferred_executor = pref
            except Exception:
                pass

        return RolePipelineOutput(
            artifact_type="tz_document",
            role=Role.DEV,
            participant_id=role_assignment.participant_id,
            participant_name=role_assignment.participant_name,
            title=title,
            content_markdown=text,
            content_structured={
                "tz_markdown": text,
                "language": "ru",
                "estimated_complexity": "medium",  # placeholder; A.5 — LLM-estimated
            },
            recommended_channels=(
                ["claude_code"] if preferred_executor == "claude_code_cli"
                else ["cursor"] if preferred_executor == "cursor_cli"
                else ["telegram"] + channel_hints
            ),
            recommended_executor=preferred_executor,
        )


__all__ = ["DevPipeline"]
