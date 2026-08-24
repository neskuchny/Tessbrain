# -*- coding: utf-8 -*-
"""Skills routes (W16).

Skill = AgentAutomation с `is_skill=true`. Эндпоинты:
- `POST /api/v1/skills/from-conversation` — построить skill из последних
  сообщений диалога (через SkillBuilder + LLM).
- `POST /api/v1/skills/{id}/run` — запустить skill с args (подставить
  параметры в instruction_template и выполнить через AgentAutomationService).
- `GET  /api/v1/skills` — список skill'ов юзера (+ shared_in_tenant).
- `GET  /api/v1/skills/{id}` — детали.
- `DELETE /api/v1/skills/{id}` — удалить.

Аутентификация — через стандартный JWT-middleware (Authorization: Bearer).
SkillBuilder и AgentAutomationService instantiated лениво.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from litestar import Router, delete, get, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter

from backend.core.observability import audit_log
from backend.core.skills.parameters import (
    ParameterSchemaError,
    parse_schema,
    substitute_parameters,
    validate_args,
)

logger = logging.getLogger(__name__)


# === Auth helper (same pattern as gdpr.py) ===============================

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


# === Service factory =====================================================

async def _get_automation_service():
    from backend.core.automations.agent_automation_service import (
        get_agent_automation_service,
    )
    return get_agent_automation_service()


def _get_llm_router():
    from backend.core.llm.router import LLMRouter
    return LLMRouter()


# === Endpoints ============================================================

@get("/")
async def list_skills(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Список skill'ов текущего пользователя + shared_in_tenant.

    При первом обращении устанавливает предустановленный набор
    (skills/preinstalled.py) — идемпотентно, best-effort."""
    user_id = _extract_user(authorization)
    svc = await _get_automation_service()
    try:
        from backend.core.skills.preinstalled import ensure_preinstalled_skills
        await ensure_preinstalled_skills(svc, user_id)
    except Exception:
        logger.debug("preinstalled skills seeding skipped", exc_info=True)
    automations = await svc.list(user_id=user_id, status=None)
    skills = [
        a.to_dict() for a in automations
        if getattr(a, "is_skill", False)
    ]
    return {"skills": skills, "count": len(skills)}


