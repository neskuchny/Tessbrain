# -*- coding: utf-8 -*-
"""Векторная индексация в гибриде вызывается по НАСТОЯЩЕЙ сигнатуре.

Дефект, который эти тесты закрепляют
(docs/ru/REVIEW_TZ_FIX_SEARCH_DEFECTS.md, задача 3):
`HybridSearchOrchestrator.index_document` звал
`vector_indexer.add_document(doc_id=..., text=..., metadata=...)`, а
сигнатура — `add_document(text, metadata, access_group="public")`.
Лишний `doc_id` давал TypeError на КАЖДОМ документе, и его тут же гасил
`except Exception` с записью в лог. Наружу ветка выглядела рабочей:
исключение не всплывало, счётчик не велся, «проиндексировано» печаталось.

Почему обычный мок такое не ловит: `Mock()` принимает любые аргументы,
поэтому тест с моком проходит и на сломанном коде. Здесь подставляется
объект с ТОЧНОЙ сигнатурой продукта, поэтому неверный вызов падает так
же, как в проде.

Контракты:
  1. КОНТРОЛЬ: дублёр действительно отвергает `doc_id=` — иначе тест
     вакуумный и прошёл бы на старом коде;
  2. index_document кладёт вектор, а doc_id доезжает через metadata["id"]
     (именно оттуда его берёт VectorIndexer.add_document);
  3. index_documents_batch — то же самое для пакета;
  4. свой `id` в metadata не затирается;
  5. отказ индексации ВИДЕН в статистике, а не только в логах.

Запуск: python tests/unit/test_vector_index_signature.py
"""
from __future__ import annotations

import asyncio
import inspect
import os
import sys

try:                                     # pragma: no cover - зависит от среды
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from backend.core.search.hybrid_search_orchestrator import (  # noqa: E402
    HybridSearchOrchestrator)
from backend.core.store.vector_indexer import VectorIndexer  # noqa: E402


class StrictVectorIndexer:
    """Дублёр с сигнатурой настоящего VectorIndexer.add_document.

    Сигнатура сверяется с продуктовой в тесте 1: если продукт её поменяет,
    дублёр разъедется и это будет видно, а не замаскировано.
    """

    def __init__(self):
        self.docs = []

    async def add_document(self, text, metadata, access_group="public"):
        doc_id = (metadata or {}).get("id")
        self.docs.append({"id": doc_id, "text": text,
                          "metadata": dict(metadata or {})})

    def get_stats(self):
        return {"total_vectors": len(self.docs)}


class BrokenVectorIndexer(StrictVectorIndexer):
    """Всегда падает — для проверки, что отказ виден в статистике."""

    async def add_document(self, text, metadata, access_group="public"):
        raise RuntimeError("векторный бэкенд недоступен")


# user_id обязан быть UUID: tenant_paths._require_uuid отвергает произвольные
# строки. На этом уже спотыкался стенд Гелиона — двадцать встреч подряд
# отвергались с «Invalid user_id», и это выглядело как результат замера.
TEST_UID = "9e110000-0000-4000-8000-0000000000aa"


def _orch(vi):
    return HybridSearchOrchestrator(vector_indexer=vi, user_id=TEST_UID)


# ── 1. Контроль: дублёр повторяет продуктовую сигнатуру ────────────────

def test_control_stub_matches_product_signature():
    real = inspect.signature(VectorIndexer.add_document)
    stub = inspect.signature(StrictVectorIndexer.add_document)
    assert list(real.parameters) == list(stub.parameters), (
        f"дублёр разъехался с продуктом: {list(real.parameters)} "
        f"против {list(stub.parameters)}")
    assert "doc_id" not in real.parameters, (
        "у add_document появился doc_id — тест надо переписать, а не удалять")

    # и он ДЕЙСТВИТЕЛЬНО отвергает старый вызов
    vi = StrictVectorIndexer()
    try:
        asyncio.run(vi.add_document(doc_id="d1", text="t", metadata={}))
    except TypeError:
        print("✅ контроль: старый вызов с doc_id= отвергается, как в проде")
        return
    raise AssertionError("контроль недействителен: дублёр принял doc_id=")


# ── 2. index_document кладёт вектор, doc_id идёт через metadata ────────

def test_index_document_indexes_vector():
    vi = StrictVectorIndexer()
    orch = _orch(vi)
    asyncio.run(orch.index_document("doc-1", "текст встречи",
                                    {"meeting_id": "m1"}))
    assert len(vi.docs) == 1, f"вектор не проиндексирован: {vi.docs}"
    assert vi.docs[0]["id"] == "doc-1", (
        f"doc_id не доехал через metadata['id']: {vi.docs[0]}")
    assert vi.docs[0]["metadata"]["meeting_id"] == "m1"
    assert orch.get_stats().get("vector_indexed") == 1
    print("✅ index_document: вектор записан, doc_id доехал через metadata")


# ── 3. То же для пакета ───────────────────────────────────────────────

def test_index_documents_batch_indexes_vectors():
    vi = StrictVectorIndexer()
    orch = _orch(vi)
    docs = [("d1", "первый", {"meeting_id": "m1"}),
            ("d2", "второй", {"meeting_id": "m2"})]
    asyncio.run(orch.index_documents_batch(docs))
    assert len(vi.docs) == 2, f"пакет не проиндексирован: {vi.docs}"
    assert [d["id"] for d in vi.docs] == ["d1", "d2"]
    print("✅ index_documents_batch: оба вектора записаны")


# ── 4. Свой id в metadata не затирается ───────────────────────────────

def test_explicit_metadata_id_wins():
    vi = StrictVectorIndexer()
    orch = _orch(vi)
    asyncio.run(orch.index_document("doc-1", "текст",
                                    {"id": "свой-id", "meeting_id": "m1"}))
    assert vi.docs[0]["id"] == "свой-id", (
        f"явный metadata['id'] затёрт: {vi.docs[0]}")
    print("✅ явный metadata['id'] сохраняется")


# ── 5. Отказ индексации виден в статистике ────────────────────────────

def test_indexing_failure_is_counted_not_only_logged():
    orch = _orch(BrokenVectorIndexer())
    asyncio.run(orch.index_document("doc-1", "текст", {"meeting_id": "m1"}))
    stats = orch.get_stats()
    assert stats.get("vector_index_errors") == 1, (
        "отказ индексации не виден в статистике — ровно так дефект и "
        f"прятался: {stats}")
    assert not stats.get("vector_indexed"), (
        "неудачная индексация засчитана как успешная")
    print("✅ отказ индексации посчитан, а не только записан в лог")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе контракты векторной индексации прошли.")
