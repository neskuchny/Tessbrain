# -*- coding: utf-8 -*-
"""Реляционные вопросы, тип ответа, уверенный структурный ответ.

Три модуля, родившиеся из разбора BrainBench, но написанные ОТ ПРОДУКТА:
русские вопросы первыми, английские — вторыми. Именно этот файл — довод,
что правила не подогнаны под чужой корпус: чужой корпус не может наградить
за «кто был на встрече в четверг».

Контракты:
  1. enumerative_detect ловит реляционные вопросы БЕЗ слова «все»
     (русские и английские), но НЕ ловит обычные вопросы;
  2. RELATIONAL_DETECT=off возвращает прежний детектор байт-в-байт;
  3. answer_type: «кто/who» → person, «когда/when» → time, прочее → None;
     вопросительное слово в середине фразы типом не считается;
  4. confident_graph: сужает только при полном наборе условий; пустой
     список кандидатов, неизвестный тип, слишком большой набор → None;
     не мутирует вход; выключен по умолчанию;
  5. оркестратор: уверенный ответ подключён ПОСЛЕ слияния, за флагом,
     в try/except (структурная проверка по исходнику).
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name, relpath):
    for pkg in ("backend", "backend.core", "backend.core.search"):
        if pkg not in sys.modules:
            m = types.ModuleType(pkg)
            m.__path__ = [os.path.join(ROOT, *pkg.split("."))]
            sys.modules[pkg] = m
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, relpath))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


det = _load("backend.core.search.enumerative_detect",
            "backend/core/search/enumerative_detect.py")
ans = _load("backend.core.search.answer_type",
            "backend/core/search/answer_type.py")
conf = _load("backend.core.search.confident_graph",
             "backend/core/search/confident_graph.py")


# ── 1. Реляционный детектор ─────────────────────────────────────────────

def test_relational_detects_russian_questions():
    os.environ["RELATIONAL_DETECT"] = "on"
    positives = [
        "Кто был на встрече в четверг?",
        "кто участвовал в обсуждении бюджета",
        "Кто отвечает за деплой?",
        "кто из отдела продаж присутствовал на демо",
        "Кто руководит проектом Восток?",
        "кто в команде интеграций",
        "Кто принял решение о переносе релиза?",
        "Кого назначили ответственным за миграцию?",
        "кто готовит отчёт по кварталу",
    ]
    for q in positives:
        v = det.detect(q)
        assert v and v.kind in ("relational", "intersect", "count", "list"), (
            f"должен ловиться: {q!r}")
    print("✅ русские реляционные вопросы ловятся без слова «все»")


def test_relational_detects_english_questions():
    os.environ["RELATIONAL_DETECT"] = "on"
    for q in ("Who attended the kickoff meeting?",
              "Who works at Acme Robotics?",
              "who invested in Beta Health",
              "Who advises Cipher Labs?",
              "who from marketing joined the call"):
        assert det.detect(q), f"должен ловиться: {q!r}"
    print("✅ английские реляционные вопросы ловятся")


def test_relational_does_not_overfire():
    os.environ["RELATIONAL_DETECT"] = "on"
    negatives = [
        "Что решили по бюджету на квартал?",
        "Покажи протокол встречи от четверга",
        "Какой статус у задачи про миграцию?",
        "Расскажи про проект Восток",
        "What is the status of the migration?",
        "Show me the budget document",
    ]
    for q in negatives:
        v = det.detect(q)
        assert not (v and v.kind == "relational"), (
            f"ложная сработка реляционного паттерна: {q!r} → {v.matched!r}")
    print("✅ обычные вопросы реляционными не помечаются")


def test_relational_kill_switch_restores_old_detector():
    os.environ["RELATIONAL_DETECT"] = "off"
    try:
        assert not det.detect("Кто был на встрече в четверг?")
        assert not det.detect("Who attended the kickoff meeting?")
        # прежние классы работают как раньше
        assert det.detect("покажи всех, кто работал над проектом").kind == "list"
        assert det.detect("сколько человек было на встрече").kind == "count"
    finally:
        os.environ["RELATIONAL_DETECT"] = "on"
    print("✅ RELATIONAL_DETECT=off возвращает прежний детектор")


# ── 2. Тип ожидаемого ответа ────────────────────────────────────────────

def test_answer_type_person_and_time():
    assert ans.expected_answer_type("Кто был на встрече?") == ans.PERSON
    assert ans.expected_answer_type("кого назначили ответственным") == ans.PERSON
    assert ans.expected_answer_type("С кем встречался Иван в марте?") == ans.PERSON
    assert ans.expected_answer_type("а кто ведёт проект?") == ans.PERSON
    assert ans.expected_answer_type("скажи, кто отвечает за релиз") == ans.PERSON
    assert ans.expected_answer_type("Who attended the demo?") == ans.PERSON
    assert ans.expected_answer_type("Когда решили переносить релиз?") == ans.TIME
    assert ans.expected_answer_type("when was the contract signed") == ans.TIME
    print("✅ «кто» → person, «когда» → time")


def test_answer_type_is_conservative():
    assert ans.expected_answer_type("Что решили по бюджету?") is None
    assert ans.expected_answer_type("Покажи документ, кто бы его ни писал") is None
    assert ans.expected_answer_type("Статус задачи, когда будет время") is None
    assert ans.expected_answer_type("") is None
    assert ans.expected_answer_type(None) is None
    print("✅ вопросительное слово в середине фразы типом не считается")


# ── 3. Уверенный структурный ответ ──────────────────────────────────────

class _FR:
    def __init__(self, doc_id, score=1.0):
        self.doc_id = doc_id
        self.rrf_score = score


def test_confident_narrows_only_with_all_conditions():
    fused = [_FR("p/ann"), _FR("d/protocol"), _FR("p/bob"), _FR("m/kickoff")]
    picked = conf.pick_confident(fused, ["p/ann", "p/bob"],
                                 verdict_truthy=True, expected_type="person")
    assert [f.doc_id for f in picked] == ["p/ann", "p/bob"], "порядок слияния сохраняется"
    assert len(fused) == 4, "вход не мутируется"
    # нет вердикта / нет типа / нет кандидатов / кандидаты не дожили → None
    assert conf.pick_confident(fused, ["p/ann"], verdict_truthy=False,
                               expected_type="person") is None
    assert conf.pick_confident(fused, ["p/ann"], verdict_truthy=True,
                               expected_type=None) is None
    assert conf.pick_confident(fused, [], verdict_truthy=True,
                               expected_type="person") is None
    assert conf.pick_confident(fused, ["p/ghost"], verdict_truthy=True,
                               expected_type="person") is None
    print("✅ сужение — только при полном наборе условий")


def test_confident_rejects_implausibly_large_sets():
    fused = [_FR(f"p/{i}") for i in range(40)]
    ids = [f"p/{i}" for i in range(conf.MAX_SET + 1)]
    assert conf.pick_confident(fused, ids, verdict_truthy=True,
                               expected_type="person") is None, (
        "полграфа — не «список участников», доверия нет")
    print("✅ неправдоподобно большой набор отвергается")


def test_confident_disabled_by_default():
    os.environ.pop("CONFIDENT_GRAPH", None)
    assert not conf.confident_enabled(), "дефолт обязан быть выключен"
    os.environ["CONFIDENT_GRAPH"] = "on"
    assert conf.confident_enabled()
    os.environ.pop("CONFIDENT_GRAPH", None)
    print("✅ CONFIDENT_GRAPH по умолчанию выключен")


# ── 4. Подключение в оркестраторе (структурно) ──────────────────────────

def test_orchestrator_wiring_is_flagged_and_after_fusion():
    src = open(os.path.join(
        ROOT, "backend/core/search/hybrid_search_orchestrator.py"),
        encoding="utf-8").read()
    i_fuse = src.index("fused = fusion.fuse(")
    i_conf = src.index("confident_enabled()")
    assert i_conf > i_fuse, "уверенный ответ обязан стоять ПОСЛЕ слияния"
    window = src[i_conf - 600:i_conf + 1800]
    assert "except Exception" in window, "сбой режима не должен ронять поиск"
    assert "pick_confident" in window
    assert "_verdict" in window, "триггер — продовый детектор, не свой"
    print("✅ оркестратор: за флагом, после слияния, в try/except")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе контракты реляционного поиска прошли.")
