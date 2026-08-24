# -*- coding: utf-8 -*-
"""P0: кросс-tenant утечка — чужая Person в панели «Эволюция объекта».

Корень: EntityResolver матчил по имени по всему общему Neo4j без tenant-
фильтра → свойства чужого узла (tenant_id, meeting_ids чужих встреч)
записывались в per-tenant temporal-стор через create_version. Фиксы:
  1. резолв ищет только среди своих tenant-ов + legacy;
  2. новые стабильные id получают tenant-суффикс (MERGE по id в общем
     Neo4j больше не склеивает одноимённых людей из разных аккаунтов);
  3. get_node_by_id умеет tenant-фильтр;
  4. knowledge_sync блокирует обновление чужого узла;
  5. temporal API прячет уже отравленные записи (read-side).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from backend.core.store.entity_resolver import EntityResolver
from backend.core.store.graph_builder import GraphBuilder
from backend.core.store.tenant_scope import (
    allowed_tenants,
    canonical_tenant,
    is_own_tenant,
)

OWN = "tenant-own-e98"
FOREIGN = "tenant-foreign-bca"


def _graph(tmp_path):
    gb = GraphBuilder(use_networkx=True,
                      graph_storage_path=str(tmp_path / "graph.json"))
    asyncio.run(gb.connect())
    return gb


def _add_person(gb, node_id, name, tenant):
    attrs = {"_label": "Person", "name": name}
    if tenant is not None:
        attrs["tenant_id"] = tenant
    gb.nx_graph.add_node(node_id, **attrs)


# === tenant_scope ==========================================================

def test_allowed_tenants_solo_user():
    """Solo-пользователь (без организаций): только он сам."""
    assert allowed_tenants(OWN) == {OWN}
    assert allowed_tenants("") == set()
    assert allowed_tenants(None) == set()


def test_is_own_tenant_legacy_permissive():
    allowed = {OWN}
    assert is_own_tenant(None, allowed)      # legacy без штампа — свой
    assert is_own_tenant("", allowed)
    assert is_own_tenant(OWN, allowed)
    assert not is_own_tenant(FOREIGN, allowed)


def test_canonical_tenant_solo_is_user():
    assert canonical_tenant(OWN) == OWN


# === EntityResolver: резолв не пересекает границу тенанта ==================

def test_resolver_does_not_match_foreign_person(tmp_path):
    """Одноимённый человек из чужого тенанта НЕ матчится — создаётся свой."""
    gb = _graph(tmp_path)
    _add_person(gb, "person_светлана", "светлана", FOREIGN)
    resolver = EntityResolver(graph_builder=gb)

    resolved = asyncio.run(resolver.resolve(
        name="Светлана", entity_type="person", organization_id=OWN))
    assert resolved.is_new, "чужой узел не должен резолвиться как существующий"
    assert resolved.canonical_id != "person_светлана"


def test_resolver_matches_own_and_legacy(tmp_path):
    gb = _graph(tmp_path)
    _add_person(gb, "person_иван", "иван", OWN)
    _add_person(gb, "person_пётр", "пётр", None)  # legacy без штампа
    resolver = EntityResolver(graph_builder=gb)

    own = asyncio.run(resolver.resolve(
        name="Иван", entity_type="person", organization_id=OWN))
    assert not own.is_new and own.canonical_id == "person_иван"

    legacy = asyncio.run(resolver.resolve(
        name="Пётр", entity_type="person", organization_id=OWN))
    assert not legacy.is_new and legacy.canonical_id == "person_пётр"


def test_resolver_fuzzy_and_alias_skip_foreign(tmp_path):
    gb = _graph(tmp_path)
    _add_person(gb, "person_светлана_иванова", "светлана иванова", FOREIGN)
    gb.nx_graph.add_node("person_алиас", _label="Person", name="другое имя",
                         tenant_id=FOREIGN, aliases=["светлана"])
    resolver = EntityResolver(graph_builder=gb)

    resolved = asyncio.run(resolver.resolve(
        name="Светлана Иванова", entity_type="person", organization_id=OWN))
    assert resolved.is_new


def test_new_entity_id_gets_tenant_suffix(tmp_path):
    """Стабильный id новой сущности — с tenant-суффиксом: MERGE по id в
    общем Neo4j не склеит одноимённых людей из разных аккаунтов."""
    gb = _graph(tmp_path)
    resolver = EntityResolver(graph_builder=gb)
    resolved = asyncio.run(resolver.resolve(
        name="Новый Человек", entity_type="person", organization_id=OWN))
    assert resolved.is_new
    assert resolved.canonical_id.startswith("person_")
    assert resolved.canonical_id.endswith(f"__{OWN}")


# === GraphBuilder.get_node_by_id: tenant-фильтр ============================

def test_get_node_by_id_tenant_filter(tmp_path):
    gb = _graph(tmp_path)
    _add_person(gb, "p_own", "свой", OWN)
    _add_person(gb, "p_foreign", "чужой", FOREIGN)
    _add_person(gb, "p_legacy", "легаси", None)

    async def run():
        # Без фильтра — прежнее поведение
        assert await gb.get_node_by_id("p_foreign") is not None
        # С фильтром: свой и legacy проходят, чужой — нет
        assert await gb.get_node_by_id("p_own", tenant_id=OWN) is not None
        assert await gb.get_node_by_id("p_legacy", tenant_id=OWN) is not None
        assert await gb.get_node_by_id("p_foreign", tenant_id=OWN) is None
        # Список тенантов (user + org)
        assert await gb.get_node_by_id(
            "p_foreign", tenant_id=[OWN, "org-1"]) is None
        assert await gb.get_node_by_id(
            "p_own", tenant_id=[OWN, "org-1"]) is not None
        # strict: legacy тоже отсекается
        assert await gb.get_node_by_id(
            "p_legacy", tenant_id=OWN, strict_tenant=True) is None

    asyncio.run(run())


# === Temporal: read-side анти-протечка =====================================

def test_temporal_route_filters_foreign_records():
    from backend.api.routes.temporal import _is_foreign_record
    allowed = {OWN}
    assert not _is_foreign_record({"name": "Иван"}, allowed)          # без штампа
    assert not _is_foreign_record({"tenant_id": OWN}, allowed)
    assert not _is_foreign_record({"tenant_id": ""}, allowed)
    assert _is_foreign_record({"tenant_id": FOREIGN}, allowed)        # чужая
    assert not _is_foreign_record(None, allowed)
    assert not _is_foreign_record("not-a-dict", allowed)


def test_temporal_tracker_list_entities_exposes_tenant(tmp_path):
    """list_entities отдаёт tenant_id последней версии — API-слой по нему
    прячет чужие записи."""
    from backend.core.temporal.temporal_tracker import TemporalTracker
    tracker = TemporalTracker(db_path=str(tmp_path / "t.db"), user_id=OWN)

    async def run():
        await tracker.create_version(
            entity_id="person_чужая", entity_type="person",
            data={"name": "Чужая", "tenant_id": FOREIGN,
                  "meeting_ids": ["m-foreign"]},
            changed_by="system", change_type="UPDATE", source="m-foreign")
        await tracker.create_version(
            entity_id="person_своя", entity_type="person",
            data={"name": "Своя", "role": "dev"},
            changed_by="system", change_type="CREATE", source="m-own")
        return await tracker.list_entities()

    entities = asyncio.run(run())
    by_id = {e["entity_id"]: e for e in entities}
    assert by_id["person_чужая"]["tenant_id"] == FOREIGN
    assert by_id["person_своя"]["tenant_id"] is None

    from backend.api.routes.temporal import _is_foreign_record
    allowed = {OWN}
    visible = [e for e in entities if not _is_foreign_record(e, allowed)]
    assert [e["entity_id"] for e in visible] == ["person_своя"]


def test_purge_script_removes_foreign_rows(tmp_path, monkeypatch, capsys):
    """purge_foreign_temporal: dry-run ничего не трогает, --apply удаляет."""
    from backend.core.temporal.temporal_tracker import TemporalTracker
    data_dir = tmp_path / "data" / "temporal"
    data_dir.mkdir(parents=True)
    db = str(data_dir / f"{OWN}.db")
    tracker = TemporalTracker(db_path=db, user_id=OWN)

    async def seed():
        await tracker.create_version(
            entity_id="person_чужая", entity_type="person",
            data={"name": "Чужая", "tenant_id": FOREIGN},
            changed_by="system", change_type="UPDATE", source="m1")
        await tracker.create_version(
            entity_id="person_своя", entity_type="person",
            data={"name": "Своя"},
            changed_by="system", change_type="CREATE", source="m2")
        await tracker.close()

    asyncio.run(seed())

    monkeypatch.chdir(tmp_path)
    import importlib
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "scripts"))
    purge = importlib.import_module("purge_foreign_temporal")

    # dry-run
    monkeypatch.setattr(sys, "argv", ["purge_foreign_temporal.py", OWN])
    assert purge.main() == 0
    import sqlite3
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 2
    conn.close()

    # apply
    monkeypatch.setattr(sys, "argv",
                        ["purge_foreign_temporal.py", OWN, "--apply"])
    assert purge.main() == 0
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT entity_id FROM versions").fetchall()
    conn.close()
    assert rows == [("person_своя",)]
