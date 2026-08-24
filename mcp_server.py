#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tessent-MCP — MCP-сервер Tessbrain (stdio, JSON-RPC 2.0).

Делает мозг компании инструментом для любого MCP-клиента (Claude Code,
Claude Desktop, Cursor и т.п.): «спроси мозг», «сгенерируй DNA-отчёт»,
«поглоти ссылку», «запусти скилл» — прямо из диалога с ассистентом.

Тонкая обёртка над тем же HTTP API, что у веб-UI/CLI — не обходит auth,
ничего не дублирует. Чистый stdlib (MCP по stdio — это JSON-RPC) + httpx.

Подключение (Claude Code):
    claude mcp add tessent -- python3 /path/to/tessent_brain/mcp_server.py
Или в конфиге MCP-клиента:
    {"mcpServers": {"tessent": {
        "command": "python3",
        "args": ["/path/to/tessent_brain/mcp_server.py"],
        "env": {"TESSENT_API_URL": "http://localhost:8000",
                "TESSENT_API_TOKEN": "...", "TESSENT_USER_ID": "..."}}}}

Конфиг — те же переменные, что у CLI: TESSENT_API_URL / TESSENT_API_TOKEN /
TESSENT_USER_ID (env или ~/.tessent.env).
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, Optional

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "tessent-brain", "version": "1.0.0"}


def _load_env_file() -> None:
    path = os.path.expanduser("~/.tessent.env")
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
    except Exception:
        pass


