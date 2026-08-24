# -*- coding: utf-8 -*-
"""Порт харнесса gbrain-evals: контракты честности сравнения.

Мы прогнали наш поисковый стек на чужой линейке (BrainBench из
gbrain-evals) — единственный честный способ сравнить системы вместо
сопоставления несравнимых метрик. Лесенка на их корпусе world-v1
(240 страниц, 145 реляционных запросов):

    голый BM25                       0.161 / 0.597
    без реляционного детектора       0.240 / 0.806
    дефолты прода (детектор on)      0.310 / 0.893
    плюс CONFIDENT_GRAPH=on (флаг)   0.729 / 0.992
    gbrain, их публикация            0.491 / 0.979

История в три акта: сначала 0.729 держалось на правилах, захардкоженных
в скрипте бенчмарка (снято как подгонка); затем честные 0.240 продом как
есть; затем те же идеи вернулись КАК ФИЧИ ПРОДУКТА — реляционный детектор
(enumerative_detect, RELATIONAL_DETECT), тип ответа (answer_type),
уверенный структурный ответ (confident_graph, в проде default OFF).
Главный контракт этого файла теперь: правила живут в продовых модулях
с выключателями, а скрипт бенчмарка их только вызывает.

Контракты:
  1. порт формул точен: precision делит на длину ВОЗВРАЩЁННОГО списка,
     recall — на размер gold; пустое → 0 (их семантика);
  2. build_queries строит все 4 типа реляционных вопросов и фильтрует
     несуществующие слаги (их filter);
  3. адаптер структурно не видит gold: sanitize_page вычищает _facts,
     в query уходит только {id, text};
  4. рёбра графа извлекаются из ссылок детерминированно — в скрипте нет
     ни одного вызова модели;
  5. правил ранжирования в скрипте нет: канал графа не фильтрует, дефолт
     прода не обрезает выдачу, сужение работает только под флагом и
     только через продовые модули; RELATIONAL_DETECT=off возвращает
     прежний детектор;
  6. адаптер детерминирован: соседи подаются в RRF отсортированными,
     иначе ранг зависит от рандомизации хешей;
  7. оговорки напечатаны в выводе: порт (не их раннер), нет вектора,
     корпус — валидация правил, дефолт CONFIDENT_GRAPH выключен.
"""
from __future__ import annotations

import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

spec = importlib.util.spec_from_file_location(
    "bb", os.path.join(ROOT, "scripts/brainbench_run.py"))
bb = importlib.util.module_from_spec(spec)
sys.modules["bb"] = bb
spec.loader.exec_module(bb)


def _mini_pages():
    return [
        {"slug": "meetings/kickoff", "type": "meeting", "title": "Kickoff",
         "compiled_truth": "Kickoff with [Ann Lee](people/ann-lee) and "
                           "[Bob Wu](people/bob-wu).",
         "timeline": "",
         "_facts": {"type": "meeting",
                    "attendees": ["people/ann-lee", "people/bob-wu",
                                  "people/ghost-not-in-corpus"]}},
        {"slug": "companies/acme", "type": "company", "title": "Acme",
         "compiled_truth": "Acme was founded by [Ann Lee](people/ann-lee).",
         "timeline": "",
         "_facts": {"type": "company", "founders": ["people/ann-lee"],
                    "employees": [], "investors": [], "advisors": []}},
        {"slug": "people/ann-lee", "type": "person", "title": "Ann Lee",
         "compiled_truth": "Ann Lee works at [Acme](companies/acme).",
         "timeline": "", "_facts": {"type": "person"}},
        {"slug": "people/bob-wu", "type": "person", "title": "Bob Wu",
         "compiled_truth": "Bob Wu attended [Kickoff](meetings/kickoff).",
         "timeline": "", "_facts": {"type": "person"}},
    ]


# ── 1. Формулы ──────────────────────────────────────────────────────────

def test_metric_formulas_match_theirs():
    rel = {"a", "b"}
    # precision делит на длину возвращённого (3), не на k
    assert abs(bb.precision_at_k(["a", "x", "b"], rel, 5) - 2 / 3) < 1e-9
    assert bb.precision_at_k([], rel, 5) == 0.0
    # recall делит на размер gold
    assert abs(bb.recall_at_k(["a", "x"], rel, 5) - 0.5) < 1e-9
    assert bb.recall_at_k(["a"], set(), 5) == 0.0
    # короткий точный список даёт precision 1.0 — из-за этого метрика
    # щедра к тому, кто обрезает выдачу. Именно на этом свойстве и держался
    # снятый подогнанный вариант; знать его надо, опираться на него — нет.
    assert bb.precision_at_k(["a", "b"], rel, 5) == 1.0
    print("✅ формулы порта соответствуют их семантике")


# ── 2. Запросы ──────────────────────────────────────────────────────────

def test_build_queries_all_four_kinds_and_filters_ghosts():
    qs = bb.build_queries(_mini_pages())
    texts = [q["text"] for q in qs]
    assert "Who attended Kickoff?" in texts
    assert "Who works at Acme?" in texts
    att = next(q for q in qs if q["text"] == "Who attended Kickoff?")
    assert "people/ghost-not-in-corpus" not in att["gold"], (
        "несуществующий слаг обязан быть отфильтрован — как их filter()"
    )
    assert set(att["gold"]) == {"people/ann-lee", "people/bob-wu"}
    # investors/advisors пустые → вопросов нет (их «if empty continue»)
    assert not any("invested" in t or "advises" in t for t in texts)
    print("✅ запросы строятся как у них, призраки отфильтрованы")


# ── 3. Адаптер не видит gold ────────────────────────────────────────────

