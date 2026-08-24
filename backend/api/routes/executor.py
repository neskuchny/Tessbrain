# -*- coding: utf-8 -*-
"""Executor API (W19).

Endpoints:
- POST   /api/v1/executor/submit          — отправить ТЗ executor'у
- GET    /api/v1/executor/{id}/status      — текущий статус
- GET    /api/v1/executor/{id}/result      — финальный результат
- POST   /api/v1/executor/{id}/cancel      — отменить
- POST   /api/v1/executor/callback         — webhook для backends'ов
- GET    /api/v1/executor/backends         — список доступных backends

Все endpoints (кроме callback) — JWT-authed.
Callback — SHA-token-authed (executor приходит с другой стороны).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from litestar import Router, get, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter

from backend.core.executors.base import ExecutorError, TaskStatus, TaskSubmission
from backend.core.executors.registry import (
    get_active_backend,
    get_backend,
    list_backend_names,
    resolve_backend_name_for_task_type,
)
from backend.core.executors.store import (
    get_handle,
    get_result,
    save_result,
    update_status,
)
from backend.core.observability import audit_log

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


# === Endpoints ===========================================================

@get("/backends")
async def list_backends() -> dict[str, Any]:
    """Список зарегистрированных backends + текущий active."""
    try:
        from backend.config import get_settings
        active = getattr(get_settings(), "executor_backend", "noop")
    except Exception:
        active = "noop"
    return {"backends": list_backend_names(), "active": active}


@post("/submit")
async def submit(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Отправить ТЗ.

    Body:
        {
          "tz_markdown": "...",
          "task_type": "landing",      # опц
          "working_dir": "/tmp/...",   # опц
          "metadata": {...},            # опц
          "callback_url": "https://...",  # опц
          "timeout_seconds": 3600,     # опц
          "backend": "noop"             # опц override default
        }
    """
    user_id = _extract_user(authorization)
    tz = (data.get("tz_markdown") or "").strip()
    if not tz:
        raise HTTPException(status_code=400, detail="tz_markdown is required")

    backend_override = (data.get("backend") or "").strip() or None

    try:
        backend = get_active_backend(override=backend_override)
    except ExecutorError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    submission = TaskSubmission(
        tz_markdown=tz,
        task_type=data.get("task_type"),
        working_dir=data.get("working_dir"),
        metadata={**(data.get("metadata") or {}), "user_id": user_id},
        callback_url=data.get("callback_url"),
        timeout_seconds=int(data.get("timeout_seconds") or 3600),
    )

    try:
        handle = await backend.submit(submission)
    except ExecutorError as exc:
        logger.warning("executor.submit failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"executor unavailable: {exc}")
    except Exception as exc:
        logger.exception("executor.submit unexpected error")
        raise HTTPException(status_code=500, detail=f"submit failed: {exc}")

    await audit_log.emit(
        action="executor.submit",
        user_id=user_id,
        resource=f"task:{handle.id}",
        metadata={
            "backend": backend.name,
            "task_type": submission.task_type,
            "tz_length": len(tz),
        },
    )
    return {"handle": handle.to_dict()}


@post("/office")
async def office(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Офисная задача под приёмкой: исполнил → проверили → доработал → человек.

    Body:
        {
          "task_text": "Собери бриф по клиенту N: история, суммы, риски",
          "acceptance": [{"kind":"contains","target":"риски"},
                          {"kind":"min_len","n":400}],   # опц
          "backend": "openworker",                        # опц override
          "max_returns": 2                                # опц
        }

    Явный вызов этого роута — и есть решение человека запустить исполнителя.
    Финальное «принято» петля НЕ ставит: она отбраковывает и дорабатывает,
    результат всегда возвращается человеку с вердиктом (прошло / не доказано /
    доработки исчерпаны — с замечаниями).
    """
    user_id = _extract_user(authorization)
    task_text = (data.get("task_text") or "").strip()
    if len(task_text) < 20:
        raise HTTPException(status_code=400,
                            detail="task_text: опишите задачу подробнее "
                                   "(минимум 20 символов)")
    backend_name = ((data.get("backend") or "").strip()
                    or resolve_backend_name_for_task_type("office"))
    try:
        backend = get_backend(backend_name)
    except ExecutorError as exc:
        raise HTTPException(status_code=502,
                            detail=f"исполнитель недоступен: {exc}")

    from backend.core.executors.office_loop import run_office_task
    report = await run_office_task(
        task_text=task_text,
        acceptance=data.get("acceptance"),
        backend=backend,
        metadata={"user_id": user_id},
        max_returns=min(3, max(0, int(data.get("max_returns") or 2))),
    )
    # Прогон сохраняется и ждёт финального решения человека — иначе
    # «финал за человеком» остался бы словами: некуда нажать «принято».
    from backend.core.executors.office_store import OfficeRunStore
    run = OfficeRunStore(user_id or "default").add(
        task_text=task_text, backend=backend.name,
        report=report.to_dict())
    await audit_log.emit(
        action="executor.office",
        user_id=user_id,
        resource=f"office:{run['id']}",
        metadata={"status": report.status,
                  "backend": backend.name,
                  "attempts": len(report.attempts),
                  "task_len": len(task_text)},
    )
    return {"run": run}


@get("/office/runs")
async def office_runs(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    limit: int = Parameter(query="limit", default=50),
) -> dict[str, Any]:
    """Прогоны офисных задач: ожидающие решения и закрытые."""
    user_id = _extract_user(authorization)
    from backend.core.executors.office_store import OfficeRunStore
    runs = OfficeRunStore(user_id or "default").list(
        limit=max(1, min(200, limit)))
    return {"runs": runs, "count": len(runs)}


@post("/office/runs/{run_id:str}/close")
async def office_close(
    run_id: str,
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Финальное решение человека: принять или отклонить результат.

    Машина к этому моменту уже отбраковала и доработала что могла —
    но «принято» ставится только здесь. Закрытый прогон не переоткрывается.
    """
    user_id = _extract_user(authorization)
    from backend.core.executors.office_store import OfficeRunStore
    res = OfficeRunStore(user_id or "default").close(
        run_id, closed_by=user_id or "unknown",
        approve=bool(data.get("approve")),
        note=str(data.get("note") or ""))
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res["error"])
    await audit_log.emit(
        action="executor.office_close",
        user_id=user_id,
        resource=f"office:{run_id}",
        metadata={"approve": bool(data.get("approve"))},
    )
    return res


@post("/auto")
async def auto(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Автономный цикл: submit → validate → (refine → resubmit) (P0).

    Тело — как у /submit, плюс опц.:
        {
          "tz_markdown": "...",          # обязателен
          "task_type": "landing",
          "task_description": "...",     # исходная просьба (для validator-контекста)
          "max_iterations": 2,            # 1..5
          "auto_approve_threshold": 8.5,
          "backend": "noop"
        }

    Возвращает FeedbackLoopResult.to_dict() — converged / final_decision /
    iterations[] / final_artifacts. Каждая итерация пишется в audit_log.
    """
    user_id = _extract_user(authorization)
    tz = (data.get("tz_markdown") or "").strip()
    if not tz:
        raise HTTPException(status_code=400, detail="tz_markdown is required")

    backend_override = (data.get("backend") or "").strip() or None
    try:
        backend = get_active_backend(override=backend_override)
    except ExecutorError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        max_iter = int(data.get("max_iterations") or 2)
    except (TypeError, ValueError):
        max_iter = 2
    max_iter = max(1, min(max_iter, 5))

    try:
        auto_thr = float(data.get("auto_approve_threshold") or 8.5)
    except (TypeError, ValueError):
        auto_thr = 8.5

    from backend.core.executors.feedback_loop import run_feedback_loop
    from backend.core.validator.code import CodeValidator
    from backend.core.validator.structural import StructuralValidator

    submission = TaskSubmission(
        tz_markdown=tz,
        task_type=data.get("task_type"),
        working_dir=data.get("working_dir"),
        metadata={**(data.get("metadata") or {}), "user_id": user_id},
        callback_url=data.get("callback_url"),
        timeout_seconds=int(data.get("timeout_seconds") or 3600),
    )

    async def _audit_iteration(rec: Any) -> None:
        await audit_log.emit(
            action="executor.feedback_iteration",
            user_id=user_id,
            resource=f"task:{rec.handle_id}",
            metadata={
                "iteration": rec.iteration,
                "decision": rec.decision,
                "aggregate_score": rec.aggregate_score,
                "issue_count": rec.issue_count,
                "blocker_seen": rec.blocker_seen,
            },
        )

    try:
        loop_result = await run_feedback_loop(
            submission=submission,
            backend=backend,
            validators=[CodeValidator(), StructuralValidator()],
            task_description=data.get("task_description") or "",
            max_iterations=max_iter,
            auto_approve_threshold=auto_thr,
            on_iteration=_audit_iteration,
        )
    except Exception as exc:  # петля не должна raises, но страхуемся
        logger.exception("executor.auto: feedback loop crashed")
        raise HTTPException(status_code=500, detail=f"feedback loop failed: {exc}")

    await audit_log.emit(
        action="executor.auto",
        user_id=user_id,
        resource=f"task:{loop_result.final_handle_id or 'unknown'}",
        metadata={
            "converged": loop_result.converged,
            "final_decision": loop_result.final_decision,
            "final_score": loop_result.final_score,
            "iterations": len(loop_result.iterations),
        },
    )
    return {"result": loop_result.to_dict()}


@get("/{handle_id:str}/status")
async def status(
    handle_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    user_id = _extract_user(authorization)
    handle = await get_handle(handle_id)
    if handle is None:
        raise HTTPException(status_code=404, detail="task not found")
    if handle.user_id and handle.user_id != user_id:
        raise HTTPException(status_code=403, detail="not your task")

    try:
        backend = get_backend(handle.backend)
    except ExecutorError:
        backend = None

    current = handle.status
    if backend is not None:
        try:
            current = await backend.get_status(handle)
        except Exception as exc:
            logger.debug("executor.status: backend get_status failed: %s", exc)

    return {"handle_id": handle_id, "status": current.value}


@get("/{handle_id:str}/result")
async def result(
    handle_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    user_id = _extract_user(authorization)
    handle = await get_handle(handle_id)
    if handle is None:
        raise HTTPException(status_code=404, detail="task not found")
    if handle.user_id and handle.user_id != user_id:
        raise HTTPException(status_code=403, detail="not your task")

    raw = await get_result(handle_id)
    if raw is None:
        # Возможно executor ещё не финализировал — возвращаем pending statе.
        return {"handle_id": handle_id, "status": handle.status.value, "result": None}
    return {"handle_id": handle_id, "result": raw}


@post("/{handle_id:str}/cancel")
async def cancel(
    handle_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    user_id = _extract_user(authorization)
    handle = await get_handle(handle_id)
    if handle is None:
        raise HTTPException(status_code=404, detail="task not found")
    if handle.user_id and handle.user_id != user_id:
        raise HTTPException(status_code=403, detail="not your task")

    try:
        backend = get_backend(handle.backend)
    except ExecutorError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    ok = await backend.cancel(handle)
    if ok:
        await update_status(handle_id, TaskStatus.CANCELLED)
        await audit_log.emit(
            action="executor.cancel",
            user_id=user_id,
            resource=f"task:{handle_id}",
        )
    return {"handle_id": handle_id, "cancelled": ok}


@post("/callback")
async def callback(data: dict[str, Any]) -> dict[str, Any]:
    """Webhook для backends'ов которые посылают результат POST'ом.

    Body:
        {"handle_id": "...", "result": {...}}

    Аутентификация: пока примитивная — shared secret в header
    `X-Executor-Token` (TODO: HMAC). Нет токена → принимаем
    только если configured `EXECUTOR_CALLBACK_TOKEN` env пустой
    (в dev/staging).
    """
    handle_id = (data.get("handle_id") or "").strip()
    result_raw = data.get("result") or {}
    if not handle_id or not isinstance(result_raw, dict):
        raise HTTPException(status_code=400, detail="handle_id and result are required")

    handle = await get_handle(handle_id)
    if handle is None:
        raise HTTPException(status_code=404, detail="task not found")

    # Минимальная защита: если EXECUTOR_CALLBACK_TOKEN установлен — нужен match.
    import os
    expected = os.environ.get("EXECUTOR_CALLBACK_TOKEN", "").strip()
    # Litestar inject'ит request, но мы не используем — на этом MVP
    # просим caller класть токен в body (`auth_token` field).
    if expected:
        if str(data.get("auth_token") or "") != expected:
            raise HTTPException(status_code=401, detail="invalid callback token")

    from backend.core.executors.base import TaskResult
    try:
        status_str = str(result_raw.get("status") or "done")
        result = TaskResult(
            handle_id=handle_id,
            status=TaskStatus(status_str),
            success=bool(result_raw.get("success", True)),
            finished_at=result_raw.get("finished_at", ""),
            summary=result_raw.get("summary", ""),
            artifacts=list(result_raw.get("artifacts") or []),
            logs_excerpt=result_raw.get("logs_excerpt", ""),
            error_message=result_raw.get("error_message"),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid result: {exc}")

    await save_result(result)
    await update_status(handle_id, result.status)

    await audit_log.emit(
        action="executor.callback",
        user_id=handle.user_id or "system",
        resource=f"task:{handle_id}",
        metadata={"status": result.status.value, "success": result.success},
    )
    return {"received": True}


router = Router(
    path="/executor",
    route_handlers=[list_backends, submit, office, office_runs, office_close,
                    auto, status, result, cancel, callback],
    tags=["Executor"],
)
