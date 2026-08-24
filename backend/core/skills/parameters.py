"""Skill parameters: schema, substitution, chat-args parser (W16).

Pure-Python helpers без LLM/HTTP/БД зависимостей. Тестируется юнитами.

Schema хранится в БД как JSONB-список:
    [
      {"name": "period", "type": "str", "required": true,
       "default": "Q4", "description": "Period (Q1/Q2/Q3/Q4 or YYYY-MM)"},
      {"name": "team", "type": "str", "required": false,
       "default": null, "description": "Team filter"}
    ]

Instruction template использует Python-style placeholders: `{period}`, `{team}`.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

_ALLOWED_TYPES = frozenset({"str", "int", "float", "bool"})

# Placeholder regex: {name} но НЕ {{escaped}}.
_PLACEHOLDER_RE = re.compile(r"(?<!\{)\{([a-zA-Z_][a-zA-Z0-9_]*)\}(?!\})")


class ParameterSchemaError(ValueError):
    """Raised при невалидной schema или невалидных args."""


@dataclass(frozen=True)
class SkillParameter:
    """Один параметр skill'а.

    Type — строка, не Python-type, чтобы JSON-сериализуемо.
    Coercion из строкового args (chat / form input) делает `_coerce`.
    """
    name: str
    type: str = "str"
    required: bool = True
    default: Any = None
    description: str = ""

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "SkillParameter":
        if not isinstance(d, Mapping):
            raise ParameterSchemaError(f"parameter must be a dict, got {type(d).__name__}")
        name = d.get("name")
        if not isinstance(name, str) or not name:
            raise ParameterSchemaError("parameter.name is required")
        if not name.replace("_", "").isalnum() or name[0].isdigit():
            raise ParameterSchemaError(f"parameter.name {name!r} must be a valid identifier")
        ptype = d.get("type", "str")
        if ptype not in _ALLOWED_TYPES:
            raise ParameterSchemaError(
                f"parameter.type {ptype!r} not in {sorted(_ALLOWED_TYPES)}"
            )
        return cls(
            name=name,
            type=ptype,
            required=bool(d.get("required", True)),
            default=d.get("default"),
            description=str(d.get("description", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "required": self.required,
            "default": self.default,
            "description": self.description,
        }


def parse_schema(raw: Any) -> list[SkillParameter]:
    """Распарсить parameters_schema из БД (list[dict]) в SkillParameter[]."""
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        import json
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ParameterSchemaError(f"parameters_schema not valid JSON: {exc}") from exc
    if not isinstance(raw, list):
        raise ParameterSchemaError("parameters_schema must be a list")
    seen: set[str] = set()
    out: list[SkillParameter] = []
    for item in raw:
        p = SkillParameter.from_dict(item)
        if p.name in seen:
            raise ParameterSchemaError(f"duplicate parameter name {p.name!r}")
        seen.add(p.name)
        out.append(p)
    return out


def _coerce(value: Any, ptype: str) -> Any:
    """Привести `value` к типу `ptype` (str/int/float/bool)."""
    if value is None:
        return None
    if ptype == "str":
        return str(value)
    if ptype == "int":
        if isinstance(value, bool):
            return int(value)
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ParameterSchemaError(f"cannot coerce {value!r} to int") from exc
    if ptype == "float":
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ParameterSchemaError(f"cannot coerce {value!r} to float") from exc
    if ptype == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            v = value.strip().lower()
            if v in {"1", "true", "yes", "y", "on"}:
                return True
            if v in {"0", "false", "no", "n", "off"}:
                return False
        raise ParameterSchemaError(f"cannot coerce {value!r} to bool")
    raise ParameterSchemaError(f"unsupported type {ptype!r}")


def validate_args(
    schema: list[SkillParameter],
    args: Mapping[str, Any],
) -> dict[str, Any]:
    """Проверить args против schema, вернуть нормализованный dict.

    - missing required → error
    - неизвестные keys → error (fail-closed: лучше упасть, чем тихо
      пропустить опечатку)
    - типы coerce'ятся согласно schema
    - missing optional получают default
    """
    schema_by_name = {p.name: p for p in schema}
    extra = set(args) - set(schema_by_name)
    if extra:
        raise ParameterSchemaError(
            f"unknown parameters: {sorted(extra)}; "
            f"expected: {sorted(schema_by_name)}"
        )
    out: dict[str, Any] = {}
    for p in schema:
        if p.name in args:
            out[p.name] = _coerce(args[p.name], p.type)
        elif p.required:
            raise ParameterSchemaError(f"missing required parameter {p.name!r}")
        else:
            out[p.name] = p.default
    return out


def substitute_parameters(template: str, args: Mapping[str, Any]) -> str:
    """Подставить args в `{name}`-placeholders в template.

    - `{{escaped}}` остаётся как `{escaped}` после раскрытия (стандартный
      Python-style escape).
    - Placeholder без значения → ParameterSchemaError (не оставляем
      `{period}` в финальной инструкции — модель будет сбита с толку).
    - Лишние args игнорируются на этом шаге (валидация делается в
      `validate_args` ДО substitute).
    """
    if not template:
        return ""

    used: set[str] = set()

    def _replace(m: re.Match) -> str:
        name = m.group(1)
        if name not in args:
            raise ParameterSchemaError(
                f"placeholder {{{name}}} has no value in args"
            )
        used.add(name)
        v = args[name]
        return "" if v is None else str(v)

    out = _PLACEHOLDER_RE.sub(_replace, template)
    # Раскрываем {{escaped}} → {escaped}.
    out = out.replace("{{", "{").replace("}}", "}")
    return out


def extract_placeholders(template: str) -> list[str]:
    """Найти все уникальные placeholder-имена в порядке появления."""
    if not template:
        return []
    seen: list[str] = []
    seen_set: set[str] = set()
    for m in _PLACEHOLDER_RE.finditer(template):
        name = m.group(1)
        if name not in seen_set:
            seen.append(name)
            seen_set.add(name)
    return seen


# === Chat-trigger parser =================================================

# Match `@skill <name> [arg=val ...]`  в начале сообщения.
_CHAT_TRIGGER_RE = re.compile(
    r"^@skill\s+(?P<name>[A-Za-z_][A-Za-z0-9_\-]*)\s*(?P<args>.*)$",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class ChatTrigger:
    skill_name: str
    args: dict[str, str]


def parse_chat_args(message: str) -> Optional[ChatTrigger]:
    """Распарсить chat-команду `@skill <name> arg1=val1 arg2="value 2"`.

    Возвращает None если сообщение не trigger.

    Args парсятся через shlex (так чтобы поддержать кавычки):
        @skill report period="Q4 2025" team=sales
        → ChatTrigger("report", {"period": "Q4 2025", "team": "sales"})
    """
    if not message:
        return None
    m = _CHAT_TRIGGER_RE.match(message.strip())
    if not m:
        return None
    skill_name = m.group("name")
    args_raw = m.group("args").strip()
    args: dict[str, str] = {}
    if args_raw:
        try:
            tokens = shlex.split(args_raw)
        except ValueError:
            # Невалидные кавычки — отдаём пустой args, skill упадёт на
            # validate_args если параметры required.
            tokens = []
        for tok in tokens:
            if "=" not in tok:
                continue
            k, _, v = tok.partition("=")
            k = k.strip()
            if not k:
                continue
            args[k] = v
    return ChatTrigger(skill_name=skill_name, args=args)


__all__ = [
    "ChatTrigger",
    "ParameterSchemaError",
    "SkillParameter",
    "extract_placeholders",
    "parse_chat_args",
    "parse_schema",
    "substitute_parameters",
    "validate_args",
]
