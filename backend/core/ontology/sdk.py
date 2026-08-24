# -*- coding: utf-8 -*-
"""Ontology SDK — типизированный слой над графовой онтологией (P4).

До P4 онтология была *описана* (`core/store/schema.py`: NODE_SCHEMA на 26
типов, RELATIONSHIP_SCHEMA на 39 связей), но **не была законом**:
`SchemaValidator` в нестрогом режиме только писал warning и пропускал что
угодно, а «действия» (write-back) были разрозненным fire-and-forget без
схемы. Это и есть пробел «Ontology SDK + Actions» из CAPABILITY_MAP.

P4 НЕ дублирует схему — он делает её **единственным источником правды** и
добавляет:

  • типизированный доступ к онтологии (объекты/связи/обязательные поля)
  • валидацию с ГРАДУИРОВАННОЙ строгостью (см. EnforcementMode):
      - extraction из встреч (грязные LLM-данные) → WARN, как сегодня,
        НО с нормализацией → конвейер не ломаем (careful rollout)
      - граница партнёрского ingest (P3) → STRICT, отклоняем мусор
  • нормализацию базовых полей (типобезопасность access_level/confidence/…)

Дизайн (как P0–P3):
- читает NODE_SCHEMA/RELATIONSHIP_SCHEMA из schema.py — без копий
- никогда не raises (контракт — degrade gracefully)
- чистый, без I/O → юнит-тест без графа/БД
- обратная совместимость: schema.SchemaValidator делегирует сюда,
  поэтому ВСЕ существующие точки записи graph_builder проходят через SDK
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from backend.core.store.schema import (
    ACCESS_LEVELS,
    NODE_SCHEMA,
    RELATIONSHIP_SCHEMA,
)

logger = logging.getLogger(__name__)


class EnforcementMode(str, Enum):
    """Насколько строго онтология применяется на конкретной границе."""
    OFF = "off"        # не валидировать (аварийный обход)
    WARN = "warn"      # нормализовать + логировать, НИКОГДА не отклонять
    STRICT = "strict"  # нормализовать + отклонять жёсткие нарушения


def extraction_mode_from_env() -> "EnforcementMode":
    """Режим онтологии для внутренней extraction из встреч.

    env `ONTOLOGY_EXTRACTION_MODE` (off|warn|strict). DEFAULT = warn —
    careful rollout P4/P6: «грязные» LLM-данные не рушим, пока ops
    осознанно не включит strict. Никогда не raises.
    """
    import os

    raw = (os.getenv("ONTOLOGY_EXTRACTION_MODE", "warn") or "warn").strip().lower()
    if raw in ("strict", "enforce", "on"):
        return EnforcementMode.STRICT
    if raw in ("off", "none", "disable"):
        return EnforcementMode.OFF
    return EnforcementMode.WARN


@dataclass
class ValidationResult:
    """Итог проверки объекта/связи по онтологии."""
    ok: bool = True
    normalized: dict[str, Any] = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)  # жёсткие (→ reject в STRICT)
    warnings: list[str] = field(default_factory=list)     # мягкие (нормализация/неизвестное)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "violations": self.violations,
            "warnings": self.warnings,
        }


# === Типизированный доступ к онтологии ===================================

def known_object_types() -> set[str]:
    return set(NODE_SCHEMA.keys())


def known_relationship_types() -> set[str]:
    return set(RELATIONSHIP_SCHEMA.keys())


def is_known_object(label: str) -> bool:
    return label in NODE_SCHEMA


def object_required_props(label: str) -> list[str]:
    spec = NODE_SCHEMA.get(label)
    return list(spec.get("required_props", [])) if spec else []


def describe_object(label: str) -> Optional[dict[str, Any]]:
    spec = NODE_SCHEMA.get(label)
    if not spec:
        return None
    return {
        "label": label,
        "description": spec.get("description", ""),
        "required_props": list(spec.get("required_props", [])),
        "optional_props": list(spec.get("optional_props", [])),
    }


# === Нормализация (применяется во ВСЕХ режимах, кроме OFF) ===============

def _coerce_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _coerce_float(v: Any, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def normalize_object_props(props: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Безопасные приведения базовых полей. Возвращает (props, warnings).

    Никогда не выбрасывает данные пользователя — только чинит типы
    системных полей, чтобы граф/доступ не падали ниже по стеку.
    """
    out = dict(props)
    warns: list[str] = []

    if "access_level" in out:
        lvl = _coerce_int(out["access_level"], 2)
        clamped = max(1, min(lvl, 5))
        if clamped != out["access_level"]:
            warns.append(f"access_level normalized → {clamped}")
        out["access_level"] = clamped
        if clamped not in ACCESS_LEVELS:
            out["access_level"] = 2

    if "confidence" in out:
        c = _coerce_float(out["confidence"], 0.8)
        clamped = max(0.0, min(c, 1.0))
        if clamped != out["confidence"]:
            warns.append(f"confidence normalized → {clamped}")
        out["confidence"] = clamped

    for list_field in ("access_groups", "aliases", "tags", "meeting_ids"):
        if list_field in out and not isinstance(out[list_field], list):
            val = out[list_field]
            if val in (None, ""):
                del out[list_field]  # пусто → пусть default проставится ниже
            else:
                warns.append(f"{list_field} coerced to list")
                out[list_field] = [val]

    # пустые/None системные поля убираем — пусть graph_builder проставит default
    for k in list(out.keys()):
        if out[k] is None:
            del out[k]

    return out, warns


