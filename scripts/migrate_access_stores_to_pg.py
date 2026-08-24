#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Перенести сторы доступов из JSON-файлов в Postgres (D1a).

Читает существующие файлы (user_org_mapping.json, orgs_metadata.json,
holdings.json) и заливает их в таблицы миграции 276. Идемпотентно (upsert).
После заливки включите ORG_STORE_BACKEND=postgres.

Запуск:
    PYTHONPATH=. python scripts/migrate_access_stores_to_pg.py [--dry-run]

Требует поднятый Postgres с применённой миграцией 276 и настроенный DATABASE_URL
(или POSTGRES_* в .env). НЕ трогает файлы (их можно удалить вручную после сверки).
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _read_json(path: str) -> dict:
    if not os.path.exists(path):
        print(f"  (нет файла {path} — пропуск)")
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:  # noqa: BLE001
        print(f"  ! не прочитать {path}: {e}")
        return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="показать что будет залито, без записи")
    args = ap.parse_args()

    from backend.core.store.tenant_paths import (
        _DATA_ROOT, user_org_mapping_path,
    )
    from backend.core.ingest import access_repo

    sources = {
        "memberships": user_org_mapping_path(),
        "orgs": str(_DATA_ROOT / "orgs_metadata.json"),
        "holdings": str(_DATA_ROOT / "holdings.json"),
        "invites": str(_DATA_ROOT / "org_invites.json"),
    }

    # Заставляем access_repo поднять движок независимо от флага бэкенда.
    if not access_repo._init_engine():  # noqa: SLF001
        print("Postgres недоступен — проверьте DATABASE_URL/POSTGRES_* и "
              "применённые миграции 276/277.")
        return 1

    # broadcasts: файл-на-org (data/broadcasts/<org>.json) → строка-на-org.
    bdir = _DATA_ROOT / "broadcasts"
    broadcasts: dict = {}
    if bdir.is_dir():
        for f in bdir.glob("*.json"):
            doc = _read_json(str(f))
            if doc:
                broadcasts[f.stem] = doc

    total = 0
    for collection, data in {**{c: _read_json(p) for c, p in sources.items()},
                             "broadcasts": broadcasts}.items():
        print(f"{collection}: {len(data)} записей")
        if args.dry_run or not data:
            continue
        # Заливаем через ту же транзакционную дорожку (diff-upsert от пустого →
        # вставит всё). locked_txn требует, чтобы движок был поднят (см. выше).
        with access_repo.locked_txn(collection):
            current = access_repo.load_all(collection)
            current.update(data)  # upsert поверх существующего
            access_repo.replace_all(collection, current)
        total += len(data)
        print(f"  → залито ({len(data)})")

    if args.dry_run:
        print("\n[dry-run] ничего не записано.")
    else:
        print(f"\nГотово: перенесено {total} записей. "
              "Теперь можно выставить ORG_STORE_BACKEND=postgres.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
