#!/usr/bin/env python3
"""Миграция llm_usage из SQLite в PostgreSQL (issue #110 пакет 5).

Однократная миграция данных при включении PostgresUsageStore в проде.

Использование:
    # Сначала применить схему PG
    psql $DATABASE_URL -f backend/db/migrations/261_llm_usage.sql

    # Затем миграция данных
    LLM_USAGE_DSN=postgres://user:pw@host/db \\
        python scripts/migrate_llm_usage_to_pg.py \\
        --source data/llm_usage.db \\
        [--batch 1000] [--dry-run]

Идемпотентность: используется ON CONFLICT (id) DO NOTHING — повторный
запуск не дублирует. Если SQLite и PG имеют пересекающиеся id (после
backfill'а на проде), укажите --remap-ids для генерации новых.

Безопасность: не удаляет SQLite-файл. Удаление — отдельный шаг после
проверки консистентности (см. вывод скрипта).
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

# Запуск из репо корня — добавим в путь.
HERE = Path(__file__).parent.parent
sys.path.insert(0, str(HERE))


# Колонки, которые копируем (порядок важен — должен совпадать с PG-схемой
# в 261_llm_usage.sql). id вычисляется PG-серилкой при --remap-ids.
_COLUMNS = [
    "id", "timestamp", "provider", "model", "model_tier",
    "input_tokens", "output_tokens", "cached_tokens", "total_tokens",
    "input_cost", "output_cost", "cached_cost", "total_cost",
    "user_id", "session_id", "agent_mode", "request_type", "tenant_id",
    "document_id", "meeting_id", "job_id", "surface",
    "success", "error", "latency_ms",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True,
                        help="Путь к SQLite файлу (например data/llm_usage.db)")
    parser.add_argument("--batch", type=int, default=1000,
                        help="Размер пачки INSERT'ов (default 1000)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Сосчитать строки и показать первую партию SQL "
                             "без записи в PG")
    parser.add_argument("--remap-ids", action="store_true",
                        help="НЕ копировать id — PG сгенерирует новые "
                             "(используйте если PG уже имеет данные с пересекающимися id)")
    args = parser.parse_args()

    src_path = args.source
    if not os.path.isfile(src_path):
        print(f"❌ Source file not found: {src_path}", file=sys.stderr)
        return 1
    dsn = os.environ.get("LLM_USAGE_DSN", "").strip()
    if not dsn.startswith(("postgres://", "postgresql://")):
        print("❌ LLM_USAGE_DSN не задан или не postgres://...", file=sys.stderr)
        return 1

    # SQLite
    src = sqlite3.connect(src_path)
    src.row_factory = sqlite3.Row
    cursor = src.cursor()
    (total,) = cursor.execute("SELECT COUNT(*) FROM llm_usage").fetchone()
    print(f"📊 SQLite: {total} записей в {src_path}")

    if args.dry_run:
        cursor.execute(f"SELECT {', '.join(_COLUMNS)} FROM llm_usage "
                       "ORDER BY id LIMIT 3")
        for row in cursor.fetchall():
            print(f"  • id={row['id']} ts={row['timestamp']} "
                  f"model={row['model']} cost={row['total_cost']}")
        print("✅ Dry-run завершён (записи не переданы в PG)")
        return 0

    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        print("❌ psycopg2-binary не установлен. "
              "pip install psycopg2-binary", file=sys.stderr)
        return 1

    pg = psycopg2.connect(dsn)
    pg_cur = pg.cursor()

    cols_for_insert = [c for c in _COLUMNS if c != "id"] if args.remap_ids else _COLUMNS
    placeholders = ", ".join(["%s"] * len(cols_for_insert))
    cols_sql = ", ".join(cols_for_insert)
    conflict = "" if args.remap_ids else "ON CONFLICT (id) DO NOTHING"

    inserted = 0
    skipped = 0
    started = time.time()

    cursor.execute(f"SELECT {', '.join(_COLUMNS)} FROM llm_usage ORDER BY id")
    batch = []
    for row in cursor:
        values = tuple(row[c] for c in cols_for_insert)
        batch.append(values)
        if len(batch) >= args.batch:
            n = _flush_batch(pg_cur, cols_sql, placeholders, conflict, batch)
            inserted += n
            skipped += len(batch) - n
            batch.clear()
            pg.commit()
            print(f"  → {inserted}/{total}  ({inserted * 100 // total}%)")
    if batch:
        n = _flush_batch(pg_cur, cols_sql, placeholders, conflict, batch)
        inserted += n
        skipped += len(batch) - n
        pg.commit()

    elapsed = time.time() - started
    print(f"✅ Migration complete: {inserted} inserted, {skipped} skipped "
          f"(already in PG), {elapsed:.1f}s")
    print(f"   Source preserved at {src_path} — delete after verification.")
    src.close()
    pg.close()
    return 0


def _flush_batch(cur, cols_sql, placeholders, conflict, batch) -> int:
    """Вставить пачку и вернуть фактическое число вставленных строк."""
    import psycopg2.extras
    sql = f"INSERT INTO llm_usage ({cols_sql}) VALUES %s {conflict}"
    template = f"({placeholders})"
    psycopg2.extras.execute_values(cur, sql, batch, template=template,
                                   page_size=len(batch))
    # rowcount после execute_values — это число фактически вставленных строк
    return max(0, cur.rowcount)


if __name__ == "__main__":
    sys.exit(main())
