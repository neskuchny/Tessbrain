"""Design pipeline — design brief + Figma-link extraction (Phase B.2).

Для дизайнеров (продуктовый/UX/UI). После встречи где обсуждались макеты,
user-flow, copy или брендинг — готовит структурированный design-brief:
- Контекст/цели
- Аудитория и контекст использования
- Найденные ссылки на макеты (Figma/Miro/Notion) — извлекаются из транскрипта
- Чек-лист «что от тебя ждут»

Output:
- artifact_type = "design_brief"
- recommended_channels = ["notion", "telegram"] (design часто живёт в Notion)
- recommended_executor = None — для человека
"""
from __future__ import annotations

import re
from typing import Any, Optional

from backend.core.coffee.role_inference import Role, RoleAssignment
from backend.core.coffee.role_pipelines.base import RolePipeline, RolePipelineOutput


_DESIGN_SYSTEM_PROMPT = """\
Ты ассистент дизайнера. По итогам встречи готовишь design-brief — короткий
структурированный документ. Формат markdown строго:

# <Название инициативы>

## Контекст
1 параграф: почему этот дизайн нужен, что меняется

## Цель UX/UI
Одна формулировка измеримой цели (что улучшаем)

## Аудитория и сценарий
1-2 буллета: кто пользователь, в какой момент сталкивается с этим экраном

## Что обсудили
3-6 буллетов с ключевыми решениями встречи

## Чек-лист задач
- [ ] задача 1
- [ ] задача 2

## Open questions
Что НЕ решено и требует обсуждения

Без эмодзи. Язык русский. ≤500 слов."""


_FIGMA_LINK_RE = re.compile(
    r"https?://(?:www\.)?(?:figma\.com|miro\.com|notion\.so|sketch\.com)/[^\s)\"'>]+",
    re.IGNORECASE,
)


def _extract_design_links(meeting_data: dict[str, Any]) -> list[str]:
    """Собрать упоминания figma/miro/notion из транскрипта и summary."""
    haystack_parts: list[str] = []
    for key in ("transcript", "summary", "title"):
        v = meeting_data.get(key)
        if isinstance(v, str):
            haystack_parts.append(v)
    # Шарим по key_topics/decisions если строки
    for key in ("key_topics", "decisions", "tasks"):
        v = meeting_data.get(key) or []
        if isinstance(v, list):
            for item in v:
                if isinstance(item, str):
                    haystack_parts.append(item)
                elif isinstance(item, dict):
                    for vv in item.values():
                        if isinstance(vv, str):
                            haystack_parts.append(vv)
    haystack = "\n".join(haystack_parts)
    seen: set[str] = set()
    out: list[str] = []
    for m in _FIGMA_LINK_RE.findall(haystack):
        link = m.rstrip(".,);")
        if link not in seen:
            seen.add(link)
            out.append(link)
    return out


class DesignPipeline(RolePipeline):
    role = Role.DESIGN

    async def generate(
        self,
        *,
        role_assignment: RoleAssignment,
        meeting_data: dict[str, Any],
        persona: Optional[Any] = None,
        llm_router: Any = None,
    ) -> RolePipelineOutput:
        title = meeting_data.get("title") or "design-инициатива"
        summary = (meeting_data.get("summary") or "")[:2000]
        decisions = meeting_data.get("decisions") or []
        tasks = meeting_data.get("tasks") or []

        decisions_str = "\n".join(
            f"- {d.get('text') if isinstance(d, dict) else d}"
            for d in decisions[:6]
        ) or "(нет решений)"
        tasks_str = "\n".join(
            f"- {t.get('title') if isinstance(t, dict) else t}"
            for t in tasks[:6]
        ) or "(нет задач)"

        design_links = _extract_design_links(meeting_data)
        links_str = "\n".join(f"- {l}" for l in design_links) if design_links else "(ссылок не найдено)"

        prompt = (
            f"Сгенерируй design-brief по итогам встречи.\n\n"
            f"Встреча: {title!r}\n\n"
            f"Summary: {summary!r}\n\n"
            f"Принятые решения:\n{decisions_str}\n\n"
            f"Задачи:\n{tasks_str}\n\n"
            f"Найденные design-ссылки:\n{links_str}\n"
        )

        text = await self._safe_llm_call(
            llm_router=llm_router,
            prompt=prompt,
            system_prompt=_DESIGN_SYSTEM_PROMPT,
            max_tokens=1500,
            temperature=0.3,
            fallback_text=(
                f"# {title}\n\n"
                "## Контекст\nДизайн-обсуждение по итогам встречи. "
                "Полный транскрипт в источнике.\n\n"
                "## Цель UX/UI\nУточнить с автором встречи.\n\n"
                "## Чек-лист задач\n- [ ] Разобрать решения\n- [ ] Свериться с автором\n"
                f"\n## Найденные design-ссылки\n{links_str}\n"
            ),
        )

        if design_links and "design-ссыл" not in text.lower() and "figma" not in text.lower():
            text = (
                text.rstrip()
                + "\n\n## Найденные design-ссылки\n"
                + "\n".join(f"- {l}" for l in design_links)
                + "\n"
            )

        channel_hints = self._persona_channel_hints(persona)
        # Дизайнеры часто живут в Notion → recommend notion перед telegram
        if "notion" in channel_hints:
            ordered = ["notion", "telegram"]
        else:
            ordered = ["telegram", "notion"]

        return RolePipelineOutput(
            artifact_type="design_brief",
            role=Role.DESIGN,
            participant_id=role_assignment.participant_id,
            participant_name=role_assignment.participant_name,
            title=f"Design brief: {title}",
            content_markdown=text,
            content_structured={
                "design_links": design_links,
                "language": "ru",
            },
            recommended_channels=ordered,
            recommended_executor=None,
        )


__all__ = ["DesignPipeline"]
