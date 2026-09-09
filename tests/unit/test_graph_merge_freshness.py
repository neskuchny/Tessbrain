# -*- coding: utf-8 -*-
"""MERGE узла: старая встреча не затирает значение, записанное свежей.

Дефект, который эти тесты закрепляют
(docs/ru/REVIEW_TZ_FIX_SEARCH_DEFECTS.md, задача 1):
`_merge_node_networkx` делал слепой `nx_graph.nodes[id].update(properties)`.
Побеждал не более свежий источник, а последний по ПОРЯДКУ ОБРАБОТКИ.

Почему это не теория. Встречи заливаются в порядке поступления, и он не
обязан совпадать с хронологией: в LongMemEval у 20 историй из 60 сессии
идут не по возрастанию даты, а на категории knowledge-update — у всех
десяти. Там же система уверенно отдавала устаревшее значение («Chicago»
вместо «the suburbs», «$350k» вместо «$400k»).

Отдельно: `updated_at` в свойствах сущностей для этого непригоден — он
ставится как `datetime.now()` в момент обработки, а не как дата встречи,
и при пакетной заливке одинаков у всех сессий.

Контракты:
  1. КОНТРОЛЬ: при подаче в ХРОНОЛОГИЧЕСКОМ порядке побеждает свежее
     значение — так вело себя и старое поведение, эта часть не сломана;
  2. при подаче в ОБРАТНОМ порядке свежее значение тоже побеждает —
     ровно то, что было сломано;
  3. пустые поля свежая встреча дозаполняет, а не блокирует;
  4. без дат поведение прежнее (никакой блокировки);
  5. `_source_date` узла — самая поздняя из виденных.

Запуск: python tests/unit/test_graph_merge_freshness.py
"""
from __future__ import annotations

import asyncio
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

from backend.core.store.graph_builder import GraphBuilder  # noqa: E402


def _fresh_graph() -> GraphBuilder:
    path = os.path.join(tempfile.mkdtemp(), "graph.json")
    gb = GraphBuilder(use_networkx=True, graph_storage_path=path)
    asyncio.run(gb.connect())
    return gb


def _norm(gb: GraphBuilder, date: str) -> str:
    """Нормализация даты, если продукт её умеет.

    На коде ДО починки метода `_normalize_source_date` нет. Тогда дата
    кладётся как есть — и набор падает на СОДЕРЖАТЕЛЬНОМ утверждении
    («старая встреча затёрла свежее значение»), а не на AttributeError.
    Это и делает контроль настоящим: он показывает дефект, а не отсутствие
    нового метода.
    """
    fn = getattr(gb, "_normalize_source_date", None)
    return fn(date) if callable(fn) else (date or "")


def _merge(gb: GraphBuilder, *, name: str, city: str, date: str,
           description: str = "") -> str:
    """Одна «встреча»: сообщает, где живёт человек, датой date."""
    gb._source_date = _norm(gb, date)
    props = {"entity_id": name, "name": name, "entity_type": "person",
             "city": city, "description": description,
             "importance": "medium", "confidence": 0.8, "attributes": "{}",
             "access_group": "public"}
    return gb._merge_node_networkx("Entity", "name", name, props,
                                   additional_match={"entity_type": "person"})


def _city(gb: GraphBuilder, node_id: str) -> str:
    return gb.nx_graph.nodes[node_id].get("city", "")


# ── 1. Контроль: хронологический порядок и раньше работал ──────────────

def test_control_chronological_order_keeps_newest():
    gb = _fresh_graph()
    nid = _merge(gb, name="Рэйчел", city="Chicago", date="2023-01-10")
    _merge(gb, name="Рэйчел", city="the suburbs", date="2023-06-20")
    assert _city(gb, nid) == "the suburbs", (
        f"даже в хронологическом порядке свежее не победило: {_city(gb, nid)}")
    print("✅ контроль: в хронологическом порядке побеждает свежее значение")


# ── 2. Тот же дефект: обратный порядок подачи ─────────────────────────

def test_older_meeting_does_not_overwrite_newer():
    gb = _fresh_graph()
    # свежая встреча обработана ПЕРВОЙ, старая — второй
    nid = _merge(gb, name="Рэйчел", city="the suburbs", date="2023-06-20")
    _merge(gb, name="Рэйчел", city="Chicago", date="2023-01-10")
    assert _city(gb, nid) == "the suburbs", (
        "старая встреча затёрла свежее значение — это и есть дефект: "
        f"получено {_city(gb, nid)!r}")
    print("✅ старая встреча не затирает свежее значение при обратном порядке")


# ── 3. Пустое поле старая встреча дозаполняет ─────────────────────────

