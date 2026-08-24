"""Тесты скана неявных упоминаний Tessbrain/MeetFlow (токено-экономно, без LLM)."""
from __future__ import annotations

import asyncio

import pytest


def _run(coro):
    return asyncio.run(coro)


class _FakeGraph:
    def __init__(self, rows):
        self._rows = rows

    def nodes(self, data=False):
        return self._rows if data else [r[0] for r in self._rows]


def _fake_gb(rows):
    class FakeGB:
        connected = True
        nx_graph = _FakeGraph(rows)

        def __init__(self, **k):
            pass

        async def connect(self):
            pass

        async def close(self, save=False):
            pass

    return FakeGB


@pytest.fixture
def graph(monkeypatch):
    import backend.core.store.graph_builder as gbm
    import backend.core.store.tenant_paths as tp

    def _install(rows):
        monkeypatch.setattr(gbm, "GraphBuilder", _fake_gb(rows))
        monkeypatch.setattr(tp, "graph_path_for_user", lambda uid: "x")
    return _install


_UID = "bca75db7-8336-411a-a0f6-49c3b2adfbbb"


def test_scan_finds_product_plus_negative(graph) -> None:
    from backend.core.help.mention_scanner import scan_mentions
    graph([
        ("n1", {"_label": "Meeting", "summary": "обсудили что Tessbrain не работает при загрузке"}),
        ("n2", {"_label": "Task", "title": "обычная задача про клиента"}),      # neutral
        ("n3", {"_label": "Decision", "text": "meetflow непонятно куда нажать"}),
        ("n4", {"_label": "Meeting", "summary": "Tessbrain — отличный инструмент"}),  # product, no negative
    ])
    hits = _run(scan_mentions(_UID))
    assert len(hits) == 2
    labels = {h["label"] for h in hits}
    assert labels == {"Meeting", "Decision"}
    assert all(h["marker"] for h in hits)


def test_scan_no_graph(monkeypatch) -> None:
    import backend.core.store.graph_builder as gbm

    class Dead:
        connected = False
        nx_graph = None

        def __init__(self, **k):
            pass

        async def connect(self):
            pass

        async def close(self, save=False):
            pass

    import backend.core.store.tenant_paths as tp
    monkeypatch.setattr(gbm, "GraphBuilder", Dead)
    monkeypatch.setattr(tp, "graph_path_for_user", lambda uid: "x")
    from backend.core.help.mention_scanner import scan_mentions
    assert _run(scan_mentions(_UID)) == []


def test_report_aggregates_without_llm(graph) -> None:
    from backend.core.help.mention_scanner import build_mention_report
    graph([
        ("n1", {"_label": "Meeting", "summary": "Tessbrain не работает"}),
        ("n3", {"_label": "Decision", "text": "meetflow непонятно"}),
    ])
    rep = _run(build_mention_report(user_ids=[_UID], use_llm=False))
    assert rep["count"] == 2 and rep["users_affected"] == 1
    assert "Неявные упоминания" in rep["summary"]
    # приватность: агрегат не содержит сырой сниппет клиента
    assert "не работает" not in rep["summary"]


def test_mention_scan_enabled(monkeypatch) -> None:
    from backend.core.help.mention_scanner import mention_scan_enabled
    monkeypatch.delenv("GUIDE_MENTION_SCAN", raising=False)
    assert mention_scan_enabled() is False
    monkeypatch.setenv("GUIDE_MENTION_SCAN", "on")
    assert mention_scan_enabled() is True
