# -*- coding: utf-8 -*-
"""Тесты движка процесс-доски (P3): топология, поток данных, ветвление."""
from __future__ import annotations

import asyncio

from backend.core.board.process_engine import _topo_order, run_graph


def _run(coro):
    return asyncio.run(coro)


async def _echo_handler(node_type, data, inputs, ctx):
    # ask_brain: вернуть prompt+входы; condition: по data.contains; иначе — echo.
    up = " ".join([str(i.get("text") if isinstance(i, dict) else i) for i in inputs if i])
    if node_type == "condition":
        return {"branch": "true" if data.get("contains", "") in up else "false"}
    return {"text": (str(data.get("text") or "") + (" " + up if up else "")).strip()}


def test_topo_order_linear() -> None:
    nodes = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    edges = [{"source": "a", "target": "b"}, {"source": "b", "target": "c"}]
    assert _topo_order(nodes, edges) == ["a", "b", "c"]


def test_topo_handles_cycle_without_hang() -> None:
    nodes = [{"id": "a"}, {"id": "b"}]
    edges = [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}]
    order = _topo_order(nodes, edges)
    assert set(order) == {"a", "b"} and len(order) == 2


def test_data_flows_downstream() -> None:
    nodes = [
        {"id": "t", "type": "trigger", "data": {"text": "привет"}},
        {"id": "q", "type": "ask_brain", "data": {"text": "эхо:"}},
    ]
    edges = [{"source": "t", "target": "q"}]
    r = _run(run_graph(nodes, edges, _echo_handler, {}))
    assert r["outputs"]["q"]["text"] == "эхо: привет"


def test_condition_true_branch_only() -> None:
    nodes = [
        {"id": "t", "type": "trigger", "data": {"text": "срочно оплата"}},
        {"id": "c", "type": "condition", "data": {"contains": "оплата"}},
        {"id": "yes", "type": "notify", "data": {"text": "ветка ДА"}},
        {"id": "no", "type": "notify", "data": {"text": "ветка НЕТ"}},
    ]
    edges = [
        {"source": "t", "target": "c"},
        {"source": "c", "target": "yes", "sourceHandle": "true"},
        {"source": "c", "target": "no", "sourceHandle": "false"},
    ]
    r = _run(run_graph(nodes, edges, _echo_handler, {}))
    logmap = {x["node"]: x for x in r["log"]}
    assert "yes" in r["outputs"]            # true-ветка исполнена
    assert logmap["no"].get("skipped") is True   # false-ветка пропущена


def test_condition_false_branch() -> None:
    nodes = [
        {"id": "t", "type": "trigger", "data": {"text": "обычный день"}},
        {"id": "c", "type": "condition", "data": {"contains": "оплата"}},
        {"id": "yes", "type": "notify", "data": {"text": "ДА"}, },
        {"id": "no", "type": "notify", "data": {"text": "НЕТ"}},
    ]
    edges = [
        {"source": "t", "target": "c"},
        {"source": "c", "target": "yes", "sourceHandle": "true"},
        {"source": "c", "target": "no", "sourceHandle": "false"},
    ]
    r = _run(run_graph(nodes, edges, _echo_handler, {}))
    logmap = {x["node"]: x for x in r["log"]}
    assert "no" in r["outputs"] and logmap["yes"].get("skipped") is True


def test_inline_note_is_transparent() -> None:
    # Заметка ВНУТРИ потока (trigger → note → ask_brain) должна быть прозрачной:
    # пропустить данные дальше, а не «повесить» downstream. Регрессия: раньше
    # note не помечалась активной → узел после неё пропускался, и вся схема из
    # планировщика «не срабатывала», хотя выглядела правильной.
    nodes = [
        {"id": "t", "type": "trigger", "data": {"text": "привет"}},
        {"id": "n", "type": "note", "data": {"text": "пояснение схемы"}},
        {"id": "q", "type": "ask_brain", "data": {"text": "эхо:"}},
    ]
    edges = [{"source": "t", "target": "n"}, {"source": "n", "target": "q"}]
    r = _run(run_graph(nodes, edges, _echo_handler, {}))
    logmap = {x["node"]: x for x in r["log"]}
    assert logmap["n"].get("skipped") is True          # note не исполняется
    assert logmap["q"].get("skipped") is not True       # но downstream РАБОТАЕТ
    assert r["outputs"]["q"]["text"] == "эхо: привет"   # данные протекли сквозь note


def test_handler_exception_does_not_crash_run() -> None:
    async def boom(node_type, data, inputs, ctx):
        raise RuntimeError("узел упал")

    nodes = [{"id": "a", "type": "ask_brain", "data": {}}]
    r = _run(run_graph(nodes, [], boom, {}))
    assert r["outputs"]["a"]["error"] and "упал" in r["outputs"]["a"]["error"]
