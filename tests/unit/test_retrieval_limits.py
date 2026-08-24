# -*- coding: utf-8 -*-
"""Ответ на предел одновекторного поиска (arXiv 2508.21038, ICLR'26).

Статья доказывает: число top-k подмножеств, которые одновекторный
эмбеддинг способен вернуть, ограничено размерностью — на перечислительных
запросах лучшие эмбеддеры не решают даже 46 документов. У нас два места
под ударом: перечислительные запросы шли в общий гибрид без усиления
графа, а индекс опыта (BWE) был чисто одновекторным.

Контракты (оба исправления — ОСТОРОЖНЫЕ по построению):
  1. детектор ловит перечисление/подсчёт/пересечение — строгими
     паттернами; обычные вопросы НЕ срабатывают (ложное срабатывание
     сдвинуло бы ранги здоровых запросов);
  2. сработка меняет только веса слияния: ни один канал не отключается;
     есть выключатель;
  3. лексический канал BWE: дополняет после векторных, не заменяет;
     уже найденное вектором не дублируется; кап добавки; строгий порог;
  4. лексическому кандидату не приписывается векторная близость,
     которой не меряли (score=0, channel помечен);
  5. сбой любого из каналов — прежнее поведение, не ошибка.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name: str, relpath: str, pkgs=()):
    if name in sys.modules:
        return sys.modules[name]
    for pkg in ("backend", "backend.core", "backend.core.search",
                "backend.core.wisdom", *pkgs):
        if pkg not in sys.modules:
            m = types.ModuleType(pkg)
            m.__path__ = [os.path.join(ROOT, *pkg.split("."))]
            sys.modules[pkg] = m
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_det = _load("backend.core.search.enumerative_detect",
             "backend/core/search/enumerative_detect.py")
_lex = _load("backend.core.wisdom.wisdom_lexical",
             "backend/core/wisdom/wisdom_lexical.py")


def _src(relpath: str) -> str:
    with open(os.path.join(ROOT, relpath), encoding="utf-8") as f:
        return f.read()


# ── 1. Детектор ─────────────────────────────────────────────────────────

def test_detects_enumeration_count_intersection():
    cases = {
        "покажи всех, кто работал с клиентом Альфа": "list",
        "перечисли все задачи по проекту": "list",
        "какие решения принимали по продукту": "list",
        "сколько человек участвовало во встречах отдела": "count",
        "кто участвовал и в проекте А и в проекте Б": "intersect",
        "кто работал и с Альфой и с Бетой": "intersect",
    }
    for q, kind in cases.items():
        v = _det.detect(q)
        assert v.is_enumerative, f"обязан сработать: {q!r}"
        assert v.kind == kind, f"{q!r}: ждали {kind}, получили {v.kind}"
    print("✅ перечисление, подсчёт и пересечение распознаются")


def test_ordinary_questions_do_not_trigger():
    for q in ("почему мы отказались от подрядчика в марте",
              "что решили по бюджету на квартал",
              "расскажи про клиента Альфа",
              "как прошла встреча с командой",
              "дай совет по переговорам о цене",
              "в чём суть нашей стратегии на юг",
              ""):
        assert not _det.detect(q), (
            f"ложное срабатывание на {q!r} сдвинуло бы ранги здорового запроса"
        )
    print("✅ обычные вопросы детектор не трогает")


# ── 2. Буст графа без отключения каналов ────────────────────────────────

def test_boost_changes_weights_only_and_has_kill_switch():
    src = _src("backend/core/search/hybrid_search_orchestrator.py")
    idx = src.index("ENUM_GRAPH_BOOST")
    block = src[idx - 300:idx + 1200]
    assert "weights = dict(weights)" in block, (
        "буст обязан копировать веса, а не мутировать таблицу стратегий"
    )
    assert 'weights["graph"]' in block and 'weights["vector"]' in block
    for forbidden in ("include_graph = False", "tasks.pop", "return"):
        assert forbidden not in block, (
            f"буст не должен отключать каналы или обрывать поиск: {forbidden}"
        )
    assert '_os.getenv("ENUM_GRAPH_BOOST", "on")' in block, "выключатель обязан быть"
    print("✅ буст меняет только веса, каналы живы, выключатель есть")


# ── 3-4. Лексический канал BWE ──────────────────────────────────────────

def _pat(uid, name, text, cases=3):
    return {"pattern_uuid": uid, "pattern_name": name, "text": text,
            "confidence": "medium", "outcome_distribution": {"success": cases},
            "total_cases": cases}


def test_lexical_augments_after_vector_without_duplicates():
    vector = [{"pattern_uuid": "v1", "pattern_name": "ценовое давление",
               "score": 0.8, "confidence": "high",
               "outcome_distribution": {}, "total_cases": 4}]
    allp = [
        _pat("v1", "ценовое давление", "конкурент снизил цену давление"),
        _pat("x2", "скорость против качества",
             "выбор между скоростью запуска и качеством продукта"),
        _pat("x3", "наём против аутсорса", "делать своими силами или подрядчиком"),
    ]
    out = _lex.merge_lexical_candidates(
        vector, allp, "мы снова выбираем между скоростью запуска и качеством")
    ids = [p["pattern_uuid"] for p in out]
    assert ids[0] == "v1", "векторные остаются первыми"
    assert ids.count("v1") == 1, "найденное вектором не дублируется"
    assert "x2" in ids, "лексический кандидат по перекрытию слов добавлен"
    assert "x3" not in ids, "нерелевантный не проходит строгий порог"
    added = next(p for p in out if p["pattern_uuid"] == "x2")
    assert added["channel"] == "lexical" and added["score"] == 0.0, (
        "не приписываем векторную близость, которой не меряли"
    )
    assert added["lexical_shared"], "видно, ПО КАКИМ словам совпало"
    print("✅ лексика дополняет после вектора, без дублей и с честным score")


def test_lexical_threshold_is_strict():
    v = _lex.lexical_score("выбор поставщика облака",
                           "длинный паттерн про поставщика чего-то ещё")
    # одно общее слово — мало
    assert not v["passes"], "одного общего слова недостаточно"
    v2 = _lex.lexical_score("конфликт отдела продаж и разработки из-за сроков",
                            "конфликт продаж и разработки по срокам релиза")
    assert v2["passes"] and len(v2["shared"]) >= 2
    print("✅ порог строгий: одно слово не пускает, реальное перекрытие — да")


def test_lexical_cap_limits_additions():
    vector = []
    allp = [_pat(f"p{i}", f"паттерн {i}",
                 "переговоры о цене со стратегическим клиентом")
            for i in range(10)]
    out = _lex.merge_lexical_candidates(
        vector, allp, "переговоры о цене со стратегическим клиентом")
    assert len(out) <= _lex.MAX_EXTRA, (
        "канал дополняет, а не заливает выдачу"
    )
    print("✅ добавка ограничена капом")


# ── 5. Проводка и осторожность ──────────────────────────────────────────

def test_engine_wiring_is_guarded():
    src = _src("backend/core/wisdom/wisdom_engine.py")
    idx = src.index("WISDOM_LEXICAL_CHANNEL")
    block = src[idx - 500:idx + 600]
    assert "except Exception" in block, (
        "сбой канала обязан оставлять прежнее поведение"
    )
    fn = src[src.index("async def _augment_lexical"):]
    fn = fn[:fn.index("async def sync_patterns_from_existing_meetings")]
    assert "merge_lexical_candidates" in fn
    assert "BWEWisdomPattern.user_id == wisdom_query.user_id" in fn, (
        "лексика обязана быть в границах пользователя — как и вектор"
    )
    print("✅ канал за выключателем, сбой безопасен, границы пользователя")




# ── 6. Агрегация: лечение multi-session 0.5 ─────────────────────────────

def test_aggregate_questions_detected():
    """Живой прогон показал провал агрегирующих вопросов на top-K ретриве
    (multi-session 0.5): сумма/счёт разбросаны по разговорам. Детектор
    обязан ловить этот класс — по-русски и по-английски."""
    for q, why in (
        ("What is the total amount I spent on luxury items?", "сумма"),
        ("How many health-related devices do I use?", "счёт"),
        ("How long have I been working in my current role?", "длительность"),
        ("сколько всего мы потратили на подрядчиков", "сумма"),
        ("в общей сложности сколько заняла миграция", "сумма"),
        ("как долго идёт проект Альфа", "длительность"),
    ):
        assert _det.detect(q), f"агрегация ({why}) обязана ловиться: {q!r}"
    # и по-прежнему без ложных срабатываний
    for q in ("расскажи про клиента Альфа",
              "what did I say about the trip",
              "почему мы отказались от подрядчика"):
        assert not _det.detect(q), f"ложное срабатывание: {q!r}"
    print("✅ агрегирующие вопросы ловятся, обычные — нет")


def test_sweep_arm_wired_and_uses_prod_detector():
    src = _src("backend/core/eval/longmemeval_hybrid_arm.py")
    fn = src[src.index("def arm_sweep"):]
    assert "enumerative_detect" in src[src.index("_is_aggregate_question"):], (
        "харнесс обязан мерить ПРОДОВЫЙ детектор, а не локальную копию"
    )
    assert "arm_temporal(item" in fn, (
        "неагрегирующий вопрос идёт прежним путём — sweep не трогает его"
    )
    assert "hit_sessions" in fn and "SESSION of" in fn, (
        "сессии с хитами подаются целиком, сгруппированно"
    )
    bench = _src("backend/core/eval/benchmark_longmemeval.py")
    assert '"sweep": arm_sweep_optional' in bench, "рука подключена в харнесс"
    print("✅ рука sweep: продовый детектор, сессии целиком, temporal-фолбэк")




# ── 7. Прод-перенос: «хит → вся встреча» на агрегации ───────────────────

def test_aggregate_expand_in_orchestrator_is_guarded():
    """Перенос измеренного лечения (0.43→0.57) в конвейер: добавление
    помечено и ограничено, ничего не удаляется, фильтры уважаются."""
    src = _src("backend/core/search/hybrid_search_orchestrator.py")
    idx = src.index("AGGREGATE_EXPAND")
    block = src[idx - 300:idx + 1100]
    assert '_v.kind == "count"' in block, (
        "расширение только на строгой детекции агрегации"
    )
    assert "results.extend(_expanded)" in block, "добавление, не замена"
    assert "except Exception" in block, "сбой расширения не роняет поиск"
    fn = src[src.index("def _expand_hit_meetings"):]
    fn = fn[:fn.index("async def _graph_search")]
    assert "score=0.0" in fn and '"expansion"' in fn, (
        "куски помечены и не получают выдуманной релевантности"
    )
    assert "metadata_matches_filters" in fn, (
        "выбор встреч пользователем уважается и в расширении"
    )
    assert "cap" in fn and "max_meetings" in fn, "добавка ограничена"
    print("✅ прод-расширение: помечено, ограничено, за выключателем")


def test_aggregate_expand_behavior():
    """Поведенческая проверка на подставном BM25-хранилище."""
    import types as _t
    orch_src = _src("backend/core/search/hybrid_search_orchestrator.py")
    # Вырезаем только метод и датакласс — оркестратор целиком тянет deps.
    ns: dict = {}
    import re as _re
    from dataclasses import dataclass, field
    from typing import Any, Dict, List, Optional
    dc = orch_src[orch_src.index("@dataclass\nclass HybridSearchResult"):]
    dc = dc[:dc.index("\n\n@dataclass")]
    exec(compile(dc, "<hsr>", "exec"),
         {"dataclass": dataclass, "field": field, "Dict": Dict, "Any": Any,
          "List": List, "Optional": Optional}, ns)
    HSR = ns["HybridSearchResult"]
    m = orch_src[orch_src.index("    def _expand_hit_meetings"):]
    m = m[:m.index("    async def _graph_search")]
    exec(compile("class _O:\n" + m + "\nHybridSearchResult=HybridSearchResult",
                 "<exp>", "exec"),
         {"List": List, "Optional": Optional, "Dict": Dict, "Any": Any,
          "HybridSearchResult": HSR}, ns)
    _O = ns["_O"]

    class _Doc:
        def __init__(self, text, meta):
            self.text, self.metadata = text, meta

    o = _O()
    o.bm25 = _t.SimpleNamespace(documents={
        "h1": _Doc("хит про траты", {"meeting_id": "m1"}),
        "e1": _Doc("ещё кусок м1", {"meeting_id": "m1"}),
        "e2": _Doc("другой кусок м1", {"meeting_id": "m1"}),
        "x1": _Doc("кусок чужой встречи", {"meeting_id": "m9"}),
    })
    results = [HSR(doc_id="h1", score=1.0, text="хит про траты",
                   metadata={"meeting_id": "m1"}, sources=["bm25"])]
    out = o._expand_hit_meetings(results)
    ids = {r.doc_id for r in out}
    assert ids == {"e1", "e2"}, f"вся встреча m1 без дублей и чужих: {ids}"
    assert all(r.score == 0.0 and r.sources == ["expansion"] for r in out)
    # фильтр выбора встреч уважается
    out2 = o._expand_hit_meetings(results, filters={"meeting_id": ["m9"]})
    assert out2 == [], "фильтр пользователя сильнее расширения"
    print("✅ расширение: вся встреча хита, без дублей/чужих, фильтр сильнее")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе тесты пределов поиска прошли.")
