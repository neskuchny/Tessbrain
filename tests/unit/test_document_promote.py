# -*- coding: utf-8 -*-
"""P2 мульти-аккаунта: промоушен ДОКУМЕНТОВ в орг-мозг (как встречи).

NetworkX-путь на реальном GraphBuilder с tmp-путями (паттерн
test_graph_view_federated): личный граф с document_{id}+чанками →
promote_document_to_org → узлы в org-графе с org_id/отделом.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest

import backend.core.ingest.meeting_promote as mp
import backend.core.ingest.membership as membership


def _uid() -> str:
    return str(uuid.uuid4())


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def tmp_env(tmp_path, monkeypatch):
    """Изолируем membership и графовые пути в tmp; NetworkX принудительно."""
    monkeypatch.setattr(membership, "_user_org_mapping_path",
                        lambda: str(tmp_path / "user_org_mapping.json"))
    membership.clear_cache()
    graphs = tmp_path / "graphs"
    orgs = tmp_path / "orgs"
    graphs.mkdir()
    monkeypatch.setattr(mp, "_graph_path_for_user",
                        lambda uid: str(graphs / f"{uid}.json"))

    def org_path(org_id: str) -> str:
        d = orgs / org_id
        d.mkdir(parents=True, exist_ok=True)
        return str(d / "graph.json")
    monkeypatch.setattr(mp, "_org_graph_path", org_path)
    # NetworkX-режим независимо от env
    monkeypatch.setenv("USE_NETWORKX", "true")
    monkeypatch.delenv("NEO4J_URI", raising=False)
    yield tmp_path
    membership.clear_cache()


async def _seed_personal_graph(user_id: str, document_id: str):
    from backend.core.store.graph_builder import GraphBuilder
    gb = GraphBuilder(use_networkx=True,
                      graph_storage_path=mp._graph_path_for_user(user_id))
    await gb.connect()
    await gb.create_node(node_id=f"document_{document_id}", label="Document",
                         properties={"document_id": document_id,
                                     "title": "Регламент продаж",
                                     "user_id": user_id})
    await gb.create_node(node_id=f"chunk_doc_chunk_{document_id}_0",
                         label="DocumentChunk",
                         properties={"document_id": document_id,
                                     "chunk_index": 0, "user_id": user_id})
    await gb.create_relationship(from_id=f"document_{document_id}",
                                 to_id=f"chunk_doc_chunk_{document_id}_0",
                                 rel_type="HAS_CHUNK")
    await gb.save()
    await gb.close(save=False)


def _org_graph_nodes(org_id: str) -> dict:
    p = Path(mp._org_graph_path(org_id))
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    return {n["id"]: n for n in data.get("nodes", [])}


def test_document_promoted_with_org_and_department(tmp_env):
    user, org = _uid(), "org-" + _uid()
    membership.add_member(user, org, role="employee")
    membership.set_member_department(user, org, "Продажи")
    doc_id = "doc-123"
    _run(_seed_personal_graph(user, doc_id))

    res = _run(mp.promote_document_to_org(
        user_id=user, document_id=doc_id, org_id=org, source="upload"))
    assert res["promoted"] is True
    assert res["backend"] == "networkx"
    assert res["nodes_copied"] == 2  # документ + чанк
    assert res["edges_copied"] == 1

    nodes = _org_graph_nodes(org)
    doc = nodes[f"document_{doc_id}"]
    assert doc["org_id"] == org and doc["tenant_id"] == org
    assert doc["promoted_from_user_id"] == user
    assert doc["department"] == "Продажи"  # отдел автора на узлах эпизода
    assert nodes[f"chunk_doc_chunk_{doc_id}_0"]["department"] == "Продажи"


def test_missing_document_is_honest(tmp_env):
    user, org = _uid(), "org-" + _uid()
    membership.add_member(user, org, role="employee")
    _run(_seed_personal_graph(user, "other-doc"))
    res = _run(mp.promote_document_to_org(
        user_id=user, document_id="nope", org_id=org))
    assert res["promoted"] is False
    assert "not_found" in (res["reason"] or "")


def test_should_promote_gate_reused_for_documents(tmp_env):
    user, org = _uid(), "org-" + _uid()
    membership.add_member(user, org, role="employee")
    # корп-источник → орга; личный источник → None; solo → None
    assert mp.should_promote(user, "upload") == org
    assert mp.should_promote(user, "mini_tess") is None
    assert mp.should_promote(_uid(), "upload") is None
