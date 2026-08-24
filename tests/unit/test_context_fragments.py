# -*- coding: utf-8 -*-
"""Гибридный контекст для досок и Mini Tess: все ветки фолбэка.

Проверяем хелпер topic_fragments на подставных модулях (реальные движки в
песочнице не поднять): гибрид из кэша, пустой гибрид → BM25, упавший
гибрид → BM25, выключатель, «не строить в бюджете мессенджера» (BM25
сейчас + фоновая сборка), сборка по требованию для досок, и полный отказ
→ ([], ""). Плюс структурно: обе врезки используют хелпер, Mini Tess — с
build_if_missing=False (не платит за сборку из своего бюджета).

Запуск:  python tests/unit/test_context_fragments.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

failures: list[str] = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  ПЛОХО ") + name + ("" if cond else f" — {detail}"))
    if not cond:
        failures.append(name)


def _pkgs(*names):
    for n in names:
        if n not in sys.modules:
            m = types.ModuleType(n)
            m.__path__ = []
            sys.modules[n] = m


_pkgs("backend", "backend.core", "backend.core.search", "backend.core.store")

# --- подставные модули ------------------------------------------------
state = {"peek": None, "built": 0, "bm25_calls": 0}


class _Hit:
    def __init__(self, text):
        self.text = text


class _Orch:
    def __init__(self, results=None, raise_=False):
        self._r = results or []
        self._raise = raise_

    async def search(self, query=None, top_k=6, **kw):
        if self._raise:
            raise RuntimeError("hybrid down")
        return [_Hit(t) for t in self._r]


hso = types.ModuleType("backend.core.search.hybrid_search_orchestrator")
hso.peek_hybrid_search_orchestrator = lambda uid=None: state["peek"]


def _factory(vector_indexer=None, graph_builder=None, user_id=None):
    state["peek"] = _Orch(["собран фоном"])
    return state["peek"]


hso.get_hybrid_search_orchestrator = _factory
sys.modules["backend.core.search.hybrid_search_orchestrator"] = hso

bm = types.ModuleType("backend.core.search.bm25_searcher")


class _BM:
    def search(self, q, top_k=6):
        state["bm25_calls"] += 1
        return [_Hit("bm25-фрагмент-1"), _Hit("bm25-фрагмент-2")]


bm.get_bm25_searcher = lambda user_id=None: _BM()
sys.modules["backend.core.search.bm25_searcher"] = bm

gv = types.ModuleType("backend.core.store.graph_view")


async def _mgv(uid, use_networkx=None):
    state["built"] += 1
    return object()


gv.merged_graph_view_for_user = _mgv
sys.modules["backend.core.store.graph_view"] = gv

tp = types.ModuleType("backend.core.store.tenant_paths")
tp.vector_index_path_for_user = lambda uid: "/tmp/x"
sys.modules["backend.core.store.tenant_paths"] = tp

vi_mod = types.ModuleType("backend.core.store.vector_indexer")


class _VI:
    def __init__(self, storage_path=None, namespace=None):
        pass

    async def connect(self):
        pass


vi_mod.VectorIndexer = _VI
sys.modules["backend.core.store.vector_indexer"] = vi_mod

spec = importlib.util.spec_from_file_location(
    "backend.core.search.context_fragments",
    os.path.join(ROOT, "backend/core/search/context_fragments.py"))
cf = importlib.util.module_from_spec(spec)
sys.modules["backend.core.search.context_fragments"] = cf
spec.loader.exec_module(cf)


async def run():
    os.environ.pop("TESSENT_CTX_HYBRID", None)

    print("ветки хелпера:")
    state["peek"] = _Orch(["гибрид-фрагмент"])
    frags, eng = await cf.topic_fragments("u1", "покажи всех, кто работал")
    check("гибрид из кэша", frags == ["гибрид-фрагмент"] and eng == "hybrid",
          f"{frags} / {eng}")

    state["peek"] = _Orch([])  # гибрид есть, но пуст
    frags, eng = await cf.topic_fragments("u1", "вопрос")
    check("пустой гибрид → BM25-фолбэк", eng == "bm25" and len(frags) == 2,
          f"{frags} / {eng}")

    state["peek"] = _Orch(raise_=True)
    frags, eng = await cf.topic_fragments("u1", "вопрос")
    check("упавший гибрид → BM25-фолбэк", eng == "bm25", eng)

    os.environ["TESSENT_CTX_HYBRID"] = "off"
    state["peek"] = _Orch(["не должен использоваться"])
    frags, eng = await cf.topic_fragments("u1", "вопрос")
    check("выключатель off → сразу BM25", eng == "bm25", eng)
    os.environ.pop("TESSENT_CTX_HYBRID")

    print("бюджет мессенджера (build_if_missing=False):")
    state["peek"] = None
    state["built"] = 0
    frags, eng = await cf.topic_fragments("u2", "вопрос", build_if_missing=False)
    check("сейчас — BM25, без ожидания сборки", eng == "bm25", eng)
    await asyncio.sleep(0)  # даём фоновой задаче отработать
    await asyncio.sleep(0)
    check("сборка запущена фоном", state["built"] == 1 and state["peek"] is not None,
          f"built={state['built']}")
    frags, eng = await cf.topic_fragments("u2", "вопрос", build_if_missing=False)
    check("следующее сообщение — уже гибрид", eng == "hybrid", eng)

    print("доски (build_if_missing=True):")
    state["peek"] = None
    state["built"] = 0
    frags, eng = await cf.topic_fragments("u3", "вопрос", build_if_missing=True)
    check("сборка по требованию → гибрид сразу",
          eng == "hybrid" and state["built"] == 1, f"{eng} built={state['built']}")

    print("полный отказ:")
    state["peek"] = _Orch(raise_=True)
    bm_old = bm.get_bm25_searcher

    def _bm_broken(user_id=None):
        raise RuntimeError("bm25 down")

    bm.get_bm25_searcher = _bm_broken
    frags, eng = await cf.topic_fragments("u1", "вопрос")
    check("оба упали → пусто без исключения", frags == [] and eng == "", f"{frags}/{eng}")
    bm.get_bm25_searcher = bm_old

    check("пустой вопрос → пусто", await cf.topic_fragments("u1", "  ") == ([], ""))


asyncio.run(run())

# --- структурно: обе врезки на месте ---------------------------------
print("врезки:")
board = io.open(os.path.join(ROOT, "backend/core/board/process_engine.py"),
                encoding="utf-8").read()
check("доски используют topic_fragments", "topic_fragments" in board)
check("в досках не остался прямой bm25 в ask_brain",
      "get_bm25_searcher(user_id=user_id).search(question" not in board)
mt = io.open(os.path.join(ROOT, "backend/core/messengers/chat_handler.py"),
             encoding="utf-8").read()
check("Mini Tess использует topic_fragments", "topic_fragments" in mt)
check("Mini Tess НЕ платит за сборку (build_if_missing=False)",
      "build_if_missing=False" in mt)

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} — {', '.join(failures)}")
    raise SystemExit(1)
print("всё сошлось")
