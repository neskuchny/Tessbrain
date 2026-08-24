#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Перенести per-user реестры метрик (sqlite) в Postgres (D1e).

Читает data/metrics/<user_id>.db (sqlite MetricRegistry) и заливает метрики и
точки в таблицы миграции 279 через MetricRegistryPG. Идемпотентно по имени
метрики и источнику (replace_source_points). После заливки включите
METRIC_STORE_BACKEND=postgres. Файлы НЕ трогаются.

Запуск:
    PYTHONPATH=. python scripts/migrate_metrics_to_pg.py [--dry-run]
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from backend.core.store.tenant_paths import _DATA_ROOT
    from backend.core.ingest import access_repo
    from backend.core.ontology.metric_registry import (
        MetricRegistry, MetricRegistryPG,
    )

    mdir = Path(_DATA_ROOT) / "metrics"
    if not mdir.is_dir():
        print(f"нет каталога {mdir} — переносить нечего")
        return 0
    if not access_repo.engine_ready():
        print("Postgres недоступен — проверьте DATABASE_URL и миграцию 279.")
        return 1

    users = points_total = 0
    for f in sorted(mdir.glob("*.db")):
        uid = f.stem
        if not _UUID_RE.match(uid):
            continue
        src = MetricRegistry(str(f))
        # сгруппировать все точки по (source_type, source_id) — заливаем
        # через replace_source_points, чтобы повторный запуск не дублировал.
        groups: dict = defaultdict(list)
        for m in src.list_metrics():
            for p in src.series(m["name"]):
                groups[(p["source_type"], p.get("source_id") or "")].append({
                    "name": m["name"], "value": p["value"],
                    "period": p.get("period"), "at": p.get("at"),
                    "unit": m.get("unit"), "kind": p.get("kind") or "fact",
                    "detail": p.get("detail")})
        n_pts = sum(len(v) for v in groups.values())
        print(f"{uid}: {len(groups)} источников, {n_pts} точек")
        if args.dry_run:
            continue
        dst = MetricRegistryPG(uid)
        for (st, sid), pts in groups.items():
            dst.replace_source_points(st, sid, pts)
        users += 1
        points_total += n_pts

    verb = "будет перенесено" if args.dry_run else "перенесено"
    print(f"{verb}: {users} пользователей, {points_total} точек. "
          + ("" if args.dry_run else "Теперь можно включить METRIC_STORE_BACKEND=postgres."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
