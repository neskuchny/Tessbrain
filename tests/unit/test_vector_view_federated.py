"""Тесты для federated vector search (P1 #6).

Реальные эмбеддинги требуют sentence-transformers (не установлен в env),
поэтому VectorIndexer.search мокается целиком. Тесты фокусируются на
оркестрации:
  - personal-only path (без org)
  - personal + org merge с dedupe (personal wins)
  - RBAC-фильтр на org-results (employee не видит CONFIDENTIAL)
  - Owner-check: свой документ — видим даже если access_level RESTRICTED
  - Org-vector отсутствует на диске → graceful fallback на personal
  - Empty query / empty user_id → []
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
import types
import uuid
from pathlib import Path
from typing import Any

import pytest

_REAL: dict[str, Any] = {}


def _force_load(name: str, rel_path: str):
    full = Path(__file__).resolve().parents[2] / rel_path
    spec = importlib.util.spec_from_file_location(name, full)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    _REAL[name] = mod
    return mod


if not getattr(sys.modules.get("backend.core.store"), "__file__", None):
    pkg = types.ModuleType("backend.core.store")
    pkg.__path__ = [str(Path(__file__).resolve().parents[2] / "backend" / "core" / "store")]
    sys.modules["backend.core.store"] = pkg
    _REAL["backend.core.store"] = pkg

_tp = _force_load("backend.core.store.tenant_paths", "backend/core/store/tenant_paths.py")
_vv = _force_load("backend.core.store.vector_view", "backend/core/store/vector_view.py")
_store_pkg = sys.modules["backend.core.store"]
setattr(_store_pkg, "tenant_paths", _tp)
setattr(_store_pkg, "vector_view", _vv)

from backend.core.access import levels  # noqa: E402
from backend.core.ingest import membership  # noqa: E402


@pytest.fixture(autouse=True)
def _restore():
    for name, mod in _REAL.items():
        sys.modules[name] = mod
    yield


@pytest.fixture(autouse=True)
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setattr(_tp, "_DATA_ROOT", tmp_path)
    membership.clear_cache()
    return tmp_path


def _uid() -> str:
    return str(uuid.uuid4())


def _run(coro):
    return asyncio.run(coro)


# === Mock VectorIndexer ====================================================


class _FakeIndexer:
    """Mock с заранее заданными результатами по namespace."""

    # namespace → list of results
    _ROUTING: dict[str, list[dict[str, Any]]] = {}

    def __init__(self, storage_path=None, namespace=None, **kw):
        self.storage_path = storage_path
        self.namespace = namespace

    async def connect(self):
        return True

    async def search(self, query, **kw):
        return list(_FakeIndexer._ROUTING.get(self.namespace, []))


@pytest.fixture(autouse=True)
def _stub_indexer(monkeypatch):
    """Подменяем VectorIndexer в vector_view._lazy_imports."""
    _FakeIndexer._ROUTING = {}
    # vector_view импортирует VectorIndexer внутри функции — патчим
    # через sys.modules чтобы lazy-import достал наш fake.
    fake_mod = types.ModuleType("backend.core.store.vector_indexer")
    fake_mod.VectorIndexer = _FakeIndexer
    monkeypatch.setitem(sys.modules, "backend.core.store.vector_indexer", fake_mod)
    return _FakeIndexer


# === Tests =================================================================


def test_empty_inputs_return_empty():
    assert _run(_vv.federated_vector_search("", "query")) == []
    assert _run(_vv.federated_vector_search("uid", "")) == []


def test_solo_user_only_personal_results(tmp_data, _stub_indexer):
    user = _uid()
    _stub_indexer._ROUTING = {
        user: [
            {"id": "p1", "score": 0.9, "payload": {"document_id": "doc1", "user_id": user}},
            {"id": "p2", "score": 0.7, "payload": {"document_id": "doc2", "user_id": user}},
        ],
    }
    results = _run(_vv.federated_vector_search(user, "query", limit=10))
    assert len(results) == 2
    assert results[0]["score"] == 0.9  # отсортировано desc


def test_user_in_org_merges_personal_and_org(tmp_data, _stub_indexer):
    user = _uid()
    org = _uid()
    membership.add_member(user, org, role="employee")

    # Создаём org-vector файл (proxy для exists() check).
    org_v = Path(_tp.org_vector_path(org))
    org_v.parent.mkdir(parents=True, exist_ok=True)
    org_v.write_text("{}")

    _stub_indexer._ROUTING = {
        user: [
            {"id": "p1", "score": 0.8, "payload": {"document_id": "doc1", "user_id": user}},
        ],
        f"org_{org}": [
            {"id": "o1", "score": 0.95, "payload": {"document_id": "doc_org", "tenant_id": org}},
        ],
    }
    results = _run(_vv.federated_vector_search(user, "query", limit=10))
    ids = {r["payload"]["document_id"] for r in results}
    assert ids == {"doc1", "doc_org"}
    # Org result имеет higher score → должен быть first
    assert results[0]["payload"]["document_id"] == "doc_org"


def test_dedupe_personal_wins(tmp_data, _stub_indexer):
    """Один document_id в обоих → берём personal."""
    user = _uid()
    org = _uid()
    membership.add_member(user, org, role="employee")
    Path(_tp.org_vector_path(org)).parent.mkdir(parents=True, exist_ok=True)
    Path(_tp.org_vector_path(org)).write_text("{}")

    _stub_indexer._ROUTING = {
        user: [
            {"id": "p1", "score": 0.5,
             "payload": {"document_id": "shared_doc", "user_id": user, "version": "personal"}},
        ],
        f"org_{org}": [
            {"id": "o1", "score": 0.99,
             "payload": {"document_id": "shared_doc", "tenant_id": org, "version": "org-stale"}},
        ],
    }
    results = _run(_vv.federated_vector_search(user, "query", limit=10))
    assert len(results) == 1
    assert results[0]["payload"]["version"] == "personal"


def test_rbac_filters_out_confidential_org_results(tmp_data, _stub_indexer):
    """Employee (max=INTERNAL) не видит CONFIDENTIAL/RESTRICTED из org."""
    user = _uid()
    org = _uid()
    membership.add_member(user, org, role="employee")
    Path(_tp.org_vector_path(org)).parent.mkdir(parents=True, exist_ok=True)
    Path(_tp.org_vector_path(org)).write_text("{}")

    _stub_indexer._ROUTING = {
        user: [],
        f"org_{org}": [
            {"id": "ok", "score": 0.9,
             "payload": {"document_id": "public_doc", "tenant_id": org,
                         "access_level": int(levels.AccessLevel.PUBLIC)}},
            {"id": "hidden", "score": 0.95,
             "payload": {"document_id": "secret_doc", "tenant_id": org,
                         "access_level": int(levels.AccessLevel.CONFIDENTIAL)}},
            {"id": "hr", "score": 0.85,
             "payload": {"document_id": "hr_doc", "tenant_id": org,
                         "access_level": int(levels.AccessLevel.RESTRICTED)}},
        ],
    }
    results = _run(_vv.federated_vector_search(user, "query", limit=10))
    doc_ids = {r["payload"]["document_id"] for r in results}
    assert doc_ids == {"public_doc"}


def test_founder_sees_all_levels(tmp_data, _stub_indexer):
    user = _uid()
    org = _uid()
    membership.add_member(user, org, role="founder")
    Path(_tp.org_vector_path(org)).parent.mkdir(parents=True, exist_ok=True)
    Path(_tp.org_vector_path(org)).write_text("{}")

    _stub_indexer._ROUTING = {
        user: [],
        f"org_{org}": [
            {"id": "1", "score": 0.9,
             "payload": {"document_id": "doc1", "tenant_id": org,
                         "access_level": int(lvl)}}
            for lvl in (
                levels.AccessLevel.PUBLIC,
                levels.AccessLevel.INTERNAL,
                levels.AccessLevel.CONFIDENTIAL,
                levels.AccessLevel.RESTRICTED,
            )
        ],
    }
    results = _run(_vv.federated_vector_search(user, "query", limit=10))
    # 4 разных уровня — все видны founder'у.
    # Заметим: фейк генерирует 4 result'а с одинаковым document_id="doc1",
    # поэтому dedupe оставит 1. Это OK — тест в первую очередь проверяет
    # что не было фильтрации. Подправим payload чтобы id были разные:
    _stub_indexer._ROUTING[f"org_{org}"] = [
        {"id": str(i), "score": 0.9 - i * 0.01,
         "payload": {"document_id": f"doc{i}", "tenant_id": org,
                     "access_level": int(lvl)}}
        for i, lvl in enumerate((
            levels.AccessLevel.PUBLIC,
            levels.AccessLevel.INTERNAL,
            levels.AccessLevel.CONFIDENTIAL,
            levels.AccessLevel.RESTRICTED,
        ))
    ]
    results = _run(_vv.federated_vector_search(user, "query", limit=10))
    assert len(results) == 4


def test_owner_sees_own_confidential_personal_doc(tmp_data, _stub_indexer):
    """REGRESSION: свой документ с access_level=RESTRICTED — видим owner'у."""
    user = _uid()
    org = _uid()
    membership.add_member(user, org, role="employee")  # max=INTERNAL

    _stub_indexer._ROUTING = {
        user: [
            {"id": "p1", "score": 0.8,
             "payload": {"document_id": "my_secret", "user_id": user,
                         "access_level": int(levels.AccessLevel.RESTRICTED)}},
        ],
    }
    results = _run(_vv.federated_vector_search(user, "query"))
    assert len(results) == 1
    assert results[0]["payload"]["document_id"] == "my_secret"


