# -*- coding: utf-8 -*-
"""User onboarding-state endpoint (Wave 3.2).

`GET /me/onboarding` возвращает «чек-лист» для UI: что новый user уже
сделал, что осталось, какой next action подсказать.

Шаги (минимально-полезный набор для команды):
    1. account_created       — JWT валидный (всегда true в этом handler)
    2. org_joined            — есть запись в user_org_mapping
    3. messenger_connected   — есть хоть один MessengerLink (Telegram/Slack)
    4. first_meeting_processed — есть хоть одна completed запись в
                                  processed_meetings

Все проверки best-effort: если Supabase / messengers недоступны,
соответствующий step возвращается с done=false + reason (а не 500).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import jwt
from litestar import Router, get
from litestar.exceptions import HTTPException
from litestar.params import Parameter

from backend.core.ingest import membership

logger = logging.getLogger(__name__)


# === Auth helper (минимальный, чтобы не зависеть от orgs.py) ================


def _extract_user_id(authorization: Optional[str]) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.split(" ", 1)[1]
    try:
        claims = jwt.decode(token, options={"verify_signature": False}) or {}
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")
    user_id = claims.get("sub") or claims.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing sub/user_id")
    return str(user_id)


# === Step checks (каждый best-effort, ошибка не валит endpoint) =============


async def _check_messengers(user_id: str) -> dict[str, Any]:
    """Есть ли хотя бы один активный MessengerLink? Возвращает done + platforms."""
    try:
        from backend.core.messengers.links import MessengerLinkService
        from backend.db.postgres import get_postgres
        # Best-effort: если postgres unreachable → пустой список, NOT raise.
        pg = None
        try:
            pg = await get_postgres()
        except Exception as e:
            logger.debug("postgres unavailable for messengers check: %s", e)
        svc = MessengerLinkService(postgres=pg)
        links = await svc.list_for_user(user_id=user_id)
        platforms = sorted({getattr(l, "platform", "") for l in links if getattr(l, "platform", "")})
        return {"done": bool(links), "platforms": platforms}
    except Exception as e:
        logger.debug("messengers check failed: %s", e)
        return {"done": False, "platforms": [], "error": "check_unavailable"}


async def _check_first_meeting(user_id: str) -> dict[str, Any]:
    """Есть ли хоть один completed/no_transcript эпизод в processed_meetings?"""
    try:
        from backend.db.supabase_client import get_supabase_client
        sb = get_supabase_client()
        rows = await sb._request(
            "GET", "/rest/v1/processed_meetings",
            params={
                "user_id": f"eq.{user_id}",
                "status": "in.(completed,no_transcript)",
                "select": "id",
                "limit": "1",
            },
        )
        return {"done": bool(rows)}
    except Exception as e:
        logger.debug("processed_meetings check failed: %s", e)
        return {"done": False, "error": "check_unavailable"}


# === Endpoint ===============================================================


@get("/me/onboarding", status_code=200)
async def get_my_onboarding(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Onboarding checklist для текущего user'а.

    Response:
        {
          "user_id": "...",
          "steps": [
              {"id": "account_created", "done": true, "label": ..., "action": null},
              {"id": "org_joined", "done": bool, "org_id": "...", "action": ...},
              {"id": "messenger_connected", "done": bool, "platforms": [...], "action": ...},
              {"id": "first_meeting_processed", "done": bool, "action": ...},
          ],
          "completed": int,
          "total": int,
          "next_action": "<step_id или null если всё done>"
        }
    """
    user_id = _extract_user_id(authorization)

    steps: list[dict[str, Any]] = []

    # 1. Account created — раз сюда дошли, JWT валиден.
    steps.append({
        "id": "account_created",
        "label": "Account created",
        "done": True,
        "action": None,
    })

    # 2. Org joined.
    org_id = membership.get_org_for_user(user_id)
    role = membership.get_user_org_role(user_id)
    steps.append({
        "id": "org_joined",
        "label": "Joined an organization",
        "done": org_id is not None,
        "org_id": org_id,
        "role": role,
        "action": None if org_id else "Ask your admin for an invite link, then POST /invites/{token}/accept",
    })

    # 3. Messenger connected.
    msg = await _check_messengers(user_id)
    steps.append({
        "id": "messenger_connected",
        "label": "Connected a messenger (Telegram/Slack/…)",
        "done": msg["done"],
        "platforms": msg.get("platforms", []),
        "action": None if msg["done"] else "POST /messengers/telegram/onboarding/start",
        **({"error": msg["error"]} if "error" in msg else {}),
    })

    # 4. First meeting processed.
    meet = await _check_first_meeting(user_id)
    steps.append({
        "id": "first_meeting_processed",
        "label": "Processed at least one meeting",
        "done": meet["done"],
        "action": None if meet["done"] else "Create a knowledge-sync subscription and POST /knowledge-sync/sync/{subscription_id}",
        **({"error": meet["error"]} if "error" in meet else {}),
    })

    completed = sum(1 for s in steps if s["done"])
    next_step = next((s["id"] for s in steps if not s["done"]), None)

    return {
        "user_id": user_id,
        "steps": steps,
        "completed": completed,
        "total": len(steps),
        "next_action": next_step,
        "is_complete": completed == len(steps),
    }


router = Router(
    path="",
    route_handlers=[get_my_onboarding],
    tags=["Onboarding"],
)
