# -*- coding: utf-8 -*-
"""Валидация playbook'а перед сохранением.

Проверяет методику на типовые ошибки, чтобы сотрудник узнал о проблеме
СРАЗУ (при создании/правке), а не при первом прогоне:
  - dimensions: key непустой и уникальный
  - dimension.code: ЗАПРЕЩЁН из API — произвольный Python на исполнение
  - quantitative с computation: формула парсится safe-calc, inputs заданы
  - benchmark: direction валиден, пороги — числа
  - access_level в диапазоне 0..4

Возвращает (errors, warnings):
  errors   — блокируют сохранение (битая формула, дубль key)
  warnings — не блокируют, но подсвечивают (qualitative с computation и т.п.)
"""
from __future__ import annotations

from typing import Any

from backend.core.analysis.models import AnalysisPlaybook, DimensionKind
from backend.core.analysis.safe_calc import SafeCalcError, evaluate_program

_VALID_DIRECTIONS = {"higher_worse", "higher_better"}


def validate_playbook(pb: AnalysisPlaybook) -> tuple[list[str], list[str]]:
    """Вернуть (errors, warnings). errors непуст → не сохранять."""
    errors: list[str] = []
    warnings: list[str] = []

    if not pb.name or not pb.name.strip():
        errors.append("name: обязательно")

    if not (0 <= int(pb.access_level) <= 4):
        errors.append(f"access_level: должен быть 0..4, получено {pb.access_level}")

    if not pb.dimensions:
        warnings.append("dimensions: список пуст — разбор ничего не извлечёт")

    seen_keys: set[str] = set()
    for i, d in enumerate(pb.dimensions):
        # Произвольный Python в методике — не пользовательские данные.
        # Поле `code` уходит в code_runner на ИСПОЛНЕНИЕ на сервере, а
        # урезанные builtins защитой не являются (из них выходят штатным
        # обращением к __subclasses__). Ни одна встроенная методика этого
        # поля не использует — единственным его источником было тело
        # HTTP-запроса, то есть любой аутентифицированный пользователь.
        # Считаем расчёты формулами safe-calc (`computation`): они
        # разбираются по белому списку AST и ничего не исполняют.
        if getattr(d, "code", None):
            errors.append(
                f"dimensions[{i}].code: произвольный код не принимается — "
                f"используйте computation (формулу safe-calc)")
        prefix = f"dimensions[{i}] ({d.key or '?'})"

        if not d.key or not d.key.strip():
            errors.append(f"{prefix}: key обязателен")
        elif d.key in seen_keys:
            errors.append(f"{prefix}: дубликат key '{d.key}'")
        else:
            seen_keys.add(d.key)

        if not d.label or not d.label.strip():
            warnings.append(f"{prefix}: label пуст — UI покажет key")

        if d.kind not in (DimensionKind.QUANTITATIVE.value, DimensionKind.QUALITATIVE.value):
            errors.append(f"{prefix}: kind должен быть quantitative|qualitative")

        # Quantitative-специфика.
        if d.kind == DimensionKind.QUANTITATIVE.value:
            if not d.computation:
                warnings.append(f"{prefix}: quantitative без computation — расчёта не будет")
            else:
                # Проверяем что формула парсится (с фиктивными inputs).
                test_ns = {inp: 1.0 for inp in (d.inputs or [])}
                # Если inputs не заданы — добавим имена из формулы не можем,
                # просто попробуем парсинг (KeyError при missing → ок, формула валидна).
                try:
                    evaluate_program(d.computation, test_ns or {"x": 1.0})
                except SafeCalcError as e:
                    errors.append(f"{prefix}: невалидная computation: {e}")
            if not d.inputs:
                warnings.append(f"{prefix}: inputs пуст — computation может не найти данных")
        else:
            # Qualitative с computation — бессмысленно.
            if d.computation:
                warnings.append(f"{prefix}: qualitative с computation — будет проигнорировано")

        # Benchmark.
        bm = d.benchmark or {}
        if bm:
            direction = bm.get("direction", "higher_worse")
            if direction not in _VALID_DIRECTIONS:
                errors.append(
                    f"{prefix}: benchmark.direction должен быть {_VALID_DIRECTIONS}, "
                    f"получено '{direction}'"
                )
            for thresh_key in ("red_flag", "warning"):
                if thresh_key in bm and not _is_number(bm[thresh_key]):
                    errors.append(f"{prefix}: benchmark.{thresh_key} должен быть числом")

    return errors, warnings


def _is_number(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    if isinstance(v, str):
        try:
            float(v)
            return True
        except ValueError:
            return False
    return False
