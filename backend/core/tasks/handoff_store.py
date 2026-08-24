# -*- coding: utf-8 -*-
"""
HandoffStore — ожидающие подтверждения делегации кодинг-агентам.

Модель (по требованию): автономный запуск кодинг-агента происходит ТОЛЬКО
по явному подтверждению ПОЛЬЗОВАТЕЛЯ на конкретную задачу:
1) `coding_handoff` создаёт PENDING-запись (ТЗ + команда) и показывает её
   пользователю — НИЧЕГО не исполняя;
2) пользователь явно подтверждает (`confirm`) → запуск; или отклоняет
   (`reject`). Двойное подтверждение блокируется статусом.
Флаг enable_coding_handoff_exec остаётся ops-рубильником «исполнение
разрешено в принципе» — но сам по себе ничего не запускает.

Статусы: pending_confirmation → running → done | failed; rejected.
Хранение — per-user JSON (file_lock + atomic), паттерн остальных примитивов.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_ITEMS = 100
PENDING = "pending_confirmation"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
REJECTED = "rejected"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(user_id: str) -> str:
    from backend.core.store.tenant_paths import _DATA_ROOT, _require_uuid
    user_id = _require_uuid(user_id, "user_id")
    d = _DATA_ROOT / "handoffs"
    d.mkdir(parents=True, exist_ok=True)
    return str(d / f"{user_id}.json")


class HandoffStore:
    def __init__(self, user_id: str):
        self._path = _path(user_id)
        self._items: Dict[str, dict] = {}
        self._load()

    def _locked(self, fn):
        from backend.core.store.tenant_io import file_lock
        with file_lock(self._path):
            self._items = {}
            self._load()
            return fn()

    def create(self, proposal: dict) -> dict:
        """Создать pending-запись. Возвращает запись с id/status."""
        def _impl():
            hid = str(uuid.uuid4())[:8]
            rec = dict(proposal)
            rec.update({"id": hid, "status": PENDING, "created_at": _now()})
            self._items[hid] = rec
            if len(self._items) > MAX_ITEMS:
                # вытесняем самые старые завершённые
                by_age = sorted(self._items.values(),
                                key=lambda r: r.get("created_at", ""))
                for r in by_age:
                    if len(self._items) <= MAX_ITEMS:
                        break
                    if r.get("status") in (DONE, FAILED, REJECTED):
                        self._items.pop(r["id"], None)
            self._save()
            return rec
        return self._locked(_impl)

    def get(self, handoff_id: str) -> Optional[dict]:
        return self._items.get(handoff_id)

    def list(self, *, status: Optional[str] = None, limit: int = 20) -> List[dict]:
        items = sorted(self._items.values(),
                       key=lambda r: r.get("created_at", ""), reverse=True)
        if status:
            items = [r for r in items if r.get("status") == status]
        # без тяжёлых полей
        return [{k: v for k, v in r.items() if k != "spec_text"}
                for r in items[:limit]]

    def transition(self, handoff_id: str, from_status: str,
                   to_status: str, **fields) -> Optional[dict]:
        """Атомарный переход статуса (под локом). None если статус не совпал
        (например, двойной confirm) или записи нет."""
        def _impl():
            rec = self._items.get(handoff_id)
            if not rec or rec.get("status") != from_status:
                return None
            rec["status"] = to_status
            rec[f"{to_status}_at"] = _now()
            rec.update(fields)
            self._save()
            return dict(rec)
        return self._locked(_impl)

    def update(self, handoff_id: str, **fields) -> Optional[dict]:
        def _impl():
            rec = self._items.get(handoff_id)
            if not rec:
                return None
            rec.update(fields)
            self._save()
            return dict(rec)
        return self._locked(_impl)

    def delete(self, handoff_id: str) -> bool:
        """Убрать запись из очереди (чтобы не захламляла список). True — удалено.
        Хранилище per-user → удалять можно только своё, чужое недостижимо."""
        def _impl():
            if handoff_id in self._items:
                self._items.pop(handoff_id, None)
                self._save()
                return True
            return False
        return self._locked(_impl)

    def clear_finished(self) -> int:
        """Удалить все завершённые (done/failed/rejected). Pending/running —
        не трогаем (идут или ждут подтверждения). Возвращает число удалённых."""
        def _impl():
            done_ids = [hid for hid, r in self._items.items()
                        if r.get("status") in (DONE, FAILED, REJECTED)]
            for hid in done_ids:
                self._items.pop(hid, None)
            if done_ids:
                self._save()
            return len(done_ids)
        return self._locked(_impl)

    # -- персистенс --
    def _save(self) -> None:
        try:
            from backend.core.store.tenant_io import atomic_write_json
            atomic_write_json(self._path, {"handoffs": list(self._items.values())})
        except Exception:
            logger.warning("HandoffStore save failed", exc_info=True)

    def _load(self) -> None:
        try:
            if os.path.exists(self._path):
                with open(self._path, encoding="utf-8") as f:
                    data = json.load(f)
                for r in data.get("handoffs", []):
                    if r.get("id"):
                        self._items[r["id"]] = r
        except Exception:
            self._items = {}
