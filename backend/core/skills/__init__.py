"""Skills layer (W16).

Skill = `AgentAutomation` с `is_skill=True` + `instruction_template` и
`parameters_schema`. Запускается через те же ReAct-tools что и обычная
агент-автоматизация, но позволяет передать параметры один раз и
переиспользовать инструкцию.
"""
from backend.core.skills.parameters import (
    ParameterSchemaError,
    SkillParameter,
    parse_chat_args,
    substitute_parameters,
    validate_args,
)

__all__ = [
    "ParameterSchemaError",
    "SkillParameter",
    "parse_chat_args",
    "substitute_parameters",
    "validate_args",
]
