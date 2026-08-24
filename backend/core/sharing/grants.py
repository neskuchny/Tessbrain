"""W31: ShareGrantService — CRUD над share_bundles + share_grants.

In-memory fallback при отсутствии postgres (для тестов / dev). Все
методы best-effort: write'ы пишут (возможно in-memory), read'ы читают
без raises.

Архитектура:
- ShareBundle = "Alice → bob@partner.com на 30 дней" (1 запись).
- ShareGrant = "этот bundle включает этот document" (N записей, по 1
  на ресурс).
- Token хранится в bundle, ровно один на bundle (мы не делаем
  per-resource токены — это путает UX, "одна ссылка на пакет").
"""
from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


VALID_RESOURCE_TYPES = frozenset({"document", "meeting", "collection", "report"})
VALID_PERMISSIONS = frozenset({"read", "read+download"})

DEFAULT_TTL_DAYS = 30
MAX_TTL_DAYS = 365
MAX_RESOURCES_PER_BUNDLE = 100


def _new_bundle_id() -> str:
    return f"shr_{uuid.uuid4().hex[:16]}"


def _new_grant_id() -> str:
    return f"sg_{uuid.uuid4().hex[:16]}"


def _new_token() -> str:
    return secrets.token_urlsafe(24)   # ~32 chars


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


@dataclass
class ShareGrantInput:
    """Один пункт списка ресурсов при создании bundle."""
    resource_type: str
    resource_id: str
    permissions: str = "read"

    def validate(self) -> None:
        if self.resource_type not in VALID_RESOURCE_TYPES:
            raise ValueError(f"invalid resource_type: {self.resource_type}")
        if self.permissions not in VALID_PERMISSIONS:
            raise ValueError(f"invalid permissions: {self.permissions}")
        if not self.resource_id or len(self.resource_id) > 200:
            raise ValueError("resource_id required and ≤200 chars")


@dataclass
class ShareGrant:
    id: str
    bundle_id: str
    resource_type: str
    resource_id: str
    permissions: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "bundle_id": self.bundle_id,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "permissions": self.permissions,
        }


@dataclass
class ShareBundle:
    id: str
    owner_user_id: str
    grantee_email: str
    grantee_org: Optional[str]
    note: str
    token: str
    expires_at: str
    revoked_at: Optional[str]
    created_at: str
    grants: list[ShareGrant] = field(default_factory=list)

    def is_active(self, *, now: Optional[datetime] = None) -> bool:
        if self.revoked_at is not None:
            return False
        try:
            exp = datetime.fromisoformat(self.expires_at)
        except Exception:
            return False
        n = now or _utc_now()
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp > n

    def to_dict(self, *, include_token: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "owner_user_id": self.owner_user_id,
            "grantee_email": self.grantee_email,
            "grantee_org": self.grantee_org,
            "note": self.note,
            "expires_at": self.expires_at,
            "revoked_at": self.revoked_at,
            "created_at": self.created_at,
            "is_active": self.is_active(),
            "resources": [g.to_dict() for g in self.grants],
        }
        if include_token:
            out["token"] = self.token
        return out


# === SQL ==================================================================

_INSERT_BUNDLE_SQL = """
INSERT INTO public.share_bundles
    (id, owner_user_id, grantee_email, grantee_org, note, token, expires_at)
VALUES (:id, :owner_user_id, :grantee_email, :grantee_org, :note, :token, :expires_at)
"""

_INSERT_GRANT_SQL = """
INSERT INTO public.share_grants
    (id, bundle_id, resource_type, resource_id, permissions)
VALUES (:id, :bundle_id, :resource_type, :resource_id, :permissions)
ON CONFLICT (bundle_id, resource_type, resource_id) DO NOTHING
"""

_GET_BUNDLE_BY_TOKEN_SQL = """
SELECT id, owner_user_id, grantee_email, grantee_org, note, token,
       expires_at, revoked_at, created_at
FROM public.share_bundles
WHERE token = :token
"""

