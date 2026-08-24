"""Org metadata store: name, created_by, created_at.

Membership-mapping (membership.py) хранит связи user↔org. Сами org-объекты
до сих пор существовали только как UUID без атрибутов. Wave 2.2 ввёл
admin API `POST /orgs`, поэтому нужно где-то держать metadata: имя
компании, кто создал, когда.

Отдельный файл `data/orgs_metadata.json` (а не вторая секция в mapping)
по двум причинам:
  - чистое разделение ответственности: mapping = граф user↔org,
    metadata = справочник орг;
  - lifecycle разный: org живёт даже если все members разбежались
    (исторический контекст для GDPR-аудита, future re-onboarding).

Concurrency: те же atomic-write + flock паттерны, что в membership.py.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, dict[str, Any]] | None = None
_CACHE_MTIME: float = 0.0


def _orgs_metadata_path() -> str:
    """Lazy: tenant_paths модуль подменяется в части тестов."""
    from backend.core.store.tenant_paths import _DATA_ROOT
    return str(Path(_DATA_ROOT) / "orgs_metadata.json")


def _pg() -> bool:
    """Postgres-бэкенд стора доступов включён? (D1a; иначе — файл)."""
    try:
        from backend.core.ingest import access_repo
        return access_repo.enabled()
    except Exception:
        return False


def _load(path: str) -> dict[str, dict[str, Any]]:
    if _pg():
        try:
            from backend.core.ingest import access_repo
            return access_repo.load_all("orgs")
        except Exception as e:  # noqa: BLE001
            logger.warning("org_store PG load failed: %s", e)
            return {}
    try:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning("orgs_metadata.json: top-level must be object")
            return {}
        return data
    except Exception as e:
        logger.warning("Failed to read orgs_metadata.json: %s", e)
        return {}


def _get_orgs() -> dict[str, dict[str, Any]]:
    """Кэш с mtime-инвалидацией."""
    global _CACHE, _CACHE_MTIME
    path = _orgs_metadata_path()
    if _pg():
        return _load(path)  # PG сам кэширует по TTL
    try:
        mtime = os.path.getmtime(path) if os.path.exists(path) else 0.0
    except OSError:
        mtime = 0.0
    with _CACHE_LOCK:
        if _CACHE is None or mtime != _CACHE_MTIME:
            _CACHE = _load(path)
            _CACHE_MTIME = mtime
        return _CACHE


def clear_cache() -> None:
    global _CACHE, _CACHE_MTIME
    with _CACHE_LOCK:
        _CACHE = None
        _CACHE_MTIME = 0.0
    if _pg():
        try:
            from backend.core.ingest import access_repo
            access_repo.invalidate("orgs")
        except Exception:
            logger.debug("org_store PG cache invalidate skipped", exc_info=True)


def _atomic_write(path: str, data: dict[str, Any]) -> None:
    """Атомарная запись с cleanup tempfile при exception в json.dump.
    Postgres-бэкенд: diff-запись в БД внутри активной writer-транзакции."""
    if _pg():
        from backend.core.ingest import access_repo
        access_repo.replace_all("orgs", data)
        return
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    tmp_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=parent,
            prefix=".tmp_orgs_", suffix=".json", delete=False,
        ) as tmp:
            tmp_name = tmp.name
            json.dump(data, tmp, ensure_ascii=False, indent=2, sort_keys=True)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
    finally:
        if tmp_name and os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                logger.debug("tempfile cleanup suppressed", exc_info=True)


def _with_file_lock(path: str, fn):
    if _pg():
        from backend.core.ingest import access_repo
        with access_repo.locked_txn("orgs"):
            return fn()
    lock_path = path + ".lock"
    try:
        import fcntl
    except ImportError:
        return fn()
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fn()
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            logger.debug("flock release suppressed", exc_info=True)
        os.close(fd)


def create_org(org_id: str, *, name: str, created_by: str,
               parent_org_id: Optional[str] = None) -> dict[str, Any]:
    """Создать новую org. Идемпотентно по org_id: повторный вызов с тем же
    UUID не перезаписывает существующую запись, raise ValueError.

    parent_org_id — родительская организация в дереве холдинг→корпорация→
    филиал. Родитель обязан существовать. Право задавать родителя проверяет
    route (управление родителем)."""
    if not (org_id and name and created_by):
        raise ValueError("org_id, name, created_by are required")
    path = _orgs_metadata_path()

    def _txn():
        current = _load(path)
        if org_id in current:
            raise ValueError(f"org {org_id} already exists")
        if parent_org_id:
            if parent_org_id == org_id:
                raise ValueError("org cannot be its own parent")
            if parent_org_id not in current:
                raise ValueError(f"parent org {parent_org_id} not found")
        current[org_id] = {
            "name": name,
            "created_by": created_by,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if parent_org_id:
            current[org_id]["parent_org_id"] = parent_org_id
        _atomic_write(path, current)
        return current[org_id]

    with _CACHE_LOCK:
        meta = _with_file_lock(path, _txn)
    clear_cache()
    return {"org_id": org_id, **meta}


# === Дерево организаций (enterprise) =======================================


def set_parent_org(org_id: str, parent_org_id: Optional[str]) -> bool:
    """Переподвесить org в дереве (None — сделать корнем). Цикл/самородитель/
    несуществующий родитель → ValueError. False если org нет."""
    if not org_id:
        return False
    path = _orgs_metadata_path()

    def _txn() -> bool:
        current = _load(path)
        if org_id not in current:
            return False
        if parent_org_id:
            if parent_org_id == org_id:
                raise ValueError("org cannot be its own parent")
            if parent_org_id not in current:
                raise ValueError(f"parent org {parent_org_id} not found")
            # цикл: поднимаемся от нового родителя вверх — не встретим ли org_id
            seen = set()
            cur = parent_org_id
            while cur and cur not in seen:
                if cur == org_id:
                    raise ValueError("cycle in org tree")
                seen.add(cur)
                cur = (current.get(cur) or {}).get("parent_org_id")
            current[org_id]["parent_org_id"] = parent_org_id
        else:
            current[org_id].pop("parent_org_id", None)
        _atomic_write(path, current)
        return True

    with _CACHE_LOCK:
        ok = _with_file_lock(path, _txn)
    clear_cache()
    return ok


def get_ancestors(org_id: str) -> list[str]:
    """Родители снизу вверх: [parent, grandparent, ...]. Устойчиво к битым
    циклам (обрывается)."""
    orgs = _get_orgs()
    out: list[str] = []
    seen = {org_id}
    cur = (orgs.get(org_id) or {}).get("parent_org_id")
    while isinstance(cur, str) and cur and cur not in seen:
        out.append(cur)
        seen.add(cur)
        cur = (orgs.get(cur) or {}).get("parent_org_id")
    return out


def get_children(org_id: str) -> list[dict[str, Any]]:
    """Прямые дочерние организации."""
    if not org_id:
        return []
    return [{"org_id": oid, **meta} for oid, meta in _get_orgs().items()
            if isinstance(meta, dict) and meta.get("parent_org_id") == org_id]


def get_descendants(org_id: str) -> list[str]:
    """Все потомки (BFS), без самого org_id."""
    out: list[str] = []
    queue = [org_id]
    seen = {org_id}
    while queue:
        cur = queue.pop(0)
        for child in get_children(cur):
            cid = child["org_id"]
            if cid not in seen:
                seen.add(cid)
                out.append(cid)
                queue.append(cid)
    return out


def org_tree(org_id: str) -> Optional[dict[str, Any]]:
    """Поддерево от org_id: {org_id, name, children: [...]}. None если нет."""
    meta = get_org(org_id)
    if not meta:
        return None
    return {
        "org_id": org_id,
        "name": meta.get("name") or org_id,
        "parent_org_id": meta.get("parent_org_id"),
        "children": [t for c in get_children(org_id)
                     if (t := org_tree(c["org_id"]))],
    }


# === Кастомные роли (enterprise) ===========================================
# Хранятся в метадате org: custom_roles = {role_name: {base, title?,
# max_access_level?}}. base ∈ {admin, manager, employee, contractor} —
# founder делегировать нельзя. Все проверки прав идут по base (см.
# membership.resolve_base_role); max_access_level может только СУЗИТЬ
# клиренс относительно base (см. access/levels.py).


def get_custom_roles(org_id: str) -> dict[str, dict[str, Any]]:
    meta = _get_orgs().get(org_id)
    if not isinstance(meta, dict):
        return {}
    roles = meta.get("custom_roles")
    return roles if isinstance(roles, dict) else {}


def set_custom_roles(org_id: str, roles: dict[str, dict[str, Any]]) -> bool:
    """Заменить реестр кастомных ролей org'а. Валидация: имя — slug до 40
    симв., base — не founder, max_access_level 0..5. False если org нет."""
    if not org_id:
        return False
    from backend.core.ingest.membership import ALL_ROLES, CUSTOM_ROLE_BASES
    cleaned: dict[str, dict[str, Any]] = {}
    for name, spec in (roles or {}).items():
        key = str(name).strip().lower()[:40]
        if not key or key in ALL_ROLES or not isinstance(spec, dict):
            raise ValueError(f"недопустимое имя кастомной роли: '{name}'")
        base = str(spec.get("base") or "").strip().lower()
        if base not in CUSTOM_ROLE_BASES:
            raise ValueError(
                f"роль '{key}': base обязан быть одним из {sorted(CUSTOM_ROLE_BASES)}")
        entry: dict[str, Any] = {"base": base}
        if spec.get("title"):
            entry["title"] = str(spec["title"])[:80]
        if spec.get("max_access_level") is not None:
            lvl = int(spec["max_access_level"])
            if not 0 <= lvl <= 5:
                raise ValueError(f"роль '{key}': max_access_level 0..5")
            entry["max_access_level"] = lvl
        cleaned[key] = entry

    path = _orgs_metadata_path()

    def _txn() -> bool:
        current = _load(path)
        if org_id not in current:
            return False
        if cleaned:
            current[org_id]["custom_roles"] = cleaned
        else:
            current[org_id].pop("custom_roles", None)
        _atomic_write(path, current)
        return True

    with _CACHE_LOCK:
        ok = _with_file_lock(path, _txn)
    clear_cache()
    return ok


def get_org(org_id: str) -> Optional[dict[str, Any]]:
    """Получить метадату org, None если нет такой."""
    if not org_id:
        return None
    meta = _get_orgs().get(org_id)
    if not isinstance(meta, dict):
        return None
    return {"org_id": org_id, **meta}


def list_orgs() -> list[dict[str, Any]]:
    """Все известные org (для system-admin листинга)."""
    return [
        {"org_id": oid, **meta}
        for oid, meta in _get_orgs().items()
        if isinstance(meta, dict)
    ]
