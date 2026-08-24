# -*- coding: utf-8 -*-
"""Analysis Engine API (Фаза A — CRUD + run lifecycle).

Endpoints (mounted под /api/v1):
    POST   /analysis/playbooks                — создать методику разбора
    GET    /analysis/playbooks                — список (org-scoped)
    GET    /analysis/playbooks/{id}           — одна методика
    PATCH  /analysis/playbooks/{id}           — обновить
    DELETE /analysis/playbooks/{id}           — удалить

    POST   /analysis/runs                      — запустить разбор (playbook + docs)
    GET    /analysis/runs                      — список прогонов (org-scoped)
    GET    /analysis/runs/{id}                 — статус + результат прогона

Фаза A — CRUD + создание run в статусе pending. Сам orchestrator петли
(parse→extract→compute→analyze→assemble) подключается в фазах B–D
(run_engine). Здесь run создаётся и опционально диспатчится в движок,
если он доступен.

Auth: любой authenticated user. Org берётся из membership (Wave 2.2).
RBAC по access_level (P1 #5) — playbook видят те, чей max_level ≥
playbook.access_level.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import jwt
from litestar import Router, delete, get, patch, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter

from backend.core.analysis.models import AnalysisPlaybook, AnalysisRun, RunStatus
from backend.core.analysis.store import PlaybookStore, RunStore

logger = logging.getLogger(__name__)


# === Auth + helpers ========================================================


def _caller(authorization: Optional[str]) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")

    # ВЕРИФИЦИРОВАННЫЙ путь (как documents.py/chat): проверяем ПОДПИСЬ токена.
    # Раньше здесь был jwt.decode(verify_signature=False) — любой валидный по
    # форме токен с произвольным sub проходил, org/RBAC строились на
    # недоверенном sub (auth-дыра). Теперь: если JWT-секрет настроен —
    # подпись проверяется; если нет — деградируем в тот же compat-режим, что
    # и весь остальной апп (не хуже, чем было, но дыра закрыта в проде).
    try:
        from backend.core.auth.service_token import trusted_user_id
        uid, src = trusted_user_id({"authorization": authorization}, "")
        if src != "unverified" and uid:
            return str(uid)
    except PermissionError:
        raise HTTPException(status_code=401, detail="token not authorized")
    except Exception:
        logger.debug("trusted_user_id failed, compat decode", exc_info=True)

    # Compat-fallback (JWT-секрет не настроен / чужая подпись): sub без
    # проверки подписи — та же поза, что trusted_user_id в non-strict.
    token = authorization.split(" ", 1)[1]
    try:
        claims = jwt.decode(token, options={"verify_signature": False}) or {}
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")
    uid = claims.get("sub") or claims.get("user_id")
    if not uid:
        raise HTTPException(status_code=401, detail="Token missing sub/user_id")
    return str(uid)


def _org_for(user_id: str) -> Optional[str]:
    try:
        from backend.core.ingest.membership import get_org_for_user
        return get_org_for_user(user_id)
    except Exception:
        return None


def _max_level_for(user_id: str) -> int:
    try:
        from backend.core.access.levels import max_access_level_for_user
        return max_access_level_for_user(user_id)
    except Exception:
        return 2  # INTERNAL default


async def _get_postgres() -> Any:
    try:
        from backend.db.postgres import get_postgres
        return await get_postgres()
    except Exception:
        return None  # in-memory fallback в store


async def _playbook_store() -> PlaybookStore:
    return PlaybookStore(postgres=await _get_postgres())


async def _run_store() -> RunStore:
    return RunStore(postgres=await _get_postgres())


def _can_see(pb: AnalysisPlaybook, user_id: str, org_id: Optional[str], max_level: int) -> bool:
    """Owner или (одна org + access_level ≤ max_level)."""
    if pb.created_by == user_id:
        return True
    if pb.org_id != org_id:
        return False
    return pb.access_level <= max_level


# === Playbook CRUD =========================================================


@post("/analysis/playbooks", status_code=201)
async def create_playbook(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Создать методику разбора. Body — поля AnalysisPlaybook
    (name обязателен; dimensions/company_context/output_template опц.)."""
    user_id = _caller(authorization)
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")

    org_id = _org_for(user_id)
    pb = AnalysisPlaybook.from_dict({**data, "org_id": org_id, "created_by": user_id})
    pb.id = AnalysisPlaybook.new_id()  # игнорируем клиентский id

    # Валидация: битая формула / дубль key → 400 с деталями.
    from backend.core.analysis.validation import validate_playbook
    errors, warnings = validate_playbook(pb)
    if errors:
        raise HTTPException(status_code=400, detail={"validation_errors": errors})

    store = await _playbook_store()
    saved = await store.create(pb)
    out = saved.to_dict()
    if warnings:
        out["validation_warnings"] = warnings
    return out