_GET_BUNDLE_BY_ID_SQL = """
SELECT id, owner_user_id, grantee_email, grantee_org, note, token,
       expires_at, revoked_at, created_at
FROM public.share_bundles
WHERE id = :id
"""

_LIST_BUNDLES_FOR_USER_SQL = """
SELECT id, owner_user_id, grantee_email, grantee_org, note, token,
       expires_at, revoked_at, created_at
FROM public.share_bundles
WHERE owner_user_id = :user_id
ORDER BY created_at DESC
LIMIT :lim OFFSET :off
"""

_LIST_GRANTS_FOR_BUNDLE_SQL = """
SELECT id, bundle_id, resource_type, resource_id, permissions
FROM public.share_grants
WHERE bundle_id = :bundle_id
"""

_REVOKE_BUNDLE_SQL = """
UPDATE public.share_bundles
SET revoked_at = now()
WHERE id = :id AND revoked_at IS NULL
"""


class ShareGrantService:
    def __init__(self, *, postgres: Any = None) -> None:
        self.postgres = postgres
        self._mem_bundles: dict[str, ShareBundle] = {}
        self._mem_token_index: dict[str, str] = {}    # token -> bundle_id

    # === Create =======================================================

    async def create_bundle(
        self,
        *,
        owner_user_id: str,
        grantee_email: str,
        resources: list[ShareGrantInput],
        note: str = "",
        grantee_org: Optional[str] = None,
        ttl_days: int = DEFAULT_TTL_DAYS,
    ) -> ShareBundle:
        if not owner_user_id:
            raise ValueError("owner_user_id required")
        email = _normalize_email(grantee_email)
        if "@" not in email:
            raise ValueError("invalid grantee_email")
        if not resources:
            raise ValueError("at least one resource required")
        if len(resources) > MAX_RESOURCES_PER_BUNDLE:
            raise ValueError(f"too many resources (max {MAX_RESOURCES_PER_BUNDLE})")
        for r in resources:
            r.validate()

        ttl = max(1, min(int(ttl_days), MAX_TTL_DAYS))
        expires = _utc_now() + timedelta(days=ttl)
        bundle_id = _new_bundle_id()
        token = _new_token()

        grants = [
            ShareGrant(
                id=_new_grant_id(),
                bundle_id=bundle_id,
                resource_type=r.resource_type,
                resource_id=r.resource_id,
                permissions=r.permissions,
            )
            for r in resources
        ]

        bundle = ShareBundle(
            id=bundle_id,
            owner_user_id=owner_user_id,
            grantee_email=email,
            grantee_org=(grantee_org or None),
            note=note or "",
            token=token,
            expires_at=expires.isoformat(),
            revoked_at=None,
            created_at=_utc_now().isoformat(),
            grants=grants,
        )

        if self.postgres is None:
            self._mem_bundles[bundle_id] = bundle
            self._mem_token_index[token] = bundle_id
            return bundle

        try:
            async with self.postgres.session() as session:
                await session.execute(_INSERT_BUNDLE_SQL, {
                    "id": bundle_id,
                    "owner_user_id": owner_user_id,
                    "grantee_email": email,
                    "grantee_org": grantee_org,
                    "note": note or "",
                    "token": token,
                    "expires_at": expires,
                })
                for g in grants:
                    await session.execute(_INSERT_GRANT_SQL, {
                        "id": g.id,
                        "bundle_id": bundle_id,
                        "resource_type": g.resource_type,
                        "resource_id": g.resource_id,
                        "permissions": g.permissions,
                    })
        except Exception as exc:
            logger.warning("create_bundle DB write failed: %s", exc)
            # Fallback to in-memory чтобы dev / тест продолжили работать.
            self._mem_bundles[bundle_id] = bundle
            self._mem_token_index[token] = bundle_id
        return bundle

    # === Read ==========================================================

    async def get_by_token(self, *, token: str) -> Optional[ShareBundle]:
        if not token:
            return None
        if self.postgres is None:
            bid = self._mem_token_index.get(token)
            return self._mem_bundles.get(bid) if bid else None
        try:
            async with self.postgres.session() as session:
                result = await session.execute(_GET_BUNDLE_BY_TOKEN_SQL, {"token": token})
                row = await _first_row(result)
                if row is None:
                    return None
                bundle = _bundle_from_row(row)
                gres = await session.execute(_LIST_GRANTS_FOR_BUNDLE_SQL, {
                    "bundle_id": bundle.id,
                })
                for grow in await _all_rows(gres):
                    bundle.grants.append(_grant_from_row(grow))
                return bundle
        except Exception as exc:
            logger.warning("get_by_token failed: %s", exc)
            return None

    async def list_for_user(
        self,
        *,
        user_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ShareBundle]:
        if not user_id:
            return []
        if self.postgres is None:
            out = [
                b for b in self._mem_bundles.values()
                if b.owner_user_id == user_id
            ]
            out.sort(key=lambda b: b.created_at, reverse=True)
            return out[offset:offset + limit]
        try:
            async with self.postgres.session() as session:
                result = await session.execute(_LIST_BUNDLES_FOR_USER_SQL, {
                    "user_id": user_id,
                    "lim": max(1, min(int(limit), 200)),
                    "off": max(0, int(offset)),
                })
                rows = await _all_rows(result)
                bundles = [_bundle_from_row(r) for r in rows]
                # Hydrate grants — последовательно (для простоты).
                for b in bundles:
                    gres = await session.execute(_LIST_GRANTS_FOR_BUNDLE_SQL, {
                        "bundle_id": b.id,
                    })
                    for grow in await _all_rows(gres):
                        b.grants.append(_grant_from_row(grow))
                return bundles
        except Exception as exc:
            logger.warning("list_for_user failed: %s", exc)
            return []

    # === Revoke =======================================================

    async def revoke(self, *, bundle_id: str) -> bool:
        if not bundle_id:
            return False
        if self.postgres is None:
            b = self._mem_bundles.get(bundle_id)
            if b is None or b.revoked_at is not None:
                return False
            b.revoked_at = _utc_now().isoformat()
            return True
        try:
            async with self.postgres.session() as session:
                await session.execute(_REVOKE_BUNDLE_SQL, {"id": bundle_id})
            return True
        except Exception as exc:
            logger.warning("revoke failed: %s", exc)
            return False


