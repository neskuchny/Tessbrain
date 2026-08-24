"""User↔org membership: read + write helpers.

`user_org_mapping.json` лежит в data/, формат:
    {user_id: {org_id, role, access_level, joined_at}}

Раньше файл рассматривался как admin-managed (правился руками). Wave 2.2
ввёл writer-API через REST (см. backend/api/routes/orgs.py), поэтому
здесь появились:
  - add_member / remove_member / update_member_role / list_members
  - can_manage_org / is_org_member — для авторизационных проверок

Concurrency:
  - in-process: threading.Lock защищает read-modify-write
  - cross-process: fcntl.flock (best-effort, POSIX) на временный sentinel
  - atomic write: запись во временный файл + os.replace → атомарная
    подмена. Партиальные записи никогда не попадают в production-файл.

Role-модель (org-scoped, отдельно от system-level Role enum из rbac.py):
  founder    — создатель org, неотчуждаемые admin-права
  admin      — может управлять membership (кроме founder)
  manager    — может промоутить решения, видит расширенные данные
  employee   — стандартный member
  contractor — read-only член (для подрядчиков)
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# === Org-scoped roles (значения хранятся как строки в JSON) =================

ROLE_FOUNDER = "founder"
ROLE_ADMIN = "admin"
ROLE_MANAGER = "manager"
ROLE_EMPLOYEE = "employee"
ROLE_CONTRACTOR = "contractor"

ALL_ROLES = frozenset({ROLE_FOUNDER, ROLE_ADMIN, ROLE_MANAGER, ROLE_EMPLOYEE, ROLE_CONTRACTOR})

# Кто имеет admin-права на membership management в своей org.
MANAGEMENT_ROLES = frozenset({ROLE_FOUNDER, ROLE_ADMIN})

# Default access_level если caller не указал. INTERNAL.
DEFAULT_ACCESS_LEVEL = 2

# Кастомные роли (enterprise): орг может объявить свои имена ролей поверх
# встроенных — каждая кастомная роль резолвится в base ∈ ALL_ROLES\{founder}
# (см. org_store.get_custom_roles). Все проверки прав идут по base-роли,
# поэтому кастомная роль не может дать больше, чем её base.
CUSTOM_ROLE_BASES = frozenset({ROLE_ADMIN, ROLE_MANAGER, ROLE_EMPLOYEE, ROLE_CONTRACTOR})


def resolve_base_role(role: Optional[str], org_id: Optional[str]) -> Optional[str]:
    """Свести роль (встроенную или кастомную) к встроенной base-роли.

    Неизвестная роль → None (fail-closed: вызывающие трактуют как
    минимальные права)."""
    if not role:
        return None
    r = str(role).strip().lower()
    if r in ALL_ROLES:
        return r
    if org_id:
        try:
            from backend.core.ingest import org_store
            custom = org_store.get_custom_roles(org_id)
            spec = custom.get(r)
            if isinstance(spec, dict):
                base = str(spec.get("base") or "").strip().lower()
                if base in CUSTOM_ROLE_BASES:
                    return base
        except Exception:
            logger.debug("custom role resolve failed", exc_info=True)
    return None


def get_user_org_base_role(user_id: str) -> Optional[str]:
    """Base-роль user'а в его активной org (кастомная → её base)."""
    return resolve_base_role(get_user_org_role(user_id), get_org_for_user(user_id))


def _user_org_mapping_path() -> str:
    """Lazy-resolver. Импорт внутри функции, потому что часть тестов в проекте
    подменяет sys.modules['backend.core.store.tenant_paths'] стабом —
    module-level import тут бы зафиксировал старую ссылку и сломал коллекцию.
    """
    from backend.core.store.tenant_paths import user_org_mapping_path
    return user_org_mapping_path()


# === Кэш с mtime-инвалидацией (in-process) ==================================

_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, dict[str, Any]] | None = None
_CACHE_MTIME: float = 0.0


def _pg() -> bool:
    """Postgres-бэкенд стора доступов включён? (D1a; иначе — файл)."""
    try:
        from backend.core.ingest import access_repo
        return access_repo.enabled()
    except Exception:
        return False


