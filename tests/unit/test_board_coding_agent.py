# -*- coding: utf-8 -*-
"""Узел «Исполнитель (агент)» на процессной доске — мост к vibe-tasking.

Аудит досок: задачи нельзя было отдать кодовому агенту/в документ из
автоматизации. Узел coding_agent: вход = ТЗ; document → готовый текст дальше
по схеме; code → CLI-агент в фоне (artifact без repo_path)."""
import asyncio

import backend.core.tasks.task_analysis as ta
from backend.core.board.process_engine import _process_handler


def test_coding_agent_node_document_mode(monkeypatch):
    calls = {}

    async def _fake_handoff(uid, task, **kw):
        calls["handoff"] = {"uid": uid, "task": task, **kw}
        return {"id": "h42"}

    async def _fake_content(uid, hid):
        calls["content"] = (uid, hid)
        return {"status": "success", "result_document": "# КП готово"}

    monkeypatch.setattr(ta, "coding_handoff", _fake_handoff)
    monkeypatch.setattr(ta, "execute_content_handoff", _fake_content)

    out = asyncio.run(_process_handler(
        "coding_agent", {"mode": "document"},
        ["ТЗ: собрать КП по тарифам"], {"user_id": "u1"}))
    assert out.get("text") == "# КП готово"
    assert out.get("handoff_id") == "h42"
    assert calls["handoff"]["spec_text"] == "ТЗ: собрать КП по тарифам"
    assert calls["content"] == ("u1", "h42")


def test_coding_agent_node_code_mode_background(monkeypatch):
    async def _fake_handoff(uid, task, **kw):
        assert kw.get("agent") == "codex"
        assert kw.get("artifact_mode") is True  # без repo_path
        return {"id": "h7"}

    async def _fake_confirm(uid, hid, **kw):
        assert kw.get("background") is True
        return {"status": "running"}

    monkeypatch.setattr(ta, "coding_handoff", _fake_handoff)
    monkeypatch.setattr(ta, "confirm_handoff", _fake_confirm)

    out = asyncio.run(_process_handler(
        "coding_agent", {"mode": "code", "agent": "codex"},
        ["ТЗ на фичу"], {"user_id": "u1"}))
    assert out.get("status") == "running" and out.get("handoff_id") == "h7"


def test_coding_agent_node_requires_input():
    out = asyncio.run(_process_handler(
        "coding_agent", {"mode": "document"}, [], {"user_id": "u1"}))
    assert "нет входного ТЗ" in str(out.get("error") or "")
