"""Unit-тесты для federated read view (personal ∪ org).

Что проверяем:
  - solo-user (без org) → merged view == personal-only
  - user в org → видит и personal-узлы, и org-узлы
  - personal-wins при конфликте node_id (узел в обоих графах)
  - read-only защита: save заблокирован, source-файлы не перетёрты
  - битый org-graph не валит personal-flow
  - graph_storage_path сохранён (важно для chat.py re-init check)
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import sys
import types
import uuid
from pathlib import Path
from typing import Any

import pytest

# Lazy-safe module bootstrap (см. test_meeting_idempotency.py — те же
# тесты в общем suite подменяют sys.modules стабами).
_REAL_STORE_MODULES: dict[str, Any] = {}


def _force_load(name: str, rel_path: str):
    full = Path(__file__).resolve().parents[2] / rel_path
    spec = importlib.util.spec_from_file_location(name, full)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    _REAL_STORE_MODULES[name] = mod
    return mod


if not getattr(sys.modules.get("backend.core.store"), "__file__", None):
    pkg = types.ModuleType("backend.core.store")
    pkg.__path__ = [str(Path(__file__).resolve().parents[2] / "backend" / "core" / "store")]
    sys.modules["backend.core.store"] = pkg
    _REAL_STORE_MODULES["backend.core.store"] = pkg

_tp = _force_load("backend.core.store.tenant_paths", "backend/core/store/tenant_paths.py")
from backend.core.store import graph_builder as _gb  # noqa: E402
from backend.core.store import graph_view as _gv  # noqa: E402
_REAL_STORE_MODULES["backend.core.store.graph_builder"] = _gb
_REAL_STORE_MODULES["backend.core.store.graph_view"] = _gv

from backend.core.ingest import membership  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_store_modules():
    for name, mod in _REAL_STORE_MODULES.items():
        sys.modules[name] = mod
    yield


@pytest.fixture
def tmp_graphs(tmp_path, monkeypatch):
    monkeypatch.setattr(_tp, "_DATA_ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def fake_mapping(tmp_path, monkeypatch):
    """Изолированный user_org_mapping.json с очисткой membership-кэша."""
    p = tmp_path / "user_org_mapping.json"
    monkeypatch.setattr(
        "backend.core.ingest.membership._user_org_mapping_path",
        lambda: str(p),
    )
    membership.clear_cache()
    return p


def _run(coro):
    return asyncio.run(coro)


async def _seed_personal(user_id: str, nodes: list[dict], edges: list[tuple] = ()):
    """Положить узлы и рёбра в personal-graph."""
    gb = _gb.GraphBuilder(
        use_networkx=True,
        graph_storage_path=_tp.graph_path_for_user(user_id),
    )
    await gb.connect()
    for n in nodes:
        await gb.create_node(
            node_id=n["id"], label=n["label"],
            properties=n.get("props", {"user_id": user_id}),
        )
    for src, tgt, rel in edges:
        await gb.create_relationship(from_id=src, to_id=tgt, rel_type=rel)
    await gb.save()
    await gb.close()


async def _seed_org(org_id: str, nodes: list[dict], edges: list[tuple] = ()):
    """Положить узлы и рёбра в org-graph."""
    gb = _gb.GraphBuilder(
        use_networkx=True,
        graph_storage_path=_tp.org_graph_path(org_id),
    )
    await gb.connect()
    for n in nodes:
        await gb.create_node(
            node_id=n["id"], label=n["label"],
            properties=n.get("props", {"tenant_id": org_id}),
        )
    for src, tgt, rel in edges:
        await gb.create_relationship(from_id=src, to_id=tgt, rel_type=rel)
    await gb.save()
    await gb.close()


# =============================================================================
# Solo-user (без org) — merged view == personal-only
# =============================================================================


def test_solo_user_merged_view_equals_personal(tmp_graphs, fake_mapping):
    """REGRESSION: Mini Tess / solo-founder без org — видит ровно свои узлы."""
    user_id = str(uuid.uuid4())
    _run(_seed_personal(user_id, [
        {"id": "meeting_a", "label": "Meeting"},
        {"id": "decision_a", "label": "Decision", "props": {"user_id": user_id, "summary": "X"}},
    ]))

    view = _run(_gv.merged_graph_view_for_user(user_id))
    try:
        assert view.connected is True
        node_ids = set(view.nx_graph.nodes())
        assert node_ids == {"meeting_a", "decision_a"}
    finally:
        _run(view.close(save=False))


def test_solo_user_with_no_personal_graph_returns_empty(tmp_graphs, fake_mapping):
    """Новый user без единой записи — пустой граф, не падаем."""
    user_id = str(uuid.uuid4())
    view = _run(_gv.merged_graph_view_for_user(user_id))
    try:
        assert view.connected is True
        assert view.nx_graph.number_of_nodes() == 0
    finally:
        _run(view.close(save=False))


# =============================================================================
# User в org — merged view = personal ∪ org
# =============================================================================


def test_user_in_org_sees_personal_and_org_nodes(tmp_graphs, fake_mapping):
    user_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())
    fake_mapping.write_text(json.dumps({user_id: {"org_id": org_id, "role": "founder"}}))
    membership.clear_cache()

    _run(_seed_personal(user_id, [
        {"id": "my_note", "label": "Meeting", "props": {"user_id": user_id, "title": "personal"}},
    ]))
    _run(_seed_org(org_id, [
        {"id": "team_meeting", "label": "Meeting", "props": {"tenant_id": org_id, "title": "team"}},
        {"id": "team_decision", "label": "Decision",
         "props": {"tenant_id": org_id, "summary": "Ship", "source_meeting_id": "team_meeting"}},
    ], edges=[("team_meeting", "team_decision", "HAS_DECISION")]))

    view = _run(_gv.merged_graph_view_for_user(user_id))
    try:
        node_ids = set(view.nx_graph.nodes())
        assert "my_note" in node_ids        # из personal
        assert "team_meeting" in node_ids   # из org
        assert "team_decision" in node_ids  # из org
        # И ребро из org должно быть видно
        assert view.nx_graph.has_edge("team_meeting", "team_decision")
    finally:
        _run(view.close(save=False))


def test_personal_wins_on_node_id_conflict(tmp_graphs, fake_mapping):
    """Узел с одним id в personal и в org → видим personal-версию."""
    user_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())
    fake_mapping.write_text(json.dumps({user_id: {"org_id": org_id, "role": "manager"}}))
    membership.clear_cache()

    shared_id = "meeting_shared"
    _run(_seed_personal(user_id, [
        {"id": shared_id, "label": "Meeting",
         "props": {"user_id": user_id, "title": "personal-fresh"}},
    ]))
    _run(_seed_org(org_id, [
        {"id": shared_id, "label": "Meeting",
         "props": {"tenant_id": org_id, "title": "org-stale"}},
    ]))

    view = _run(_gv.merged_graph_view_for_user(user_id))
    try:
        node_attrs = view.nx_graph.nodes[shared_id]
        # personal wins — title из personal-версии
        assert node_attrs.get("title") == "personal-fresh"
    finally:
        _run(view.close(save=False))


# =============================================================================
# Read-only защита: save не должен перетереть исходные файлы
# =============================================================================


def test_save_on_merged_view_is_blocked(tmp_graphs, fake_mapping):
    """REGRESSION: попытка save() на merged view НЕ должна перетереть personal-файл."""
    user_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())
    fake_mapping.write_text(json.dumps({user_id: {"org_id": org_id, "role": "founder"}}))
    membership.clear_cache()

    _run(_seed_personal(user_id, [
        {"id": "p1", "label": "Meeting", "props": {"user_id": user_id, "title": "p"}},
    ]))
    _run(_seed_org(org_id, [
        {"id": "o1", "label": "Meeting", "props": {"tenant_id": org_id, "title": "o"}},
    ]))

    personal_file = Path(_tp.graph_path_for_user(user_id))
    personal_before = personal_file.read_text()

    view = _run(_gv.merged_graph_view_for_user(user_id))
    try:
        # Симулируем кого-то, кто по ошибке вызвал save (или close(save=True))
        _run(view.save())  # должно быть no-op + ERROR-log
    finally:
        _run(view.close(save=True))  # тоже no-op

    personal_after = personal_file.read_text()
    assert personal_after == personal_before, \
        "merged view save() leaked org nodes into personal-graph!"


# =============================================================================
# Resilience: битый org-файл не валит personal-flow
# =============================================================================


def test_corrupt_org_graph_does_not_break_personal_view(tmp_graphs, fake_mapping):
    user_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())
    fake_mapping.write_text(json.dumps({user_id: {"org_id": org_id, "role": "founder"}}))
    membership.clear_cache()

    _run(_seed_personal(user_id, [
        {"id": "p1", "label": "Meeting", "props": {"user_id": user_id, "title": "p"}},
    ]))
    # Записать битый org-файл (некорректный JSON).
    org_file = Path(_tp.org_graph_path(org_id))
    org_file.write_text("{not valid json")

    view = _run(_gv.merged_graph_view_for_user(user_id))
    try:
        assert view.connected is True
        assert "p1" in view.nx_graph.nodes()  # personal жив
    finally:
        _run(view.close(save=False))


# =============================================================================
# Совместимость с chat.py: graph_storage_path сохраняется
# =============================================================================


def test_graph_storage_path_preserved_for_chat_reinit_check(tmp_graphs, fake_mapping):
    """chat.py re-init проверяет gb.graph_storage_path != graph_path.
    Если бы helper передавал другой path, кэш бы каждый раз сбрасывался.
    """
    user_id = str(uuid.uuid4())
    view = _run(_gv.merged_graph_view_for_user(user_id))
    try:
        assert view.graph_storage_path == _tp.graph_path_for_user(user_id)
    finally:
        _run(view.close(save=False))


# =============================================================================
# writer_graph_for_user — writer-side helper
# =============================================================================


def test_writer_graph_for_user_is_writeable(tmp_graphs):
    """REGRESSION: writer-helper НЕ блокирует save."""
    user_id = str(uuid.uuid4())
    wb = _run(_gv.writer_graph_for_user(user_id, use_networkx=True))
    try:
        # save должен реально работать (не быть подменён на blocker).
        # Проверяем что метод — оригинальный bound method, а не блокер.
        assert wb._save_graph_to_file is not _gv._readonly_save_blocker
        # Реально пишем узел и сохраняем.
        _run(wb.create_node(
            node_id="written_via_writer",
            label="Decision",
            properties={"user_id": user_id, "summary": "test"},
        ))
        _run(wb.save())
    finally:
        _run(wb.close(save=False))

    # После повторного open узел на месте — save сработал.
    wb2 = _run(_gv.writer_graph_for_user(user_id, use_networkx=True))
    try:
        assert wb2.nx_graph.has_node("written_via_writer")
    finally:
        _run(wb2.close(save=False))


def test_writer_graph_does_not_load_org(tmp_graphs, fake_mapping):
    """REGRESSION: writer НЕ загружает org-граф (изоляция от personal-файла)."""
    user_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())
    fake_mapping.write_text(json.dumps({user_id: {"org_id": org_id, "role": "employee"}}))
    membership.clear_cache()

    # Кладём узел в org-граф (отдельно).
    _run(_seed_org(org_id, [
        {"id": "org_only_node", "label": "Decision",
         "props": {"tenant_id": org_id, "summary": "org-data"}},
    ]))

    wb = _run(_gv.writer_graph_for_user(user_id, use_networkx=True))
    try:
        # writer-builder видит только personal-граф (пустой).
        # org-узлы НЕ должны попасть — это изоляция: писатель
        # personal-flow не должен случайно расширить personal-файл
        # org-данными при следующем save.
        assert not wb.nx_graph.has_node("org_only_node")
    finally:
        _run(wb.close(save=False))
