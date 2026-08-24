"""Unit-тесты для core.skills.parameters (W16)."""
from __future__ import annotations

import pytest
from backend.core.skills.parameters import (
    ChatTrigger,
    ParameterSchemaError,
    SkillParameter,
    extract_placeholders,
    parse_chat_args,
    parse_schema,
    substitute_parameters,
    validate_args,
)

# === SkillParameter.from_dict ===========================================

def test_from_dict_minimal() -> None:
    p = SkillParameter.from_dict({"name": "period"})
    assert p.name == "period"
    assert p.type == "str"
    assert p.required is True


def test_from_dict_full() -> None:
    p = SkillParameter.from_dict({
        "name": "amount", "type": "int", "required": False,
        "default": 0, "description": "Amount in USD",
    })
    assert p.type == "int"
    assert p.required is False
    assert p.default == 0


def test_from_dict_missing_name() -> None:
    with pytest.raises(ParameterSchemaError):
        SkillParameter.from_dict({})


def test_from_dict_invalid_name_starts_digit() -> None:
    with pytest.raises(ParameterSchemaError):
        SkillParameter.from_dict({"name": "1period"})


def test_from_dict_invalid_name_special_chars() -> None:
    with pytest.raises(ParameterSchemaError):
        SkillParameter.from_dict({"name": "my-param"})


def test_from_dict_invalid_type() -> None:
    with pytest.raises(ParameterSchemaError):
        SkillParameter.from_dict({"name": "x", "type": "object"})


def test_from_dict_not_a_dict() -> None:
    with pytest.raises(ParameterSchemaError):
        SkillParameter.from_dict("not a dict")  # type: ignore[arg-type]


# === parse_schema =======================================================

def test_parse_schema_empty() -> None:
    assert parse_schema([]) == []
    assert parse_schema(None) == []
    assert parse_schema("") == []


def test_parse_schema_from_json_string() -> None:
    out = parse_schema('[{"name": "x"}]')
    assert len(out) == 1
    assert out[0].name == "x"


def test_parse_schema_invalid_json() -> None:
    with pytest.raises(ParameterSchemaError):
        parse_schema("not valid json")


def test_parse_schema_not_a_list() -> None:
    with pytest.raises(ParameterSchemaError):
        parse_schema({"name": "x"})


def test_parse_schema_duplicates() -> None:
    with pytest.raises(ParameterSchemaError):
        parse_schema([{"name": "x"}, {"name": "x"}])


# === validate_args ======================================================

def test_validate_args_basic() -> None:
    schema = [
        SkillParameter(name="period", type="str", required=True),
        SkillParameter(name="count", type="int", required=False, default=10),
    ]
    out = validate_args(schema, {"period": "Q4"})
    assert out == {"period": "Q4", "count": 10}


def test_validate_args_coerces_types() -> None:
    schema = [
        SkillParameter(name="n", type="int"),
        SkillParameter(name="enabled", type="bool"),
    ]
    out = validate_args(schema, {"n": "42", "enabled": "yes"})
    assert out == {"n": 42, "enabled": True}


def test_validate_args_missing_required() -> None:
    schema = [SkillParameter(name="period", required=True)]
    with pytest.raises(ParameterSchemaError) as exc:
        validate_args(schema, {})
    assert "missing required" in str(exc.value)


def test_validate_args_unknown_keys_rejected() -> None:
    """Fail-closed на опечатки — не игнорируем."""
    schema = [SkillParameter(name="period")]
    with pytest.raises(ParameterSchemaError) as exc:
        validate_args(schema, {"period": "Q4", "perid": "Q3"})
    assert "unknown" in str(exc.value).lower()


def test_validate_args_int_coercion_failure() -> None:
    schema = [SkillParameter(name="n", type="int")]
    with pytest.raises(ParameterSchemaError):
        validate_args(schema, {"n": "not-a-number"})


