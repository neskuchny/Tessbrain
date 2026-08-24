# -*- coding: utf-8 -*-
"""Проверка цитат: метка «подтверждено» должна что-то значить.

Полевые персоны продаются тем, что каждая боль помечена «подтверждена
дословной цитатой» или «домысел, проверить», и решает это КОД, а не
модель. Метка `origin='field'` — единственный якорь доверия ко всему
блоку исследования.

Найденная дыра: для цитаты длиннее 60 символов проверялось лишь окно
`q[15:60]` — 45 символов. То есть модель могла взять короткий реальный
фрагмент и дописать к нему что угодно любой длины, а метка
«подтверждено» ставилась всё равно.

Новое правило: точное вхождение — да; иначе для длинной цитаты требуем,
чтобы НАЧАЛО И КОНЕЦ нашлись в корпусе и совпало не меньше половины
кусков. Легитимный случай (модель выбросила кусок из середины) проходит,
дописанный хвост или начало — нет.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load():
    name = "backend.core.marketing.research_engine"
    if name in sys.modules:
        return sys.modules[name]
    for pkg in ("backend", "backend.core", "backend.core.marketing"):
        if pkg not in sys.modules:
            mod = types.ModuleType(pkg)
            mod.__path__ = [os.path.join(ROOT, *pkg.split("."))]
            sys.modules[pkg] = mod
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, "backend", "core", "marketing",
                           "research_engine.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_re = _load()
verify_quote = _re.verify_quote
_norm_text = _re._norm_text
mark_insights_origin = _re.mark_insights_origin

CORPUS = _norm_text(
    "Мы перестали переделывать макеты по третьему кругу, потому что теперь "
    "бриф согласуют заранее и правки собирают в один заход. "
    "Ещё жалуются что дизайнеры не читают ТЗ."
)


def test_exact_long_quote_verified():
    q = ("Мы перестали переделывать макеты по третьему кругу, потому что "
         "теперь бриф согласуют заранее")
    assert verify_quote(q, CORPUS) is True
    print("✅ точная длинная цитата подтверждается")


def test_model_dropped_middle_still_verified():
    """Легитимный случай, ради которого послабление и существует."""
    q = ("Мы перестали переделывать макеты по третьему кругу теперь бриф "
         "согласуют заранее и правки собирают в один заход")
    assert verify_quote(q, CORPUS) is True
    print("✅ выброшенный моделью кусок середины не ломает подтверждение")


def test_fabricated_tail_rejected():
    """Главная дыра: настоящее начало + дописанный хвост."""
    q = ("Мы перестали переделывать макеты по"
         " и поэтому команда решила сменить подрядчика и уволить дизайнера "
         "немедленно прямо завтра")
    assert verify_quote(q, CORPUS) is False, (
        "дописанный хвост не может получать метку «подтверждено»"
    )
    print("✅ дописанный хвост отвергается")


def test_fabricated_head_rejected():
    q = ("Руководство приняло стратегическое решение об изменении процессов "
         "правки собирают в один заход")
    assert verify_quote(q, CORPUS) is False
    print("✅ дописанное начало отвергается")


def test_half_fabricated_rejected():
    q = ("Мы перестали переделывать макеты по третьему кругу, потому что теперь"
         " всех дизайнеров отправили на курсы повышения квалификации за счёт "
         "компании")
    assert verify_quote(q, CORPUS) is False
    print("✅ наполовину выдуманная цитата отвергается")


def test_pure_invention_rejected():
    q = ("Клиенты постоянно жалуются на то что сроки срываются каждую неделю "
         "без объяснений")
    assert verify_quote(q, CORPUS) is False
    print("✅ полностью выдуманная длинная цитата отвергается")


def test_short_quotes_need_exact_match():
    assert verify_quote("не читают ТЗ", CORPUS) is False, "короче порога"
    assert verify_quote("полная выдумка", CORPUS) is False
    assert verify_quote("дизайнеры не читают ТЗ", CORPUS) is True
    print("✅ короткие цитаты требуют точного вхождения")


def test_origin_marking_end_to_end():
    """Метки расставляет код: field только при подтверждённой цитате."""
    marked = mark_insights_origin([
        {"text": "боль", "quote": "дизайнеры не читают ТЗ", "source": "S1"},
        {"text": "без цитаты", "quote": "", "source": ""},
        {"text": "выдумка", "quote": "все мечтают купить немедленно прямо сейчас",
         "source": "S1"},
    ], CORPUS)
    assert [m["origin"] for m in marked] == ["field", "hypothesis", "hypothesis"]
    print("✅ origin расставляется кодом: field / hypothesis")


def test_old_window_hole_is_closed():
    """Защита от возврата прежнего правила."""
    src = open(os.path.join(ROOT, "backend/core/marketing/research_engine.py"),
               encoding="utf-8").read()
    # Ищем именно исполняемую форму: упоминание в комментарии-объяснении,
    # почему так делать нельзя, — это не возврат правила.
    code_lines = [ln.split("#")[0] for ln in src.splitlines()
                  if not ln.lstrip().startswith("#")]
    body = "\n".join(code_lines)
    assert "mid = q[15:60]" not in body, "окно из 45 символов вернулось"
    assert "return mid in corpus_norm" not in body
    assert "_windows_found" in body, "проверка по окнам должна остаться"
    print("✅ прежнее правило одного окна не вернулось")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе тесты проверки цитат прошли.")
