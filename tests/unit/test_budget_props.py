# -*- coding: utf-8 -*-
"""Свойства бюджета проекта на узле графа.

ProjectStatusAgent извлекал `resource_analysis.budget_info` с самого начала,
но knowledge_sync читал только status_assessment — бюджет выбрасывался.
Теперь он сохраняется и версионируется.

Главное правило, ради которого нужен отдельный хелпер: **пустое поле не
пишется вовсе**. На большинстве встреч бюджет не обсуждают; если писать
пустую строку, то первая же планёрка без денег обнулила бы известную сумму.
Отсутствие ключа в update_node значит «не трогаем», пустая строка значила бы
«обнулили» — разница между «мы не знаем» и «мы знаем, что ноль».

Хелпер загружается по пути файла: knowledge_sync целиком тянет numpy,
которого нет в песочнице, а сам хелпер — чистый stdlib.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name: str, relpath: str):
    if name in sys.modules:
        return sys.modules[name]
    for pkg in ("backend", "backend.core", "backend.core.ontology"):
        if pkg not in sys.modules:
            mod = types.ModuleType(pkg)
            mod.__path__ = [os.path.join(ROOT, *pkg.split("."))]
            sys.modules[pkg] = mod
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_mu = _load("backend.core.ontology.money_units",
            "backend/core/ontology/money_units.py")
parse_money = _mu.parse_money


def _budget_props(parsed, raw_text, budget_status):
    """Копия контракта из knowledge_sync._budget_props.

    Импортировать оригинал нельзя — модуль тянет numpy/qdrant. Тело
    сверяется с оригиналом тестом test_matches_source_implementation ниже:
    он читает исходник и проверяет, что правила не разошлись.
    """
    props = {}
    if raw_text:
        props["budget_text"] = raw_text
    if budget_status:
        props["budget_status"] = budget_status
    if parsed:
        props["budget"] = parsed["normalized"]
        props["budget_amount"] = parsed["amount"]
        props["budget_currency"] = parsed["currency"]
        props["budget_currency_known"] = parsed["currency_known"]
    return props


def test_budget_with_amount_is_stored_parsed():
    p = _budget_props(parse_money("2,5 млн ₽"), "2,5 млн ₽", "on")
    assert p["budget"] == "2.5 млн ₽"
    assert p["budget_amount"] == 2_500_000.0
    assert p["budget_currency"] == "RUB"
    assert p["budget_currency_known"] is True
    assert p["budget_status"] == "on"
    assert p["budget_text"] == "2,5 млн ₽"
    print("✅ бюджет с суммой сохраняется разобранным и сравнимым")


def test_missing_budget_writes_nothing():
    """Встреча без бюджета не должна затирать известную сумму."""
    assert _budget_props(None, "", "") == {}
    print("✅ бюджет не обсуждали → не пишем ни одного поля")


def test_text_without_amount_keeps_wording_but_no_number():
    """«Уточняется» — это состояние, а не сумма."""
    p = _budget_props(parse_money("уточняется"), "уточняется", "")
    assert p == {"budget_text": "уточняется"}
    assert "budget_amount" not in p, "у фразы без числа не может быть суммы"
    print("✅ формулировка без числа сохранена, сумма не выдумана")


def test_amount_without_currency_is_flagged():
    p = _budget_props(parse_money("500 тысяч"), "500 тысяч", "")
    assert p["budget_amount"] == 500_000.0
    assert p["budget_currency"] == ""
    assert p["budget_currency_known"] is False, (
        "валюту не называли — нельзя выдавать её за известную"
    )
    print("✅ сумма без валюты помечена")


def test_status_alone_is_stored():
    """Бюджет назвали «перерасходом» без цифры — это тоже факт."""
    p = _budget_props(None, "", "over")
    assert p == {"budget_status": "over"}
    print("✅ статус бюджета без суммы сохраняется")


def test_budget_versions_are_comparable():
    """Ради чего всё: разные формулировки из разных встреч сравнимы."""
    v1 = _budget_props(parse_money("2 млн рублей"), "2 млн рублей", "on")
    v2 = _budget_props(parse_money("2 500 000 руб"), "2 500 000 руб", "over")
    assert v2["budget_amount"] > v1["budget_amount"]
    assert v2["budget_amount"] - v1["budget_amount"] == 500_000.0
    print("✅ рост бюджета считается между версиями: +500 000 ₽")


def test_matches_source_implementation():
    """Копия в тесте не должна разойтись с оригиналом в knowledge_sync."""
    src = open(os.path.join(ROOT, "backend/core/knowledge_sync.py"),
               encoding="utf-8").read()
    start = src.index("def _budget_props(")
    body = src[start:src.index("\n\n\n", start)]
    for rule in (
        'if raw_text:', 'props["budget_text"] = raw_text',
        'if budget_status:', 'props["budget_status"] = budget_status',
        'if parsed:', 'props["budget"] = parsed["normalized"]',
        'props["budget_amount"] = parsed["amount"]',
        'props["budget_currency"] = parsed["currency"]',
        'props["budget_currency_known"] = parsed["currency_known"]',
    ):
        assert rule in body, f"оригинал разошёлся с тестом: нет {rule!r}"
    print("✅ тест сверен с оригиналом _budget_props")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе тесты бюджета прошли.")
