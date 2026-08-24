# -*- coding: utf-8 -*-
"""
Единая модель прав на ресурсы (docs/ru/ACCESS_CONTROL_DESIGN.md, S1+S2+S5;
механизмы — по образцу MeetFlow ACCESS_CONTROL).

Четыре проверки по порядку (check_permission, ЧИСТАЯ функция):
1. Владелец → полный доступ.
2. Прямой user-грант → его уровень.
3. Team-грант → уровень гранта, НО не выше потолка роли участника
   (TEAM_ROLE_PERMISSION_CAP: viewer→read, member→write, admin→admin).
4. Каскад по родителям (block→project и т.п.) — те же проверки выше.

БЕЗОПАСНОСТЬ ПО ПОСТРОЕНИЮ:
- аддитивно: нет грантов на ресурс → решает только владение (прежняя
  логика «моё/не моё» не меняется ни для одного существующего ресурса);
- fail-closed: неизвестный уровень/роль → NONE/отказ;
- ядро — чистые функции (тестируются без БД); БД-слой тонкий и
  best-effort (Postgres недоступен → пустые гранты → прежняя логика);
- флаг enable_resource_permissions (default ON) гейтит ТОЛЬКО API
  управления грантами; сама проверка безопасна всегда.
"""
from __future__ import annotations

import datetime
import logging
from typing import Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# приоритеты уровней: admin покрывает write и read
_PRIORITY = {"read": 1, "write": 2, "admin": 3}

# потолок фактических прав при доступе через команду (MeetFlow-механизм)
TEAM_ROLE_PERMISSION_CAP = {
    "viewer": "read",
    "member": "write",
    "admin": "admin",
    "owner": "admin",
}

RESOURCE_TYPES = ("project", "dataset", "artifact", "report",
                  "snapshot", "session")
PERMISSION_TYPES = ("read", "write", "admin")


def permission_satisfies(actual: Optional[str], required: str) -> bool:
    """actual достаточно для required? Неизвестное → False (fail-closed)."""
    return _PRIORITY.get(str(actual or "").lower(), 0) >= \
        _PRIORITY.get(str(required or "").lower(), 99)


def _cap(level: str, role: Optional[str]) -> Optional[str]:
    """Потолок team-гранта по роли участника. Неизвестная роль → None
    (fail-closed: доступ через команду не даётся)."""
    cap = TEAM_ROLE_PERMISSION_CAP.get(str(role or "").lower())
    if cap is None:
        return None
    return level if _PRIORITY[level] <= _PRIORITY[cap] else cap


def _grant_active(g: dict, now: Optional[datetime.datetime] = None) -> bool:
    exp = g.get("expires_at")
    if not exp:
        return True
    try:
        dt = datetime.datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt > (now or datetime.datetime.now(datetime.timezone.utc))
    except (ValueError, AttributeError):
        return False    # битый срок → fail-closed


