# -*- coding: utf-8 -*-
"""Postgres-бэкенд чат-сессий и реестра папок (D1c).

Раньше переписки жили в data/chat_sessions/<user>/<session>.json — файлы
не дают транзакций/конкурентности/multi-node и выпадают из ретеншна БД.
Здесь — строка-на-сессию (PK user_id+session_id), doc JSONB = тот же объект,
что лежал в файле. Логика sessions.py не меняется: маршрутизируются только
IO-хелперы (_load/_save/_delete/_list + реестр папок).

Точечный доступ (НЕ load_all, в отличие от access_repo): у пользователя могут
быть мегабайты переписок — читаем/пишем по (user_id, session_id), списки —
только своего пользователя по индексу.

Sync (psycopg2, общий движок access_repo): вызывается из хендлеров так же
инлайново, как раньше файловый IO. За флагом CHAT_STORE_BACKEND (default file).
Fail-safe: настроен postgres, но БД недоступна → громкий откат на файл.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_backend_decided: Optional[str] = None


def backend() -> str:
    """'postgres' если настроено И БД доступна, иначе 'file'."""
    global _backend_decided
    if _backend_decided is not None:
        return _backend_decided
    try:
        from backend.config import settings
        want = getattr(settings, "chat_store_backend", "file")
    except Exception:
        want = "file"
    if want == "postgres":
        try:
            from backend.core.ingest import access_repo
            if access_repo.engine_ready():
                _backend_decided = "postgres"
                return _backend_decided
            logger.error("chat_session_store: Postgres недоступен — "
                         "откат на файловый бэкенд чатов")
        except Exception:
            logger.error("chat_session_store: движок не поднялся — откат на файл",
                         exc_info=True)
    _backend_decided = "file"
    return _backend_decided


def enabled() -> bool:
    return backend() == "postgres"


def reset_for_tests() -> None:
    global _backend_decided
    _backend_decided = None


def _session():
    from backend.core.ingest import access_repo
    return access_repo.pg_session()


def _doc(row_doc: Any) -> dict:
    return json.loads(row_doc) if isinstance(row_doc, str) else row_doc


def get_session(user_id: str, session_id: str) -> Optional[Dict[str, Any]]:
    from sqlalchemy import text
    with _session() as s:
        row = s.execute(text(
            "SELECT doc FROM chat_session_store "
            "WHERE user_id = :u AND session_id = :s"),
            {"u": user_id, "s": session_id}).fetchone()
    return _doc(row.doc) if row else None


def put_session(user_id: str, session_id: str, data: Dict[str, Any]) -> None:
    from sqlalchemy import text
    with _session() as s:
        s.execute(text(
            "INSERT INTO chat_session_store (user_id, session_id, doc, updated_at) "
            "VALUES (:u, :s, CAST(:d AS JSONB), now()) "
            "ON CONFLICT (user_id, session_id) "
            "DO UPDATE SET doc = EXCLUDED.doc, updated_at = now()"),
            {"u": user_id, "s": session_id,
             "d": json.dumps(data, ensure_ascii=False)})


def delete_session(user_id: str, session_id: str) -> bool:
    from sqlalchemy import text
    with _session() as s:
        res = s.execute(text(
            "DELETE FROM chat_session_store "
            "WHERE user_id = :u AND session_id = :s"),
            {"u": user_id, "s": session_id})
    return bool(getattr(res, "rowcount", 0))


def list_sessions(user_id: str) -> List[Dict[str, Any]]:
    """Все сессии пользователя (по индексу user_id), свежие первыми."""
    from sqlalchemy import text
    with _session() as s:
        rows = s.execute(text(
            "SELECT doc FROM chat_session_store WHERE user_id = :u "
            "ORDER BY updated_at DESC"), {"u": user_id}).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = _doc(r.doc)
        # прежняя file-защита: владелец в документе обязан совпадать
        if isinstance(d, dict) and d.get("user_id") == user_id:
            out.append(d)
    out.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return out


def get_folders(user_id: str) -> List[str]:
    from sqlalchemy import text
    with _session() as s:
        row = s.execute(text(
            "SELECT doc FROM chat_folder_store WHERE user_id = :u"),
            {"u": user_id}).fetchone()
    if not row:
        return []
    d = _doc(row.doc)
    return [str(x) for x in (d.get("folders") or [])] if isinstance(d, dict) else []


def put_folders(user_id: str, folders: List[str]) -> None:
    from sqlalchemy import text
    payload = {"folders": sorted(set(folders))[:100]}
    with _session() as s:
        s.execute(text(
            "INSERT INTO chat_folder_store (user_id, doc, updated_at) "
            "VALUES (:u, CAST(:d AS JSONB), now()) "
            "ON CONFLICT (user_id) DO UPDATE SET doc = EXCLUDED.doc, "
            "updated_at = now()"),
            {"u": user_id, "d": json.dumps(payload, ensure_ascii=False)})
