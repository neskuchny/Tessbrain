# -*- coding: utf-8 -*-
"""Relevance-rerank (RRF) в data-driven ТЗ: релевантный документ (высокий cosine)
поднимается НАД слаборелевантными узлами графа и переживает усечение — решает
СМЫСЛ, а не тип источника (граф/документ)."""
from __future__ import annotations

import asyncio

from backend.core.think.task_specification.data_driven_orchestrator import (
    DataDrivenTaskSystem,
)


def _run(coro):
    return asyncio.run(coro)


def test_relevant_document_outranks_weak_graph_nodes() -> None:
    sys = DataDrivenTaskSystem(storage={"vector_indexer": object()})
    sys._current_user_id = None

    # 80 слаборелевантных узлов графа (низкий match_score)
    async def fake_graph(query, depth=2):
        return [{"node_id": f"n{i}", "label": "Task", "name": f"Задача {i}",
                 "content": f"Задача {i}", "data": {}, "match_score": 0.05}
                for i in range(80)]

    # документ с прайсом и ВЫСОКИМ cosine
    async def fake_vec(query, top_k=10):
        return [{"content": {"score": 0.95,
                             "payload": {"text": "Тариф: 2,2 руб/мин при 10 001–100 000 минут",
                                         "title": "Прайс Meflow"}}}]

    sys._graph_search = fake_graph
    sys._vector_search = fake_vec
    plan = {"search_queries": [{"query": "тариф стоимость минуты прайс", "priority": 9}]}
    collected = _run(sys._stage_3_collect_data(plan))

    raw = collected["raw_results"]
    # документ должен оказаться в топ-3 (а не в хвосте из 80 узлов)
    top_labels = [r.get("label") for r in raw[:3]]
    assert "Document" in top_labels, f"документ не в топе: {top_labels}"
    # и его цена реально в контенте
    doc = next(r for r in raw if r.get("label") == "Document")
    assert "2,2" in doc["content"]


def test_no_documents_still_works() -> None:
    sys = DataDrivenTaskSystem(storage={"vector_indexer": object()})
    sys._current_user_id = None

    async def fake_graph(query, depth=2):
        return [{"node_id": "n1", "label": "Task", "name": "Задача",
                 "content": "Задача", "data": {}, "match_score": 0.5}]

    async def fake_vec(query, top_k=10):
        return []

    sys._graph_search = fake_graph
    sys._vector_search = fake_vec
    collected = _run(sys._stage_3_collect_data({"search_queries": [{"query": "q"}]}))
    assert collected["raw_results"] and collected["raw_results"][0]["label"] == "Task"
