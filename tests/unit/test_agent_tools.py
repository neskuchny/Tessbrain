"""Unit-тесты P12f: агентные инструменты (skill_manage/delegate/exec).

Чистый stdlib — importlib. Store/delegate/runner инъектируем.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import sys
import types

_ROOT = pathlib.Path(__file__).resolve().parents[2]

for pkg in ("backend", "backend.core", "backend.core.hermes"):
    if pkg not in sys.modules:
        m = types.ModuleType(pkg)
        m.__path__ = []  # type: ignore[attr-defined]
        sys.modules[pkg] = m


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_store_mod = _load("backend.core.hermes.skill_store",
                   "backend/core/hermes/skill_store.py")
_at = _load("backend.core.hermes.agent_tools",
            "backend/core/hermes/agent_tools.py")
_ap = _load("backend.core.hermes.approval",
            "backend/core/hermes/approval.py")

SkillStore = _store_mod.SkillStore
agent_tool_defs = _at.agent_tool_defs
is_agent_tool = _at.is_agent_tool
execute_agent_tool = _at.execute_agent_tool
classify_tool = _ap.classify_tool
RiskLevel = _ap.RiskLevel


def _run(c):
    return asyncio.run(c)


# === defs / classification gating ======================================

def test_defs_and_is_agent_tool() -> None:
    names = {d["name"] for d in agent_tool_defs()}
    assert names == {"skill_manage", "delegate_to_agent", "execute_code"}
    assert is_agent_tool("skill_manage")
    assert not is_agent_tool("graph_search")


def test_approval_classifies_agent_tools() -> None:
    # P12f: должны гейтиться (skill_manage/delegate=WRITE,
    # execute_code=DANGEROUS)
    assert classify_tool("skill_manage", {})[0] == RiskLevel.WRITE
    assert classify_tool("delegate_to_agent", {})[0] == RiskLevel.WRITE
    assert classify_tool("execute_code", {})[0] == RiskLevel.DANGEROUS


# === skill_manage (реальный, через SkillStore) =========================

def test_skill_manage_create_patch_delete(tmp_path) -> None:
    s = SkillStore(str(tmp_path))
    r = json.loads(_run(execute_agent_tool(
        "skill_manage",
        {"action": "create", "name": "deploy", "procedure": "1. test",
         "description": "d"},
        store=s, user_id="u")))
    assert r["ok"] is True
    assert s.view("u", "deploy") is not None

    r2 = json.loads(_run(execute_agent_tool(
        "skill_manage",
        {"action": "patch", "name": "deploy", "old": "1. test",
         "new": "1. run tests"}, store=s, user_id="u")))
    assert r2["ok"] is True
    assert "run tests" in s.view("u", "deploy").procedure

    r3 = json.loads(_run(execute_agent_tool(
        "skill_manage", {"action": "delete", "name": "deploy"},
        store=s, user_id="u")))
    assert r3["ok"] is True and s.view("u", "deploy") is None


def test_skill_manage_bad_input_safe(tmp_path) -> None:
    s = SkillStore(str(tmp_path))
    assert "error" in json.loads(_run(execute_agent_tool(
        "skill_manage", {"action": "create"}, store=s, user_id="u")))
    assert "error" in json.loads(_run(execute_agent_tool(
        "skill_manage", {"action": "boom", "name": "x"}, store=s,
        user_id="u")))


def test_skill_manage_without_store() -> None:
    out = json.loads(_run(execute_agent_tool(
        "skill_manage", {"action": "create", "name": "x"}, store=None,
        user_id="u")))
    assert "error" in out


# === delegate (injectable, bounded) ====================================

def test_delegate_not_configured_is_safe() -> None:
    out = json.loads(_run(execute_agent_tool(
        "delegate_to_agent", {"target": "brain", "query": "hi"})))
    assert out["error"] == "delegation_not_configured"


def test_delegate_calls_injected_fn() -> None:
    async def deleg(target, query):
        return f"{target} says: {query}"

    out = _run(execute_agent_tool(
        "delegate_to_agent", {"target": "brain", "query": "summarize"},
        delegate_fn=deleg))
    assert out == "brain says: summarize"


def test_delegate_invalid_target_and_empty_query() -> None:
    async def deleg(t, q):
        return "x"

    assert "error" in json.loads(_run(execute_agent_tool(
        "delegate_to_agent", {"target": "evil", "query": "q"},
        delegate_fn=deleg)))
    assert "error" in json.loads(_run(execute_agent_tool(
        "delegate_to_agent", {"target": "brain", "query": ""},
        delegate_fn=deleg)))


def test_delegate_depth_limit() -> None:
    async def deleg(t, q):
        return "ok"

    out = json.loads(_run(execute_agent_tool(
        "delegate_to_agent", {"target": "brain", "query": "q"},
        delegate_fn=deleg, depth=1)))
    assert "depth limit" in out["error"]


def test_delegate_fn_failure_safe() -> None:
    async def boom(t, q):
        raise RuntimeError("agent down")

    out = json.loads(_run(execute_agent_tool(
        "delegate_to_agent", {"target": "brain", "query": "q"},
        delegate_fn=boom)))
    assert "error" in out  # never raises


# === execute_code (slot, default disabled) =============================

def test_execute_code_disabled_by_default() -> None:
    out = json.loads(_run(execute_agent_tool(
        "execute_code", {"code": "print(1)"})))
    assert out["error"] == "code_execution_not_configured"


def test_execute_code_with_runner() -> None:
    async def runner(code, lang):
        return f"ran {lang}: {code}"

    out = _run(execute_agent_tool(
        "execute_code", {"code": "print(1)", "lang": "python"},
        code_runner=runner))
    assert out == "ran python: print(1)"


def test_execute_code_empty_and_runner_failure_safe() -> None:
    async def boom(c, l):
        raise RuntimeError("sandbox blew up")

    assert "error" in json.loads(_run(execute_agent_tool(
        "execute_code", {"code": "  "})))
    assert "error" in json.loads(_run(execute_agent_tool(
        "execute_code", {"code": "x"}, code_runner=boom)))


def test_unknown_agent_tool_safe() -> None:
    assert "error" in json.loads(_run(execute_agent_tool("nope", {})))
