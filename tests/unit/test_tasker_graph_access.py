# -*- coding: utf-8 -*-
"""Таскер должен видеть те же знания, что и чат.

Инцидент: на запрос «сделай описание ролика для vibe tasking» Таскер написал
«Найдено 0 фактов» и досочинил контент, хотя чат по тому же запросу находил
13 узлов графа. Причина оказалась не в модели: chat.py передаёт
storage=<GraphBuilder>, а _get_graph_builder() распознавал только dict-с-ключом
и объект-с-атрибутом .graph_builder → возвращал None → Neo4j даже не пробовался
→ «Ни один граф не найден» → 0 фактов → LLM добирала из головы.
"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

nx = pytest.importorskip("networkx")

from backend.core.think.task_specification.data_driven_orchestrator import (  # noqa: E402
    DataDrivenTaskSystem,
)


class _FakeGraphBuilder:
    """Похож на настоящий GraphBuilder по утиной типизации (есть driver)."""

    def __init__(self, nx_graph=None):
        self.driver = None          # Neo4j не поднят → пойдём в nx-fallback
        self.nx_graph = nx_graph


def _sys(storage):
    return DataDrivenTaskSystem(storage=storage, llm_router=None,
                                output_dir="/tmp/tasker_test_out")


# ── Корневая причина ────────────────────────────────────────────────────────

def test_storage_is_graph_builder_itself():
    """Именно так его зовёт chat.py — и именно это раньше давало None."""
    gb = _FakeGraphBuilder()
    assert _sys(gb)._get_graph_builder() is gb


def test_dict_storage_still_works():
    gb = _FakeGraphBuilder()
    assert _sys({"graph_builder": gb})._get_graph_builder() is gb


def test_nested_attribute_still_works():
    gb = _FakeGraphBuilder()

    class _Wrapper:
        graph_builder = gb

    assert _sys(_Wrapper())._get_graph_builder() is gb


def test_junk_storage_gives_none():
    assert _sys(object())._get_graph_builder() is None
    assert _sys(None)._get_graph_builder() is None


# ── Сценарий пользователя: знание про vibe tasking должно находиться ────────

def _graph_with_vibe_tasking():
    g = nx.DiGraph()
    g.add_node("m1", name="vibe tasking", _label="Meeting",
               description="Система Vibe Tasking в T-Send: задачи из встреч "
                           "выполняются автоматически на цифровом мозге")
    g.add_node("p1", name="Meetflow", _label="Product",
               description="Платформа записи и анализа встреч")
    return g


def test_vibe_tasking_is_found_via_nx_fallback():
    """Путь с NetworkX-графом (без Neo4j) — знание должно находиться."""
    import asyncio

    sysm = _sys(_FakeGraphBuilder(_graph_with_vibe_tasking()))
    res = asyncio.run(sysm._graph_search("описание ролика для vibe tasking"))
    names = " ".join(str(r) for r in res).lower()
    assert res, "граф найден, но результатов нет — Таскер снова ослеп"
    assert "vibe tasking" in names


# ── ПРОДОВЫЙ путь: Neo4j-режим (именно он и отказал) ───────────────────────
# В проде GraphBuilder работает на Neo4j, а .nx_graph пуст — поэтому старый код
# не спасал даже fallback'ом: _get_graph_builder() возвращал None → Neo4j не
# пробовался → «Ни один граф не найден». Мокаем драйвер, чтобы проверить
# именно эту ветку (без живой БД).

class _Rec(dict):
    pass


class _FakeSession:
    def __init__(self, sink):
        self.sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, query, params=None):
        self.sink.append({"query": query, "params": params or {}})
        if "MATCH (a)-[r]->(b)" in query:
            return []
        return [_Rec(id="m1", labels=["Meeting"],
                     props={"name": "vibe tasking",
                            "description": "Система Vibe Tasking в T-Send",
                            "tenant_id": "u-1"})]


class _FakeDriver:
    def __init__(self, sink):
        self.sink = sink

    def session(self):
        return _FakeSession(self.sink)

    def close(self):
        pass


class _Neo4jBuilder:
    """GraphBuilder в Neo4j-режиме: driver есть, nx_graph пуст (как в проде)."""

    def __init__(self):
        self.driver = object()
        self.uri, self.user, self.password = "bolt://x", "u", "p"
        self.nx_graph = None


def test_neo4j_mode_reaches_the_graph(monkeypatch):
    """Продовый сценарий инцидента: без фикса сюда не доходило вовсе."""
    import asyncio

    import neo4j
    sink = []
    monkeypatch.setattr(neo4j.GraphDatabase, "driver",
                        lambda *a, **k: _FakeDriver(sink))

    sysm = _sys(_Neo4jBuilder())
    sysm._current_user_id = "u-1"
    res = asyncio.run(sysm._graph_search("описание ролика для vibe tasking"))

    assert sink, "запрос к Neo4j не ушёл — Таскер снова не видит граф"
    assert res and "vibe tasking" in " ".join(str(r) for r in res).lower()


def test_neo4j_query_is_scoped_and_relevant(monkeypatch):
    """Запрос обязан быть привязан к тенанту и к терминам запроса, а не
    тянуть первые 1000 узлов подряд (утечка + мимо релевантности)."""
    import asyncio

    import neo4j
    sink = []
    monkeypatch.setattr(neo4j.GraphDatabase, "driver",
                        lambda *a, **k: _FakeDriver(sink))

    sysm = _sys(_Neo4jBuilder())
    sysm._current_user_id = "u-1"
    asyncio.run(sysm._graph_search("vibe tasking"))

    node_q = next(q for q in sink if "MATCH (a)-[r]->(b)" not in q["query"])
    assert "tenant_id" in node_q["query"], "нет тенант-фильтра — узлы чужих аккаунтов"
    assert node_q["params"].get("tenant") == "u-1"
    assert "$terms" in node_q["query"], "нет фильтра по терминам — берутся случайные узлы"
    assert "vibe" in node_q["params"].get("terms", [])
