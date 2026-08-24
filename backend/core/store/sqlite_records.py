# -*- coding: utf-8 -*-
"""
SqliteRecordStore — SQLite-бэкенд для append-only сторов (аудит M4).

ПРОБЛЕМА (M4): event_log и cross_source_mentions на каждую запись
пересериализуют ВЕСЬ JSON-файл (O(n) на запись, O(n²) накопительно) — для
активного пользователя за год это мегабайтные файлы и деградация ингеста.

РЕШЕНИЕ: общая таблица append-only записей в SQLite (stdlib, без новой
инфраструктуры). Одна запись = один INSERT (O(1)); идемпотентность через
PRIMARY KEY + INSERT OR IGNORE; конкурентность процессов (granian workers +
taskiq worker на одном томе) — WAL + busy_timeout, file_lock из tenant_io
больше не нужен. Почему SQLite, а не Postgres: ноль новой инфраструктуры,
работает в тестах без сервисов, per-user файлы сохраняют раскладку
data/<store>/{user_id}.sqlite3 — бэкап app_data работает как раньше.
Postgres имеет смысл при переходе на мульти-нод (тогда этот класс — место
для замены).

СХЕМА (универсальная для событий/упоминаний):
    records(id TEXT PK, k1 TEXT, k2 TEXT, at TEXT, doc TEXT)
    - id: идемпотентный ключ записи (event_id / mention_id)
    - k1/k2: индексируемые ключи фильтрации (actor/kind или entity/source_type)
    - at: ISO-дата события (сортировка/диапазоны — даты фильтрует вызывающий,
      семантика _parse_iso сохраняется)
    - doc: полный JSON записи (источник истины при чтении)

МИГРАЦИЯ: при первом открытии, если таблица пуста и рядом лежит старый
JSON-файл — записи импортируются (bulk, одна транзакция). JSON не удаляется
(страховка/откат), но после включения флага новые записи идут ТОЛЬКО в
SQLite — откат флага без экспорта потеряет записи sqlite-периода.

Включение: флаг enable_sqlite_stores (default OFF — поведение прежнее,
JSON + file_lock).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)


def sqlite_stores_enabled() -> bool:
    try:
        from backend.core.config.feature_flags import get_feature_flags
        return bool(get_feature_flags().enable_sqlite_stores)
    except Exception:
        return False


def sqlite_path_for(persist_path: str) -> str:
    """data/event_logs/{uid}.json → data/event_logs/{uid}.sqlite3."""
    base, _ = os.path.splitext(persist_path)
    return base + ".sqlite3"


class SqliteRecordStore:
    """Append-only стор записей. Каждая операция — своё соединение
    (fork-safe для multi-process granian; на наших объёмах открытие
    соединения дешевле, чем держать пул)."""

    def __init__(self, db_path: str, *, table: str = "records"):
        if not table.replace("_", "").isalnum():
            raise ValueError(f"bad table name: {table}")
        self.db_path = db_path
        self.table = table
        d = os.path.dirname(db_path)
        if d:
            os.makedirs(d, exist_ok=True)
        with self._conn() as con:
            con.execute(
                f"CREATE TABLE IF NOT EXISTS {table} ("
                "id TEXT PRIMARY KEY, k1 TEXT, k2 TEXT, at TEXT, doc TEXT NOT NULL)")
            con.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_k1k2 ON {table}(k1, k2)")
            con.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_at ON {table}(at)")

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=10.0)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("PRAGMA synchronous=NORMAL")
        return con

    # -------- запись --------
    def add(self, rec_id: str, k1: str, k2: str, at: str, doc: dict) -> str:
        """INSERT OR IGNORE. Возвращает 'added' | 'exists' (идемпотентность)."""
        with self._conn() as con:
            cur = con.execute(
                f"INSERT OR IGNORE INTO {self.table}(id, k1, k2, at, doc) VALUES (?,?,?,?,?)",
                (rec_id, k1, k2, at, json.dumps(doc, ensure_ascii=False, default=str)))
            return "added" if cur.rowcount else "exists"

    def add_many(self, rows: Iterable[tuple]) -> dict:
        """rows: (id, k1, k2, at, doc_dict). Одна транзакция. {added, existed}."""
        rows = list(rows)
        if not rows:
            return {"added": 0, "existed": 0}
        added = 0
        with self._conn() as con:
            for rec_id, k1, k2, at, doc in rows:
                cur = con.execute(
                    f"INSERT OR IGNORE INTO {self.table}(id, k1, k2, at, doc) VALUES (?,?,?,?,?)",
                    (rec_id, k1, k2, at, json.dumps(doc, ensure_ascii=False, default=str)))
                added += cur.rowcount
        return {"added": added, "existed": len(rows) - added}

    # -------- чтение --------
    @staticmethod
    def _where(k1: Optional[str], k2: Optional[str]) -> tuple:
        conds, params = [], []
        if k1:
            conds.append("k1 = ?"); params.append(k1)
        if k2:
            conds.append("k2 = ?"); params.append(k2)
        return (" WHERE " + " AND ".join(conds)) if conds else "", params

    def docs(self, k1: Optional[str] = None, k2: Optional[str] = None) -> list:
        """Записи (dict из doc), отсортированные по at."""
        where, params = self._where(k1, k2)
        with self._conn() as con:
            rows = con.execute(
                f"SELECT doc FROM {self.table}{where} ORDER BY at", params).fetchall()
        out = []
        for (doc,) in rows:
            try:
                out.append(json.loads(doc))
            except Exception:
                logger.warning("sqlite_records: bad doc row skipped (%s)", self.db_path)
        return out

    def count(self, k1: Optional[str] = None, k2: Optional[str] = None) -> int:
        where, params = self._where(k1, k2)
        with self._conn() as con:
            return con.execute(
                f"SELECT COUNT(*) FROM {self.table}{where}", params).fetchone()[0]

    def group_counts(self, by: str = "k2", *, k1: Optional[str] = None,
                     k2: Optional[str] = None) -> dict:
        """{значение by-колонки: count} с опциональным фильтром по другому ключу."""
        if by not in ("k1", "k2"):
            raise ValueError("by must be 'k1' or 'k2'")
        where, params = self._where(k1, k2)
        with self._conn() as con:
            rows = con.execute(
                f"SELECT {by}, COUNT(*) FROM {self.table}{where} "
                f"GROUP BY {by}", params).fetchall()
        return {k: n for k, n in rows if k is not None}

    # -------- миграция со старого JSON --------
    def migrate_from_json(self, json_path: str,
                          row_adapter: Callable[[dict], tuple]) -> int:
        """Если таблица ПУСТА и json_path существует — импортировать записи.

        row_adapter(d) → (id, k1, k2, at, doc). JSON-файл не удаляется
        (страховка). Возвращает число импортированных записей."""
        try:
            if self.count() > 0 or not (json_path and os.path.exists(json_path)):
                return 0
            with open(json_path, encoding="utf-8") as f:
                arr = json.load(f)
            rows = []
            for d in arr or []:
                try:
                    rows.append(row_adapter(d))
                except Exception:
                    continue
            res = self.add_many(rows)
            if res["added"]:
                logger.info("sqlite_records: migrated %d records %s -> %s",
                            res["added"], json_path, self.db_path)
            return res["added"]
        except Exception as exc:
            logger.warning("sqlite_records: migration from %s failed: %s", json_path, exc)
            return 0


# ============================================================================
# SqliteKVStore — KV-документы для МУТИРУЕМЫХ сторов (M4, вторая волна:
# supersede_store / plan_memory / insight_store).
# ============================================================================

class _KVTxn:
    """API внутри exclusive-транзакции (BEGIN IMMEDIATE)."""

    def __init__(self, con: sqlite3.Connection, table: str):
        self._con = con
        self._table = table

    def load_all(self) -> dict:
        rows = self._con.execute(
            f"SELECT key, doc FROM {self._table}").fetchall()
        out = {}
        for k, doc in rows:
            try:
                out[k] = json.loads(doc)
            except Exception:
                logger.warning("sqlite_kv: bad doc for key %s skipped", k)
        return out

    def replace_all(self, docs: dict) -> None:
        """Снапшот стора одной транзакцией: upsert всех + удаление лишних.

        Сторы малы (значения/планы/инсайты, не потоки событий) — полный
        снапшот осознанно; целостность гарантирует транзакция."""
        keys = set(docs.keys())
        existing = {k for (k,) in self._con.execute(
            f"SELECT key FROM {self._table}").fetchall()}
        for k in existing - keys:
            self._con.execute(f"DELETE FROM {self._table} WHERE key = ?", (k,))
        for k, d in docs.items():
            self._con.execute(
                f"INSERT INTO {self._table}(key, doc) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET doc = excluded.doc",
                (k, json.dumps(d, ensure_ascii=False, default=str)))


class SqliteKVStore:
    """KV-документы с обновлением (в отличие от append-only SqliteRecordStore).

    Кросс-процессная сериализация писателей — `exclusive()` (BEGIN
    IMMEDIATE + busy_timeout) вместо fcntl-flock: нет lock-сайдкаров,
    работает без fcntl, целостность снапшота гарантирует транзакция.
    Читатели (load_all) не блокируются (WAL).
    """

    def __init__(self, db_path: str, *, table: str = "kv"):
        if not table.replace("_", "").isalnum():
            raise ValueError(f"bad table name: {table}")
        self.db_path = db_path
        self.table = table
        d = os.path.dirname(db_path)
        if d:
            os.makedirs(d, exist_ok=True)
        with self._connect() as con:
            con.execute(f"CREATE TABLE IF NOT EXISTS {table} ("
                        "key TEXT PRIMARY KEY, doc TEXT NOT NULL)")

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=10.0)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("PRAGMA synchronous=NORMAL")
        return con

    def load_all(self) -> dict:
        """Все документы (read-only, без блокировки писателей)."""
        with self._connect() as con:
            return _KVTxn(con, self.table).load_all()

    def exclusive(self):
        """Контекст эксклюзивной мутации: reload → fn → снапшот в ОДНОЙ
        транзакции. Использование:
            with kv.exclusive() as txn:
                docs = txn.load_all(); ...; txn.replace_all(docs)
        """
        import contextlib

        @contextlib.contextmanager
        def _ctx():
            con = self._connect()
            try:
                con.execute("BEGIN IMMEDIATE")
                yield _KVTxn(con, self.table)
                con.commit()
            except Exception:
                con.rollback()
                raise
            finally:
                con.close()
        return _ctx()

    def count(self) -> int:
        with self._connect() as con:
            return con.execute(
                f"SELECT COUNT(*) FROM {self.table}").fetchone()[0]

    def migrate_from_json(self, json_path: str,
                          docs_adapter: Callable[[Any], dict]) -> int:
        """Если таблица ПУСТА и json_path существует — импортировать.

        docs_adapter(json_root) → {key: doc}. JSON не удаляется (страховка).
        Возвращает число импортированных документов."""
        try:
            if self.count() > 0 or not (json_path and os.path.exists(json_path)):
                return 0
            with open(json_path, encoding="utf-8") as f:
                root = json.load(f)
            docs = docs_adapter(root) or {}
            if not docs:
                return 0
            with self.exclusive() as txn:
                txn.replace_all(docs)
            logger.info("sqlite_kv: migrated %d docs %s -> %s",
                        len(docs), json_path, self.db_path)
            return len(docs)
        except Exception as exc:
            logger.warning("sqlite_kv: migration from %s failed: %s",
                           json_path, exc)
            return 0
