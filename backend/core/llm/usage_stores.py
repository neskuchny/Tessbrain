# -*- coding: utf-8 -*-
"""UsageStore — абстрактный backend хранения llm_usage (issue #110 пакет 4).

Зачем: до пакета 4 `UsageTracker` зашивал SQLite-write напрямую в методы.
Это ломало два сценария:
  1) На проде с несколькими воркерами/хостами локальный `data/llm_usage.db`
     не виден из других процессов — атрибуция работает только в пределах
     одного контейнера.
  2) Тесты не могли подменить store без манкипатча `tracker.db_path`.

Решение: общий интерфейс `UsageStore`, SqliteUsageStore (current behavior),
PostgresUsageStore (П4.b — добавляется отдельным коммитом).

Все query-методы возвращают «сырой» агрегат (счётчики/group-by строки),
форматирование (cost_formatted, рендер для UI) делает caller. Это упрощает
тестирование и переиспользование в attribution.py / quota.py.
"""
from __future__ import annotations

import logging
import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from backend.core.llm.usage_tracker import UsageRecord

logger = logging.getLogger(__name__)

# Тот же allowlist, что и в attribution.py — единственный источник
# для защиты от SQL-injection через group_by.
_ATTR_COL_ALLOWLIST = frozenset(
    {"meeting_id", "document_id", "job_id", "surface", "model"})

# Для quota.py — scope→column. Дублирует _SCOPE_COLUMN в quota.py
# намеренно, чтобы избежать кругового импорта.
_QUOTA_SCOPE_COLUMN = {"user": "user_id", "tenant": "tenant_id", "job": "job_id"}


def _resolve_tenant_id_arg(explicit: Optional[str]) -> Optional[str]:
    """Если caller передал tenant_id явно — он побеждает. Иначе
    подхватываем из tenant_context (если установлен). Если и его нет —
    возвращаем None, и caller обязан применить safe-default «только
    legacy (NULL)», не leak.

    Этот helper защищает от ситуации, когда query-метод вызвали с
    tenant_id=None (например, забыли пробросить из API-слоя): мы всё
    ещё попадём в правильный tenant через context.
    """
    if explicit:
        return explicit
    try:
        from backend.core.observability.tenant_context import (
            get_current_tenant,
        )
        return get_current_tenant()
    except Exception:
        return None


def _period_start_iso(period: str) -> Optional[str]:
    """Начало периода в UTC-isoformat. None = без фильтра по времени."""
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    if period == "today":
        return now_utc.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    if period == "week":
        return (now_utc - timedelta(days=7)).isoformat()
    if period == "month":
        return (now_utc - timedelta(days=30)).isoformat()
    return None