# === Row helpers ============================================================

def _iso(v: Any) -> Optional[str]:
    if v is None:
        return None
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


def _bundle_from_row(row: dict[str, Any]) -> ShareBundle:
    return ShareBundle(
        id=row["id"],
        owner_user_id=row["owner_user_id"],
        grantee_email=row["grantee_email"],
        grantee_org=row.get("grantee_org"),
        note=row.get("note") or "",
        token=row["token"],
        expires_at=_iso(row.get("expires_at")) or _utc_now().isoformat(),
        revoked_at=_iso(row.get("revoked_at")),
        created_at=_iso(row.get("created_at")) or _utc_now().isoformat(),
        grants=[],
    )


def _grant_from_row(row: dict[str, Any]) -> ShareGrant:
    return ShareGrant(
        id=row["id"],
        bundle_id=row["bundle_id"],
        resource_type=row["resource_type"],
        resource_id=row["resource_id"],
        permissions=row.get("permissions") or "read",
    )


async def _first_row(result: Any) -> Optional[dict[str, Any]]:
    try:
        m = result.mappings()
        row = m.first() if hasattr(m, "first") else None
        if row is not None:
            return dict(row)
    except Exception:
        pass
    if hasattr(result, "first"):
        row = result.first()
        if row is None:
            return None
        if hasattr(row, "_mapping"):
            return dict(row._mapping)
        if isinstance(row, dict):
            return row
    return None


async def _all_rows(result: Any) -> list[dict[str, Any]]:
    try:
        m = result.mappings()
        rows = m.all() if hasattr(m, "all") else []
        return [dict(r) for r in rows]
    except Exception:
        pass
    if hasattr(result, "all"):
        out = []
        for r in result.all():
            if hasattr(r, "_mapping"):
                out.append(dict(r._mapping))
            elif isinstance(r, dict):
                out.append(r)
        return out
    return []