def _load(path: str) -> dict[str, dict[str, Any]]:
    """Загрузить mapping. Never-raise: при ошибке вернёт {}.

    Postgres-бэкенд: отдаёт всю коллекцию (внутри writer-транзакции — снапшот
    под локом). Файловый — читает JSON."""
    if _pg():
        try:
            from backend.core.ingest import access_repo
            return access_repo.load_all("memberships")
        except Exception as e:  # noqa: BLE001
            logger.warning("membership PG load failed: %s", e)
            return {}
    try:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning("user_org_mapping.json: top-level must be object, got %s", type(data).__name__)
            return {}
        return data
    except Exception as e:
        logger.warning("Failed to read user_org_mapping.json: %s", e)
        return {}


def _get_mapping() -> dict[str, dict[str, Any]]:
    """Вернуть текущий mapping (из кэша, перечитать если mtime сменился)."""
    global _CACHE, _CACHE_MTIME
    path = _user_org_mapping_path()
    if _pg():
        # PG-бэкенд сам кэширует по TTL (access_repo); mtime неприменим.
        return _load(path)
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
    """Сбросить кэш — для тестов и hot-reload."""
    global _CACHE, _CACHE_MTIME
    with _CACHE_LOCK:
        _CACHE = None
        _CACHE_MTIME = 0.0
    if _pg():
        try:
            from backend.core.ingest import access_repo
            access_repo.invalidate("memberships")
        except Exception:
            logger.debug("membership PG cache invalidate skipped", exc_info=True)


# === Read API ==============================================================


def get_org_for_user(user_id: str) -> Optional[str]:
    """Вернуть org_id, к которому привязан user_id, или None если solo.

    None означает «работаем только в personal-graph, не promote'им».
    Это нормальный режим для Mini Tess-only-юзеров и solo-founders.
    """
    if not user_id:
        return None
    entry = _get_mapping().get(user_id)
    if not isinstance(entry, dict):
        return None
    org_id = entry.get("org_id")
    if isinstance(org_id, str) and org_id:
        return org_id
    return None


def get_user_org_role(user_id: str) -> Optional[str]:
    """Роль user'а в его org. None если solo."""
    if not user_id:
        return None
    entry = _get_mapping().get(user_id)
    if not isinstance(entry, dict):
        return None
    role = entry.get("role")
    return role if isinstance(role, str) and role else None


# === Мульти-орг членство (enterprise) ======================================
# Модель: `org_id` в записи — АКТИВНАЯ (primary) организация; все enforcement-
# пути (видимость графа, роли, отделы) работают по ней, как раньше. Плюс
# опциональный словарь `secondary_orgs: {org_id: {role, department?,
# access_level, joined_at}}` — членства «про запас» (директор портфеля состоит
# в холдинге и в дочках). Переключение активной org — switch_primary_org:
# поля активного членства (role/department/reports_to/person_entity_id)
# перекладываются в secondary_orgs, выбранное membership поднимается наверх.


