"""Schema migration runner (W10 production hygiene).

Применяет SQL-миграции из `backend/db/migrations/` в порядке имени файла,
отслеживая применённые версии в таблице `public.tessent_schema_migrations`.

Дизайн:
- Pure-stdlib + asyncpg (один прод-зависимый импорт). НЕ Alembic —
  миграции у нас уже plain SQL и идемпотентны, нагнуть Alembic под
  существующий стиль сложнее, чем написать 100 строк runner'а.
- Идемпотентно: повторный `migrate()` пропускает уже применённые версии.
- Per-file транзакция: миграция либо применяется целиком, либо откатывается
  и НЕ записывается в `schema_migrations`.
- CLI: `python -m backend.db.migrate [--target VERSION] [--dry-run]`.
- Lock через `pg_advisory_lock(MIGRATION_LOCK_KEY)` — две реплики при старте
  не побьют друг друга.

Версионирование:
- Версия = `Path(filename).stem`, например `040_audit_log`.
- Сортировка лексикографическая → префикс с zero-pad обязателен (мы и так так делаем).
- Файлы с расширением `.template` / `.example` игнорируются (это не runnable
  миграции, а рукописные шаблоны для оператора).

Применение в проде:
```
docker compose exec api python -m backend.db.migrate
```
"""
from __future__ import annotations

import argparse
import re
import asyncio
import hashlib
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# pg_advisory_lock numeric key. Произвольное значение, главное стабильное
# между запусками — две реплики ждут на одном и том же ключе.
_MIGRATION_LOCK_KEY = 0x7E55E_71B  # "tessent" в hex-намёке

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# Какие файлы пропускаем (не runnable, см. docstring).
_SKIP_SUFFIXES = (".template", ".example")
# bwe_schema.sql — legacy, применяется отдельно.
# 001_create_documents_table.sql, 003_knowledge_sync_subscriptions.sql и
# 070_skills_extension.sql — Supabase-only миграции: соответствующие таблицы
# (`documents`, `knowledge_sync_subscriptions`, `agent_automations`) живут в
# Supabase и читаются кодом ИСКЛЮЧИТЕЛЬНО через PostgREST (`/rest/v1/...`,
# см. agent_automation_service), а сами SQL опираются на Supabase-специфику
# (`auth.uid()`, роли authenticated/service_role/anon) либо ALTER-ят таблицу,
# которой нет в self-hosted Postgres (070 расширяет agent_automations, базовая
# таблица создаётся на стороне Supabase). Их RLS к тому же конфликтует с
# локальной tenant-RLS из 020 (см. docs/ru/RLS_ROLLOUT_RUNBOOK.md). Применять их
# нужно в Supabase SQL Editor, а не локальным runner'ом.
_SKIP_NAMES = frozenset({
    "bwe_schema.sql",
    "001_create_documents_table.sql",
    "003_knowledge_sync_subscriptions.sql",
    "070_skills_extension.sql",
    # 263 — ALTER-ит public.knowledge_sync_subscriptions (Supabase-only таблица
    # из 003, выше в skip-списке). На self-hosted Postgres таблицы нет →
    # `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` всё равно падает
    # UndefinedTableError (IF NOT EXISTS относится к колонке, не к таблице) и
    # роняет весь runner до 266. Реконсиляцию колонок подписок применяют в
    # Supabase SQL Editor вместе с 003.
    "263_knowledge_sync_subscription_columns.sql",
    # 250_processed_meetings_org_dedup.sql — ALTER-ит public.processed_meetings,
    # а сама таблица создаётся ТОЛЬКО в 003 (Supabase-only, выше в этом же
    # skip-списке). На self-hosted Postgres таблицы нет → ALTER падал с
    # UndefinedTableError. Это org-расширение того же Supabase-only журнала,
    # поэтому и применять его нужно в Supabase SQL Editor вместе с 003, а не
    # локальным runner'ом.
    "250_processed_meetings_org_dedup.sql",
    # 267 — ALTER CHECK на том же Supabase-only журнале processed_meetings.
    "267_processed_meetings_status_check.sql",
    # 268 — Supabase-only таблица тикетов обратной связи (guide_feedback).
    "268_feedback_tickets.sql",
    # 269 — folder-колонка Supabase-only таблицы documents (001).
    "269_documents_folder.sql",
})

