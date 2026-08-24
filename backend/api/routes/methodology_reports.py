# -*- coding: utf-8 -*-
"""Методологии-отчёты — API для секции «Отчёты» на странице инсайтов.

Перенос из meetflow report_automation (см. core/reports/methodology_service):
консалтинговые методологии (аудит автоматизации, DNA-карта, тройной анализ,
анализ команды и др.) поверх встреч пользователя, с rolling-памятью и
динамикой «что изменилось с прошлого отчёта».

Endpoints:
- GET  /reports/methodology/types      — каталог методологий
- POST /reports/methodology/generate   — сгенерировать отчёт
- GET  /reports/methodology/           — история отчётов (без полного текста)
- GET  /reports/methodology/{id}       — полный отчёт (+ sub_reports у triple)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from litestar import Request, Router, get, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter
from pydantic import BaseModel

logger = logging.getLogger(__name__)


def _resolve_user(request: Request, user_id: Optional[str]) -> str:
    """Верифицированный токен > query (анти-спуфинг, как в knowledge_sync)."""
    try:
        from backend.core.auth.service_token import trusted_user_id
        uid, _src = trusted_user_id(request.headers, user_id or "")
    except PermissionError:
        raise HTTPException(status_code=403, detail="token/user_id mismatch")
    except Exception:
        uid = user_id or ""
    if not uid:
        raise HTTPException(status_code=401, detail="user_id required")
    return uid


class GenerateReportRequest(BaseModel):
    report_type: str
    user_id: Optional[str] = None
    project_id: Optional[str] = None
    folder_id: Optional[str] = None
    days_back: int = 30
    custom_prompt: Optional[str] = None
    dynamic: bool = True
    model_tier: str = "standard"


@get("/types")
async def list_methodologies() -> Dict[str, Any]:
    from backend.core.reports.methodology_service import METHODOLOGIES
    return {"types": [
        {"id": k, **v} for k, v in METHODOLOGIES.items()
    ]}


@post("/generate")
async def generate_report(data: GenerateReportRequest, request: Request) -> Dict[str, Any]:
    uid = _resolve_user(request, data.user_id)
    from backend.core.llm.lang import resolve_answer_lang
    from backend.core.reports.methodology_service import (
        METHODOLOGIES,
        generate_methodology_report,
    )
    if data.report_type not in METHODOLOGIES:
        raise HTTPException(status_code=400,
                            detail=f"unknown report_type: {data.report_type}")
    try:
        result = await generate_methodology_report(
            uid, data.report_type,
            project_id=data.project_id, folder_id=data.folder_id,
            days_back=max(1, min(data.days_back, 365)),
            custom_prompt=data.custom_prompt,
            dynamic=data.dynamic,
            model_tier=data.model_tier,
            # отчёт читает тот, кто его заказал — язык берём из запроса
            lang=await resolve_answer_lang(uid),
        )
        # полный текст в ответе генерации нужен сразу (UI показывает отчёт)
        return result
    except Exception as e:
        logger.error(f"methodology report failed: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


@get("/")
async def list_reports(
    request: Request,
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
    report_type: Optional[str] = Parameter(query="report_type", default=None,
                                           required=False),
    limit: int = Parameter(query="limit", default=50, required=False),
) -> Dict[str, Any]:
    uid = _resolve_user(request, user_id)
    from backend.core.reports.methodology_service import report_store_for_user
    store = report_store_for_user(uid)
    reports = store.list_reports(report_type=report_type,
                                 limit=max(1, min(limit, 200)))
    return {"reports": reports, "count": len(reports)}


@get("/{report_id:str}")
async def get_report(
    report_id: str,
    request: Request,
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    uid = _resolve_user(request, user_id)
    from backend.core.reports.methodology_service import report_store_for_user
    report = report_store_for_user(uid).get_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="report not found")
    return {"report": report}


router = Router(
    path="/reports/methodology",
    route_handlers=[list_methodologies, generate_report, list_reports, get_report],
    tags=["Methodology Reports"],
)