def _secondary(entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    sec = entry.get("secondary_orgs")
    return sec if isinstance(sec, dict) else {}


def get_orgs_for_user(user_id: str) -> list[dict[str, Any]]:
    """Все членства user'а: [{org_id, role, department, primary}]. Активная
    org первой. Пусто для solo."""
    if not user_id:
        return []
    entry = _get_mapping().get(user_id)
    if not isinstance(entry, dict):
        return []
    out: list[dict[str, Any]] = []
    primary_org = entry.get("org_id")
    if isinstance(primary_org, str) and primary_org:
        out.append({"org_id": primary_org, "role": entry.get("role"),
                    "department": entry.get("department"), "primary": True})
    for oid, m in _secondary(entry).items():
        if isinstance(m, dict) and oid != primary_org:
            out.append({"org_id": oid, "role": m.get("role"),
                        "department": m.get("department"), "primary": False})
    return out


def is_org_member_any(user_id: str, org_id: str) -> bool:
    """Член ли user org'а хоть каким членством (активным или secondary)."""
    return any(m["org_id"] == org_id for m in get_orgs_for_user(user_id))


def switch_primary_org(user_id: str, org_id: str) -> bool:
    """Сделать org_id активной организацией user'а.

    Требование: членство в org_id уже существует (активное — no-op True,
    или secondary). Орг-скоупные поля активного членства (role, department,
    reports_to, person_entity_id, access_level) уезжают в secondary_orgs
    прежней org и поднимаются из secondary_orgs новой."""
    if not (user_id and org_id):
        return False
    path = _user_org_mapping_path()
    _ORG_FIELDS = ("role", "department", "reports_to", "person_entity_id",
                   "access_level", "joined_at")

    def _txn() -> bool:
        current = _load(path)
        entry = current.get(user_id)
        if not isinstance(entry, dict):
            return False
        old_org = entry.get("org_id")
        if old_org == org_id:
            return True  # уже активная
        sec = dict(_secondary(entry))
        target = sec.pop(org_id, None)
        if not isinstance(target, dict):
            return False  # членства в org_id нет
        # активное членство → в secondary
        if isinstance(old_org, str) and old_org:
            sec[old_org] = {k: entry[k] for k in _ORG_FIELDS if k in entry}
        # выбранное членство → наверх
        for k in _ORG_FIELDS:
            entry.pop(k, None)
        entry.update({k: v for k, v in target.items() if k in _ORG_FIELDS})
        entry["org_id"] = org_id
        entry["secondary_orgs"] = sec
        if not sec:
            entry.pop("secondary_orgs", None)
        current[user_id] = entry
        _atomic_write_json(path, current)
        return True

    with _CACHE_LOCK:
        ok = _with_file_lock(path, _txn)
    clear_cache()
    return ok


def get_user_org_department(user_id: str) -> Optional[str]:
    """Отдел user'а в его org (P2 мульти-аккаунта: видимость чужих отделов
    ограничена — см. access/levels). None если не задан / solo."""
    if not user_id:
        return None
    entry = _get_mapping().get(user_id)
    if not isinstance(entry, dict):
        return None
    dept = entry.get("department")
    return dept if isinstance(dept, str) and dept.strip() else None


def set_member_department(user_id: str, org_id: str,
                          department: Optional[str]) -> bool:
    """Задать (или сбросить: None/'') отдел участника. False если user
    не в этом org. Отдел влияет на видимость: контент отдела читается
    другими отделами только с уровня руководителя."""
    if not (user_id and org_id):
        return False
    dept = (department or "").strip()
    path = _user_org_mapping_path()

    def _txn() -> bool:
        current = _load(path)
        entry = current.get(user_id)
        if not isinstance(entry, dict) or entry.get("org_id") != org_id:
            return False
        if dept:
            entry["department"] = dept
        else:
            entry.pop("department", None)
        current[user_id] = entry
        _atomic_write_json(path, current)
        return True

    with _CACHE_LOCK:
        updated = _with_file_lock(path, _txn)
    clear_cache()
    return updated


def get_user_org_reports_to(user_id: str) -> Optional[str]:
    """user_id руководителя этого участника (иерархия), либо None.

    Иерархия нужна для синхронизации уровней «отдел↔генеральный» и
    «руководитель↔руководитель» (CogniLayer Ф0). Отсутствие reports_to у
    участника управленческой роли трактуется как верх иерархии."""
    if not user_id:
        return None
    entry = _get_mapping().get(user_id)
    if not isinstance(entry, dict):
        return None
    rt = entry.get("reports_to")
    return str(rt) if rt else None


def set_member_reports_to(user_id: str, org_id: str,
                          reports_to: Optional[str]) -> bool:
    """Задать (или сбросить) руководителя участника. False если user не в этом
    org. reports_to должен быть членом того же org (иначе не пишем)."""
    if not (user_id and org_id):
        return False
    rt = (reports_to or "").strip()
    if rt == user_id:
        return False  # сам себе руководитель — некорректно
    path = _user_org_mapping_path()

    def _txn() -> bool:
        current = _load(path)
        entry = current.get(user_id)
        if not isinstance(entry, dict) or entry.get("org_id") != org_id:
            return False
        if rt:
            mgr = current.get(rt)
            if not isinstance(mgr, dict) or mgr.get("org_id") != org_id:
                return False  # руководитель не в этом org
            entry["reports_to"] = rt
        else:
            entry.pop("reports_to", None)
        current[user_id] = entry
        _atomic_write_json(path, current)
        return True

    with _CACHE_LOCK:
        updated = _with_file_lock(path, _txn)
    clear_cache()
    return updated


def get_user_person_entity(user_id: str) -> Optional[str]:
    """person_entity_id графа, сшитый с этим аккаунтом, либо None.

    Сшивка «логин = Person-сущность из встреч» (CogniLayer Ф0) — фундамент под
    единый когнитивный профиль и синхронизацию по сотрудникам."""
    if not user_id:
        return None
    entry = _get_mapping().get(user_id)
    if not isinstance(entry, dict):
        return None
    pid = entry.get("person_entity_id")
    return str(pid) if pid else None


def find_user_by_person_entity(person_entity_id: str, org_id: str) -> Optional[str]:
    """Обратный резолв: аккаунт, сшитый с этой Person-сущностью, в рамках org."""
    if not (person_entity_id and org_id):
        return None
    for uid, entry in _get_mapping().items():
        if not isinstance(entry, dict) or entry.get("org_id") != org_id:
            continue
        if str(entry.get("person_entity_id") or "") == str(person_entity_id):
            return uid
    return None


def set_member_person_entity(user_id: str, org_id: str,
                             person_entity_id: Optional[str]) -> bool:
    """Сшить (или расшить: None/'') аккаунт с Person-сущностью графа. False если
    user не в этом org. Одна Person не должна быть сшита с двумя аккаунтами:
    при конфликте расшиваем предыдущий (последняя сшивка — источник истины)."""
    if not (user_id and org_id):
        return False
    pid = (person_entity_id or "").strip()
    path = _user_org_mapping_path()

    def _txn() -> bool:
        current = _load(path)
        entry = current.get(user_id)
        if not isinstance(entry, dict) or entry.get("org_id") != org_id:
            return False
        if pid:
            # расшиваем прежнего владельца этой Person в том же org
            for other_uid, other in current.items():
                if (other_uid != user_id and isinstance(other, dict)
                        and other.get("org_id") == org_id
                        and str(other.get("person_entity_id") or "") == pid):
                    other.pop("person_entity_id", None)
                    current[other_uid] = other
            entry["person_entity_id"] = pid
        else:
            entry.pop("person_entity_id", None)
        current[user_id] = entry
        _atomic_write_json(path, current)
        return True

    with _CACHE_LOCK:
        updated = _with_file_lock(path, _txn)
    clear_cache()
    return updated


def is_org_member(user_id: str, org_id: str) -> bool:
    """Принадлежит ли user_id указанному org_id (любая роль)."""
    if not user_id or not org_id:
        return False
    return get_org_for_user(user_id) == org_id


def can_manage_org(user_id: str, org_id: str) -> bool:
    """Может ли user_id управлять membership этого org'а.

    True для founder/admin своего org. False для members, не-членов
    и для членов другой org. System-admin'ы проверяются отдельным каналом
    (на уровне route — см. _require_org_admin_or_system_admin).
    """
    if not (user_id and org_id):
        return False
    if get_org_for_user(user_id) != org_id:
        # Наследование по дереву организаций: founder/admin родительской
        # организации управляет дочерними (холдинг → корпорация → филиал).
        try:
            from backend.core.ingest import org_store
            for ancestor in org_store.get_ancestors(org_id):
                if (get_org_for_user(user_id) == ancestor
                        and resolve_base_role(get_user_org_role(user_id),
                                              ancestor) in MANAGEMENT_ROLES):
                    return True
        except Exception:
            logger.debug("hierarchical manage check failed", exc_info=True)
        return False
    role = resolve_base_role(get_user_org_role(user_id), org_id)
    return role in MANAGEMENT_ROLES


def list_members(org_id: str) -> list[dict[str, Any]]:
    """Список всех members указанного org_id.

    Каждый элемент: `{user_id, role, access_level, joined_at}`.
    Пустой список если org не существует или не имеет members.
    """
    if not org_id:
        return []
    mapping = _get_mapping()
    if _pg():
        # Fast-path (D1a.2): выборка по индексу org_id вместо скана всех
        # пользователей (главный O(all)-скан в файловой модели).
        try:
            from backend.core.ingest import access_repo
            mapping = access_repo.docs_by_index("memberships", "org_id", org_id)
        except Exception:
            logger.debug("list_members fast-path failed, fallback to scan",
                         exc_info=True)
    out: list[dict[str, Any]] = []
    for uid, entry in mapping.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("org_id") != org_id:
            continue
        out.append({
            "user_id": uid,
            "role": entry.get("role"),
            "access_level": entry.get("access_level", DEFAULT_ACCESS_LEVEL),
            "department": entry.get("department"),
            # Иерархия и сшивка идентичности (CogniLayer Ф0): кто руководитель
            # этого участника и с какой Person-сущностью графа он сшит.
            "reports_to": entry.get("reports_to"),
            "person_entity_id": entry.get("person_entity_id"),
            "joined_at": entry.get("joined_at"),
        })
    return out


# === Write API (atomic + lock) =============================================


def _atomic_write_json(path: str, data: dict[str, Any]) -> None:
    """Атомарно записать JSON. Падение в середине → старый файл цел + tempfile
    удалён (без утечки диска при ре-try на ошибках сериализации).

    POSIX-гарантия: os.replace атомарен (rename внутри одной FS).
    Postgres-бэкенд: пишет diff в БД (внутри активной writer-транзакции)."""
    if _pg():
        from backend.core.ingest import access_repo
        access_repo.replace_all("memberships", data)
        return
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    # delete=False: мы сами управляем удалением через os.replace.
    tmp_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=parent,
            prefix=".tmp_mapping_", suffix=".json", delete=False,
        ) as tmp:
            tmp_name = tmp.name
            json.dump(data, tmp, ensure_ascii=False, indent=2, sort_keys=True)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)  # POSIX atomic
        tmp_name = None  # успех — не нужно cleanup
    finally:
        if tmp_name and os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                logger.debug("tempfile cleanup suppressed", exc_info=True)


