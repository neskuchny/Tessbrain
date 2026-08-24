# -*- coding: utf-8 -*-
"""TZ templates + iteration + judge API (W18 + W24 CRUD).

Endpoints:
- `GET    /api/v1/tz-templates/defaults`  — список built-in default seeds
- `GET    /api/v1/tz-templates/defaults/{task_type}` — конкретный default
- `POST   /api/v1/tz-templates/validate` — структурная валидация шаблона
- `POST   /api/v1/tz-templates/judge` — sufficiency-judge на готовом ТЗ
- `POST   /api/v1/tz-templates/delta` — iteration delta-spec по feedback'у
- `GET    /api/v1/tz-templates`  (W24) — list tenant + defaults
- `POST   /api/v1/tz-templates`  (W24) — create custom template
- `GET    /api/v1/tz-templates/{id}` (W24) — read одного template
- `PATCH  /api/v1/tz-templates/{id}` (W24) — update mutable fields
- `POST   /api/v1/tz-templates/{id}/set-default` (W24) — выставить default
- `DELETE /api/v1/tz-templates/{id}` (W24) — удалить
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from litestar import Router, delete, get, patch, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter

from backend.core.observability import audit_log
from backend.core.tz.iteration import apply_delta, generate_delta
from backend.core.tz.template_service import (
    TemplateConflict,
    TemplateNotFound,
    TZTemplateService,
)
from backend.core.tz.templates import (
    DEFAULT_TEMPLATES,
    TemplateValidationError,
    TZTemplate,
    get_default_template,
    list_default_task_types,
)
from backend.core.tz.validator_judge import judge_specification

logger = logging.getLogger(__name__)


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


def _get_llm_router():
    try:
        from backend.core.llm.router import LLMRouter
        return LLMRouter()
    except Exception as exc:
        logger.debug("tz_templates: llm router unavailable: %s", exc)
        return None


# === Defaults / templates =================================================

@get("/defaults")
async def list_defaults() -> dict[str, Any]:
    """Список default seed-шаблонов которые мы поставляем из коробки."""
    return {
        "task_types": list_default_task_types(),
        "summaries": [
            {
                "task_type": tt,
                "name": DEFAULT_TEMPLATES[tt]["name"],
                "description": DEFAULT_TEMPLATES[tt].get("description", ""),
                "blocks": [b["name"] for b in DEFAULT_TEMPLATES[tt]["blocks"]],
            }
            for tt in list_default_task_types()
        ],
    }


@get("/defaults/{task_type:str}")
async def get_default(task_type: str) -> dict[str, Any]:
    template = get_default_template(task_type)
    if not template:
        raise HTTPException(status_code=404, detail=f"no default for task_type={task_type!r}")
    return {"template": template.to_dict()}


@post("/validate")
async def validate_template(data: dict[str, Any]) -> dict[str, Any]:
    """Структурная валидация шаблона.

    Body:
        {"template": {...}}
    """
    raw = data.get("template")
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="body.template must be a dict")
    try:
        tpl = TZTemplate.from_dict(raw)
    except TemplateValidationError as exc:
        return {"valid": False, "error": str(exc)}
    return {"valid": True, "template": tpl.to_dict()}


# === Judge =================================================================

@post("/judge")
async def judge(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Запустить sufficiency-judge на ТЗ.

    Body:
        {
          "task_description": "...",
          "tz_markdown": "...",
          "template_required_blocks": ["hero", "pricing"]   # опц
        }
    """
    user_id = _extract_user(authorization)
    task_description = (data.get("task_description") or "").strip()
    tz_markdown = (data.get("tz_markdown") or "").strip()
    if not task_description or not tz_markdown:
        raise HTTPException(
            status_code=400,
            detail="task_description and tz_markdown are required",
        )

    required_blocks = data.get("template_required_blocks") or []
    if not isinstance(required_blocks, list):
        required_blocks = []

    result = await judge_specification(
        task_description=task_description,
        tz_markdown=tz_markdown,
        template_required_blocks=[str(b) for b in required_blocks],
        llm_router=_get_llm_router(),
    )

    await audit_log.emit(
        action="tz.judge",
        user_id=user_id,
        resource="tz",
        metadata={"score": result.score, "verdict": result.verdict},
    )
    return result.to_dict()


# === Iteration delta =======================================================

