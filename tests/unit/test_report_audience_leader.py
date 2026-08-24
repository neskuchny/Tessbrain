# -*- coding: utf-8 -*-
"""Аудитория визуального отчёта: «руководителю» — это личная версия.

Найдено при сверке раздела «Доски» с кодом. `audience_of()` считала личными
только слова private/личное/лично/me/self, а встроенный шаблон
«Два отчёта: команде и себе» задаёт узлу `audience: "leader"`. Значение
нормализовалось в `public`, и руководитель получал ту же вычищенную
версию, что и команда: «на грани выгорания», конфликты и риск ухода из
неё вырезаны политикой эмоций.

То есть шаблон обещал две РАЗНЫЕ версии, а делал две одинаковых — и
именно та, ради которой он существует, приходила пустой.

Направление ошибки при незнакомом слове остаётся прежним и намеренным:
не узнали — считаем публичным. Ошибка в эту сторону скроет лишнее,
ошибка в другую — покажет команде личное.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load():
    name = "backend.core.board.emotion_policy"
    if name in sys.modules:
        return sys.modules[name]
    for pkg in ("backend", "backend.core", "backend.core.board"):
        if pkg not in sys.modules:
            mod = types.ModuleType(pkg)
            mod.__path__ = [os.path.join(ROOT, *pkg.split("."))]
            sys.modules[pkg] = mod
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, "backend", "core", "board", "emotion_policy.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_ep = _load()
audience_of = _ep.audience_of
apply_emotion_policy = _ep.apply_emotion_policy


def test_leader_is_private():
    """Главный случай: значение из встроенного шаблона."""
    assert audience_of("leader") == "private"
    print("✅ «leader» — личная версия, а не командная")


def test_manager_synonyms_are_private():
    for word in ("manager", "руководитель", "руководителю", "boss",
                 "private", "личное", "лично", "личный", "me", "self"):
        assert audience_of(word) == "private", f"{word!r} должно быть личным"
    print("✅ синонимы адресной версии распознаются")


def test_team_words_stay_public():
    for word in ("public", "team", "команде", "все", "all", ""):
        assert audience_of(word) == "public", f"{word!r} должно быть публичным"
    print("✅ командные значения остаются публичными")


def test_unknown_word_defaults_to_public():
    """Направление ошибки: не узнали — чистим. Скрыть лишнее безопаснее,
    чем показать команде личное."""
    assert audience_of("совет директоров") == "public"
    assert audience_of(None) == "public"
    assert audience_of(12345) == "public"
    print("✅ незнакомое значение → публичная (вычищенная) версия")


def test_private_keeps_sensitive_public_strips():
    """Разница между версиями реальна, а не только в метке."""
    def _payload():
        return {
            "attention": "Иван на грани выгорания",
            "items": [
                {"text": "Релиз сдвинулся на неделю"},
                {"text": "Конфликт в команде дизайна", "sensitive": True},
            ],
        }

    private = apply_emotion_policy(_payload(), "private")
    public = apply_emotion_policy(_payload(), "public")

    assert private.get("attention"), "личная версия сохраняет блок внимания"
    assert not public.get("attention"), "публичная версия блок внимания теряет"
    assert len(public.get("items", [])) < len(private.get("items", [])), (
        "чувствительный пункт обязан исчезнуть из командной версии"
    )
    print("✅ личная версия полнее командной — разница не декоративная")


def test_bundled_dual_report_template_now_works():
    """Тот самый шаблон, ради которого чинилось."""
    path = os.path.join(ROOT, "frontend/public/templates/process-dual-report.json")
    if not os.path.exists(path):
        print("⚠️  шаблон process-dual-report.json не найден — пропуск")
        return
    tpl = json.load(open(path, encoding="utf-8"))
    full = [n for n in tpl.get("nodes", [])
            if "полный" in str(n.get("data", {}).get("label", "")).lower()]
    assert full, "в шаблоне должен быть узел «Полный визуальный отчёт»"
    audience = full[0].get("data", {}).get("audience")
    assert audience_of(audience) == "private", (
        f"узел подписан «Полный визуальный отчёт», а аудитория {audience!r} "
        f"нормализуется в {audience_of(audience)} — отчёт придёт вычищенным"
    )
    print(f"✅ «Полный визуальный отчёт» (audience={audience!r}) "
          f"действительно полный")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе тесты аудитории отчёта прошли.")
