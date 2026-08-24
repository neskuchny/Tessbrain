# -*- coding: utf-8 -*-
"""Task Analysis API — анализ выполненных/невыполненных задач и делегация
доработки кодинг-агентам (Claude Code / Cursor / Codex).

Endpoints:
- GET  /task-analysis/            — сводка done/open/blocked/по владельцам
- POST /task-analysis/handoff     — сгенерировать ТЗ + команду делегации
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

from litestar import Request, Router, get, post
from litestar.exceptions import HTTPException
from litestar.params import Body, Parameter
from pydantic import BaseModel

logger = logging.getLogger(__name__)


def _resolve_user(request: Request, user_id: Optional[str]) -> str:
    try:
        from backend.core.auth.service_token import trusted_user_id
        uid, _ = trusted_user_id(request.headers, user_id or "")
    except PermissionError:
        raise HTTPException(status_code=403, detail="token/user_id mismatch")
    except Exception:
        uid = user_id or ""
    if not uid:
        raise HTTPException(status_code=401, detail="user_id required")
    return uid


class HandoffRequest(BaseModel):
    user_id: Optional[str] = None
    task_id: Optional[str] = None       # id задачи из графа
    task: Optional[Dict[str, Any]] = None  # либо явная задача {title, description}
    agent: str = "claude"               # claude | cursor | codex
    repo_path: Optional[str] = None
    # Режим «собери артефакт кодом» БЕЗ git-репо: агент строит файл-результат
    # (презентация/КП/таблица) в изолированной scratch-папке. За флагом
    # enable_handoff_artifact_mode; исполнение — по подтверждению, как обычно.
    artifact_mode: bool = False
    # Готовое ТЗ (например, из meeting-pipeline / SIMA generatedTZ). Если
    # передано — генерация ТЗ пропускается, текст уходит в гейт как есть.
    spec_text: Optional[str] = None
    # Происхождение задачи для отображения в очереди (аддитивно): напр.
    # {"kind": "meeting", "meeting_id": "...", "meeting_title": "..."} или
    # {"kind": "manual"}. Ничего не исполняет — только метка «откуда пришло».
    source: Optional[Dict[str, Any]] = None


class TaskActionRequest(BaseModel):
    """Действие над задачей в задачнике (недеструктивно)."""
    user_id: Optional[str] = None
    system: str                          # yougile | trello | jira
    task_id: str
    action: str                          # comment | attach_result | close | update
    text: Optional[str] = None           # для comment/attach_result/close(result)
    fields: Optional[Dict[str, Any]] = None  # для update
    target_column_id: Optional[str] = None   # для close (yougile/trello)
    transition_name: Optional[str] = None    # для close (jira)
    author: Optional[str] = None             # для attach_result


@get("/")
async def analyze(
    request: Request,
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    uid = _resolve_user(request, user_id)
    from backend.core.tasks.task_analysis import analyze_tasks
    return await analyze_tasks(uid)


@post("/handoff")
async def handoff(data: HandoffRequest, request: Request) -> Dict[str, Any]:
    """ФАЗА 1: ТЗ по задаче + команда + PENDING-хэндофф (НИЧЕГО не исполняет).

    Запуск агента — ТОЛЬКО после явного подтверждения пользователя:
    POST /task-analysis/handoff/{id}/confirm (плюс ops-флаг
    enable_coding_handoff_exec). Отклонить: /handoff/{id}/reject."""
    uid = _resolve_user(request, data.user_id)
    from backend.core.tasks.task_analysis import (
        analyze_tasks,
        coding_handoff,
    )
    task = data.task
    if not task and data.task_id:
        summary = await analyze_tasks(uid)
        task = next((t for t in summary["tasks"]
                     if str(t.get("id")) == str(data.task_id)), None)
        if not task:
            raise HTTPException(status_code=404, detail="task not found")
    # С готовым ТЗ (spec_text) задача может быть минимальной — достаточно
    # заголовка. Разрешаем handoff без task_id, если пришёл spec_text.
    if not task and data.spec_text:
        task = {"title": "Готовое ТЗ", "description": ""}
    if not task:
        raise HTTPException(status_code=400,
                            detail="нужен task_id, task {title, description} или spec_text")
    try:
        result = await coding_handoff(uid, task, agent=data.agent,
                                      repo_path=data.repo_path,
                                      spec_text=data.spec_text,
                                      artifact_mode=bool(data.artifact_mode),
                                      source=data.source)
        return {"status": "success", **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"coding_handoff failed: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


@post("/handoff/{handoff_id:str}/confirm")
async def handoff_confirm(
    handoff_id: str,
    data: Dict[str, Any],
    request: Request,
) -> Dict[str, Any]:
    """ФАЗА 2: явное подтверждение ПОЛЬЗОВАТЕЛЯ → реальный запуск агента.

    Body: {user_id?, repo_path?}. Двойной confirm блокируется атомарным
    переходом статуса; без enable_coding_handoff_exec — понятный отказ с
    командой для ручного запуска."""
    uid = _resolve_user(request, (data or {}).get("user_id"))
    from backend.core.tasks.task_analysis import confirm_handoff
    try:
        # background=True: не держим HTTP-запрос до конца прогона (`claude -p`
        # может идти минуты) — исполнитель уходит в фон, ответ возвращается
        # сразу со статусом running; завершение придёт WS-сигналом.
        return await confirm_handoff(uid, handoff_id,
                                     repo_path=(data or {}).get("repo_path"),
                                     background=True)
    except Exception as e:
        logger.error(f"handoff confirm failed: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


@get("/handoff/{handoff_id:str}/artifact/{index:int}")
async def handoff_artifact(
    handoff_id: str,
    index: int,
    request: Request,
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Any:
    """Скачать файл-результат (artifact_mode) по индексу из собранного списка.

    Изоляция: HandoffStore per-user (чужую запись не отдаст). Путь строго
    внутри scratch work_dir записи (защита от ../ обхода)."""
    import os

    from litestar.response import File

    uid = _resolve_user(request, user_id)
    from backend.core.tasks.handoff_store import HandoffStore
    rec = HandoffStore(uid).get(handoff_id)
    if not rec:
        raise HTTPException(status_code=404, detail="handoff not found")
    work_dir = rec.get("work_dir")
    arts = rec.get("artifacts") or []
    if not work_dir or not os.path.isdir(work_dir):
        raise HTTPException(status_code=404, detail="scratch dir not found")
    if index < 0 or index >= len(arts):
        raise HTTPException(status_code=404, detail="artifact index out of range")
    rel = str(arts[index].get("rel") or "")
    root = os.path.realpath(work_dir)
    fp = os.path.realpath(os.path.join(root, rel))
    if fp != root and not fp.startswith(root + os.sep):
        raise HTTPException(status_code=400, detail="path escapes work dir")
    if not os.path.isfile(fp):
        raise HTTPException(status_code=404, detail="artifact file missing")
    return File(path=fp, filename=os.path.basename(fp), content_disposition_type="attachment")


@get("/handoff/{handoff_id:str}/bundle")
async def handoff_bundle(
    handoff_id: str,
    request: Request,
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Any:
    """Скачать РЕЗУЛЬТАТ прогона целиком — zip рабочей папки.

    Последняя миля сценария «сотрудник получает мини-сервис»: раньше код
    оставался папкой на сервере, и забрать его человек без доступа к
    серверу не мог никак. Отдаём zip; служебное (git, зависимости,
    kanon-атлас) не пакуем — человеку нужен продукт, а не обвязка.

    Изоляция: HandoffStore per-user; только завершённые прогоны (иначе
    отдали бы наполовину написанный код как готовый).
    """
    import io
    import os
    import zipfile

    from litestar.response import Response

    uid = _resolve_user(request, user_id)
    from backend.core.tasks.handoff_store import DONE, HandoffStore
    rec = HandoffStore(uid).get(handoff_id)
    if not rec:
        raise HTTPException(status_code=404, detail="handoff not found")
    if rec.get("status") != DONE:
        raise HTTPException(
            status_code=409,
            detail="прогон не завершён — отдавать наполовину написанный "
                   "код как готовый нельзя")
    folder = rec.get("repo_path") or rec.get("work_dir")
    if not folder or not os.path.isdir(folder):
        raise HTTPException(status_code=404, detail="папка результата не найдена")

    _SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
                  "atlas", "dist", ".next"}
    _MAX_BYTES = 50 * 1024 * 1024   # общий кап архива
    _MAX_FILE = 10 * 1024 * 1024    # кап на файл

    root = os.path.realpath(folder)
    buf = io.BytesIO()
    total = 0
    skipped_big = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fn in sorted(filenames):
                fp = os.path.realpath(os.path.join(dirpath, fn))
                # симлинк наружу не пакуем — защита от выноса чужих файлов
                if not fp.startswith(root + os.sep) and fp != root:
                    continue
                try:
                    size = os.path.getsize(fp)
                except OSError:
                    continue
                if size > _MAX_FILE:
                    skipped_big += 1
                    continue
                if total + size > _MAX_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="результат больше 50 МБ — заберите папку "
                               "с сервера напрямую")
                total += size
                zf.write(fp, os.path.relpath(fp, root))
        if skipped_big:
            zf.writestr(
                "_SKIPPED.txt",
                f"Пропущено файлов больше 10 МБ: {skipped_big}. "
                f"Они остались в папке на сервере: {root}",
            )
    buf.seek(0)
    name = f"{os.path.basename(root) or 'result'}.zip"
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@get("/handoff/{handoff_id:str}/render/{fmt:str}")
async def handoff_render(
    handoff_id: str,
    fmt: str,
    request: Request,
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Any:
    """Отдать результат хэндоффа файлом в нужном формате: pdf|docx|xlsx|pptx.

    Рендерит result_document (готовый документ) — или, если его нет, spec_text
    (ТЗ) — тем же модулем документов, что и Доска (реальные PDF/DOCX/xlsx/pptx,
    без заглушек). Хранилище per-user → чужой хэндофф недоступен."""
    from litestar.response import Response

    uid = _resolve_user(request, user_id)
    from backend.core.tasks.handoff_store import HandoffStore
    rec = HandoffStore(uid).get(handoff_id)
    if not rec:
        raise HTTPException(status_code=404, detail="handoff not found")
    md = str(rec.get("result_document") or rec.get("spec_text") or "").strip()
    if not md:
        raise HTTPException(status_code=404, detail="нет документа для рендера")
    title = str(rec.get("task_title") or "Документ")[:110]

    fmt = (fmt or "").lower().strip()
    from backend.core.analysis import export as _exp
    _MIME = {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "html": "text/html; charset=utf-8",
    }
    if fmt not in _MIME:
        raise HTTPException(status_code=400, detail="формат: pdf|docx|xlsx|pptx|html")
    try:
        if fmt == "pdf":
            blob = _exp.markdown_to_pdf_bytes(md, title=title)
        elif fmt == "docx":
            blob = _exp.markdown_to_docx_bytes(md, title=title)
        elif fmt == "xlsx":
            blob = _exp.markdown_to_xlsx_bytes(md, title=title)
        elif fmt == "html":
            blob = _exp.markdown_to_html_bytes(md, title=title)
        else:
            blob = _exp.markdown_to_pptx_bytes(md, title=title)
    except Exception as e:
        logger.error("handoff render %s failed: %s", fmt, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"render error: {e}")
    if not blob:
        raise HTTPException(status_code=503,
                            detail=f"{fmt.upper()} недоступен на сервере (нет библиотеки/движка)")
    # content-disposition: заголовки HTTP только ASCII. `\w` в Python юникод-
    # aware и оставлял кириллицу → битый заголовок. Даём ASCII-fallback
    # (filename="...") + RFC 6266 filename* с percent-encoded UTF-8 именем.
    import urllib.parse
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", title).strip("_") or "document"
    utf8_name = urllib.parse.quote(f"{title}.{fmt}")
    return Response(
        content=blob, media_type=_MIME[fmt],
        headers={"content-disposition":
                 f"attachment; filename=\"{ascii_name}.{fmt}\"; filename*=UTF-8''{utf8_name}"})


@post("/dispatch")
async def dispatch_tasks(data: Dict[str, Any], request: Request) -> Dict[str, Any]:
    """ОДИН вызов для внешней системы (Minitest): выбранные задачи → ТЗ →
    исполнение. Явный выбор человека в Minitest = гейт подтверждения.

    Body: {user_id?, items: [{task: {title, description?, assignee?,
    tracker?, tracker_task_id?}, mode: "document"|"code"|"skip",
    agent?: "claude"|..., repo_path?: str, artifact_mode?: bool},...],
    source?: {kind:"meeting", meeting_id, meeting_title}}.

    mode=document → ТЗ + генерация готового документа (LLM, без CLI);
    mode=code     → ТЗ + запуск кодинг-агента в фоне (repo или artifact);
    mode=skip     → пропустить (фиксируется в ответе).
    Ответ сразу (исполнение в фоне); завершение — webhook
    TESSENT_HANDOFF_WEBHOOK_URL и/или поллинг GET /handoff/{id}."""
    import asyncio as _aio

    uid = _resolve_user(request, (data or {}).get("user_id"))
    items = (data or {}).get("items") or []
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail="items: непустой список")
    src = (data or {}).get("source")
    from backend.core.tasks.task_analysis import (
        coding_handoff,
        confirm_handoff,
        execute_content_handoff,
    )

    results: List[Dict[str, Any]] = []
    for it in items[:20]:
        mode = str((it or {}).get("mode") or "").lower()
        task = (it or {}).get("task") or {}
        title = (task.get("title") or "").strip()
        if mode == "skip":
            results.append({"title": title, "mode": mode, "status": "skipped"})
            continue
        if mode not in ("document", "code") or not title:
            results.append({"title": title, "mode": mode, "status": "error",
                            "message": "mode: document|code|skip; task.title обязателен"})
            continue
        try:
            rec = await coding_handoff(
                uid, task, agent=(it.get("agent") or "claude"),
                repo_path=(it.get("repo_path") or ""),
                artifact_mode=bool(it.get("artifact_mode"))
                or (mode == "code" and not it.get("repo_path")),
                source=src if isinstance(src, dict) else None)
            hid = rec.get("id") or ""
            if mode == "document":
                # генерация документа может идти ~минуту → в фон, статус
                # подтянется webhook'ом/поллингом (как и code-путь)
                _t = _aio.create_task(execute_content_handoff(uid, hid))
                _BG = getattr(dispatch_tasks, "_bg", set())
                _BG.add(_t); _t.add_done_callback(_BG.discard)
                dispatch_tasks._bg = _BG  # type: ignore[attr-defined]
                results.append({"title": title, "mode": mode,
                                "status": "running", "handoff_id": hid})
            else:
                conf = await confirm_handoff(
                    uid, hid, repo_path=it.get("repo_path"), background=True)
                results.append({"title": title, "mode": mode,
                                "status": conf.get("status", "running"),
                                "handoff_id": hid,
                                "message": conf.get("message", "")})
        except Exception as e:
            logger.error(f"dispatch item failed: {e}", exc_info=True)
            results.append({"title": title, "mode": mode,
                            "status": "error", "message": str(e)})
    return {"status": "accepted", "items": results,
            "poll": "/api/v1/task-analysis/handoff/{id}",
            "webhook": "TESSENT_HANDOFF_WEBHOOK_URL (если настроен)"}


@get("/employee-report")
async def employee_report(
    request: Request,
    person: str = Parameter(query="person"),
    days: Optional[int] = Parameter(query="days", default=30, required=False),
    fmt: Optional[str] = Parameter(query="fmt", default=None, required=False),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Any:
    """Отчёт по сотруднику: встречи (граф PARTICIPATED_IN) + задачи из графа
    и задачников с ВЫЧИСЛЕННОЙ просрочкой (deadline vs сегодня) + активность.
    person — имя или person_id. Без fmt → JSON {markdown, stats, overdue};
    fmt=html|pdf|docx → файл существующим экспортом."""
    from litestar.response import Response

    uid = _resolve_user(request, user_id)
    if not (person or "").strip():
        raise HTTPException(status_code=400, detail="нужен person (имя/id)")
    from backend.core.reports.employee_report import build_employee_report
    rep = await build_employee_report(uid, person.strip(),
                                      days=int(days or 30))
    f = (fmt or "").lower().strip()
    if not f:
        return rep

    md = rep["markdown"]
    title = f"Отчёт по сотруднику {rep.get('person') or person}"[:110]
    from backend.core.analysis import export as _exp
    _MIME = {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "html": "text/html; charset=utf-8",
    }
    if f not in _MIME:
        raise HTTPException(status_code=400, detail="формат: html|pdf|docx")
    try:
        if f == "pdf":
            blob = _exp.markdown_to_pdf_bytes(md, title=title)
        elif f == "docx":
            blob = _exp.markdown_to_docx_bytes(md, title=title)
        else:
            blob = _exp.markdown_to_html_bytes(md, title=title)
    except Exception as e:
        logger.error("employee report render %s failed: %s", f, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"render error: {e}")
    if not blob:
        raise HTTPException(status_code=503,
                            detail=f"{f.upper()} недоступен на сервере")
    import urllib.parse
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", title).strip("_") or "employee_report"
    utf8_name = urllib.parse.quote(f"{title}.{f}")
    return Response(
        content=blob, media_type=_MIME[f],
        headers={"content-disposition":
                 f"attachment; filename=\"{ascii_name}.{f}\"; filename*=UTF-8''{utf8_name}"})


@get("/crm-owner-map")
async def crm_owner_map_get(
    request: Request,
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    """Маппинг CRM owner_id → человек (для CRM-секции отчёта сотрудника)."""
    uid = _resolve_user(request, user_id)
    from backend.core.reports.crm_workload import get_owner_map
    return {"map": get_owner_map(uid)}


@post("/crm-owner-map")
async def crm_owner_map_set(data: Dict[str, Any], request: Request) -> Dict[str, Any]:
    """Привязать owner_id провайдера к человеку.
    Body: {provider: amocrm|bitrix24|hubspot|pipedrive, owner_id, person}
    (person="" — отвязать)."""
    uid = _resolve_user(request, (data or {}).get("user_id"))
    from backend.core.reports.crm_workload import set_owner_mapping
    return set_owner_mapping(uid, str((data or {}).get("provider") or ""),
                             str((data or {}).get("owner_id") or ""),
                             str((data or {}).get("person") or ""))


@get("/meeting-summary")
async def meeting_summary(
    request: Request,
    meeting_id: Optional[str] = Parameter(query="meeting_id", default=None, required=False),
    meeting_title: Optional[str] = Parameter(query="meeting_title", default=None, required=False),
    fmt: Optional[str] = Parameter(query="fmt", default=None, required=False),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Any:
    """Итоги встречи: какие задачи выполнены исполнителями и зачем.

    Агрегирует handoff-записи пользователя с source.kind=meeting в один отчёт
    (задача → владелец → статус → вердикт → файлы → цель из ТЗ). Без fmt →
    JSON {markdown, stats, tasks}; fmt=html|pdf|docx|pptx → файл тем же
    экспортом, что и «Готовый документ». Хранилище per-user."""
    from litestar.response import Response

    uid = _resolve_user(request, user_id)
    if not (meeting_id or meeting_title):
        raise HTTPException(status_code=400,
                            detail="нужен meeting_id или meeting_title")
    from backend.core.tasks.meeting_summary import build_meeting_summary
    summary = build_meeting_summary(uid, meeting_id=meeting_id or "",
                                    meeting_title=meeting_title or "")
    f = (fmt or "").lower().strip()
    if not f:
        return summary

    md = summary["markdown"]
    title = f"Итоги встречи {summary.get('meeting_title') or ''}".strip()[:110]
    from backend.core.analysis import export as _exp
    _MIME = {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "html": "text/html; charset=utf-8",
    }
    if f not in _MIME:
        raise HTTPException(status_code=400, detail="формат: html|pdf|docx|pptx")
    try:
        if f == "pdf":
            blob = _exp.markdown_to_pdf_bytes(md, title=title)
        elif f == "docx":
            blob = _exp.markdown_to_docx_bytes(md, title=title)
        elif f == "pptx":
            blob = _exp.markdown_to_pptx_bytes(md, title=title)
        else:
            blob = _exp.markdown_to_html_bytes(md, title=title)
    except Exception as e:
        logger.error("meeting summary render %s failed: %s", f, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"render error: {e}")
    if not blob:
        raise HTTPException(status_code=503,
                            detail=f"{f.upper()} недоступен на сервере")
    import urllib.parse
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", title).strip("_") or "meeting_summary"
    utf8_name = urllib.parse.quote(f"{title}.{f}")
    return Response(
        content=blob, media_type=_MIME[f],
        headers={"content-disposition":
                 f"attachment; filename=\"{ascii_name}.{f}\"; filename*=UTF-8''{utf8_name}"})


@post("/handoff/{handoff_id:str}/reject")
async def handoff_reject(
    handoff_id: str,
    data: Dict[str, Any],
    request: Request,
) -> Dict[str, Any]:
    """Отклонить делегацию (ничего не запускается)."""
    uid = _resolve_user(request, (data or {}).get("user_id"))
    from backend.core.tasks.task_analysis import reject_handoff
    return await reject_handoff(uid, handoff_id,
                                reason=(data or {}).get("reason", ""))


@post("/handoff/{handoff_id:str}/deliver")
async def handoff_deliver(
    handoff_id: str,
    data: Dict[str, Any],
    request: Request,
) -> Dict[str, Any]:
    """Отправить результат завершённого handoff'а в задачник (существующая/
    новая задача YouGile/Trello/Jira) или CRM (env-гейт ENABLE_CRM_WRITEBACK).
    body: {target: {kind, system|provider, task_id|column_id|entity_id, …}}"""
    uid = _resolve_user(request, (data or {}).get("user_id"))
    if not uid:
        return {"status": "error", "message": "Authentication required"}
    from backend.core.tasks.task_analysis import deliver_handoff_result
    return await deliver_handoff_result(uid, handoff_id,
                                        (data or {}).get("target") or {})


@get("/llm-tiers")
async def llm_tiers_get(
    request: Request,
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    """Настройка «Модели»: override уровней Стандарт/Премиум (без ключей)."""
    uid = _resolve_user(request, user_id)
    if not uid:
        return {"status": "error", "message": "Authentication required"}
    from backend.core.llm.tier_overrides import public_view
    return {"status": "success", "levels": public_view(uid)}


@post("/llm-tiers")
async def llm_tiers_set(
    data: Dict[str, Any],
    request: Request,
) -> Dict[str, Any]:
    """Задать/сбросить модель уровня. body: {level, config:{provider,model,
    api_key,base_url}|null} либо {reset_all:true} — вернуться к моделям и
    ключам платформы."""
    uid = _resolve_user(request, (data or {}).get("user_id"))
    if not uid:
        return {"status": "error", "message": "Authentication required"}
    from backend.core.llm.tier_overrides import reset_all, set_override
    if (data or {}).get("reset_all"):
        return reset_all(uid)
    return set_override(uid, str((data or {}).get("level") or ""),
                        (data or {}).get("config"))


@get("/tracker-refs")
async def tracker_refs(
    request: Request,
    system: str = Parameter(query="system", default="yougile"),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    """Справочники задачника ДЛЯ ВЫБОРА СПИСКОМ (люди не любят ID):
    колонки/списки/проекты + последние задачи выбранной системы."""
    uid = _resolve_user(request, user_id)
    if not uid:
        return {"status": "error", "message": "Authentication required",
                "columns": [], "tasks": []}
    import json as _json
    columns: list = []
    tasks: list = []
    try:
        from backend.core.tasks.task_actions import _load_meetflow
        mf = _load_meetflow(uid)
        raw = await mf.list_task_columns(system=system)
        data = _json.loads(raw) if isinstance(raw, str) else (raw or {})
        for c in (data.get("columns") or []):
            cid = c.get("id") or c.get("column_id")
            name = c.get("name") or c.get("column_title") or c.get("title") or ""
            if cid and not c.get("archived"):
                columns.append({"id": str(cid), "name": str(name),
                                "board": str(c.get("board_title") or "")})
    except Exception as e:
        logger.debug(f"tracker-refs columns failed: {e}")
    try:
        from backend.core.tasks.task_analysis import collect_tasks_from_trackers
        all_tasks = await collect_tasks_from_trackers(uid)
        tasks = [{"id": t["id"], "title": t.get("title") or t["id"],
                  "status": t.get("status") or ""}
                 for t in all_tasks if t.get("tracker") == system and t.get("id")][:100]
    except Exception as e:
        logger.debug(f"tracker-refs tasks failed: {e}")
    return {"status": "success", "system": system,
            "columns": columns, "tasks": tasks}


@get("/delivery-route")
async def delivery_route_get(
    request: Request,
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    """Маршрут доставки результатов по умолчанию (None = не задан)."""
    uid = _resolve_user(request, user_id)
    if not uid:
        return {"status": "error", "message": "Authentication required"}
    from backend.core.tasks.delivery_prefs import get_route
    return {"status": "success", "target": get_route(uid)}


@post("/delivery-route")
async def delivery_route_set(
    data: Dict[str, Any],
    request: Request,
) -> Dict[str, Any]:
    """Сохранить/очистить маршрут по умолчанию. body: {target: {...}|null}."""
    uid = _resolve_user(request, (data or {}).get("user_id"))
    if not uid:
        return {"status": "error", "message": "Authentication required"}
    from backend.core.tasks.delivery_prefs import set_route
    return set_route(uid, (data or {}).get("target"))


@get("/recipients")
async def known_recipients(
    request: Request,
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    """Известные получатели из «Интеграций» — для выбора вместо ручных ID:
    Telegram-чаты (привязанный по умолчанию + группы получателей) и email'ы
    (из групп получателей). Пусто → UI показывает обычное поле ввода."""
    uid = _resolve_user(request, user_id)
    if not uid:
        return {"status": "error", "message": "Authentication required",
                "telegram": [], "emails": []}
    telegram: list = []
    emails: list = []
    try:
        from backend.core.messengers.links import resolve_telegram_chat_id
        cid = await resolve_telegram_chat_id(uid)
        if cid:
            telegram.append({"id": str(cid), "label": "Мой привязанный чат"})
    except Exception:
        logger.debug("recipients: default tg chat skipped", exc_info=True)
    try:
        from backend.core.automations.recipient_groups import list_groups
        for g in await list_groups(uid):
            ch = str(g.get("channel") or "telegram").lower()
            for r in (g.get("recipients") or []):
                r = str(r).strip()
                if not r:
                    continue
                if ch == "email" or "@" in r:
                    if r not in emails:
                        emails.append(r)
                else:
                    if all(t["id"] != r for t in telegram):
                        telegram.append({"id": r, "label": str(g.get("name") or "группа")})
    except Exception:
        logger.debug("recipients: groups skipped", exc_info=True)
    return {"status": "success", "telegram": telegram, "emails": emails}


@post("/handoff/{handoff_id:str}/delete")
async def handoff_delete(
    handoff_id: str,
    data: Dict[str, Any],
    request: Request,
) -> Dict[str, Any]:
    """Убрать запись из очереди (чтобы список не захламлялся). Хранилище
    per-user → удаляется только своя запись. Ничего не исполняет."""
    uid = _resolve_user(request, (data or {}).get("user_id"))
    if not uid:
        return {"status": "error", "message": "Authentication required"}
    from backend.core.tasks.handoff_store import HandoffStore
    ok = HandoffStore(uid).delete(handoff_id)
    return {"status": "success" if ok else "error",
            "deleted": ok, "handoff_id": handoff_id,
            **({} if ok else {"message": "запись не найдена"})}


@post("/handoffs/clear-finished")
async def handoffs_clear_finished(
    data: Dict[str, Any],
    request: Request,
) -> Dict[str, Any]:
    """Очистить все завершённые (done/failed/rejected) записи очереди. Идущие
    и ожидающие подтверждения не трогаются."""
    uid = _resolve_user(request, (data or {}).get("user_id"))
    if not uid:
        return {"status": "error", "message": "Authentication required"}
    from backend.core.tasks.handoff_store import HandoffStore
    removed = HandoffStore(uid).clear_finished()
    return {"status": "success", "removed": removed}


@post("/handoff/{handoff_id:str}/rework")
async def handoff_rework(
    handoff_id: str,
    data: Dict[str, Any],
    request: Request,
) -> Dict[str, Any]:
    """«Вернуть в доработку»: новая pending-запись с замечаниями ревью +
    комментарием заказчика (attempt++, parent-ссылка). Body: {note?, user_id?}."""
    uid = _resolve_user(request, (data or {}).get("user_id"))
    from backend.core.tasks.task_analysis import rework_handoff
    try:
        return await rework_handoff(uid, handoff_id,
                                    note=(data or {}).get("note", ""))
    except Exception as e:
        logger.error(f"handoff rework failed: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


@get("/lessons")
async def lessons_list(
    request: Request,
    limit: int = 50,
) -> Dict[str, Any]:
    """Уроки исполнения (Self-Grown): вердикты/замечания по каждому handoff."""
    uid = _resolve_user(request, None)
    from backend.core.tasks.lessons import list_lessons, list_playbooks
    return {"status": "success",
            "lessons": list_lessons(uid, limit=max(1, min(200, limit))),
            "playbooks": list_playbooks(uid)}


@post("/playbooks/crystallize")
async def playbooks_crystallize(
    data: Dict[str, Any],
    request: Request,
) -> Dict[str, Any]:
    """Черновики плейбуков из уроков (≥3 схожих; ничего не сохраняет)."""
    uid = _resolve_user(request, (data or {}).get("user_id"))
    from backend.core.tasks.lessons import crystallize_playbooks
    return await crystallize_playbooks(uid)


@post("/playbooks/dictate")
async def playbooks_dictate(
    data: Dict[str, Any],
    request: Request,
) -> Dict[str, Any]:
    """Надиктованный паттерн (голос→текст) → плейбук, применяемый всегда."""
    uid = _resolve_user(request, (data or {}).get("user_id"))
    if not uid:
        return {"status": "error", "message": "Authentication required"}
    from backend.core.tasks.lessons import dictate_playbook
    return await dictate_playbook(uid, str((data or {}).get("text") or ""))


@post("/playbooks/accept")
async def playbooks_accept(
    data: Dict[str, Any],
    request: Request,
) -> Dict[str, Any]:
    """Human gate: сохранить плейбук по явному действию. Body: {draft}."""
    uid = _resolve_user(request, (data or {}).get("user_id"))
    from backend.core.tasks.lessons import accept_playbook
    return accept_playbook(uid, (data or {}).get("draft") or {})


@post("/handoff/{handoff_id:str}/execute-content")
async def handoff_execute_content(
    handoff_id: str,
    data: Dict[str, Any],
    request: Request,
) -> Dict[str, Any]:
    """ИСПОЛНИТЬ контентное ТЗ: LLM генерирует готовый документ (КП/статью),
    авто-проверка + доставка. Body: {user_id?}."""
    uid = _resolve_user(request, (data or {}).get("user_id"))
    from backend.core.tasks.task_analysis import execute_content_handoff
    try:
        return await execute_content_handoff(uid, handoff_id)
    except Exception as e:
        logger.error(f"handoff execute-content failed: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


@post("/handoff/{handoff_id:str}/accept")
async def handoff_accept(
    handoff_id: str,
    data: Dict[str, Any],
    request: Request,
) -> Dict[str, Any]:
    """Принять КОНТЕНТНОЕ ТЗ как готовый результат (документ = результат).
    Body: {user_id?, note?}. Закрывает handoff без кодинг-исполнителя."""
    uid = _resolve_user(request, (data or {}).get("user_id"))
    from backend.core.tasks.task_analysis import accept_handoff_result
    try:
        return await accept_handoff_result(uid, handoff_id,
                                           note=(data or {}).get("note", ""))
    except Exception as e:
        logger.error(f"handoff accept failed: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


@post("/handoff/{handoff_id:str}/web-result")
async def handoff_web_result(
    handoff_id: str,
    data: Dict[str, Any],
    request: Request,
) -> Dict[str, Any]:
    """Зафиксировать результат web-хэндоффа (URL артефакта из Lovable/v0/…).

    Body: {user_id?, result_url}. Переводит pending web-хэндофф в done и, если
    задача была создана в трекере, прикладывает ссылку результата."""
    uid = _resolve_user(request, (data or {}).get("user_id"))
    result_url = str((data or {}).get("result_url") or "").strip()
    if not result_url:
        raise HTTPException(status_code=400, detail="result_url обязателен")
    from backend.core.tasks.task_analysis import submit_web_result
    return await submit_web_result(uid, handoff_id, result_url)


@get("/web-targets")
async def web_targets_list(request: Request) -> Dict[str, Any]:
    """Реестр web-only исполнителей (Lovable/v0/Bolt/Replit/Claude-web/ChatGPT)
    для выбора в UI."""
    from backend.core.executors.web_targets import list_targets
    return {"targets": list_targets()}


@get("/cli-health")
async def cli_health(
    request: Request,
    user_id: str | None = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    """Проверка, какие CLI-помощники реально установлены на сервере и включён
    ли рубильник исполнения. UI показывает это ДО подтверждения запуска —
    чтобы обычный человек не жал «Подтвердить» в пустоту («агент не найден»).
    Плюс — привязан ли СВОЙ ключ пользователя (BYO)."""
    import shutil
    bins = {"claude": "claude", "cursor": "cursor-agent", "codex": "codex"}
    found = {}
    for agent, binary in bins.items():
        path = shutil.which(binary)
        found[agent] = {"installed": bool(path), "binary": binary,
                        "path": path or None}
    try:
        from backend.core.config.feature_flags import get_feature_flags
        exec_enabled = bool(get_feature_flags().enable_coding_handoff_exec)
    except Exception:
        exec_enabled = False
    my_key = {"has_key": False}
    try:
        uid = _resolve_user(request, user_id)
        from backend.core.executors.user_credentials import build_credential_status
        my_key = await build_credential_status(uid)
    except Exception:
        pass
    return {"agents": found, "exec_enabled": exec_enabled, "my_key": my_key}


class CodingKeyRequest(BaseModel):
    provider: str  # anthropic | openai
    api_key: str
    user_id: Optional[str] = None


@get("/my-coding-key")
async def get_my_coding_key(
    request: Request,
    user_id: str | None = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    """Статус доступа к помощнику (свой BYO ИЛИ ключ из Интеграций;
    маскированно, plaintext не отдаём)."""
    uid = _resolve_user(request, user_id)
    from backend.core.executors.user_credentials import build_credential_status
    return await build_credential_status(uid)


@post("/my-coding-key")
async def set_my_coding_key(data: CodingKeyRequest, request: Request) -> Dict[str, Any]:
    """Привязать СВОЙ ключ помощника (свой аккаунт/биллинг вместо серверного).
    Ключ шифруется, наружу возвращается только маска."""
    uid = _resolve_user(request, data.user_id)
    from backend.core.executors.user_credentials import set_key
    res = set_key(uid, data.provider, data.api_key)
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res


@post("/my-coding-key/delete")
async def delete_my_coding_key(
    request: Request,
    user_id: str | None = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    """Отвязать свой доступ — сборка вернётся на серверный дефолт (если настроен)."""
    uid = _resolve_user(request, user_id)
    from backend.core.executors.user_credentials import delete_key
    return delete_key(uid)


@post("/my-coding-subscription")
async def set_my_coding_subscription(
    request: Request,
    user_id: str | None = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    """Переключить на «свою подписку/свой CLI»: заводим изолированные каталоги
    входа. Возвращает per-agent команды разового логина + статус (подключён/нет).
    Логин делается один раз командой (claude/codex login в своей папке)."""
    uid = _resolve_user(request, user_id)
    from backend.core.executors.user_credentials import set_subscription
    return set_subscription(uid)


@get("/handoffs")
async def handoffs_list(
    request: Request,
    user_id: str | None = Parameter(query="user_id", default=None, required=False),
    status: str | None = Parameter(query="status", default=None, required=False),
) -> Dict[str, Any]:
    """Список хэндоффов пользователя (pending — ждут подтверждения)."""
    uid = _resolve_user(request, user_id)
    from backend.core.tasks.handoff_store import HandoffStore
    items = HandoffStore(uid).list(status=status)
    return {"handoffs": items, "count": len(items)}


@get("/handoff/{handoff_id:str}")
async def handoff_status(
    handoff_id: str,
    request: Request,
    user_id: str | None = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    """Статус/результат хэндоффа (включая хвост вывода агента)."""
    uid = _resolve_user(request, user_id)
    from backend.core.tasks.handoff_store import HandoffStore
    rec = HandoffStore(uid).get(handoff_id)
    if not rec:
        raise HTTPException(status_code=404, detail="handoff not found")
    return {"handoff": rec}


@post("/task-action")
async def task_action(data: TaskActionRequest, request: Request) -> Dict[str, Any]:
    """Действие над задачей в задачнике (yougile/trello/jira), недеструктивно:
    - comment: добавить комментарий/заметку;
    - attach_result: прикрепить результат выполнения (агент сделал работу);
    - close: сменить статус на «готово» (без удаления; опц. с результатом);
    - update: обновить поля задачи.
    """
    uid = _resolve_user(request, data.user_id)
    from backend.core.tasks import task_actions
    act = data.action
    try:
        if act == "comment":
            if not data.text:
                raise HTTPException(status_code=400, detail="text обязателен")
            return await task_actions.comment_task(uid, data.system,
                                                   data.task_id, data.text)
        if act == "attach_result":
            if not data.text:
                raise HTTPException(status_code=400, detail="text обязателен")
            return await task_actions.attach_result(
                uid, data.system, data.task_id, result_text=data.text,
                author=data.author or "Tessbrain agent")
        if act == "close":
            return await task_actions.close_task(
                uid, data.system, data.task_id,
                target_column_id=data.target_column_id or "",
                transition_name=data.transition_name or "",
                result_text=data.text or "")
        if act == "update":
            return await task_actions.update_task(
                uid, data.system, data.task_id, data.fields or {})
        raise HTTPException(status_code=400,
                            detail="action: comment|attach_result|close|update")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"task_action failed: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


@get("/meeting-tasks")
async def meeting_tasks(
    request: Request,
    meeting_id: str = Parameter(query="meeting_id", required=True),
    user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> Dict[str, Any]:
    """Задачи конкретной встречи (Task→CREATED_FROM→Meeting из графа).

    Сценарий «кофе»: выбрать встречу → отметить задачи → пачкой в очередь
    Vibe Tasking (POST /handoff на каждую отмеченную)."""
    uid = _resolve_user(request, user_id)
    from backend.core.tasks.task_analysis import collect_meeting_tasks
    tasks = await collect_meeting_tasks(uid, meeting_id)
    return {"meeting_id": meeting_id, "tasks": tasks, "total": len(tasks),
            "note": ("" if tasks else
                     "задач не нашлось: встреча ещё не обработана "
                     "синхронизацией или задачи из неё не извлеклись")}


@post("/meeting-tasks/extract")
async def extract_meeting_tasks_route(
    request: Request,
    data: dict = Body(),
    user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> Dict[str, Any]:
    """Извлечь задачи из транскрипта встречи ПО ЗАПРОСУ — когда встреча ещё не
    обработана синхронизацией. Один LLM-вызов, best-effort сохраняет Task-узлы
    (найдутся в meeting-tasks), возвращает список для Vibe Tasking."""
    uid = _resolve_user(request, user_id)
    meeting_id = str((data or {}).get("meeting_id") or "").strip()
    if not meeting_id:
        return {"tasks": [], "total": 0, "note": "нужен meeting_id"}
    from backend.core.tasks.task_analysis import extract_meeting_tasks
    res = await extract_meeting_tasks(uid, meeting_id)
    return {"meeting_id": meeting_id, **res}


router = Router(
    path="/task-analysis",
    route_handlers=[analyze, handoff, handoff_confirm, handoff_artifact,
                    handoff_bundle,
                    handoff_render, handoff_reject, meeting_summary,
                    dispatch_tasks, employee_report,
                    crm_owner_map_get, crm_owner_map_set,
                    handoff_delete, handoff_deliver, handoffs_clear_finished,
                    tracker_refs, delivery_route_get, delivery_route_set,
                    known_recipients, llm_tiers_get, llm_tiers_set,
                    handoff_rework, lessons_list, playbooks_crystallize,
                    playbooks_accept, playbooks_dictate,
                    meeting_tasks, extract_meeting_tasks_route,
                    handoff_accept, handoff_execute_content,
                    handoff_web_result, web_targets_list, cli_health,
                    get_my_coding_key, set_my_coding_key, delete_my_coding_key,
                    set_my_coding_subscription,
                    handoffs_list, handoff_status, task_action],
    tags=["Task Analysis"],
)
