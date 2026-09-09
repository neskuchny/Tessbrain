# -*- coding: utf-8 -*-
"""Режим краткого ответа: отделяет ДЛИНУ ответа от КАЧЕСТВА памяти.

Зачем (docs/ru/REVIEW_TZ_FIX_SEARCH_DEFECTS.md, раздел 7). Продуктовый
промпт синтеза требует «ПОДРОБНЫЙ и РАЗВЁРНУТЫЙ» ответ и структуру
заголовками — осознанный выбор для человека, читающего аналитику. В замере
это дало 2556 символов против 353 у простой руки.

Отчёт списал развёрнутость как нейтральную черту. Это неверно: судья
LongMemEval для части типов вопросов инструктирован отвечать «нет», если
ответ содержит лишь ЧАСТЬ требуемого. Длинный ответ, перечисляющий
кандидатов и оговорки, проигрывает системно — независимо от того, что
нашла память. Пока рука с коротким ответом на том же ретриве не прогнана,
часть разрыва 0.350 против 0.617 нельзя приписать качеству памяти.

Режим `answer_style="brief"` существует ровно для этого прогона. По
умолчанию — "full": продуктовое поведение не меняется.

Контракты:
  1. по умолчанию промпт требует развёрнутости (регрессии нет);
  2. в brief требования развёрнутости и структуры сняты;
  3. brief не трогает остальное: язык, доказательность, дату — на месте.

Запуск: python tests/unit/test_answer_style_brief.py
"""
from __future__ import annotations

import os
import re
import sys

try:                                     # pragma: no cover - зависит от среды
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

SRC = os.path.join(ROOT, "backend", "core", "search",
                   "enhanced_search_orchestrator.py")
CODE = open(SRC, encoding="utf-8").read()


def test_search_accepts_answer_style():
    assert re.search(r"answer_style: str = \"full\"", CODE), (
        "у search() нет параметра answer_style — прогнать краткую руку нечем")
    assert 'self._answer_style = answer_style' in CODE, (
        "answer_style не сохраняется на инстансе, до промпта он не доедет")
    print("✅ search() принимает answer_style и сохраняет его")


def test_full_mode_keeps_verbose_requirements():
    assert "Ответ должен быть ПОДРОБНЫМ и РАЗВЁРНУТЫМ" in CODE, (
        "требование развёрнутости пропало — это регрессия продуктового "
        "поведения, а не починка бенчмарка")
    assert "Структурируй ответ с помощью списков и заголовков" in CODE
    print("✅ режим по умолчанию сохраняет продуктовую развёрнутость")


def test_brief_mode_removes_verbosity_and_structure():
    assert "Отвечай КРАТКО и по существу" in CODE, (
        "в brief нет требования краткости")
    assert "Не используй заголовки и списки" in CODE, (
        "в brief не снято требование структуры — длина останется прежней")
    print("✅ brief снимает и развёрнутость, и структуру")


def test_brief_does_not_touch_the_rest():
    """Краткость не должна утаскивать за собой доказательность и язык."""
    for must_stay in ("СЕГОДНЯ:",                       # дата отсчёта
                      "_answer_lang_instruction",       # язык ответа
                      "КРИТИЧЕСКИ ВАЖНО:"):             # анти-отказ
        assert must_stay in CODE, f"из промпта пропало: {must_stay}"
    print("✅ дата, язык и анти-отказ на месте в обоих режимах")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе контракты режима краткого ответа прошли.")
