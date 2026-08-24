"""Тесты для Neo4j-режима promote и tombstone (P2 #10 prep).

Реальный Neo4j-driver не запускаем — мокаем через подмену AsyncGraphDatabase
для promote (через GraphBuilder) и GraphDatabase для tombstone (sync через
os.environ + neo4j module).

Цель: убедиться что при включённом Neo4j-режиме:
  - meeting_promote использует Cypher (in-place tag update), не файл-copy
  - org_cascade.tombstone использует одну Cypher-транзакцию
  - Result содержит backend="neo4j"
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import os
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

from backend.core.gdpr import org_cascade  # noqa: E402
from backend.core.ingest import meeting_promote  # noqa: E402


@pytest.fixture(autouse=True)
def _restore():
    for name, mod in _REAL.items():
        sys.modules[name] = mod
    yield


@pytest.fixture(autouse=True)
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setattr(_tp, "_DATA_ROOT", tmp_path)
    return tmp_path


def _uid() -> str:
    return str(uuid.uuid4())


def _run(coro):
    return asyncio.run(coro)


# =============================================================================
# Neo4j-side promote_meeting_to_org (via mock probe + driver)
# =============================================================================


class _FakeNeo4jRecord:
    def __init__(self, touched: int):
        self._d = {"touched": touched}

    def __getitem__(self, k):
        return self._d[k]


class _FakeNeo4jResult:
    def __init__(self, touched: int):
        self._touched = touched

    async def single(self):
        return _FakeNeo4jRecord(self._touched)


class _FakeNeo4jSession:
    def __init__(self, touched: int):
        self._touched = touched
        self.queries: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def run(self, cypher, params=None):
        self.queries.append((cypher, params or {}))
        return _FakeNeo4jResult(self._touched)


class _FakeNeo4jDriver:
    def __init__(self, touched: int = 3):
        self.touched = touched
        self.last_session: _FakeNeo4jSession | None = None
        self.closed = False

    def session(self):
        self.last_session = _FakeNeo4jSession(self.touched)
        return self.last_session

    async def close(self):
        self.closed = True


class _FakeNeo4jGraphBuilder:
    """Мокает GraphBuilder в Neo4j-режиме."""
    def __init__(self, *a, **kw):
        self.use_networkx = False
        self.driver = _FakeNeo4jDriver(touched=3)
        self.connected = False

    async def connect(self):
        self.connected = True
        return True

    async def close(self, save=False):
        await self.driver.close()


def _patch_graphbuilder(monkeypatch, builder_cls):
    """Прямая подмена GraphBuilder в загруженном модуле + sys.modules.
    monkeypatch.setattr через строку путей не работает когда parent
    backend.core.store — это stub-ModuleType из других тестов."""
    # Загружаем real-graph_builder через importlib (минуя store/__init__).
    spec = importlib.util.spec_from_file_location(
        "backend.core.store.graph_builder",
        Path(__file__).resolve().parents[2] / "backend" / "core" / "store" / "graph_builder.py",
    )
    gb_mod = importlib.util.module_from_spec(spec)
    sys.modules["backend.core.store.graph_builder"] = gb_mod
    spec.loader.exec_module(gb_mod)
    monkeypatch.setattr(gb_mod, "GraphBuilder", builder_cls)


async def test_promote_neo4j_uses_cypher_in_place(monkeypatch):
    """Neo4j-mode: promote = in-place tag через Cypher, не file copy."""
    _patch_graphbuilder(monkeypatch, _FakeNeo4jGraphBuilder)

    result = await meeting_promote.promote_meeting_to_org(
        user_id=_uid(), meeting_id=_uid(),
        org_id=_uid(), source="zoom",
    )

    assert result["backend"] == "neo4j"
    assert result["promoted"] is True
    assert result["nodes_copied"] == 3   # touched count
    assert result["edges_copied"] == 0   # в shared instance edges не копируются
    assert result["reason"] is None


async def test_promote_neo4j_no_meeting_returns_reason(monkeypatch):
    """touched=0 → meeting не существует → правильный reason."""

    class _ZeroBuilder(_FakeNeo4jGraphBuilder):
        def __init__(self, *a, **kw):
            super().__init__()
            self.driver = _FakeNeo4jDriver(touched=0)

    _patch_graphbuilder(monkeypatch, _ZeroBuilder)

    result = await meeting_promote.promote_meeting_to_org(
        user_id=_uid(), meeting_id=_uid(),
        org_id=_uid(), source="zoom",
    )
    assert result["backend"] == "neo4j"
    assert result["promoted"] is False
    assert result["reason"] == "meeting_node_not_found"


# =============================================================================
# Neo4j-side tombstone (через mock neo4j sync driver + env)
# =============================================================================


class _FakeSyncSession:
    def __init__(self, touched: int):
        self._touched = touched
        self.queries: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, cypher, params=None):
        self.queries.append((cypher, params or {}))
        # Sync result with .single() — emulate neo4j Result.
        rec = type("R", (), {"__getitem__": lambda self, k: touched_map[k]})()
        touched_map = {"touched": self._touched}
        return type("Res", (), {"single": lambda self: rec})()


class _FakeSyncDriver:
    def __init__(self, touched: int = 5):
        self.touched = touched
        self.last_session: _FakeSyncSession | None = None
        self.closed = False

    def session(self):
        self.last_session = _FakeSyncSession(self.touched)
        return self.last_session

    def close(self):
        self.closed = True


@pytest.fixture
def fake_neo4j_module(monkeypatch):
    """Inject fake neo4j SDK + NEO4J_URI env."""
    fake_neo4j = types.ModuleType("neo4j")

    class _FakeGraphDatabase:
        last_driver: _FakeSyncDriver | None = None

        @staticmethod
        def driver(uri, auth):
            d = _FakeSyncDriver(touched=5)
            _FakeGraphDatabase.last_driver = d
            return d

    fake_neo4j.GraphDatabase = _FakeGraphDatabase
    monkeypatch.setitem(sys.modules, "neo4j", fake_neo4j)
    monkeypatch.setenv("NEO4J_URI", "bolt://localhost:7687")
    monkeypatch.setenv("NEO4J_USER", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", "secret")
    monkeypatch.delenv("USE_NETWORKX_GRAPH", raising=False)
    return _FakeGraphDatabase


def test_tombstone_neo4j_uses_single_cypher(fake_neo4j_module):
    user = _uid()
    result = org_cascade.tombstone_in_org_graphs(user)
    assert result["backend"] == "neo4j"
    assert result["nodes_tombstoned"] == 5  # touched
    assert result["orgs_scanned"] == 1     # один shared instance

    # Проверим что запустился UPDATE-cypher.
    drv = fake_neo4j_module.last_driver
    queries = drv.last_session.queries
    assert len(queries) == 1
    cypher, params = queries[0]
    assert "SET n.tombstoned_at" in cypher
    assert "REMOVE" in cypher
    assert params["uid"] == user
    assert params["tombstone"].startswith("tombstone_")


def test_tombstone_neo4j_dry_run_uses_count_query(fake_neo4j_module):
    user = _uid()
    result = org_cascade.tombstone_in_org_graphs(user, dry_run=True)
    assert result["dry_run"] is True
    assert result["backend"] == "neo4j"

    drv = fake_neo4j_module.last_driver
    queries = drv.last_session.queries
    assert len(queries) == 1
    cypher, _ = queries[0]
    # Dry-run uses count-only query (no SET).
    assert "SET" not in cypher
    assert "count(n)" in cypher


def test_tombstone_falls_back_to_networkx_when_neo4j_unavailable(
    tmp_data, monkeypatch,
):
    """Если NEO4J_URI не задан И SDK нет → NetworkX path."""
    monkeypatch.delenv("NEO4J_URI", raising=False)
    monkeypatch.setitem(sys.modules, "neo4j", None)

    user = _uid()
    # Создаём пустой data/orgs/ — NetworkX-path сработает (просто ничего не найдёт).
    result = org_cascade.tombstone_in_org_graphs(user)
    assert result["backend"] == "networkx"


def test_tombstone_use_networkx_env_forces_networkx_even_if_neo4j_available(
    tmp_data, fake_neo4j_module, monkeypatch,
):
    """USE_NETWORKX_GRAPH=true — переопределяет, идём в NetworkX-путь."""
    monkeypatch.setenv("USE_NETWORKX_GRAPH", "true")
    result = org_cascade.tombstone_in_org_graphs(_uid())
    assert result["backend"] == "networkx"
