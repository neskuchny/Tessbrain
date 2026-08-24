# -*- coding: utf-8 -*-
"""
InsightStore — персистенс ночных инсайтов (docs/CAPABILITY_READINESS.md, B6).

ПРОБЛЕМА: NightProcessor генерирует инсайты (CONNECTION/PATTERN/RISK/
OPPORTUNITY/ANOMALY/TREND с приоритетами и evidence), но они жили только в
RAM-кэше процесса и в разовой отправке в каналы — ИСТОРИИ нет, «проактивный
консультант» забывал собственные находки.

Решение по паттерну примитивов памяти: per-user JSON-стор (tenant_paths),
идемпотентность по insight_id (повторный ночной прогон не задваивает),
unread-маркировка, фильтры по приоритету/типу/дате. Чистый stdlib.
"""
from __future__ import annotations

import datetime
import json
import os
import tempfile
from typing import List, Optional


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stable_insight_id(insight: dict) -> str:
    """Детерминированный id по содержимому (тип+заголовок+описание).

    Для продуцентов без собственного стабильного id (агрегатор ленты,
    proactive-скан с uuid4 на каждый прогон): одинаковая находка при
    повторном прогоне НЕ задваивается в сторе."""
    import hashlib
    canon = json.dumps({
        "t": insight.get("insight_type") or insight.get("type") or "",
        "h": (insight.get("title") or "")[:200],
        "d": (insight.get("description") or "")[:400],
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(canon.encode("utf-8", errors="ignore")).hexdigest()[:16]


class InsightStore:
    """Хранилище инсайтов. Записи — dict (NightInsight.to_dict())."""

    def __init__(self, persist_path: Optional[str] = None):
        self._path = persist_path
        self._items: dict = {}        # insight_id → dict
        # M4 (вторая волна): SQLite-KV за флагом enable_sqlite_stores.
        self._db = None
        self._txn = None
        if persist_path:
            try:
                from backend.core.store.sqlite_records import (
                    SqliteKVStore,
                    sqlite_path_for,
                    sqlite_stores_enabled,
                )
                if sqlite_stores_enabled():
                    self._db = SqliteKVStore(sqlite_path_for(persist_path),
                                             table="insights")
                    self._db.migrate_from_json(
                        persist_path,
                        lambda root: {d.get("insight_id") or d.get("id"): d
                                      for d in (root.get("insights") or [])
                                      if d.get("insight_id") or d.get("id")})
            except Exception:
                self._db = None
        if persist_path:
            self._load()

    def _locked(self, fn):
        """M1: мутация под локом с чистым reload. SQLite-режим — exclusive-
        транзакция вместо flock."""
        if not self._path:
            return fn()
        if self._db is not None:
            with self._db.exclusive() as txn:
                self._txn = txn
                try:
                    self._items = txn.load_all()
                    return fn()
                finally:
                    self._txn = None
        from backend.core.store.tenant_io import file_lock
        with file_lock(self._path):
            self._items = {}
            self._load()
            return fn()

    def add(self, insights: List[dict]) -> dict:
        """Сохранить пачку инсайтов. Идемпотентно по insight_id. M1: под локом.

        В ответе `new_ids` — id реально добавленных (для доставки только
        нового, без повторов)."""
        def _impl():
            added, skipped = 0, 0
            new_ids: List[str] = []
            for ins in insights or []:
                d = dict(ins)
                iid = d.get("insight_id") or d.get("id")
                if not iid:
                    skipped += 1
                    continue
                if iid in self._items:
                    skipped += 1
                    continue
                d.setdefault("stored_at", _now_iso())
                d.setdefault("read", False)
                self._items[iid] = d
                added += 1
                new_ids.append(str(iid))
            if added:
                self._save()
            return {"added": added, "skipped": skipped,
                    "total": len(self._items), "new_ids": new_ids}
        result = self._locked(_impl)
        # Wow-screen real-time pulse (issue #110 wow-step 2): каждый новый
        # ночной insight → push на /ws/signals → HomeTab подсветится без F5.
        # Fire-and-forget — telemetry не должна ронять insight_store.add().
        if result.get("added"):
            try:
                from backend.api.websocket.signals_ws import publish_signal
                # Пульс, а не шторм: при массовом добавлении (ночь/скан) сотни
                # событий переполняли WS-очередь («Signals queue full» флудом).
                # Для «подсветить без F5» хватает нескольких.
                _sent = 0
                for ins in insights or []:
                    if _sent >= 5:
                        break
                    iid = ins.get("insight_id") or ins.get("id")
                    if iid in result.get("new_ids", []):
                        _sent += 1
                        publish_signal(
                            "insight_added",
                            {
                                "id": iid,
                                "type": ins.get("type") or ins.get("insight_type", "unknown"),
                                "title": ins.get("title", ""),
                                "priority": ins.get("priority", "medium"),
                            },
                            tenant_id=ins.get("tenant_id"),
                        )
            except Exception:
                pass
        return result

    def list(self, *, unread_only: bool = False, priority: Optional[str] = None,
             insight_type: Optional[str] = None, since: Optional[str] = None,
             limit: int = 50) -> List[dict]:
        """Инсайты, новые сверху. Фильтры опциональны."""
        out = []
        for d in self._items.values():
            if unread_only and d.get("read"):
                continue
            if priority and str(d.get("priority", "")).lower() != priority.lower():
                continue
            if insight_type and str(d.get("insight_type", d.get("type", ""))).lower() \
                    != insight_type.lower():
                continue
            if since and str(d.get("stored_at", "")) < since:
                continue
            out.append(dict(d))
        out.sort(key=lambda d: str(d.get("stored_at", "")), reverse=True)
        return out[:max(1, limit)]

    def react(self, insight_id: str, reaction: str) -> bool:
        """Реакция пользователя: useful | declined (порт анти-повтора из
        проактивного диспетчера meetflow: отклонённое больше не показываем,
        а источники с частыми отказами топятся в ранжировании)."""
        if reaction not in ("useful", "declined"):
            return False

        def _impl():
            d = self._items.get(insight_id)
            if not d:
                return False
            d["reaction"] = reaction
            d["reacted_at"] = _now_iso()
            d["read"] = True
            self._save()
            return True
        return self._locked(_impl)

    def declined_counts_by_source(self) -> dict:
        """{source: число отказов} — для пенальти в ранжировании ленты."""
        out: dict = {}
        for d in self._items.values():
            if d.get("reaction") == "declined":
                src = d.get("source", "unknown")
                out[src] = out.get(src, 0) + 1
        return out

    def mark_read(self, insight_id: str) -> bool:
        def _impl():
            d = self._items.get(insight_id)
            if not d:
                return False
            d["read"] = True
            d["read_at"] = _now_iso()
            self._save()
            return True
        return self._locked(_impl)

    def counts(self) -> dict:
        unread = sum(1 for d in self._items.values() if not d.get("read"))
        return {"total": len(self._items), "unread": unread}

    # -- outcome-проверка («помогло ли» через N дней) ---------------------
    def check_outcomes(self, fresh_ids: set, *, min_age_days: int = 7) -> dict:
        """Сверить старые инсайты со СВЕЖИМ сканом (структурно, без LLM).

        Идея: id инсайта детерминирован по содержимому, а сканеры
        детерминированы — значит, если инсайт старше min_age_days СНОВА
        всплыл в свежем скане → проблема "persists" (persist_checks++);
        перестал всплывать → "resolved". Resolved может вернуться в
        persists, если проблема возникла снова (честно).

        fresh_ids: id ВСЕХ находок текущего скана (не только новых).
        Возвращает {checked, persists, resolved}.
        """
        from datetime import datetime, timezone

        def _impl():
            now = datetime.now(timezone.utc)
            checked = persists = resolved = 0
            changed = False
            for iid, d in self._items.items():
                stored = d.get("stored_at", "")
                try:
                    dt = datetime.fromisoformat(str(stored).replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                except (ValueError, AttributeError):
                    continue
                if (now - dt).days < min_age_days:
                    continue
                checked += 1
                new_outcome = "persists" if iid in fresh_ids else "resolved"
                if new_outcome == "persists":
                    persists += 1
                    d["persist_checks"] = int(d.get("persist_checks", 0)) + 1
                else:
                    resolved += 1
                if d.get("outcome") != new_outcome or new_outcome == "persists":
                    d["outcome"] = new_outcome
                    d["outcome_checked_at"] = _now_iso()
                    changed = True
            if changed:
                self._save()
            return {"checked": checked, "persists": persists,
                    "resolved": resolved}
        return self._locked(_impl)

    # -- персистенс -----------------------------------------------------
    def _save(self) -> None:
        if not self._path:
            return
        if self._db is not None:
            try:
                if self._txn is not None:        # внутри exclusive-транзакции
                    self._txn.replace_all(self._items)
                else:
                    with self._db.exclusive() as txn:
                        txn.replace_all(self._items)
            except Exception:
                pass
            return
        # M5-fix: общий хелпер добавляет fsync файла и директории
        from backend.core.store.tenant_io import atomic_write_json
        atomic_write_json(self._path,
                          {"insights": list(self._items.values())})

    def _load(self) -> None:
        if self._db is not None:
            try:
                self._items = self._db.load_all()
            except Exception:
                self._items = {}
            return
        if not (self._path and os.path.exists(self._path)):
            return
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            for d in data.get("insights", []):
                iid = d.get("insight_id") or d.get("id")
                if iid:
                    self._items[iid] = d
        except Exception:
            self._items = {}
