# -*- coding: utf-8 -*-
"""Postgres-бэкенд для сторов доступов (D1a): членства/холдинги/орг-метадата.

Зачем: раньше эти сторы жили в JSON-файлах, которые переписывались целиком на
каждую мутацию (multi-node/NFS небезопасно, O(all) на операцию). Здесь —
row-per-entity в Postgres, транзакционно, с advisory-локом на writer'а.

Ключевое ограничение: `membership`/`org_store`/`holdings` — СИНХРОННЫЕ и зовутся
из sync-путей (фильтрация графа по правам). Поэтому здесь sync-движок (psycopg2),
а НЕ async — чтобы не делать async-рябь по всему коду. Логика сторов не меняется:
маршрутизируются только примитивы персистентности (`_load`/`_atomic_write`/лок).

Модель: каждый стор = «коллекция» {pk -> doc}. `doc` (JSONB) хранит полную запись
как в файле. Чтения — полный load с TTL-кэшем; записи — под xact-advisory-локом
через `locked_txn(...)`, `replace_all(...)` делает diff против снапшота начала
транзакции (upsert изменённых, delete удалённых) — типично O(1) на мутацию.

Fail-safe: `backend()` возвращает 'postgres' ТОЛЬКО если так настроено И движок
поднялся. Если настроен postgres, но БД недоступна — громкий warning и откат на
'file' (приложение не падает; оператор включает postgres, когда БД реально есть).
"""
from __future__ import annotations

import contextlib
import copy
import json
import logging
import threading
import time
from contextvars import ContextVar
from typing import Any, Callable, Dict, Iterator, Optional

logger = logging.getLogger(__name__)

# collection -> (table, pk_column, {indexed_column: doc_key})
_COLLECTIONS: Dict[str, tuple[str, str, Dict[str, str]]] = {
    "memberships": ("org_membership_store", "user_id", {"org_id": "org_id"}),
    "orgs": ("org_metadata_store", "org_id", {"parent_org_id": "parent_org_id"}),
    "holdings": ("holding_store", "holding_id", {"owner_user_id": "owner_user_id"}),
    "invites": ("org_invite_store", "invite_id",
                {"org_id": "org_id", "token_hash": "token_hash"}),
    "broadcasts": ("broadcast_store", "org_id", {}),
}

_CACHE_TTL = 2.0  # сек — ограниченная staleness чтений на multi-node

_engine = None
_SessionLocal = None
_engine_lock = threading.Lock()
_backend_decided: Optional[str] = None

# TTL-кэш полного load по коллекции: {collection: (ts, dict)}
_load_cache: Dict[str, tuple[float, Dict[str, Any]]] = {}
_cache_lock = threading.Lock()

# Активная транзакция на коллекцию (для маршрутизации in-txn _load/_atomic_write):
# {collection: {"session":Session, "snapshot":dict}}
_TXN: ContextVar[Dict[str, Any]] = ContextVar("access_repo_txn", default={})


def _init_engine() -> bool:
    """Поднять sync-движок (idempotent). True если готов."""
    global _engine, _SessionLocal
    if _SessionLocal is not None:
        return True
    with _engine_lock:
        if _SessionLocal is not None:
            return True
        try:
            from sqlalchemy import create_engine
            from sqlalchemy.orm import sessionmaker
            from backend.config import settings
            _engine = create_engine(
                settings.db_url_sync, pool_pre_ping=True,
                pool_size=5, max_overflow=5, future=True,
                # doc-словари содержат кириллицу; psycopg2 по умолчанию может
                # взять ASCII client_encoding и падать на UnicodeEncodeError.
                connect_args={"client_encoding": "utf8"})
            # быстрый health: коннект + SELECT 1
            from sqlalchemy import text
            with _engine.connect() as c:
                c.execute(text("SELECT 1"))
            _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False,
                                         future=True)
            return True
        except Exception as e:  # noqa: BLE001
            logger.error("access_repo: sync-движок Postgres не поднялся (%s) — "
                         "откат на файловый бэкенд сторов доступов", e)
            _engine = None
            _SessionLocal = None
            return False


def backend() -> str:
    """'postgres' если настроено И БД доступна, иначе 'file'. Решение кэшируется."""
    global _backend_decided
    if _backend_decided is not None:
        return _backend_decided
    try:
        from backend.config import settings
        want = getattr(settings, "org_store_backend", "file")
    except Exception:
        want = "file"
    if want == "postgres" and _init_engine():
        _backend_decided = "postgres"
    else:
        _backend_decided = "file"
    return _backend_decided


def reset_for_tests() -> None:
    """Сбросить решение о бэкенде и кэши (для тестов)."""
    global _backend_decided, _engine, _SessionLocal
    with _engine_lock:
        _backend_decided = None
        _engine = None
        _SessionLocal = None
    with _cache_lock:
        _load_cache.clear()


def enabled() -> bool:
    return backend() == "postgres"


def engine_ready() -> bool:
    """Поднять sync-движок независимо от org-флага (для других sync-сторов:
    чат-сессии D1c, скрипты миграции). True если БД доступна."""
    return _init_engine()


@contextlib.contextmanager
def pg_session() -> Iterator[Any]:
    """Публичная sync-сессия общего движка (commit по выходу, rollback при
    исключении). Для сторов вне access-коллекций (chat_session_store и т.п.)."""
    if not _init_engine():
        raise RuntimeError("Postgres sync-движок недоступен")
    assert _SessionLocal is not None
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _key(collection: str) -> int:
    """Стабильный int-ключ коллекции для pg_advisory_xact_lock."""
    return {"memberships": 776001, "orgs": 776002, "holdings": 776003,
            "invites": 776004, "broadcasts": 776005}.get(collection, 776000)


