# -*- coding: utf-8 -*-
"""Migration runner для Tessbrain Postgres (A.4).

Применяет SQL-миграции из backend/db/migrations/*.sql по порядку, с
tracking-таблицей `schema_migrations` (какие уже применены).

Использование:
    # Применить все непримененные миграции:
    python scripts/apply_migrations.py

    # Dry-run — показать что будет применено, без выполнения:
    python scripts/apply_migrations.py --dry-run

    # Применить конкретную миграцию (даже если в трекере есть):
    python scripts/apply_migrations.py --only 250_processed_meetings_org_dedup

    # Verify-only — проверить consistency без apply:
    python scripts/apply_migrations.py --verify

Connection: берётся из settings.db_url (env DATABASE_URL или
POSTGRES_* переменные). См. backend/config.py.

Безопасность:
  - Каждая миграция в своей транзакции; сбой одной не откатывает
    предыдущие применённые
  - Tracking-таблица фиксирует имя файла + sha256 + время + статус
  - Повторный запуск идемпотентен (применённые пропускаются)
  - Если содержимое файла изменилось после применения (sha256 mismatch)
    — WARNING, требуется --force чтобы перезапустить

ВНИМАНИЕ: миграции должны быть написаны идемпотентно (CREATE TABLE IF
NOT EXISTS, ADD COLUMN IF NOT EXISTS). Этот runner не делает rollback —
для отката используйте отдельные down-миграции или manual SQL.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from pathlib import Path

# tessent_brain root в sys.path
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_MIGRATIONS_DIR = _ROOT / "backend" / "db" / "migrations"

_TRACKING_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    id           BIGSERIAL PRIMARY KEY,
    filename     TEXT NOT NULL UNIQUE,
    sha256       TEXT NOT NULL,
    applied_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    status       TEXT NOT NULL DEFAULT 'applied',  -- applied | failed
    error        TEXT
);
"""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _discover_migrations() -> list[tuple[str, Path]]:
    """Вернуть [(name, path), ...] отсортированные по числовому префиксу.

    Файлы вида NNN_description.sql. Сортировка по числовому префиксу
    (003 < 020 < 250), а при равенстве — лексикографически (020 < 020b).
    """
    files = sorted(_MIGRATIONS_DIR.glob("*.sql"))

    def _sort_key(p: Path):
        stem = p.stem
        num_part = ""
        for ch in stem:
            if ch.isdigit():
                num_part += ch
            else:
                break
        num = int(num_part) if num_part else 999999
        return (num, stem)

    return [(p.stem, p) for p in sorted(files, key=_sort_key)]


async def _get_engine():
    from sqlalchemy.ext.asyncio import create_async_engine
    from backend.config import settings
    # Отдельный engine — не трогаем pool основного приложения.
    return create_async_engine(settings.db_url, echo=False, pool_pre_ping=True)


async def _ensure_tracking(engine) -> None:
    from sqlalchemy import text
    async with engine.begin() as conn:
        await conn.execute(text(_TRACKING_TABLE_SQL))


async def _get_applied(engine) -> dict[str, str]:
    """Вернуть {filename: sha256} уже применённых миграций."""
    from sqlalchemy import text
    async with engine.connect() as conn:
        result = await conn.execute(text(
            "SELECT filename, sha256 FROM public.schema_migrations "
            "WHERE status = 'applied'"
        ))
        rows = result.fetchall()
    return {r[0]: r[1] for r in rows}


async def _record(engine, *, filename: str, sha: str, status: str, error: str | None) -> None:
    from sqlalchemy import text
    async with engine.begin() as conn:
        await conn.execute(
            text("""
                INSERT INTO public.schema_migrations (filename, sha256, status, error)
                VALUES (:fn, :sha, :st, :err)
                ON CONFLICT (filename) DO UPDATE
                SET sha256 = EXCLUDED.sha256,
                    status = EXCLUDED.status,
                    error = EXCLUDED.error,
                    applied_at = now()
            """),
            {"fn": filename, "sha": sha, "st": status, "err": error},
        )


