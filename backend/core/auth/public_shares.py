# -*- coding: utf-8 -*-
"""Публичные ссылки на одиночные ресурсы (ACCESS_CONTROL_DESIGN, S4).

В отличие от sharing.py bundles — простая ссылка на ОДИН ресурс по
образцу meeting_shares у MeetFlow: токен, TTL, пароль (bcrypt), лимит
просмотров. Дают доступ только на чтение/комментирование, это НЕ
грант в resource_permissions (отдельный механизм, не повышает права).

Чистое ядро (consume_share) тестируется без БД и крипты.
"""
from __future__ import annotations

import datetime
import hashlib
import logging
import secrets
from typing import Optional

logger = logging.getLogger(__name__)

ACCESS_LEVELS = ("read", "comment")
MIN_PASSWORD_LEN = 6


# ──────────────────────────────────────────────────────────────────────
# Пароль: bcrypt при наличии, иначе fallback на PBKDF2 (тоже корректный)
# ──────────────────────────────────────────────────────────────────────

def _hash_password(plain: str) -> str:
    plain = str(plain or "").strip()
    if not plain:
        return ""
    try:
        import bcrypt
        return bcrypt.hashpw(plain.encode("utf-8"),
                             bcrypt.gensalt(rounds=10)).decode("utf-8")
    except Exception:
        salt = secrets.token_hex(16)
        dk = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"),
                                 salt.encode(), 200_000)
        return f"pbkdf2_sha256${salt}${dk.hex()}"


def verify_password(plain: str, hashed: Optional[str]) -> bool:
    if not hashed:
        return True       # без пароля — пропускаем
    if not plain:
        return False
    if hashed.startswith("pbkdf2_sha256$"):
        parts = hashed.split("$", 2)
        if len(parts) != 3 or not parts[1] or not parts[2]:
            return False  # испорченный формат → fail-closed, не падаем unpack'ом
        _, salt, want = parts
        dk = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"),
                                 salt.encode(), 200_000)
        return secrets.compare_digest(dk.hex(), want)
    try:
        import bcrypt
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────
# Чистая логика валидации (тестируется без БД)
# ──────────────────────────────────────────────────────────────────────

def consume_share(record: Optional[dict], password: Optional[str] = None,
                  now: Optional[datetime.datetime] = None) -> dict:
    """Пройти проверки одной ссылки. Возвращает {ok, error?, share?}.
    Никакой инфы о существовании несуществующей ссылки не раскрываем:
    record=None → «ссылка не найдена или отозвана»."""
    if not record:
        return {"ok": False, "error": "ссылка не найдена или отозвана"}
    if not record.get("is_active", True):
        return {"ok": False, "error": "ссылка не найдена или отозвана"}
    now = now or datetime.datetime.now(datetime.timezone.utc)
    exp = record.get("expires_at")
    if exp:
        try:
            dt = datetime.datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            if dt <= now:
                return {"ok": False, "error": "срок действия ссылки истёк"}
        except (ValueError, AttributeError):
            return {"ok": False, "error": "ссылка не найдена или отозвана"}
    max_v = record.get("max_views")
    if max_v and int(record.get("current_views", 0)) >= int(max_v):
        return {"ok": False, "error": "лимит просмотров исчерпан"}
    if record.get("password_hash"):
        if not verify_password(str(password or ""), record["password_hash"]):
            return {"ok": False, "error": "нужен пароль"}
    return {"ok": True, "share": record}


# ──────────────────────────────────────────────────────────────────────
# Тонкий БД-слой (best-effort)
# ──────────────────────────────────────────────────────────────────────

async def create_share(actor_id: str, *, resource_type: str,
                       resource_id: str, access_level: str = "read",
                       expires_at: Optional[str] = None,
                       max_views: Optional[int] = None,
                       password: Optional[str] = None) -> dict:
    """Создать ссылку. Только владелец/admin (проверка — в роуте через
    check())."""
    from backend.core.auth.resource_permissions import (
        PERMISSION_TYPES,
        RESOURCE_TYPES,
        audit,
    )
    if resource_type not in RESOURCE_TYPES:
        return {"success": False, "error": "resource_type не поддержан"}
    if access_level not in ACCESS_LEVELS:
        return {"success": False, "error": f"access_level из {ACCESS_LEVELS}"}
    pwd = (password or "").strip() or None
    if pwd and len(pwd) < MIN_PASSWORD_LEN:
        return {"success": False,
                "error": f"пароль короче {MIN_PASSWORD_LEN} символов"}
    _ = PERMISSION_TYPES   # импорт нужен для констант, но проверки делает /check
    token = secrets.token_urlsafe(32)
    try:
        from sqlalchemy import text

        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session() as session:
            await session.execute(
                text("""INSERT INTO resource_shares
                    (resource_type, resource_id, share_token, access_level,
                     created_by, expires_at, max_views, password_hash)
                    VALUES (:rt, :rid, :tok, :al, :by,
                            CAST(:exp AS timestamptz), :mv, :ph)"""),
                {"rt": resource_type, "rid": str(resource_id), "tok": token,
                 "al": access_level, "by": str(actor_id), "exp": expires_at,
                 "mv": max_views, "ph": _hash_password(pwd) if pwd else None})
        await audit(actor_id, "public_share_created", resource_type,
                    resource_id, {"access_level": access_level,
                                  "has_password": bool(pwd),
                                  "max_views": max_views})
        return {"success": True, "share_token": token,
                "access_level": access_level}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def fetch_share(token: str) -> Optional[dict]:
    try:
        from sqlalchemy import text

        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session() as session:
            res = await session.execute(
                text("SELECT id, resource_type, resource_id, access_level, "
                     "expires_at, max_views, current_views, password_hash, "
                     "is_active FROM resource_shares "
                     "WHERE share_token = :t"), {"t": str(token)})
            row = res.fetchone()
            return dict(row._mapping) if row else None
    except Exception as e:
        logger.debug(f"fetch_share unavailable: {e}")
        return None


async def log_view(share_id: str, *, ip: Optional[str] = None,
                   user_agent: Optional[str] = None) -> None:
    try:
        from sqlalchemy import text

        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session() as session:
            await session.execute(
                text("UPDATE resource_shares SET current_views = "
                     "current_views + 1 WHERE id = :id"),
                {"id": str(share_id)})
            await session.execute(
                text("INSERT INTO resource_share_logs (share_id, ip, "
                     "user_agent) VALUES (:s, :ip, :ua)"),
                {"s": str(share_id), "ip": ip, "ua": user_agent})
    except Exception as e:
        logger.debug(f"log_view skipped: {e}")


async def revoke_share(actor_id: str, share_id: str) -> dict:
    try:
        from sqlalchemy import text

        from backend.core.auth.resource_permissions import audit
        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session() as session:
            res = await session.execute(
                text("UPDATE resource_shares SET is_active = FALSE "
                     "WHERE id = :id RETURNING resource_type, resource_id"),
                {"id": str(share_id)})
            row = res.fetchone()
        if row:
            await audit(actor_id, "public_share_revoked",
                        row._mapping["resource_type"],
                        row._mapping["resource_id"],
                        {"share_id": share_id})
        return {"success": True, "revoked": bool(row)}
    except Exception as e:
        return {"success": False, "error": str(e)}
