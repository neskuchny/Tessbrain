"""Unit-тесты P12c: skill-runtime (инструменты петли + автообучение).

Чистый модуль (stdlib+yaml) — грузим через importlib. LLM и store
инъектируем. Без графа/БД/сети.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import sys
import types

_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load(modname, rel):
    spec = importlib.util.spec_from_file_location(modname, _ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


for pkg in ("backend", "backend.core", "backend.core.hermes"):
    if pkg not in sys.modules:
        m = types.ModuleType(pkg)
        m.__path__ = []  # type: ignore[attr-defined]
        sys.modules[pkg] = m
_store_mod = _load("backend.core.hermes.skill_store",
                   "backend/core/hermes/skill_store.py")
_rt = _load("backend.core.hermes.skill_runtime",
            "backend/core/hermes/skill_runtime.py")

SkillDoc = _store_mod.SkillDoc
SkillStore = _store_mod.SkillStore
skill_tool_defs = _rt.skill_tool_defs
is_skill_tool = _rt.is_skill_tool
build_skill_nudge = _rt.build_skill_nudge
execute_skill_tool = _rt.execute_skill_tool
should_propose_skill = _rt.should_propose_skill
draft = _rt.draft_skill_from_trajectory_sync
maybe_autolearn = _rt.maybe_autolearn


def _run(c):
    return asyncio.run(c)


class _Step:
    def __init__(self, tool, args=None, thought=""):
        self.tool, self.args, self.thought = tool, args or {}, thought


def _traj(n, tool="search_meetings"):
    return [_Step(tool, {"q": i}) for i in range(n)] + [_Step("done")]


# === tool defs / dispatch ==============================================

# === P12i-C: системный nudge ==========================================

def test_nudge_empty_when_skills_off() -> None:
    assert build_skill_nudge(skills_enabled=False) == ""
    assert build_skill_nudge(skills_enabled=False,
                             agent_tools_enabled=True) == ""


def test_nudge_minimal_skills_only() -> None:
    n = build_skill_nudge(skills_enabled=True)
    assert "[Память навыков]" in n
    assert "skills_list" in n and "skill_view" in n
    # без agent_tools → НЕ зовём skill_manage, говорим про авто-сохранение
    assert "skill_manage" not in n
    assert "автоматически" in n
    assert "Уроки из прошлого" not in n   # lessons off


def test_nudge_with_agent_tools_mentions_skill_manage() -> None:
    n = build_skill_nudge(skills_enabled=True, agent_tools_enabled=True)
    assert "skill_manage(action=create" in n
    assert "Pitfalls/Verification" in n


def test_nudge_with_lessons_line() -> None:
    n = build_skill_nudge(skills_enabled=True, lessons_enabled=True)
    assert "Уроки из прошлого опыта" in n and "⚠️" in n


def test_nudge_never_raises_on_odd_input() -> None:
    # булевы флаги — но на всякий проверим устойчивость к truthy
    assert isinstance(build_skill_nudge(skills_enabled=1), str)  # type: ignore[arg-type]


def test_tool_defs_shape() -> None:
    defs = skill_tool_defs()
    names = {d["name"] for d in defs}
    assert names == {"skills_list", "skill_view"}
    assert is_skill_tool("skills_list") and not is_skill_tool("search_x")


def test_skills_list_tool(tmp_path) -> None:
    s = SkillStore(str(tmp_path))
    s.create("u", SkillDoc(name="a", description="d", category="ops",
                           procedure="p"))
    out = json.loads(execute_skill_tool("skills_list", {}, store=s,
                                        user_id="u"))
    assert out["count"] == 1 and out["skills"][0]["name"] == "a"
    # metadata only — без procedure
    assert "procedure" not in out["skills"][0]


def test_skill_view_tool_and_missing(tmp_path) -> None:
    s = SkillStore(str(tmp_path))
    s.create("u", SkillDoc(name="deploy", description="d",
                           procedure="step1", verification="curl"))
    ok = json.loads(execute_skill_tool("skill_view", {"name": "deploy"},
                                       store=s, user_id="u"))
    assert ok["procedure"] == "step1" and ok["verification"] == "curl"
    miss = json.loads(execute_skill_tool("skill_view", {"name": "ghost"},
                                         store=s, user_id="u"))
    assert "error" in miss


def test_unknown_skill_tool_safe(tmp_path) -> None:
    s = SkillStore(str(tmp_path))
    out = json.loads(execute_skill_tool("nope", {}, store=s, user_id="u"))
    assert "error" in out


# === should_propose_skill ==============================================

def test_propose_requires_success() -> None:
    ok, _ = should_propose_skill(_traj(6), success=False)
    assert ok is False


def test_propose_on_complex_task() -> None:
    ok, why = should_propose_skill(_traj(5), success=True)
    assert ok is True and "5" in why


def test_propose_on_multi_tool_workflow() -> None:
    steps = [_Step("a"), _Step("b"), _Step("a"), _Step("done")]
    ok, why = should_propose_skill(steps, success=True)
    assert ok is True and "tools" in why


def test_no_propose_on_trivial() -> None:
    ok, _ = should_propose_skill([_Step("a"), _Step("done")], success=True)
    assert ok is False


def test_propose_garbage_safe() -> None:
    assert should_propose_skill("nope", success=True)[0] is False
    assert should_propose_skill(None, success=True)[0] is False


# === P12i-A: триггеры 1:1 с Hermes (error-recovery, user-correction) ===

class _StepObs:
    def __init__(self, tool, observation="", thought=""):
        self.tool, self.args = tool, {}
        self.observation, self.thought = observation, thought


def test_propose_on_error_recovery() -> None:
    # короткая траектория, НО был шаг с ошибкой → потом успех
    steps = [
        _StepObs("graph_search", observation='{"error":"not found"}'),
        _StepObs("hybrid_search", observation="ok, 3 results"),
        _StepObs("done"),
    ]
    ok, why = should_propose_skill(steps, success=True)
    assert ok is True and "error-recovery" in why


def test_no_error_recovery_when_clean() -> None:
    steps = [_StepObs("a", observation="all good"), _StepObs("done")]
    assert should_propose_skill(steps, success=True)[0] is False


def test_propose_on_user_correction() -> None:
    steps = [_Step("a"), _Step("done")]              # тривиально по шагам
    ok, why = should_propose_skill(
        steps, success=True,
        user_message="нет, не так — исправь, ищи по графу")
    assert ok is True and "user-correction" in why
    # английский вариант
    ok2, _ = should_propose_skill(
        steps, success=True, user_message="no, do it instead via graph")
    assert ok2 is True


def test_no_correction_on_plain_message() -> None:
    ok, _ = should_propose_skill(
        [_Step("a"), _Step("done")], success=True,
        user_message="найди последние встречи")
    assert ok is False


def test_correction_requires_success_and_steps() -> None:
    # не успех → нет
    assert should_propose_skill(
        [_Step("a")], success=False, user_message="нет, исправь")[0] is False
    # успех, но пустая траектория → нет (norm пуст)
    assert should_propose_skill(
        [_Step("done")], success=True, user_message="нет, исправь")[0] is False


def test_backward_compatible_signature() -> None:
    # старый вызов без user_message всё ещё работает
    assert should_propose_skill(
        [_Step(f"t{i}") for i in range(6)], success=True)[0] is True


def test_error_recovery_scans_dict_steps_safe() -> None:
    steps = [{"tool": "x", "observation": "Traceback (most recent call)"},
             {"tool": "done"}]
    ok, why = should_propose_skill(steps, success=True)
    assert ok is True and "error-recovery" in why


# === draft parsing =====================================================

def test_draft_from_clean_json() -> None:
    d = draft(json.dumps({"name": "x-skill", "procedure": "1. do",
                          "description": "d", "tags": ["t"]}))
    assert d is not None and d.name == "x-skill" and d.tags == ["t"]


def test_draft_from_fenced_json() -> None:
    d = draft('```json\n{"name":"y","procedure":"steps"}\n```')
    assert d is not None and d.name == "y"


def test_draft_rejects_incomplete_or_garbage() -> None:
    assert draft('{"name":"only-name"}') is None      # no procedure
    assert draft('{"procedure":"x"}') is None          # no name
    assert draft("not json") is None
    assert draft(42) is None


# === maybe_autolearn (async, injectable LLM) ===========================

def test_autolearn_disabled_noop(tmp_path) -> None:
    s = SkillStore(str(tmp_path))

    async def llm(_p):
        raise AssertionError("LLM must not be called when disabled")

    r = _run(maybe_autolearn("task", _traj(6), True, store=s,
                             user_id="u", llm_json_call=llm, enabled=False))
    assert r is None and s.list("u") == []


def test_autolearn_creates_skill(tmp_path) -> None:
    s = SkillStore(str(tmp_path))

    async def llm(_p):
        return {"name": "found-meetings", "description": "find",
                "procedure": "1. search 2. filter", "verification": "count>0"}

    r = _run(maybe_autolearn("найди встречи", _traj(6), True, store=s,
                             user_id="u", llm_json_call=llm, enabled=True))
    assert r == "found-meetings"
    sk = s.view("u", "found-meetings")
    assert sk is not None and "search" in sk.procedure


def test_autolearn_skips_unworthy_trajectory(tmp_path) -> None:
    s = SkillStore(str(tmp_path))
    called = []

    async def llm(_p):
        called.append(1)
        return {}

    r = _run(maybe_autolearn("t", [_Step("a"), _Step("done")], True,
                             store=s, user_id="u", llm_json_call=llm,
                             enabled=True))
    assert r is None and called == []  # heuristic gate before LLM


def test_autolearn_llm_failure_is_safe(tmp_path) -> None:
    s = SkillStore(str(tmp_path))

    async def boom(_p):
        raise RuntimeError("LLM down")

    r = _run(maybe_autolearn("t", _traj(6), True, store=s, user_id="u",
                             llm_json_call=boom, enabled=True))
    assert r is None  # never raises, no skill

def test_autolearn_bad_draft_no_skill(tmp_path) -> None:
    s = SkillStore(str(tmp_path))

    async def llm(_p):
        return "garbage not json"

    r = _run(maybe_autolearn("t", _traj(6), True, store=s, user_id="u",
                             llm_json_call=llm, enabled=True))
    assert r is None and s.list("u") == []
