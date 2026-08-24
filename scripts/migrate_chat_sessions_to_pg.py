#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Перенести чат-сессии и реестры папок из файлов в Postgres (D1c).

Читает data/chat_sessions/<user_id>/*.json (+ _folders.json) и заливает в
таблицы миграции 278. Идемпотентно (upsert). После заливки включите
CHAT_STORE_BACKEND=postgres. Файлы НЕ трогаются (удалить вручную после сверки).

Запуск:
    PYTHONPATH=. python scripts/migrate_chat_sessions_to_pg.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
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
    from backend.core.store import chat_session_store as cs

    root = Path(_DATA_ROOT) / "chat_sessions"
    if not root.is_dir():
        print(f"нет каталога {root} — переносить нечего")
        return 0
    if not access_repo.engine_ready():
        print("Postgres недоступен — проверьте DATABASE_URL и миграцию 278.")
        return 1

    n_sessions = n_folders = 0
    for udir in sorted(root.iterdir()):
        if not (udir.is_dir() and _UUID_RE.match(udir.name)):
            continue
        uid = udir.name
        for f in udir.glob("*.json"):
            if f.name == "_folders.json":
                try:
                    folders = json.loads(f.read_text(encoding="utf-8")).get("folders") or []
                except Exception as e:  # noqa: BLE001
                    print(f"  ! {f}: {e}")
                    continue
                if not args.dry_run:
                    cs.put_folders(uid, [str(x) for x in folders])
                n_folders += 1
                continue
            sid = f.stem
            if not _UUID_RE.match(sid):
                continue
            try:
                doc = json.loads(f.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001
                print(f"  ! {f}: {e}")
                continue
            if not (isinstance(doc, dict) and doc.get("user_id") == uid):
                print(f"  ! {f}: user_id в документе не совпадает с каталогом — пропуск")
                continue
            if not args.dry_run:
                cs.put_session(uid, sid, doc)
            n_sessions += 1

    verb = "будет перенесено" if args.dry_run else "перенесено"
    print(f"{verb}: {n_sessions} сессий, {n_folders} реестров папок. "
          + ("" if args.dry_run else "Теперь можно включить CHAT_STORE_BACKEND=postgres."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
