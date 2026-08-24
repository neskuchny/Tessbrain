# -*- coding: utf-8 -*-
"""Деньги: словарь валют/масштабов и разбор суммы из свободного текста.

Здесь живёт КАНОНИЧЕСКИЙ словарь валют и масштабов. Он был заведён в
dataset_registry для профилирования колонок таблиц («Выручка, млн ₽») и
переехал сюда, когда тот же словарь понадобился второму потребителю —
бюджету проекта из встречи. Два списка валют в разных файлах разошлись бы
на первой же правке.

Разбор текста (`parse_money`) отличается от профилирования колонки: там на
входе сотня однородных значений и заголовок, здесь — одна фраза из
разговора («примерно 2,5 млн рублей», «$50k», «бюджет пока не утверждён»).

Принцип тот же, что и в профилировании: НИЧЕГО НЕ ВЫДУМЫВАЕМ. Не нашли
число — вернём None. Нашли число без валюты — так и скажем
(`currency_known=False`), а не подставим рубли по месту работы компании.
Чистый stdlib.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# Порядок важен: сперва масштаб (тыс/млн/млрд), затем валюта.
SCALE_PATTERNS = (
    (re.compile(r"млрд|миллиард|billions?|\bbn\b", re.I), 1_000_000_000, "млрд"),
    (re.compile(r"млн|миллион|millions?|\bmln\b|\bmm\b", re.I), 1_000_000, "млн"),
    # «\d\s*k» отдельно от «\bk\b»: в «$50k» между цифрой и k нет границы
    # слова, и один только \bk\b его не ловит.
    (re.compile(r"тыс|thousands?|\d\s*k\b|\bk\b|т\.р", re.I), 1_000, "тыс"),
)
CURRENCY_PATTERNS = (
    (re.compile(r"₽|руб|\brub\b", re.I), "RUB", "₽"),
    (re.compile(r"\$|\busd\b|доллар", re.I), "USD", "$"),
    (re.compile(r"€|\beur\b|евро", re.I), "EUR", "€"),
)
# Что ПО СМЫСЛУ деньги — если валюта не распознана, честно просим уточнить,
# а не гадаем.
MONEY_NAME_RE = re.compile(
    r"выручк|оборот|доход|расход|бюджет|стоимост|сумм|прибыл|затрат|"
    r"cost|price|revenue|budget|amount|income|profit", re.I)

# Число целиком: 1200, 1 200 000, 1'200, 2,5, 2.5, 1 200,50, 1,200,000.
# Берём широкий токен «цифра … цифра» и разбираем разделители в _to_number:
# отдельным регексом английские разряды («1,200,000») и русская десятичная
# запятая («2,5») не различаются — это решается только по числу групп.
# Разделители разрядов — пробел (в т.ч. неразрывный и узкий), апостроф.
_NUMBER_RX = re.compile(r"\d[\d\u0020\u00a0\u202f'’.,]*\d|\d")

# Слова, после которых число — НЕ сумма, а срок/доля/количество. Ловим
# «бюджет вырос на 20%» и «команда из 5 человек», чтобы не выдать их за деньги.
_NOT_MONEY_TAIL_RX = re.compile(
    r"^\s*(?:%|процент|человек|людей|сотрудник|штук|шт\.|дн|день|дня|дней|"
    r"недел|месяц|год|лет|час|мин|шт\b)", re.I)


def _to_number(raw: str) -> Optional[float]:
    """«1 200 000» → 1200000.0, «2,5» → 2.5. None, если не разобрали.

    Запятая с ровно тремя цифрами после неё и ещё цифрами дальше — это
    английский разделитель разрядов («1,200,000»), а не десятичная запятая.
    """
    s = re.sub(r"[   '’]", "", raw or "").strip()
    # Токен берётся широко, поэтому с краёв может прилипнуть разделитель
    # («2,5.» из «стоит 2,5. Потом решим»).
    s = s.strip(".,")
    if not s:
        return None
    if "," in s and "." in s:
        # «1,200.50» — запятая разрядная, точка десятичная
        s = s.replace(",", "")
    elif s.count(",") > 1:
        s = s.replace(",", "")            # «1,200,000»
    elif s.count(".") > 1:
        s = s.replace(".", "")            # «1.200.000» — европейские разряды
    elif "," in s:
        head, _, tail = s.partition(",")
        s = head + tail if len(tail) == 3 and head.isdigit() and len(head) > 0 \
            and not head.startswith("0") else head + "." + tail
    try:
        return float(s)
    except ValueError:
        return None


def parse_money(text: Any) -> Optional[Dict[str, Any]]:
    """Разобрать денежную сумму из свободного текста.

    Возвращает None, если суммы в тексте нет (в том числе для «бюджет пока
    не утверждён» — это не ноль, это отсутствие числа).

    Иначе dict:
      amount          — число в базовых единицах с учётом масштаба (2 млн → 2000000.0)
      raw_amount      — число до умножения на масштаб (2.0)
      scale/scale_label — множитель и его ярлык («млн»)
      currency/currency_symbol — код валюты и знак, если распознаны
      currency_known  — False, если валюту в тексте не назвали
      text            — исходная строка
      normalized      — человекочитаемая форма («2 млн ₽»)
    """
    s = str(text or "").strip()
    if not s:
        return None

    m = _NUMBER_RX.search(s)
    if not m:
        return None
    raw_amount = _to_number(m.group(0))
    if raw_amount is None:
        return None

    tail = s[m.end():]
    if _NOT_MONEY_TAIL_RX.match(tail):
        # «на 20%», «5 человек» — это не сумма.
        return None

    # Масштаб и валюту ищем во всей фразе: «бюджет в рублях — 2 млн»
    # и «2 млн рублей» одинаково валидны.
    scale, scale_label = 1, ""
    for rx, mult, label in SCALE_PATTERNS:
        if rx.search(s):
            scale, scale_label = mult, label
            break

    currency = currency_symbol = ""
    for rx, code, sym in CURRENCY_PATTERNS:
        if rx.search(s):
            currency, currency_symbol = code, sym
            break

    amount = raw_amount * scale
    parts = [f"{raw_amount:g}"]
    if scale_label:
        parts.append(scale_label)
    if currency_symbol:
        parts.append(currency_symbol)

    return {
        "amount": amount,
        "raw_amount": raw_amount,
        "scale": scale,
        "scale_label": scale_label,
        "currency": currency,
        "currency_symbol": currency_symbol,
        "currency_known": bool(currency),
        "text": s,
        "normalized": " ".join(parts),
    }


def parse_money_list(values: List[Any]) -> List[Dict[str, Any]]:
    """Разобрать несколько строк, пропустив те, где суммы нет."""
    out = []
    for v in values or []:
        parsed = parse_money(v)
        if parsed:
            out.append(parsed)
    return out
