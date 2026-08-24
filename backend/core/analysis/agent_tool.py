# -*- coding: utf-8 -*-
"""Agent-tool обёртка над Analysis Engine.

Позволяет агенту (Hermes / AG2 / agent_oc) запустить разбор документов
по playbook'у и получить краткий результат. Регистрируется как tool в
агентском runtime:

    from backend.core.analysis.agent_tool import run_document_analysis_tool
    # AG2:  register_function(run_document_analysis_tool, ...)
    # Hermes: добавить в tool registry

Tool возвращает компактный JSON-summary (verdict + severity + ссылку на
полный отчёт), а не весь markdown — чтобы не раздувать контекст агента.
Полный отчёт агент берёт через get-run при необходимости.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Описание tool для LLM (function-calling schema). Агентский runtime
# использует это чтобы понять когда/как вызвать.
TOOL_SCHEMA = {
    "name": "run_document_analysis",
    "description": (
        "Запустить аналитический разбор загруженных документов по методике "
        "(playbook). Используй когда нужно проанализировать документ(ы) "
        "клиента по фиксированному набору параметров (due-diligence, "
        "финансовый аудит и т.п.) и получить структурный вывод с расчётами."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "playbook_id": {"type": "string", "description": "ID методики разбора"},
            "document_ids": {
                "type": "array", "items": {"type": "string"},
                "description": "ID загруженных документов для анализа",
            },
            "client_context": {
                "type": "string",
                "description": "Опц. контекст сделки/клиента (что обсуждалось)",
            },
        },
        "required": ["playbook_id", "document_ids"],
    },
}


async def run_document_analysis_tool(
    playbook_id: str,
    document_ids: list[str],
    *,
    user_id: str,
    client_context: Optional[str] = None,
) -> str:
    """Создать и выполнить analysis run. Возвращает JSON-summary (строка).

    Args:
        playbook_id: методика разбора
        document_ids: документы (уже загруженные через /documents)
        user_id: от чьего имени (для org-scope/RBAC)
        client_context: опц. контекст клиента

    Returns: JSON-строка {ok, run_id, status, verdict, severity, error?}.
    """
    from backend.core.analysis.models import AnalysisRun, RunStatus
    from backend.core.analysis.run_engine import dispatch_run
    from backend.core.analysis.store import PlaybookStore, RunStore

    if not playbook_id or not document_ids:
        return json.dumps({"ok": False, "error": "playbook_id and document_ids required"})

    # Postgres (или mem fallback).
    pg = None
    try:
        from backend.db.postgres import get_postgres
        pg = await get_postgres()
    except Exception:
        pg = None

    pb_store = PlaybookStore(postgres=pg)
    pb = await pb_store.get(playbook_id)
    if pb is None:
        return json.dumps({"ok": False, "error": f"playbook {playbook_id} not found"})

    # org_id из membership.
    org_id = None
    try:
        from backend.core.ingest.membership import get_org_for_user
        org_id = get_org_for_user(user_id)
    except Exception:
        org_id = None

    run = AnalysisRun(
        id=AnalysisRun.new_id(),
        playbook_id=playbook_id,
        org_id=org_id,
        user_id=user_id,
        input_document_ids=[str(d) for d in document_ids],
        client_context=client_context,
        status=RunStatus.PENDING.value,
    )
    run_store = RunStore(postgres=pg)
    await run_store.create(run)

    # Выполнить петлю синхронно.
    try:
        await dispatch_run(run.id)
    except Exception as e:
        logger.warning("agent_tool dispatch failed: %s", e)
        return json.dumps({
            "ok": False, "run_id": run.id, "error": str(e)[:200],
        })

    # Перечитать финальный статус.
    final = await run_store.get(run.id)
    if final is None:
        return json.dumps({"ok": False, "run_id": run.id, "error": "run vanished"})

    report = final.final_report or {}
    severity = (report.get("severity_summary") or {}).get("overall", "")
    return json.dumps({
        "ok": final.status == RunStatus.DONE.value,
        "run_id": final.id,
        "status": final.status,
        "verdict": report.get("verdict", ""),
        "severity": severity,
        "error": final.error,
        "report_url": f"/api/v1/analysis/runs/{final.id}/report",
    }, ensure_ascii=False)
