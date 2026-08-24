# -*- coding: utf-8 -*-
"""Детерминированный расчёт денег для документов (КП/счёт/договор).

Перенос принципа из MeetFlow: суммы (построчно, НДС, итог, прописью)
считает КОД, не LLM — модель числа не выдумывает. Decimal + HALF_UP.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any, List, Optional

_CENT = Decimal("0.01")


def _dec(v: Any, default: str = "0") -> Decimal:
    if v is None or v == "":
        v = default
    try:
        return Decimal(str(v).replace(",", ".").replace(" ", ""))
    except Exception:
        return Decimal(default)


def compute_totals(items: List[dict], *, vat_rate: float = 0,
                   vat_included: bool = False) -> dict:
    """Построчные суммы + НДС (сверху или «в том числе») + итог + прописью.

    items: [{name, qty?, unit?, price?}]. Цена квантуется к копейкам ДО
    умножения; все округления HALF_UP."""
    lines = []
    subtotal = Decimal("0.00")
    for it in items or []:
        qty = _dec(it.get("qty"), "1")
        if qty == 0 and it.get("qty") in (None, ""):
            qty = Decimal("1")
        price = _dec(it.get("price")).quantize(_CENT, rounding=ROUND_HALF_UP)
        line_total = (qty * price).quantize(_CENT, rounding=ROUND_HALF_UP)
        lines.append({
            "name": str(it.get("name") or ""),
            "qty": qty,
            "unit": str(it.get("unit") or "").strip() or "шт",
            "price": price,
            "line_total": line_total,
        })
        subtotal += line_total
    subtotal = subtotal.quantize(_CENT, rounding=ROUND_HALF_UP)

    rate = _dec(vat_rate)
    if rate > 0 and vat_included:
        vat_amount = (subtotal * rate / (Decimal(100) + rate)).quantize(
            _CENT, rounding=ROUND_HALF_UP)
        total = subtotal
    elif rate > 0:
        vat_amount = (subtotal * rate / Decimal(100)).quantize(
            _CENT, rounding=ROUND_HALF_UP)
        total = (subtotal + vat_amount).quantize(_CENT, rounding=ROUND_HALF_UP)
    else:
        vat_amount = Decimal("0.00")
        total = subtotal
    return {
        "lines": lines,
        "subtotal": subtotal,
        "vat_rate": rate,
        "vat_included": bool(vat_included and rate > 0),
        "vat_amount": vat_amount,
        "total": total,
        "total_in_words": rubles_in_words(total),
    }


# ── Сумма прописью (рубли/копейки, рода и склонения) ────────────────────

_UNITS_M = ["", "один", "два", "три", "четыре", "пять", "шесть", "семь",
            "восемь", "девять"]
_UNITS_F = ["", "одна", "две", "три", "четыре", "пять", "шесть", "семь",
            "восемь", "девять"]
_TEENS = ["десять", "одиннадцать", "двенадцать", "тринадцать",
          "четырнадцать", "пятнадцать", "шестнадцать", "семнадцать",
          "восемнадцать", "девятнадцать"]
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят",
         "шестьдесят", "семьдесят", "восемьдесят", "девяносто"]
_HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот",
             "шестьсот", "семьсот", "восемьсот", "девятьсот"]
# (ед., 2-4, 5+) для каждой тройки разрядов
_SCALES = [
    ("рубль", "рубля", "рублей", "m"),
    ("тысяча", "тысячи", "тысяч", "f"),
    ("миллион", "миллиона", "миллионов", "m"),
    ("миллиард", "миллиарда", "миллиардов", "m"),
]


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def _triple_words(n: int, gender: str) -> str:
    units = _UNITS_F if gender == "f" else _UNITS_M
    words = []
    if n >= 100:
        words.append(_HUNDREDS[n // 100])
        n %= 100
    if 10 <= n <= 19:
        words.append(_TEENS[n - 10])
    else:
        if n >= 20:
            words.append(_TENS[n // 10])
            n %= 10
        if n:
            words.append(units[n])
    return " ".join(words)


def rubles_in_words(amount: Any) -> str:
    """«1234.50» → «Одна тысяча двести тридцать четыре рубля 50 копеек»."""
    total = _dec(amount).quantize(_CENT, rounding=ROUND_HALF_UP)
    rub = int(total)
    kop = int((total - rub) * 100)

    if rub == 0:
        words = "ноль"
    else:
        parts: List[str] = []
        triples: List[int] = []
        n = rub
        while n:
            triples.append(n % 1000)
            n //= 1000
        for idx in range(len(triples) - 1, 0, -1):
            t = triples[idx]
            if not t:
                continue
            one, few, many, gender = _SCALES[idx]
            parts.append(_triple_words(t, gender))
            parts.append(_plural(t, one, few, many))
        if triples[0]:
            parts.append(_triple_words(triples[0], "m"))
        words = " ".join(p for p in parts if p)

    rub_word = _plural(rub, *_SCALES[0][:3])
    kop_word = _plural(kop, "копейка", "копейки", "копеек")
    out = f"{words} {rub_word} {kop:02d} {kop_word}"
    return out[0].upper() + out[1:]