def test_sanitize_strips_gold_fields():
    p = bb.sanitize_page(_mini_pages()[0])
    assert "_facts" not in p and "frontmatter" not in p
    assert set(p) == {"slug", "type", "title", "compiled_truth", "timeline"}
    src = open(os.path.join(ROOT, "scripts/brainbench_run.py"),
               encoding="utf-8").read()
    run_fn = src[src.index("def run("):src.index("def main(")]
    assert "sanitize_page" in run_fn, "в адаптеры уходят только public-страницы"
    assert '{"id": q["id"], "text": q["text"]}' in run_fn, (
        "в query уходит только id+text — gold остаётся у скорера"
    )
    print("✅ адаптер структурно не видит gold")


# ── 4. Рёбра без модели ─────────────────────────────────────────────────

def test_graph_edges_deterministic_no_llm():
    a = bb.TessentAdapter()
    a.init([bb.sanitize_page(p) for p in _mini_pages()])
    assert "people/ann-lee" in a.edges.get("meetings/kickoff", set())
    assert "meetings/kickoff" in a.edges.get("people/ann-lee", set()), (
        "рёбра ненаправленные — упоминание связывает обе страницы"
    )
    src = open(os.path.join(ROOT, "scripts/brainbench_run.py"),
               encoding="utf-8").read()
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.strip().startswith("#"))
    for bad in ("import openai", "llm_router", "LLMRouter", ".generate(",
                "chat/completions"):
        assert bad not in code, f"в порте не должно быть вызова модели: {bad}"
    print("✅ рёбра из ссылок, детерминированно, ни одного вызова модели")


# ── 5. Правила ранжирования живут в продукте, не в скрипте ──────────────

def test_ranking_rules_live_in_product_modules_not_in_script():
    import os as _os

    # (а) графовый КАНАЛ не отсекает не-людей никогда: сужение — отдельный
    # шаг (confident_graph) с собственными условиями. У Ann Lee сосед —
    # компания, и в канале она обязана остаться.
    a = bb.TessentAdapter()
    a.init([bb.sanitize_page(p) for p in _mini_pages()])
    hits = {h["id"] for h in a._graph_channel(
        {"id": "q", "text": "Who works at Ann Lee?"})}
    assert "companies/acme" in hits, (
        f"фильтр по типу пролез в канал графа: {hits}")

    # (б) дефолты прода: CONFIDENT_GRAPH выключен → выдача НЕ обрезается.
    prod = bb.ProdDefaultAdapter()
    prod.init([bb.sanitize_page(p) for p in _mini_pages()])
    ranked = prod.query({"id": "q1", "text": "Who attended Kickoff?"})
    assert set(ranked) - {"people/ann-lee", "people/bob-wu"}, (
        "с выключенным флагом выдача обрезана — дефолт перестал быть безопасным")

    # (в) с флагом: уверенный структурный ответ — ровно участники, без шума.
    conf = bb.ConfidentOnAdapter()
    conf.init([bb.sanitize_page(p) for p in _mini_pages()])
    ranked_c = conf.query({"id": "q2", "text": "Who attended Kickoff?"})
    assert set(ranked_c) == {"people/ann-lee", "people/bob-wu"}, (
        f"уверенный ответ должен быть списком участников, а не {ranked_c}")

    # (г) реляционный детектор — фича продукта с выключателем, а не правило
    # скрипта: on → ловит голое «Who attended», off → прежний детектор.
    det = bb._load_module("backend.core.search.enumerative_detect",
                          "backend/core/search/enumerative_detect.py")
    _os.environ["RELATIONAL_DETECT"] = "on"
    assert det.detect("Who attended Kickoff?"), "реляционный паттерн не сработал"
    _os.environ["RELATIONAL_DETECT"] = "off"
    assert not det.detect("Who attended Kickoff?"), (
        "выключатель RELATIONAL_DETECT=off не возвращает прежний детектор")
    _os.environ["RELATIONAL_DETECT"] = "on"

    # (д) в самом скрипте нет ни одного собственного правила по форме вопроса.
    src = open(os.path.join(ROOT, "scripts/brainbench_run.py"),
               encoding="utf-8").read()
    assert 'startswith(("who ' not in src, (
        "в скрипте снова появилось правило под форму вопроса")
    print("✅ правила ранжирования — в продовых модулях, скрипт их только зовёт")


# ── 6. Детерминизм ──────────────────────────────────────────────────────

def test_graph_channel_is_deterministically_ordered():
    a = bb.TessentAdapter()
    a.init([bb.sanitize_page(p) for p in _mini_pages()])
    q = {"id": "q", "text": "Who attended Kickoff?"}
    ids = [h["id"] for h in a._graph_channel(q)]
    assert ids == sorted(ids), (
        "соседи должны подаваться в RRF отсортированными: ранг в "
        "rrf_fusion.fuse берётся из порядка входа, а обход множества "
        "зависит от рандомизации хешей — без сортировки прогон плавал"
    )
    assert a.query(q) == a.query(q), "повторный запрос обязан совпасть"
    print("✅ порядок графового канала детерминирован")


# ── 7. Оговорки в выводе ────────────────────────────────────────────────

def test_disclaimers_printed():
    src = open(os.path.join(ROOT, "scripts/brainbench_run.py"),
               encoding="utf-8").read()
    assert "не их раннер" in src
    assert "векторного канала в прогоне нет" in src
    assert "валидация" in src, (
        "оговорка «корпус — валидация, а не источник правил» обязана печататься")
    assert "по умолчанию ВЫКЛЮЧЕН" in src, (
        "оговорка о дефолте CONFIDENT_GRAPH обязана печататься")
    assert "0.491" in src, "их опубликованное число печатается для сверки"
    print("✅ оговорки — в выводе, а не в уме")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе контракты порта BrainBench прошли.")
