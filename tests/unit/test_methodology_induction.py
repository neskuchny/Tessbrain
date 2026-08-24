# -*- coding: utf-8 -*-
"""МОРМ-lite: числовые правила считает код с гейтом на held-out против
базлайна; структурные правила гейтятся долей «держится» на отложенных
документах; < 4 документов — без гейта, статус «пилот»."""
from __future__ import annotations

import asyncio

from backend.core.analysis.methodology_induction import (
    build_methodology,
    collect_numeric_points,
    induce_numeric,
    parse_md_tables,
    render_methodology_md,
    _split,
)


def _doc(title, md):
    return {"id": title, "title": title, "content": md}


def _mediaplan_md(cpm_direct=300, budget=100000):
    clicks = budget // 25
    return f"""Медиаплан
| Канал | Бюджет | CPM | Клики |
|---|---|---|---|
| Яндекс.Директ | {budget} | {cpm_direct} | {clicks} |
| VK | {budget // 2} | 180 | {budget // 2 // 25} |
"""


def test_parse_md_tables():
    tables = parse_md_tables(_mediaplan_md())
    assert len(tables) == 1
    # имена колонок нормализуются (синонимы 1С, нижний регистр)
    assert tables[0][0]["канал"] == "Яндекс.Директ"
    assert tables[0][1]["cpm"] == "180"


def test_collect_numeric_points_normalizes_labels():
    docs = [_doc("a", _mediaplan_md()), _doc("b", _mediaplan_md(cpm_direct=310))]
    pts, units, notes = collect_numeric_points(docs)
    key = ("яндекс директ", "cpm")
    assert key in pts and len(pts[key]) == 2
    assert notes == []


def test_anchor_rule_passes_gate():
    # CPM Директа стабилен во всех 6 документах → якорь проходит гейт
    docs = [_doc(f"d{i}", _mediaplan_md(cpm_direct=300)) for i in range(6)]
    tr, hd = _split(len(docs))
    res = induce_numeric(docs, tr, hd)
    anchors = [r for r in res["rules"] if r["kind"] == "anchor"]
    assert any("cpm" in r["rule"] for r in anchors)
    a = next(r for r in anchors if "директ" in r["rule"])
    assert a["trust"] >= 0.9
    assert "held-out" in a["gate"]


def test_unstable_value_no_anchor():
    # CPM скачет в разы → якоря нет (и в discarded он не обязан быть:
    # разброс отсеивает его ещё до гейта)
    docs = [_doc(f"d{i}", _mediaplan_md(cpm_direct=100 * (i + 1)))
            for i in range(6)]
    tr, hd = _split(len(docs))
    res = induce_numeric(docs, tr, hd)
    assert not any(r["kind"] == "anchor" and "директ» / cpm" in r["rule"]
                   for r in res["rules"])


def test_ratio_rule_budget_clicks():
    # Клики = Бюджет/25 во всех строках → связка «бюджет ≈ 25 × клики»
    docs = [_doc(f"d{i}", _mediaplan_md(budget=50000 + i * 10000))
            for i in range(8)]
    tr, hd = _split(len(docs))
    res = induce_numeric(docs, tr, hd)
    ratios = [r for r in res["rules"] if r["kind"] == "ratio"]
    assert any("бюджет" in r["rule"] and "клики" in r["rule"] for r in ratios)


def test_split_small_corpus_no_heldout():
    tr, hd = _split(3)
    assert tr == [0, 1, 2] and hd == []
    tr, hd = _split(8)
    assert len(hd) == 2 and hd == [6, 7]


def test_render_md_pilot_warning():
    md = render_methodology_md(
        genre="посты", title="Т", n_docs=3, n_heldout=0,
        status="пилот гипотез (мало пар)", rules=[], discarded=[],
        skeleton=[], notes=["мало документов"])
    assert "ПИЛОТ ГИПОТЕЗ" in md
    assert "null — тоже данные" in md.lower() or "не прошло гейт" in md


class _FakeLLM:
    """Структурная ветка: экстракция → правила → гейт-матрица."""
    def __init__(self):
        self.calls = 0

    async def generate_json(self, prompt=None, **kw):
        self.calls += 1
        if "извлеки" in prompt.lower() or "structure" in prompt.lower():
            n = prompt.count("### DOC")
            return [{"doc": i,
                     "sections": ["хук", "боль", "оффер", "cta"],
                     "format": "лонгрид",
                     "style": {"words": 300, "cta_count": 1},
                     "moves": ["вопрос в первой строке"]} for i in range(n)]
        if "сформулируй" in prompt.lower():
            return [
                {"rule": "Пост всегда открывается хуком-вопросом",
                 "frame": "продающие посты; не для новостей",
                 "check": "Начинается ли документ с хука-вопроса?"},
                {"rule": "CTA ровно один, в конце",
                 "frame": "все посты",
                 "check": "Есть ли ровно один CTA в конце?"},
            ]
        # гейт-матрица: правило 1 держится везде, правило 2 — нигде
        n_docs = prompt.count("### DOC")
        return {"matrix": [[True, False] for _ in range(n_docs)]}