# NB: имя tessent_schema_migrations (не schema_migrations) — в БД уже может
# существовать public.schema_migrations от другого инструмента (Supabase и др.)
# с ДРУГОЙ схемой (без колонки version). CREATE TABLE IF NOT EXISTS её не
# трогает, а SELECT version падал с UndefinedColumnError. Свою tracking-таблицу
# держим под уникальным именем — коллизии нет.
_MIGRATIONS_TABLE = "public.tessent_schema_migrations"

_BOOTSTRAP_SQL = f"""\
CREATE TABLE IF NOT EXISTS {_MIGRATIONS_TABLE} (
    version    TEXT PRIMARY KEY,
    checksum   TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


# SQLSTATE'ы «объект уже существует». При свежей tracking-таблице раннер
# прогоняет ВСЁ заново; старые не-идемпотентные миграции (CREATE POLICY без
# IF NOT EXISTS и т.п.) падают на уже существующих объектах. Такие ошибки
# трактуем как «уже применено» и идём дальше (реконсиляция состояния).
_ALREADY_EXISTS_SQLSTATES = frozenset({
    "42P07",  # duplicate_table
    "42710",  # duplicate_object (policy / constraint / trigger)
    "42701",  # duplicate_column
    "42P06",  # duplicate_schema
    "42723",  # duplicate_function
    "42P04",  # duplicate_database
    "42P16",  # invalid_table_definition (иногда при повторном ALTER)
    "42P05",  # duplicate_prepared_statement
})


def _is_already_exists(exc: BaseException) -> bool:
    """True, если ошибка = «объект уже существует» (миграция уже применялась)."""
    sqlstate = getattr(exc, "sqlstate", "") or ""
    if sqlstate in _ALREADY_EXISTS_SQLSTATES:
        return True
    return "already exists" in str(exc).lower()


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def discover_migrations(directory: Path = _MIGRATIONS_DIR) -> list[Migration]:
    """Найти все *.sql миграции и вернуть отсортированными по version."""
    out: list[Migration] = []
    if not directory.exists():
        return out
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        if path.suffix != ".sql":
            continue
        if path.name.endswith(_SKIP_SUFFIXES) or path.name in _SKIP_NAMES:
            continue
        out.append(Migration(
            version=path.stem,
            path=path,
            sql=path.read_text(encoding="utf-8"),
        ))
    return out


async def _connect(dsn: str):
    """Импортируем asyncpg внутри функции, чтобы модуль был
    легковесно импортируемым (для тестов discover_migrations и т.п.)."""
    import asyncpg

    # Конвертируем `postgresql+asyncpg://...` в чистый `postgresql://...`
    # asyncpg не любит SQLAlchemy-style URI.
    if dsn.startswith("postgresql+asyncpg://"):
        dsn = "postgresql://" + dsn[len("postgresql+asyncpg://"):]
    return await asyncpg.connect(dsn)


async def _applied_versions(conn) -> set[str]:
    rows = await conn.fetch(f"SELECT version FROM {_MIGRATIONS_TABLE}")
    return {r["version"] for r in rows}


async def migrate(
    *,
    dsn: str,
    target: Optional[str] = None,
    dry_run: bool = False,
    directory: Path = _MIGRATIONS_DIR,
) -> dict[str, list[str]]:
    """Применить все pending миграции.

    Args:
        dsn: postgres connection string. Принимается и `postgresql://`,
             и SQLAlchemy-style `postgresql+asyncpg://`.
        target: применять до этой версии включительно. None → все.
        dry_run: логировать что бы применилось, но не выполнять SQL.
        directory: где искать .sql файлы.

    Returns:
        {"applied": [...], "skipped": [...]} — две сортированные группы.
    """
    discovered = discover_migrations(directory)
    if not discovered:
        logger.info("No migrations found in %s", directory)
        return {"applied": [], "skipped": []}

    applied: list[str] = []
    skipped: list[str] = []

    try:
        conn = await _connect(dsn)
    except OSError as exc:
        # Постгрес не поднят — самая частая ошибка на шаге 4 быстрого старта.
        # Сырой трейсбек asyncpg на 40 строк тут ничего не объясняет: человек
        # видит WinError 1225 / Connection refused и не знает, что забыл
        # `docker compose up -d`. Показываем, куда стучались и что делать.
        safe = re.sub(r"://[^@/]*@", "://***@", dsn)
        raise SystemExit(
            f"Cannot reach Postgres at {safe}\n"
            f"  {type(exc).__name__}: {exc}\n\n"
            "The database is not accepting connections. Usually one of:\n"
            "  1. Infrastructure is not running — start it: docker compose up -d\n"
            "     (then wait a few seconds for the postgres healthcheck)\n"
            "  2. Docker itself is not running.\n"
            "  3. Your .env points somewhere else — check POSTGRES_HOST/PORT\n"
            "     or DATABASE_URL. Defaults match docker-compose.yml.\n\n"
            "To run without Postgres at all, see the zero-infrastructure tier\n"
            "in deploy/README.md (USE_NETWORKX=true, USE_QDRANT=false)."
        ) from exc

    try:
        # Bootstrap tracking-таблицы ВНЕ advisory_lock — иначе на холодной БД
        # advisory_lock не сможет даже завестись.
        await conn.execute(_BOOTSTRAP_SQL)

        # Распределённый лок: блокирует параллельные мигрейторы из разных реплик.
        await conn.execute("SELECT pg_advisory_lock($1)", _MIGRATION_LOCK_KEY)
        try:
            already = await _applied_versions(conn)

            for m in discovered:
                if target is not None and m.version > target:
                    skipped.append(m.version)
                    continue
                if m.version in already:
                    skipped.append(m.version)
                    continue

                logger.info("Applying migration %s (%s)", m.version, m.path.name)
                if dry_run:
                    applied.append(m.version)
                    continue

                # Per-migration транзакция — сама миграция или транзакционна
                # внутри (наши .sql файлы используют BEGIN/COMMIT), или
                # будет обёрнута здесь в неявную транзакцию connection'а.
                _record_sql = (
                    f"INSERT INTO {_MIGRATIONS_TABLE}(version, checksum) "
                    "VALUES ($1, $2) ON CONFLICT (version) DO NOTHING"
                )
                try:
                    async with conn.transaction():
                        await conn.execute(m.sql)
                        await conn.execute(_record_sql, m.version, m.checksum)
                    applied.append(m.version)
                except Exception as exc:  # noqa: BLE001 — классифицируем ниже
                    if not _is_already_exists(exc):
                        raise
                    # Объекты миграции уже есть (применялась до появления
                    # tracking-таблицы). Помечаем применённой и продолжаем —
                    # иначе раннер навсегда застрянет на легаси-миграции.
                    logger.warning(
                        "Migration %s: объекты уже существуют (%s) — "
                        "помечаю применённой и продолжаю",
                        m.version, str(exc)[:120],
                    )
                    await conn.execute(_record_sql, m.version, m.checksum)
                    applied.append(m.version)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _MIGRATION_LOCK_KEY)
    finally:
        await conn.close()

    return {"applied": sorted(applied), "skipped": sorted(skipped)}


def _resolve_dsn(cli_dsn: Optional[str]) -> str:
    if cli_dsn:
        return cli_dsn
    # Через Settings — чтобы подхватить env / .env.
    try:
        from backend.config import get_settings
        return get_settings().db_url
    except Exception as exc:
        raise SystemExit(f"Cannot resolve DSN (pass --dsn): {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Tessbrain — schema migration runner"
    )
    parser.add_argument("--dsn", help="postgres DSN (default: from Settings)")
    parser.add_argument("--target", help="apply up to this version (inclusive)")
    parser.add_argument("--dry-run", action="store_true", help="don't actually apply")
    parser.add_argument("--list", action="store_true", help="list discovered migrations and exit")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if args.list:
        for m in discover_migrations():
            print(f"{m.version}  {m.path.name}  sha256={m.checksum[:12]}")  # noqa: T201
        return 0

    dsn = _resolve_dsn(args.dsn)
    result = asyncio.run(migrate(
        dsn=dsn, target=args.target, dry_run=args.dry_run,
    ))

    print(f"applied: {len(result['applied'])} {result['applied']}")  # noqa: T201
    print(f"skipped: {len(result['skipped'])} {result['skipped']}")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