class UsageStore(ABC):
    """Backend для хранения и чтения llm_usage записей.

    Реализации:
    - SqliteUsageStore: локальная sqlite, default.
    - PostgresUsageStore (П4.b): production, через PostgresClient.session().
    """

    @abstractmethod
    def init_schema(self) -> None:
        """Создать таблицу/индексы (идемпотентно)."""

    @abstractmethod
    def save(self, record: "UsageRecord") -> Optional[int]:
        """Записать UsageRecord. Возвращает id новой записи (если backend
        поддерживает) или None."""

    @abstractmethod
    def daily_aggregate(self, date: str, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """Сводный агрегат за UTC-сегодня + breakdown по model и agent_mode."""

    @abstractmethod
    def total_aggregate(self) -> Dict[str, Any]:
        """Сводный агрегат за всё время."""

    @abstractmethod
    def recent(self, limit: int) -> List[Dict[str, Any]]:
        """Последние N записей (для дрилл-дауна в UI)."""

    @abstractmethod
    def aggregate_by_entity(
        self, column: str, value: str, tenant_id: Optional[str],
    ) -> Dict[str, Any]:
        """Агрегат + breakdown по одной сущности (document/meeting/job).
        column обязан быть в _ATTR_COL_ALLOWLIST — проверьте на caller-side."""

    @abstractmethod
    def top_by_entity(
        self, group_by: str, period: str,
        surface: Optional[str], tenant_id: Optional[str], limit: int,
    ) -> List[Dict[str, Any]]:
        """Топ-N entity'ев по cost за период."""

    @abstractmethod
    def sum_metric_for_quota(
        self, scope: str, subject_id: str, metric: str,
        since_date: Optional[str] = None,
    ) -> float:
        """Сумма metric (total_tokens / total_cost) для одного scope'а
        (user_id / tenant_id / job_id) начиная с since_date (ISO YYYY-MM-DD,
        включительно). since_date=None → UTC-сегодня (backward-compat,
        дневная квота). Недельные/месячные окна биллинга передают начало
        своего окна. Для scope='job' фильтр по датам НЕ применяется."""


class SqliteUsageStore(UsageStore):
    """SQLite-имплементация UsageStore. Текущее поведение — переезд из
    UsageTracker без изменения семантики. Single-file, потокобезопасно
    через connect-per-call."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    # ───── schema ────────────────────────────────────────────────────

    def init_schema(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS llm_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    model_tier TEXT DEFAULT 'standard',
                    input_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    cached_tokens INTEGER DEFAULT 0,
                    total_tokens INTEGER DEFAULT 0,
                    input_cost REAL DEFAULT 0.0,
                    output_cost REAL DEFAULT 0.0,
                    cached_cost REAL DEFAULT 0.0,
                    total_cost REAL DEFAULT 0.0,
                    user_id TEXT,
                    session_id TEXT,
                    agent_mode TEXT,
                    request_type TEXT,
                    tenant_id TEXT,
                    success INTEGER DEFAULT 1,
                    error TEXT,
                    latency_ms INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Phase 7 + issue #110: идемпотентные ALTER (если БД старая).
            for col_ddl in (
                "tenant_id TEXT",
                "document_id TEXT", "meeting_id TEXT",
                "job_id TEXT", "surface TEXT",
            ):
                try:
                    cursor.execute(f"ALTER TABLE llm_usage ADD COLUMN {col_ddl}")
                except sqlite3.OperationalError:
                    pass
            for idx_sql in (
                "CREATE INDEX IF NOT EXISTS idx_timestamp ON llm_usage(timestamp)",
                "CREATE INDEX IF NOT EXISTS idx_user_id ON llm_usage(user_id)",
                "CREATE INDEX IF NOT EXISTS idx_model ON llm_usage(model)",
                "CREATE INDEX IF NOT EXISTS idx_agent_mode ON llm_usage(agent_mode)",
                "CREATE INDEX IF NOT EXISTS idx_tenant_id ON llm_usage(tenant_id)",
                "CREATE INDEX IF NOT EXISTS idx_tenant_document "
                "ON llm_usage(tenant_id, document_id) WHERE document_id IS NOT NULL",
                "CREATE INDEX IF NOT EXISTS idx_tenant_meeting "
                "ON llm_usage(tenant_id, meeting_id) WHERE meeting_id IS NOT NULL",
                "CREATE INDEX IF NOT EXISTS idx_tenant_job "
                "ON llm_usage(tenant_id, job_id) WHERE job_id IS NOT NULL",
                "CREATE INDEX IF NOT EXISTS idx_surface "
                "ON llm_usage(surface) WHERE surface IS NOT NULL",
            ):
                cursor.execute(idx_sql)
            # daily_usage — отдельная агрегация (legacy)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS daily_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL UNIQUE,
                    total_requests INTEGER DEFAULT 0,
                    total_input_tokens INTEGER DEFAULT 0,
                    total_output_tokens INTEGER DEFAULT 0,
                    total_cached_tokens INTEGER DEFAULT 0,
                    total_cost REAL DEFAULT 0.0,
                    by_model TEXT,
                    by_agent_mode TEXT,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    # ───── writes ────────────────────────────────────────────────────

    def save(self, record: "UsageRecord") -> Optional[int]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO llm_usage (
                        timestamp, provider, model, model_tier,
                        input_tokens, output_tokens, cached_tokens, total_tokens,
                        input_cost, output_cost, cached_cost, total_cost,
                        user_id, session_id, agent_mode, request_type, tenant_id,
                        document_id, meeting_id, job_id, surface,
                        success, error, latency_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              ?, ?, ?, ?, ?, ?, ?)
                """, (
                    record.timestamp, record.provider, record.model, record.model_tier,
                    record.input_tokens, record.output_tokens, record.cached_tokens,
                    record.total_tokens,
                    record.input_cost, record.output_cost, record.cached_cost,
                    record.total_cost,
                    record.user_id, record.session_id, record.agent_mode,
                    record.request_type, record.tenant_id,
                    record.document_id, record.meeting_id, record.job_id, record.surface,
                    1 if record.success else 0, record.error, record.latency_ms,
                ))
                return cursor.lastrowid
        except Exception as e:
            logger.error(f"SqliteUsageStore.save failed: {e}")
            return None

    # ───── reads ─────────────────────────────────────────────────────

    def daily_aggregate(self, date: str, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        # issue #112 RLS hotfix: SQLite не имеет RLS, поэтому app-level
        # WHERE tenant_id обязателен.
        # P2 #11: _resolve_tenant_id_arg — explicit tenant_id (admin
        # override «usage конкретного org») побеждает; иначе берётся из
        # tenant_context (RLS-default). Без обоих — safe-default «только
        # legacy NULL», не leak. Route обязан проверить admin-роль перед
        # explicit-override (см. /usage/stats).
        tenant_id = _resolve_tenant_id_arg(tenant_id)
        if tenant_id:
            tenant_clause = " AND tenant_id = ?"
            tenant_params: tuple = (tenant_id,)
        else:
            tenant_clause = " AND (tenant_id IS NULL OR tenant_id = '')"
            tenant_params = ()
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(f"""
                    SELECT
                        COUNT(*) as requests_count,
                        COALESCE(SUM(input_tokens), 0) as total_input_tokens,
                        COALESCE(SUM(output_tokens), 0) as total_output_tokens,
                        COALESCE(SUM(cached_tokens), 0) as total_cached_tokens,
                        COALESCE(SUM(total_tokens), 0) as total_tokens,
                        COALESCE(SUM(total_cost), 0) as total_cost,
                        COALESCE(SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END), 0) as errors_count,
                        COALESCE(AVG(latency_ms), 0) as avg_latency_ms
                    FROM llm_usage WHERE DATE(timestamp) = ?{tenant_clause}
                """, (date, *tenant_params))
                row = cursor.fetchone()
                cursor.execute(f"""
                    SELECT model, COUNT(*) as requests,
                           SUM(total_tokens) as tokens, SUM(total_cost) as cost
                    FROM llm_usage WHERE DATE(timestamp) = ?{tenant_clause}
                    GROUP BY model
                """, (date, *tenant_params))
                by_model = {r["model"]: {"requests": r["requests"],
                                         "tokens": r["tokens"], "cost": r["cost"]}
                            for r in cursor.fetchall()}
                cursor.execute(f"""
                    SELECT COALESCE(agent_mode, 'unknown') as agent_mode,
                           COUNT(*) as requests,
                           SUM(total_tokens) as tokens, SUM(total_cost) as cost
                    FROM llm_usage WHERE DATE(timestamp) = ?{tenant_clause}
                    GROUP BY agent_mode
                """, (date, *tenant_params))
                by_agent_mode = {r["agent_mode"]: {"requests": r["requests"],
                                                   "tokens": r["tokens"],
                                                   "cost": r["cost"]}
                                 for r in cursor.fetchall()}
                return {
                    "date": date,
                    "requests_count": row["requests_count"],
                    "total_input_tokens": row["total_input_tokens"],
                    "total_output_tokens": row["total_output_tokens"],
                    "total_cached_tokens": row["total_cached_tokens"],
                    "total_tokens": row["total_tokens"],
                    "total_cost": round(row["total_cost"], 4),
                    "total_cost_formatted": f"${row['total_cost']:.4f}",
                    "errors_count": row["errors_count"],
                    "avg_latency_ms": round(row["avg_latency_ms"], 0),
                    "by_model": by_model,
                    "by_agent_mode": by_agent_mode,
                }
        except Exception as e:
            logger.error(f"SqliteUsageStore.daily_aggregate failed: {e}")
            return {"error": str(e)}

    def total_aggregate(self) -> Dict[str, Any]:
        # issue #112 RLS hotfix (SQLite — только app-level). Без tenant —
        # только legacy NULL-записи, не leak чужих данных.
        try:
            from backend.core.observability.tenant_context import get_current_tenant
            tenant_id = get_current_tenant()
        except Exception:
            tenant_id = None
        if tenant_id:
            tenant_clause = " WHERE tenant_id = ?"
            tenant_params: tuple = (tenant_id,)
        else:
            tenant_clause = " WHERE (tenant_id IS NULL OR tenant_id = '')"
            tenant_params = ()
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(f"""
                    SELECT
                        COUNT(*) as requests_count,
                        COALESCE(SUM(input_tokens), 0) as total_input_tokens,
                        COALESCE(SUM(output_tokens), 0) as total_output_tokens,
                        COALESCE(SUM(cached_tokens), 0) as total_cached_tokens,
                        COALESCE(SUM(total_tokens), 0) as total_tokens,
                        COALESCE(SUM(total_cost), 0) as total_cost,
                        MIN(timestamp) as first_request,
                        MAX(timestamp) as last_request
                    FROM llm_usage{tenant_clause}
                """, tenant_params)
                row = cursor.fetchone()
                return {
                    "requests_count": row["requests_count"],
                    "total_input_tokens": row["total_input_tokens"],
                    "total_output_tokens": row["total_output_tokens"],
                    "total_cached_tokens": row["total_cached_tokens"],
                    "total_tokens": row["total_tokens"],
                    "total_cost": round(row["total_cost"], 4),
                    "total_cost_formatted": f"${row['total_cost']:.4f}",
                    "first_request": row["first_request"],
                    "last_request": row["last_request"],
                }
        except Exception as e:
            logger.error(f"SqliteUsageStore.total_aggregate failed: {e}")
            return {"error": str(e)}

    def recent(self, limit: int) -> List[Dict[str, Any]]:
        # issue #112 RLS hotfix (SQLite — только app-level). Без tenant —
        # только legacy NULL-записи.
        try:
            from backend.core.observability.tenant_context import get_current_tenant
            tenant_id = get_current_tenant()
        except Exception:
            tenant_id = None
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                if tenant_id:
                    cursor.execute("""
                        SELECT * FROM llm_usage WHERE tenant_id = ?
                        ORDER BY timestamp DESC LIMIT ?
                    """, (tenant_id, limit))
                else:
                    cursor.execute("""
                        SELECT * FROM llm_usage
                        WHERE tenant_id IS NULL OR tenant_id = ''
                        ORDER BY timestamp DESC LIMIT ?
                    """, (limit,))
                return [dict(r) for r in cursor.fetchall()]
        except Exception as e:
            logger.error(f"SqliteUsageStore.recent failed: {e}")
            return []

    def aggregate_by_entity(
        self, column: str, value: str, tenant_id: Optional[str],
    ) -> Dict[str, Any]:
        if column not in _ATTR_COL_ALLOWLIST:
            return {"error": f"unknown column: {column}"}
        # issue #112 RLS hotfix: если caller не передал tenant —
        # подхватываем из tenant_context; если и его нет — safe default:
        # только legacy (tenant_id NULL/''), не leak чужих данных.
        tenant_id = _resolve_tenant_id_arg(tenant_id)
        where_parts = [f"{column} = ?"]
        params: List[Any] = [value]
        if tenant_id:
            where_parts.append("tenant_id = ?")
            params.append(tenant_id)
        else:
            where_parts.append("(tenant_id IS NULL OR tenant_id = '')")
        where_clause = " AND ".join(where_parts)
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(f"""
                    SELECT
                        COUNT(*) AS requests,
                        COALESCE(SUM(input_tokens), 0) AS input_tokens,
                        COALESCE(SUM(output_tokens), 0) AS output_tokens,
                        COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                        COALESCE(SUM(total_tokens), 0) AS total_tokens,
                        COALESCE(SUM(total_cost), 0.0) AS total_cost,
                        COALESCE(SUM(CASE WHEN success=0 THEN 1 ELSE 0 END), 0) AS errors,
                        COALESCE(AVG(latency_ms), 0) AS avg_latency_ms,
                        MIN(timestamp) AS first_at,
                        MAX(timestamp) AS last_at
                    FROM llm_usage WHERE {where_clause}
                """, params)
                agg = dict(cursor.fetchone() or {})
                cursor.execute(f"""
                    SELECT model, COUNT(*) AS requests,
                           COALESCE(SUM(total_tokens), 0) AS tokens,
                           COALESCE(SUM(total_cost), 0.0) AS cost
                    FROM llm_usage WHERE {where_clause}
                    GROUP BY model ORDER BY cost DESC
                """, params)
                by_model_rows = [dict(r) for r in cursor.fetchall()]
                cursor.execute(f"""
                    SELECT COALESCE(surface, 'unknown') AS surface,
                           COUNT(*) AS requests,
                           COALESCE(SUM(total_tokens), 0) AS tokens,
                           COALESCE(SUM(total_cost), 0.0) AS cost
                    FROM llm_usage WHERE {where_clause}
                    GROUP BY surface ORDER BY cost DESC
                """, params)
                by_surface_rows = [dict(r) for r in cursor.fetchall()]
                cursor.execute(f"""
                    SELECT timestamp, model, agent_mode, surface, request_type,
                           total_tokens, total_cost, latency_ms, success
                    FROM llm_usage WHERE {where_clause}
                    ORDER BY timestamp DESC LIMIT 50
                """, params)
                records = [dict(r) for r in cursor.fetchall()]
                return {
                    "agg": agg,
                    "by_model_rows": by_model_rows,
                    "by_surface_rows": by_surface_rows,
                    "recent": records,
                }
        except Exception as e:
            logger.error(f"SqliteUsageStore.aggregate_by_entity failed: {e}")
            return {"error": str(e)}

    def top_by_entity(
        self, group_by: str, period: str,
        surface: Optional[str], tenant_id: Optional[str], limit: int,
    ) -> List[Dict[str, Any]]:
        if group_by not in _ATTR_COL_ALLOWLIST:
            return []
        # issue #112 RLS hotfix — safe-default tenant (см. aggregate_by_entity).
        tenant_id = _resolve_tenant_id_arg(tenant_id)
        where_parts = [f"{group_by} IS NOT NULL"]
        params: List[Any] = []
        if tenant_id:
            where_parts.append("tenant_id = ?")
            params.append(tenant_id)
        else:
            where_parts.append("(tenant_id IS NULL OR tenant_id = '')")
        if surface:
            where_parts.append("surface = ?")
            params.append(surface)
        start_iso = _period_start_iso(period)
        if start_iso:
            where_parts.append("timestamp >= ?")
            params.append(start_iso)
        where_clause = " AND ".join(where_parts)
        params.append(int(limit))
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(f"""
                    SELECT {group_by} AS key, COUNT(*) AS requests,
                           COALESCE(SUM(total_tokens), 0) AS tokens,
                           COALESCE(SUM(total_cost), 0.0) AS cost,
                           MAX(timestamp) AS last_at
                    FROM llm_usage WHERE {where_clause}
                    GROUP BY {group_by}
                    ORDER BY cost DESC LIMIT ?
                """, params)
                return [dict(r) for r in cursor.fetchall()]
        except Exception as e:
            logger.error(f"SqliteUsageStore.top_by_entity failed: {e}")
            return []

    def sum_metric_for_quota(
        self, scope: str, subject_id: str, metric: str,
        since_date: Optional[str] = None,
    ) -> float:
        column = _QUOTA_SCOPE_COLUMN.get(scope)
        if column is None or metric not in ("total_tokens", "total_cost"):
            return 0.0
        # Для job_id не фильтруем по датам (job коротка).
        # since_date=None → сегодня (дневная квота); иначе — окно
        # «с since_date включительно» (неделя/месяц биллинга).
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                if scope == "job":
                    date_filter = ""
                    params: tuple = (subject_id,)
                elif since_date:
                    date_filter = " AND DATE(timestamp) >= ?"
                    params = (subject_id, since_date)
                else:
                    date_filter = " AND DATE(timestamp) = ?"
                    params = (subject_id,
                              datetime.now(timezone.utc).strftime("%Y-%m-%d"))
                cursor.execute(
                    f"SELECT COALESCE(SUM({metric}), 0) FROM llm_usage "
                    f"WHERE {column} = ?{date_filter}", params)
                (used,) = cursor.fetchone()
                return float(used or 0)
        except sqlite3.Error as exc:
            logger.warning("SqliteUsageStore.sum_metric_for_quota error: %s", exc)
            return 0.0


class PostgresUsageStore(UsageStore):
    """PG-имплементация UsageStore через psycopg2 (sync).

    issue #110 пакет 4.b: production backend для multi-worker/multi-host
    окружений. PostgreSQL даёт consistent view на затраты со всех воркеров,
    приёмников webhook'ов и фоновых задач (что SQLite-файл per-container не
    мог).

    Драйвер: psycopg2 синхронный (a) совместим с sync UsageTracker.track(),
    который зовётся из любого контекста, (b) уже широко распространён в
    проде, (c) lazy import — отсутствие psycopg2 не валит загрузку модуля,
    только создание стора.

    Схема — backend/db/migrations/261_llm_usage.sql (применяется отдельно
    через migrate.py, init_schema здесь — no-op для совместимости с ABC).

    Tenant-isolation на уровне приложения (через WHERE tenant_id=…),
    RLS-политика добавляется отдельной миграцией при необходимости.
    """

    def __init__(self, dsn: str):
        try:
            import importlib
            importlib.import_module("psycopg2")
        except ImportError as e:
            raise ImportError(
                "PostgresUsageStore требует psycopg2-binary. "
                "Установите: pip install psycopg2-binary"
            ) from e
        self.dsn = dsn
        self.db_path = dsn  # backward-compat (tracker.db_path)

    def _conn(self):
        """Свежий PG-connection. Один-запрос-одно-соединение упрощает
        thread-safety и не требует пулинга для редких записей usage'а
        (даже на 100 RPS LLM-вызовов это <1% overhead против сетевой
        задержки самого LLM)."""
        import psycopg2
        import psycopg2.extras
        return psycopg2.connect(self.dsn,
                                cursor_factory=psycopg2.extras.RealDictCursor)

    @staticmethod
    def _current_tenant_id() -> Optional[str]:
        """issue #112 RLS hotfix: tenant_id для каждого запроса.

        Источник единый: backend.core.observability.tenant_context, тот же
        что использует PostgresClient.session() для SET LOCAL app.tenant_id.
        Если контекст пуст (None) — RLS политика в миграции 020c покажет
        только tenant_id IS NULL legacy-строки. Это безопасно.
        """
        try:
            from backend.core.observability.tenant_context import (
                get_current_tenant,
            )
            return get_current_tenant()
        except Exception:
            return None

    def _apply_tenant_setting(self, cursor) -> None:
        """Установить app.tenant_id в текущей PG-транзакции для RLS
        (миграция 020c_llm_usage_rls.sql). Без этого SET RLS-политика
        покажет только tenant_id IS NULL (legacy) — безопасный default.
        """
        tenant_id = self._current_tenant_id()
        if tenant_id:
            cursor.execute("SET LOCAL app.tenant_id = %s", (tenant_id,))

    def init_schema(self) -> None:
        # PG-схема создаётся миграцией 261_llm_usage.sql (см. backend/db/
        # migrations). Здесь — no-op, чтобы не дублировать DDL.
        logger.info("PostgresUsageStore.init_schema: see "
                    "backend/db/migrations/261_llm_usage.sql")

    def save(self, record: "UsageRecord") -> Optional[int]:
        try:
            with self._conn() as conn, conn.cursor() as cursor:
                # issue #112 RLS hotfix: SET LOCAL для INSERT WITH CHECK
                # (политика tenant_isolation проверяет tenant_id и при insert).
                self._apply_tenant_setting(cursor)
                cursor.execute("""
                    INSERT INTO llm_usage (
                        timestamp, provider, model, model_tier,
                        input_tokens, output_tokens, cached_tokens, total_tokens,
                        input_cost, output_cost, cached_cost, total_cost,
                        user_id, session_id, agent_mode, request_type, tenant_id,
                        document_id, meeting_id, job_id, surface,
                        success, error, latency_ms
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                              %s, %s, %s, %s, %s,
                              %s, %s, %s, %s,
                              %s, %s, %s)
                    RETURNING id
                """, (
                    record.timestamp, record.provider, record.model,
                    record.model_tier,
                    record.input_tokens, record.output_tokens,
                    record.cached_tokens, record.total_tokens,
                    record.input_cost, record.output_cost, record.cached_cost,
                    record.total_cost,
                    record.user_id, record.session_id, record.agent_mode,
                    record.request_type, record.tenant_id,
                    record.document_id, record.meeting_id, record.job_id,
                    record.surface,
                    1 if record.success else 0, record.error, record.latency_ms,
                ))
                row = cursor.fetchone()
                conn.commit()
                return int(row["id"]) if row else None
        except Exception as e:
            logger.error(f"PostgresUsageStore.save failed: {e}")
            return None

    def _exec_select(self, sql: str, params: tuple) -> List[Dict[str, Any]]:
        try:
            with self._conn() as conn, conn.cursor() as cursor:
                # issue #112 RLS hotfix: SET LOCAL app.tenant_id для текущей
                # транзакции — иначе RLS политика (020c) не отфильтрует.
                self._apply_tenant_setting(cursor)
                cursor.execute(sql, params)
                return [dict(r) for r in cursor.fetchall()]
        except Exception as e:
            logger.error(f"PostgresUsageStore query failed: {e}\nSQL: {sql}")
            return []

    def daily_aggregate(self, date: str, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        # issue #112 RLS hotfix (defense-in-depth): app-level фильтр по
        # tenant_id. RLS (миграция 020c) уже отрезает на уровне БД, но
        # явный WHERE гарантирует изоляцию даже если миграция не применена.
        # P2 #11: explicit tenant_id (admin override) побеждает; иначе
        # из контекста. Без обоих — только legacy NULL, не leak.
        tenant_id = tenant_id or self._current_tenant_id()
        if tenant_id:
            tenant_clause = " AND tenant_id = %s"
            tenant_params: tuple = (tenant_id,)
        else:
            tenant_clause = " AND (tenant_id IS NULL OR tenant_id = '')"
            tenant_params = ()
        rows = self._exec_select(f"""
            SELECT
                COUNT(*) AS requests_count,
                COALESCE(SUM(input_tokens), 0) AS total_input_tokens,
                COALESCE(SUM(output_tokens), 0) AS total_output_tokens,
                COALESCE(SUM(cached_tokens), 0) AS total_cached_tokens,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(total_cost), 0) AS total_cost,
                COALESCE(SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END), 0) AS errors_count,
                COALESCE(AVG(latency_ms), 0) AS avg_latency_ms
            FROM llm_usage WHERE LEFT(timestamp, 10) = %s{tenant_clause}
        """, (date, *tenant_params))
        row = rows[0] if rows else {}
        by_model_rows = self._exec_select(f"""
            SELECT model, COUNT(*) AS requests,
                   COALESCE(SUM(total_tokens), 0) AS tokens,
                   COALESCE(SUM(total_cost), 0) AS cost
            FROM llm_usage WHERE LEFT(timestamp, 10) = %s{tenant_clause}
            GROUP BY model
        """, (date, *tenant_params))
        by_agent_rows = self._exec_select(f"""
            SELECT COALESCE(agent_mode, 'unknown') AS agent_mode,
                   COUNT(*) AS requests,
                   COALESCE(SUM(total_tokens), 0) AS tokens,
                   COALESCE(SUM(total_cost), 0) AS cost
            FROM llm_usage WHERE LEFT(timestamp, 10) = %s{tenant_clause}
            GROUP BY agent_mode
        """, (date, *tenant_params))
        return {
            "date": date,
            "requests_count": int(row.get("requests_count", 0) or 0),
            "total_input_tokens": int(row.get("total_input_tokens", 0) or 0),
            "total_output_tokens": int(row.get("total_output_tokens", 0) or 0),
            "total_cached_tokens": int(row.get("total_cached_tokens", 0) or 0),
            "total_tokens": int(row.get("total_tokens", 0) or 0),
            "total_cost": round(float(row.get("total_cost", 0.0) or 0.0), 4),
            "total_cost_formatted": f"${float(row.get('total_cost', 0.0) or 0.0):.4f}",
            "errors_count": int(row.get("errors_count", 0) or 0),
            "avg_latency_ms": round(float(row.get("avg_latency_ms", 0) or 0), 0),
            "by_model": {r["model"]: {"requests": r["requests"],
                                      "tokens": r["tokens"], "cost": r["cost"]}
                         for r in by_model_rows},
            "by_agent_mode": {r["agent_mode"]: {"requests": r["requests"],
                                                "tokens": r["tokens"],
                                                "cost": r["cost"]}
                              for r in by_agent_rows},
        }

    def total_aggregate(self) -> Dict[str, Any]:
        # issue #112 RLS hotfix (defense-in-depth)
        tenant_id = self._current_tenant_id()
        if tenant_id:
            tenant_clause = " WHERE tenant_id = %s"
            tenant_params: tuple = (tenant_id,)
        else:
            tenant_clause = " WHERE (tenant_id IS NULL OR tenant_id = '')"
            tenant_params = ()
        rows = self._exec_select(f"""
            SELECT
                COUNT(*) AS requests_count,
                COALESCE(SUM(input_tokens), 0) AS total_input_tokens,
                COALESCE(SUM(output_tokens), 0) AS total_output_tokens,
                COALESCE(SUM(cached_tokens), 0) AS total_cached_tokens,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(total_cost), 0) AS total_cost,
                MIN(timestamp) AS first_request,
                MAX(timestamp) AS last_request
            FROM llm_usage{tenant_clause}
        """, tenant_params)
        row = rows[0] if rows else {}
        return {
            "requests_count": int(row.get("requests_count", 0) or 0),
            "total_input_tokens": int(row.get("total_input_tokens", 0) or 0),
            "total_output_tokens": int(row.get("total_output_tokens", 0) or 0),
            "total_cached_tokens": int(row.get("total_cached_tokens", 0) or 0),
            "total_tokens": int(row.get("total_tokens", 0) or 0),
            "total_cost": round(float(row.get("total_cost", 0.0) or 0.0), 4),
            "total_cost_formatted": f"${float(row.get('total_cost', 0.0) or 0.0):.4f}",
            "first_request": row.get("first_request"),
            "last_request": row.get("last_request"),
        }

    def recent(self, limit: int) -> List[Dict[str, Any]]:
        # issue #112 RLS hotfix (defense-in-depth)
        tenant_id = self._current_tenant_id()
        if tenant_id:
            return self._exec_select(
                "SELECT * FROM llm_usage WHERE tenant_id = %s "
                "ORDER BY timestamp DESC LIMIT %s",
                (tenant_id, int(limit)))
        # Без tenant — только legacy NULL
        return self._exec_select(
            "SELECT * FROM llm_usage "
            "WHERE tenant_id IS NULL OR tenant_id = '' "
            "ORDER BY timestamp DESC LIMIT %s",
            (int(limit),))

    def aggregate_by_entity(
        self, column: str, value: str, tenant_id: Optional[str],
    ) -> Dict[str, Any]:
        if column not in _ATTR_COL_ALLOWLIST:
            return {"error": f"unknown column: {column}"}
        # issue #112 RLS hotfix: defence in depth поверх PG RLS — даже
        # если кто-то забудет SET LOCAL app.tenant_id, явный WHERE спасёт.
        tenant_id = _resolve_tenant_id_arg(tenant_id)
        where_parts = [f"{column} = %s"]
        params: List[Any] = [value]
        if tenant_id:
            where_parts.append("tenant_id = %s")
            params.append(tenant_id)
        else:
            where_parts.append("(tenant_id IS NULL OR tenant_id = '')")
        where_clause = " AND ".join(where_parts)
        agg_rows = self._exec_select(f"""
            SELECT
                COUNT(*) AS requests,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(total_cost), 0.0) AS total_cost,
                COALESCE(SUM(CASE WHEN success=0 THEN 1 ELSE 0 END), 0) AS errors,
                COALESCE(AVG(latency_ms), 0) AS avg_latency_ms,
                MIN(timestamp) AS first_at,
                MAX(timestamp) AS last_at
            FROM llm_usage WHERE {where_clause}
        """, tuple(params))
        by_model_rows = self._exec_select(f"""
            SELECT model, COUNT(*) AS requests,
                   COALESCE(SUM(total_tokens), 0) AS tokens,
                   COALESCE(SUM(total_cost), 0.0) AS cost
            FROM llm_usage WHERE {where_clause}
            GROUP BY model ORDER BY cost DESC
        """, tuple(params))
        by_surface_rows = self._exec_select(f"""
            SELECT COALESCE(surface, 'unknown') AS surface,
                   COUNT(*) AS requests,
                   COALESCE(SUM(total_tokens), 0) AS tokens,
                   COALESCE(SUM(total_cost), 0.0) AS cost
            FROM llm_usage WHERE {where_clause}
            GROUP BY surface ORDER BY cost DESC
        """, tuple(params))
        records = self._exec_select(f"""
            SELECT timestamp, model, agent_mode, surface, request_type,
                   total_tokens, total_cost, latency_ms, success
            FROM llm_usage WHERE {where_clause}
            ORDER BY timestamp DESC LIMIT 50
        """, tuple(params))
        return {
            "agg": agg_rows[0] if agg_rows else {},
            "by_model_rows": by_model_rows,
            "by_surface_rows": by_surface_rows,
            "recent": records,
        }

    def top_by_entity(
        self, group_by: str, period: str,
        surface: Optional[str], tenant_id: Optional[str], limit: int,
    ) -> List[Dict[str, Any]]:
        if group_by not in _ATTR_COL_ALLOWLIST:
            return []
        # issue #112 RLS hotfix — defence in depth (см. aggregate_by_entity).
        tenant_id = _resolve_tenant_id_arg(tenant_id)
        where_parts = [f"{group_by} IS NOT NULL"]
        params: List[Any] = []
        if tenant_id:
            where_parts.append("tenant_id = %s")
            params.append(tenant_id)
        else:
            where_parts.append("(tenant_id IS NULL OR tenant_id = '')")
        if surface:
            where_parts.append("surface = %s")
            params.append(surface)
        start_iso = _period_start_iso(period)
        if start_iso:
            where_parts.append("timestamp >= %s")
            params.append(start_iso)
        where_clause = " AND ".join(where_parts)
        params.append(int(limit))
        return self._exec_select(f"""
            SELECT {group_by} AS key, COUNT(*) AS requests,
                   COALESCE(SUM(total_tokens), 0) AS tokens,
                   COALESCE(SUM(total_cost), 0.0) AS cost,
                   MAX(timestamp) AS last_at
            FROM llm_usage WHERE {where_clause}
            GROUP BY {group_by} ORDER BY cost DESC LIMIT %s
        """, tuple(params))

    def sum_metric_for_quota(
        self, scope: str, subject_id: str, metric: str,
        since_date: Optional[str] = None,
    ) -> float:
        column = _QUOTA_SCOPE_COLUMN.get(scope)
        if column is None or metric not in ("total_tokens", "total_cost"):
            return 0.0
        if scope == "job":
            date_filter = ""
            params: tuple = (subject_id,)
        elif since_date:
            date_filter = " AND LEFT(timestamp, 10) >= %s"
            params = (subject_id, since_date)
        else:
            date_filter = " AND LEFT(timestamp, 10) = %s"
            params = (subject_id,
                      datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        rows = self._exec_select(
            f"SELECT COALESCE(SUM({metric}), 0) AS s FROM llm_usage "
            f"WHERE {column} = %s{date_filter}", params)
        return float(rows[0].get("s", 0) if rows else 0)


def make_default_store(db_path: Optional[str] = None) -> UsageStore:
    """Фабрика. issue #110 пакет 4.b: добавлена ветка PostgresUsageStore
    по env `LLM_USAGE_DSN` (postgres://... DSN).

    Приоритет:
      1) явный db_path аргумент → SqliteUsageStore (legacy)
      2) LLM_USAGE_DSN env с postgres:// → PostgresUsageStore (если
         psycopg2 установлен; иначе fallback на SQLite с warning'ом)
      3) LLM_USAGE_DB env → SqliteUsageStore по этому пути
      4) дефолт data/llm_usage.db → SqliteUsageStore

    Failsafe: если PG-store не удалось создать (psycopg2 not installed,
    DSN кривой) — fallback на SQLite. Tracker не должен ронять процесс
    из-за telemetry-backend'а.
    """
    import os
    from pathlib import Path

    if db_path is not None:
        store = SqliteUsageStore(db_path)
        store.init_schema()
        return store

    dsn = os.environ.get("LLM_USAGE_DSN", "").strip()
    if dsn.startswith(("postgres://", "postgresql://")):
        try:
            store = PostgresUsageStore(dsn)
            store.init_schema()
            logger.info("LLM usage store: PostgreSQL (%s...)", dsn[:30])
            return store
        except ImportError as e:
            logger.warning(
                "LLM_USAGE_DSN=postgres://... но %s — fallback на SQLite", e)
        except Exception as e:
            logger.error("PostgresUsageStore init failed (%s) — fallback на SQLite", e)

    env_path = os.environ.get("LLM_USAGE_DB")
    if env_path:
        db_path = env_path
    else:
        data_dir = Path(__file__).parent.parent.parent.parent / "data"
        data_dir.mkdir(exist_ok=True)
        db_path = str(data_dir / "llm_usage.db")
    store = SqliteUsageStore(db_path)
    store.init_schema()
    return store