# === Валидация объекта ===================================================

def validate_object(
    label: str,
    props: dict[str, Any],
    mode: EnforcementMode = EnforcementMode.WARN,
) -> ValidationResult:
    """Проверить узел по онтологии. Никогда не raises.

    STRICT отклоняет: неизвестный тип, отсутствие обязательных полей.
    WARN: всё то же, но ok=True (только warnings) — конвейер не рушим.
    OFF: пропустить как есть.
    """
    if mode == EnforcementMode.OFF:
        return ValidationResult(ok=True, normalized=dict(props))

    res = ValidationResult(normalized=dict(props))

    if not isinstance(label, str) or not label:
        res.violations.append("empty/invalid object label")
        res.ok = mode != EnforcementMode.STRICT
        return res

    norm, warns = normalize_object_props(props if isinstance(props, dict) else {})
    res.normalized = norm
    res.warnings.extend(warns)

    if label not in NODE_SCHEMA:
        msg = f"unknown object type '{label}'"
        if mode == EnforcementMode.STRICT:
            res.violations.append(msg)
            res.ok = False
        else:
            res.warnings.append(msg)
        return res

    required = NODE_SCHEMA[label].get("required_props", [])
    missing = [p for p in required if p not in norm or norm.get(p) in (None, "")]
    if missing:
        msg = f"object '{label}' missing required props: {missing}"
        if mode == EnforcementMode.STRICT:
            res.violations.append(msg)
            res.ok = False
        else:
            res.warnings.append(msg)

    return res


def validate_relationship(
    from_label: str,
    to_label: str,
    rel_type: str,
    mode: EnforcementMode = EnforcementMode.WARN,
) -> ValidationResult:
    """Проверить связь по онтологии. Никогда не raises."""
    if mode == EnforcementMode.OFF:
        return ValidationResult(ok=True)

    res = ValidationResult()

    if rel_type not in RELATIONSHIP_SCHEMA:
        msg = f"unknown relationship type '{rel_type}'"
        if mode == EnforcementMode.STRICT:
            res.violations.append(msg)
            res.ok = False
        else:
            res.warnings.append(msg)
        return res

    allowed = RELATIONSHIP_SCHEMA[rel_type]
    ok_pair = any(
        (c["from"] in ("*", from_label)) and (c["to"] in ("*", to_label))
        for c in allowed
    )
    if not ok_pair:
        msg = f"invalid relationship {from_label} -[{rel_type}]-> {to_label}"
        if mode == EnforcementMode.STRICT:
            res.violations.append(msg)
            res.ok = False
        else:
            res.warnings.append(msg)

    return res


__all__ = [
    "EnforcementMode",
    "ValidationResult",
    "describe_object",
    "extraction_mode_from_env",
    "is_known_object",
    "known_object_types",
    "known_relationship_types",
    "normalize_object_props",
    "object_required_props",
    "validate_object",
    "validate_relationship",
]