def effective_level(user_id: str, *,
                    owner_id: Optional[str],
                    grants: Sequence[dict],
                    team_roles: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Фактический уровень пользователя на ОДНОМ ресурсе (без каскада).

    grants — гранты этого ресурса [{grantee_type, grantee_id,
    permission_type, expires_at}]; team_roles — {team_id: роль юзера}."""
    if owner_id is not None and str(user_id) == str(owner_id):
        return "owner"
    best: Optional[str] = None

    def _better(level: Optional[str]) -> Optional[str]:
        if level is None:
            return best
        if best is None or _PRIORITY.get(level, 0) > _PRIORITY.get(best, 0):
            return level
        return best

    for g in grants or []:
        if not _grant_active(g):
            continue
        level = str(g.get("permission_type") or "").lower()
        if level not in PERMISSION_TYPES:
            continue
        gtype = str(g.get("grantee_type") or "user")
        gid = str(g.get("grantee_id") or "")
        if gtype == "user" and gid == str(user_id):
            best = _better(level)
        elif gtype == "team" and gid in (team_roles or {}):
            best = _better(_cap(level, (team_roles or {})[gid]))
    return best


def check_permission(user_id: str, required: str, *,
                     owner_id: Optional[str],
                     grants: Sequence[dict],
                     parents: Optional[List[dict]] = None,
                     team_roles: Optional[Dict[str, str]] = None) -> dict:
    """Главная проверка: ресурс + каскад по родителям.

    parents — цепочка вверх [{owner_id, grants}] (например блок →
    проект): право на родителя действует на детей (MeetFlow-каскад).
    Возвращает {allowed, level, via} — via объясняет, откуда право."""
    level = effective_level(user_id, owner_id=owner_id, grants=grants,
                            team_roles=team_roles)
    if level == "owner" or permission_satisfies(level, required):
        return {"allowed": True, "level": level, "via": "direct"}
    for i, p in enumerate(parents or []):
        plevel = effective_level(
            user_id, owner_id=p.get("owner_id"),
            grants=p.get("grants") or [], team_roles=team_roles)
        if plevel == "owner" or permission_satisfies(plevel, required):
            return {"allowed": True, "level": plevel,
                    "via": f"cascade:{p.get('label') or i}"}
    return {"allowed": False, "level": level, "via": None}


# ──────────────────────────────────────────────────────────────────────
# Тонкий БД-слой (Postgres; best-effort — нет БД → пустые гранты)
# ──────────────────────────────────────────────────────────────────────

async def fetch_grants(resource_type: str, resource_id: str) -> List[dict]:
    try:
        from sqlalchemy import text

        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session() as session:
            res = await session.execute(
                text("SELECT grantee_type, grantee_id, permission_type, "
                     "expires_at FROM resource_permissions "
                     "WHERE resource_type = :rt AND resource_id = :rid"),
                {"rt": resource_type, "rid": str(resource_id)})
            return [dict(r._mapping) for r in res.fetchall()]
    except Exception as e:
        logger.debug(f"fetch_grants unavailable: {e}")
        return []


async def fetch_grants_for_grantee(grantee_id: str,
                                   resource_type: Optional[str] = None) -> List[dict]:
    """Обратный lookup (P1 мульти-аккаунта): что расшарено ЭТОМУ пользователю.

    Нужен для «Доступные мне» в списках (напр. SIMA-проекты коллег).
    Только активные user-гранты; best-effort (нет Postgres → [])."""
    if not grantee_id:
        return []
    try:
        from sqlalchemy import text

        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        q = ("SELECT resource_type, resource_id, permission_type, expires_at "
             "FROM resource_permissions "
             "WHERE grantee_type = 'user' AND grantee_id = :gid")
        params: dict = {"gid": str(grantee_id)}
        if resource_type:
            q += " AND resource_type = :rt"
            params["rt"] = resource_type
        async with pg.session() as session:
            res = await session.execute(text(q), params)
            rows = [dict(r._mapping) for r in res.fetchall()]
        return [r for r in rows if _grant_active(r)]
    except Exception as e:
        logger.debug(f"fetch_grants_for_grantee unavailable: {e}")
        return []


async def grant(actor_id: str, *, resource_type: str, resource_id: str,
                grantee_type: str, grantee_id: str,
                permission_type: str,
                expires_at: Optional[str] = None) -> dict:
    """Выдать/заменить грант (UPSERT по уникальной паре) + аудит."""
    if resource_type not in RESOURCE_TYPES:
        return {"success": False,
                "error": f"resource_type из {RESOURCE_TYPES}"}
    if permission_type not in PERMISSION_TYPES:
        return {"success": False,
                "error": f"permission_type из {PERMISSION_TYPES}"}
    if grantee_type not in ("user", "team"):
        return {"success": False, "error": "grantee_type: user|team"}
    try:
        from sqlalchemy import text

        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session() as session:
            await session.execute(
                text("""
                    INSERT INTO resource_permissions
                        (resource_type, resource_id, grantee_type,
                         grantee_id, permission_type, granted_by, expires_at)
                    VALUES (:rt, :rid, :gt, :gid, :pt, :by,
                            CAST(:exp AS timestamptz))
                    ON CONFLICT (resource_type, resource_id,
                                 grantee_type, grantee_id)
                    DO UPDATE SET permission_type = :pt,
                                  granted_by = :by,
                                  expires_at = CAST(:exp AS timestamptz)
                """),
                {"rt": resource_type, "rid": str(resource_id),
                 "gt": grantee_type, "gid": str(grantee_id),
                 "pt": permission_type, "by": str(actor_id),
                 "exp": expires_at})
        await audit(actor_id, "grant", resource_type, resource_id,
                    {"grantee": f"{grantee_type}:{grantee_id}",
                     "level": permission_type})
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def revoke(actor_id: str, *, resource_type: str, resource_id: str,
                 grantee_type: str, grantee_id: str) -> dict:
    try:
        from sqlalchemy import text

        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session() as session:
            res = await session.execute(
                text("DELETE FROM resource_permissions WHERE "
                     "resource_type = :rt AND resource_id = :rid AND "
                     "grantee_type = :gt AND grantee_id = :gid"),
                {"rt": resource_type, "rid": str(resource_id),
                 "gt": grantee_type, "gid": str(grantee_id)})
        await audit(actor_id, "revoke", resource_type, resource_id,
                    {"grantee": f"{grantee_type}:{grantee_id}"})
        return {"success": True, "revoked": res.rowcount > 0}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def audit(actor_id: str, action: str, resource_type: str,
                resource_id: str, detail: Optional[dict] = None) -> None:
    """Аудит-запись (best-effort — не роняет основную операцию)."""
    try:
        import json as _json

        from sqlalchemy import text

        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session() as session:
            await session.execute(
                text("INSERT INTO permission_audit (actor_id, action, "
                     "resource_type, resource_id, detail) VALUES "
                     "(:a, :act, :rt, :rid, CAST(:d AS jsonb))"),
                {"a": str(actor_id), "act": action, "rt": resource_type,
                 "rid": str(resource_id),
                 "d": _json.dumps(detail or {}, ensure_ascii=False)})
    except Exception as e:
        logger.debug(f"permission audit skipped: {e}")


async def check(user_id: str, required: str, *, resource_type: str,
                resource_id: str, owner_id: Optional[str],
                parents: Optional[List[dict]] = None,
                team_roles: Optional[Dict[str, str]] = None,
                log_denied: bool = True) -> dict:
    """Прод-проверка: гранты из БД + чистый check_permission.
    parents: [{label, resource_type, resource_id, owner_id}] — гранты
    родителей дотягиваются сами."""
    grants = await fetch_grants(resource_type, resource_id)
    if team_roles is None:        # S3: членства из таблицы team_members
        try:
            from backend.core.auth.teams import team_roles_for_user
            team_roles = await team_roles_for_user(user_id)
        except Exception:
            team_roles = {}
    full_parents = []
    for p in parents or []:
        pg = await fetch_grants(p.get("resource_type", ""),
                                p.get("resource_id", ""))
        full_parents.append({"label": p.get("label"),
                             "owner_id": p.get("owner_id"),
                             "grants": pg})
    out = check_permission(user_id, required, owner_id=owner_id,
                           grants=grants, parents=full_parents,
                           team_roles=team_roles)
    if not out["allowed"] and log_denied:
        await audit(user_id, "check_denied", resource_type, resource_id,
                    {"required": required})
    return out


def permissions_enabled() -> bool:
    try:
        from backend.core.config.feature_flags import get_feature_flags
        return bool(get_feature_flags().enable_resource_permissions)
    except Exception:
        return False
