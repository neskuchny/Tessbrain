# -*- coding: utf-8 -*-
"""Регулярный inbound-sync датасетов (A1 enterprise-роадмапа).

«Тянуть данные из систем по расписанию, а не по кнопке». Строится ПОВЕРХ уже
готового:
  - `refresh_dataset(user_id, dataset_id)` — единица синка (тянет источник
    crm/url, перестраивает профиль+онтологию, обновляет метрики; refresh
    идемпотентен — строки заменяются, метрики через replace_source_points);
  - `is_due_spec(spec, last, now)` из board/triggers — та же семантика
    расписаний interval/daily/weekly, что у процесс-досок;
  - APScheduler в automations/scheduler — периодический драйвер.

Здесь добавляем на датасет:
  - `refresh_schedule` — спека расписания (когда тянуть);
  - `sync` — НАБЛЮДАЕМОСТЬ: last_at/status/rows/error/duration_ms — чтобы
    синк не был «молчаливым» (застрявший источник виден в статусе и логах).

Инкрементальность (updated_since-курсор) — отдельная фаза A1.2 (требует
доработки коннекторов под каждый провайдер). Здесь — полный refresh по
расписанию с дедупом на стороне refresh и явной наблюдаемостью.

Never-raise на уровне драйвера: один упавший датасет не рушит проход.
За флагом `enable_dataset_sync` (default off).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_VALID_KINDS = ("interval", "daily", "weekly")
# источники, которые вообще умеют тянуть свежее (refresh_dataset)
_SYNCABLE_SOURCE_KINDS = ("crm", "url")


def sync_enabled() -> bool:
    """Глобальный флаг регулярного синка (default off).

    ENV `ENABLE_DATASET_SYNC=1` ИЛИ feature-flag `enable_dataset_sync`."""
    if os.getenv("ENABLE_DATASET_SYNC", "").strip().lower() in ("1", "true", "on", "yes"):
        return True
    try:
        from backend.core.config.feature_flags import get_feature_flags
        return bool(getattr(get_feature_flags(), "enable_dataset_sync", False))
    except Exception:
        return False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_schedule(spec: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Проверить и нормализовать спеку расписания (та же форма, что у
    board-триггеров). Бросает ValueError на мусор.

      interval: {"kind":"interval","minutes":N}      (N ≥ 5)
      daily:    {"kind":"daily","time":"HH:MM"}
      weekly:   {"kind":"weekly","time":"HH:MM","weekday":0..6}  (0=пн)
    """
    if not isinstance(spec, dict):
        raise ValueError("schedule должен быть объектом")
    kind = str(spec.get("kind") or "").strip().lower()
    if kind not in _VALID_KINDS:
        raise ValueError(f"kind должен быть одним из {_VALID_KINDS}")
    out: Dict[str, Any] = {"kind": kind}
    if kind == "interval":
        try:
            minutes = int(spec.get("minutes"))
        except (TypeError, ValueError):
            raise ValueError("interval требует целое minutes")
        if minutes < 5:
            raise ValueError("минимальный интервал — 5 минут")
        out["minutes"] = minutes
    else:  # daily / weekly
        t = str(spec.get("time") or "09:00")
        try:
            h, m = t.split(":")
            h, m = int(h), int(m)
            if not (0 <= h < 24 and 0 <= m < 60):
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("time в формате HH:MM (0-23:0-59)")
        out["time"] = f"{h:02d}:{m:02d}"
        if kind == "weekly":
            try:
                wd = int(spec.get("weekday", 0))
            except (TypeError, ValueError):
                raise ValueError("weekday — целое 0..6 (0=пн)")
            if not (0 <= wd <= 6):
                raise ValueError("weekday — 0..6 (0=пн)")
            out["weekday"] = wd
    return out


def set_schedule(user_id: str, dataset_id: str,
                 spec: Dict[str, Any]) -> Dict[str, Any]:
    """Назначить датасету расписание синка. Только для источников, умеющих
    refresh (crm/url). Возвращает {success, schedule|error}."""
    from backend.core.ontology.dataset_service import registry_for_user
    reg = registry_for_user(user_id)
    rec = reg.get(dataset_id)
    if not rec:
        return {"success": False, "error": "датасет не найден"}
    kind = (rec.get("source") or {}).get("kind")
    if kind not in _SYNCABLE_SOURCE_KINDS:
        return {"success": False,
                "error": f"источник «{kind}» не поддерживает авто-синк "
                         f"(нужен один из {_SYNCABLE_SOURCE_KINDS})"}
    try:
        norm = normalize_schedule(spec)
    except ValueError as e:
        return {"success": False, "error": str(e)}
    reg.update(dataset_id, {"refresh_schedule": norm})
    logger.info("dataset sync: расписание задано (%s) для %s/%s",
                norm.get("kind"), user_id[:8], dataset_id)
    return {"success": True, "schedule": norm}


def clear_schedule(user_id: str, dataset_id: str) -> Dict[str, Any]:
    """Снять авто-синк с датасета (запись состояния остаётся для истории)."""
    from backend.core.ontology.dataset_service import registry_for_user
    reg = registry_for_user(user_id)
    rec = reg.get(dataset_id)
    if not rec:
        return {"success": False, "error": "датасет не найден"}
    reg.update(dataset_id, {"refresh_schedule": None})
    return {"success": True}