@post("/delta")
async def iteration_delta(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Применить feedback пользователя к ТЗ через delta-spec.

    Body:
        {
          "tz_markdown": "...",
          "user_feedback": "hero: цифр 50K не 5K",
          "apply": true,                     # default true — вернуть merged markdown
          "brand_context_block": "..."       # опц
        }

    Returns:
        {
          "delta": {...},
          "merged_markdown": "...",   # если apply=true
        }
    """
    user_id = _extract_user(authorization)
    tz_markdown = (data.get("tz_markdown") or "").strip()
    user_feedback = (data.get("user_feedback") or "").strip()
    if not tz_markdown or not user_feedback:
        raise HTTPException(
            status_code=400,
            detail="tz_markdown and user_feedback are required",
        )
    apply = bool(data.get("apply", True))
    brand_block = str(data.get("brand_context_block") or "")

    delta = await generate_delta(
        original_markdown=tz_markdown,
        user_feedback=user_feedback,
        brand_context_block=brand_block,
        llm_router=_get_llm_router(),
    )

    response: dict[str, Any] = {"delta": delta.to_dict()}
    if apply and delta.affected_blocks and not delta.error:
        response["merged_markdown"] = apply_delta(tz_markdown, delta)

    await audit_log.emit(
        action="tz.iteration_delta",
        user_id=user_id,
        resource="tz",
        metadata={
            "affected_blocks": delta.affected_blocks,
            "had_error": bool(delta.error),
        },
    )
    return response


# === W24: Custom templates CRUD ==========================================

def _build_service() -> TZTemplateService:
    """Best-effort builder для TZTemplateService."""
    postgres = None
    try:
        from backend.db.postgres import PostgresClient
        postgres = PostgresClient()
    except Exception as exc:
        logger.debug("tz_templates: postgres unavailable: %s", exc)
    return TZTemplateService(postgres=postgres)


@get("/")
async def list_templates(
    task_type: Optional[str] = Parameter(query="task_type", default=None),
    include_defaults: bool = Parameter(query="include_defaults", default=True),
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """List built-in defaults + tenant-specific templates."""
    _extract_user(authorization)
    svc = _build_service()
    rows = await svc.list(task_type=task_type, include_defaults=include_defaults)
    return {"templates": [r.to_dict() for r in rows], "count": len(rows)}


@post("/", status_code=201)
async def create_template(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Создать custom template для tenant'а.

    Body:
        {
          "task_type": "landing",
          "name": "my_custom_landing",
          "description": "...",
          "blocks": [...],
          "is_default": false
        }
    """
    user_id = _extract_user(authorization)
    task_type = (data.get("task_type") or "").strip()
    name = (data.get("name") or "").strip()
    blocks = data.get("blocks")
    if not task_type or not name:
        raise HTTPException(status_code=400, detail="task_type and name are required")
    if not isinstance(blocks, list) or not blocks:
        raise HTTPException(status_code=400, detail="blocks must be a non-empty list")

    svc = _build_service()
    if not svc.has_db:
        raise HTTPException(
            status_code=503,
            detail="postgres unavailable; cannot create custom templates",
        )

    try:
        created = await svc.create(
            task_type=task_type,
            name=name,
            description=str(data.get("description") or ""),
            blocks=blocks,
            is_default=bool(data.get("is_default", False)),
            created_by=user_id,
        )
    except TemplateValidationError as exc:
        raise HTTPException(status_code=400, detail=f"validation: {exc}")
    except TemplateConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        logger.exception("tz_templates.create failed")
        raise HTTPException(status_code=500, detail=f"create failed: {exc}")

    await audit_log.emit(
        action="tz_template.create",
        user_id=user_id,
        resource=f"tz_template:{created.id}",
        metadata={"task_type": task_type, "name": name,
                  "is_default": created.is_default},
    )
    return {"template": created.to_dict()}


@get("/{template_id:str}")
async def get_template(
    template_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Read одного template (built-in default или tenant-specific)."""
    _extract_user(authorization)
    svc = _build_service()
    found = await svc.get(template_id=template_id)
    if found is None:
        raise HTTPException(status_code=404, detail="template not found")
    return found.to_dict()


@patch("/{template_id:str}")
async def update_template(
    template_id: str,
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Update mutable fields. is_default — отдельным /set-default."""
    user_id = _extract_user(authorization)
    svc = _build_service()
    if not svc.has_db:
        raise HTTPException(status_code=503, detail="postgres unavailable")

    try:
        updated = await svc.update(
            template_id=template_id,
            name=data.get("name"),
            description=data.get("description"),
            blocks=data.get("blocks"),
        )
    except TemplateValidationError as exc:
        raise HTTPException(status_code=400, detail=f"validation: {exc}")
    except TemplateNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except TemplateConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        logger.exception("tz_templates.update failed")
        raise HTTPException(status_code=500, detail=f"update failed: {exc}")

    await audit_log.emit(
        action="tz_template.update",
        user_id=user_id,
        resource=f"tz_template:{template_id}",
    )
    return {"template": updated.to_dict()}


@post("/{template_id:str}/set-default")
async def set_default_template(
    template_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Сделать template дефолтным для своего task_type."""
    user_id = _extract_user(authorization)
    svc = _build_service()
    if not svc.has_db:
        raise HTTPException(status_code=503, detail="postgres unavailable")
    try:
        updated = await svc.set_default(template_id=template_id)
    except TemplateNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except TemplateConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        logger.exception("tz_templates.set_default failed")
        raise HTTPException(status_code=500, detail=f"set_default failed: {exc}")
    await audit_log.emit(
        action="tz_template.set_default",
        user_id=user_id,
        resource=f"tz_template:{template_id}",
        metadata={"task_type": updated.task_type},
    )
    return {"template": updated.to_dict()}


@delete("/{template_id:str}", status_code=204)
async def delete_template(
    template_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> None:
    user_id = _extract_user(authorization)
    svc = _build_service()
    if not svc.has_db:
        raise HTTPException(status_code=503, detail="postgres unavailable")
    try:
        ok = await svc.delete(template_id=template_id)
    except TemplateConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        logger.exception("tz_templates.delete failed")
        raise HTTPException(status_code=500, detail=f"delete failed: {exc}")
    if not ok:
        raise HTTPException(status_code=500, detail="delete failed")
    await audit_log.emit(
        action="tz_template.delete",
        user_id=user_id,
        resource=f"tz_template:{template_id}",
    )


router = Router(
    path="/tz-templates",
    route_handlers=[
        list_defaults,
        get_default,
        validate_template,
        judge,
        iteration_delta,
        # W24 CRUD:
        list_templates,
        create_template,
        get_template,
        update_template,
        set_default_template,
        delete_template,
    ],
    tags=["TZ"],
)
