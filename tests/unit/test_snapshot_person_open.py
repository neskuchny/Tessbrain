# -*- coding: utf-8 -*-
"""Клик по сотруднику из иерархии снапшотов.

Прод-кейс: иерархия показывает сотрудника (p:ceo, «Основатель / CEO»), а
клик отвечает «не найден в графе знаний». Причины: (1) кэшированный
singleton-генератор не перечитывает диск, файлы которого пишут свежие
генераторы других роутов и консолидация; (2) синтетический id из иерархии
не является узлом графа, хотя ИМЯ человека резолвится штатно.

Плюс: батч заголовков встреч в поиске ронялся целиком из-за одного
не-UUID id (docsource_…) — 400 22P02 на весь чанк из 100 встреч.
"""
import asyncio
import json
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def test_person_snapshot_reloaded_from_disk(tmp_path, monkeypatch):
    """Файл снапшота появился ПОСЛЕ создания генератора — клик его видит."""
    from backend.core.sleep.enhanced_snapshot import (
        EnhancedSnapshotGenerator,
        PersonSnapshot,
    )

    gen = EnhancedSnapshotGenerator(graph_builder=None, llm_router=None,
                                    storage_path=str(tmp_path))
    assert gen._person_snapshots == {}

    # другой процесс/генератор записал снапшот на диск
    persons = tmp_path / "persons"
    persons.mkdir()
    snap = PersonSnapshot(name="Антон", role="CEO / разработчик")
    (persons / "p_ceo.json").write_text(
        json.dumps(snap.to_dict(), ensure_ascii=False), encoding="utf-8")

    assert gen._reload_person_snapshot("p_ceo") is True
    assert gen._person_snapshots["p_ceo"].name == "Антон"
    assert gen._reload_person_snapshot("nope") is False


def test_meeting_titles_batch_filters_non_uuid(monkeypatch):
    """docsource_… не попадает в uuid-запрос и не роняет весь чанк."""
    import backend.core.search.enhanced_search_orchestrator as eso

    monkeypatch.setattr(eso, "_meeting_titles_cache", {}, raising=False)
    sent = []

    class _SB:
        async def _request(self, method, path, params=None):
            sent.append(params["id"])
            assert "docsource_" not in params["id"], \
                "не-UUID id не должен попадать в запрос по meetings.id"
            return [{"id": "1e8400e2-9b2d-4c1e-8f6a-000000000001",
                     "title": "Планёрка"}]

    monkeypatch.setattr("backend.db.supabase_client.get_supabase_client",
                        lambda: _SB())

    orch = eso.EnhancedSearchOrchestrator.__new__(
        eso.EnhancedSearchOrchestrator)
    asyncio.run(orch._get_meeting_titles_batch([
        "1e8400e2-9b2d-4c1e-8f6a-000000000001",
        "docsource_703d9d2a-7e75-4cea-8ad9-d1e5d094234d",
    ]))
    assert len(sent) == 1, "запрос ушёл (uuid остался в пачке)"
    assert eso._meeting_titles_cache[
        "1e8400e2-9b2d-4c1e-8f6a-000000000001"] == "Планёрка"
    # чужеродный id закэширован пустым — не будет дёргаться повторно
    assert eso._meeting_titles_cache[
        "docsource_703d9d2a-7e75-4cea-8ad9-d1e5d094234d"] == ""


def test_meeting_titles_batch_all_non_uuid(monkeypatch):
    """Пачка целиком из не-UUID — запроса нет вовсе, всё в кэше пустым."""
    import backend.core.search.enhanced_search_orchestrator as eso

    monkeypatch.setattr(eso, "_meeting_titles_cache", {}, raising=False)

    class _SB:
        async def _request(self, *a, **kw):
            raise AssertionError("запроса быть не должно")

    monkeypatch.setattr("backend.db.supabase_client.get_supabase_client",
                        lambda: _SB())
    orch = eso.EnhancedSearchOrchestrator.__new__(
        eso.EnhancedSearchOrchestrator)
    asyncio.run(orch._get_meeting_titles_batch(["docsource_x", "chat:y"]))
    assert eso._meeting_titles_cache == {"docsource_x": "", "chat:y": ""}