_load_env_file()
BASE = os.environ.get("TESSENT_API_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.environ.get("TESSENT_API_TOKEN", "")
USER_ID = os.environ.get("TESSENT_USER_ID", "")

# Per-request пользователь для HTTP/remote-режима (OAuth-коннектор): один процесс
# обслуживает разных людей, поэтому user_id берём из контекста запроса, а не из
# глобального env. Сервер-токен (TESSENT_API_TOKEN) общий — API исполняет от
# имени переданного user_id (service-auth + ?user_id=). stdio-режим контекст не
# ставит → работает по env, как раньше.
import contextvars as _contextvars

_req_user: "_contextvars.ContextVar[Optional[str]]" = _contextvars.ContextVar(
    "tessent_mcp_req_user", default=None)


def set_request_user(user_id: Optional[str]):
    """Установить пользователя текущего запроса (remote-режим). Возврат token
    для последующего reset (contextvars)."""
    return _req_user.set(user_id or None)


def _effective_user() -> str:
    return (_req_user.get() or USER_ID or "").strip()


def _api(method: str, path: str, *, params: Optional[dict] = None,
         body: Optional[dict] = None, timeout: float = 300.0) -> dict:
    import httpx
    params = dict(params or {})
    _uid = _effective_user()
    if _uid and "user_id" not in params:
        params["user_id"] = _uid
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    r = httpx.request(method, f"{BASE}/api/v1{path}", params=params,
                      json=body, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()


# ============================================================================
# Инструменты: имя → (описание, JSON-schema входа, обработчик)
# ============================================================================

def t_ask_brain(args: dict) -> str:
    """Вопрос мозгу компании (как чат в UI)."""
    body = {"messages": [{"role": "user", "content": args["question"]}],
            "stream": False,
            "model_tier": args.get("model_tier", "standard")}
    if args.get("strategy"):
        body["strategy"] = args["strategy"]
    data = _api("POST", "/chat/completions", body=body)
    answer = ((data.get("message") or {}).get("content")
              or data.get("content") or "(пустой ответ)")
    n_src = len(data.get("sources") or [])
    return answer + (f"\n\n[источников: {n_src}]" if n_src else "")


def t_compose(args: dict) -> str:
    data = _api("POST", "/compose/", body={
        "request": args["request"], "user_id": _effective_user(),
        "company_context": args.get("company_context", "")})
    out = data.get("artifact_text") or json.dumps(data, ensure_ascii=False)
    gaps = data.get("data_gaps") or []
    if gaps:
        out += "\n\n[пробелы данных: " + ", ".join(gaps) + "]"
    return out


def t_generate_report(args: dict) -> str:
    data = _api("POST", "/reports/methodology/generate", body={
        "report_type": args["report_type"], "user_id": _effective_user(),
        "days_back": int(args.get("days_back", 30)),
        "model_tier": args.get("model_tier", "standard"),
        "custom_prompt": args.get("custom_prompt"),
        "project_id": args.get("project_id")}, timeout=600.0)
    if data.get("status") != "success":
        return f"Не удалось: {data.get('message', data)}"
    rep = data["report"]
    return (f"# {rep.get('icon', '')} {rep.get('title', '')}\n"
            f"(встреч: {rep.get('meetings_count')}, id: {rep.get('id')})\n\n"
            + rep.get("content_text", ""))


def t_list_report_types(args: dict) -> str:
    data = _api("GET", "/reports/methodology/types")
    return "\n".join(f"{t['icon']} {t['id']} — {t['name']}: {t['description']}"
                     for t in data.get("types", []))


def t_list_reports(args: dict) -> str:
    data = _api("GET", "/reports/methodology/",
                params={"limit": str(args.get("limit", 20))})
    rows = [f"{r.get('created_at', '')[:16]} {r.get('icon', '')} "
            f"{r.get('report_type', '')} — {r.get('summary', '')[:120]} "
            f"(id: {r.get('id')})" for r in data.get("reports", [])]
    return "\n".join(rows) or "Отчётов пока нет."


def t_get_report(args: dict) -> str:
    data = _api("GET", f"/reports/methodology/{args['report_id']}")
    return (data.get("report") or {}).get("content_text", "не найден")


def t_insights(args: dict) -> str:
    data = _api("GET", "/meetflow/insights")
    rows = []
    for i in data.get("insights", [])[:int(args.get("limit", 20))]:
        rows.append(f"[{i.get('priority')}] {i.get('title')} "
                    f"(источник: {i.get('source')}, id: {i.get('id')})\n"
                    f"  {i.get('description', '')[:300]}")
    return "\n".join(rows) or "Инсайтов нет."


def t_search_memory(args: dict) -> str:
    """Поиск по памяти через тот же чат-endpoint в quick-режиме."""
    body = {"messages": [{"role": "user", "content": args["query"]}],
            "stream": False, "strategy": "quick", "model_tier": "standard"}
    data = _api("POST", "/chat/completions", body=body)
    return ((data.get("message") or {}).get("content")
            or data.get("content") or "(пусто)")


def t_ingest_url(args: dict) -> str:
    data = _api("POST", "/documents/ingest-url",
                body={"url": args["url"], "sync": bool(args.get("sync", True))})
    return json.dumps(data, ensure_ascii=False, indent=1)


def t_run_skill(args: dict) -> str:
    data = _api("POST", f"/skills/{args['skill_id']}/run",
                body={"args": args.get("params") or {}}, timeout=600.0)
    return (data.get("result") or data.get("response")
            or json.dumps(data, ensure_ascii=False))


def t_list_skills(args: dict) -> str:
    data = _api("GET", "/skills/")
    rows = []
    for s in data.get("skills", []):
        params = ", ".join(p.get("name", "")
                           for p in (s.get("parameters_schema") or []))
        rows.append(f"{s.get('name')} (id: {s.get('id')})"
                    + (f" — параметры: {params}" if params else ""))
    return "\n".join(rows) or "Скиллов нет."


def t_goals_progress(args: dict) -> str:
    data = _api("GET", "/goals/progress")
    rows = []
    for g in data.get("goals", []):
        tp = g.get("task_progress") or {}
        rows.append(f"[{g.get('level')}] {g.get('progress', 0)}% {g.get('title')}"
                    + (f" (задач {tp.get('done', 0)}/{tp.get('total', 0)})"
                       if tp.get("total") else ""))
    return "\n".join(rows) or "Целей пока нет."


def t_weekly_review(args: dict) -> str:
    data = _api("GET", "/goals/weekly-review",
                params={"with_llm": "true"}, timeout=300.0)
    if data.get("error"):
        return f"Ошибка: {data['error']}"
    head = (f"Неделя {data.get('week')}: вперёд {data.get('moved_forward')}↑, "
            f"застряло {data.get('stalled')}→, назад {data.get('regressed')}↓")
    moves = "\n".join(f"  {m['trend']} [{m['level']}] {m['progress']}% "
                      f"(Δ{m['delta']:+d}) {m['title']}"
                      for m in data.get("movement", []))
    rec = data.get("recommendation") or ""
    return f"{head}\n{moves}\n\n{rec}".strip()


def t_analyze_tasks(args: dict) -> str:
    data = _api("GET", "/task-analysis/")
    out = [f"Задач {data.get('total', 0)}: выполнено {data.get('done', 0)} "
           f"({data.get('completion_rate', 0)}%), открыто {data.get('open', 0)}"]
    c = data.get("counts", {})
    out.append(f"todo={c.get('todo', 0)} in_progress={c.get('in_progress', 0)} "
               f"blocked={c.get('blocked', 0)} done={c.get('done', 0)}")
    for t in (data.get("blocked") or [])[:10]:
        out.append(f"  🚫 {t.get('title')} ({t.get('assignee', '—')})")
    return "\n".join(out)


def t_coding_handoff(args: dict) -> str:
    data = _api("POST", "/task-analysis/handoff", body={
        "task": {"title": args["title"],
                 "description": args.get("description", "")},
        "agent": args.get("agent", "claude"),
        "repo_path": args.get("repo_path")}, timeout=600.0)
    if data.get("status") not in ("success", "pending_confirmation"):
        return f"Ошибка: {data.get('message', data)}"
    return (f"PENDING-хэндофф создан (id: {data.get('id')}). НИЧЕГО не "
            f"запущено — нужен явный confirm пользователя.\n"
            f"Запустить: confirm_coding_handoff(handoff_id='{data.get('id')}') "
            f"ИЛИ вручную команда:\n\n{data.get('command')}\n\n"
            f"--- ТЗ ---\n{data.get('spec_text', '')[:2000]}")


def t_confirm_handoff(args: dict) -> str:
    """ВНИМАНИЕ: вызывать ТОЛЬКО когда пользователь явно подтвердил запуск."""
    body = {}
    if args.get("repo_path"):
        body["repo_path"] = args["repo_path"]
    data = _api("POST",
                f"/task-analysis/handoff/{args['handoff_id']}/confirm",
                body=body, timeout=1900.0)
    return json.dumps(data, ensure_ascii=False)


def t_reject_handoff(args: dict) -> str:
    data = _api("POST",
                f"/task-analysis/handoff/{args['handoff_id']}/reject",
                body={"reason": args.get("reason", "")})
    return json.dumps(data, ensure_ascii=False)


def t_sima_kanon_status(args: dict) -> str:
    data = _api("GET", f"/sima/kanon/status?projectId={args['project_id']}")
    if data.get("success") is False:
        return f"Ошибка: {data.get('error')}"
    out = [f"Kanon-статус: {data.get('blocks_ready_for_handoff', 0)} из "
           f"{data.get('blocks_total', 0)} блоков готовы к handoff"]
    for b in data.get("blocks", []):
        mark = "✅" if b.get("ready_for_handoff") else "▫️"
        verdict = b.get("last_verdict") or "—"
        out.append(f"  {mark} {b.get('name')}: вердикт={verdict}, "
                   f"детерм. проверок={b.get('deterministic_checks', 0)}")
    return "\n".join(out)


def t_sima_kanon_verify(args: dict) -> str:
    data = _api("POST", "/sima/kanon/verify", body={
        "projectId": args["project_id"], "blockId": args["block_id"],
        "workspace": args["workspace"],
        "runCommands": bool(args.get("run_commands"))}, timeout=600.0)
    return json.dumps(data, ensure_ascii=False)


def t_sima_kanon_handoff(args: dict) -> str:
    data = _api("POST", "/sima/kanon/handoff", body={
        "projectId": args["project_id"],
        "agent": args.get("agent", "claude"),
        "folder": args.get("folder")}, timeout=600.0)
    if data.get("success") is False:
        return f"Ошибка: {data.get('error')}"
    return (f"PENDING-хэндофф SIMA-проекта создан (id: {data.get('id')}), "
            f"workspace: {data.get('projectFolder')}. НИЧЕГО не запущено — "
            f"нужен явный confirm пользователя "
            f"(confirm_coding_handoff(handoff_id='{data.get('id')}')).")


def t_task_comment(args: dict) -> str:
    data = _api("POST", "/task-analysis/task-action", body={
        "system": args["system"], "task_id": args["task_id"],
        "action": "attach_result" if args.get("as_result") else "comment",
        "text": args["text"], "author": args.get("author", "Tessbrain agent")})
    return json.dumps(data, ensure_ascii=False)


def t_task_close(args: dict) -> str:
    data = _api("POST", "/task-analysis/task-action", body={
        "system": args["system"], "task_id": args["task_id"], "action": "close",
        "text": args.get("result_text", ""),
        "target_column_id": args.get("target_column_id", ""),
        "transition_name": args.get("transition_name", "")})
    return json.dumps(data, ensure_ascii=False)


def t_status(args: dict) -> str:
    out = {}
    for name, path in (("api", "/health"), ("scheduler", "/scheduler-status")):
        try:
            out[name] = _api("GET", path, timeout=10.0)
        except Exception as e:
            out[name] = {"error": str(e)}
    return json.dumps(out, ensure_ascii=False, indent=1)


def _s(props: dict, required: list) -> dict:
    return {"type": "object", "properties": props, "required": required}

TOOLS: Dict[str, tuple] = {
    "ask_brain": (
        "Задать вопрос мозгу компании: поиск и анализ по всей памяти "
        "(встречи, документы, граф знаний, история).",
        _s({"question": {"type": "string"},
            "strategy": {"type": "string",
                         "enum": ["auto", "quick", "standard", "deep"]},
            "model_tier": {"type": "string", "enum": ["standard", "premium"]}},
           ["question"]), t_ask_brain),
    "compose_artifact": (
        "Выполнить произвольную задачу через композитор: подготовить КП, "
        "exit-план, резюме по человеку и т.п. — мозг сам планирует и собирает.",
        _s({"request": {"type": "string"},
            "company_context": {"type": "string"}}, ["request"]), t_compose),
    "generate_report": (
        "Сгенерировать методологию-отчёт по встречам: automation_audit "
        "(аудит автоматизации), business_dna_map (DNA-карта), triple "
        "(тройной анализ), employee_analysis, consultant_analysis, "
        "key_decisions, project_summary, project_progress, custom.",
        _s({"report_type": {"type": "string"},
            "days_back": {"type": "integer"},
            "model_tier": {"type": "string", "enum": ["standard", "premium"]},
            "custom_prompt": {"type": "string"},
            "project_id": {"type": "string"}}, ["report_type"]),
        t_generate_report),
    "list_report_types": ("Каталог доступных методологий-отчётов.",
                          _s({}, []), t_list_report_types),
    "list_reports": ("История сгенерированных отчётов (без полного текста).",
                     _s({"limit": {"type": "integer"}}, []), t_list_reports),
    "get_report": ("Полный текст отчёта по id.",
                   _s({"report_id": {"type": "string"}}, ["report_id"]),
                   t_get_report),
    "insights_feed": ("Лента инсайтов системы (слепые зоны, рассинхроны, "
                      "аномалии, паттерны из встреч).",
                      _s({"limit": {"type": "integer"}}, []), t_insights),
    "search_memory": ("Быстрый поиск по памяти компании (quick-режим).",
                      _s({"query": {"type": "string"}}, ["query"]),
                      t_search_memory),
    "ingest_url": ("Поглотить внешнюю ссылку в память компании: скачать, "
                   "извлечь текст, проиндексировать в граф и вектора.",
                   _s({"url": {"type": "string"},
                       "sync": {"type": "boolean"}}, ["url"]), t_ingest_url),
    "list_skills": ("Список скиллов (повторно используемых задач) с параметрами.",
                    _s({}, []), t_list_skills),
    "run_skill": ("Запустить скилл по id с параметрами.",
                  _s({"skill_id": {"type": "string"},
                      "params": {"type": "object"}}, ["skill_id"]), t_run_skill),
    "goals_progress": ("Цели/эпики компании и прогресс по связанным задачам.",
                       _s({}, []), t_goals_progress),
    "weekly_review": ("Недельный обзор движения к целям: что продвинулось/"
                      "застряло + рекомендации, что поменять.",
                      _s({}, []), t_weekly_review),
    "analyze_tasks": ("Анализ задач: сколько выполнено/открыто/застряло, "
                      "по владельцам.", _s({}, []), t_analyze_tasks),
    "coding_handoff": ("ФАЗА 1: сгенерировать ТЗ + PENDING-хэндофф кодинг-"
                       "агенту (claude/cursor/codex). НИЧЕГО не запускает — "
                       "покажи пользователю и жди его явного решения.",
                       _s({"title": {"type": "string"},
                           "description": {"type": "string"},
                           "agent": {"type": "string",
                                     "enum": ["claude", "cursor", "codex"]},
                           "repo_path": {"type": "string"}}, ["title"]),
                       t_coding_handoff),
    "confirm_coding_handoff": (
        "ФАЗА 2: запустить кодинг-агента по хэндоффу. Вызывать ТОЛЬКО когда "
        "пользователь ЯВНО подтвердил запуск (спроси его). Требует ops-флаг "
        "enable_coding_handoff_exec на сервере.",
        _s({"handoff_id": {"type": "string"},
            "repo_path": {"type": "string"}}, ["handoff_id"]),
        t_confirm_handoff),
    "reject_coding_handoff": (
        "Отклонить хэндофф (пользователь решил не делегировать).",
        _s({"handoff_id": {"type": "string"},
            "reason": {"type": "string"}}, ["handoff_id"]),
        t_reject_handoff),
    "sima_kanon_status": (
        "Kanon-статус SIMA-проекта: готовность контрактов блоков к "
        "handoff, вердикты верификаций (pass/fail/inconclusive).",
        _s({"project_id": {"type": "string"}}, ["project_id"]),
        t_sima_kanon_status),
    "sima_kanon_verify": (
        "Tri-state верификация блока SIMA против workspace (папки "
        "реализации) + каскад по зависимостям + drift-скан.",
        _s({"project_id": {"type": "string"},
            "block_id": {"type": "string"},
            "workspace": {"type": "string"},
            "run_commands": {"type": "boolean"}},
           ["project_id", "block_id", "workspace"]),
        t_sima_kanon_verify),
    "sima_kanon_handoff": (
        "ФАЗА 1: Kanon-workspace + ТЗ SIMA-проекта → PENDING-хэндофф "
        "кодинг-агенту. НИЧЕГО не запускает — покажи пользователю и жди "
        "его явного решения (confirm_coding_handoff).",
        _s({"project_id": {"type": "string"},
            "agent": {"type": "string",
                      "enum": ["claude", "cursor", "codex"]},
            "folder": {"type": "string"}}, ["project_id"]),
        t_sima_kanon_handoff),
    "task_comment": ("Добавить комментарий/результат выполнения к задаче в "
                     "задачнике (yougile/trello/jira), недеструктивно.",
                     _s({"system": {"type": "string",
                                    "enum": ["yougile", "trello", "jira"]},
                         "task_id": {"type": "string"},
                         "text": {"type": "string"},
                         "as_result": {"type": "boolean"},
                         "author": {"type": "string"}},
                        ["system", "task_id", "text"]), t_task_comment),
    "task_close": ("Закрыть задачу сменой статуса (без удаления; опц. с "
                   "результатом). yougile/trello — done-колонка, jira — "
                   "transition.",
                   _s({"system": {"type": "string",
                                  "enum": ["yougile", "trello", "jira"]},
                       "task_id": {"type": "string"},
                       "result_text": {"type": "string"},
                       "target_column_id": {"type": "string"},
                       "transition_name": {"type": "string"}},
                      ["system", "task_id"]), t_task_close),
    "brain_status": ("Здоровье API и планировщика автоматизаций.",
                     _s({}, []), t_status),
}


# ============================================================================
# JSON-RPC по stdio
# ============================================================================

def _reply(msg_id, result=None, error=None) -> None:
    out: Dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle_message(msg: dict) -> Optional[dict]:
    """Чистый JSON-RPC обработчик (без I/O): вернёт ответ-словарь или None
    для уведомлений. Используется и stdio-loop'ом, и hosted HTTP-роутом
    (backend/api/routes/mcp_http.py) — одна логика, ноль дублирования."""
    method = msg.get("method", "")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    def _resp(result=None, error=None) -> dict:
        out: Dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
        if error is not None:
            out["error"] = error
        else:
            out["result"] = result
        return out

    if method == "initialize":
        return _resp({"protocolVersion": PROTOCOL_VERSION,
                      "capabilities": {"tools": {}},
                      "serverInfo": SERVER_INFO})
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None  # уведомления без ответа
    if method == "ping":
        return _resp({})
    if method == "tools/list":
        tools = [{"name": name, "description": desc, "inputSchema": schema}
                 for name, (desc, schema, _fn) in TOOLS.items()]
        return _resp({"tools": tools})
    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        entry = TOOLS.get(name)
        if not entry:
            return _resp(error={"code": -32602,
                                "message": f"unknown tool: {name}"})
        try:
            text = entry[2](args)
            return _resp({"content": [{"type": "text", "text": text}],
                          "isError": False})
        except Exception as e:
            return _resp({"content": [{"type": "text",
                                       "text": f"Ошибка: {e}"}],
                          "isError": True})
    if msg_id is not None:
        return _resp(error={"code": -32601,
                            "message": f"method not found: {method}"})
    return None


def _handle(msg: dict) -> None:
    resp = handle_message(msg)
    if resp is None:
        return
    if "error" in resp:
        _reply(resp["id"], error=resp["error"])
    else:
        _reply(resp["id"], result=resp["result"])


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            _handle(msg)
        except Exception as e:
            if msg.get("id") is not None:
                _reply(msg["id"], error={"code": -32603, "message": str(e)})


if __name__ == "__main__":
    main()
