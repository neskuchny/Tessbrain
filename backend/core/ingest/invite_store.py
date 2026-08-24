"""Org invites store (Wave 3.1).

Onboarding-flow: founder/admin создаёт invite → шарит ссылку с raw-token
сотруднику → сотрудник принимает → становится member через membership.add_member.

Безопасность токена:
  - raw token (32 байта base64url) показывается caller'у ОДИН РАЗ при create
  - в storage лежит SHA256(raw) — компрометация файла не даёт принять invite
  - lookup всегда через hash, никогда через равенство строк

Структура файла `data/org_invites.json`:
  {invite_id (uuid): {
      org_id, token_hash, role, access_level,
      invited_email (optional), invited_by, created_at,
      expires_at, used_at, used_by, revoked_at
  }}

Concurrency: atomic write + fcntl-lock (тот же паттерн что в membership.py).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Default expiry: неделя достаточно для большинства onboarding-сценариев;
# admin может revoke в любой момент.
DEFAULT_EXPIRY = timedelta(days=7)
TOKEN_BYTES = 32  # 256 бит → 43 base64url-символа


_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, dict[str, Any]] | None = None
_CACHE_MTIME: float = 0.0


def _invites_path() -> str:
    """Lazy: tenant_paths подменяется в части тестов."""
    from backend.core.store.tenant_paths import _DATA_ROOT
    return str(Path(_DATA_ROOT) / "org_invites.json")


def _pg() -> bool:
    """Postgres-бэкенд стора доступов включён? (D1b; иначе — файл)."""
    try:
        from backend.core.ingest import access_repo
        return access_repo.enabled()
    except Exception:
        return False


def _load(path: str) -> dict[str, dict[str, Any]]:
    if _pg():
        try:
            from backend.core.ingest import access_repo
            return access_repo.load_all("invites")
        except Exception as e:  # noqa: BLE001
            logger.warning("invite_store PG load failed: %s", e)
            return {}
    try:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception as e:
        logger.warning("Failed to read org_invites.json: %s", e)
        return {}


def _get_all() -> dict[str, dict[str, Any]]:
    global _CACHE, _CACHE_MTIME
    path = _invites_path()
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
            access_repo.invalidate("invites")
        except Exception:
            logger.debug("invite_store PG cache invalidate skipped", exc_info=True)


def _atomic_write(path: str, data: dict[str, Any]) -> None:
    if _pg():
        from backend.core.ingest import access_repo
        access_repo.replace_all("invites", data)
        return
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    tmp_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=parent,
            prefix=".tmp_invites_", suffix=".json", delete=False,
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
        with access_repo.locked_txn("invites"):
            return fn()
    try:
        import fcntl
    except ImportError:
        return fn()
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fn()
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            logger.debug("flock release suppressed", exc_info=True)
        os.close(fd)


# === Token utilities ========================================================


def _hash_token(raw: str) -> str:
    """SHA256 raw-токена в hex. Используется как storage-ключ для lookup."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _generate_raw_token() -> str:
    """Cryptographically secure URL-safe token."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def _parse_iso(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_expired(entry: dict[str, Any]) -> bool:
    exp = _parse_iso(entry.get("expires_at", ""))
    return exp is not None and _now() >= exp


def _is_used(entry: dict[str, Any]) -> bool:
    return bool(entry.get("used_at"))


def _is_revoked(entry: dict[str, Any]) -> bool:
    return bool(entry.get("revoked_at"))


def _public_view(invite_id: str, entry: dict[str, Any], *, include_email: bool = True) -> dict[str, Any]:
    """View без token_hash. Безопасно отдавать в API responses."""
    out = {
        "invite_id": invite_id,
        "org_id": entry.get("org_id"),
        "role": entry.get("role"),
        "access_level": entry.get("access_level"),
        "invited_by": entry.get("invited_by"),
        "created_at": entry.get("created_at"),
        "expires_at": entry.get("expires_at"),
        "used_at": entry.get("used_at"),
        "used_by": entry.get("used_by"),
        "revoked_at": entry.get("revoked_at"),
        "status": _status(entry),
    }
    if include_email and entry.get("invited_email"):
        out["invited_email"] = entry["invited_email"]
    return out


def _status(entry: dict[str, Any]) -> str:
    if _is_revoked(entry):
        return "revoked"
    if _is_used(entry):
        return "used"
    if _is_expired(entry):
        return "expired"
    return "pending"


# === Public API =============================================================


def create_invite(
    org_id: str,
    *,
    invited_by: str,
    role: str = "employee",
    access_level: int = 2,
    invited_email: Optional[str] = None,
    expires_in: Optional[timedelta] = None,
) -> dict[str, Any]:
    """Создать invite. Возвращает:
        {
          "invite_id": str,
          "token": raw_token,   # ⚠️ показывается ОДИН РАЗ
          ... остальные публичные поля
        }
    """
    if not (org_id and invited_by):
        raise ValueError("org_id and invited_by are required")

    raw_token = _generate_raw_token()
    invite_id = str(uuid.uuid4())
    exp = _now() + (expires_in or DEFAULT_EXPIRY)
    entry = {
        "org_id": org_id,
        "token_hash": _hash_token(raw_token),
        "role": role,
        "access_level": int(access_level),
        "invited_email": (invited_email or "").strip().lower() or None,
        "invited_by": invited_by,
        "created_at": _now().isoformat(),
        "expires_at": exp.isoformat(),
        "used_at": None,
        "used_by": None,
        "revoked_at": None,
    }

    path = _invites_path()

    def _txn():
        current = _load(path)
        current[invite_id] = entry
        _atomic_write(path, current)

    with _CACHE_LOCK:
        _with_file_lock(path, _txn)
    clear_cache()

    return {"token": raw_token, **_public_view(invite_id, entry)}


def list_invites_for_org(org_id: str, *, include_used: bool = False) -> list[dict[str, Any]]:
    """Список invites org. По умолчанию — только pending."""
    if not org_id:
        return []
    items = _get_all()
    if _pg():
        # Fast-path (D1a.2): выборка по индексу org_id вместо скана всех.
        try:
            from backend.core.ingest import access_repo
            items = access_repo.docs_by_index("invites", "org_id", org_id)
        except Exception:
            logger.debug("invite org fast-path failed, fallback to scan",
                         exc_info=True)
    out: list[dict[str, Any]] = []
    for iid, entry in items.items():
        if not isinstance(entry, dict) or entry.get("org_id") != org_id:
            continue
        if not include_used and _status(entry) != "pending":
            continue
        out.append(_public_view(iid, entry))
    # Стабильный порядок (по created_at) — UI приятнее.
    out.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return out


def get_invite_by_token(raw_token: str) -> Optional[dict[str, Any]]:
    """Lookup по raw-токену через hash. Возвращает public view + status.

    Используется landing-page'ом (`GET /invites/{token}`) — показать
    инвайти инфу об org до того как он залогинится / accept'нет.
    """
    if not raw_token or len(raw_token) < 16:
        return None
    target_hash = _hash_token(raw_token)
    if _pg():
        # Fast-path (D1a.2): O(1) по индексу token_hash вместо скана всех.
        try:
            from backend.core.ingest import access_repo
            rows = access_repo.docs_by_index("invites", "token_hash", target_hash)
            for iid, entry in rows.items():
                return _public_view(iid, entry, include_email=True)
            return None
        except Exception:
            logger.debug("invite token fast-path failed, fallback to scan",
                         exc_info=True)
    for iid, entry in _get_all().items():
        if not isinstance(entry, dict):
            continue
        # constant-time compare защита от timing attack (избыточно для
        # SHA256-сравнения, но дешёво — стандартная практика).
        stored = entry.get("token_hash", "")
        if hmac.compare_digest(stored, target_hash):
            return _public_view(iid, entry, include_email=True)
    return None


def consume_invite(
    raw_token: str,
    *,
    consuming_user_id: str,
    consuming_user_email: Optional[str] = None,
) -> dict[str, Any]:
    """Атомарно accept invite: пометить used + вернуть данные для add_member.

    Returns:
        {
          "ok": True,
          "org_id": str, "role": str, "access_level": int,
          "invite_id": str,
        }

    Raises ValueError с одним из reason'ов:
        "token_not_found" | "expired" | "used" | "revoked"
        | "email_mismatch" (invite был с email, accepter — с другим)
    """
    if not (raw_token and consuming_user_id):
        raise ValueError("token and consuming_user_id required")
    target_hash = _hash_token(raw_token)
    path = _invites_path()

    def _txn() -> dict[str, Any]:
        current = _load(path)
        found_id: Optional[str] = None
        entry: Optional[dict[str, Any]] = None
        for iid, e in current.items():
            if isinstance(e, dict) and hmac.compare_digest(
                e.get("token_hash", ""), target_hash
            ):
                found_id, entry = iid, e
                break
        if not entry or not found_id:
            raise ValueError("token_not_found")
        if _is_revoked(entry):
            raise ValueError("revoked")
        if _is_used(entry):
            raise ValueError("used")
        if _is_expired(entry):
            raise ValueError("expired")
        # Email-pinning: если invite создан с конкретным email — accepter
        # должен совпасть. Защита от пересылки токена другому человеку.
        expected_email = entry.get("invited_email")
        if expected_email:
            got = (consuming_user_email or "").strip().lower()
            if got != expected_email:
                raise ValueError("email_mismatch")

        # Атомарно помечаем used.
        entry["used_at"] = _now().isoformat()
        entry["used_by"] = consuming_user_id
        current[found_id] = entry
        _atomic_write(path, current)
        return {
            "ok": True,
            "invite_id": found_id,
            "org_id": entry["org_id"],
            "role": entry["role"],
            "access_level": entry["access_level"],
        }

    with _CACHE_LOCK:
        result = _with_file_lock(path, _txn)
    clear_cache()
    return result


def revoke_invite(invite_id: str, org_id: str, *, revoked_by: str) -> bool:
    """Revoke pending invite. Возвращает False если invite не существует
    в этой org или уже не pending."""
    if not (invite_id and org_id):
        return False
    path = _invites_path()

    def _txn() -> bool:
        current = _load(path)
        entry = current.get(invite_id)
        if not isinstance(entry, dict) or entry.get("org_id") != org_id:
            return False
        if _status(entry) != "pending":
            return False
        entry["revoked_at"] = _now().isoformat()
        entry["revoked_by"] = revoked_by
        current[invite_id] = entry
        _atomic_write(path, current)
        return True

    with _CACHE_LOCK:
        revoked = _with_file_lock(path, _txn)
    clear_cache()
    return revoked
