# -*- coding: utf-8 -*-
"""Язык визуальных отчётов: весь вывод — на языке пользователя (RU/EN).

Контент даёт LLM-экстракция (директива языка в промпте), фиксированные подписи в
Pillow-рендере и в промпте image-модели переключаются через pick(). Инвариант
направления, не опция — см. docs/VISUAL_REPORTS.md §7.6.
"""
from __future__ import annotations

from typing import Any


def is_ru(lang: Any) -> bool:
    return str(lang or "ru").strip().lower().startswith("ru")


def pick(lang: Any, ru: str, en: str) -> str:
    """Выбрать строку по языку."""
    return ru if is_ru(lang) else en


def lang_line(lang: Any) -> str:
    """Директива языка для промпта экстракции."""
    return ("\nОтветь на РУССКОМ языке." if is_ru(lang)
            else "\nRespond in ENGLISH.")


def all_text_note(lang: Any) -> str:
    """Требование к тексту на картинке для промпта image-модели.

    Лимит объёма — общий для всех форматов: image-модели плохо рисуют много
    кириллицы; немного крупного читаемого текста бьёт много нечитаемого
    (аудит промптов vs концепция §2.1)."""
    return (
        "ВЕСЬ ТЕКСТ — НА РУССКОМ, чётко читаемый, БЕЗ орфографических "
        "ошибок, точно как задан выше. КАЖДАЯ подпись КОРОТКАЯ — ключевые "
        "слова, 2-5 слов (длинную фразу сократи без потери смысла); коротких "
        "подписей может быть много — плотная карта читаема, длинные "
        "предложения — нет."
        if is_ru(lang) else
        "ALL TEXT IN ENGLISH, clearly legible, NO spelling mistakes, exactly "
        "as given above. EVERY label is SHORT — keywords, 2-5 words (shorten "
        "long phrases without losing meaning); many short labels are fine — "
        "a dense map stays readable, long sentences do not."
    )


__all__ = ["is_ru", "pick", "lang_line", "all_text_note"]
