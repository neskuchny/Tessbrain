# -*- coding: utf-8 -*-
"""Бюджет прогона доски: лимиты объявлены до старта, остановка — честная.

При исчерпании BOARD_MAX_NODES / BOARD_MAX_SECONDS / BOARD_MAX_LLM_CALLS прогон
останавливается, возвращает лучшее готовое (выходы исполненных узлов) и запись
node="__budget__" в логе; статус — "partial". Частичный провал не прячется:
упавшие узлы при дошедшем до конца прогоне тоже дают "partial" + errors_count.
"""
from __future__ import annotations

import asyncio

from backend.core.board import process_engine as pe
from backend.core.board.process_engine import run_graph


def _chain(n: int, ntype: str = "generate"):
    """Цепочка из n фейковых узлов ntype: n0 → n1 → … → n(n-1)."""
    nodes = [{"id": f"n{i}", "type": ntype, "data": {"label": f"Узел {i}"}}
             for i in range(n)]
    edges = [{"source": f"n{i}", "target": f"n{i + 1}"} for i in range(n - 1)]
    return nodes, edges


async def _ok_handler(node_type, data, inputs, ctx):
    return {"text": f"выход {node_type}"}


def test_max_nodes_budget_stops_run(monkeypatch):
    """BOARD_MAX_NODES=2 на цепочке из 4 → partial, __budget__ в логе,
    выполненные узлы сохранили результат, невыполненные названы."""
    monkeypatch.setenv("BOARD_MAX_NODES", "2")
    nodes, edges = _chain(4)
    res = asyncio.run(run_graph(nodes, edges, _ok_handler))
    assert res["status"] == "partial"
    stops = [l for l in res["log"] if l.get("node") == "__budget__"]
    assert len(stops) == 1 and stops[0]["status"] == "stopped"
    msg = stops[0]["message"]
    assert "Бюджет прогона исчерпан" in msg and "выполнено 2 узлов" in msg
    assert "не выполнено 2" in msg and "Узел 2" in msg and "Узел 3" in msg
    assert "Результаты выполненных узлов сохранены" in msg
    # лучшее готовое возвращено: выходы n0/n1 есть, n2/n3 не исполнялись
    assert res["outputs"]["n0"]["text"] and res["outputs"]["n1"]["text"]
    assert "n2" not in res["outputs"] and "n3" not in res["outputs"]


def test_max_llm_calls_budget(monkeypatch):
    """BOARD_MAX_LLM_CALLS=1: второй LLM-узел (тип generate из _LLM_NODE_TYPES)
    уже не исполняется — стоп с причиной именно про LLM-вызовы."""
    monkeypatch.setenv("BOARD_MAX_LLM_CALLS", "1")
    nodes, edges = _chain(3, ntype="generate")
    res = asyncio.run(run_graph(nodes, edges, _ok_handler))
    assert res["status"] == "partial"
    stop = next(l for l in res["log"] if l.get("node") == "__budget__")
    assert "LLM-вызовы" in stop["message"]
    assert "n0" in res["outputs"] and "n1" not in res["outputs"]


def test_llm_limit_ignores_non_llm_nodes(monkeypatch):
    """Не-LLM узлы (trigger/notify/condition) счётчик LLM не тратят."""
    monkeypatch.setenv("BOARD_MAX_LLM_CALLS", "1")
    nodes, edges = _chain(4, ntype="notify")
    res = asyncio.run(run_graph(nodes, edges, _ok_handler))
    assert res["status"] == "done"
    assert not [l for l in res["log"] if l.get("node") == "__budget__"]


def test_defaults_keep_prev_behavior(monkeypatch):
    """Без переменных окружения — щедрые дефолты, поведение прежнее: done,
    без записей __budget__."""
    for v in ("BOARD_MAX_NODES", "BOARD_MAX_SECONDS", "BOARD_MAX_LLM_CALLS"):
        monkeypatch.delenv(v, raising=False)
    nodes, edges = _chain(3)
    res = asyncio.run(run_graph(nodes, edges, _ok_handler))
    assert res["status"] == "done"
    assert all(f"n{i}" in res["outputs"] for i in range(3))
    assert not [l for l in res["log"] if l.get("node") == "__budget__"]


