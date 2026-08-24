# -*- coding: utf-8 -*-
"""Расшаривание встречи через MeetFlow (публичная ссылка по токену).

MeetFlow сам владеет встречами и правами — мы НЕ дублируем его логику, а вызываем
его публичный API от имени пользователя:
  POST {MEETFLOW_API_URL}/api/meetings/{id}/shares  → {share_token}
  открывается по  {MEETFLOW_PUBLIC_URL}/shared/meeting/{token}  (без логина).

Авторизация — токеном пользователя (тот же Supabase-JWT, что и в Tessbrain, — общий
auth) для интерактивной кнопки, ИЛИ Personal Access Token (mf_pat_…) из
«Интеграций» для серверных прогонов доски/автоматизаций. Never-raise; всё за
флагом ENABLE_MEETFLOW_SHARE (без него узел/эндпоинт честно говорят «выключено»).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Публичная ссылка (meeting_shares) — БД жёстко разрешает только эти уровни
# (CHECK constraint). admin/edit тут невозможны — для них внутренние права.
SHARE_ACCESS = ("view", "comment")
# Внутренние права (resource_permissions) — по email/пользователю/команде.
PERMISSION_TYPES = ("read", "write", "admin")


def share_enabled() -> bool:
    return os.getenv("ENABLE_MEETFLOW_SHARE", "").strip().lower() in ("1", "on", "true", "yes")


def meetflow_api_base() -> str:
    return (os.getenv("MEETFLOW_API_URL", "") or "").strip().rstrip("/")


def meetflow_public_base() -> str:
    """База для ПУБЛИЧНОЙ ссылки (фронтенд MeetFlow). Нет → падаем на API-базу."""
    return ((os.getenv("MEETFLOW_PUBLIC_URL", "") or "").strip().rstrip("/")
            or meetflow_api_base())


async def meetflow_pat(user_id: Optional[str]) -> Optional[str]:
    """Personal Access Token пользователя для MeetFlow из «Интеграций»
    (для серверных вызовов, где нет живого токена сессии). Env MEETFLOW_PAT —
    платформенный фолбэк. None, если нигде нет."""
    env = (os.getenv("MEETFLOW_PAT", "") or "").strip()
    if env:
        return env
    if not user_id:
        return None
    try:
        from backend.api.routes.integrations import get_user_integration_keys
        keys = await get_user_integration_keys(user_id, "meetflow") or {}
        for k in ("pat", "api_key", "token", "access_token"):
            if keys.get(k):
                return str(keys[k]).strip()
    except Exception:
        logger.debug("meetflow PAT lookup failed", exc_info=True)
    return None


def meeting_public_link(meeting_id: str) -> Optional[str]:
    """Обычная ссылка на встречу (для доступа после гранта по email)."""
    pub = meetflow_public_base()
    mid = str(meeting_id or "").strip()
    return f"{pub}/meeting/{mid}" if (pub and mid) else None


def _extract_token(data: Any) -> str:
    """Достать share_token из ответа MeetFlow (разные обёртки)."""
    if not isinstance(data, dict):
        return ""
    if data.get("share_token"):
        return str(data["share_token"])
    for key in ("share", "data", "result"):
        inner = data.get(key)
        if isinstance(inner, dict) and inner.get("share_token"):
            return str(inner["share_token"])
    return ""


async def _meetflow_post(path: str, *, auth_token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """POST на MeetFlow с Bearer. Возврат {ok, status, data|error}. Never-raise."""
    base = meetflow_api_base()
    if not base:
        return {"ok": False, "error": "MEETFLOW_API_URL не задан на сервере"}
    if not (auth_token and str(auth_token).strip()):
        return {"ok": False, "error": "нет авторизации к MeetFlow (токен/PAT)"}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{base}{path}",
                headers={"Authorization": f"Bearer {str(auth_token).strip()}"},
                json=body)
    except Exception as e:
        logger.info("meetflow POST %s failed: %s", path, e)
        return {"ok": False, "error": f"MeetFlow недоступен: {e}"}
    if r.status_code not in (200, 201):
        # 401/403 — не админ встречи; 400 — валидация. Отдаём честно.
        return {"ok": False, "status": r.status_code,
                "error": f"MeetFlow {r.status_code}: {r.text[:200]}"}
    try:
        return {"ok": True, "status": r.status_code, "data": r.json()}
    except Exception:
        return {"ok": True, "status": r.status_code, "data": {}}


async def create_public_share(meeting_id: str, *, auth_token: str,
                              access_level: str = "view",
                              expires_in_days: Optional[int] = 7,
                              max_views: Optional[int] = None,
                              allowed_domains: Optional[List[str]] = None,
                              password: Optional[str] = None) -> Dict[str, Any]:
    """Публичная ссылка на встречу (meeting_shares). access_level — ТОЛЬКО
    view/comment (ограничение БД MeetFlow; admin/edit → через grant по email).
    expires_in_days: int (срок), None (бессрочно), по умолчанию 7 у MeetFlow.
    Возврат {ok, share_url?, token?, access_level?, error?}. Never-raise."""
    mid = str(meeting_id or "").strip()
    if not mid:
        return {"ok": False, "error": "не задан ID встречи"}
    al = (access_level or "view").strip().lower() or "view"
    if al not in SHARE_ACCESS:
        return {"ok": False, "error": (
            f"публичная ссылка поддерживает только {', '.join(SHARE_ACCESS)}; "
            "для доступа admin/edit используйте выдачу по email (права).")}

    body: Dict[str, Any] = {"access_level": al}
    # Всегда шлём expires_in_days явно: int → срок, None → бессрочно.
    body["expires_in_days"] = expires_in_days
    if max_views:
        try:
            body["max_views"] = max(1, int(max_views))
        except (TypeError, ValueError):
            pass
    domains = [d.strip() for d in (allowed_domains or []) if str(d).strip()]
    if domains:
        body["allowed_domains"] = domains
    if password and str(password).strip():
        body["password"] = str(password).strip()

    res = await _meetflow_post(f"/api/meetings/{mid}/shares",
                               auth_token=auth_token, body=body)
    if not res.get("ok"):
        return res
    token = _extract_token(res.get("data"))
    if not token:
        return {"ok": False, "error": "MeetFlow не вернул share_token",
                "raw": res.get("data")}
    pub = meetflow_public_base()
    return {"ok": True, "token": token,
            "share_url": (f"{pub}/shared/meeting/{token}" if pub else None),
            "access_level": al}


async def grant_meeting_permission(meeting_id: str, *, auth_token: str,
                                   grantee_id: str,
                                   permission_type: str = "read") -> Dict[str, Any]:
    """Дать доступ к встрече конкретному человеку по EMAIL или UUID (внутренние
    права resource_permissions). MeetFlow сам резолвит email→user. permission_type
    — read/write/admin. Возврат {ok, meeting_link?, granted_to?, permission_type?,
    error?}. Never-raise."""
    mid = str(meeting_id or "").strip()
    grantee = str(grantee_id or "").strip()
    if not (mid and grantee):
        return {"ok": False, "error": "нужны встреча и email/ID получателя"}
    pt = (permission_type or "read").strip().lower() or "read"
    if pt not in PERMISSION_TYPES:
        return {"ok": False, "error": f"permission_type из {PERMISSION_TYPES}"}
    body = {"grantee_type": "user", "grantee_id": grantee, "permission_type": pt}
    res = await _meetflow_post(f"/api/meetings/{mid}/permissions",
                               auth_token=auth_token, body=body)
    if not res.get("ok"):
        return res
    return {"ok": True, "granted_to": grantee, "permission_type": pt,
            "meeting_link": meeting_public_link(mid)}


__all__ = [
    "PERMISSION_TYPES",
    "SHARE_ACCESS",
    "create_public_share",
    "grant_meeting_permission",
    "meetflow_api_base",
    "meetflow_pat",
    "meetflow_public_base",
    "meeting_public_link",
    "share_enabled",
]
