# -*- coding: utf-8 -*-
"""W31: per-document external sharing — owner & partner endpoints.

Owner-side (JWT, current user):
  POST   /shares                      — создать bundle (resources + email + ttl)
  GET    /shares                      — list owner'овских bundles
  GET    /shares/{id}                 — детали bundle (audit log compact)
  DELETE /shares/{id}                 — revoke

Partner-side (public, no JWT):
  GET    /shared/{token}              — landing meta (без secret data) +
                                        просьба ввести email
  POST   /shared/{token}/verify       — body: {email} → выдаём ViewerJWT
                                        (если email == grantee_email)
  GET    /shared/{token}/manifest     — Authorization: Bearer <viewer_jwt>
                                        список ресурсов в scope
  GET    /shared/{token}/resource/{type}/{id}  — Bearer viewer_jwt + scope check
                                                 → возвращаем view-only proxy
                                                 (заглушка — реальный fetch в
                                                  отдельном слое documents/meetings)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from litestar import Request, Router, delete, get, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter
from litestar.response import Stream

from backend.core.observability import audit_log
from backend.core.sharing.audit import record_view
from backend.core.sharing.grants import (
    DEFAULT_TTL_DAYS,
    MAX_RESOURCES_PER_BUNDLE,
    ShareBundle,
    ShareGrantInput,
    ShareGrantService,
)
from backend.core.sharing.scope import (
    ScopeViolation,
    check_resource_in_scope,
)
from backend.core.sharing.viewer_token import (
    decode_viewer_token,
    issue_viewer_token,
    viewer_token_remaining_seconds,
)

logger = logging.getLogger(__name__)


# === Auth helpers ==========================================================

def _decode_jwt_unsafe(token: str) -> dict[str, Any]:
    # issue #107 T-1: verify-first (strict → {} на плохой подписи → 401;
    # compat по умолчанию → decode без проверки, как раньше).
    from backend.core.auth.service_token import decode_claims_guarded
    return decode_claims_guarded(token)


def _extract_user(authorization: Optional[str]) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.split(" ", 1)[1]
    try:
        claims = _decode_jwt_unsafe(token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")
    user_id = claims.get("sub") or claims.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing sub/user_id")
    return user_id


def _extract_viewer_token(authorization: Optional[str]) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Viewer token required")
    return authorization.split(" ", 1)[1]


async def _build_service() -> ShareGrantService:
    postgres = None
    try:
        from backend.db.postgres import get_postgres
        postgres = await get_postgres()
    except Exception:
        postgres = None
    return ShareGrantService(postgres=postgres)


def _client_meta(request: Request) -> tuple[Optional[str], Optional[str]]:
    ip = None
    try:
        ip = (
            request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else None)
        )
    except Exception:
        pass
    ua = request.headers.get("user-agent")
    return ip, ua


# === Owner-side ============================================================


@post("/shares")
async def create_share(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Создать bundle.

    Body:
        {
          "grantee_email": "bob@partner.com",
          "grantee_org": "Acme Investments",   // optional
          "note": "Q4 board pack",             // optional
          "ttl_days": 30,                      // optional
          "resources": [
            {"resource_type": "document", "resource_id": "doc_abc",
             "permissions": "read"},
            ...
          ]
        }
    """
    user_id = _extract_user(authorization)

    grantee = (data.get("grantee_email") or "").strip()
    raw_resources = data.get("resources") or []
    if not isinstance(raw_resources, list):
        raise HTTPException(status_code=400, detail="resources must be list")
    if len(raw_resources) > MAX_RESOURCES_PER_BUNDLE:
        raise HTTPException(
            status_code=400,
            detail=f"too many resources (max {MAX_RESOURCES_PER_BUNDLE})",
        )

    inputs: list[ShareGrantInput] = []
    for r in raw_resources:
        if not isinstance(r, dict):
            raise HTTPException(status_code=400, detail="resource must be dict")
        try:
            inp = ShareGrantInput(
                resource_type=str(r.get("resource_type") or ""),
                resource_id=str(r.get("resource_id") or ""),
                permissions=str(r.get("permissions") or "read"),
            )
            inp.validate()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        inputs.append(inp)

    ttl_days = int(data.get("ttl_days") or DEFAULT_TTL_DAYS)
    svc = await _build_service()

    try:
        bundle = await svc.create_bundle(
            owner_user_id=user_id,
            grantee_email=grantee,
            resources=inputs,
            note=str(data.get("note") or ""),
            grantee_org=(data.get("grantee_org") or None),
            ttl_days=ttl_days,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    public_url = _public_share_url(bundle.token)
    from backend.core.observability import metrics as _m
    _m.record_share_event("created")
    await audit_log.emit(
        action="share.granted",
        user_id=user_id,
        resource=f"share_bundle:{bundle.id}",
        metadata={
            "grantee_email": bundle.grantee_email,
            "resources": len(bundle.grants),
            "ttl_days": ttl_days,
        },
    )
    out = bundle.to_dict(include_token=True)
    out["public_url"] = public_url

    # W33: best-effort email delivery партнёру.
    # По умолчанию notify_partner=true, можно выключить если frontend сам шлёт.
    notify_partner = bool(data.get("notify_partner", True))
    email_result: Optional[dict[str, Any]] = None
    if notify_partner:
        from backend.core.notifications.share_invite import send_share_invite
        owner_label = (data.get("owner_label") or user_id[:8] or "Коллега").strip()
        org_or_email = (
            data.get("owner_org_or_email")
            or data.get("owner_label")
            or user_id
        ).strip()
        result = await send_share_invite(
            grantee_email=bundle.grantee_email,
            owner_label=owner_label,
            owner_org_or_email=org_or_email,
            note=bundle.note,
            resource_count=len(bundle.grants),
            expires_at=bundle.expires_at,
            public_url=public_url,
        )
        email_result = {
            "ok": result.ok,
            "transport": result.transport,
            "error": result.error,
        }
        await audit_log.emit(
            action=("share.email_sent" if result.ok else "share.email_failed"),
            user_id=user_id,
            resource=f"share_bundle:{bundle.id}",
            metadata={
                "grantee_email": bundle.grantee_email,
                "transport": result.transport,
                "error": result.error,
            },
        )
        _m.record_share_event(
            "email_sent" if result.ok else "email_failed",
        )
    out["email_sent"] = bool(email_result and email_result.get("ok"))
    out["email_delivery"] = email_result
    return out


@get("/shares")
async def list_shares(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    user_id = _extract_user(authorization)
    svc = await _build_service()
    bundles = await svc.list_for_user(user_id=user_id, limit=limit, offset=offset)
    return {
        "items": [b.to_dict(include_token=False) for b in bundles],
        "limit": limit,
        "offset": offset,
    }


@get("/shares/{bundle_id:str}")
async def get_share(
    bundle_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    user_id = _extract_user(authorization)
    svc = await _build_service()
    user_bundles = await svc.list_for_user(user_id=user_id, limit=200)
    found = next((b for b in user_bundles if b.id == bundle_id), None)
    if found is None:
        raise HTTPException(status_code=404, detail="share not found")
    out = found.to_dict(include_token=True)
    out["public_url"] = _public_share_url(found.token)
    return out


@delete("/shares/{bundle_id:str}", status_code=200)
async def revoke_share(
    bundle_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    user_id = _extract_user(authorization)
    svc = await _build_service()
    # Ownership check.
    user_bundles = await svc.list_for_user(user_id=user_id, limit=200)
    if not any(b.id == bundle_id for b in user_bundles):
        raise HTTPException(status_code=404, detail="share not found")
    ok = await svc.revoke(bundle_id=bundle_id)
    from backend.core.observability import metrics as _m
    _m.record_share_event("revoked")
    await audit_log.emit(
        action="share.revoked",
        user_id=user_id,
        resource=f"share_bundle:{bundle_id}",
    )
    return {"revoked": ok, "bundle_id": bundle_id}


def _public_share_url(token: str) -> str:
    try:
        from backend.config import get_settings
        base = (get_settings().share_public_base_url or "").rstrip("/")
    except Exception:
        base = ""
    return f"{base}/shared/{token}" if base else f"/shared/{token}"


# === Partner-side (public, no auth) ========================================


@get("/shared/{token:str}")
async def shared_landing(
    token: str,
    request: Request,
) -> dict[str, Any]:
    """Public landing.

    Возвращает min meta — owner_org / note / count_of_resources / expires_at.
    Никаких resource_id или sensitive data до email verification.
    """
    svc = await _build_service()
    bundle = await svc.get_by_token(token=token)
    if bundle is None:
        raise HTTPException(status_code=404, detail="link not found")

    ip, ua = _client_meta(request)
    await record_view(
        postgres=svc.postgres,
        bundle_id=bundle.id,
        action="landing_open",
        ip_address=ip,
        user_agent=ua,
    )

    if not bundle.is_active():
        return {
            "active": False,
            "reason": "revoked" if bundle.revoked_at else "expired",
            "expires_at": bundle.expires_at,
            "revoked_at": bundle.revoked_at,
        }

    return {
        "active": True,
        "grantee_org": bundle.grantee_org,
        "note": bundle.note,
        "resources_count": len(bundle.grants),
        "expires_at": bundle.expires_at,
        "needs_email_verification": True,
    }


@post("/shared/{token:str}/verify")
async def shared_verify_email(
    token: str,
    data: dict[str, Any],
    request: Request,
) -> dict[str, Any]:
    """Partner вводит email — мы сверяем и выдаём ViewerJWT.

    Body: {"email": "bob@partner.com"}
    """
    email = (data.get("email") or "").strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="invalid email")

    svc = await _build_service()
    bundle = await svc.get_by_token(token=token)
    if bundle is None:
        raise HTTPException(status_code=404, detail="link not found")
    if not bundle.is_active():
        raise HTTPException(status_code=410, detail="link expired or revoked")

    if email != bundle.grantee_email.lower():
        # Не leakаем "правильный" email — generic 403.
        ip, ua = _client_meta(request)
        await record_view(
            postgres=svc.postgres,
            bundle_id=bundle.id,
            action="email_mismatch",
            grantee_email=email,
            ip_address=ip,
            user_agent=ua,
        )
        raise HTTPException(status_code=403, detail="email does not match")

    try:
        bundle_exp = datetime.fromisoformat(bundle.expires_at)
        if bundle_exp.tzinfo is None:
            bundle_exp = bundle_exp.replace(tzinfo=timezone.utc)
    except Exception:
        bundle_exp = None

    viewer_jwt = issue_viewer_token(
        bundle_id=bundle.id,
        grantee_email=email,
        bundle_expires_at=bundle_exp,
    )
    ip, ua = _client_meta(request)
    await record_view(
        postgres=svc.postgres,
        bundle_id=bundle.id,
        action="email_verified",
        grantee_email=email,
        ip_address=ip,
        user_agent=ua,
    )
    return {
        "viewer_token": viewer_jwt,
        "expires_at": bundle.expires_at,
        "resources_count": len(bundle.grants),
    }


async def _resolve_viewer_bundle(
    *,
    token: str,
    authorization: Optional[str],
) -> ShareBundle:
    """Общий guard: проверить что viewer_jwt валиден И принадлежит этому token-bundle."""
    viewer_jwt = _extract_viewer_token(authorization)
    claims = decode_viewer_token(viewer_jwt)
    if claims is None:
        raise HTTPException(status_code=401, detail="viewer token invalid/expired")
    svc = await _build_service()
    bundle = await svc.get_by_token(token=token)
    if bundle is None or bundle.id != claims.bundle_id:
        raise HTTPException(status_code=404, detail="share not found")
    if not bundle.is_active():
        raise HTTPException(status_code=410, detail="share expired or revoked")
    return bundle


@get("/shared/{token:str}/manifest")
async def shared_manifest(
    token: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Список ресурсов в scope (после email verification)."""
    bundle = await _resolve_viewer_bundle(
        token=token, authorization=authorization,
    )
    return {
        "bundle_id": bundle.id,
        "note": bundle.note,
        "expires_at": bundle.expires_at,
        "remaining_seconds": viewer_token_remaining_seconds(
            decode_viewer_token(_extract_viewer_token(authorization))
        ),
        "resources": [
            {
                "resource_type": g.resource_type,
                "resource_id": g.resource_id,
                "permissions": g.permissions,
            }
            for g in bundle.grants
        ],
    }


@get("/shared/{token:str}/resource/{resource_type:str}/{resource_id:str}")
async def shared_resource(
    token: str,
    resource_type: str,
    resource_id: str,
    request: Request,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Read-only proxy под viewer scope.

    Сейчас — meta-only response (resource_type, resource_id, permissions).
    Реальный fetch контента (PDF / meeting transcript) делается отдельным
    слоем (documents.fetch_for_share / meetings.fetch_for_share), который
    в W32+ подключим. Главное — scope check'ер сейчас работает 403'ит.
    """
    bundle = await _resolve_viewer_bundle(
        token=token, authorization=authorization,
    )
    try:
        permissions = check_resource_in_scope(
            bundle=bundle,
            resource_type=resource_type,
            resource_id=resource_id,
        )
    except ScopeViolation:
        ip, ua = _client_meta(request)
        await record_view(
            postgres=(await _build_service()).postgres,
            bundle_id=bundle.id,
            action="scope_violation",
            grantee_email=bundle.grantee_email,
            resource_type=resource_type,
            resource_id=resource_id,
            ip_address=ip,
            user_agent=ua,
        )
        raise HTTPException(status_code=403, detail="resource not in scope")

    # W32: реальный fetch контента ресурса.
    from backend.core.sharing.resource_fetcher import get_resource_fetcher
    fetcher = get_resource_fetcher()
    fetched = await fetcher.fetch(
        resource_type=resource_type,
        resource_id=resource_id,
    )

    ip, ua = _client_meta(request)
    await record_view(
        postgres=(await _build_service()).postgres,
        bundle_id=bundle.id,
        action="resource_view",
        grantee_email=bundle.grantee_email,
        resource_type=resource_type,
        resource_id=resource_id,
        ip_address=ip,
        user_agent=ua,
        metadata={
            "available": fetched.available,
            "truncated": fetched.truncated,
            "size_chars": len(fetched.content or ""),
        },
    )
    out = fetched.to_dict()
    out["permissions"] = permissions
    return out


# === W37: binary file download =========================================


@get("/shared/{token:str}/resource/{resource_type:str}/{resource_id:str}/download")
async def shared_resource_download(
    token: str,
    resource_type: str,
    resource_id: str,
    request: Request,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> Stream:
    """Streaming binary download для bundle с permissions=read+download.

    Двойная защита:
    1. ViewerJWT валиден + bundle активен
    2. Ресурс в scope
    3. **permissions == "read+download"** (только read → 403)

    Streaming через AsyncIterator — не буферизирует всё в память,
    подходит для PDF до DEFAULT_MAX_FILE_BYTES (50MB).
    """
    from urllib.parse import quote

    from backend.core.sharing.resource_fetcher import get_resource_fetcher

    bundle = await _resolve_viewer_bundle(
        token=token, authorization=authorization,
    )
    try:
        permissions = check_resource_in_scope(
            bundle=bundle,
            resource_type=resource_type,
            resource_id=resource_id,
        )
    except ScopeViolation:
        ip, ua = _client_meta(request)
        await record_view(
            postgres=(await _build_service()).postgres,
            bundle_id=bundle.id,
            action="scope_violation",
            grantee_email=bundle.grantee_email,
            resource_type=resource_type,
            resource_id=resource_id,
            ip_address=ip,
            user_agent=ua,
            metadata={"endpoint": "download"},
        )
        raise HTTPException(status_code=403, detail="resource not in scope")

    if permissions != "read+download":
        ip, ua = _client_meta(request)
        await record_view(
            postgres=(await _build_service()).postgres,
            bundle_id=bundle.id,
            action="download_denied",
            grantee_email=bundle.grantee_email,
            resource_type=resource_type,
            resource_id=resource_id,
            ip_address=ip,
            user_agent=ua,
            metadata={"permissions": permissions},
        )
        raise HTTPException(
            status_code=403,
            detail="permissions=read does not allow download (need read+download)",
        )

    fetcher = get_resource_fetcher()
    artifact = await fetcher.download(
        resource_type=resource_type,
        resource_id=resource_id,
    )
    if artifact is None:
        raise HTTPException(
            status_code=404,
            detail="file not available for download",
        )

    ip, ua = _client_meta(request)
    await record_view(
        postgres=(await _build_service()).postgres,
        bundle_id=bundle.id,
        action="resource_download",
        grantee_email=bundle.grantee_email,
        resource_type=resource_type,
        resource_id=resource_id,
        ip_address=ip,
        user_agent=ua,
        metadata={
            "file_name": artifact.safe_filename(),
            "mime_type": artifact.mime_type,
            "size_bytes": artifact.size_bytes,
        },
    )

    safe_name = artifact.safe_filename()
    headers = {
        "Content-Disposition": (
            f"attachment; filename=\"{safe_name}\"; "
            f"filename*=UTF-8''{quote(safe_name)}"
        ),
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if artifact.size_bytes >= 0:
        headers["Content-Length"] = str(artifact.size_bytes)

    return Stream(
        artifact.chunks,
        media_type=artifact.mime_type or "application/octet-stream",
        headers=headers,
    )


router = Router(
    path="",
    route_handlers=[
        create_share,
        list_shares,
        get_share,
        revoke_share,
        shared_landing,
        shared_verify_email,
        shared_manifest,
        shared_resource,
        shared_resource_download,
    ],
    tags=["Sharing"],
)
