# -*- coding: utf-8 -*-
"""Реранкер вторым проходом: гарантии осторожности.

Кросс-энкодер — выход из предела одновекторного поиска (arXiv
2508.21038), у gbrain он в бою. У нас его не было. Код новый, на наших
данных не проверен — поэтому дисциплина жёстче, чем у прежних
включений, и контракты фиксируют её:

  1. ВЫКЛЮЧЕН по умолчанию — включение только явное;
  2. переставляет, а не фильтрует: тот же набор объектов, ничего не
     выброшено и не добавлено;
  3. хвост за пределами top-N не трогается; кандидат без текста
     остаётся на месте, скоринг идёт по остальным;
  4. сбой скорера, кривые оценки, таймаут → исходный порядок, не ошибка;
  5. оркестратор: при выключенном флаге пул слияния прежний (top_k),
     при включённом — шире с обрезкой ПОСЛЕ реранка; вставка стоит до
     темпорального буста и не ломает его.

Скорер инъектируется — модель для проверки гарантий не нужна.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name: str, relpath: str):
    if name in sys.modules:
        return sys.modules[name]
    for pkg in ("backend", "backend.core", "backend.core.search"):
        if pkg not in sys.modules:
            m = types.ModuleType(pkg)
            m.__path__ = [os.path.join(ROOT, *pkg.split("."))]
            sys.modules[pkg] = m
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_rr = _load("backend.core.search.reranker", "backend/core/search/reranker.py")


def _docs(*texts):
    return [{"id": i, "text": t} for i, t in enumerate(texts)]


def _run(coro):
    return asyncio.run(coro)


# ── 1. Выключен по умолчанию ────────────────────────────────────────────

def test_disabled_by_default():
    os.environ.pop("RERANKER_ENABLED", None)
    assert _rr.reranker_enabled() is False, (
        "новый непроверенный код не должен включаться сам"
    )
    os.environ["RERANKER_ENABLED"] = "on"
    assert _rr.reranker_enabled() is True
    os.environ.pop("RERANKER_ENABLED", None)
    print("✅ выключен по умолчанию, включается явно")


# ── 2-3. Переставляет, не фильтрует ─────────────────────────────────────

def test_reorders_same_set_no_loss():
    docs = _docs("про бюджет", "про кадры", "про сроки")

    def scorer(_q, texts):
        # «про сроки» — самый релевантный
        return [0.1 if "бюджет" in t else 0.5 if "кадры" in t else 0.9
                for t in texts]
    out = _run(_rr.rerank("что со сроками", docs,
                          text_of=lambda d: d["text"], scorer=scorer))
    assert [d["id"] for d in out] == [2, 1, 0], "порядок — по оценкам пар"
    assert {id(d) for d in out} == {id(d) for d in docs}, (
        "тот же набор ОБЪЕКТОВ: ничего не создано и не выброшено"
    )
    print("✅ переставляет тот же набор, ничего не теряя")


def test_tail_beyond_top_n_untouched():
    docs = _docs("a", "b", "c", "d", "e")

    def scorer(_q, texts):
        return list(range(len(texts)))    # последний из head — самый сильный
    out = _run(_rr.rerank("q", docs, text_of=lambda d: d["text"],
                          top_n=3, scorer=scorer))
    assert [d["id"] for d in out[3:]] == [3, 4], (
        "хвост за пределами top-N сохраняет свой порядок"
    )
    assert [d["id"] for d in out[:3]] == [2, 1, 0]
    print("✅ хвост за top-N не трогается")


def test_empty_text_candidate_stays_in_place():
    docs = [{"id": 0, "text": "релевантное про отпуск"},
            {"id": 1, "text": ""},                    # нет текста
            {"id": 2, "text": "нерелевантное про сервер"}]

    def scorer(_q, texts):
        assert len(texts) == 2, "пустой текст не должен попасть в скоринг"
        return [0.2 if "сервер" in t else 0.9 for t in texts]
    out = _run(_rr.rerank("про отпуск", docs,
                          text_of=lambda d: d["text"], scorer=scorer))
    assert out[1]["id"] == 1, "кандидат без текста остался на своём месте"
    assert out[0]["id"] == 0 and out[2]["id"] == 2
    print("✅ кандидат без текста остаётся на месте, скоринг по остальным")


# ── 4. Сбои → исходный порядок ──────────────────────────────────────────

def test_failures_keep_original_order():
    docs = _docs("a", "b", "c")
    original = [d["id"] for d in docs]

    def broken(_q, _t):
        raise RuntimeError("модель умерла")
    out = _run(_rr.rerank("q", docs, text_of=lambda d: d["text"],
                          scorer=broken))
    assert [d["id"] for d in out] == original, "сбой → порядок прежний"

    def none_scorer(_q, _t):
        return None
    out2 = _run(_rr.rerank("q", docs, text_of=lambda d: d["text"],
                           scorer=none_scorer))
    assert [d["id"] for d in out2] == original

    def wrong_len(_q, texts):
        return [1.0]                      # оценок меньше, чем текстов
    out3 = _run(_rr.rerank("q", docs, text_of=lambda d: d["text"],
                           scorer=wrong_len))
    assert [d["id"] for d in out3] == original, "кривые оценки → прежний"
    print("✅ сбой, None и кривые оценки не меняют порядок")


def test_timeout_keeps_original_order():
    docs = _docs("a", "b", "c")

    def slow(_q, texts):
        import time
        time.sleep(2.0)
        return [1.0] * len(texts)
    out = _run(_rr.rerank("q", docs, text_of=lambda d: d["text"],
                          scorer=slow, timeout_s=0.2))
    assert [d["id"] for d in out] == [0, 1, 2], "таймаут → порядок прежний"
    print("✅ таймаут — порядок прежний, не ошибка")


def test_trivial_inputs_passthrough():
    one = _docs("a")
    assert _run(_rr.rerank("q", one, text_of=lambda d: d["text"])) == one
    assert _run(_rr.rerank("", _docs("a", "b"),
                           text_of=lambda d: d["text"]))[0]["id"] == 0
    print("✅ один документ или пустой запрос — без изменений")


# ── 5. Проводка в оркестраторе ──────────────────────────────────────────

def test_orchestrator_wiring_is_guarded():
    src = open(os.path.join(
        ROOT, "backend/core/search/hybrid_search_orchestrator.py"),
        encoding="utf-8").read()
    idx = src.index("reranker_enabled")
    block = src[idx - 400:idx + 1400]
    assert "top_k * 3 if _rr_on else top_k" in block, (
        "пул шире ТОЛЬКО при включённом реранкере — выключен = байт-в-байт"
    )
    assert "fused = fused[:top_k]" in block, "обрезка до top_k после реранка"
    assert "except Exception" in block, "сбой реранкера не роняет поиск"
    assert block.index("rerank") < src[idx:].index("temporal_rerank") + idx - idx, True
    # реранкер стоит ДО темпорального буста
    assert src.index("from backend.core.search.reranker import rerank") < \
        src.index("apply_temporal_rerank"), (
        "второй проход по тексту — до временного буста"
    )
    print("✅ проводка: шире пул только при флаге, сбой безопасен")


def test_airgap_note_present():
    src = open(os.path.join(ROOT, "backend/core/search/reranker.py"),
               encoding="utf-8").read()
    assert "скачиваются с HuggingFace" in src, (
        "оговорка для закрытого контура обязана быть в модуле"
    )
    print("✅ оговорка про веса модели для закрытого контура на месте")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе контракты реранкера прошли.")