def test_validate_args_bool_variants() -> None:
    schema = [SkillParameter(name="b", type="bool")]
    for truthy in ("true", "1", "yes", "on", "TRUE", True, 1):
        assert validate_args(schema, {"b": truthy})["b"] is True
    for falsy in ("false", "0", "no", "off", False, 0):
        assert validate_args(schema, {"b": falsy})["b"] is False


def test_validate_args_default_used_when_missing() -> None:
    schema = [SkillParameter(name="x", required=False, default="dflt")]
    assert validate_args(schema, {})["x"] == "dflt"


# === substitute_parameters ==============================================

def test_substitute_basic() -> None:
    out = substitute_parameters("Hello {name}", {"name": "Alice"})
    assert out == "Hello Alice"


def test_substitute_multiple() -> None:
    out = substitute_parameters("{a} + {b} = {c}", {"a": 1, "b": 2, "c": 3})
    assert out == "1 + 2 = 3"


def test_substitute_missing_value_raises() -> None:
    with pytest.raises(ParameterSchemaError):
        substitute_parameters("{period}", {})


def test_substitute_escaped_braces_preserved() -> None:
    """{{escaped}} → {escaped} (стандартный python-style)."""
    out = substitute_parameters("use {{literal}} not {param}", {"param": "X"})
    assert out == "use {literal} not X"


def test_substitute_none_value_renders_empty() -> None:
    out = substitute_parameters("a={x}b", {"x": None})
    assert out == "a=b"


def test_substitute_empty_template() -> None:
    assert substitute_parameters("", {}) == ""


def test_substitute_extra_args_ignored() -> None:
    """Лишние args не вызывают ошибку (валидацию делает validate_args)."""
    out = substitute_parameters("Hi {name}", {"name": "x", "extra": "y"})
    assert out == "Hi x"


# === extract_placeholders ==============================================

def test_extract_placeholders_basic() -> None:
    assert extract_placeholders("a {x} b {y} c {x}") == ["x", "y"]


def test_extract_placeholders_skips_escaped() -> None:
    """{{escaped}} не должен считаться placeholder'ом."""
    assert extract_placeholders("{{not}} {real}") == ["real"]


def test_extract_placeholders_empty() -> None:
    assert extract_placeholders("") == []
    assert extract_placeholders("no placeholders here") == []


# === parse_chat_args (@skill trigger) ===================================

def test_parse_chat_args_minimal() -> None:
    out = parse_chat_args("@skill weekly_report")
    assert isinstance(out, ChatTrigger)
    assert out.skill_name == "weekly_report"
    assert out.args == {}


def test_parse_chat_args_with_args() -> None:
    out = parse_chat_args("@skill report period=Q4 team=sales")
    assert out is not None
    assert out.skill_name == "report"
    assert out.args == {"period": "Q4", "team": "sales"}


def test_parse_chat_args_quoted_value() -> None:
    out = parse_chat_args('@skill report period="Q4 2025" team=sales')
    assert out is not None
    assert out.args["period"] == "Q4 2025"


def test_parse_chat_args_case_insensitive_trigger() -> None:
    assert parse_chat_args("@SKILL report").skill_name == "report"


def test_parse_chat_args_not_a_trigger() -> None:
    assert parse_chat_args("just a normal message") is None
    assert parse_chat_args("hello @skill report") is None  # должен быть в начале
    assert parse_chat_args("") is None
    assert parse_chat_args(None) is None  # type: ignore[arg-type]


def test_parse_chat_args_empty_args_ignored() -> None:
    """`=value` без ключа — пропускаем, не падаем."""
    out = parse_chat_args("@skill x =bad foo=ok")
    assert out is not None
    assert out.args == {"foo": "ok"}


def test_parse_chat_args_args_without_equals_ignored() -> None:
    out = parse_chat_args("@skill x foo bar=baz")
    assert out is not None
    assert out.args == {"bar": "baz"}
