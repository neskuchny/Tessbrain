"""Chat trigger handler для skills (W16).

Распознаёт `@skill <name> arg1=val1` в чат-сообщении, находит skill
по имени для текущего юзера, валидирует args, подставляет в шаблон,
запускает через AgentAutomationService.

Использование (в chat-pipeline):

    from backend.core.skills.chat_trigger import maybe_handle_skill_trigger

    result = await maybe_handle_skill_trigger(message, user_id=user_id)
    if result is not None:
        # сообщение было @skill — вернуть результат вместо обычного chat-flow
        return result

В реальном chat-handler решение «использовать ли skill-trigger» принимает
caller. Эта функция — pure delegating: либо находит skill и запускает,
либо возвращает None.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from backend.core.skills.parameters import (
    ParameterSchemaError,
    parse_chat_args,
    parse_schema,
    substitute_parameters,
    validate_args,
)

logger = logging.getLogger(__name__)


async def maybe_handle_skill_trigger(
    message: str,
    *,
    user_id: str,
    automation_service: Any = None,
) -> Optional[dict[str, Any]]:
    """Если message — это `@skill ...` команда, выполнить и вернуть результат.

    Returns:
        - dict с {success, skill_id, final_answer, ...} при успехе/ошибке валидации
        - None если message не trigger (caller должен обработать как обычный chat)

    НЕ raises — все ошибки оборачиваются в response с `success=false`.
    """
    trigger = parse_chat_args(message)
    if trigger is None:
        return None

    if automation_service is None:
        from backend.core.automations.agent_automation_service import (
            get_agent_automation_service,
        )
        automation_service = get_agent_automation_service()

    # Находим skill по имени (case-insensitive). Юзер видит свои skills
    # + shared_in_tenant.
    automations = await automation_service.list(user_id=user_id, status=None)
    skill = None
    name_lc = trigger.skill_name.lower()
    for a in automations:
        if not getattr(a, "is_skill", False):
            continue
        if a.name.lower() == name_lc:
            skill = a
            break

    if skill is None:
        return {
            "success": False,
            "trigger": "skill",
            "error": f"skill {trigger.skill_name!r} not found",
            "hint": "Use `GET /api/v1/skills` to list available skills.",
        }

    try:
        params = parse_schema(skill.parameters_schema or [])
        normalized = validate_args(params, trigger.args)
        template = skill.instruction_template or skill.instruction or ""
        rendered = substitute_parameters(template, normalized)
    except ParameterSchemaError as exc:
        return {
            "success": False,
            "trigger": "skill",
            "skill_name": skill.name,
            "error": f"args validation failed: {exc}",
        }

    skill.instruction = rendered
    try:
        result = await automation_service.execute(skill)
    except Exception as exc:
        logger.exception("Skill execution failed via chat trigger")
        return {
            "success": False,
            "trigger": "skill",
            "skill_name": skill.name,
            "error": f"execution failed: {exc}",
        }

    return {
        "success": bool(result.get("success")),
        "trigger": "skill",
        "skill_id": skill.id,
        "skill_name": skill.name,
        "args": normalized,
        "rendered_instruction": rendered,
        "final_answer": result.get("final_answer") or "",
        "trace": result.get("trace") or [],
    }


__all__ = ["maybe_handle_skill_trigger"]
