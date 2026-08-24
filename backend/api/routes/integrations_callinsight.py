# -*- coding: utf-8 -*-
"""HTTP API интеграции с CallInsight Pro (docs/CALLINSIGHT_INTEGRATION.md).

- POST /integrations/callinsight/events     — приёмник их webhooks
  (X-CIP-Signature, дедуп по event_id)
- GET  /integrations/callinsight/customer   — карточка клиента: live
  snapshot/timeline их REST + наш контекст + накопленные инсайты
- GET  /integrations/context-by-customer    — их этап 4: наши
  human/measured-артефакты по клиенту (для их Gemini-промпта)
"""
from __future__ import annotations

import logging
from typing import Optional

from litestar import Request, Router, get, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter

logger = logging.getLogger(__name__)


def _resolve_user(request: Request, user_id: Optional[str]) -> str:
    """См. integrations_daily_pulse._resolve_user — общая логика: 403 на
    PermissionError, 500 (с логом) на непредвиденную ошибку auth-слоя,
    401 если uid пуст. Тихий fail-open в Exception-ветке убран."""
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
    from backend.core.integrations.callinsight import integration_enabled
    if not integration_enabled():
        raise HTTPException(
            status_code=403,
            detail="интеграция выключена (enable_callinsight_integration=false)")


@post("/callinsight/events")
async def callinsight_ingest_route(request: Request) -> dict:
    """Принять webhook CallInsight. Подпись X-CIP-Signature
    (hex HMAC-SHA256 от сырого тела). Невалидная → 401, сырое сохраняется.
    Повторная доставка того же event_id → {"duplicate": true} без
    повторной проекции (их воркер ретраит при 5xx)."""
    _guard()
    raw = await request.body()
    sig_header = request.headers.get("x-cip-signature")
    from backend.core.integrations.callinsight import (
        project_event,
        resolve_receiver,
        secret_from_env,
        store_raw_event_dedup,
        verify_signature,
    )
    from backend.core.integrations.daily_pulse import apply_projection
    ok = verify_signature(secret_from_env(), raw, sig_header)
    try:
        import json as _json
        envelope = _json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        await store_raw_event_dedup("<unparseable>", {}, signature_ok=ok)
        raise HTTPException(status_code=400, detail="bad json")
    event = str(envelope.get("event") or "")
    fresh = await store_raw_event_dedup(event, envelope, signature_ok=ok)
    if not ok:
        raise HTTPException(status_code=401, detail="signature mismatch")
    if not fresh:
        return {"success": True, "duplicate": True, "event": event,
                "event_id": envelope.get("event_id")}
    proj = project_event(event, envelope)
    receiver = await resolve_receiver(envelope)
    ev, ins = await apply_projection(receiver, proj)
    return {"success": not proj.get("errors"),
            "event": event, "event_id": envelope.get("event_id"),
            "receiver": receiver,
            "events_pushed": ev, "insights_pushed": ins,
            "errors": proj.get("errors") or []}


@get("/callinsight/customer")
async def callinsight_customer_route(
        request: Request,
        phone: str = Parameter(query="phone"),
        name: Optional[str] = Parameter(query="name", default=None),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Карточка клиента (страница «Клиенты»): live snapshot/timeline из
    CallInsight + наш контекст + накопленные инсайты по этому телефону.
    Их API недоступен → card.available=false (честное «недоступно»)."""
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.integrations.callinsight import (
        fetch_customer_card,
        normalize_phone,
    )
    from backend.core.integrations.context_api import context_for_customer
    card = await fetch_customer_card(phone)
    our = await context_for_customer(uid, phone, name=name)
    # накопленные инсайты этого клиента (жалобы/at-risk из webhooks)
    norm = normalize_phone(phone)
    accrued = []
    try:
        from backend.core.sleep.insight_store import InsightStore
        from backend.core.store.tenant_paths import insights_path_for_user
        store = InsightStore(persist_path=insights_path_for_user(uid))
        for d in store.list(limit=200):
            if d.get("source") != "callinsight":
                continue
            if norm and d.get("customer_phone") == norm:
                accrued.append(d)
    except Exception as e:
        logger.debug(f"accrued insights skipped: {e}")
    return {"card": card, "our_context": our, "insights": accrued}


@get("/context-by-customer")
async def context_by_customer_route(
        request: Request,
        phone: str = Parameter(query="phone"),
        name: Optional[str] = Parameter(query="name", default=None),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Контекст Brain по клиенту для CallInsight (их этап 4): только
    human/measured-артефакты — llm_narrative исключён намеренно."""
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.integrations.context_api import context_for_customer
    return await context_for_customer(uid, phone, name=name)


@get("/callinsight/voice-report")
async def voice_report_route(
        request: Request,
        since: Optional[str] = Parameter(query="since", default=None),
        until: Optional[str] = Parameter(query="until", default=None),
        fmt: Optional[str] = Parameter(query="format", default=None),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Отчёт «Голос клиента»: жалобы, сделки к спасению, дельта возражений,
    coach-исходы, разбивка по менеджерам. Все числа считает код из
    накопленных событий (measured); цитаты — llm_narrative с пометкой."""
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.integrations.callinsight import (
        voice_of_customer,
        voice_of_customer_markdown,
    )
    events = []
    try:
        from backend.core.memory.event_log import EventLog
        from backend.core.store.tenant_paths import event_log_path_for_user
        log = EventLog(persist_path=event_log_path_for_user(uid))
        events = list(log.events())
        events = [e.to_dict() if hasattr(e, "to_dict") else dict(e)
                  for e in events]
    except Exception as e:
        logger.debug(f"voice report events: {e}")
    report = voice_of_customer(events, since=since, until=until)
    out = {"report": report}
    if (fmt or "").lower() in ("md", "markdown"):
        out["markdown"] = voice_of_customer_markdown(report)
    return out


@post("/callinsight/kb/sync")
async def kb_sync_route(
        request: Request,
        since: Optional[str] = Parameter(query="since", default=None),
        dry_run: bool = Parameter(query="dry_run", default=False),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Мост 4б: тянем их KB-секции (overview/products/services/objections/
    audience/competitors/kpis) и применяем по правилу verified=true → факт
    в нашем verified-сторе (human), verified=false → черновик в инсайтах
    (llm_narrative) на подтверждение owner. supersede store не трогается.

    dry_run=true — только показать, что бы применилось, ничего не пишем."""
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.integrations.callinsight import (
        apply_kb_projection,
        fetch_kb_sections,
        project_kb_sections,
    )
    fetched = await fetch_kb_sections(since)
    if not fetched.get("available"):
        return {"available": False, "error": fetched.get("error"),
                "applied": {"verified_facts": 0, "draft_insights": 0}}
    proj = project_kb_sections(fetched["items"])
    if dry_run:
        return {"available": True, "dry_run": True,
                "would_apply": {
                    "verified_facts": len(proj["verified_facts"]),
                    "draft_insights": len(proj["insights"])},
                "skipped": proj["skipped"]}
    applied = await apply_kb_projection(uid, proj)
    return {"available": True, "as_of": fetched.get("as_of"),
            "applied": applied, "skipped": proj["skipped"]}


callinsight_router = Router(
    path="/integrations",
    route_handlers=[callinsight_ingest_route, callinsight_customer_route,
                    context_by_customer_route, voice_report_route,
                    kb_sync_route],
    tags=["Integrations"],
)
