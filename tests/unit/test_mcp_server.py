"""Тесты stdio MCP-сервера Tessbrain (JSON-RPC 2.0 поверх HTTP API).

HTTP-слой (_api) мокается — проверяем схему инструментов и диспатч, не сеть.
Модуль лежит в корне tessent_brain (importable как `mcp_server`).
"""
from __future__ import annotations

import io
import json
import sys

import pytest

import mcp_server as m


def _rpc(msg: dict) -> dict:
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        m._handle(msg)
    finally:
        sys.stdout = old
    out = buf.getvalue().strip()
    return json.loads(out) if out else {}


def test_initialize() -> None:
    r = _rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert r["result"]["serverInfo"]["name"] == "tessent-brain"
    assert "protocolVersion" in r["result"]


def test_tools_list_shape() -> None:
    r = _rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = r["result"]["tools"]
    assert len(tools) >= 20
    names = {t["name"] for t in tools}
    for expected in ("ask_brain", "compose_artifact", "generate_report",
                     "insights_feed", "analyze_tasks", "coding_handoff",
                     "confirm_coding_handoff"):
        assert expected in names
    for t in tools:
        assert t["description"] and t["inputSchema"]["type"] == "object"


def test_call_ask_brain(monkeypatch) -> None:
    monkeypatch.setattr(m, "_api", lambda method, path, **k: {
        "message": {"content": "ответ мозга"}, "sources": [{"title": "встреча"}]})
    r = _rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "ask_brain", "arguments": {"question": "как дела?"}}})
    txt = r["result"]["content"][0]["text"]
    assert txt.startswith("ответ мозга") and "источников: 1" in txt
    assert r["result"]["isError"] is False


def test_call_analyze_tasks(monkeypatch) -> None:
    monkeypatch.setattr(m, "_api", lambda method, path, **k: {
        "total": 10, "done": 4, "open": 6, "completion_rate": 40,
        "counts": {"todo": 3, "in_progress": 2, "blocked": 1, "done": 4},
        "blocked": [{"title": "оплата", "assignee": "Аня"}]})
    r = _rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
              "params": {"name": "analyze_tasks", "arguments": {}}})
    txt = r["result"]["content"][0]["text"]
    assert "Задач 10" in txt and "🚫 оплата" in txt


def test_call_unknown_tool() -> None:
    r = _rpc({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
              "params": {"name": "nope", "arguments": {}}})
    assert r["error"]["code"] == -32602


def test_coding_handoff_returns_pending(monkeypatch) -> None:
    # хэндофф НИЧЕГО не запускает — только PENDING (важная инвариант безопасности)
    monkeypatch.setattr(m, "_api", lambda method, path, **k: {
        "status": "pending_confirmation", "id": "h1",
        "command": "claude -p ...", "spec_text": "# ТЗ"})
    r = _rpc({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
              "params": {"name": "coding_handoff",
                         "arguments": {"title": "сделать лендинг"}}})
    txt = r["result"]["content"][0]["text"]
    assert "PENDING" in txt and "h1" in txt and "confirm" in txt.lower()


def test_call_tool_error_is_reported(monkeypatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("api down")
    monkeypatch.setattr(m, "_api", boom)
    r = _rpc({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
              "params": {"name": "insights_feed", "arguments": {}}})
    assert r["result"]["isError"] is True and "api down" in r["result"]["content"][0]["text"]