# === Bundled templates (методики из коробки) ===============================


@get("/analysis/templates", status_code=200)
async def list_playbook_templates(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Готовые методики (DD, финразбор, оценка КП). Сотрудник берёт шаблон
    и кастомизирует, а не настраивает с нуля."""
    _caller(authorization)
    from backend.core.analysis.bundled import list_templates
    return {"templates": list_templates()}


@post("/analysis/playbooks/from-template", status_code=201)
async def create_playbook_from_template(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Создать playbook из готового шаблона. Body: {template_id, name?,
    overrides?}. overrides сливаются поверх шаблона (можно переопределить
    любое поле)."""
    user_id = _caller(authorization)
    template_id = (data.get("template_id") or "").strip()
    if not template_id:
        raise HTTPException(status_code=400, detail="template_id required")

    from backend.core.analysis.bundled import get_template
    tpl = get_template(template_id)
    if tpl is None:
        raise HTTPException(status_code=404, detail=f"template '{template_id}' not found")

    # overrides поверх шаблона (имя/dimensions/...).
    overrides = data.get("overrides") or {}
    merged = {**tpl, **overrides}
    if data.get("name"):
        merged["name"] = data["name"]

    org_id = _org_for(user_id)
    pb = AnalysisPlaybook.from_dict({**merged, "org_id": org_id, "created_by": user_id})
    pb.id = AnalysisPlaybook.new_id()

    from backend.core.analysis.validation import validate_playbook
    errors, _warnings = validate_playbook(pb)
    if errors:
        raise HTTPException(status_code=400, detail={"validation_errors": errors})

    store = await _playbook_store()
    saved = await store.create(pb)
    out = saved.to_dict()
    out["created_from_template"] = template_id
    return out


@post("/analysis/playbooks/from-document", status_code=201)
async def create_playbook_from_document(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Загрузить методологию документом → система сама соберёт методику.

    Body: {text?, document_id?, name?}. Даёшь методологию текстом (`text`) или
    ссылкой на уже загруженный документ (`document_id`) — LLM раскладывает её на
    параметры разбора (dimensions), контекст и структуру отчёта, мы валидируем и
    сохраняем как обычный playbook. Не распозналось → черновик (редактируется).
    """
    user_id = _caller(authorization)
    org_id = _org_for(user_id)

    text = (data.get("text") or "").strip()
    document_id = (data.get("document_id") or "").strip()
    if not text and document_id:
        try:
            from backend.core.analysis.run_engine import _default_document_loader
            doc = await _default_document_loader(document_id,
                                                 owner_user_id=user_id)
            text = str((doc or {}).get("content") or "").strip()
        except Exception as exc:
            logger.warning("from-document loader failed: %s", exc)
        if not text:
            raise HTTPException(status_code=404,
                                detail=f"document '{document_id}' not found or empty")
    if not text:
        raise HTTPException(status_code=400, detail="text or document_id required")

    from backend.core.analysis.methodology_import import playbook_from_methodology
    pb, warnings = await playbook_from_methodology(
        text, name=(data.get("name") or None), created_by=user_id,
        org_id=org_id, user_id=user_id)

    store = await _playbook_store()
    saved = await store.create(pb)
    out = saved.to_dict()
    out["parsed_from"] = "document" if document_id else "text"
    if warnings:
        out["import_warnings"] = warnings
    return out


@get("/analysis/playbooks", status_code=200)
async def list_playbooks(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    user_id = _caller(authorization)
    org_id = _org_for(user_id)
    max_level = _max_level_for(user_id)
    store = await _playbook_store()
    all_pb = await store.list_for_org(org_id)
    visible = [p for p in all_pb if _can_see(p, user_id, org_id, max_level)]
    return {"playbooks": [p.to_dict() for p in visible], "count": len(visible)}


@get("/analysis/playbooks/{playbook_id:str}", status_code=200)
async def get_playbook(
    playbook_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    user_id = _caller(authorization)
    org_id = _org_for(user_id)
    max_level = _max_level_for(user_id)
    store = await _playbook_store()
    pb = await store.get(playbook_id)
    if pb is None:
        raise HTTPException(status_code=404, detail="playbook not found")
    if not _can_see(pb, user_id, org_id, max_level):
        raise HTTPException(status_code=403, detail="not allowed to view this playbook")
    return pb.to_dict()


@patch("/analysis/playbooks/{playbook_id:str}", status_code=200)
async def update_playbook(
    playbook_id: str,
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    user_id = _caller(authorization)
    org_id = _org_for(user_id)
    max_level = _max_level_for(user_id)
    store = await _playbook_store()
    pb = await store.get(playbook_id)
    if pb is None:
        raise HTTPException(status_code=404, detail="playbook not found")
    if not _can_see(pb, user_id, org_id, max_level):
        raise HTTPException(status_code=403, detail="not allowed to edit this playbook")

    # Merge разрешённых полей (id/org_id/created_by/created_at не меняем).
    merged = pb.to_dict()
    for fld in ("name", "analysis_type", "description", "dimensions",
                "company_context", "reference_report", "output_template",
                "access_level"):
        if fld in data:
            merged[fld] = data[fld]
    updated = AnalysisPlaybook.from_dict(merged)
    updated.id = pb.id
    updated.org_id = pb.org_id
    updated.created_by = pb.created_by
    updated.created_at = pb.created_at
    updated.version = pb.version  # store.update сам инкрементит

    from backend.core.analysis.validation import validate_playbook
    errors, warnings = validate_playbook(updated)
    if errors:
        raise HTTPException(status_code=400, detail={"validation_errors": errors})

    saved = await store.update(updated)
    out = saved.to_dict()
    if warnings:
        out["validation_warnings"] = warnings
    return out


@delete("/analysis/playbooks/{playbook_id:str}", status_code=200)
async def delete_playbook(
    playbook_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    user_id = _caller(authorization)
    org_id = _org_for(user_id)
    max_level = _max_level_for(user_id)
    store = await _playbook_store()
    pb = await store.get(playbook_id)
    if pb is None:
        raise HTTPException(status_code=404, detail="playbook not found")
    if not _can_see(pb, user_id, org_id, max_level):
        raise HTTPException(status_code=403, detail="not allowed to delete this playbook")
    ok = await store.delete(playbook_id)
    return {"deleted": ok, "playbook_id": playbook_id}


# === Run lifecycle =========================================================


@post("/analysis/runs", status_code=201)
async def create_run(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    wait: bool = Parameter(query="wait", default=False),
) -> dict[str, Any]:
    """Запустить разбор. Body: {playbook_id, document_ids[], client_context?}.

    По умолчанию (wait=false) run запускается В ФОНЕ — endpoint сразу
    возвращает pending, клиент поллит GET /analysis/runs/{id} до status=done.
    Это важно для 100-страничных документов (разбор = минуты, HTTP не ждёт).

    wait=true → синхронно (для малых документов / отладки); endpoint
    вернёт уже завершённый run.
    """
    user_id = _caller(authorization)
    org_id = _org_for(user_id)
    max_level = _max_level_for(user_id)

    playbook_id = (data.get("playbook_id") or "").strip()
    doc_ids = data.get("document_ids") or data.get("input_document_ids") or []
    if not playbook_id:
        raise HTTPException(status_code=400, detail="playbook_id required")
    if not isinstance(doc_ids, list) or not doc_ids:
        raise HTTPException(status_code=400, detail="document_ids (non-empty list) required")

    pb_store = await _playbook_store()
    pb = await pb_store.get(playbook_id)
    if pb is None:
        raise HTTPException(status_code=404, detail="playbook not found")
    if not _can_see(pb, user_id, org_id, max_level):
        raise HTTPException(status_code=403, detail="not allowed to run this playbook")

    run = AnalysisRun(
        id=AnalysisRun.new_id(),
        playbook_id=playbook_id,
        playbook_version=pb.version,  # фиксируем версию методики на момент запуска
        org_id=org_id,
        user_id=user_id,
        input_document_ids=[str(d) for d in doc_ids],
        client_context=data.get("client_context"),
        status=RunStatus.PENDING.value,
    )
    run.add_step("created", "run queued", {"documents": len(run.input_document_ids)})
    run_store = await _run_store()
    saved = await run_store.create(run)

    # Dispatch в движок. wait=true → синхронно; иначе → фон.
    dispatched = False
    channel = None
    if wait:
        # Синхронно — endpoint вернёт завершённый run (малые документы/тесты).
        try:
            from backend.core.analysis.run_engine import dispatch_run
            await dispatch_run(saved.id)
            dispatched = True
        except Exception as exc:
            logger.debug("run_engine sync dispatch failed (run pending): %s", exc)
        # Перечитать финальное состояние.
        refreshed = await run_store.get(saved.id)
        out = (refreshed or saved).to_dict()
        out["dispatched"] = dispatched
        out["mode"] = "sync"
        return out

    # Фон (default): не блокируем HTTP на минуты.
    try:
        from backend.core.analysis.run_engine import dispatch_run_background
        channel = await dispatch_run_background(saved.id)
        dispatched = True
    except Exception as exc:
        logger.debug("run_engine bg dispatch failed (run pending): %s", exc)

    out = saved.to_dict()
    out["dispatched"] = dispatched
    out["mode"] = "background"
    out["dispatch_channel"] = channel  # "taskiq" | "asyncio" | None
    return out


@post("/analysis/runs/freeform", status_code=201)
async def create_freeform_run(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    wait: bool = Parameter(query="wait", default=False),
) -> dict[str, Any]:
    """Свободный разбор БЕЗ готовой методики (клиент сам говорит, что анализировать).

    Body: {document_ids[], request}. `request` — что именно нужно проанализировать
    и что важно клиенту (идёт в client_context, выводы приоритезируются под него).
    Создаёт exploratory-методику на лету и запускает разбор одним вызовом.
    """
    user_id = _caller(authorization)
    org_id = _org_for(user_id)
    doc_ids = data.get("document_ids") or data.get("input_document_ids") or []
    request_text = (data.get("request") or data.get("query") or "").strip()
    if not isinstance(doc_ids, list) or not doc_ids:
        raise HTTPException(status_code=400, detail="document_ids (non-empty list) required")

    from backend.core.analysis.bundled import get_template
    tpl = get_template("exploratory_generic")
    if tpl is None:
        raise HTTPException(status_code=500, detail="exploratory template missing")
    pb = AnalysisPlaybook.from_dict({**tpl, "org_id": org_id, "created_by": user_id})
    pb.id = AnalysisPlaybook.new_id()
    pb_store = await _playbook_store()
    pb = await pb_store.create(pb)

    tier = str(data.get("model_tier") or "standard").lower()
    if tier not in ("standard", "premium"):
        tier = "standard"
    run = AnalysisRun(
        id=AnalysisRun.new_id(),
        playbook_id=pb.id,
        playbook_version=pb.version,
        org_id=org_id, user_id=user_id,
        input_document_ids=[str(d) for d in doc_ids],
        client_context=request_text or None,
        model_tier=tier,
        status=RunStatus.PENDING.value,
    )
    run.add_step("created", "freeform run queued",
                 {"documents": len(run.input_document_ids),
                  "model_tier": tier})
    run_store = await _run_store()
    saved = await run_store.create(run)

    dispatched = False
    if wait:
        try:
            from backend.core.analysis.run_engine import dispatch_run
            await dispatch_run(saved.id)
            dispatched = True
        except Exception as exc:
            logger.debug("freeform sync dispatch failed: %s", exc)
        refreshed = await run_store.get(saved.id)
        out = (refreshed or saved).to_dict()
        out["dispatched"] = dispatched
        out["mode"] = "sync"
        return out
    channel = None
    try:
        from backend.core.analysis.run_engine import dispatch_run_background
        channel = await dispatch_run_background(saved.id)
        dispatched = True
    except Exception as exc:
        logger.debug("freeform bg dispatch failed: %s", exc)
    out = saved.to_dict()
    out["dispatched"] = dispatched
    out["mode"] = "background"
    out["dispatch_channel"] = channel
    return out


@get("/analysis/runs", status_code=200)
async def list_runs(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    limit: int = Parameter(query="limit", default=50),
) -> dict[str, Any]:
    user_id = _caller(authorization)
    org_id = _org_for(user_id)
    store = await _run_store()
    runs = await store.list_for_org(org_id, limit=max(1, min(int(limit), 200)))
    # Краткая форма (без тяжёлых facts/report) для списка.
    summary = [{
        "id": r.id, "playbook_id": r.playbook_id, "status": r.status,
        "documents": len(r.input_document_ids),
        "created_at": r.created_at, "completed_at": r.completed_at,
    } for r in runs]
    return {"runs": summary, "count": len(summary)}


@get("/analysis/runs/{run_id:str}", status_code=200)
async def get_run(
    run_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    user_id = _caller(authorization)
    org_id = _org_for(user_id)
    store = await _run_store()
    run = await store.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    # Org-scope: run виден членам той же org или его автору.
    if run.org_id != org_id and run.user_id != user_id:
        raise HTTPException(status_code=403, detail="not allowed to view this run")
    return run.to_dict()


@get("/analysis/runs/{run_id:str}/report", status_code=200)
async def get_run_report(
    run_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    fmt: str = Parameter(query="format", default="markdown"),
) -> Any:
    """Финальный отчёт прогона. format=markdown|json|docx.

    markdown → {format, markdown, verdict, severity_summary}
    json     → полный final_report объект
    docx     → .docx файл (Word) на скачивание
    """
    user_id = _caller(authorization)
    org_id = _org_for(user_id)
    store = await _run_store()
    run = await store.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run.org_id != org_id and run.user_id != user_id:
        raise HTTPException(status_code=403, detail="not allowed to view this run")
    if run.user_id != user_id:
        # аудит чтения чужого (орг-шаринг) отчёта — след для compliance
        from backend.core.observability import audit_log
        await audit_log.emit(action="analysis.report.read", user_id=user_id,
                             resource=f"analysis_run:{run_id}",
                             metadata={"owner_user_id": run.user_id})
    if not run.final_report:
        raise HTTPException(
            status_code=409,
            detail=f"report not ready (run status: {run.status})",
        )
    if fmt == "json":
        return {"format": "json", "report": run.final_report}
    if fmt == "docx":
        from litestar.response import Response

        from backend.core.analysis.export import report_to_docx_bytes
        # Имя playbook'а для заголовка docx (best-effort).
        pb_name = ""
        try:
            pb_store = await _playbook_store()
            pb = await pb_store.get(run.playbook_id)
            pb_name = pb.name if pb else ""
        except Exception:
            pb_name = ""
        try:
            data = report_to_docx_bytes(run.final_report, playbook_name=pb_name)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"docx export failed: {str(e)[:160]}")
        fname = f"analysis_{run_id}.docx"
        return Response(
            content=data,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    return {
        "format": "markdown",
        "markdown": run.final_report.get("markdown", ""),
        "verdict": run.final_report.get("verdict", ""),
        "severity_summary": run.final_report.get("severity_summary", {}),
        "generated_by": run.final_report.get("generated_by", ""),
    }


@post("/analysis/methodology/induce", status_code=200)
async def induce_methodology(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """МОРМ-lite: вывести методологию компании из загруженных примеров.

    Body: {document_ids: [...], genre: "медиапланы|посты|стратегии|...",
    title?: "..."} — документы из общего стора (вкладка Анализ / 📎).
    Числовые правила считает код (якоря/связки + held-out гейт против
    базлайна), структурные извлекает LLM и гейтит на отложенных документах.
    Результат сохраняется в Документы (папка «Методологии») и заготовкой.
    """
    user_id = _caller(authorization)
    doc_ids = data.get("document_ids") or []
    if not isinstance(doc_ids, list) or len(doc_ids) < 2:
        raise HTTPException(status_code=400,
                            detail="document_ids: минимум 2 документа")
    from backend.core.analysis.methodology_induction import build_methodology
    try:
        return await build_methodology(
            user_id,
            document_ids=[str(d) for d in doc_ids],
            genre=str(data.get("genre") or "документы"),
            title=str(data.get("title") or ""))
    except Exception as e:
        logger.exception("methodology induction failed")
        return {"success": False, "error": f"индукция не отработала: {e}"}


@post("/analysis/methodology/regate", status_code=200)
async def regate_methodology_route(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Пере-гейт методологии на свежих примерах (дрейф цен/процессов).

    Body: {methodology_document_id, document_ids: [...]}. Правила
    перепроверяются предсказанием; дрейфанувшие помечаются 🔴 (не
    стираются), trust и версия документа обновляются.
    """
    user_id = _caller(authorization)
    mid = str(data.get("methodology_document_id") or "").strip()
    doc_ids = data.get("document_ids") or []
    if not mid or not isinstance(doc_ids, list) or not doc_ids:
        raise HTTPException(
            status_code=400,
            detail="methodology_document_id и document_ids обязательны")
    from backend.core.analysis.methodology_induction import regate_methodology
    try:
        return await regate_methodology(
            user_id, methodology_document_id=mid,
            document_ids=[str(d) for d in doc_ids])
    except Exception as e:
        logger.exception("methodology regate failed")
        return {"success": False, "error": f"пере-гейт не отработал: {e}"}


router = Router(
    path="",
    route_handlers=[
        create_playbook, list_playbooks, get_playbook,
        update_playbook, delete_playbook,
        list_playbook_templates, create_playbook_from_template,
        create_playbook_from_document,
        create_run, create_freeform_run, list_runs, get_run, get_run_report,
        induce_methodology, regate_methodology_route,
    ],
    tags=["Analysis"],
)