def test_org_vector_missing_file_falls_back_to_personal_only(tmp_data, _stub_indexer):
    """Если org_vector_path не существует на диске — graceful только personal."""
    user = _uid()
    org = _uid()
    membership.add_member(user, org, role="employee")
    # НЕ создаём файл org_vector_path

    _stub_indexer._ROUTING = {
        user: [{"id": "p1", "score": 0.7, "payload": {"document_id": "d1", "user_id": user}}],
        f"org_{org}": [{"id": "o1", "score": 0.9, "payload": {"document_id": "d2"}}],
    }
    results = _run(_vv.federated_vector_search(user, "query"))
    # Org не подключился — нет результатов от него.
    assert len(results) == 1
    assert results[0]["payload"]["document_id"] == "d1"


def test_personal_search_exception_does_not_break(tmp_data, _stub_indexer, monkeypatch):
    """REGRESSION: если personal search упал, org-results всё равно вернутся."""
    user = _uid()
    org = _uid()
    membership.add_member(user, org, role="founder")
    Path(_tp.org_vector_path(org)).parent.mkdir(parents=True, exist_ok=True)
    Path(_tp.org_vector_path(org)).write_text("{}")

    async def _bad_search(self, **kw):
        if self.namespace == user:
            raise RuntimeError("personal index corrupted")
        return list(_FakeIndexer._ROUTING.get(self.namespace, []))

    _FakeIndexer.search = _bad_search  # type: ignore
    _stub_indexer._ROUTING = {
        f"org_{org}": [{"id": "o1", "score": 0.9,
                        "payload": {"document_id": "org_doc", "tenant_id": org}}],
    }
    try:
        results = _run(_vv.federated_vector_search(user, "query"))
    finally:
        # Восстановим оригинальный search чтобы не загрязнить другие тесты.
        async def _normal(self, **kw):
            return list(_FakeIndexer._ROUTING.get(self.namespace, []))
        _FakeIndexer.search = _normal  # type: ignore

    assert len(results) == 1
    assert results[0]["payload"]["document_id"] == "org_doc"


def test_top_k_respects_limit(tmp_data, _stub_indexer):
    user = _uid()
    _stub_indexer._ROUTING = {
        user: [
            {"id": str(i), "score": 0.9 - i * 0.01,
             "payload": {"document_id": f"doc{i}", "user_id": user}}
            for i in range(20)
        ],
    }
    results = _run(_vv.federated_vector_search(user, "query", limit=5))
    assert len(results) == 5
    # Сортировка по score desc
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)
