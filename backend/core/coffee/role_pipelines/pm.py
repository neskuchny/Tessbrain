"""PM pipeline — Jira-карточки + roadmap-фрагмент.

Для продактов: разбивает обсуждение на карточки в стиле Jira /
Linear / Trello, плюс короткий roadmap-fragment для синхронизации.

Output:
- artifact_type = "jira_cards"
- content_structured["cards"] = list[{summary, description, priority, labels}]
- Channel: notion (если у PM есть Notion integration), email, telegram
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from backend.core.coffee.role_inference import Role, RoleAssignment
from backend.core.coffee.role_pipelines.base import RolePipeline, RolePipelineOutput


_PM_SYSTEM_PROMPT = """\
Ты ассистент продакт-менеджера. По итогам встречи строишь набор
Jira-карточек в JSON-формате СТРОГО:

{
  "summary_of_meeting": "1-2 предложения",
  "roadmap_fragment": "короткий impact на текущий roadmap, 1-3 предложения",
  "cards": [
    {
      "summary": "<заголовок карточки, ≤80 символов>",
      "description": "<детали 2-4 предложения>",
      "priority": "high" | "medium" | "low",
      "labels": ["category1", "category2"],
      "estimated_points": 1 | 2 | 3 | 5 | 8
    }
  ]
}

3-7 карточек. Без markdown в полях. Язык русский. Только JSON."""


class PMPipeline(RolePipeline):
    role = Role.PM

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
        tasks = meeting_data.get("tasks") or []
        decisions = meeting_data.get("decisions") or []

        tasks_str = "\n".join(
            f"- {t.get('title') if isinstance(t, dict) else t}"
            for t in tasks[:10]
        ) or "(нет извлечённых задач)"
        decisions_str = "\n".join(
            f"- {d.get('text') if isinstance(d, dict) else d}"
            for d in decisions[:5]
        ) or "(нет извлечённых решений)"

        prompt = (
            f"Сгенерируй карточки по итогам встречи.\n\n"
            f"Встреча: {title!r}\n\n"
            f"Summary: {summary!r}\n\n"
            f"Принятые решения:\n{decisions_str}\n\n"
            f"Назначенные задачи:\n{tasks_str}\n"
        )

        raw = await self._safe_llm_call(
            llm_router=llm_router,
            prompt=prompt,
            system_prompt=_PM_SYSTEM_PROMPT,
            max_tokens=2000,
            temperature=0.2,
            fallback_text=json.dumps({
                "summary_of_meeting": f"Встреча {title}",
                "roadmap_fragment": "Требуется разбор автора",
                "cards": [
                    {
                        "summary": (t.get("title") if isinstance(t, dict) else str(t))[:80],
                        "description": str(t)[:400],
                        "priority": "medium",
                        "labels": [],
                        "estimated_points": 3,
                    }
                    for t in (tasks or [])[:5]
                ],
            }, ensure_ascii=False),
        )

        # Парсим JSON — устойчиво к code-fence и truncated output
        parsed = {}
        try:
            parsed = json.loads(raw)
        except Exception:
            m = re.search(r"\{[\s\S]*\}", raw)
            if m:
                try:
                    parsed = json.loads(m.group(0))
                except Exception:
                    parsed = {}

        cards = parsed.get("cards") if isinstance(parsed, dict) else []
        roadmap_fragment = parsed.get("roadmap_fragment", "") if isinstance(parsed, dict) else ""
        m_summary = parsed.get("summary_of_meeting", "") if isinstance(parsed, dict) else ""

        if not isinstance(cards, list):
            cards = []

        markdown_lines = [
            f"# Карточки по встрече '{title}'",
            "",
            f"_{m_summary}_" if m_summary else "",
            "",
            f"**Roadmap impact:** {roadmap_fragment}" if roadmap_fragment else "",
            "",
            "## Карточки",
        ]
        for i, c in enumerate(cards, 1):
            if not isinstance(c, dict):
                continue
            markdown_lines.append(
                f"### {i}. {c.get('summary', '...')}"
                f"  *(priority: {c.get('priority', 'medium')}, "
                f"points: {c.get('estimated_points', '?')})*"
            )
            markdown_lines.append("")
            markdown_lines.append(c.get("description", ""))
            labels = c.get("labels") or []
            if labels:
                markdown_lines.append(
                    f"_labels: {', '.join(str(l) for l in labels)}_"
                )
            markdown_lines.append("")

        channel_hints = self._persona_channel_hints(persona)
        return RolePipelineOutput(
            artifact_type="jira_cards",
            role=Role.PM,
            participant_id=role_assignment.participant_id,
            participant_name=role_assignment.participant_name,
            title=f"Карточки по '{title}'",
            content_markdown="\n".join(l for l in markdown_lines if l is not None),
            content_structured={
                "cards": cards,
                "roadmap_fragment": roadmap_fragment,
                "summary_of_meeting": m_summary,
                "language": "ru",
            },
            recommended_channels=(
                ["notion"] + channel_hints
                if "notion" in channel_hints
                else ["telegram", "notion"]
            ),
            recommended_executor=None,
        )


__all__ = ["PMPipeline"]
