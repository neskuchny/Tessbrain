# -*- coding: utf-8 -*-
"""Очистка per-tenant temporal-стора от ЧУЖИХ записей (кросс-tenant утечка).

До фикса tenant-фильтра в EntityResolver резолв по имени матчил узлы чужих
аккаунтов в общем Neo4j, и их свойства (tenant_id, meeting_ids чужих встреч)
записывались в data/temporal/{tenant}.db через create_version. Панель
«Эволюция объекта» показывала чужого человека. Read-side фильтр в API уже
прячет такие записи; этот скрипт удаляет их из стора физически.

Запуск (из tessent_brain):
    python scripts/purge_foreign_temporal.py <user_id>            # dry-run
    python scripts/purge_foreign_temporal.py <user_id> --apply    # удалить

Чужая запись = версия, у которой data.tenant_id задан и НЕ входит в свои
tenant-ы (user + его организации + коллеги по ним). Записи без штампа
считаются своими (legacy / CREATE-ветка knowledge_sync).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply_changes = "--apply" in sys.argv
    if not args:
        print(__doc__)
        return 2
    user_id = args[0]

    from backend.core.store.tenant_scope import allowed_tenants, canonical_tenant

    allowed = allowed_tenants(user_id)
    tenant = canonical_tenant(user_id)
    db_path = os.path.join("data", "temporal", f"{tenant}.db")
    print(f"user_id:  {user_id}")
    print(f"tenant:   {tenant}")
    print(f"db:       {db_path}")
    print(f"свои tenant-ы ({len(allowed)}): {sorted(allowed)}")
    if not os.path.exists(db_path):
        print("Стор не найден — чистить нечего.")
        return 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 1) Чужие версии
    foreign_rows: list[tuple[int, str, str]] = []  # (rowid, entity_id, tenant)
    # AS rid: SQLite подставляет вместо rowid имя INTEGER PRIMARY KEY-колонки
    cur.execute("SELECT rowid AS rid, entity_id, data FROM versions")
    for row in cur.fetchall():
        try:
            d = json.loads(row["data"])
        except Exception:
            continue
        t = d.get("tenant_id") if isinstance(d, dict) else None
        if t not in (None, "") and str(t) not in allowed:
            foreign_rows.append((row["rid"], row["entity_id"], str(t)))

    if not foreign_rows:
        print("Чужих версий не найдено — стор чист.")
        conn.close()
        return 0

    by_entity: dict[str, int] = {}
    by_tenant: dict[str, int] = {}
    for _, eid, t in foreign_rows:
        by_entity[eid] = by_entity.get(eid, 0) + 1
        by_tenant[t] = by_tenant.get(t, 0) + 1

    print(f"\nЧужих версий: {len(foreign_rows)}")
    for t, n in sorted(by_tenant.items()):
        print(f"  из тенанта {t}: {n}")
    print("По сущностям:")
    for eid, n in sorted(by_entity.items()):
        print(f"  {eid}: {n} версий")

    # 2) Сущности, у которых ПОСЛЕ удаления чужих версий не останется ничего
    # своего — для них чистим и timeline_events/change_logs.
    fully_foreign: list[str] = []
    for eid in by_entity:
        cur.execute("SELECT COUNT(*) AS c FROM versions WHERE entity_id = ?", (eid,))
        total = cur.fetchone()["c"]
        if total == by_entity[eid]:
            fully_foreign.append(eid)
    if fully_foreign:
        print("\nЦеликом чужие сущности (уйдут вместе с timeline/change_logs):")
        for eid in fully_foreign:
            print(f"  {eid}")

    if not apply_changes:
        print("\nDRY-RUN: ничего не удалено. Повторите с --apply для удаления.")
        conn.close()
        return 0

    # 3) Удаление
    deleted_versions = 0
    for rowid, _, _ in foreign_rows:
        cur.execute("DELETE FROM versions WHERE rowid = ?", (rowid,))
        deleted_versions += cur.rowcount
    deleted_other = 0
    for eid in fully_foreign:
        for table in ("timeline_events", "change_logs", "audit_trail"):
            try:
                cur.execute(f"DELETE FROM {table} WHERE entity_id = ?", (eid,))
                deleted_other += cur.rowcount
            except sqlite3.OperationalError:
                pass  # таблицы может не быть в старых сторах
    conn.commit()
    conn.close()
    print(f"\nУдалено: {deleted_versions} версий, "
          f"{deleted_other} записей timeline/change_logs/audit_trail.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
