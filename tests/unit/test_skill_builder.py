"""Unit-тесты для core.skills.builder (W16).

Проверяем pure logic: parse_proposal, валидация placeholders vs schema.
LLM-call мокается.
"""
from __future__ import annotations

import asyncio

import pytest
from backend.core.skills.builder import (
    SkillBuilder,
    SkillProposal,
    _name_from_description,
)
from backend.core.skills.parameters import ParameterSchemaError

# === _name_from_description ============================================

def test_name_from_description() -> None:
    assert _name_from_description("Weekly sales report") == "weekly_sales_report"


def test_name_strips_punctuation() -> None:
    """Знаки препинания убираются, до 4 слов попадают в имя."""
    out = _name_from_description("Send report (urgent!): now")
    assert out == "send_report_urgent_now"
    assert "!" not in out and "(" not in out and ":" not in out


def test_name_fallback_when_empty() -> None:
    assert _name_from_description("") == "skill"


def test_name_truncated() -> None:
    long = " ".join(["word"] * 30)
    out = _name_from_description(long)
    assert len(out) <= 64


# === _parse_proposal validation ========================================

def test_parse_proposal_minimal() -> None:
    proposal = SkillBuilder._parse_proposal({
        "name": "test_skill",
        "description": "test",
        "instruction_template": "do something",
        "parameters_schema": [],
    })
    assert proposal.name == "test_skill"
    assert proposal.parameters_schema == []


def test_parse_proposal_with_params() -> None:
    proposal = SkillBuilder._parse_proposal({
        "name": "report",
        "description": "Generate report",
        "instruction_template": "Make report for {period}",
        "parameters_schema": [
            {"name": "period", "type": "str", "required": True},
        ],
    })
    assert len(proposal.parameters_schema) == 1
    assert proposal.parameters_schema[0].name == "period"


def test_parse_proposal_placeholder_without_schema_fails() -> None:
    """LLM забыл описать `{period}` в schema — fail."""
    with pytest.raises(ParameterSchemaError) as exc:
        SkillBuilder._parse_proposal({
            "name": "report",
            "description": "",
            "instruction_template": "Make report for {period}",
            "parameters_schema": [],
        })
    assert "placeholders without schema" in str(exc.value)


def test_parse_proposal_schema_without_placeholder_fails() -> None:
    """LLM описал `period` но не использовал в template — fail."""
    with pytest.raises(ParameterSchemaError) as exc:
        SkillBuilder._parse_proposal({
            "name": "report",
            "description": "",
            "instruction_template": "no placeholders here",
            "parameters_schema": [{"name": "period"}],
        })
    assert "schema entries without placeholder" in str(exc.value)


def test_parse_proposal_empty_template_fails() -> None:
    with pytest.raises(ParameterSchemaError):
        SkillBuilder._parse_proposal({
            "name": "x",
            "description": "y",
            "instruction_template": "",
            "parameters_schema": [],
        })


def test_parse_proposal_name_falls_back_to_description() -> None:
    proposal = SkillBuilder._parse_proposal({
        "name": "",
        "description": "Send daily report",
        "instruction_template": "send daily summary",
        "parameters_schema": [],
    })
    assert proposal.name == "send_daily_report"


def test_parse_proposal_invalid_top_level() -> None:
    with pytest.raises(ParameterSchemaError):
        SkillBuilder._parse_proposal("not a dict")


def test_parse_proposal_from_string_json() -> None:
    proposal = SkillBuilder._parse_proposal(
        '{"name":"x","description":"d","instruction_template":"do","parameters_schema":[]}'
    )
    assert proposal.name == "x"


def test_parse_proposal_from_invalid_json_string() -> None:
    with pytest.raises(ParameterSchemaError):
        SkillBuilder._parse_proposal("{not valid json")


def test_skill_proposal_to_dict() -> None:
    proposal = SkillProposal(
        name="x", description="y", instruction_template="z",
        parameters_schema=[],
    )
    d = proposal.to_dict()
    assert d == {
        "name": "x", "description": "y",
        "instruction_template": "z", "parameters_schema": [],
    }


# === build() with mocked LLM router ====================================

class _FakeRouter:
    def __init__(self, response: dict) -> None:
        self._response = response
        self.last_kwargs = {}

    async def generate_json(self, **kwargs):
        self.last_kwargs = kwargs
        return self._response


def test_build_calls_llm_with_temperature_zero() -> None:
    router = _FakeRouter({
        "name": "test", "description": "",
        "instruction_template": "do thing", "parameters_schema": [],
    })
    builder = SkillBuilder(llm_router=router)
    asyncio.run(builder.build(messages=[{"role": "user", "content": "hi"}]))
    assert router.last_kwargs.get("temperature") == 0


def test_build_includes_existing_names_in_prompt() -> None:
    router = _FakeRouter({
        "name": "test", "description": "",
        "instruction_template": "do", "parameters_schema": [],
    })
    builder = SkillBuilder(llm_router=router)
    asyncio.run(builder.build(
        messages=[{"role": "user", "content": "hi"}],
        existing_skill_names=["existing_skill_a", "existing_skill_b"],
    ))
    prompt = router.last_kwargs.get("prompt", "")
    assert "existing_skill_a" in prompt
    assert "existing_skill_b" in prompt


def test_build_raises_on_empty_messages() -> None:
    builder = SkillBuilder(llm_router=_FakeRouter({}))
    with pytest.raises(ValueError):
        asyncio.run(builder.build(messages=[]))


def test_build_raises_without_router() -> None:
    builder = SkillBuilder(llm_router=None)
    with pytest.raises(RuntimeError):
        asyncio.run(builder.build(messages=[{"role": "user", "content": "x"}]))