def _with_file_lock(path: str, fn):
    """Выполнить fn() удерживая cross-process advisory lock на path.lock.

    fcntl.flock — POSIX-only. Если platform не поддерживает (Windows),
    fallback на чистый in-process threading.Lock (мы его уже держим).
    Best-effort: блокировка advisory, чужие writer'ы могут её игнорировать,
    но штатные пути все через эту функцию идут.
    """
    if _pg():
        # Postgres: xact advisory-лок + снапшот транзакции; _load/_atomic_write
        # внутри fn маршрутизируются на ту же транзакцию.
        from backend.core.ingest import access_repo
        with access_repo.locked_txn("memberships"):
            return fn()
    lock_path = path + ".lock"
    try:
        import fcntl  # POSIX-only
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


def _validate_role(role: str, org_id: Optional[str] = None) -> str:
    """Валидная роль: встроенная либо кастомная роль этого org'а."""
    role = (role or "").strip().lower()
    if role in ALL_ROLES:
        return role
    if org_id and resolve_base_role(role, org_id) is not None:
        return role
    raise ValueError(
        f"unknown role '{role}', allowed: {sorted(ALL_ROLES)} "
        "или кастомная роль организации"
    )


def add_member(
    user_id: str,
    org_id: str,
    *,
    role: str = ROLE_EMPLOYEE,
    access_level: int = DEFAULT_ACCESS_LEVEL,
    secondary: bool = False,
) -> dict[str, Any]:
    """Добавить user_id в org_id с указанной ролью.

    Если user уже состоит в другом org:
      - secondary=False (default) — raises ValueError (прежняя семантика:
        одна активная org);
      - secondary=True — членство пишется в secondary_orgs, активная org
        не меняется (директор портфеля состоит в холдинге и дочках;
        переключение — switch_primary_org).

    Если user уже в этом же org — обновляем role/access_level (idempotent).
    """
    if not (user_id and org_id):
        raise ValueError("user_id and org_id are required")
    role = _validate_role(role, org_id)

    path = _user_org_mapping_path()

    def _txn():
        # read-modify-write под локом → атомарная транзакция.
        current = _load(path)
        existing = current.get(user_id)
        if isinstance(existing, dict):
            existing_org = existing.get("org_id")
            if existing_org and existing_org != org_id:
                if not secondary:
                    raise ValueError(
                        f"user {user_id} already in org {existing_org}; "
                        "remove first before adding to another org "
                        "(или secondary=true для второго членства)"
                    )
                # второе членство: активная org не трогается
                sec = dict(existing.get("secondary_orgs") or {})
                prev = sec.get(org_id) if isinstance(sec.get(org_id), dict) else {}
                sec[org_id] = {
                    "role": role,
                    "access_level": int(access_level),
                    "joined_at": prev.get("joined_at")
                    or datetime.now(timezone.utc).isoformat(),
                }
                if isinstance(prev.get("department"), str) and prev["department"].strip():
                    sec[org_id]["department"] = prev["department"]
                existing["secondary_orgs"] = sec
                current[user_id] = existing
                _atomic_write_json(path, current)
                return {"org_id": org_id, **sec[org_id], "secondary": True}
        joined_at = (existing or {}).get("joined_at") or \
            datetime.now(timezone.utc).isoformat()
        new_entry = {
            "org_id": org_id,
            "role": role,
            "access_level": int(access_level),
            "joined_at": joined_at,
        }
        # идемпотентный re-add не должен терять отдел (P2) и вторые членства
        prev_dept = (existing or {}).get("department")
        if isinstance(prev_dept, str) and prev_dept.strip():
            new_entry["department"] = prev_dept
        prev_sec = (existing or {}).get("secondary_orgs")
        if isinstance(prev_sec, dict) and prev_sec:
            new_entry["secondary_orgs"] = prev_sec
        for keep in ("reports_to", "person_entity_id"):
            if existing and keep in existing:
                new_entry[keep] = existing[keep]
        current[user_id] = new_entry
        _atomic_write_json(path, current)
        return current[user_id]

    with _CACHE_LOCK:
        entry = _with_file_lock(path, _txn)
    clear_cache()  # invalidate, next read перечитает с диска
    return {"user_id": user_id, **entry}


