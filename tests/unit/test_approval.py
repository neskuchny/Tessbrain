"""Unit-тесты P12e: approval-gate (политика безопасного tool-call).

Чистый stdlib-модуль — importlib. Без сети/графа.
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
_spec = importlib.util.spec_from_file_location(
    "backend.core.hermes.approval",
    _ROOT / "backend/core/hermes/approval.py",
)
_ap = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _ap
_spec.loader.exec_module(_ap)

RiskLevel = _ap.RiskLevel
ApprovalPolicy = _ap.ApprovalPolicy
classify_tool = _ap.classify_tool
decide = _ap.decide
guard_executor = _ap.guard_executor
policy_from_env = _ap.policy_from_env


def _run(c):
    return asyncio.run(c)


# === classify ==========================================================

def test_classify_safe_write_dangerous() -> None:
    assert classify_tool("skills_list", {})[0] == RiskLevel.SAFE
    assert classify_tool("graph_search", {"q": 1})[0] == RiskLevel.SAFE
    assert classify_tool("create_jira_issue", {})[0] == RiskLevel.WRITE
    assert classify_tool("send_email", {})[0] == RiskLevel.WRITE
    assert classify_tool("delete_node", {})[0] == RiskLevel.DANGEROUS
    assert classify_tool("execute_code", {})[0] == RiskLevel.DANGEROUS


def test_classify_arg_signal() -> None:
    # имя нейтральное, но аргумент command= → dangerous
    lvl, _ = classify_tool("tool", {"command": "rm -rf"})
    assert lvl == RiskLevel.DANGEROUS


def test_classify_never_raises() -> None:
    assert classify_tool(None, None)[0] in (
        RiskLevel.SAFE, RiskLevel.WRITE, RiskLevel.DANGEROUS)


# === decide ============================================================

def test_off_allows_everything() -> None:
    p = ApprovalPolicy(mode="off")
    assert decide("delete_all", {}, policy=p).allowed is True


def test_enforce_blocks_dangerous_allows_safe() -> None:
    p = ApprovalPolicy(mode="enforce")
    assert decide("graph_search", {}, policy=p).allowed is True
    d = decide("execute_code", {}, policy=p)
    assert d.allowed is False
    assert "approval_required" in d.observation


def test_enforce_allow_list_overrides() -> None:
    p = ApprovalPolicy(mode="enforce", allow={"execute_code"})
    assert decide("execute_code", {}, policy=p).allowed is True


def test_enforce_deny_list_wins() -> None:
    p = ApprovalPolicy(mode="enforce", allow={"send_email"},
                       deny={"send_email"})
    d = decide("send_email", {}, policy=p)
    assert d.allowed is False and "deny-list" in d.reason


def test_auto_allow_level_write() -> None:
    p = ApprovalPolicy(mode="enforce", auto_allow_level=RiskLevel.WRITE)
    assert decide("create_task", {}, policy=p).allowed is True   # write ok
    assert decide("delete_task", {}, policy=p).allowed is False  # danger no


def test_observe_allows_but_flags() -> None:
    p = ApprovalPolicy(mode="observe")
    d = decide("execute_code", {}, policy=p)
    assert d.allowed is True and "observe" in d.reason


# === policy_from_env ===================================================

def test_policy_env_default_off(monkeypatch) -> None:
    monkeypatch.delenv("HERMES_APPROVAL_MODE", raising=False)
    assert policy_from_env().mode == "off"


def test_policy_env_parsing(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_APPROVAL_MODE", "enforce")
    monkeypatch.setenv("HERMES_APPROVAL_ALLOW", "a, b ,c")
    monkeypatch.setenv("HERMES_APPROVAL_DENY", "x")
    p = policy_from_env()
    assert p.mode == "enforce" and p.allow == {"a", "b", "c"} and p.deny == {"x"}
    monkeypatch.setenv("HERMES_APPROVAL_MODE", "garbage")
    assert policy_from_env().mode == "off"


# === guard_executor ====================================================

def test_guard_passes_safe_blocks_dangerous_enforce() -> None:
    calls = []

    async def real(tool, args):
        calls.append(tool)
        return "executed:" + tool

    p = ApprovalPolicy(mode="enforce")
    g = guard_executor(real, policy=p)
    assert _run(g("graph_search", {})) == "executed:graph_search"
    blocked = json.loads(_run(g("execute_code", {"code": "x"})))
    assert blocked["error"] == "approval_required"
    assert calls == ["graph_search"]  # dangerous НЕ исполнен


def test_guard_off_is_transparent() -> None:
    async def real(tool, args):
        return "ok"

    g = guard_executor(real, policy=ApprovalPolicy(mode="off"))
    assert _run(g("delete_everything", {})) == "ok"


def test_guard_audit_called_on_block() -> None:
    events = []

    async def real(tool, args):
        return "ok"

    async def audit(event, detail):
        events.append((event, detail["tool"]))

    g = guard_executor(real, policy=ApprovalPolicy(mode="enforce"),
                       audit=audit)
    _run(g("delete_x", {}))
    assert events and events[0][0] == "approval.blocked"


def test_guard_audit_failure_does_not_break() -> None:
    async def real(tool, args):
        return "ok"

    async def bad_audit(event, detail):
        raise RuntimeError("audit down")

    g = guard_executor(real, policy=ApprovalPolicy(mode="enforce"),
                       audit=bad_audit)
    # блок всё равно возвращается, без исключения
    out = json.loads(_run(g("drop_table", {})))
    assert out["error"] == "approval_required"


def test_guard_executor_exception_propagates_only_from_real() -> None:
    async def real(tool, args):
        raise RuntimeError("real tool failed")

    g = guard_executor(real, policy=ApprovalPolicy(mode="off"))
    # off → guard прозрачен, ошибка реального исполнителя не глотается
    # гардом (это контракт исходного executor, не наша зона)
    try:
        _run(g("x", {}))
        assert False, "should have raised"
    except RuntimeError:
        pass
