# -*- coding: utf-8 -*-
"""
Temporal resolver — резолюция ОТНОСИТЕЛЬНЫХ дат против даты-якоря (даты встречи/
сессии/документа). Обоснование — docs/ru/BENCHMARK_PUBLIC_LOCOMO_HOTPOT.md.

ROOT CAUSE (измерено на LoCoMo temporal). Дешёвая модель при ответе на «когда X»
КОПИРУЕТ видимую дату сессии вместо вычисления «дата − смещение»: текст
«[22 Aug 2022] …won yesterday» → модель отвечает «22 Aug» (а верно 21 Aug).
Арифметику она делает, только когда абсолютной даты рядом нет («last summer» →
год). Промпт-инструкция «применяй смещение» проигрывает приору «эхо видимой даты».

ФИКС (скелет+LLM): считаем смещение В КОДЕ и АННОТИРУЕМ текст разрешённой датой,
чтобы модель не делала (и не путала) арифметику. Замер на temporal-подмножестве
LoCoMo (n=10, gpt-4o-mini): analysis 0.20 → analysis+resolved **0.60** (×3),
флипнулись ровно «yesterday»/«last week» кейсы (Evan 6→5 янв, Maria 16→9 июня,
Nate 22→21 авг, Joanna 6 окт→29 сен).

Чистый stdlib (тестируется изолированно). Обрабатываем ПРОШЛЫЕ выражения (доказано);
будущее («next week») намеренно НЕ трогаем — на замере не помогло, риск регрессий.
"""
from __future__ import annotations

import datetime
import re
from typing import List, Optional, Tuple

_MONTHS = {mn.lower(): i for i, mn in enumerate(
    ["", "January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"])}
# русские месяцы (род. и им. падеж): «21 августа 2022», «21 август 2022»
for _i, _ru in enumerate(
        ["", "январ", "феврал", "март", "апрел", "ма", "июн", "июл", "август",
         "сентябр", "октябр", "ноябр", "декабр"]):
    if _ru:
        _MONTHS[_ru] = _i  # префикс — матчим по startswith ниже

# дата-якорь из строки: "3:57 pm on 21 August, 2022" | "22 August, 2022" |
# "21 августа 2022" (кириллица поддержана)
_DATE_RX = re.compile(r"(\d{1,2})\s+([A-Za-zА-Яа-яЁё]+)\.?,?\s+(\d{4})")