def remove_member(user_id: str, org_id: str) -> bool:
    """Удалить user из org. Возвращает True если запись была.

    Активное членство: если есть secondary-членства — активной становится
    первое из них (аккаунт не теряет остальные организации); иначе запись
    удаляется целиком. Secondary-членство просто вычёркивается.
    """
    if not (user_id and org_id):
        return False
    path = _user_org_mapping_path()

    def _txn() -> bool:
        current = _load(path)
        entry = current.get(user_id)
        if not isinstance(entry, dict):
            return False
        sec = dict(entry.get("secondary_orgs") or {})
        if entry.get("org_id") == org_id:
            if sec:
                # промоутим первое secondary-членство в активное
                next_org, next_m = next(iter(sec.items()))
                sec.pop(next_org, None)
                for k in ("role", "department", "reports_to",
                          "person_entity_id", "access_level", "joined_at"):
                    entry.pop(k, None)
                entry.update({k: v for k, v in (next_m or {}).items()})
                entry["org_id"] = next_org
                if sec:
                    entry["secondary_orgs"] = sec
                else:
                    entry.pop("secondary_orgs", None)
                current[user_id] = entry
            else:
                del current[user_id]
            _atomic_write_json(path, current)
            return True
        if org_id in sec:
            sec.pop(org_id, None)
            if sec:
                entry["secondary_orgs"] = sec
            else:
                entry.pop("secondary_orgs", None)
            current[user_id] = entry
            _atomic_write_json(path, current)
            return True
        return False

    with _CACHE_LOCK:
        removed = _with_file_lock(path, _txn)
    clear_cache()
    return removed


def update_member_role(
    user_id: str,
    org_id: str,
    new_role: str,
) -> bool:
    """Сменить роль user'а в org. False если user не в этом org."""
    if not (user_id and org_id):
        return False
    new_role = _validate_role(new_role, org_id)
    path = _user_org_mapping_path()

    def _txn() -> bool:
        current = _load(path)
        entry = current.get(user_id)
        if not isinstance(entry, dict) or entry.get("org_id") != org_id:
            return False
        entry["role"] = new_role
        current[user_id] = entry
        _atomic_write_json(path, current)
        return True

    with _CACHE_LOCK:
        updated = _with_file_lock(path, _txn)
    clear_cache()
    return updated