def _select_all(session, collection: str) -> Dict[str, Any]:
    from sqlalchemy import text
    table, pk, _ = _COLLECTIONS[collection]
    rows = session.execute(text(f"SELECT {pk} AS k, doc FROM {table}")).fetchall()
    out: Dict[str, Any] = {}
    for r in rows:
        doc = r.doc
        if isinstance(doc, str):
            doc = json.loads(doc)
        out[str(r.k)] = doc
    return out


def load_all(collection: str) -> Dict[str, Any]:
    """Полный словарь коллекции {pk: doc}. TTL-кэш (2с). Если внутри активной
    транзакции этой коллекции — отдаём снапшот транзакции (свежий, под локом)."""
    txn = _TXN.get().get(collection)
    if txn is not None:
        return copy.deepcopy(txn["snapshot"])
    now = time.time()
    with _cache_lock:
        cached = _load_cache.get(collection)
        if cached and (now - cached[0]) < _CACHE_TTL:
            return copy.deepcopy(cached[1])
    assert _SessionLocal is not None
    with _SessionLocal() as session:
        data = _select_all(session, collection)
    with _cache_lock:
        _load_cache[collection] = (now, data)
    return copy.deepcopy(data)


def invalidate(collection: str) -> None:
    with _cache_lock:
        _load_cache.pop(collection, None)


def docs_by_index(collection: str, column: str, value: str) -> Dict[str, Any]:
    """Fast-path (D1a.2): выборка по индексному столбцу — {pk: doc} только
    подходящих строк, без загрузки всей коллекции (list_members по org_id,
    consume-invite по token_hash). Внутри активной транзакции — фильтр по
    её снапшоту (консистентность с locked_txn)."""
    table, pk, indexed = _COLLECTIONS[collection]
    if column not in indexed:
        raise ValueError(f"{collection}: столбец {column} не индексирован")
    txn = _TXN.get().get(collection)
    if txn is not None:
        doc_key = indexed[column]
        return {k: copy.deepcopy(v) for k, v in txn["snapshot"].items()
                if isinstance(v, dict) and str(v.get(doc_key) or "") == str(value)}
    from sqlalchemy import text
    assert _SessionLocal is not None
    with _SessionLocal() as session:
        rows = session.execute(
            text(f"SELECT {pk} AS k, doc FROM {table} WHERE {column} = :v"),
            {"v": str(value)}).fetchall()
    out: Dict[str, Any] = {}
    for r in rows:
        doc = r.doc
        if isinstance(doc, str):
            doc = json.loads(doc)
        out[str(r.k)] = doc
    return out


@contextlib.contextmanager
def locked_txn(collection: str) -> Iterator[None]:
    """Транзакция-writer: xact advisory-лок + снапшот начала транзакции.
    Внутри блока `load_all(collection)` отдаёт снапшот, `replace_all(...)` пишет
    diff. По выходу — commit (или rollback при исключении)."""
    from sqlalchemy import text
    assert _SessionLocal is not None
    session = _SessionLocal()
    try:
        session.execute(text("SELECT pg_advisory_xact_lock(:k)"),
                        {"k": _key(collection)})
        snapshot = _select_all(session, collection)
        current = dict(_TXN.get())
        current[collection] = {"session": session, "snapshot": snapshot}
        token = _TXN.set(current)
        try:
            yield
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            restored = dict(_TXN.get())
            restored.pop(collection, None)
            _TXN.reset(token)
    finally:
        session.close()
        invalidate(collection)


def replace_all(collection: str, new: Dict[str, Any]) -> None:
    """Записать новое состояние коллекции. ТОЛЬКО внутри locked_txn: diff против
    снапшота начала транзакции → upsert изменённых, delete удалённых."""
    from sqlalchemy import text
    txn = _TXN.get().get(collection)
    if txn is None:
        raise RuntimeError("replace_all вне locked_txn")
    session = txn["session"]
    snapshot: Dict[str, Any] = txn["snapshot"]
    table, pk, indexed = _COLLECTIONS[collection]

    removed = [k for k in snapshot if k not in new]
    changed = [k for k, v in new.items()
               if k not in snapshot or snapshot[k] != v]

    if removed:
        session.execute(
            text(f"DELETE FROM {table} WHERE {pk} = ANY(:ks)"),
            {"ks": list(removed)})

    cols = [pk] + list(indexed.keys()) + ["doc", "updated_at"]
    idx_cols = list(indexed.keys())
    set_clause = ", ".join(
        [f"{c} = EXCLUDED.{c}" for c in idx_cols]
        + ["doc = EXCLUDED.doc", "updated_at = now()"])
    placeholders = ", ".join([f":{pk}"] + [f":{c}" for c in idx_cols]
                             + ["CAST(:doc AS JSONB)", "now()"])
    sql = text(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({pk}) DO UPDATE SET {set_clause}")
    for k in changed:
        doc = new[k]
        params: Dict[str, Any] = {pk: k, "doc": json.dumps(doc, ensure_ascii=False)}
        for col, doc_key in indexed.items():
            val = doc.get(doc_key) if isinstance(doc, dict) else None
            params[col] = str(val) if val not in (None, "") else None
        session.execute(sql, params)


def guarded_replace(collection: str,
                    mutator: Callable[[Dict[str, Any]], Any]) -> Any:
    """Read-modify-write под локом. mutator получает текущий словарь (снапшот),
    мутирует его на месте (как файловые _txn), возвращает retval. Мы пишем diff.
    Возвращает retval mutator'а."""
    with locked_txn(collection):
        current = load_all(collection)  # копия снапшота
        retval = mutator(current)
        replace_all(collection, current)
    return retval
