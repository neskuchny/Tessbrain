# -*- coding: utf-8 -*-
"""Validator API (W21) — финальная проверка результата executor'а.

Endpoint:
- `POST /api/v1/validator/run` — запустить набор validators на artifacts
- `POST /api/v1/validator/from-task/{handle_id}` — взять artifacts из
  TaskResult и прогнать validators
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from litestar import Router, get, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter

from backend.core.observability import audit_log
from backend.core.validator.aggregator import aggregate
from backend.core.validator.ai_judge import AIJudgeValidator
from backend.core.validator.base import Validator, ValidatorReport
from backend.core.validator.code import CodeValidator
from backend.core.validator.store import (
    QuotaConfig,
    QuotaExceeded,
    ValidationResultService,
    assert_within_quota,
)
from backend.core.validator.structural import StructuralValidator
from backend.core.validator.visual import VisualDiffValidator

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


def _build_validators(enabled: list[str]) -> list[Validator]:
    out: list[Validator] = []
    if "structural" in enabled:
        out.append(StructuralValidator())
    if "code" in enabled:
        out.append(CodeValidator())
    if "ai_judge" in enabled:
        try:
            from backend.core.llm.router import LLMRouter
            out.append(AIJudgeValidator(llm_router=LLMRouter()))
        except Exception as exc:
            logger.debug("validator routes: AI judge unavailable: %s", exc)
            out.append(AIJudgeValidator(llm_router=None))
    if "visual_diff" in enabled:
        out.append(VisualDiffValidator())
    return out


_DEFAULT_ENABLED = ["structural", "ai_judge"]
_DEFAULT_QUOTA = QuotaConfig()


def _build_store() -> ValidationResultService:
    """Best-effort builder для ValidationResultService."""
    postgres = None
    try:
        from backend.db.postgres import PostgresClient
        postgres = PostgresClient()
    except Exception as exc:
        logger.debug("validator.store: postgres unavailable: %s", exc)
    return ValidationResultService(postgres=postgres)


@post("/run")
async def run_validators(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Запустить validators на переданных artifacts.

    Body:
        {
          "task_description": "...",
          "tz_markdown": "...",
          "artifacts": [...],
          "template": {...},                  # опц для structural
          "brand_context_block": "...",       # опц для ai_judge
          "validators": ["structural", "ai_judge"],   # опц
          "weights": {"ai_judge": 3.0},                # опц
          "auto_approve_threshold": 8.5,                # опц
          "human_review_threshold": 5.0
        }
    """
    user_id = _extract_user(authorization)
    task_description = (data.get("task_description") or "").strip()
    tz_markdown = (data.get("tz_markdown") or "").strip()
    artifacts = data.get("artifacts") or []
    if not isinstance(artifacts, list):
        raise HTTPException(status_code=400, detail="artifacts must be a list")
    if not task_description and not tz_markdown:
        raise HTTPException(
            status_code=400,
            detail="task_description or tz_markdown is required",
        )

    enabled = data.get("validators") or _DEFAULT_ENABLED
    if not isinstance(enabled, list):
        enabled = _DEFAULT_ENABLED

    # W23: per-tenant quota (best-effort, fail-open если postgres недоступен).
    store = _build_store()
    try:
        await assert_within_quota(service=store, config=_DEFAULT_QUOTA)
    except QuotaExceeded as exc:
        from backend.core.observability import metrics as _m
        _m.validator_quota_blocks_total.labels(scope=exc.scope).inc()
        raise HTTPException(
            status_code=429,
            detail={
                "error": "validation_quota_exceeded",
                "scope": exc.scope,
                "limit": exc.limit,
                "used": exc.used,
            },
            headers={"Retry-After": "60"},
        )

    import time as _time
    _t_start = _time.monotonic()

    template = data.get("template")
    brand_context_block = str(data.get("brand_context_block") or "")
    persist = bool(data.get("persist", True))
    task_handle_id = data.get("task_handle_id")
    task_type = data.get("task_type")

    validators = _build_validators([str(v).lower() for v in enabled])
    if not validators:
        raise HTTPException(status_code=400, detail="no validators selected")

    reports: list[ValidatorReport] = []
    for v in validators:
        try:
            r = await v.validate(
                task_description=task_description,
                tz_markdown=tz_markdown,
                artifacts=artifacts,
                template=template,
                brand_context_block=brand_context_block,
            )
        except Exception as exc:
            logger.exception("validator %s raised", v.name)
            r = ValidatorReport(
                validator=v.name,
                score=5.0,
                error=f"validator raised: {exc}",
                summary=f"{v.name} crashed during validation",
            )
        reports.append(r)

    weights = data.get("weights") or None
    auto_thr = float(data.get("auto_approve_threshold") or 8.5)
    review_thr = float(data.get("human_review_threshold") or 5.0)
    aggregated = aggregate(
        reports,
        weights=weights if isinstance(weights, dict) else None,
        auto_approve_threshold=auto_thr,
        human_review_threshold=review_thr,
    )

    # W35: instrument run.
    from backend.core.observability import metrics as _m
    _m.record_validator_run(
        decision=aggregated.decision.value,
        duration_s=_time.monotonic() - _t_start,
    )

    # W23: persist в Postgres (best-effort).
    persisted_id: Optional[str] = None
    if persist:
        persisted_id = await store.save(
            user_id=user_id,
            aggregated=aggregated,
            artifacts=artifacts,
            task_handle_id=task_handle_id,
            task_type=task_type,
        )

    await audit_log.emit(
        action="validator.run",
        user_id=user_id,
        resource=f"validation:{persisted_id or 'transient'}",
        metadata={
            "validators": [r.validator for r in reports],
            "decision": aggregated.decision.value,
            "score": aggregated.aggregate_score,
            "persisted_id": persisted_id,
        },
    )

    response = aggregated.to_dict()
    if persisted_id:
        response["validation_id"] = persisted_id
    return response