def test_build_methodology_end_to_end_structural_gate():
    docs = {f"p{i}": _doc(f"p{i}", f"Пост {i}: Вы тоже теряете лиды? "
                                   "Боль. Оффер. Пишите в личку!")
            for i in range(12)}

    async def loader(doc_id):
        return docs.get(doc_id)

    saved = {}

    async def fake_save(doc, user_id):
        saved["doc"] = doc

    import backend.core.documents.doc_store as ds
    orig = ds.save_document
    ds.save_document = fake_save
    try:
        res = asyncio.run(build_methodology(
            "u1", document_ids=list(docs.keys()), genre="посты",
            llm=_FakeLLM(), doc_loader=loader))
    finally:
        ds.save_document = orig

    assert res["success"] is True
    assert res["status"].startswith("методология")
    rules = [r["rule"] for r in res["rules"]]
    # правило 1 прошло гейт, правило 2 (0% held-out) — выброшено
    assert any("хуком-вопросом" in r for r in rules)
    assert not any("CTA ровно один" in r for r in rules)
    assert any("CTA ровно один" in d for d in res["discarded"])
    # скелет собран из структур
    assert "хук" in res["skeleton"]
    # сохранено в папку «Методологии»
    assert getattr(saved["doc"], "folder", "") == "Методологии"
    assert saved["doc"].document_type == "methodology"
    # честность в тексте
    assert "гейт" in res["markdown"].lower()


def test_build_methodology_pilot_status_few_docs():
    docs = {f"p{i}": _doc(f"p{i}", "Пост. Вопрос? Оффер. CTA.")
            for i in range(3)}

    async def loader(doc_id):
        return docs.get(doc_id)

    async def fake_save(doc, user_id):
        pass

    import backend.core.documents.doc_store as ds
    orig = ds.save_document
    ds.save_document = fake_save
    try:
        res = asyncio.run(build_methodology(
            "u1", document_ids=list(docs.keys()), genre="посты",
            llm=_FakeLLM(), doc_loader=loader))
    finally:
        ds.save_document = orig
    assert res["success"] is True
    assert res["status"].startswith("пилот")
    assert "ПИЛОТ" in res["markdown"]


def test_build_methodology_too_few_docs():
    async def loader(doc_id):
        return None
    res = asyncio.run(build_methodology(
        "u1", document_ids=["x", "y"], genre="посты",
        llm=_FakeLLM(), doc_loader=loader))
    assert res["success"] is False


# ── 1С-выгрузки, единицы измерения, пере-гейт ───────────────────────────

def _onec_md(price_t=45000, qty_t=2.5):
    """Типовая 1С-выгрузка: мусорная шапка отчёта, синонимы колонок,
    единицы, строка «Итого»."""
    total = price_t * qty_t
    return f"""--- Sheet: TDSheet ---
| Отчёт по продажам |  |  |  |  |
|---|---|---|---|---|
| Период: январь 2026 |  |  |  |  |
| Наименование | Кол-во | Ед. изм. | Цена | Сумма |
| Лист стальной 3мм | {qty_t} | т | {price_t} | {total} |
| Крепёж комплект | 100 | шт | 250 | 25000 |
| Итого |  |  |  | {total + 25000} |
"""


def test_onec_header_detected_and_totals_dropped():
    from backend.core.analysis.methodology_induction import (
        _pipe_blocks, normalize_table)
    blocks = _pipe_blocks(_onec_md())
    assert len(blocks) == 1
    rows = normalize_table(blocks[0])
    # мусорные строки отчёта выше шапки и «Итого» выброшены
    assert len(rows) == 2
    assert rows[0]["номенклатура"] == "Лист стальной 3мм"
    assert rows[0]["количество"] == "2.5"
    assert rows[0]["ед"] == "т"
    assert rows[1]["цена"] == "250"


def test_units_price_normalized_to_base():
    # Один документ с ценой за тонну, другой — за кг: после нормализации
    # (₽/т ÷1000 → ₽/кг) якорь сходится
    doc_t = _doc("a", """| Наименование | Кол-во | Ед. изм. | Цена |
|---|---|---|---|
| Лист стальной | 2 | т | 45000 |
""")
    doc_kg = _doc("b", """| Наименование | Кол-во | Ед. изм. | Цена |
|---|---|---|---|
| Лист стальной | 500 | кг | 45 |
""")
    pts, units, notes = collect_numeric_points([doc_t, doc_kg])
    key = ("лист стальной", "цена")
    vals = sorted(v for _, v in pts[key])
    assert vals == [45.0, 45.0]          # обе цены в ₽/кг
    assert units[key] == "кг"
    # количество приведено к кг: 2 т → 2000
    qty = sorted(v for _, v in pts[("лист стальной", "количество")])
    assert qty == [500.0, 2000.0]


