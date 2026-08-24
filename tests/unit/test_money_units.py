# -*- coding: utf-8 -*-
"""Разбор денежной суммы из живой речи встречи.

ProjectStatusAgent уже извлекает `resource_analysis.budget_info.
estimated_budget` — но строкой, как её произнесли: «примерно 2,5 млн
рублей», «$50k», «пока не утверждён». Чтобы бюджет стал версионируемым
фактом, строку надо превратить в сравнимое число, не выдумав ничего
сверх сказанного.

Главный инвариант: ничего не додумываем. Нет числа — None. Есть число без
валюты — так и говорим (currency_known=False), а не подставляем рубли.

Загрузка модуля здесь прямая, по пути файла: пакет backend.core.ontology в
__init__ тянет numpy, которого нет в песочнице, а сам разбор — чистый
stdlib и от пакета не зависит.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_money_units():
    name = "backend.core.ontology.money_units"
    if name in sys.modules:
        return sys.modules[name]
    for pkg in ("backend", "backend.core", "backend.core.ontology"):
        if pkg not in sys.modules:
            mod = types.ModuleType(pkg)
            mod.__path__ = [os.path.join(ROOT, *pkg.split("."))]
            sys.modules[pkg] = mod
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, "backend", "core", "ontology", "money_units.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_mu = _load_money_units()
parse_money = _mu.parse_money
parse_money_list = _mu.parse_money_list


def test_scale_and_currency_from_speech():
    """Как это реально произносят на встрече."""
    m = parse_money("примерно 2,5 млн рублей")
    assert m["amount"] == 2_500_000.0
    assert m["raw_amount"] == 2.5
    assert m["scale_label"] == "млн"
    assert m["currency"] == "RUB"
    assert m["currency_known"] is True
    assert m["normalized"] == "2.5 млн ₽"
    print(f"✅ «примерно 2,5 млн рублей» → {m['amount']:,.0f} RUB")


def test_english_shorthand():
    m = parse_money("$50k")
    assert m["amount"] == 50_000.0
    assert m["currency"] == "USD"
    print("✅ «$50k» → 50 000 USD")


def test_spaced_thousands():
    m = parse_money("1 200 000 ₽")
    assert m["amount"] == 1_200_000.0
    assert m["currency"] == "RUB"
    print("✅ разряды пробелами")


def test_english_comma_thousands_vs_russian_decimal():
    """«1,200,000» — разряды; «2,5» — десятичная запятая. Разные вещи."""
    assert parse_money("1,200,000 руб")["amount"] == 1_200_000.0
    assert parse_money("2,5 млн")["raw_amount"] == 2.5
    assert parse_money("1,200.50 USD")["amount"] == 1200.5
    print("✅ запятая разрядная и десятичная различаются")


def test_no_number_means_no_amount():
    """«Бюджет не утверждён» — это отсутствие суммы, а не ноль."""
    for text in ("бюджет пока не утверждён", "Планируемый бюджет",
                 "обсудим позже", "", None, "   "):
        assert parse_money(text) is None, f"на {text!r} придумали сумму"
    print("✅ нет числа → None, а не ноль")


def test_amount_without_currency_is_marked_unknown():
    """Число назвали, валюту — нет. Не подставляем рубли молча."""
    m = parse_money("бюджет 500 тысяч")
    assert m["amount"] == 500_000.0
    assert m["currency"] == ""
    assert m["currency_known"] is False
    assert m["normalized"] == "500 тыс"
    print("✅ сумма без валюты помечена как валюта-неизвестна")


def test_percent_and_headcount_are_not_money():
    """Классический ложный срабатыш: «бюджет вырос на 20%»."""
    assert parse_money("вырос на 20%") is None
    assert parse_money("20 процентов") is None
    assert parse_money("команда из 5 человек") is None
    assert parse_money("через 3 месяца") is None
    assert parse_money("30 дней") is None
    print("✅ проценты, люди и сроки не выдаются за деньги")


def test_scale_priority_is_largest_first():
    """«млрд» не должен читаться как «млн» из-за порядка проверок."""
    assert parse_money("1 млрд ₽")["amount"] == 1_000_000_000.0
    assert parse_money("1 млн ₽")["amount"] == 1_000_000.0
    assert parse_money("1 тыс ₽")["amount"] == 1_000.0
    print("✅ масштаб распознаётся от большего к меньшему")


def test_currency_before_number():
    """«в рублях — 2 млн»: валюта стоит до числа."""
    m = parse_money("бюджет в рублях — 2 млн")
    assert m["amount"] == 2_000_000.0
    assert m["currency"] == "RUB"
    print("✅ валюта распознаётся и до числа")


def test_euro_and_dollar_words():
    assert parse_money("300 тысяч евро")["currency"] == "EUR"
    assert parse_money("300 тысяч долларов")["currency"] == "USD"
    assert parse_money("300 тыс. €")["currency"] == "EUR"
    print("✅ евро и доллары словами и знаками")


def test_parse_list_skips_empty():
    out = parse_money_list(["2 млн ₽", "не определён", None, "$1k"])
    assert [m["amount"] for m in out] == [2_000_000.0, 1_000.0]
    assert parse_money_list([]) == []
    assert parse_money_list(None) == []
    print("✅ список: пустые значения пропускаются")


def test_comparable_across_scales():
    """Ради чего всё: суммы из разных встреч сравнимы между собой."""
    a = parse_money("2 млн ₽")["amount"]
    b = parse_money("2 500 000 руб")["amount"]
    c = parse_money("1,8 млн рублей")["amount"]
    assert c < a < b, "версии бюджета обязаны сравниваться как числа"
    print("✅ бюджеты из разных формулировок сравнимы")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе тесты разбора сумм прошли.")
