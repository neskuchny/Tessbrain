# -*- coding: utf-8 -*-
"""Holding store — холдинг владеет N организациями (консоль холдинга, Слой 1).

`data/holdings.json`:
    {holding_id: {id, name, owner_user_id, org_ids: [...], created_at}}

Холдинг НЕ смешивает данные корпораций — только группирует их и даёт владельцу
(директору) наблюдательный доступ. Каждая org остаётся изолированной (свой мозг,
RLS). См. docs/HOLDING_CONSOLE.md.

Конкурентность как в membership/org_store: in-process threading.Lock +
atomic write (tmp + os.replace)."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_DATA_ROOT = Path(__file__).resolve().parents[2] / "data"


def _path() -> str:
    return str(_DATA_ROOT / "holdings.json")


def _pg() -> bool:
    """Postgres-бэкенд стора доступов включён? (D1a; иначе — файл)."""
    try:
        from backend.core.ingest import access_repo
        return access_repo.enabled()
    except Exception:
        return False


import contextlib as _contextlib


@_contextlib.contextmanager
def _guard():
    """Сериализация writer'ов: in-process Lock (файл) или xact advisory-лок +
    снапшот транзакции (Postgres). В PG-режиме _load/_atomic_write внутри блока
    маршрутизируются на ту же транзакцию."""
    if _pg():
        from backend.core.ingest import access_repo
        with access_repo.locked_txn("holdings"):
            yield
    else:
        with _LOCK:
            yield


def _load() -> dict[str, dict[str, Any]]:
    if _pg():
        try:
            from backend.core.ingest import access_repo
            return access_repo.load_all("holdings")
        except Exception as e:  # noqa: BLE001
            logger.warning("holding_store PG load failed: %s", e)
            return {}
    p = _path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:  # noqa: BLE001
        logger.warning("holdings.json read failed: %s", e)
        return {}


def _atomic_write(data: dict[str, Any]) -> None:
    if _pg():
        from backend.core.ingest import access_repo
        access_repo.replace_all("holdings", data)
        return
    p = _path()
    parent = os.path.dirname(p) or "."
    os.makedirs(parent, exist_ok=True)
    tmp_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=parent, delete=False) as tmp:
            json.dump(data, tmp, ensure_ascii=False, indent=2, sort_keys=True)
            tmp_name = tmp.name
        os.replace(tmp_name, p)
        tmp_name = None
    finally:
        if tmp_name and os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_holding(owner_user_id: str, name: str) -> dict[str, Any]:
    """Создать холдинг. Владелец — директор (наблюдательный доступ ко всем org)."""
    hid = str(uuid.uuid4())
    rec = {"id": hid, "name": (name or "").strip() or "Холдинг",
           "owner_user_id": owner_user_id, "org_ids": [], "created_at": _now()}
    with _guard():
        data = _load()
        data[hid] = rec
        _atomic_write(data)
    return rec


def get_holding(holding_id: str) -> Optional[dict[str, Any]]:
    return _load().get(holding_id)


def get_holdings_for_owner(owner_user_id: str) -> list[dict[str, Any]]:
    return [h for h in _load().values()
            if h.get("owner_user_id") == owner_user_id]


def is_owner(holding_id: str, user_id: str) -> bool:
    h = get_holding(holding_id)
    return bool(h and h.get("owner_user_id") == user_id)


def owns_org(user_id: str, org_id: str) -> bool:
    """Владеет ли user хотя бы одним холдингом, содержащим org_id."""
    for h in _load().values():
        if h.get("owner_user_id") == user_id and org_id in (h.get("org_ids") or []):
            return True
    return False


def add_org(holding_id: str, org_id: str,
            data_policy: str = "full") -> Optional[dict[str, Any]]:
    """Привязать org. data_policy — сегрегация портфеля («китайская стена»):
      full      — владелец холдинга читает отчёт/задаёт вопросы к мозгу org;
      aggregate — только агрегаты (счётчики overview/rollup), содержимое
                  мозга через холдинг НЕ читается.
    """
    policy = data_policy if data_policy in ("full", "aggregate") else "full"
    with _guard():
        data = _load()
        h = data.get(holding_id)
        if not h:
            return None
        orgs = h.setdefault("org_ids", [])
        if org_id not in orgs:
            orgs.append(org_id)
        h.setdefault("org_policies", {})[org_id] = policy
        _atomic_write(data)
        return h


def org_policy(holding_id: str, org_id: str) -> str:
    """Политика данных org внутри холдинга (нет записи → full, прежнее
    поведение)."""
    h = get_holding(holding_id) or {}
    pol = (h.get("org_policies") or {}).get(org_id)
    return pol if pol in ("full", "aggregate") else "full"


def remove_org(holding_id: str, org_id: str) -> Optional[dict[str, Any]]:
    with _guard():
        data = _load()
        h = data.get(holding_id)
        if not h:
            return None
        if org_id in (h.get("org_ids") or []):
            h["org_ids"] = [o for o in h["org_ids"] if o != org_id]
            (h.get("org_policies") or {}).pop(org_id, None)
            _atomic_write(data)
        return h
