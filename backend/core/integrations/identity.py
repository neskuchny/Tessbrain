# -*- coding: utf-8 -*-
"""
Identity-маппинг внешних систем (Daily Pulse, MeetFlow и т.п.) —
INTEGRATION_TESSENT.md, Фаза 0. Источник правды один: tessent_user_id
(Supabase Auth UUID); внешние ID привязываются к нему через таблицу
external_identity_mapping. Маппинг по email при первом обращении,
явная верификация — owner организации в админке.

Чистые функции (валидация формы внешнего ID, разрешение из payload)
тестируются без БД. БД-слой best-effort.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Известные внешние системы — fail-closed на чужих
KNOWN_SOURCES = ("daily_pulse", "meetflow", "callinsight")

_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def normalize_external_id(source: str, external_id: str) -> Optional[str]:
    """Нормализовать внешний ID. None → ID невалиден (fail-closed)."""
    if source not in KNOWN_SOURCES:
        return None
    eid = str(external_id or "").strip()
    if not eid:
        return None
    if source == "daily_pulse":
        # telegram_id — целое число (string-форма ОК); email — отдельно
        if eid.startswith("@"):
            eid = eid[1:]
        if eid.isdigit() or _EMAIL.match(eid):
            return eid.lower() if _EMAIL.match(eid) else eid
        return None
    # meetflow/callinsight: внутренний uuid или email (email — в нижний регистр)
    return eid.lower() if _EMAIL.match(eid) else eid


def is_email(value: str) -> bool:
    return bool(_EMAIL.match(str(value or "").strip()))


# ──────────────────────────────────────────────────────────────────────
# БД-слой (best-effort; нет Postgres → None / пустые ответы)
# ──────────────────────────────────────────────────────────────────────

async def get_mapping(source: str, external_id: str) -> Optional[dict]:
    if normalize_external_id(source, external_id) is None:
        return None
    try:
        from sqlalchemy import text

        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session() as session:
            res = await session.execute(
                text("SELECT tessent_user_id, email, verified "
                     "FROM external_identity_mapping "
                     "WHERE external_system = :s AND external_id = :e"),
                {"s": source, "e": str(external_id)})
            row = res.fetchone()
            return dict(row._mapping) if row else None
    except Exception as e:
        logger.debug(f"get_mapping unavailable: {e}")
        return None


async def resolve_or_link_by_email(source: str, external_id: str,
                                   email: Optional[str]) -> Optional[str]:
    """Найти tessent_user_id по external_id; если нет — попытаться
    привязать через email (verified=false до явного подтверждения owner).
    Возвращает tessent_user_id или None (нет совпадений)."""
    if normalize_external_id(source, external_id) is None:
        return None
    existing = await get_mapping(source, external_id)
    if existing:
        return str(existing["tessent_user_id"])
    if not email or not is_email(email):
        return None
    try:
        from sqlalchemy import text

        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session() as session:
            # ищем tessent-пользователя по email (Supabase auth.users)
            res = await session.execute(
                text("SELECT id FROM auth.users WHERE email = :e LIMIT 1"),
                {"e": email.lower()})
            row = res.fetchone()
            if not row:
                return None
            uid = str(row._mapping["id"])
            await session.execute(
                text("INSERT INTO external_identity_mapping "
                     "(external_system, external_id, tessent_user_id, "
                     "email, verified) VALUES (:s, :e, :u, :em, FALSE) "
                     "ON CONFLICT (external_system, external_id) "
                     "DO NOTHING"),
                {"s": source, "e": str(external_id), "u": uid,
                 "em": email.lower()})
            from backend.core.auth.resource_permissions import audit
            await audit(uid, "identity_auto_linked", "identity", external_id,
                        {"source": source, "via": "email"})
            return uid
    except Exception as e:
        logger.debug(f"resolve_or_link unavailable: {e}")
        return None


async def link(actor_id: str, source: str, external_id: str,
               tessent_user_id: str, email: Optional[str] = None,
               verified: bool = False) -> dict:
    """Ручная привязка (admin/owner). Идемпотентно по UNIQUE."""
    if normalize_external_id(source, external_id) is None:
        return {"success": False, "error": "невалидный external_id"}
    try:
        from sqlalchemy import text

        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session() as session:
            await session.execute(
                text("INSERT INTO external_identity_mapping "
                     "(external_system, external_id, tessent_user_id, "
                     "email, verified) VALUES (:s, :e, :u, :em, :v) "
                     "ON CONFLICT (external_system, external_id) "
                     "DO UPDATE SET tessent_user_id = :u, email = :em, "
                     "verified = :v"),
                {"s": source, "e": str(external_id),
                 "u": str(tessent_user_id),
                 "em": email.lower() if email else None,
                 "v": bool(verified)})
        from backend.core.auth.resource_permissions import audit
        await audit(actor_id, "identity_linked", "identity", external_id,
                    {"source": source, "user": tessent_user_id,
                     "verified": verified})
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def list_mappings(source: Optional[str] = None) -> list:
    """Все маппинги (для админ-UI). Best-effort."""
    try:
        from sqlalchemy import text

        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        sql = ("SELECT external_system, external_id, tessent_user_id, "
               "email, verified, created_at FROM external_identity_mapping")
        params = {}
        if source:
            sql += " WHERE external_system = :s"
            params["s"] = source
        sql += " ORDER BY created_at DESC LIMIT 500"
        async with pg.session() as session:
            res = await session.execute(text(sql), params)
            out = []
            for r in res.fetchall():
                d = dict(r._mapping)
                if d.get("created_at") is not None:
                    d["created_at"] = str(d["created_at"])
                out.append(d)
            return out
    except Exception as e:
        logger.debug(f"list_mappings unavailable: {e}")
        return []


async def set_verified(actor_id: str, source: str, external_id: str,
                       verified: bool) -> dict:
    """Подтвердить/снять верификацию маппинга (owner/admin)."""
    try:
        from sqlalchemy import text

        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session() as session:
            res = await session.execute(
                text("UPDATE external_identity_mapping SET verified = :v "
                     "WHERE external_system = :s AND external_id = :e"),
                {"v": bool(verified), "s": source, "e": str(external_id)})
        from backend.core.auth.resource_permissions import audit
        await audit(actor_id, "identity_verified", "identity", external_id,
                    {"source": source, "verified": verified})
        return {"success": True, "updated": res.rowcount > 0}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def recent_events(source: str = "daily_pulse", limit: int = 50) -> list:
    """Последние integration_events (диагностика «дошли ли события»)."""
    try:
        from sqlalchemy import text

        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session() as session:
            res = await session.execute(
                text("SELECT event, team_ref, user_ref, occurred_at, "
                     "received_at, signature_ok FROM integration_events "
                     "WHERE source = :s ORDER BY received_at DESC LIMIT :n"),
                {"s": source, "n": max(1, min(int(limit), 200))})
            out = []
            for r in res.fetchall():
                d = dict(r._mapping)
                for k in ("occurred_at", "received_at"):
                    if d.get(k) is not None:
                        d[k] = str(d[k])
                out.append(d)
            return out
    except Exception as e:
        logger.debug(f"recent_events unavailable: {e}")
        return []
