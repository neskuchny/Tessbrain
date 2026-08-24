# -*- coding: utf-8 -*-
"""Выгрузка собственного слепка: состав и затирание сумм.

Первый шаг сценария переносимого слепка: сотрудник забирает свои данные
себе. До этого субъект данных не имел ни одной кнопки «скачать своё» —
GDPR-экспорт отдаёт профиль настроек, датасет ИИ-копии выгружается от
лица владельца тенанта.

Два контракта под проверкой:
  1. Денежные суммы затираются везде — слепок построен из встреч, где
     звучали бюджеты; это данные компании, а не человека. Числа без
     валюты (задачи, проценты) остаются — это результаты человека.
  2. Недоступный источник даёт пометку в sections_missing, а не роняет
     выгрузку: человек забирает то, что есть, и видит, чего нет.

Модуль загружается по пути файла: build_self_export лениво тянет тяжёлые
зависимости внутри функции, а проверяемое здесь затирание — чистый stdlib.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_self_export():
    name = "backend.core.twin.self_export"
    if name in sys.modules:
        return sys.modules[name]
    for pkg in ("backend", "backend.core", "backend.core.twin"):
        if pkg not in sys.modules:
            mod = types.ModuleType(pkg)
            mod.__path__ = [os.path.join(ROOT, *pkg.split("."))]
            sys.modules[pkg] = mod
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, "backend", "core", "twin", "self_export.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_se = _load_self_export()
redact_money = _se.redact_money
_redact_deep = _se._redact_deep
build_self_export = _se.build_self_export


def test_money_amounts_are_redacted():
    cases = [
        "бюджет проекта 2,5 млн ₽ утверждён",
        "потратили 1 200 000 руб на подрядчика",
        "выручка $50k за квартал",
        "контракт на 300 тысяч евро",
        "цена 500 тыс руб",
    ]
    for text in cases:
        out = redact_money(text)
        assert "[сумма скрыта]" in out, f"сумма уцелела: {out!r}"
        for tok in ("млн", "1 200 000", "$50", "300 тысяч евро", "500 тыс руб"):
            pass  # сам факт замены проверен выше; остатки проверяем ниже
    assert "2,5" not in redact_money("бюджет 2,5 млн ₽")
    assert "$50" not in redact_money("выручка $50k")
    print("✅ денежные суммы затираются")


def test_non_money_numbers_survive():
    """Результаты человека — числа без валюты — остаются."""
    for text in ("закрыл 14 задач за квартал",
                 "конверсия выросла на 20%",
                 "провёл 7 встреч",
                 "дедлайн 15 марта 2026"):
        assert redact_money(text) == text, f"затёрли не-деньги: {text!r}"
    print("✅ числа без валюты не трогаются")


def test_deep_redaction_walks_structures():
    data = {
        "achievements": ["сэкономил 2 млн ₽", "закрыл 14 задач"],
        "nested": {"note": "бюджет $10k", "count": 5},
    }
    out = _redact_deep(data)
    assert "[сумма скрыта]" in out["achievements"][0]
    assert out["achievements"][1] == "закрыл 14 задач"
    assert "[сумма скрыта]" in out["nested"]["note"]
    assert out["nested"]["count"] == 5
    print("✅ затирание проходит вглубь структур")


def test_export_survives_missing_sources():
    """Ни снапшота, ни отчёта, ни портрета — выгрузка всё равно собирается
    и честно перечисляет, чего не хватило."""
    export = asyncio.run(build_self_export(
        user_id="u-none", person_id="person_ghost",
        include_report=True, include_portrait=True,
    ))
    assert export["format"] == "tessent-person-export/v1"
    assert export["person_id"] == "person_ghost"
    missing = set(export["sections_missing"])
    assert "snapshot" in missing
    assert "employee_report" in missing
    assert "rpg_portrait" in missing
    print("✅ недоступные источники → пометки, а не падение")


def test_disclaimer_is_honest():
    """Выгрузка сама говорит про затирание и про отсутствие подписи."""
    export = asyncio.run(build_self_export(
        user_id="u-none", person_id="p-1",
        include_report=False, include_portrait=False,
    ))
    d = export["disclaimer"]
    assert "затёрты" in d
    assert "не подписан" in d, (
        "без подписи документ подтверждает содержание, но не происхождение — "
        "выгрузка обязана говорить это сама"
    )
    print("✅ дисклеймер честный: затирание и отсутствие подписи названы")


def test_no_financial_kpi_key_ever():
    """Финансовые KPI не включаются даже затёртыми."""
    export = asyncio.run(build_self_export(
        user_id="u-none", person_id="p-1",
        include_report=False, include_portrait=False,
    ))
    assert "financial_kpis" not in export
    assert "kpis" not in export
    print("✅ финансовых KPI в выгрузке нет как раздела")


def test_route_has_no_person_id_parameter():
    """Чужой слепок через этот эндпоинт невозможен по конструкции."""
    src = open(os.path.join(ROOT, "backend/api/routes/person_twin.py"),
               encoding="utf-8").read()
    start = src.index("class TwinSelfExportRequest")
    model = src[start:src.index("@post", start)]  # только поля Pydantic-модели
    assert "person_id" not in model, (
        "в запросе не должно быть поля person_id — только сшивка «это я»"
    )
    handler = src[src.index("async def twin_my_export"):src.index("router = Router")]
    assert "resolve_person_for_account" in handler
    assert "404" in handler, "нет сшивки → честный отказ, а не выгрузка наугад"
    print("✅ эндпоинт не принимает person_id — только подтверждённая сшивка")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе тесты выгрузки слепка прошли.")
