# -*- coding: utf-8 -*-
"""Дата источника доезжает до цитаты — и это дата встречи, а не дня заливки.

Дефект. В ответе цитата выходила как «[Unknown] [user] I'm planning a
trip…» — без даты. Причина не в модели: в контекст ей уходил заголовок
фрагмента `[{тип}] {название}` (+ название встречи), и даты в нём не было
вообще. Датировать цитату модели было НЕЧЕМ.

Второе звено — глубже, на индексации. У 480 из 500 фрагментов демо-корпуса
не было ни одного поля с датой встречи: `date` проставлялся только карточке
встречи, а решения, задачи, участники, сущности, KPI и знания получали лишь
`indexed_at` — время заливки. При этом `indexed_at` стоял в списке полей
якоря, и temporal-резолвер молча брал его: «вчера» из мартовской встречи
разрешалось во «вчера от дня индексации». Неверный якорь хуже отсутствующего.

Третье — формат. `parse_anchor_date` понимал ISO и «21 August, 2022», но не
`2023/05/30 (Tue) 23:40` (выгрузки мессенджеров). На таком корпусе резолвер
не работал вовсе, и это не было видно: он недеструктивен и молчит.

Контракты:
  1. выгрузочные форматы даты разбираются, ISO не сломан;
  2. время обработки якорем НЕ становится, дата встречи — становится;
  3. заголовок фрагмента датирован, когда дата есть;
  4. когда даты нет — скобки нет (даты не выдумываем);
  5. когда есть только `indexed_at` — в заголовке НЕТ дня заливки;
  6. индексация кладёт дату встречи в payload каждого фрагмента;
  7. create_node штампует `_source_date` (через него идут Knowledge и Chunk).

Запуск: python tests/unit/test_source_date_in_citations.py
"""
from __future__ import annotations

import asyncio
import datetime
import os
import sys
import tempfile

try:                                     # pragma: no cover - зависит от среды
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from backend.core.search.temporal_resolver import (  # noqa: E402
    _PROCESS_TIME_FIELDS,
    anchor_from_metadata,
    parse_anchor_date,
)

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  ПЛОХО ") + name + ("" if cond else f" — {detail}"))
    if not cond:
        failures.append(name)


# ── 1. форматы даты ──────────────────────────────────────────────────
def test_export_date_formats():
    print("разбор даты:")
    d = datetime.date(2023, 5, 30)
    check("выгрузка мессенджера '2023/05/30 (Tue) 23:40'",
          parse_anchor_date("2023/05/30 (Tue) 23:40") == d,
          str(parse_anchor_date("2023/05/30 (Tue) 23:40")))
    check("точечный '2023.05.30'", parse_anchor_date("2023.05.30") == d)
    # регрессия: то, что работало, работает
    check("ISO '2023-05-30'", parse_anchor_date("2023-05-30") == d)
    check("ISO с временем", parse_anchor_date("2023-05-30T23:40:00") == d)
    check("словесный '21 August, 2022'",
          parse_anchor_date("21 August, 2022") == datetime.date(2022, 8, 21))
    check("мусор → None", parse_anchor_date("не дата") is None)


# ── 2. что считается якорем ──────────────────────────────────────────
def test_process_time_is_not_an_anchor():
    print("выбор якоря:")
    # Перебираем именно объявленный список: добавят туда поле — проверка
    # накроет его сама, без правки теста.
    check("список полей времени обработки не пуст", len(_PROCESS_TIME_FIELDS) >= 5)
    for field in _PROCESS_TIME_FIELDS:
        got = anchor_from_metadata({field: "2026-09-09T11:47:37"})
        check(f"одно лишь «{field}» якорем не становится", got is None, str(got))
    check("дата встречи — становится",
          anchor_from_metadata({"date": "2026-03-09",
                                "indexed_at": "2026-09-09"})
          == datetime.date(2026, 3, 9))
    check("meeting_date тоже",
          anchor_from_metadata({"meeting_date": "2026-03-09"})
          == datetime.date(2026, 3, 9))


# ── 3-5. заголовок фрагмента ─────────────────────────────────────────
def _header_for(metadata: dict) -> str:
    """Первая строка контекста, который уйдёт модели."""
    from backend.core.search.enhanced_search_orchestrator import (
        EnhancedSearchOrchestrator,
    )

    orch = object.__new__(EnhancedSearchOrchestrator)
    orch._answer_mode = None
    orch.verification_enabled = False

    class _Res:
        def __init__(self, md):
            self.doc_id, self.metadata, self.score = "frag_1", md, 0.9
            self.text = "Клиент просил ускорить модуль отчётности. " * 3
            self.sources = []

    class _Batch:
        def __init__(self, rs):
            self.results = rs

    out = asyncio.run(orch._assemble_context(
        _Batch([_Res(metadata)]), "когда просили?", {}))
    return (out.get("text") or "").splitlines()[0]


