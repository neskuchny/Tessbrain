# -*- coding: utf-8 -*-
"""Data lineage — происхождение узлов графа (P7).

ПРОБЕЛ: узлы несли `source_meeting_id`/`created_at`, но НЕ несли
полноценного lineage — кто/какой pipeline их создал и какие
трансформации над ними прошли. Без этого нельзя ни отладить «откуда
взялся этот кривой узел» (особенно важно после P6: eval-harness
ловит violation, но не показывает источник), ни предъявить
provenance при P3-федерации.

P7 добавляет на узел компактную lineage-метку:

  props["_lineage"] = {
    "created_by":  str,        # актор/сервис, создавший узел
    "pipeline_id": str,        # какой прогон конвейера
    "origin_stage": str,       # стадия, где узел родился
    "created_at": iso,
    "history": [               # bounded list трансформаций
      {"stage","actor","op","ts","note"}
    ],
  }
  + плоские зеркала props["created_by"], props["pipeline_id"]
  (для дешёвой фильтрации в графовых запросах без парсинга JSON).

Дизайн (как P0–P6): чистый, инъектируемые часы, never-raises,
идемпотентный (повторный stamp не перетирает created_by, а
добавляет событие), bounded история. Careful rollout: env
`DATA_LINEAGE_ENABLED` (default OFF) → нулевое изменение props,
пока ops не включит.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

LINEAGE_KEY = "_lineage"
_MAX_HISTORY = 25  # bounded — узел не должен пухнуть бесконечно

ClockFn = Callable[[], str]


def _default_clock() -> str:
    return datetime.now(timezone.utc).isoformat()


def lineage_enabled_from_env() -> bool:
    """env `DATA_LINEAGE_ENABLED`. Никогда не raises.

    ВКЛЮЧЕНО по умолчанию 2026-08-13 (аудит бизнес-карты, раздел 16):
    модуль, API и интерфейс были готовы, но метки не копились — а задним
    числом происхождение не восстановить. Каждый день выключенной трассы —
    день фактов без ответа «откуда это известно». Запись ограничена
    (_MAX_HISTORY на узел), выключение: DATA_LINEAGE_ENABLED=off.
    """
    raw = (os.getenv("DATA_LINEAGE_ENABLED", "on") or "on").strip().lower()
    return raw in ("1", "true", "on", "yes", "enabled")


@dataclass
class LineageEvent:
    stage: str
    actor: str
    op: str
    ts: str
    note: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "stage": self.stage, "actor": self.actor, "op": self.op,
            "ts": self.ts, "note": self.note,
        }


@dataclass
class LineageRecord:
    created_by: str = ""
    pipeline_id: str = ""
    origin_stage: str = ""
    created_at: str = ""
    history: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "created_by": self.created_by,
            "pipeline_id": self.pipeline_id,
            "origin_stage": self.origin_stage,
            "created_at": self.created_at,
            "history": list(self.history),
        }


def get_lineage(props: dict) -> LineageRecord:
    """Прочитать lineage из props. Никогда не raises."""
    if not isinstance(props, dict):
        return LineageRecord()
    raw = props.get(LINEAGE_KEY)
    if not isinstance(raw, dict):
        return LineageRecord()
    hist = raw.get("history")
    return LineageRecord(
        created_by=str(raw.get("created_by", "")),
        pipeline_id=str(raw.get("pipeline_id", "")),
        origin_stage=str(raw.get("origin_stage", "")),
        created_at=str(raw.get("created_at", "")),
        history=list(hist) if isinstance(hist, list) else [],
    )


def stamp_props(
    props: dict,
    *,
    created_by: str,
    pipeline_id: str,
    stage: str,
    op: str = "create",
    note: str = "",
    clock: ClockFn | None = None,
) -> dict:
    """Проставить/дополнить lineage. Возвращает НОВЫЙ dict. Не raises.

    Идемпотентность: первый stamp фиксирует created_by/pipeline_id/
    origin_stage; последующие НЕ перетирают их, а только добавляют
    событие в bounded history. Так трассируется вся цепочка
    трансформаций, а «кто создал» остаётся неизменным.
    """
    if not isinstance(props, dict):
        return props
    now = (clock or _default_clock)()
    out = dict(props)

    existing = out.get(LINEAGE_KEY)
    first = not isinstance(existing, dict) or not existing.get("created_by")

    if first:
        rec = {
            "created_by": str(created_by),
            "pipeline_id": str(pipeline_id),
            "origin_stage": str(stage),
            "created_at": now,
            "history": [],
        }
    else:
        rec = {
            "created_by": str(existing.get("created_by", created_by)),
            "pipeline_id": str(existing.get("pipeline_id", pipeline_id)),
            "origin_stage": str(existing.get("origin_stage", stage)),
            "created_at": str(existing.get("created_at", now)),
            "history": list(existing.get("history") or []),
        }

    event = LineageEvent(
        stage=str(stage), actor=str(created_by), op=str(op),
        ts=now, note=str(note),
    ).to_dict()
    rec["history"].append(event)
    # bounded: держим последние N
    if len(rec["history"]) > _MAX_HISTORY:
        rec["history"] = rec["history"][-_MAX_HISTORY:]

    out[LINEAGE_KEY] = rec
    # плоские зеркала для дешёвой фильтрации в графе
    out["created_by"] = rec["created_by"]
    out["pipeline_id"] = rec["pipeline_id"]
    return out


def append_event(
    props: dict,
    *,
    stage: str,
    actor: str,
    op: str,
    note: str = "",
    clock: ClockFn | None = None,
) -> dict:
    """Добавить событие трансформации к существующему lineage.

    Если lineage ещё не проставлен — создаёт его (actor как
    created_by). Не raises, bounded, возвращает новый dict.
    """
    return stamp_props(
        props,
        created_by=actor,
        pipeline_id=get_lineage(props).pipeline_id or "",
        stage=stage,
        op=op,
        note=note,
        clock=clock,
    )


def trace(props: dict) -> list[dict]:
    """Упорядоченная история трансформаций узла. Не raises."""
    return get_lineage(props).history


__all__ = [
    "LINEAGE_KEY",
    "LineageEvent",
    "LineageRecord",
    "lineage_enabled_from_env",
    "get_lineage",
    "stamp_props",
    "append_event",
    "trace",
]