def _record_sync(reg, dataset_id: str, *, status: str,
                 rows: Optional[int] = None, error: Optional[str] = None,
                 duration_ms: Optional[int] = None, at: Optional[str] = None,
                 trigger: str = "schedule") -> Dict[str, Any]:
    """Записать наблюдаемость последнего синка в запись датасета."""
    state = {
        "last_at": at or _now().isoformat(),
        "last_status": status,           # ok | error | empty | skipped
        "last_rows": rows,
        "last_error": (str(error)[:300] if error else None),
        "last_duration_ms": duration_ms,
        "last_trigger": trigger,
    }
    try:
        reg.update(dataset_id, {"sync": state})
    except Exception:
        logger.debug("sync state persist failed", exc_info=True)
    return state


async def sync_now(user_id: str, dataset_id: str, *,
                   trigger: str = "manual", fetch_fn=None) -> Dict[str, Any]:
    """Синхронизировать один датасет сейчас + записать наблюдаемость.
    Возвращает {success, status, rows?, error?}."""
    from backend.core.ontology.dataset_service import (
        refresh_dataset, registry_for_user,
    )
    reg = registry_for_user(user_id)
    rec = reg.get(dataset_id)
    if not rec:
        return {"success": False, "status": "error", "error": "датасет не найден"}
    started = _now()
    try:
        res = await refresh_dataset(user_id, dataset_id, fetch_fn)
    except Exception as e:  # never-raise: фиксируем как error, не роняем проход
        logger.warning("dataset sync failed for %s/%s: %s",
                       user_id[:8], dataset_id, e)
        dur = int((_now() - started).total_seconds() * 1000)
        _record_sync(reg, dataset_id, status="error", error=str(e),
                     duration_ms=dur, trigger=trigger)
        return {"success": False, "status": "error", "error": str(e)}
    dur = int((_now() - started).total_seconds() * 1000)
    if not res.get("success"):
        _record_sync(reg, dataset_id, status="error",
                     error=res.get("error"), duration_ms=dur, trigger=trigger)
        return {"success": False, "status": "error",
                "error": res.get("error")}
    rows_total = ((res.get("dataset") or {}).get("rows_total")
                  or (res.get("dataset") or {}).get("profile", {}).get("rows_total"))
    status = "ok" if rows_total else "empty"
    _record_sync(reg, dataset_id, status=status, rows=rows_total,
                 duration_ms=dur, trigger=trigger)
    return {"success": True, "status": status, "rows": rows_total}


def sync_status(user_id: str) -> List[Dict[str, Any]]:
    """Датасеты с авто-синком + их последнее состояние (для UI-панели
    «Источники данных»)."""
    from backend.core.ontology.dataset_service import registry_for_user
    reg = registry_for_user(user_id)
    out: List[Dict[str, Any]] = []
    for rec in reg.list():
        sched = rec.get("refresh_schedule")
        if not sched:
            continue
        out.append({
            "dataset_id": rec.get("dataset_id"),
            "title": rec.get("title"),
            "source": rec.get("source"),
            "schedule": sched,
            "sync": rec.get("sync") or {},
            "refreshed_at": rec.get("refreshed_at"),
        })
    return out


def _iter_user_ids() -> List[str]:
    """Все пользователи с реестром датасетов (по каталогам data/datasets/*)."""
    try:
        from backend.core.store.tenant_paths import _DATA_ROOT
        base = _DATA_ROOT / "datasets"
        if not base.is_dir():
            return []
        return [p.name for p in base.iterdir()
                if p.is_dir() and (p / "index.json").exists()]
    except Exception:
        logger.debug("dataset sync: user enumeration failed", exc_info=True)
        return []


async def run_due_dataset_syncs(now: Optional[datetime] = None) -> Dict[str, Any]:
    """Драйвер: обойти датасеты всех пользователей и синхронизировать
    назревшие по расписанию. Best-effort; один упавший датасет не рушит
    проход. {enabled, scanned, ran, errors}."""
    if not sync_enabled():
        return {"enabled": False, "scanned": 0, "ran": 0, "errors": 0}
    now = now or _now()
    from backend.core.board.triggers import is_due_spec
    from backend.core.ontology.dataset_service import registry_for_user

    scanned = ran = errors = 0
    for uid in _iter_user_ids():
        try:
            reg = registry_for_user(uid)
            records = reg.list()
        except Exception:
            logger.debug("dataset sync: registry load failed for %s", uid[:8],
                         exc_info=True)
            continue
        for rec in records:
            sched = rec.get("refresh_schedule")
            if not sched:
                continue
            scanned += 1
            last_at = (rec.get("sync") or {}).get("last_at")
            try:
                due = is_due_spec(sched, last_at, now)
            except Exception:
                due = False
            if not due:
                continue
            res = await sync_now(uid, rec.get("dataset_id"), trigger="schedule")
            if res.get("success"):
                ran += 1
            else:
                errors += 1
    if ran or errors:
        logger.info("🔄 dataset sync: синхронизировано %d, ошибок %d (из %d по расписанию)",
                    ran, errors, scanned)
    return {"enabled": True, "scanned": scanned, "ran": ran, "errors": errors}


__all__ = ["sync_enabled", "normalize_schedule", "set_schedule",
           "clear_schedule", "sync_now", "sync_status", "run_due_dataset_syncs"]
