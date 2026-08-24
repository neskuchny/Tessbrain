# -*- coding: utf-8 -*-
"""Memory corrections store (issue #112 T-8).

Хранит очередь human-review для низко-confidence attribution + историю
исправлений. SQLite backend (как usage_tracker) для dev/test; на проде
зеркалируется в Postgres через migrations/262_memory_corrections.sql.

Workflow:
  1. graph_builder создаёт Task/Decision с needs_human_review=True →
     enqueue() кладёт в очередь
  2. Review queue UI (T-10) показывает pending → list_pending()
  3. Человек подтверждает/исправляет → apply_correction()
  4. История исправлений → для active learning (статистика 'LLM ошибся')

Дедупликация: один (tenant, entity_type, entity_id) не дублируется в
очереди (UNIQUE-индекс эмулируется проверкой при enqueue).
"""
from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class CorrectionsStore:
    """SQLite-хранилище memory_corrections. Singleton через
    get_corrections_store()."""

    _instance: Optional["CorrectionsStore"] = None

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            data_dir = Path(__file__).parent.parent.parent.parent / "data"
            data_dir.mkdir(exist_ok=True)
            db_path = str(data_dir / "memory_corrections.db")
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_corrections (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    original_attribution TEXT,
                    original_confidence TEXT,
                    review_reason TEXT,
                    suggested_correction TEXT,
                    correction TEXT,
                    correction_action TEXT,
                    needs_review INTEGER DEFAULT 1,
                    reviewed_by TEXT,
                    reviewed_at TEXT,
                    title TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mc_tenant_review "
                "ON memory_corrections(tenant_id, needs_review)")
            # Эмуляция UNIQUE для дедупа активных записей
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_mc_entity_active "
                "ON memory_corrections(tenant_id, entity_type, entity_id) "
                "WHERE needs_review = 1")
            conn.commit()

    def enqueue(
        self,
        *,
        entity_type: str,
        entity_id: str,
        tenant_id: Optional[str] = None,
        original_attribution: Optional[str] = None,
        original_confidence: Optional[str] = None,
        review_reason: Optional[str] = None,
        suggested_correction: Optional[str] = None,
        title: Optional[str] = None,
    ) -> Optional[str]:
        """Добавить запись в очередь review. Идемпотентно — если активная
        запись для (tenant, entity_type, entity_id) уже есть, не дублирует.

        Возвращает id записи (новой или существующей) либо None при ошибке.
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                # Проверка дедупа
                existing = conn.execute(
                    "SELECT id FROM memory_corrections WHERE "
                    "COALESCE(tenant_id,'')=COALESCE(?,'') AND entity_type=? "
                    "AND entity_id=? AND needs_review=1",
                    (tenant_id, entity_type, entity_id),
                ).fetchone()
                if existing:
                    return existing["id"]

                cid = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO memory_corrections "
                    "(id, tenant_id, entity_type, entity_id, "
                    " original_attribution, original_confidence, review_reason, "
                    " suggested_correction, title, needs_review, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                    (cid, tenant_id, entity_type, entity_id,
                     original_attribution, original_confidence, review_reason,
                     suggested_correction, title,
                     datetime.now(timezone.utc).isoformat()),
                )
                conn.commit()
                return cid
        except Exception as e:
            logger.warning("CorrectionsStore.enqueue failed: %s", e)
            return None

    def list_pending(
        self, tenant_id: Optional[str] = None, limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Список ожидающих review (новые сверху)."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                if tenant_id:
                    rows = conn.execute(
                        "SELECT * FROM memory_corrections WHERE needs_review=1 "
                        "AND COALESCE(tenant_id,'')=? ORDER BY created_at DESC "
                        "LIMIT ?", (tenant_id, int(limit))).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM memory_corrections WHERE needs_review=1 "
                        "ORDER BY created_at DESC LIMIT ?",
                        (int(limit),)).fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            logger.warning("CorrectionsStore.list_pending failed: %s", e)
            return []

    def apply_correction(
        self,
        correction_id: str,
        *,
        action: str,                       # confirm | fix | delete | skip
        correction: Optional[str] = None,  # новое значение (для fix)
        reviewed_by: Optional[str] = None,
    ) -> bool:
        """Применить human-решение. Закрывает запись (needs_review=0).

        action:
          - confirm: система была права, attribution подтверждена
          - fix: человек дал correction (новое имя assignee)
          - delete: эта attribution неверна, удалить связь
          - skip: отложить (остаётся в очереди — needs_review=1)
        """
        if action not in ("confirm", "fix", "delete", "skip"):
            logger.warning("apply_correction: unknown action %r", action)
            return False
        try:
            with sqlite3.connect(self.db_path) as conn:
                if action == "skip":
                    # Не закрываем, только помечаем что смотрели
                    conn.execute(
                        "UPDATE memory_corrections SET reviewed_by=? "
                        "WHERE id=?", (reviewed_by, correction_id))
                else:
                    conn.execute(
                        "UPDATE memory_corrections SET needs_review=0, "
                        "correction_action=?, correction=?, reviewed_by=?, "
                        "reviewed_at=? WHERE id=?",
                        (action, correction, reviewed_by,
                         datetime.now(timezone.utc).isoformat(),
                         correction_id))
                conn.commit()
                return True
        except Exception as e:
            logger.warning("CorrectionsStore.apply_correction failed: %s", e)
            return False

    def stats(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """Статистика очереди для метрик / active learning."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                where = "WHERE COALESCE(tenant_id,'')=?" if tenant_id else ""
                params: tuple = (tenant_id,) if tenant_id else ()
                pending = conn.execute(
                    f"SELECT COUNT(*) AS c FROM memory_corrections "
                    f"{where}{' AND' if where else 'WHERE'} needs_review=1",
                    params).fetchone()["c"]
                # Разбивка по action для уже-разрешённых
                by_action = {}
                rows = conn.execute(
                    f"SELECT correction_action AS a, COUNT(*) AS c FROM "
                    f"memory_corrections {where}{' AND' if where else 'WHERE'} "
                    f"needs_review=0 GROUP BY correction_action", params
                ).fetchall()
                for r in rows:
                    if r["a"]:
                        by_action[r["a"]] = r["c"]
                # Точность системы: confirm / (confirm + fix + delete)
                resolved = sum(by_action.get(a, 0)
                               for a in ("confirm", "fix", "delete"))
                accuracy = (
                    round(100.0 * by_action.get("confirm", 0) / resolved, 1)
                    if resolved else None
                )
                return {
                    "tenant_id": tenant_id,
                    "pending": pending,
                    "by_action": by_action,
                    "system_accuracy_pct": accuracy,
                }
        except Exception as e:
            logger.warning("CorrectionsStore.stats failed: %s", e)
            return {"tenant_id": tenant_id, "pending": 0, "by_action": {}}

    def get_learned_aliases(
        self, tenant_id: Optional[str] = None, min_count: int = 1,
    ) -> Dict[str, str]:
        """Active learning (issue #112 продолжение): извлечь алиасы из
        истории fix-исправлений.

        Когда человек правит attribution X → Y (action=fix), это сигнал
        что 'X на самом деле Y' для этого тенанта. Например прозвище
        'Чарли' → 'Максим Иванов', или опечатка 'Иваннов' → 'Иванов'.

        Возвращает {lower(original): correction} — мапу для авто-резолва.
        Учитываем только пары, встретившиеся >= min_count раз (защита от
        случайных одноразовых правок). При конфликте (один X правился в
        разные Y) берём самый частый Y.

        Используется в graph_builder T-5 validation: если assignee не в
        attendees напрямую, но есть learned alias → резолвим через него.
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                where = "WHERE COALESCE(tenant_id,'')=?" if tenant_id else "WHERE 1=1"
                params: tuple = (tenant_id,) if tenant_id else ()
                rows = conn.execute(
                    f"SELECT original_attribution AS orig, correction AS corr, "
                    f"COUNT(*) AS c FROM memory_corrections {where} "
                    f"AND correction_action='fix' "
                    f"AND original_attribution IS NOT NULL AND original_attribution != '' "
                    f"AND correction IS NOT NULL AND correction != '' "
                    f"GROUP BY LOWER(original_attribution), correction "
                    f"ORDER BY c DESC", params,
                ).fetchall()
        except Exception as e:
            logger.warning("get_learned_aliases failed: %s", e)
            return {}

        # При конфликте берём самый частый (rows уже ORDER BY c DESC)
        aliases: Dict[str, str] = {}
        for r in rows:
            if r["c"] < min_count:
                continue
            key = (r["orig"] or "").strip().lower()
            if not key or key in aliases:
                continue  # первый (самый частый) побеждает
            aliases[key] = r["corr"].strip()
        return aliases


_store: Optional[CorrectionsStore] = None


def get_corrections_store() -> CorrectionsStore:
    global _store
    if _store is None:
        _store = CorrectionsStore()
    return _store
