"""SkillBuilder: генерит skill из conversation messages через LLM (W16).

Идея: юзер прошёл multi-turn диалог где он попросил «собери отчёт о продажах
за Q4 для команды sales». Кликает «Сохранить как skill» — LLM получает
последние messages и решает:
1. как обобщить инструкцию (что было «делать», что было «параметром»)
2. какой parameters_schema получился
3. имя и описание skill'а

Builder возвращает `SkillProposal` — caller'у решать сохранять или нет.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from backend.core.skills.parameters import (
    ParameterSchemaError,
    SkillParameter,
    extract_placeholders,
    parse_schema,
)

logger = logging.getLogger(__name__)


_BUILDER_SYSTEM_PROMPT = """\
Ты — помощник который превращает фрагменты диалога в reusable skill (шаблон
повторяющейся задачи). На входе — последние сообщения пользователя и
ассистента. Найди что было КОНКРЕТНОЙ ПРОСЬБОЙ пользователя и какие
параметры юзер мог хотеть менять при следующем запуске.

Правила:
1. Имя skill'а — короткий snake_case (e.g. "weekly_sales_report").
2. instruction_template должен быть на русском, выглядеть как полная
   самостоятельная инструкция для агента и использовать `{name}`-плейсхолдеры
   для параметров. Пример:
   "Собери отчёт о продажах за {period}. Включи метрики команды {team}.
    Отправь результат в Telegram канал #sales."
3. parameters_schema: для каждого `{name}` ОБЯЗАТЕЛЬНО опиши параметр:
   {"name": "period", "type": "str", "required": true, "default": "Q4",
    "description": "Период для отчёта (Q1/Q2/Q3/Q4 или YYYY-MM)"}
4. Не добавляй параметры которых нет в шаблоне. Не добавляй placeholder'ы
   которых нет в parameters_schema.
5. Если параметризация не имеет смысла (одноразовая задача) — верни пустой
   parameters_schema=[] и instruction_template без placeholder'ов.
6. Минимум: name, description (1 строка), instruction_template, parameters_schema.

Формат ответа — ТОЛЬКО JSON:
{
  "name": "...",
  "description": "...",
  "instruction_template": "...",
  "parameters_schema": [...]
}
"""


@dataclass
class SkillProposal:
    """Предложение skill'а от LLM-builder'а.

    Caller (route handler) решает сохранять как is, дать юзеру отредактировать,
    или отклонить.
    """
    name: str
    description: str
    instruction_template: str
    parameters_schema: list[SkillParameter]
    raw_response: Optional[str] = None  # для debug/audit

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "instruction_template": self.instruction_template,
            "parameters_schema": [p.to_dict() for p in self.parameters_schema],
        }


def _name_from_description(text: str) -> str:
    """Fallback: вытянуть snake_case-имя из текста."""
    if not text:
        return "skill"
    cleaned = re.sub(r"[^\w\s\-]", "", text.lower())
    parts = [p for p in re.split(r"\s+", cleaned) if p][:4]
    name = "_".join(parts).strip("_-") or "skill"
    return name[:64]


def _validate_proposal(proposal: SkillProposal) -> None:
    """Sanity-check: placeholder'ы и parameters_schema согласованы."""
    placeholders = set(extract_placeholders(proposal.instruction_template))
    schema_names = {p.name for p in proposal.parameters_schema}
    if placeholders != schema_names:
        missing = placeholders - schema_names
        extra = schema_names - placeholders
        msg = []
        if missing:
            msg.append(f"placeholders without schema: {sorted(missing)}")
        if extra:
            msg.append(f"schema entries without placeholder: {sorted(extra)}")
        raise ParameterSchemaError("; ".join(msg))


class SkillBuilder:
    """LLM-driven построитель skill'а из conversation messages."""

    def __init__(self, llm_router: Any = None) -> None:
        self.llm_router = llm_router

    async def build(
        self,
        *,
        messages: list[Mapping[str, Any]],
        existing_skill_names: Optional[list[str]] = None,
    ) -> SkillProposal:
        """Сгенерировать SkillProposal.

        Args:
            messages: history `[{"role": "user"|"assistant", "content": str}]`.
            existing_skill_names: подсказка LLM "не повторяй существующие
                имена" — он учтёт.

        Raises:
            RuntimeError: если LLM router недоступен.
            ParameterSchemaError: если LLM выдал невалидную schema (не падаем
                на отсутствии mandatory полей — fallback на минимальный skill).
        """
        if self.llm_router is None:
            raise RuntimeError("SkillBuilder needs llm_router")
        if not messages:
            raise ValueError("messages are empty")

        # Берём последние 20 сообщений; обрезаем длинные content до 1500 chars.
        tail = messages[-20:]
        history_lines: list[str] = []
        for m in tail:
            role = (m.get("role") or "user")
            content = (m.get("content") or "")[:1500]
            if not content:
                continue
            history_lines.append(f"[{role}] {content}")
        history_text = "\n".join(history_lines)

        existing_hint = ""
        if existing_skill_names:
            existing_hint = (
                "\n\nСуществующие skill names в этом workspace (не повторяй):\n"
                + ", ".join(existing_skill_names[:50])
            )

        prompt = (
            "<conversation>\n" + history_text + "\n</conversation>" + existing_hint
            + "\n\nВерни JSON-объект согласно правилам system prompt'а."
        )

        try:
            raw = await self.llm_router.generate_json(
                prompt=prompt,
                system_prompt=_BUILDER_SYSTEM_PROMPT,
                temperature=0,
                max_tokens=2000,
            )
        except Exception as exc:
            logger.warning("SkillBuilder: LLM call failed: %s", exc)
            raise

        return self._parse_proposal(raw)

    @staticmethod
    def _parse_proposal(raw: Any) -> SkillProposal:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ParameterSchemaError(f"LLM returned invalid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ParameterSchemaError(f"LLM response must be a dict, got {type(raw).__name__}")

        name = str(raw.get("name") or "").strip()
        description = str(raw.get("description") or "").strip()
        template = str(raw.get("instruction_template") or "").strip()
        if not template:
            raise ParameterSchemaError("instruction_template is required and non-empty")

        if not name:
            name = _name_from_description(description or template)

        params = parse_schema(raw.get("parameters_schema") or [])

        proposal = SkillProposal(
            name=name,
            description=description,
            instruction_template=template,
            parameters_schema=params,
            raw_response=json.dumps(raw, ensure_ascii=False)[:2000],
        )
        _validate_proposal(proposal)
        return proposal


__all__ = ["SkillBuilder", "SkillProposal"]
