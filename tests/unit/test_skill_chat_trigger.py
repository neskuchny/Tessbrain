"""Unit-тесты для core.skills.chat_trigger (W16)."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from backend.core.skills.chat_trigger import maybe_handle_skill_trigger


@dataclass
class _FakeSkill:
    id: str
    user_id: str
    name: str
    is_skill: bool = True
    instruction: str = ""
    instruction_template: str = ""
    parameters_schema: list = None
    shared_in_tenant: bool = False


class _FakeAutomationService:
    def __init__(self, skills: list[_FakeSkill], execute_result: dict | None = None) -> None:
        self.skills = skills
        self.execute_result = execute_result or {"success": True, "final_answer": "done"}
        self.executed: list[_FakeSkill] = []

    async def list(self, user_id: str, status: Any = None) -> list[_FakeSkill]:
        return [s for s in self.skills if s.user_id == user_id]

    async def execute(self, skill: _FakeSkill) -> dict:
        self.executed.append(skill)
        return self.execute_result


def _run(coro):
    return asyncio.run(coro)


def test_returns_none_when_not_a_trigger() -> None:
    svc = _FakeAutomationService(skills=[])
    out = _run(maybe_handle_skill_trigger(
        "regular chat message", user_id="u-1", automation_service=svc,
    ))
    assert out is None


def test_returns_error_when_skill_not_found() -> None:
    svc = _FakeAutomationService(skills=[])
    out = _run(maybe_handle_skill_trigger(
        "@skill missing", user_id="u-1", automation_service=svc,
    ))
    assert out["success"] is False
    assert "not found" in out["error"]


def test_executes_skill_with_args() -> None:
    skill = _FakeSkill(
        id="s-1", user_id="u-1", name="report",
        instruction_template="Report for {period}",
        parameters_schema=[{"name": "period", "type": "str", "required": True}],
    )
    svc = _FakeAutomationService(skills=[skill])
    out = _run(maybe_handle_skill_trigger(
        "@skill report period=Q4", user_id="u-1", automation_service=svc,
    ))
    assert out["success"] is True
    assert out["skill_id"] == "s-1"
    assert out["rendered_instruction"] == "Report for Q4"
    assert len(svc.executed) == 1
    assert svc.executed[0].instruction == "Report for Q4"


def test_skill_name_match_case_insensitive() -> None:
    skill = _FakeSkill(
        id="s-1", user_id="u-1", name="MyReport",
        instruction_template="run",
        parameters_schema=[],
    )
    svc = _FakeAutomationService(skills=[skill])
    out = _run(maybe_handle_skill_trigger(
        "@skill myreport", user_id="u-1", automation_service=svc,
    ))
    assert out["success"] is True


def test_args_validation_error_returned_in_response() -> None:
    skill = _FakeSkill(
        id="s-1", user_id="u-1", name="report",
        instruction_template="Report for {period}",
        parameters_schema=[{"name": "period", "type": "str", "required": True}],
    )
    svc = _FakeAutomationService(skills=[skill])
    out = _run(maybe_handle_skill_trigger(
        "@skill report",  # не передан required period
        user_id="u-1", automation_service=svc,
    ))
    assert out["success"] is False
    assert "missing required" in out["error"]
    # execute не должен быть вызван
    assert svc.executed == []


def test_skip_non_skills() -> None:
    """Обычная AgentAutomation (не is_skill) не должна попасть в trigger."""
    skill = _FakeSkill(
        id="s-1", user_id="u-1", name="report", is_skill=False,
        instruction_template="run", parameters_schema=[],
    )
    svc = _FakeAutomationService(skills=[skill])
    out = _run(maybe_handle_skill_trigger(
        "@skill report", user_id="u-1", automation_service=svc,
    ))
    assert out["success"] is False
    assert "not found" in out["error"]


def test_execution_failure_returns_error() -> None:
    skill = _FakeSkill(
        id="s-1", user_id="u-1", name="report",
        instruction_template="run", parameters_schema=[],
    )

    class _Boom(_FakeAutomationService):
        async def execute(self, skill):
            raise RuntimeError("boom")

    svc = _Boom(skills=[skill])
    out = _run(maybe_handle_skill_trigger(
        "@skill report", user_id="u-1", automation_service=svc,
    ))
    assert out["success"] is False
    assert "execution failed" in out["error"]