def test_units_incompatible_mix_is_dropped_with_note():
    doc1 = _doc("a", """| Наименование | Кол-во | Ед. изм. | Цена |
|---|---|---|---|
| Профиль | 10 | шт | 1200 |
""")
    doc2 = _doc("b", """| Наименование | Кол-во | Ед. изм. | Цена |
|---|---|---|---|
| Профиль | 50 | кг | 80 |
""")
    pts, units, notes = collect_numeric_points([doc1, doc2])
    assert ("профиль", "цена") not in pts     # не гадаем
    assert any("смешаны" in n for n in notes)


def test_header_scale_thousands():
    doc = _doc("a", """| Наименование | Сумма, тыс. руб. |
|---|---|
| Фундамент | 1 250 |
""")
    pts, _, _ = collect_numeric_points([doc])
    key = ("фундамент", "сумма тыс руб")
    assert pts[key][0][1] == 1_250_000.0


def test_regate_numeric_drift(monkeypatch):
    """Якорь «цена ≈ 300» дрейфует: свежие документы дают 390 → 🔴 +
    предложение нового значения; держащееся правило остаётся ✅."""
    import backend.core.documents.doc_store as ds
    from backend.core.analysis.methodology_induction import regate_methodology

    stored_row = {
        "document_id": "m1", "title": "Методология: сметы",
        "topic": "сметы", "version": "1.0", "summary": "s",
        "content_markdown": "# М", "keywords": [], "source_meetings": [],
        "status": "draft", "folder": "Методологии",
        "sections": [{"kind": "morm_rules", "genre": "сметы", "rules": [
            {"kind": "anchor", "rule": "«профиль» / цена ≈ 300",
             "frame": "", "origin": "", "check": None, "trust": 0.95,
             "target": {"type": "anchor", "label": "профиль",
                        "col": "цена", "value": 300, "unit": ""}},
            {"kind": "anchor", "rule": "«крепёж» / цена ≈ 250",
             "frame": "", "origin": "", "check": None, "trust": 0.95,
             "target": {"type": "anchor", "label": "крепёж",
                        "col": "цена", "value": 250, "unit": ""}},
        ]}],
    }
    saved = {}

    async def fake_get(document_id, user_id):
        return stored_row

    async def fake_save(doc, user_id):
        saved["doc"] = doc

    monkeypatch.setattr(ds, "get_document", fake_get)
    monkeypatch.setattr(ds, "save_document", fake_save)

    fresh = {f"f{i}": _doc(f"f{i}", """| Наименование | Цена |
|---|---|
| Профиль | 450 |
| Крепёж | 252 |
""") for i in range(3)}

    async def loader(doc_id):
        return fresh.get(doc_id)

    res = asyncio.run(regate_methodology(
        "u1", methodology_document_id="m1",
        document_ids=list(fresh.keys()), llm=_FakeLLM(),
        doc_loader=loader))
    assert res["success"] is True
    assert res["drifted"] == 1
    v_drift = next(v for v in res["verdicts"] if v["verdict"] == "дрейф")
    assert "профиль" in v_drift["rule"] and "450" in v_drift["detail"]
    v_ok = next(v for v in res["verdicts"] if v["verdict"] == "держится")
    assert "крепёж" in v_ok["rule"]
    # документ обновлён: версия выросла, дрейф в markdown, правило помечено
    doc = saved["doc"]
    assert doc.version == "1.1"
    assert "Пере-гейт" in doc.content_markdown and "🔴" in doc.content_markdown
    rules = doc.sections[0]["rules"]
    assert next(r for r in rules if "профиль" in r["rule"])["drift"] is True
    assert next(r for r in rules if "крепёж" in r["rule"])["drift"] is False


def test_regate_requires_machine_rules(monkeypatch):
    import backend.core.documents.doc_store as ds
    from backend.core.analysis.methodology_induction import regate_methodology

    async def fake_get(document_id, user_id):
        return {"document_id": "m1", "sections": []}
    monkeypatch.setattr(ds, "get_document", fake_get)

    async def loader(doc_id):
        return None
    res = asyncio.run(regate_methodology(
        "u1", methodology_document_id="m1", document_ids=["x"],
        llm=_FakeLLM(), doc_loader=loader))
    assert res["success"] is False and "пересоберите" in res["error"]


def test_excel_parser_keeps_column_positions():
    """None-ячейки больше не сдвигают колонки, лист — валидная md-таблица."""
    import pytest
    openpyxl = pytest.importorskip("openpyxl")
    import io
    from backend.core.documents.file_parser import FileParser
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Наименование", "Кол-во", "Цена", "Сумма"])
    ws.append(["Лист стальной", None, 45000, 90000])   # дырка в Кол-во
    buf = io.BytesIO()
    wb.save(buf)
    text, kind = FileParser.parse_file(buf.getvalue(), "смета.xlsx")
    assert kind == "excel"
    # цена осталась в СВОЕЙ колонке, дырка — пустой ячейкой
    assert "| Лист стальной |  | 45000 | 90000 |" in text
    # и это валидный вход для индукции
    from backend.core.analysis.methodology_induction import parse_md_tables
    tables = parse_md_tables(text)
    assert tables and tables[0][0]["цена"] == "45000"
