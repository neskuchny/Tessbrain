# -*- coding: utf-8 -*-
"""Честный статус прогона процесс-доски: несоединённые блоки → warning, а не
ложный зелёный «успех» по каждому узлу."""
from __future__ import annotations

from backend.core.board.process_engine import _connectivity_warnings


def _n(nid, ntype):
    return {"id": nid, "type": ntype, "data": {}}


def test_connected_chain_has_no_warnings():
    nodes = [_n("t", "trigger"), _n("b", "ask_brain"), _n("nt", "notify")]
    edges = [{"source": "t", "target": "b"}, {"source": "b", "target": "nt"}]
    assert _connectivity_warnings(nodes, edges) == []


def test_isolated_nodes_warn():
    nodes = [_n("t", "trigger"), _n("b", "ask_brain"), _n("nt", "notify")]
    # ни одной стрелки — все три висят отдельно
    warns = _connectivity_warnings(nodes, [])
    assert warns and "отдельно" in warns[0]


def test_delivery_node_without_input_warns():
    # цепочка t→b есть, но notify не подключён (висит с входом? нет — без входа)
    nodes = [_n("t", "trigger"), _n("b", "ask_brain"), _n("nt", "notify")]
    edges = [{"source": "t", "target": "b"}]  # notify без входящей стрелки, b без выхода
    warns = _connectivity_warnings(nodes, edges)
    # notify изолирован (нет ни входа, ни выхода) → попадёт в «висят отдельно»
    assert any("отдельно" in w for w in warns)


def test_delivery_with_only_outgoing_flagged_as_dangling():
    # notify имеет исходящую стрелку, но нет входящей — не изолирован, но без входа
    nodes = [_n("nt", "notify"), _n("o", "output")]
    edges = [{"source": "nt", "target": "o"}]
    warns = _connectivity_warnings(nodes, edges)
    assert any("без входящей стрелки" in w for w in warns)


def test_note_only_or_single_node_no_warning():
    assert _connectivity_warnings([_n("x", "note"), _n("t", "trigger")], []) == []
    assert _connectivity_warnings([_n("t", "trigger")], []) == []


def test_run_process_board_sets_warning_status(monkeypatch):
    """Прогон с несоединёнными блоками → status='warning' + warnings.
    Не связано → status='success'. run_graph застаблен (без сети/хендлеров)."""
    import asyncio

    from backend.core.board import process_engine as pe

    monkeypatch.setattr(pe, "process_exec_enabled", lambda: True)

    async def fake_run_graph(nodes, edges, handler, ctx):
        return {"outputs": {}, "log": []}
    monkeypatch.setattr(pe, "run_graph", fake_run_graph)

    _run = asyncio.get_event_loop().run_until_complete

    disc = {"nodes": [_n("a", "trigger"), _n("b", "notify")], "edges": []}
    res = _run(pe.run_process_board(disc, user_id="u"))
    assert res["status"] == "warning" and res.get("warnings")

    conn = {"nodes": [_n("a", "trigger"), _n("b", "notify")],
            "edges": [{"source": "a", "target": "b"}]}
    res2 = _run(pe.run_process_board(conn, user_id="u"))
    assert res2["status"] == "success" and "warnings" not in res2
