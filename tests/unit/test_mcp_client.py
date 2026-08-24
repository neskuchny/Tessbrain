"""Unit-тесты P12h: MCP-клиент (чистый протокол-слой).

Транспорт инъектируем (фейк) — без процессов/сети. stdio-транспорт
интеграционный, тут не тестируется (нужен реальный MCP-сервер).
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
    "backend.core.hermes.mcp_client",
    _ROOT / "backend/core/hermes/mcp_client.py",
)
_mc = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mc
_spec.loader.exec_module(_mc)

is_mcp_tool = _mc.is_mcp_tool
normalize_tool_defs = _mc.normalize_tool_defs
MCPRegistry = _mc.MCPRegistry
registry_from_config = _mc.registry_from_config


def _run(c):
    return asyncio.run(c)


def _fake_transport(tools=None, call_result=None, fail=False):
    async def _t(method, params):
        if fail:
            raise RuntimeError("transport down")
        if method == "tools/list":
            return {"tools": tools or []}
        if method == "tools/call":
            return call_result if call_result is not None else {
                "ok": True, "echo": params}
        return {}
    return _t


# === naming / split ====================================================

def test_is_mcp_tool() -> None:
    assert is_mcp_tool("mcp__fs__read_file")
    assert not is_mcp_tool("graph_search")
    assert not is_mcp_tool(None)  # type: ignore[arg-type]


# === normalize =========================================================

def test_normalize_namespaces_and_shapes() -> None:
    raw = [
        {"name": "read_file", "description": "read",
         "inputSchema": {"type": "object", "properties": {"p": {}}}},
        {"name": "write_file"},
        {"no_name": 1},          # skipped
        "garbage",                # skipped
    ]
    defs = normalize_tool_defs("fs", raw)
    names = [d["name"] for d in defs]
    assert names == ["mcp__fs__read_file", "mcp__fs__write_file"]
    assert defs[0]["parameters"] == {"type": "object", "properties": {"p": {}}}
    assert defs[1]["parameters"] == {"type": "object", "properties": {}}


def test_normalize_garbage_safe() -> None:
    assert normalize_tool_defs("s", None) == []
    assert normalize_tool_defs("s", "nope") == []


def test_normalize_bounded() -> None:
    raw = [{"name": f"t{i}"} for i in range(200)]
    assert len(normalize_tool_defs("s", raw)) <= 64


# === registry discover/execute =========================================

def test_discover_aggregates_servers() -> None:
    reg = MCPRegistry()
    reg.register("fs", _fake_transport(tools=[{"name": "read_file"}]))
    reg.register("gh", _fake_transport(tools=[{"name": "create_issue"}]))
    defs = _run(reg.discover())
    names = {d["name"] for d in defs}
    assert names == {"mcp__fs__read_file", "mcp__gh__create_issue"}


def test_discover_isolates_failing_server() -> None:
    reg = MCPRegistry()
    reg.register("ok", _fake_transport(tools=[{"name": "t"}]))
    reg.register("bad", _fake_transport(fail=True))
    defs = _run(reg.discover())
    assert [d["name"] for d in defs] == ["mcp__ok__t"]  # never raises


def test_execute_routes_to_server() -> None:
    reg = MCPRegistry()
    reg.register("fs", _fake_transport(call_result={"content": "hello"}))
    out = json.loads(_run(reg.execute("mcp__fs__read_file", {"p": "/x"})))
    assert out == {"content": "hello"}


def test_execute_unknown_server_safe() -> None:
    reg = MCPRegistry()
    out = json.loads(_run(reg.execute("mcp__ghost__t", {})))
    assert "error" in out


def test_execute_transport_failure_safe() -> None:
    reg = MCPRegistry()
    reg.register("fs", _fake_transport(fail=True))
    out = json.loads(_run(reg.execute("mcp__fs__t", {})))
    assert "error" in out  # never raises


def test_register_ignores_bad_input() -> None:
    reg = MCPRegistry()
    reg.register("", _fake_transport())
    reg.register("x", "not callable")  # type: ignore[arg-type]
    assert reg.server_names == []


# === registry_from_config (injectable factory) =========================

def test_registry_from_config_empty_is_noop() -> None:
    reg = registry_from_config(None)
    assert reg.server_names == []
    reg2 = registry_from_config({})
    assert reg2.server_names == []


def test_registry_from_config_uses_injected_factory() -> None:
    made = []

    def factory(cmd):
        made.append(cmd)
        return _fake_transport(tools=[{"name": "t"}])

    reg = registry_from_config({"fs": "npx server-fs /tmp"},
                               transport_factory=factory)
    assert reg.server_names == ["fs"]
    assert made == ["npx server-fs /tmp"]
    defs = _run(reg.discover())
    assert defs[0]["name"] == "mcp__fs__t"


def test_registry_from_config_factory_failure_safe() -> None:
    def bad_factory(cmd):
        raise RuntimeError("spawn fail")

    reg = registry_from_config({"x": "cmd"}, transport_factory=bad_factory)
    assert reg.server_names == []  # never raises, skipped
