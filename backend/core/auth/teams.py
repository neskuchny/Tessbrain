# -*- coding: utf-8 -*-
"""Команды для модели прав (docs/ru/ACCESS_CONTROL_DESIGN.md, S3).

Роль участника в команде задаёт ПОТОЛОК его фактических прав на
ресурсах, расшаренных команде (TEAM_ROLE_PERMISSION_CAP в
resource_permissions). Управлять составом может owner/admin команды.

Тонкий БД-слой best-effort: нет Postgres → пустые членства → team-гранты
просто не действуют (fail-closed, ничего не ломается).
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

TEAM_ROLES = ("viewer", "member", "admin", "owner")


async def team_roles_for_user(user_id: str) -> Dict[str, str]:
    """{team_id: роль} пользователя — вход для check_permission."""
    try:
        from sqlalchemy import text

        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session() as session:
            res = await session.execute(
                text("SELECT team_id, role FROM team_members "
                     "WHERE user_id = :u"), {"u": str(user_id)})
            return {str(r._mapping["team_id"]): str(r._mapping["role"])
                    for r in res.fetchall()}
    except Exception as e:
        logger.debug(f"team roles unavailable: {e}")
        return {}


async def create_team(user_id: str, name: str,
                      org_id: Optional[str] = None) -> dict:
    if not str(name or "").strip():
        return {"success": False, "error": "name обязателен"}
    try:
        import uuid as _uuid

        from sqlalchemy import text

        from backend.db.postgres import get_postgres
        team_id = str(_uuid.uuid4())
        pg = await get_postgres()
        async with pg.session() as session:
            await session.execute(
                text("INSERT INTO teams (id, org_id, name, created_by) "
                     "VALUES (:id, :org, :n, :by)"),
                {"id": team_id, "org": org_id, "n": name.strip(),
                 "by": str(user_id)})
            # создатель — owner команды
            await session.execute(
                text("INSERT INTO team_members (team_id, user_id, role, "
                     "added_by) VALUES (:t, :u, 'owner', :u)"),
                {"t": team_id, "u": str(user_id)})
        return {"success": True, "team_id": team_id, "name": name.strip()}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def my_teams(user_id: str) -> List[dict]:
    try:
        from sqlalchemy import text

        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session() as session:
            res = await session.execute(
                text("SELECT t.id, t.name, t.org_id, m.role "
                     "FROM teams t JOIN team_members m ON m.team_id = t.id "
                     "WHERE m.user_id = :u ORDER BY t.created_at"),
                {"u": str(user_id)})
            return [{"team_id": str(r._mapping["id"]),
                     "name": r._mapping["name"],
                     "org_id": r._mapping["org_id"],
                     "my_role": r._mapping["role"]}
                    for r in res.fetchall()]
    except Exception as e:
        logger.debug(f"my_teams unavailable: {e}")
        return []


async def _my_role_in(user_id: str, team_id: str) -> Optional[str]:
    roles = await team_roles_for_user(user_id)
    return roles.get(str(team_id))


async def add_member(actor_id: str, team_id: str, user_id: str,
                     role: str = "member") -> dict:
    """Добавить/сменить роль участника. Только owner/admin команды;
    выдать роль выше своей нельзя (fail-closed)."""
    if role not in TEAM_ROLES:
        return {"success": False, "error": f"role из {TEAM_ROLES}"}
    my_role = await _my_role_in(actor_id, team_id)
    if my_role not in ("owner", "admin"):
        return {"success": False,
                "error": "управлять составом может owner/admin команды"}
    order = {r: i for i, r in enumerate(TEAM_ROLES)}
    if order[role] > order[my_role]:
        return {"success": False,
                "error": "нельзя выдать роль выше собственной"}
    try:
        from sqlalchemy import text

        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session() as session:
            await session.execute(
                text("INSERT INTO team_members (team_id, user_id, role, "
                     "added_by) VALUES (:t, :u, :r, :by) "
                     "ON CONFLICT (team_id, user_id) "
                     "DO UPDATE SET role = :r, added_by = :by"),
                {"t": str(team_id), "u": str(user_id), "r": role,
                 "by": str(actor_id)})
        from backend.core.auth.resource_permissions import audit
        await audit(actor_id, "team_member_set", "team", team_id,
                    {"user": user_id, "role": role})
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def remove_member(actor_id: str, team_id: str, user_id: str) -> dict:
    my_role = await _my_role_in(actor_id, team_id)
    if my_role not in ("owner", "admin") and str(actor_id) != str(user_id):
        return {"success": False,
                "error": "удалять может owner/admin (или сам себя)"}
    try:
        from sqlalchemy import text

        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session() as session:
            res = await session.execute(
                text("DELETE FROM team_members WHERE team_id = :t "
                     "AND user_id = :u"),
                {"t": str(team_id), "u": str(user_id)})
        from backend.core.auth.resource_permissions import audit
        await audit(actor_id, "team_member_removed", "team", team_id,
                    {"user": user_id})
        return {"success": True, "removed": res.rowcount > 0}
    except Exception as e:
        return {"success": False, "error": str(e)}
