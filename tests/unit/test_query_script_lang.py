# -*- coding: utf-8 -*-
"""Язык ответа поиска выбирается по ПРЕОБЛАДАЮЩЕМУ алфавиту вопроса.

Отдельный файл, а не дополнение к test_answer_language.py: тот закрепляет
ДРУГУЮ политику — язык ответа = язык ИНТЕРФЕЙСА пользователя
(resolve_answer_lang: локаль запроса → Persona → дефолт). Здесь про
`query_script_lang` — определение языка ПО ТЕКСТУ вопроса, которое нужно
там, где ни запроса, ни персоны нет: фоновая задача, бенчмарк.

История дефекта (docs/ru/REVIEW_TZ_FIX_SEARCH_DEFECTS.md, раздел 6).
Инструкция №7 промпта синтеза была прибита к русскому при заявленной
двуязычности: на английский вопрос приходил русский ответ (доля кириллицы
в ответах на англоязычном наборе доходила до 86 %). Первая починка
проверяла «есть ли хоть один кириллический символ» — и заворачивала в
русский ЛЮБОЙ английский вопрос с русским именем собственным. Для
двуязычной компании «What did Титан-Банк decide?» — обычный запрос.

Контракты:
  1. чисто русский вопрос → "ru" (регрессии для основной аудитории нет);
  2. чисто английский → "en";
  3. КЛЮЧЕВОЙ: английский вопрос с русским именем собственным → "en";
  4. русский вопрос с латинским термином → "ru" (симметрично);
  5. неясная смесь и текст без букв → "", чтобы вызывающий ушёл к языку
     интерфейса, а не угадывал по одному символу перевеса.

Запуск: python tests/unit/test_query_script_lang.py
"""
from __future__ import annotations

import os
import sys

try:                                     # pragma: no cover - зависит от среды
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from backend.core.llm.lang import query_script_lang  # noqa: E402


def test_pure_russian_is_ru():
    assert query_script_lang("Какие решения приняли на планёрке?") == "ru"
    print("✅ чисто русский вопрос → ru")


def test_pure_english_is_en():
    assert query_script_lang("What did we decide about the deadline?") == "en"
    print("✅ чисто английский вопрос → en")


def test_english_with_russian_proper_noun_stays_english():
    """Тот самый случай, который ломала проверка «есть хоть один символ»."""
    q = "What did Титан-Банк decide about the integration deadline?"
    got = query_script_lang(q)
    assert got == "en", (
        f"английский вопрос с русским именем собственным ушёл в {got!r} — "
        "это и есть дефект: одно кириллическое слово переключало весь ответ")
    print("✅ английский вопрос с русским именем остаётся английским")


def test_russian_with_latin_term_stays_russian():
    q = "Что решили по интеграции с API и деплою на staging?"
    assert query_script_lang(q) == "ru", (
        "русский вопрос с латинскими терминами не должен уезжать в английский")
    print("✅ русский вопрос с латинскими терминами остаётся русским")


def test_ambiguous_and_letterless_defer_to_caller():
    assert query_script_lang("") == ""
    assert query_script_lang("2026-03-17 ???") == ""
    # Примерно равная смесь: 9 кириллических против 8 латинских — ни одна
    # доля не берёт 0.6, язык не выбирается.
    assert query_script_lang("Титан Банк deadline") == ""
    print("✅ при неясной смеси и без букв решение отдаётся вызывающему")


def test_single_cyrillic_char_does_not_flip_long_english():
    q = ("Please summarise the quarterly revenue discussion and list every "
         "commitment the team made, including the one mentioned by Пётр.")
    assert query_script_lang(q) == "en", (
        "длинный английский вопрос перевернулся из-за одного имени")
    print("✅ одно имя не переворачивает длинный английский вопрос")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе контракты выбора языка по тексту вопроса прошли.")