@post("/from-task/{handle_id:str}")
async def run_from_task(
    handle_id: str,
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Загрузить artifacts из TaskResult и прогнать validators.

    Body — те же опции что у /run, но без `artifacts` (берутся из
    handle_id) и `tz_markdown` (передаётся явно).
    """
    user_id = _extract_user(authorization)
    from backend.core.executors.store import get_handle, get_result
    handle = await get_handle(handle_id)
    if handle is None:
        raise HTTPException(status_code=404, detail="task not found")
    if handle.user_id and handle.user_id != user_id:
        raise HTTPException(status_code=403, detail="not your task")
    raw = await get_result(handle_id)
    if raw is None:
        raise HTTPException(status_code=404, detail="task result not yet available")

    artifacts = list(raw.get("artifacts") or [])
    enriched = {**data, "artifacts": artifacts}
    return await run_validators(enriched, authorization=authorization)


@get("/history")
async def list_history(
    decision: Optional[str] = Parameter(query="decision", default=None),
    limit: int = Parameter(query="limit", default=50),
    offset: int = Parameter(query="offset", default=0),
    unreviewed_only: bool = Parameter(query="unreviewed_only", default=False),
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """История validation runs текущего юзера. RLS фильтрует tenant'.

    Query:
    - decision: auto_approve / human_review / reject (опц)
    - unreviewed_only: True → только pending в human-review queue
    """
    user_id = _extract_user(authorization)
    from backend.core.validator.aggregator import ValidationDecision
    decision_enum = None
    if decision:
        try:
            decision_enum = ValidationDecision(decision)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"unknown decision: {decision}")

    store = _build_store()
    rows = await store.list_for_user(
        user_id=user_id,
        decision=decision_enum,
        limit=limit,
        offset=offset,
        unreviewed_only=unreviewed_only,
    )
    return {"validations": [r.to_dict() for r in rows], "count": len(rows)}


@get("/{validation_id:str}")
async def get_validation(
    validation_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    user_id = _extract_user(authorization)
    store = _build_store()
    row = await store.get(validation_id=validation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="validation not found")
    if row.user_id and row.user_id != user_id:
        raise HTTPException(status_code=403, detail="not your validation")
    return row.to_dict()


@post("/{validation_id:str}/review")
async def review(
    validation_id: str,
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Записать human review результата (approve / reject / escalate).

    Body:
        {"decision": "approve", "notes": "looks good"}
    """
    user_id = _extract_user(authorization)
    decision_str = (data.get("decision") or "").strip().lower()
    if decision_str not in {"approve", "reject", "escalate"}:
        raise HTTPException(
            status_code=400,
            detail="decision must be approve / reject / escalate",
        )
    notes = str(data.get("notes") or "")[:2000]

    store = _build_store()
    target = await store.get(validation_id=validation_id)
    if target is None:
        raise HTTPException(status_code=404, detail="validation not found")
    if target.user_id and target.user_id != user_id:
        # Reviewer может быть не autor — но всё равно tenant_isolation
        # уже сработала на get(). Разрешаем review.
        pass

    ok = await store.mark_reviewed(
        validation_id=validation_id,
        reviewer_user_id=user_id,
        human_decision=decision_str,
        human_notes=notes,
    )
    if not ok:
        raise HTTPException(status_code=500, detail="review save failed")

    await audit_log.emit(
        action="validator.review",
        user_id=user_id,
        resource=f"validation:{validation_id}",
        metadata={"human_decision": decision_str},
    )
    return {"reviewed": True, "validation_id": validation_id}


router = Router(
    path="/validator",
    route_handlers=[
        run_validators, run_from_task,
        list_history, get_validation, review,
    ],
    tags=["Validator"],
)
