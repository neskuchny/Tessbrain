#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ruff: noqa: T201 — это CLI, print это его вывод
"""
tessent — CLI для Tessbrain (обёртка над HTTP API).

Аудит показал: полноценного CLI не было (только служебные скрипты, ходящие
напрямую в данные). Этот CLI — тонкая обёртка над теми же endpoints, что
использует веб-UI/боты, поэтому ничего не дублирует и не обходит auth.

Настройка (env или ~/.tessent.env):
    TESSENT_API_URL    — базовый URL (default http://localhost:8000)
    TESSENT_API_TOKEN  — Bearer-токен (JWT из /auth/login или service-JWT)
    TESSENT_USER_ID    — user_id (если токена нет / для query-параметра)

Команды:
    tessent ask "вопрос"                 — спросить мозг (как чат в UI)
    tessent compose "задача"             — композитор артефактов
    tessent report types                 — каталог методологий-отчётов
    tessent report generate <type>       — сгенерировать отчёт
    tessent report list / show <id>      — история / полный отчёт
    tessent insights                     — лента инсайтов
    tessent skills [run <id> k=v ...]    — скиллы: список / запуск
    tessent sync <subscription_id>       — запустить синхронизацию знаний
    tessent ingest-url <url>             — поглотить внешнюю ссылку в память
    tessent automations                  — список автоматизаций
    tessent goals                        — цели/эпики и прогресс
    tessent review                       — недельный обзор движения к целям
    tessent tasks                        — анализ задач (done/open/blocked)
    tessent task-comment <sys> <id> <текст>   — добавить комментарий к задаче
    tessent task-close <sys> <id>             — закрыть задачу (смена статуса)
    tessent status                       — здоровье API/планировщика

Зависимости: stdlib + httpx (уже в requirements backend'а).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import httpx


def _load_env_file() -> None:
    path = os.path.expanduser("~/.tessent.env")
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except Exception:
        pass


_load_env_file()
BASE = os.environ.get("TESSENT_API_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.environ.get("TESSENT_API_TOKEN", "")
USER_ID = os.environ.get("TESSENT_USER_ID", "")


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if TOKEN:
        h["Authorization"] = f"Bearer {TOKEN}"
    return h


def _request(method: str, path: str, *, params: dict | None = None,
             body: dict | None = None, timeout: float = 180.0) -> dict:
    params = dict(params or {})
    if USER_ID and "user_id" not in params:
        params["user_id"] = USER_ID
    url = f"{BASE}/api/v1{path}"
    try:
        r = httpx.request(method, url, params=params, json=body,
                          headers=_headers(), timeout=timeout)
    except httpx.HTTPError as e:
        _die(f"API недоступен ({url}): {e}")
    if r.status_code >= 400:
        _die(f"HTTP {r.status_code}: {r.text[:500]}")
    try:
        return r.json()
    except Exception:
        _die(f"не-JSON ответ: {r.text[:300]}")


def _die(msg: str) -> None:
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


def _print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


# ============================== команды ==============================

def cmd_ask(args) -> None:
    body = {"messages": [{"role": "user", "content": args.question}],
            "stream": False, "model_tier": args.tier}
    if args.strategy:
        body["strategy"] = args.strategy
    data = _request("POST", "/chat/completions", body=body)
    answer = ((data.get("message") or {}).get("content")
              or data.get("content") or "(пустой ответ)")
    print(answer)
    sources = data.get("sources") or []
    if sources and not args.quiet:
        print(f"\n— Источников: {len(sources)}", file=sys.stderr)


def cmd_compose(args) -> None:
    body = {"request": args.request, "user_id": USER_ID,
            "company_context": args.context or ""}
    data = _request("POST", "/compose/", body=body)
    print(data.get("artifact_text") or json.dumps(data, ensure_ascii=False)[:2000])


def cmd_report(args) -> None:
    if args.action == "types":
        data = _request("GET", "/reports/methodology/types")
        for t in data.get("types", []):
            print(f"{t['icon']} {t['id']:<20} — {t['name']}: {t['description']}")
    elif args.action == "generate":
        if not args.type:
            _die("укажи тип: tessent report generate automation_audit")
        body = {"report_type": args.type, "user_id": USER_ID,
                "days_back": args.days, "model_tier": args.tier}
        if args.prompt:
            body["custom_prompt"] = args.prompt
        if args.project:
            body["project_id"] = args.project
        print(f"⏳ Генерирую отчёт «{args.type}»…", file=sys.stderr)
        data = _request("POST", "/reports/methodology/generate", body=body,
                        timeout=600.0)
        if data.get("status") != "success":
            _die(data.get("message", "ошибка генерации"))
        rep = data["report"]
        print(rep.get("content_text", ""))
        print(f"\n— id: {rep['id']} | встреч: {rep['meetings_count']}",
              file=sys.stderr)
    elif args.action == "list":
        data = _request("GET", "/reports/methodology/")
        for r in data.get("reports", []):
            print(f"{r.get('created_at', '')[:16]}  {r.get('icon', '')} "
                  f"{r.get('report_type', ''):<20} {r.get('id', '')}")
    elif args.action == "show":
        if not args.type:
            _die("укажи id: tessent report show <report_id>")
        data = _request("GET", f"/reports/methodology/{args.type}")
        print((data.get("report") or {}).get("content_text", ""))


def cmd_insights(args) -> None:
    data = _request("GET", "/meetflow/insights")
    for i in data.get("insights", [])[:args.limit]:
        mark = "•" if i.get("is_read") else "🔵"
        print(f"{mark} [{i.get('priority', '?'):<6}] {i.get('title', '')}"
              f"  ({i.get('source', '')})")
        if args.verbose:
            print(f"    {i.get('description', '')}\n")
    print(f"\nВсего: {data.get('total', 0)}", file=sys.stderr)


def cmd_skills(args) -> None:
    if args.action == "run":
        if not args.skill_id:
            _die("укажи id: tessent skills run <skill_id> period=Q1")
        kv = {}
        for pair in args.params or []:
            if "=" in pair:
                k, v = pair.split("=", 1)
                kv[k] = v
        data = _request("POST", f"/skills/{args.skill_id}/run",
                        body={"args": kv}, timeout=600.0)
        print(data.get("result") or data.get("response")
              or json.dumps(data, ensure_ascii=False)[:2000])
    else:
        data = _request("GET", "/skills/")
        for s in data.get("skills", []):
            params = ", ".join(p.get("name", "")
                               for p in (s.get("parameters_schema") or []))
            print(f"{s.get('id', '')}  {s.get('name', '')}"
                  + (f"  (параметры: {params})" if params else ""))


def cmd_sync(args) -> None:
    data = _request("POST", f"/knowledge-sync/sync/{args.subscription_id}",
                    params={"wait": "false"})
    _print_json(data)


def cmd_ingest_url(args) -> None:
    data = _request("POST", "/documents/ingest-url",
                    body={"url": args.url, "sync": not args.no_sync},
                    timeout=300.0)
    _print_json(data)


def cmd_automations(args) -> None:
    data = _request("GET", "/automations")
    items = data.get("automations") or data.get("items") or []
    for a in items:
        print(f"[{a.get('status', '?'):<9}] {a.get('schedule_type', ''):<8} "
              f"{a.get('name', '')}  ({a.get('action_type', '')})")


def cmd_goals(args) -> None:
    data = _request("GET", "/goals/progress")
    for g in data.get("goals", []):
        tp = g.get("task_progress") or {}
        bar = f"{g.get('progress', 0)}%"
        tasks = f" задач {tp.get('done', 0)}/{tp.get('total', 0)}" if tp.get("total") else ""
        print(f"[{g.get('level', ''):<10}] {bar:>4} {g.get('title', '')}{tasks}")
    if not data.get("goals"):
        print("Целей пока нет. Создавай через UI/API /goals.", file=sys.stderr)


def cmd_review(args) -> None:
    data = _request("GET", "/goals/weekly-review",
                    params={"with_llm": "true" if not args.no_llm else "false"},
                    timeout=300.0)
    if data.get("error"):
        _die(data["error"])
    print(f"📅 Неделя {data.get('week')}: целей {data.get('goals_total')}, "
          f"вперёд {data.get('moved_forward')} ↑, застряло {data.get('stalled')} →, "
          f"назад {data.get('regressed')} ↓, с блокерами {data.get('blocked_goals')}")
    for m in data.get("movement", []):
        print(f"  {m['trend']} [{m['level']:<10}] {m['progress']}% (Δ{m['delta']:+d}) "
              f"{m['title']}")
    rec = data.get("recommendation")
    if rec:
        print(f"\n💡 Рекомендации:\n{rec}")


def cmd_tasks(args) -> None:
    data = _request("GET", "/task-analysis/")
    print(f"Задач: {data.get('total', 0)} | выполнено {data.get('done', 0)} "
          f"({data.get('completion_rate', 0)}%) | открыто {data.get('open', 0)}")
    counts = data.get("counts", {})
    print(f"  todo={counts.get('todo', 0)} in_progress={counts.get('in_progress', 0)} "
          f"blocked={counts.get('blocked', 0)} done={counts.get('done', 0)}")
    if data.get("blocked"):
        print("\n🚫 Застрявшие:")
        for t in data["blocked"][:10]:
            print(f"  - {t.get('title', '')} ({t.get('assignee', '—')})")


def cmd_task_comment(args) -> None:
    data = _request("POST", "/task-analysis/task-action", body={
        "system": args.system, "task_id": args.task_id,
        "action": "attach_result" if args.result else "comment",
        "text": args.text})
    _print_json(data)


def cmd_task_close(args) -> None:
    body = {"system": args.system, "task_id": args.task_id, "action": "close"}
    if args.result:
        body["text"] = args.result
    if args.column:
        body["target_column_id"] = args.column
    if args.transition:
        body["transition_name"] = args.transition
    _print_json(_request("POST", "/task-analysis/task-action", body=body))


def cmd_handoffs(args) -> None:
    params = {"status": args.status} if args.status else None
    data = _request("GET", "/task-analysis/handoffs", params=params)
    for h in data.get("handoffs", []):
        print(f"[{h.get('status'):<20}] {h.get('id')}  {h.get('agent'):<7} "
              f"{h.get('task_title', '')[:60]}")
    print(f"\nВсего: {data.get('count', 0)}", file=sys.stderr)


def cmd_handoff_confirm(args) -> None:
    body = {}
    if args.repo:
        body["repo_path"] = args.repo
    print(f"⏳ Подтверждаю и запускаю агента (handoff {args.handoff_id})…",
          file=sys.stderr)
    data = _request("POST", f"/task-analysis/handoff/{args.handoff_id}/confirm",
                    body=body, timeout=1900.0)
    _print_json(data)


def cmd_handoff_reject(args) -> None:
    _print_json(_request("POST",
                         f"/task-analysis/handoff/{args.handoff_id}/reject",
                         body={"reason": args.reason or ""}))


def cmd_sima_kanon(args) -> None:
    """Kanon-операции SIMA: status | verify | handoff | batch."""
    if args.kanon_action == "status":
        data = _request("GET", "/sima/kanon/status",
                        params={"projectId": args.project})
        if data.get("success") is False:
            _print_json(data)
            return
        print(f"Готовы к handoff: {data.get('blocks_ready_for_handoff')}"
              f"/{data.get('blocks_total')}")
        for b in data.get("blocks", []):
            mark = "✅" if b.get("ready_for_handoff") else "▫️"
            print(f"  {mark} {b.get('name'):<30} вердикт="
                  f"{b.get('last_verdict') or '—':<13} "
                  f"детерм.проверок={b.get('deterministic_checks', 0)}")
    elif args.kanon_action == "verify":
        _print_json(_request("POST", "/sima/kanon/verify", body={
            "projectId": args.project, "blockId": args.block,
            "workspace": args.workspace,
            "runCommands": bool(args.run_commands)}, timeout=900.0))
    elif args.kanon_action == "handoff":
        data = _request("POST", "/sima/kanon/handoff", body={
            "projectId": args.project, "agent": args.agent,
            "folder": args.folder}, timeout=600.0)
        _print_json(data)
        if data.get("id"):
            print(f"\n⚠️ PENDING — агент НЕ запущен. Подтвердить: "
                  f"tessent handoff-confirm {data['id']}", file=sys.stderr)
    elif args.kanon_action == "batch":
        data = _request("POST", "/sima/kanon/handoff-batch", body={
            "projectId": args.project, "agent": args.agent,
            "folder": args.folder, "limit": args.limit}, timeout=900.0)
        _print_json(data)


def cmd_status(args) -> None:
    out = {}
    for name, path in (("api", "/health"), ("scheduler", "/scheduler-status"),
                       ("nightly", "/nightly-status")):
        try:
            out[name] = _request("GET", path, timeout=10.0)
        except SystemExit:
            out[name] = {"status": "unreachable"}
    _print_json(out)


def main() -> None:
    p = argparse.ArgumentParser(prog="tessent",
                                description="Tessbrain CLI (HTTP API)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("ask", help="спросить мозг (как чат в UI)")
    sp.add_argument("question")
    sp.add_argument("--strategy", choices=["auto", "quick", "standard", "deep"])
    sp.add_argument("--tier", default="standard", choices=["standard", "premium"])
    sp.add_argument("-q", "--quiet", action="store_true")
    sp.set_defaults(fn=cmd_ask)

    sp = sub.add_parser("compose", help="композитор артефактов")
    sp.add_argument("request")
    sp.add_argument("--context", help="контекст/принципы компании")
    sp.set_defaults(fn=cmd_compose)

    sp = sub.add_parser("report", help="методологии-отчёты")
    sp.add_argument("action", choices=["types", "generate", "list", "show"])
    sp.add_argument("type", nargs="?", help="тип отчёта / id для show")
    sp.add_argument("--days", type=int, default=30)
    sp.add_argument("--tier", default="standard", choices=["standard", "premium"])
    sp.add_argument("--prompt", help="для type=custom")
    sp.add_argument("--project", help="project_id")
    sp.set_defaults(fn=cmd_report)

    sp = sub.add_parser("insights", help="лента инсайтов")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("-v", "--verbose", action="store_true")
    sp.set_defaults(fn=cmd_insights)

    sp = sub.add_parser("skills", help="скиллы")
    sp.add_argument("action", nargs="?", default="list", choices=["list", "run"])
    sp.add_argument("skill_id", nargs="?")
    sp.add_argument("params", nargs="*", help="параметры k=v")
    sp.set_defaults(fn=cmd_skills)

    sp = sub.add_parser("sync", help="запустить синхронизацию знаний")
    sp.add_argument("subscription_id")
    sp.set_defaults(fn=cmd_sync)

    sp = sub.add_parser("ingest-url", help="поглотить внешнюю ссылку")
    sp.add_argument("url")
    sp.add_argument("--no-sync", action="store_true",
                    help="сохранить документ без индексации")
    sp.set_defaults(fn=cmd_ingest_url)

    sp = sub.add_parser("automations", help="список автоматизаций")
    sp.set_defaults(fn=cmd_automations)

    sp = sub.add_parser("goals", help="цели/эпики и прогресс")
    sp.set_defaults(fn=cmd_goals)

    sp = sub.add_parser("review", help="недельный обзор движения к целям")
    sp.add_argument("--no-llm", action="store_true", help="без рекомендаций LLM")
    sp.set_defaults(fn=cmd_review)

    sp = sub.add_parser("tasks", help="анализ задач (done/open/blocked)")
    sp.set_defaults(fn=cmd_tasks)

    sp = sub.add_parser("task-comment", help="добавить комментарий/результат к задаче")
    sp.add_argument("system", choices=["yougile", "trello", "jira"])
    sp.add_argument("task_id")
    sp.add_argument("text")
    sp.add_argument("--result", action="store_true", help="как результат выполнения")
    sp.set_defaults(fn=cmd_task_comment)

    sp = sub.add_parser("handoffs", help="хэндоффы кодинг-агентам (pending ждут подтверждения)")
    sp.add_argument("--status", help="фильтр: pending_confirmation|running|done|failed|rejected")
    sp.set_defaults(fn=cmd_handoffs)

    sp = sub.add_parser("handoff-confirm",
                        help="ЯВНО подтвердить и запустить кодинг-агента")
    sp.add_argument("handoff_id")
    sp.add_argument("--repo", help="путь к репозиторию (если не задан в handoff)")
    sp.set_defaults(fn=cmd_handoff_confirm)

    sp = sub.add_parser("handoff-reject", help="отклонить хэндофф")
    sp.add_argument("handoff_id")
    sp.add_argument("--reason", help="причина")
    sp.set_defaults(fn=cmd_handoff_reject)

    sp = sub.add_parser("sima-kanon",
                        help="Kanon-операции SIMA: статус контрактов, "
                             "верификация, handoff (PENDING до confirm)")
    sp.add_argument("kanon_action",
                    choices=["status", "verify", "handoff", "batch"])
    sp.add_argument("project", help="id SIMA-проекта")
    sp.add_argument("--block", help="id блока (для verify)")
    sp.add_argument("--workspace", help="папка реализации (для verify)")
    sp.add_argument("--run-commands", action="store_true",
                    help="исполнять cmd:-проверки приёмки")
    sp.add_argument("--agent", default="claude",
                    choices=["claude", "cursor", "codex"])
    sp.add_argument("--folder", help="папка workspace (default ~/sima-projects/<slug>)")
    sp.add_argument("--limit", type=int, default=5,
                    help="макс. блоков в batch-пачке")
    sp.set_defaults(fn=cmd_sima_kanon)

    sp = sub.add_parser("task-close", help="закрыть задачу (смена статуса, без удаления)")
    sp.add_argument("system", choices=["yougile", "trello", "jira"])
    sp.add_argument("task_id")
    sp.add_argument("--result", help="прикрепить текст результата перед закрытием")
    sp.add_argument("--column", help="id done-колонки (yougile/trello)")
    sp.add_argument("--transition", help="имя статуса (jira), default Done")
    sp.set_defaults(fn=cmd_task_close)

    sp = sub.add_parser("status", help="здоровье API/планировщика")
    sp.set_defaults(fn=cmd_status)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