@get("/{skill_id:str}")
async def get_skill(
    skill_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    user_id = _extract_user(authorization)
    svc = await _get_automation_service()
    skill = await svc.get(skill_id)
    if not skill or not getattr(skill, "is_skill", False):
        raise HTTPException(status_code=404, detail="skill not found")
    if skill.user_id != user_id and not getattr(skill, "shared_in_tenant", False):
        raise HTTPException(status_code=403, detail="not your skill")
    return skill.to_dict()


@post("/from-conversation")
async def create_skill_from_conversation(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Сгенерировать skill из последних messages.

    Body:
        {
          "messages": [{"role": "...", "content": "..."}],
          "conversation_id": "optional",
          "save": true,
          "shared_in_tenant": false
        }

    Если save=false — возвращаем proposal без сохранения (preview).
    Если save=true — создаём AgentAutomation с is_skill=true.
    """
    user_id = _extract_user(authorization)
    messages = data.get("messages") or []
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="messages must be a non-empty list")

    save = bool(data.get("save", True))
    conversation_id = data.get("conversation_id")
    shared = bool(data.get("shared_in_tenant", False))

    from backend.core.skills.builder import SkillBuilder, SkillProposal

    # Существующие skill names для подсказки LLM (избегаем дубликатов).
    svc = await _get_automation_service()
    existing = await svc.list(user_id=user_id, status=None)
    existing_names = [a.name for a in existing if getattr(a, "is_skill", False)]

    builder = SkillBuilder(llm_router=_get_llm_router())
    try:
        proposal = await builder.build(
            messages=messages,
            existing_skill_names=existing_names,
        )
    except ParameterSchemaError as exc:
        raise HTTPException(status_code=422, detail=f"skill validation failed: {exc}")
    except Exception as exc:
        logger.exception("SkillBuilder failed")
        raise HTTPException(status_code=500, detail=f"builder failed: {exc}")

    if not save:
        return {"saved": False, "proposal": proposal.to_dict()}

    # Сохраняем как AgentAutomation с is_skill=true.
    skill_id = str(uuid.uuid4())
    payload: dict[str, Any] = {
        "id": skill_id,
        "user_id": user_id,
        "name": proposal.name,
        "description": proposal.description,
        # `instruction` остаётся как fallback (без подстановки параметров),
        # реально при run использовать instruction_template.
        "instruction": proposal.instruction_template,
        "schedule_type": "once",
        "trigger_type": "schedule",
        "status": "active",
        # W16:
        "is_skill": True,
        "instruction_template": proposal.instruction_template,
        "parameters_schema": [p.to_dict() for p in proposal.parameters_schema],
        "shared_in_tenant": shared,
        "source_conversation_id": conversation_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        await svc.create(payload)
    except Exception as exc:
        logger.exception("Failed to save skill")
        raise HTTPException(status_code=500, detail=f"save failed: {exc}")

    await audit_log.emit(
        action="skill.create",
        user_id=user_id,
        resource=f"skill:{skill_id}",
        metadata={
            "name": proposal.name,
            "params_count": len(proposal.parameters_schema),
            "from_conversation": bool(conversation_id),
        },
    )
    return {"saved": True, "skill": payload, "proposal": proposal.to_dict()}


@post("/{skill_id:str}/run")
async def run_skill(
    skill_id: str,
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Запустить skill с указанными args.

    Body:
        {"args": {"period": "Q4", "team": "sales"}}
    """
    user_id = _extract_user(authorization)
    args = data.get("args") or {}
    if not isinstance(args, dict):
        raise HTTPException(status_code=400, detail="args must be a dict")

    svc = await _get_automation_service()
    skill = await svc.get(skill_id)
    if not skill or not getattr(skill, "is_skill", False):
        raise HTTPException(status_code=404, detail="skill not found")
    if skill.user_id != user_id and not getattr(skill, "shared_in_tenant", False):
        raise HTTPException(status_code=403, detail="not your skill")

    try:
        params = parse_schema(skill.parameters_schema or [])
        normalized = validate_args(params, args)
        template = skill.instruction_template or skill.instruction or ""
        rendered = substitute_parameters(template, normalized)
    except ParameterSchemaError as exc:
        raise HTTPException(status_code=422, detail=f"args validation failed: {exc}")

    # Подменяем instruction для этого run, не персистим.
    skill.instruction = rendered
    try:
        result = await svc.execute(skill)
    except Exception as exc:
        logger.exception("Skill run failed")
        raise HTTPException(status_code=500, detail=f"run failed: {exc}")

    await audit_log.emit(
        action="skill.run",
        user_id=user_id,
        resource=f"skill:{skill_id}",
        metadata={"args_keys": sorted(normalized.keys())},
    )
    return {
        "success": bool(result.get("success")),
        "skill_id": skill_id,
        "skill_name": skill.name,
        "args": normalized,
        "rendered_instruction": rendered,
        "trace": result.get("trace") or [],
        "final_answer": result.get("final_answer") or "",
    }


@delete("/{skill_id:str}", status_code=204)
async def delete_skill(
    skill_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> None:
    user_id = _extract_user(authorization)
    svc = await _get_automation_service()
    skill = await svc.get(skill_id)
    if not skill or not getattr(skill, "is_skill", False):
        raise HTTPException(status_code=404, detail="skill not found")
    if skill.user_id != user_id:
        raise HTTPException(status_code=403, detail="not your skill")
    await svc.delete(skill_id)
    await audit_log.emit(
        action="skill.delete",
        user_id=user_id,
        resource=f"skill:{skill_id}",
    )


router = Router(
    path="/skills",
    route_handlers=[
        list_skills,
        get_skill,
        create_skill_from_conversation,
        run_skill,
        delete_skill,
    ],
    tags=["Skills"],
)
