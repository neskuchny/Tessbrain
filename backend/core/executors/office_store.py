# -*- coding: utf-8 -*-
"""Хранилище прогонов офисных задач: финал человека должен куда-то лечь.

Петля office_loop возвращает отчёт, но без хранилища он жил бы только в
HTTP-ответе — и «финальное решение за человеком» осталось бы словами:
некуда нажать «принято». Здесь прогон сохраняется, ждёт человека и
закрывается его явным решением. Закрытый не переоткрывается.

Per-user JSON, как у остальных примитивов (file_lock + atomic_write),
кап 200 прогонов.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_RUNS = 200

STATUS_AWAITING = "awaiting_human"
STATUS_EXHAUSTED = "returned_exhausted"
STATUS_FAILED = "failed"
STATUS_CLOSED = "closed"        # человек принял
STATUS_CANCELLED = "cancelled"  # человек отклонил


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(user_id: str) -> str:
    try:
        from backend.core.store.tenant_paths import _DATA_ROOT
        root = str(_DATA_ROOT)
    except Exception:
        root = os.getenv("TESSENT_DATA_DIR", "data")
    safe = "".join(c for c in str(user_id) if c.isalnum() or c in "-_")
    return os.path.join(root, "office_runs", f"{safe or 'default'}.json")


class OfficeRunStore:
    def __init__(self, user_id: str, path: Optional[str] = None):
        self._user = str(user_id)
        self._path = path or _path(user_id)

    def _load(self) -> List[dict]:
        try:
            if os.path.exists(self._path):
                with open(self._path, encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    return list(d.get("runs") or [])
        except Exception:
            logger.warning("office run store load failed", exc_info=True)
        return []

    def _write(self, runs: List[dict]) -> None:
        from backend.core.store.tenant_io import atomic_write_json, file_lock
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        with file_lock(self._path):
            atomic_write_json(self._path, {"runs": runs[-MAX_RUNS:]})

    # ── операции ────────────────────────────────────────────────────────
    def add(self, *, task_text: str, backend: str,
            report: Dict[str, Any]) -> dict:
        run = {
            "id": f"or_{uuid.uuid4().hex[:12]}",
            "task_text": task_text,
            "backend": backend,
            "status": report.get("status") or STATUS_FAILED,
            "final_text": report.get("final_text") or "",
            "final_verdict": report.get("final_verdict") or {},
            "attempts": report.get("attempts") or [],
            "created_at": _now(),
            "closed_at": "",
            "closed_by": "",
            "close_note": "",
        }
        runs = self._load()
        runs.append(run)
        self._write(runs)
        return run

    def list(self, *, limit: int = 50) -> List[dict]:
        runs = self._load()
        return sorted(runs, key=lambda r: r.get("created_at", ""),
                      reverse=True)[:limit]

    def get(self, run_id: str) -> Optional[dict]:
        for r in self._load():
            if r.get("id") == run_id:
                return r
        return None

    def close(self, run_id: str, *, closed_by: str, approve: bool,
              note: str = "") -> Dict[str, Any]:
        """Финал человека. Закрытый и отклонённый не переоткрываются."""
        runs = self._load()
        for r in runs:
            if r.get("id") != run_id:
                continue
            if r.get("status") in (STATUS_CLOSED, STATUS_CANCELLED):
                return {"ok": False,
                        "error": "прогон уже закрыт — решение не меняется"}
            if r.get("status") == STATUS_FAILED:
                return {"ok": False,
                        "error": "исполнитель не дал результата — "
                                 "принимать нечего, запустите заново"}
            r["status"] = STATUS_CLOSED if approve else STATUS_CANCELLED
            r["closed_at"] = _now()
            r["closed_by"] = str(closed_by)
            r["close_note"] = str(note or "")[:500]
            self._write(runs)
            return {"ok": True, "run": r}
        return {"ok": False, "error": "прогон не найден"}


__all__ = ["OfficeRunStore", "STATUS_AWAITING", "STATUS_CANCELLED",
           "STATUS_CLOSED", "STATUS_EXHAUSTED", "STATUS_FAILED"]