# относительные ПРОШЛЫЕ выражения → смещение в днях (нечёткие — приближённо).
# Порядок важен: более специфичные/длинные раньше. EN — валидировано на LoCoMo;
# RU — продуктовый язык (транскрипты встреч): «решили вчера», «на прошлой неделе».
_REL: List[Tuple[re.Pattern, int]] = [
    # --- английские (валидированы замером LoCoMo: 0.20→0.60) ---
    (re.compile(r"\bday before yesterday\b", re.I), -2),
    (re.compile(r"\byesterday\b", re.I), -1),
    (re.compile(r"\blast night\b", re.I), -1),
    (re.compile(r"\ba week ago\b", re.I), -7),
    (re.compile(r"\blast week\b", re.I), -7),
    (re.compile(r"\blast (monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I), -7),
    (re.compile(r"\bthe other day\b", re.I), -3),
    (re.compile(r"\ba few days ago\b", re.I), -3),
    (re.compile(r"\blast month\b", re.I), -30),
    (re.compile(r"\ba month ago\b", re.I), -30),
    (re.compile(r"\blast year\b", re.I), -365),
    # --- русские (язык продукта; тот же механизм смещения) ---
    (re.compile(r"\bпозавчера\b", re.I), -2),
    (re.compile(r"\bвчера\b", re.I), -1),
    (re.compile(r"\bнеделю назад\b", re.I), -7),
    (re.compile(r"\bна прошлой неделе\b", re.I), -7),
    (re.compile(r"\bв прошл(ый|ую) (понедельник|вторник|среду|четверг|пятницу|субботу)\b", re.I), -7),
    (re.compile(r"\bв прошлое воскресенье\b", re.I), -7),
    (re.compile(r"\bна днях\b", re.I), -3),
    (re.compile(r"\bнесколько дней назад\b", re.I), -3),
    (re.compile(r"\bпару дней назад\b", re.I), -2),
    (re.compile(r"\bв прошлом месяце\b", re.I), -30),
    (re.compile(r"\bмесяц назад\b", re.I), -30),
    (re.compile(r"\bв прошлом году\b", re.I), -365),
    (re.compile(r"\bгод назад\b", re.I), -365),
]


def parse_anchor_date(s) -> Optional[datetime.date]:
    """Достать дату-якорь. Принимает: date/datetime, ISO 'YYYY-MM-DD[...]',
    текст '21 August, 2022' / '3:57 pm on 21 August, 2022'. None, если не распознано."""
    if s is None:
        return None
    if isinstance(s, datetime.datetime):
        return s.date()
    if isinstance(s, datetime.date):
        return s
    s = str(s)
    if not s:
        return None
    # ISO: 2022-08-21 или 2022-08-21T13:00:00
    iso = re.match(r"\s*(\d{4})-(\d{2})-(\d{2})", s)
    if iso:
        try:
            return datetime.date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None
    mo = _DATE_RX.search(s)
    if not mo:
        return None
    mon = _month_from_word(mo.group(2))
    if not mon:
        return None
    try:
        return datetime.date(int(mo.group(3)), mon, int(mo.group(1)))
    except ValueError:
        return None


def _month_from_word(w: str) -> Optional[int]:
    """Номер месяца из слова: 'August', 'Aug', 'августа', 'мая'.

    Префикс-матч в обе стороны, выбираем самый ДЛИННЫЙ ключ:
    'августа'.startswith('август') — русские падежи; 'august'.startswith('aug')
    — английские сокращения. Longest-match решает 'марта' (март, не «ма»=май).
    """
    w = (w or "").lower()
    if w in _MONTHS:
        return _MONTHS[w]
    if len(w) < 2:
        return None
    best, best_len = None, 0
    for name, idx in _MONTHS.items():
        if idx and (w.startswith(name) or name.startswith(w)) and len(name) > best_len:
            best, best_len = idx, len(name)
    return best


_MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December"]


def _fmt(d: datetime.date) -> str:
    # без zero-pad, платформенно-независимо (без strftime('%-d'))
    return f"{d.day} {_MONTH_NAMES[d.month]} {d.year}"


def detect_relative(text: Optional[str]) -> Optional[Tuple[str, int]]:
    """Первое относительное прошлое выражение в тексте → (выражение, смещение_дней)."""
    if not text:
        return None
    for rx, off in _REL:
        mo = rx.search(text)
        if mo:
            return mo.group(0), off
    return None


def resolve_in_text(text: Optional[str], anchor: Optional[datetime.date]) -> Optional[datetime.date]:
    """Разрешённая абсолютная дата для относительного выражения в тексте (или None)."""
    if not text or anchor is None:
        return None
    hit = detect_relative(text)
    if not hit:
        return None
    return anchor + datetime.timedelta(days=hit[1])


def annotate(text: str, anchor: Optional[datetime.date]) -> str:
    """Дописать к тексту разрешённую дату, если есть относительное выражение.

    Недеструктивно: если выражения/якоря нет — возвращает текст без изменений
    (байт-в-байт). Аннотация одна (по первому выражению) — как в валидированном замере.
    """
    resolved = resolve_in_text(text, anchor)
    if resolved is None:
        return text
    return f"{text} (resolved date ≈ {_fmt(resolved)})"


# поля метаданных, где может лежать дата-якорь источника (по приоритету)
_ANCHOR_FIELDS = ("date", "meeting_date", "session_date", "decided_at", "created_at",
                  "timestamp", "deadline", "indexed_at")


def anchor_from_metadata(metadata: Optional[dict]) -> Optional[datetime.date]:
    """Дата-якорь из metadata источника (первое распознанное поле)."""
    if not metadata:
        return None
    for f in _ANCHOR_FIELDS:
        v = metadata.get(f)
        if v:
            d = parse_anchor_date(v)
            if d is not None:
                return d
    return None
