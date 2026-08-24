"""Sales pipeline — черновик письма следующего шага.

Для аккаунт-менеджеров и SDR/BDR. После встречи с клиентом готовит:
- subject + body черновика email'а
- Hubspot-like task spec (recipients_guess из contacts графа)

Рекомендуемые каналы: telegram (для подтверждения) + email_draft (для отправки).
Executor: None — для человеческой проверки, не для AI.
"""
from __future__ import annotations

from typing import Any, Optional

from backend.core.coffee.role_inference import Role, RoleAssignment
from backend.core.coffee.role_pipelines.base import RolePipeline, RolePipelineOutput


_SALES_SYSTEM_PROMPT = """\
Ты ассистент аккаунт-менеджера. После встречи с клиентом ты пишешь
ЧЕРНОВИК письма-follow-up. Формат строго:

SUBJECT: <одна строка>
BODY:
<тело письма, 80-150 слов, профессиональный тон,
обязательно: приветствие, краткое summary встречи (1-2 предложения),
явный CTA (следующий шаг с датой/действием), вежливая подпись>

Без markdown в body. Без эмодзи. Язык письма = язык встречи."""


class SalesPipeline(RolePipeline):
    role = Role.SALES

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
        tasks = meeting_data.get("tasks") or []

        decisions_str = "\n".join(
            f"- {d.get('text') if isinstance(d, dict) else d}"
            for d in decisions[:5]
        ) or "(нет извлечённых решений)"
        tasks_str = "\n".join(
            f"- {t.get('title') if isinstance(t, dict) else t}"
            for t in tasks[:5]
        ) or "(нет извлечённых задач)"

        prompt = (
            f"Сгенерируй черновик письма следующего шага по итогам встречи.\n\n"
            f"Встреча: {title!r}\n\n"
            f"Summary: {summary!r}\n\n"
            f"Принятые решения:\n{decisions_str}\n\n"
            f"Назначенные задачи:\n{tasks_str}\n"
        )

        text = await self._safe_llm_call(
            llm_router=llm_router,
            prompt=prompt,
            system_prompt=_SALES_SYSTEM_PROMPT,
            max_tokens=600,
            temperature=0.3,
            fallback_text=(
                f"SUBJECT: Следующие шаги по итогам '{title}'\n"
                "BODY:\nДобрый день! Спасибо за встречу. По итогам у нас "
                "есть несколько решений и задач, которые я закреплю отдельным "
                "сообщением. Предлагаю созвониться на следующей неделе для "
                "сверки. С уважением."
            ),
        )

        # Парсим SUBJECT/BODY
        subject = ""
        body = text
        for line in text.splitlines():
            ls = line.strip()
            if ls.upper().startswith("SUBJECT:"):
                subject = ls[8:].strip()
                continue
            if ls.upper() == "BODY:":
                body = text.split("BODY:", 1)[-1].strip()
                break
        if not subject:
            subject = f"Следующие шаги по итогам '{title}'"

        channel_hints = self._persona_channel_hints(persona)
        return RolePipelineOutput(
            artifact_type="email_draft",
            role=Role.SALES,
            participant_id=role_assignment.participant_id,
            participant_name=role_assignment.participant_name,
            title=subject,
            content_markdown=f"**Subject:** {subject}\n\n{body}",
            content_structured={
                "subject": subject,
                "body": body,
                "language": "ru",
            },
            recommended_channels=(
                # Если founder предпочитает email — сначала email_draft,
                # иначе шлём preview в telegram + email_draft артефакт
                ["email_draft"] + channel_hints
                if "email_long" in channel_hints or "email_short" in channel_hints
                else ["telegram", "email_draft"]
            ),
            recommended_executor=None,
        )


__all__ = ["SalesPipeline"]
