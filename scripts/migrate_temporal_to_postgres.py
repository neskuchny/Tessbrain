# -*- coding: utf-8 -*-
"""Перенос temporal-истории из per-user SQLite (data/temporal/*.db) в Postgres
(таблицы temporal_* из миграции 270).

Запуск (на staging/проде, где поднят Postgres и накатана миграция 270):

    python -m scripts.migrate_temporal_to_postgres            # все .db
    python -m scripts.migrate_temporal_to_postgres <tenant>   # один файл

Идемпотентность: строки вставляются ON CONFLICT DO NOTHING по натуральному
ключу (tenant_id, entity_id, version) — повторный прогон не задваивает.
tenant_id берётся из ИМЕНИ файла (data/temporal/<tenant>.db) — это уже
org∪user, как проставляет get_temporal_tracker.

ВНИМАНИЕ: скрипт только КОПИРУЕТ (SQLite не трогает) — откат = просто не
переключать TESSENT_TEMPORAL_BACKEND. Сверьте счётчики в конце.
"""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TEMPORAL_DIR = _ROOT / "data" / "temporal"


async def _migrate_one(db_path: Path) -> dict:
    from sqlalchemy import text
    from backend.db.postgres import get_postgres

    tenant = db_path.stem  # <tenant>.db → tenant
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    counts = {"versions": 0, "change_logs": 0, "timeline": 0}

    client = await get_postgres()
    async with client.session(apply_tenant=False) as s:
        await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})

        for r in conn.execute("SELECT * FROM versions").fetchall():
            await s.execute(text(
                "INSERT INTO temporal_versions "
                "(tenant_id, entity_id, entity_type, version, data, ts, changed_by, change_type) "
                "VALUES (:t,:e,:et,:v,CAST(:d AS JSONB),:ts,:cb,:ct) "
                "ON CONFLICT (tenant_id, entity_id, version) DO NOTHING"),
                {"t": tenant, "e": r["entity_id"], "et": r["entity_type"], "v": r["version"],
                 "d": r["data"], "ts": r["timestamp"], "cb": r["changed_by"], "ct": r["change_type"]})
            counts["versions"] += 1

        for r in conn.execute("SELECT * FROM change_logs").fetchall():
            await s.execute(text(
                "INSERT INTO temporal_change_logs "
                "(tenant_id, entity_id, version, field, old_value, new_value, ts, changed_by) "
                "VALUES (:t,:e,:v,:f,CAST(:ov AS JSONB),CAST(:nv AS JSONB),:ts,:cb)"),
                {"t": tenant, "e": r["entity_id"], "v": r["version"], "f": r["field"],
                 "ov": r["old_value"], "nv": r["new_value"], "ts": r["timestamp"], "cb": r["changed_by"]})
            counts["change_logs"] += 1

        for r in conn.execute("SELECT * FROM timeline_events").fetchall():
            await s.execute(text(
                "INSERT INTO temporal_timeline_events "
                "(tenant_id, entity_id, entity_type, event_type, event_data, event_time, meeting_id, description) "
                "VALUES (:t,:e,:et,:ev,CAST(:d AS JSONB),:tm,:m,:desc)"),
                {"t": tenant, "e": r["entity_id"], "et": r["entity_type"], "ev": r["event_type"],
                 "d": r["event_data"] or "{}", "tm": r["event_time"],
                 "m": r["meeting_id"], "desc": r["description"]})
            counts["timeline"] += 1

    conn.close()
    return {"tenant": tenant, **counts}


async def main(argv):
    if not _TEMPORAL_DIR.exists():
        print(f"нет каталога {_TEMPORAL_DIR}")
        return
    files = ([_TEMPORAL_DIR / f"{argv[0]}.db"] if argv
             else sorted(_TEMPORAL_DIR.glob("*.db")))
    total = []
    for f in files:
        if not f.exists():
            print(f"пропуск (нет файла): {f}")
            continue
        try:
            res = await _migrate_one(f)
            total.append(res)
            print(f"✅ {res['tenant']}: versions={res['versions']} "
                  f"change_logs={res['change_logs']} timeline={res['timeline']}")
        except Exception as e:  # noqa: BLE001
            print(f"❌ {f.name}: {e}")
    print(f"\nИтого перенесено файлов: {len(total)}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
