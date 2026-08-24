# -*- coding: utf-8 -*-
"""Линза кластеров: состав — математика, имена — LLM с запретом выдумывать.

Синтетический граф с двумя явными связками проверяет, что Louvain их
разделяет, Meeting-узлы не попадают в состав (но считаются рядом),
мелочь (<3 сущностей) отбрасывается, fingerprint бережёт LLM-вызовы."""
import asyncio
import pathlib
import sys

import networkx as nx
import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.core.insight import communities as cm  # noqa: E402


@pytest.fixture(autouse=True)
def _store(monkeypatch, tmp_path):
    monkeypatch.setattr(cm, "_store_dir", lambda: tmp_path)


def _two_cluster_graph():
    g = nx.MultiDiGraph()
    # связка А: клиент + люди + задачи вокруг продукта (через встречу)
    a = [("cl1", "Client", "ООО Ромашка"), ("p1", "Person", "Иван"),
         ("p2", "Person", "Мария"), ("pr1", "Product", "Кассовый модуль"),
         ("m1", "Meeting", "Встреча с Ромашкой")]
    # связка Б: отдельный проект с другими людьми
    b = [("pj1", "Project", "Редизайн сайта"), ("p3", "Person", "Олег"),
         ("p4", "Person", "Анна"), ("t1", "Task", "Собрать макеты")]
    for nid, label, name in a + b:
        g.add_node(nid, _label=label, name=name)
    for s, t in [("cl1", "pr1"), ("p1", "cl1"), ("p2", "cl1"),
                 ("p1", "m1"), ("p2", "m1"), ("cl1", "m1")]:
        g.add_edge(s, t, _type="related")
    for s, t in [("p3", "pj1"), ("p4", "pj1"), ("t1", "pj1"), ("p3", "t1")]:
        g.add_edge(s, t, _type="works_on")
    return g


def test_detect_separates_clusters_and_hides_meetings():
    out = cm.detect_from_nx(_two_cluster_graph())
    comms = out["communities"]
    assert len(comms) == 2, "две явные связки должны разделиться"
    names = [{m["name"] for m in c["members"]} for c in comms]
    romashka = next(s for s in names if "ООО Ромашка" in s)
    project = next(s for s in names if "Редизайн сайта" in s)
    assert {"Иван", "Мария", "Кассовый модуль"} <= romashka
    assert {"Олег", "Анна", "Собрать макеты"} <= project
    assert "Встреча с Ромашкой" not in romashka, \
        "Meeting участвует в разбиении, но в состав не выводится"
    c_rom = comms[[i for i, s in enumerate(names) if "ООО Ромашка" in s][0]]
    assert c_rom["meetings_involved"] == 1
    assert c_rom["edge_types"].get("related", 0) > 0
    assert c_rom["bridges"], "центры связности посчитаны"


def test_small_groups_dropped():
    g = nx.MultiDiGraph()
    g.add_node("a", _label="Person", name="А")
    g.add_node("b", _label="Person", name="Б")
    g.add_edge("a", "b", _type="knows")
    out = cm.detect_from_nx(g)
    assert out["communities"] == [], "пара узлов — не кластер (мин. 3)"


def test_build_uses_fingerprint_to_skip_llm(monkeypatch):
    g = _two_cluster_graph()

    class _GB:
        nx_graph = g

        async def close(self, save=False):
            pass

    async def _mv(uid, use_networkx=None):
        return _GB()

    monkeypatch.setattr(
        "backend.core.store.graph_view.merged_graph_view_for_user", _mv)
    llm_calls = {"n": 0}

    class _LLM:
        async def generate_json(self, prompt="", temperature=0.3):
            llm_calls["n"] += 1
            assert "ООО Ромашка" in prompt, "в промпте реальный состав"
            assert "ТОЛЬКО сущности из списка" in prompt
            return {"summaries": [
                {"id": "c1", "name": "Связка Ромашки",
                 "pattern": "клиент, команда и продукт",
                 "significance": "похоже, что это ключевой клиент",
                 "watch_out": "проверить загрузку Ивана"}]}

    monkeypatch.setattr("backend.core.llm.router.get_llm_router",
                        lambda: _LLM())

    out = asyncio.run(cm.build_communities("u1"))
    assert out["status"] == "success" and not out["unchanged"]
    assert llm_calls["n"] == 1
    named = [c for c in out["communities"] if c.get("summary")]
    assert named and named[0]["summary"]["name"] == "Связка Ромашки"

    # граф не изменился → без пересборки и без LLM
    out2 = asyncio.run(cm.build_communities("u1"))
    assert out2["unchanged"] is True
    assert llm_calls["n"] == 1, "fingerprint сберёг LLM-вызов"

    ctx = cm.communities_context("u1")
    assert "Связка Ромашки" in ctx and "гипотезы" in ctx


def test_empty_graph_is_honest(monkeypatch):
    class _GB:
        nx_graph = nx.MultiDiGraph()

        async def close(self, save=False):
            pass

    async def _mv(uid, use_networkx=None):
        return _GB()

    monkeypatch.setattr(
        "backend.core.store.graph_view.merged_graph_view_for_user", _mv)
    out = asyncio.run(cm.build_communities("u1", with_llm=False))
    assert out["status"] == "success" and out["communities"] == []
    assert cm.communities_context("u1") == ""


def test_neo4j_backend_builds_projection(monkeypatch):
    """Регрессия «линза пока работает на networkx»: инсталляция с Neo4j
    (nx_graph=None, есть driver) обязана собирать проекцию и кластеры."""
    nodes_by_label = {
        "Client": [{"id": "cl1", "name": "ООО Ромашка"}],
        "Person": [{"id": "p1", "name": "Иван"}, {"id": "p2", "name": "Мария"},
                   {"id": "p3", "name": "Олег"}],
        "Product": [{"id": "pr1", "name": "Кассовый модуль"}],
    }
    edges = [("cl1", "pr1", "uses"), ("p1", "cl1", "works_with"),
             ("p2", "cl1", "works_with"), ("p1", "p2", "knows"),
             ("p1", "pr1", "works_on"), ("p2", "pr1", "works_on")]

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def __aiter__(self):
            self._it = iter(self._rows)
            return self

        async def __anext__(self):
            try:
                return next(self._it)
            except StopIteration:
                raise StopAsyncIteration

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def run(self, q, params):
            assert "a.id IN $ids" in q
            ids = set(params["ids"])
            return _Result([{"s": s, "t": t, "ty": ty}
                            for s, t, ty in edges
                            if s in ids and t in ids])

    class _Driver:
        def session(self):
            return _Session()

    class _GB:
        nx_graph = None
        driver = _Driver()

        async def get_all_nodes_async(self, label=None, limit=5000,
                                      tenant_id=None, strict_tenant=False):
            return nodes_by_label.get(label, [])

        async def close(self, save=False):
            pass

    async def _mv(uid, use_networkx=None):
        return _GB()

    monkeypatch.setattr(
        "backend.core.store.graph_view.merged_graph_view_for_user", _mv)
    out = asyncio.run(cm.build_communities("u1", with_llm=False))
    assert out["status"] == "success"
    assert len(out["communities"]) == 1
    names = {m["name"] for m in out["communities"][0]["members"]}
    assert {"ООО Ромашка", "Иван", "Мария", "Кассовый модуль"} <= names
