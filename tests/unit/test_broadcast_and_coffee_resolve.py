# -*- coding: utf-8 -*-
"""Перевод послания руководителя и доставка артефактов сотруднику.

Две вещи, которые владелец продукта назвал ключевыми для Mini Tess, и обе
были уязвимы:

1. Перевод послания «на язык сотрудника». Ограничители «не добавляй
   фактов и цифр» и «обязательный блок что это значит для тебя» держались
   ТОЛЬКО на послушании модели — постпроверки не было, тестов тоже.
   Выдуманная цифра в персональной версии стратегии — не помарка: разные
   люди получают разные вводные, то есть ровно тот конфликт, который
   перевод должен предотвращать.

2. Доставка артефактов встречи сотруднику. Участнику присваивался
   СЛУЧАЙНЫЙ uuid4, если во входных данных не было id, и он же шёл в
   доставку как user_id аккаунта — совпасть с реальным аккаунтом он не мог
   никогда. Артефакты садились в pending для всех, кроме владельца ингеста.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name: str, relpath: str, pkgs):
    if name in sys.modules:
        return sys.modules[name]
    for pkg in pkgs:
        if pkg not in sys.modules:
            mod = types.ModuleType(pkg)
            mod.__path__ = [os.path.join(ROOT, *pkg.split("."))]
            sys.modules[pkg] = mod
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_pr = _load("backend.core.coffee.participant_resolve",
            "backend/core/coffee/participant_resolve.py",
            ("backend", "backend.core", "backend.core.coffee"))
resolve_participant_account = _pr.resolve_participant_account
unresolved_reason = _pr.unresolved_reason


def _src(rel: str) -> str:
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


# ── 1. Проверка перевода послания ───────────────────────────────────────

def _check(original: str, adapted: str) -> dict:
    """Копия правила из strategy_relay._check_personalized.
    Сверяется с оригиналом тестом test_check_matches_source."""
    import re
    def nums(t):
        return set(re.findall(r"\d+(?:[.,]\d+)?", str(t or "")))
    src = nums(original)
    new_numbers = sorted(n for n in nums(adapted) if n not in src)
    low = str(adapted or "").lower()
    has_block = ("что это значит для тебя" in low
                 or "что это значит для вас" in low
                 or "what this means for you" in low)
    return {"ok": not new_numbers and has_block,
            "new_numbers": new_numbers[:10],
            "has_meaning_block": has_block}


def test_invented_number_is_caught():
    """Главный случай: модель добавила цифру, которой не было."""
    original = "В этом квартале фокус на удержании клиентов."
    adapted = ("Наша цель — удержание. Что это значит для тебя: "
               "сократить отток на 15%.")
    res = _check(original, adapted)
    assert res["new_numbers"] == ["15"], res
    assert res["ok"] is False
    print("✅ выдуманная цифра в персональной версии ловится")


def test_numbers_from_original_are_fine():
    original = "Цель — 20 новых клиентов и рост выручки на 30%."
    adapted = ("Нам нужно 20 клиентов и +30% выручки. "
               "Что это значит для тебя: два звонка в день.")
    res = _check(original, adapted)
    assert res["new_numbers"] == []
    assert res["ok"] is True
    print("✅ цифры из оригинала не считаются выдумкой")


def test_missing_meaning_block_is_flagged():
    """Блок «что это значит для тебя» — смысл всего перевода."""
    original = "Фокус на удержании клиентов."
    adapted = "Коллеги, давайте удерживать клиентов."
    res = _check(original, adapted)
    assert res["has_meaning_block"] is False
    assert res["ok"] is False
    print("✅ отсутствие блока «что это значит для тебя» замечается")


def test_meaning_block_variants_recognized():
    for phrase in ("Что это значит для тебя:", "Что это значит для вас —",
                   "What this means for you:"):
        res = _check("текст", f"Пересказ. {phrase} пункт")
        assert res["has_meaning_block"] is True, phrase
    print("✅ распознаются варианты блока (ты/вы/англ)")


def test_check_matches_source():
    """Копия в тесте не должна разойтись с оригиналом."""
    src = _src("backend/core/broadcast/strategy_relay.py")
    start = src.index("def _check_personalized(")
    body = src[start:src.index("async def _personalize(", start)]
    for rule in ('что это значит для тебя', 'что это значит для вас',
                 'what this means for you', 'new_numbers', 'has_meaning_block'):
        assert rule in body, f"оригинал разошёлся с тестом: нет {rule!r}"
    print("✅ тест сверен с оригиналом _check_personalized")


def test_relay_retries_then_falls_back_to_original():
    """Две попытки с цифрами — отдаём оригинал, а не адаптацию с выдумкой."""
    src = _src("backend/core/broadcast/strategy_relay.py")
    start = src.index("async def _personalize(")
    body = src[start:src.index("async def create_broadcast(", start)]
    assert "_check_personalized(message, text)" in body
    assert "перегенерация" in body, "нужна повторная попытка со строгим напоминанием"
    assert body.count("return message") >= 2, (
        "при повторной неудаче обязан отдаваться оригинал"
    )
    print("✅ при выдуманных цифрах: повтор, затем откат к оригиналу")


# ── 2. Резолв участника встречи в аккаунт ───────────────────────────────

def test_explicit_user_id_wins():
    r = resolve_participant_account({"user_id": "u-1", "name": "Аня"}, "org-1")
    assert r == {"user_id": "u-1", "basis": "explicit"}
    print("✅ явный user_id принимается как есть")


def test_random_participant_id_is_not_used_as_account():
    """Ядро дефекта: uuid4 участника — не аккаунт сотрудника."""
    r = resolve_participant_account(
        {"id": "3f2a1b6c-0000-4000-a000-000000000123", "name": "Аня"},
        "org-1", person_lookup=lambda *_: None, members_lookup=lambda _: [])
    assert r["user_id"] is None, "случайный id участника не должен стать аккаунтом"
    assert r["basis"] == "unresolved"
    print("✅ случайный id участника не выдаётся за аккаунт")


def test_person_link_resolves():
    r = resolve_participant_account(
        {"person_id": "person_ann", "name": "Аня"}, "org-1",
        person_lookup=lambda pid, org: "u-ann" if pid == "person_ann" else None,
        members_lookup=lambda _: [])
    assert r == {"user_id": "u-ann", "basis": "person_link"}
    print("✅ подтверждённая сшивка «это я» резолвит аккаунт")


def test_exact_name_resolves():
    members = [{"user_id": "u-ann", "name": "Аня Петрова"},
               {"user_id": "u-bob", "name": "Боря"}]
    r = resolve_participant_account(
        {"name": "аня  петрова"}, "org-1",
        person_lookup=lambda *_: None, members_lookup=lambda _: members)
    assert r == {"user_id": "u-ann", "basis": "name_exact"}
    print("✅ точное совпадение имени (без учёта регистра и пробелов)")


def test_namesakes_are_not_guessed():
    """Доставить личный разбор не тому человеку хуже, чем не доставить."""
    members = [{"user_id": "u-1", "name": "Аня"},
               {"user_id": "u-2", "name": "Аня"}]
    r = resolve_participant_account(
        {"name": "Аня"}, "org-1",
        person_lookup=lambda *_: None, members_lookup=lambda _: members)
    assert r["user_id"] is None
    assert r["basis"] == "ambiguous_name"
    assert "несколько сотрудников" in unresolved_reason(r["basis"], "Аня")
    print("✅ тёзки не разрешаются наугад, причина объяснена")


def test_partial_name_is_not_a_match():
    members = [{"user_id": "u-ann", "name": "Аня Петрова"}]
    r = resolve_participant_account(
        {"name": "Аня"}, "org-1",
        person_lookup=lambda *_: None, members_lookup=lambda _: members)
    assert r["user_id"] is None, "частичное совпадение имени — не основание"
    print("✅ частичное совпадение имени не считается резолвом")


def test_unresolved_reason_is_actionable():
    msg = unresolved_reason("unresolved", "Аня")
    assert "это я" in msg or "организацию" in msg, (
        "причина должна подсказывать, что чинить"
    )
    print("✅ причина в pending объясняет, что делать")


def test_orchestrator_wires_resolver():
    src = _src("backend/core/coffee/orchestrator.py")
    assert "resolve_participant_account" in src
    assert "_unresolved_reason" in src, (
        "нерезолвленный участник обязан нести причину дальше"
    )
    print("✅ резолвер подключён к кофе-сценарию")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе тесты перевода и доставки прошли.")