def test_older_meeting_still_fills_empty_fields():
    gb = _fresh_graph()
    nid = _merge(gb, name="Рэйчел", city="the suburbs", date="2023-06-20",
                 description="")
    _merge(gb, name="Рэйчел", city="Chicago", date="2023-01-10",
           description="работает в маркетинге")
    assert _city(gb, nid) == "the suburbs", "свежий город затёрт"
    assert gb.nx_graph.nodes[nid].get("description") == "работает в маркетинге", (
        "старая встреча не дозаполнила ПУСТОЕ поле — защита слишком широкая")
    print("✅ пустое поле дозаполняется и старым источником")


# ── 4. Без дат поведение прежнее ──────────────────────────────────────

def test_without_dates_behaviour_unchanged():
    gb = _fresh_graph()
    nid = _merge(gb, name="Рэйчел", city="Chicago", date="")
    _merge(gb, name="Рэйчел", city="the suburbs", date="")
    assert _city(gb, nid) == "the suburbs", (
        "без дат должно работать прежнее «последний выиграл», "
        f"получено {_city(gb, nid)!r}")
    print("✅ без дат поведение не изменилось")


# ── 5. _source_date — самая поздняя из виденных ───────────────────────

def test_source_date_keeps_the_latest():
    gb = _fresh_graph()
    nid = _merge(gb, name="Рэйчел", city="the suburbs", date="2023-06-20")
    _merge(gb, name="Рэйчел", city="Chicago", date="2023-01-10")
    assert gb.nx_graph.nodes[nid].get("_source_date") == "2023-06-20", (
        f"_source_date откатился назад: {gb.nx_graph.nodes[nid].get('_source_date')}")
    assert gb.nx_graph.nodes[nid].get("_updated_at"), "_updated_at не проставлен"
    print("✅ _source_date держит самую позднюю дату, _updated_at проставлен")


# ── 6. Разбор дат разных форматов ─────────────────────────────────────

def test_date_normalisation_formats():
    gb = _fresh_graph()
    assert hasattr(gb, "_normalize_source_date"), (
        "нет _normalize_source_date — сравнивать свежесть нечем")
    assert gb._normalize_source_date("2023/05/30 (Tue) 23:40") == "2023-05-30"
    assert gb._normalize_source_date("2023-05-30T10:00:00Z") == "2023-05-30"
    assert gb._normalize_source_date("вчера") == "", (
        "неразобранная дата обязана давать пустоту, иначе она блокирует "
        "запись по мусорному сравнению")
    assert gb._normalize_source_date(None) == ""
    print("✅ даты разных форматов приводятся к сравнимому виду")


# ── 7. Симметрия бэкендов: та же защита в Cypher ──────────────────────

def test_neo4j_branch_has_the_same_guard():
    """Проверяется ФОРМА запроса, а не поведение.

    Поведение Neo4j-ветки без поднятого инстанса проверить нечем, и
    выдавать форму за поведение нельзя. Но асимметрия бэкендов — сама по
    себе дефект: один конвейер отдавал бы на Neo4j устаревший факт, а на
    NetworkX свежий. Этот тест ловит именно расхождение: если guard из
    Cypher уберут, а из NetworkX нет (или наоборот), набор упадёт.

    ЧТО ОСТАЁТСЯ НЕПРОВЕРЕННЫМ: что Cypher исполняется без ошибки и что
    сравнение дат в нём отрабатывает как задумано. Это задача прогона на
    живом Neo4j — см. REVIEW_TZ_FIX_SEARCH_DEFECTS.md, остаток 3.
    """
    src = open(os.path.join(ROOT, "backend", "core", "store",
                            "graph_builder.py"), encoding="utf-8").read()
    i = src.find("def _merge_node_neo4j")
    assert i > 0, "не найден _merge_node_neo4j"
    blk = src[i:i + 3500]

    assert "_normalize_source_date" in blk, (
        "Neo4j-ветка не считает дату источника — бэкенды разъехались")
    assert '"src": src' in blk, "параметр src не передан в запрос"
    assert "$src < n._source_date" in blk, (
        "нет ветки, защищающей свежее значение от старого источника")
    assert "n._source_date = $src" in blk, (
        "_source_date не проставляется при создании узла")
    # обе половины CASE закрыты
    tpl = blk[blk.find("props_set_parts.append("):blk.find("on_match_set = ")]
    assert tpl.count("CASE") == tpl.count("END"), (
        f"несбалансированный CASE/END в Cypher: {tpl.count('CASE')}/{tpl.count('END')}")
    print("✅ Neo4j-ветка несёт ту же защиту (проверена форма, не поведение)")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе контракты свежести MERGE прошли.")