async def _apply_one(engine, name: str, path: Path) -> tuple[bool, str | None]:
    """Применить одну миграцию в транзакции. Returns (ok, error).

    Multi-statement SQL (CREATE TABLE + CREATE INDEX + CREATE POLICY + ...)
    нужно гонять через raw asyncpg.Connection.execute() — он использует
    simple query protocol (без prepared statement), который как раз
    разрешает несколько команд в одном запросе. exec_driver_sql() через
    SQLAlchemy всё равно префарит → PostgresSyntaxError 'cannot insert
    multiple commands into a prepared statement'.
    """
    sql = path.read_text(encoding="utf-8")
    try:
        async with engine.begin() as conn:
            raw = await conn.get_raw_connection()
            asyncpg_conn = raw.driver_connection  # asyncpg.Connection
            await asyncpg_conn.execute(sql)
        return True, None
    except Exception as e:
        return False, str(e)


async def run(args) -> int:
    print(f"📂 Migrations dir: {_MIGRATIONS_DIR}")
    migrations = _discover_migrations()
    print(f"   Found {len(migrations)} migration files\n")

    try:
        engine = await _get_engine()
    except Exception as e:
        print(f"❌ Cannot create DB engine: {e}")
        print("   Проверьте DATABASE_URL / POSTGRES_* env vars (backend/config.py)")
        return 2

    try:
        await _ensure_tracking(engine)
        applied = await _get_applied(engine)
    except Exception as e:
        print(f"❌ Cannot connect / init tracking table: {e}")
        return 2

    print(f"📊 Already applied: {len(applied)} migrations\n")

    if args.verify:
        # Проверка консистентности: для каждой applied миграции
        # сравнить sha256 с текущим файлом.
        mismatches = []
        for name, path in migrations:
            if name in applied:
                cur_sha = _sha256(path.read_text(encoding="utf-8"))
                if cur_sha != applied[name]:
                    mismatches.append(name)
        if mismatches:
            print("⚠️  SHA mismatch (файл изменён после применения):")
            for m in mismatches:
                print(f"    - {m}")
            print("\n   Это значит миграция была отредактирована после apply.")
            print("   Используйте --only <name> --force для повторного применения.")
            await engine.dispose()
            return 1
        print("✅ Verify OK — все применённые миграции совпадают с файлами")
        await engine.dispose()
        return 0

    to_apply: list[tuple[str, Path]] = []
    for name, path in migrations:
        if args.only and name != args.only:
            continue
        sha = _sha256(path.read_text(encoding="utf-8"))
        if name in applied and not args.force:
            if applied[name] != sha:
                print(f"⚠️  {name}: SHA changed — skip (use --force to re-apply)")
            continue
        to_apply.append((name, path))

    if not to_apply:
        print("✅ Nothing to apply — schema up to date")
        await engine.dispose()
        return 0

    print(f"🔧 To apply: {len(to_apply)} migrations:")
    for name, _ in to_apply:
        print(f"    - {name}")
    print()

    if args.dry_run:
        print("🟡 DRY RUN — ничего не выполнено")
        await engine.dispose()
        return 0

    failed = 0
    for name, path in to_apply:
        sha = _sha256(path.read_text(encoding="utf-8"))
        print(f"⏳ Applying {name} ...", end=" ", flush=True)
        ok, err = await _apply_one(engine, name, path)
        if ok:
            await _record(engine, filename=name, sha=sha, status="applied", error=None)
            print("✅")
        else:
            await _record(engine, filename=name, sha=sha, status="failed", error=err)
            print(f"❌\n    {err}")
            failed += 1
            if not args.continue_on_error:
                print("\n   Stopping (use --continue-on-error to skip failures)")
                break

    await engine.dispose()

    if failed:
        print(f"\n❌ {failed} migration(s) failed")
        return 1
    print(f"\n✅ Applied {len(to_apply)} migration(s) successfully")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Tessbrain migration runner")
    parser.add_argument("--dry-run", action="store_true",
                        help="показать что будет применено, без выполнения")
    parser.add_argument("--verify", action="store_true",
                        help="проверить consistency (sha256) без apply")
    parser.add_argument("--only", type=str, default=None,
                        help="применить только одну миграцию по имени (без .sql)")
    parser.add_argument("--force", action="store_true",
                        help="перезапустить даже если уже применена")
    parser.add_argument("--continue-on-error", action="store_true",
                        help="не останавливаться при сбое одной миграции")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