def test_header_carries_the_date():
    print("заголовок фрагмента:")
    head = _header_for({"type": "knowledge", "title": "Практика",
                        "date": "2026-03-09"})
    check("дата встречи попала в заголовок", "(2026-03-09)" in head, head)
    check("тип и название на месте", "[knowledge] Практика" in head, head)

    head = _header_for({"type": "knowledge", "title": "Практика"})
    check("даты нет — скобки с датой нет",
          "(20" not in head, head)

    # Ключевой анти-регресс: день заливки не должен выдавать себя за дату
    # источника. Раньше он становился якорем и был бы напечатан здесь.
    head = _header_for({"type": "knowledge", "title": "Практика",
                        "indexed_at": "2026-09-09T11:47:37"})
    check("только время заливки — в заголовке даты нет",
          "2026-09-09" not in head, head)

    head = _header_for({"type": "knowledge", "title": "Практика",
                        "date": "2023/05/30 (Tue) 23:40"})
    check("выгрузочный формат нормализуется в ISO",
          "(2023-05-30)" in head, head)


# ── 6. индексация ────────────────────────────────────────────────────
def test_indexing_puts_meeting_date_in_every_payload():
    print("индексация:")
    from backend.core.store.vector_indexer import VectorIndexer

    idx = object.__new__(VectorIndexer)
    idx.connected = True
    idx.use_qdrant = False
    idx.collections = {k: k for k in
                       ("meetings", "decisions", "tasks", "participants",
                        "entities", "kpis")}
    captured: list[dict] = []

    async def _fake_upsert(collection, id, text, payload):
        captured.append(payload)

    idx._upsert_vector = _fake_upsert
    idx._save_index_to_file = lambda: None
    idx._generate_id = lambda prefix, text: f"{prefix}_x"
    idx.namespace = None

    results = {
        "summary": {"participants_count": 1, "decisions_count": 1, "tasks_count": 1},
        "decisions": [{"decision_id": "d1", "decision_summary": "Ускорить отчётность"}],
        "tasks": [{"task_id": "t1", "title": "Собрать требования"}],
        "participants": [{"name": "Никита Фролов"}],
        "entities": [{"entity_id": "e1", "name": "Титан-Банк"}],
        "kpis": [{"kpi_id": "k1", "name": "срок", "value": {"current": 5}}],
    }
    asyncio.run(idx.index_meeting_results(
        "m1", results, {"title": "Звонок", "date": "2026-03-09"}, "public"))

    by_type = {p.get("type"): p for p in captured}
    check("проиндексированы все шесть видов фрагментов",
          len(by_type) == 6, str(sorted(by_type)))
    for t in ("meeting", "decision", "task", "participant", "entity", "kpi"):
        p = by_type.get(t, {})
        check(f"у фрагмента «{t}» есть дата встречи",
              p.get("date") == "2026-03-09", str(p.get("date")))
        check(f"у фрагмента «{t}» дата ≠ времени заливки",
              p.get("date") != p.get("indexed_at"))


# ── 7. граф ──────────────────────────────────────────────────────────
def test_create_node_stamps_source_date():
    print("create_node:")
    from backend.core.store.graph_builder import GraphBuilder

    path = os.path.join(tempfile.mkdtemp(), "graph.json")
    gb = GraphBuilder(use_networkx=True, graph_storage_path=path)
    asyncio.run(gb.connect())

    asyncio.run(gb.create_node(
        node_id="k1", label="Knowledge",
        properties={"name": "Практика", "content": "текст",
                    "_source_date": "2026-03-09"}))
    check("Knowledge получил _source_date из свойств",
          gb.nx_graph.nodes["k1"].get("_source_date") == "2026-03-09",
          str(gb.nx_graph.nodes["k1"].get("_source_date")))

    # Партийная дата (та, что ставит save_meeting_results) — паритет с MERGE.
    gb._source_date = "2026-04-01"
    asyncio.run(gb.create_node(
        node_id="c1", label="Chunk",
        properties={"chunk_id": "c1", "text": "фрагмент", "index": 0}))
    check("Chunk унаследовал дату партии",
          gb.nx_graph.nodes["c1"].get("_source_date") == "2026-04-01",
          str(gb.nx_graph.nodes["c1"].get("_source_date")))

    # Узел встречи создаётся ДО конвейера, когда партийная дата ещё от
    # предыдущей встречи. Собственная `date` узла обязана победить её —
    # иначе мартовская встреча получает апрельскую дату источника.
    gb._source_date = "2026-04-01"
    asyncio.run(gb.create_node(
        node_id="meeting_m9", label="Meeting",
        properties={"meeting_id": "m9", "title": "Звонок", "date": "2026-03-09"}))
    check("своя дата узла важнее устаревшей партийной",
          gb.nx_graph.nodes["meeting_m9"].get("_source_date") == "2026-03-09",
          str(gb.nx_graph.nodes["meeting_m9"].get("_source_date")))

    # «Нет даты» ≠ «дата — пустая строка»: пустое значение сравнивать нельзя,
    # а в графе оно выглядит как заполненное поле.
    gb._source_date = ""
    asyncio.run(gb.create_node(
        node_id="k2", label="Knowledge",
        properties={"name": "Без даты", "content": "текст", "_source_date": ""}))
    check("пустая дата не пишется в узел",
          "_source_date" not in gb.nx_graph.nodes["k2"],
          str(gb.nx_graph.nodes["k2"].get("_source_date")))


if __name__ == "__main__":
    test_export_date_formats()
    test_process_time_is_not_an_anchor()
    test_header_carries_the_date()
    test_indexing_puts_meeting_date_in_every_payload()
    test_create_node_stamps_source_date()
    print()
    if failures:
        print(f"ПРОВАЛЕНО {len(failures)}: " + "; ".join(failures))
        sys.exit(1)
    print("Все контракты датирования источника прошли.")
