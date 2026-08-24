# -*- coding: utf-8 -*-
"""HTTP API интеграции с Daily Pulse (INTEGRATION_TESSENT_RESPONSE.md).

- POST /integrations/daily-pulse/events  — ingest webhook (HMAC)
- GET  /integrations/context             — Фаза 2: контекст команде
- POST /integrations/identity/link       — ручная привязка
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from litestar import Request, Router, get, post
from litestar.exceptions import HTTPException
from litestar.params import Body, Parameter

logger = logging.getLogger(__name__)


def _resolve_user(request: Request, user_id: Optional[str]) -> str:
    """Определить user_id вызова. PermissionError → 403, непредвиденная
    ошибка валидации → 500 (а не тихий fail-open: чтобы баг в auth-слое
    был громким, а не маскировался). Бэккомпат для enable_service_auth=
    false работает через сам trusted_user_id (он возвращает uid из query
    без исключения)."""
    try:
        from backend.core.auth.service_token import trusted_user_id
        uid, _src = trusted_user_id(request.headers, user_id or "")
    except PermissionError:
        raise HTTPException(status_code=403, detail="token/user_id mismatch")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"_resolve_user: auth subsystem error: {e}",
                     exc_info=True)
        raise HTTPException(status_code=500, detail="auth subsystem error")
    if not uid:
        raise HTTPException(status_code=401, detail="user_id required")
    return uid


def _guard() -> None:
    from backend.core.integrations.daily_pulse import integration_enabled
    if not integration_enabled():
        raise HTTPException(
            status_code=403,
            detail="интеграция выключена (enable_daily_pulse_integration=false)")


@post("/daily-pulse/events")
async def ingest_event_route(request: Request) -> dict:
    """Принять событие. Проверка HMAC по заголовку X-Tessbrain-Signature
    (sha256=hex). Невалидная подпись → 401, но сырое сохраняется."""
    _guard()
    raw = await request.body()
    sig_header = request.headers.get("x-tessent-signature")
    from backend.core.integrations.daily_pulse import (
        apply_projection,
        project_event,
        secret_from_env,
        store_raw_event,
        verify_signature,
    )
    from backend.core.integrations.identity import resolve_or_link_by_email
    ok = verify_signature(secret_from_env(), raw, sig_header)
    try:
        import json as _json
        body = _json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        await store_raw_event("daily_pulse", "<unparseable>", {},
                              signature_ok=ok)
        raise HTTPException(status_code=400, detail="bad json")
    event = str(body.get("event") or "")
    await store_raw_event("daily_pulse", event, body, signature_ok=ok)
    if not ok:
        raise HTTPException(status_code=401, detail="signature mismatch")
    # external_id → tessent_user_id (best-effort; нет совпадения → None,
    # сырое сохранено, проекция всё равно вернётся клиенту)
    tessent_uid = await resolve_or_link_by_email(
        "daily_pulse", str(body.get("user_ref") or ""),
        email=body.get("user_email"))
    proj = project_event(event, body)
    ev, ins = await apply_projection(tessent_uid, proj)
    return {"success": not proj.get("errors"),
            "event": event,
            "tessent_user_id": tessent_uid,
            "events_pushed": ev, "insights_pushed": ins,
            "errors": proj.get("errors") or []}


@get("/context")
async def context_route(
        request: Request,
        team_ref: str = Parameter(query="team_ref"),
        since: Optional[str] = Parameter(query="since", default=None),
        until: Optional[str] = Parameter(query="until", default=None),
        topics: Optional[str] = Parameter(query="topics", default=None),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Контекст компании для команды Daily Pulse (Фаза 2)."""
    _guard()
    _resolve_user(request, user_id)
    from backend.core.integrations.context_api import context_for_team
    topics_list = [t.strip() for t in (topics or "").split(",") if t.strip()]
    return await context_for_team(team_ref, since=since, until=until,
                                  topics=topics_list)


@post("/identity/link")
async def identity_link_route(
        request: Request, data: dict = Body(),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Ручная привязка внешнего ID (admin/owner)."""
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.integrations.identity import link
    return await link(uid,
                      source=data.get("source", ""),
                      external_id=str(data.get("external_id") or ""),
                      tessent_user_id=str(data.get("tessent_user_id") or ""),
                      email=data.get("email"),
                      verified=bool(data.get("verified")))


@get("/identity/mappings")
async def list_mappings_route(
        request: Request,
        source: Optional[str] = Parameter(query="source", default=None),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Список identity-маппингов (админ-UI)."""
    _guard()
    _resolve_user(request, user_id)
    from backend.core.integrations.identity import list_mappings
    items = await list_mappings(source)
    return {"mappings": items, "total": len(items)}


@post("/identity/verify")
async def verify_mapping_route(
        request: Request, data: dict = Body(),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Подтвердить/снять верификацию: {source, external_id, verified}."""
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.integrations.identity import set_verified
    return await set_verified(uid, data.get("source", "daily_pulse"),
                              str(data.get("external_id") or ""),
                              bool(data.get("verified")))


@get("/events/recent")
async def recent_events_route(
        request: Request,
        source: Optional[str] = Parameter(query="source", default="daily_pulse"),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Последние принятые события (диагностика интеграции)."""
    _guard()
    _resolve_user(request, user_id)
    from backend.core.integrations.identity import recent_events
    items = await recent_events(source or "daily_pulse")
    return {"events": items, "total": len(items)}


integrations_router = Router(
    path="/integrations",
    route_handlers=[ingest_event_route, context_route, identity_link_route,
                    list_mappings_route, verify_mapping_route,
                    recent_events_route],
    tags=["Integrations"],
)