def test_node_error_makes_run_partial_with_errors_count(monkeypatch):
    """Прогон дошёл до конца, но узел упал → итог "partial" + errors_count
    (частичный провал не прячется за гладким статусом)."""
    async def handler(node_type, data, inputs, ctx):
        if node_type == "trigger":
            return {"text": "старт"}
        return {"error": "узел упал"}

    monkeypatch.setattr(pe, "_process_handler", handler)
    monkeypatch.setattr(pe, "process_exec_enabled", lambda: True)
    graph = {"nodes": [{"id": "t", "type": "trigger", "data": {}},
                       {"id": "g", "type": "generate", "data": {}}],
             "edges": [{"source": "t", "target": "g"}]}
    res = asyncio.run(pe.run_process_board(graph, user_id="u"))
    assert res["status"] == "partial"
    assert res["errors_count"] == 1


def test_error_propagation_counts_root_only(monkeypatch):
    """Протяжка ошибки по цепочке (input_failed) — тень ОДНОГО корневого сбоя:
    errors_count считает только его."""
    async def handler(node_type, data, inputs, ctx):
        if node_type == "trigger":
            return {"text": ""}
        if node_type == "meeting_data":
            return {"error": "не задана встреча"}
        return {"text": "ok"}

    monkeypatch.setattr(pe, "_process_handler", handler)
    monkeypatch.setattr(pe, "process_exec_enabled", lambda: True)
    graph = {"nodes": [{"id": "t", "type": "trigger", "data": {}},
                       {"id": "m", "type": "meeting_data", "data": {}},
                       {"id": "g", "type": "generate", "data": {}},
                       {"id": "n", "type": "notify", "data": {}}],
             "edges": [{"source": "t", "target": "m"},
                       {"source": "m", "target": "g"},
                       {"source": "g", "target": "n"}]}
    res = asyncio.run(pe.run_process_board(graph, user_id="u"))
    assert res["status"] == "partial"
    assert res["errors_count"] == 1


def test_clean_run_keeps_prev_status(monkeypatch):
    """Прогон без лимитов и без ошибок — прежний статус (без partial и
    errors_count)."""
    for v in ("BOARD_MAX_NODES", "BOARD_MAX_SECONDS", "BOARD_MAX_LLM_CALLS"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(pe, "_process_handler", _ok_handler)
    monkeypatch.setattr(pe, "process_exec_enabled", lambda: True)
    graph = {"nodes": [{"id": "t", "type": "trigger", "data": {}},
                       {"id": "g", "type": "generate", "data": {}}],
             "edges": [{"source": "t", "target": "g"}]}
    res = asyncio.run(pe.run_process_board(graph, user_id="u"))
    # прежний статус завершённого прогона (run_graph отдаёт "done", merge в
    # run_process_board его сохраняет — поведение существующих досок не меняем)
    assert res["status"] == "done"
    assert "errors_count" not in res
    assert not [l for l in res["log"] if l.get("node") == "__budget__"]


def test_budget_stop_survives_run_process_board(monkeypatch):
    """Бюджет-стоп из run_graph доезжает до итога run_process_board как
    "partial" (merge статусов его не съедает)."""
    monkeypatch.setenv("BOARD_MAX_NODES", "1")
    monkeypatch.setattr(pe, "_process_handler", _ok_handler)
    monkeypatch.setattr(pe, "process_exec_enabled", lambda: True)
    graph = {"nodes": [{"id": "t", "type": "trigger", "data": {}},
                       {"id": "g", "type": "generate", "data": {}}],
             "edges": [{"source": "t", "target": "g"}]}
    res = asyncio.run(pe.run_process_board(graph, user_id="u"))
    assert res["status"] == "partial"
    assert any(l.get("node") == "__budget__" for l in res["log"])
