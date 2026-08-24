# -*- coding: utf-8 -*-
"""Golden-set харнесс (M4-компаундинг из спеки живой памяти).

Идея: фиксированный набор вопросов гоняется по чату регулярно (после
консолидаций/релизов); ответы сохраняются с датой. Сравнивая прогоны,
видно ЧИСЛОМ, улучшается память со временем или деградирует — вместо
«кажется, стало лучше».

Использование (backend должен быть запущен):
    python scripts/run_golden_set.py --user-id <uuid> [--strategy standard]

Вопросы: data/golden_questions.json — СПИСОК строк; правится владельцем
(не тем, кто пишет код системы — дисциплина спеки §9). Если файла нет,
создаётся стартовый шаблон.

Результат: data/golden_runs/run_<дата>.json
  {question, answer, sources_count, elapsed_s} по каждому вопросу.
Сравнение двух прогонов: python scripts/run_golden_set.py --compare A B
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "http://localhost:8000"
QUESTIONS_FILE = Path("data/golden_questions.json")
RUNS_DIR = Path("data/golden_runs")

_STARTER = [
    "Что мы знаем о Екатерине Постоваловой?",
    "Какие активные проекты у компании и в каком они состоянии?",
    "Кто отвечает за маркетинг?",
    "Какие ключевые решения были приняты за последний месяц?",
    "Какие правила и регламенты действуют в компании?",
]


def _auth_token(user_id: str) -> str:
    """Сервис-токен для строгого режима чата (enable_strict_chat_auth):
    без Bearer прогон получает 403 на каждый вопрос и молча пишет 0/N.
    Секрет берётся из настроек бэкенда/env; нет секрета → без токена."""
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from make_service_token import make_token
        import os
        secret = os.getenv("SERVICE_JWT_SECRET", "") or os.getenv("TESSENT_SERVICE_JWT_SECRET", "")
        audience, issuer = "tessent-brain", "meetflow"
        if not secret:
            sys.path.insert(0, str(Path(__file__).parent.parent))
            from backend.config import settings
            secret = getattr(settings, "service_jwt_secret", "") or ""
            audience = getattr(settings, "service_jwt_audience", audience)
            issuer = getattr(settings, "service_jwt_issuer", issuer)
        if secret:
            return make_token(secret, user_id, audience, issuer, days=1)
    except Exception:
        pass
    return ""


def _post_chat(user_id: str, question: str, strategy: str, token: str) -> dict:
    body = json.dumps({
        "messages": [{"role": "user", "content": question}],
        "session_id": "golden_set",
        "strategy": strategy,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{API}/api/v1/chat/completions?user_id={user_id}",
        data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run(user_id: str, strategy: str) -> None:
    if not QUESTIONS_FILE.exists():
        QUESTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        QUESTIONS_FILE.write_text(
            json.dumps(_STARTER, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Создан стартовый {QUESTIONS_FILE} — отредактируйте вопросы "
              f"под свой бизнес и перезапустите.")
        return
    questions = json.loads(QUESTIONS_FILE.read_text(encoding="utf-8"))
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    token = _auth_token(user_id)
    if not token:
        print("⚠️ Сервис-токен не выпущен: секрет не найден.\n"
              "   Как починить (1 раз):\n"
              "   1) добавьте в .env строку:  SERVICE_JWT_SECRET=<длинная случайная строка>\n"
              "      (сгенерировать: python -c \"import secrets;print(secrets.token_urlsafe(48))\")\n"
              "   2) перезапустите бэкенд (granian)\n"
              "   3) запустите этот скрипт снова\n"
              "   При включённом strict-auth без этого чат отвечает 403.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    results = []
    for i, q in enumerate(questions, 1):
        print(f"[{i}/{len(questions)}] {q[:70]}…", end=" ", flush=True)
        t0 = time.monotonic()
        try:
            data = _post_chat(user_id, q, strategy, token)
            answer = (data.get("message") or {}).get("content", "")
            if answer:
                print(f"ok ({len(answer)} симв.)")
                results.append({
                    "question": q,
                    "answer": answer,
                    "answer_len": len(answer),
                    "sources_count": len(data.get("sources") or []),
                    "elapsed_s": round(time.monotonic() - t0, 1),
                })
            else:
                # пустой ответ — показываем, ЧТО вернул сервер, а не молчим
                err = (data.get("error") or data.get("detail")
                       or json.dumps(data, ensure_ascii=False)[:200])
                print(f"ПУСТО: {err}")
                results.append({"question": q, "error": str(err),
                                "elapsed_s": round(time.monotonic() - t0, 1)})
        except Exception as e:
            print(f"ОШИБКА: {e}")
            results.append({"question": q, "error": str(e),
                            "elapsed_s": round(time.monotonic() - t0, 1)})
    out = RUNS_DIR / f"run_{stamp}.json"
    out.write_text(json.dumps(
        {"stamp": stamp, "strategy": strategy, "results": results},
        ensure_ascii=False, indent=2), encoding="utf-8")
    ok = sum(1 for r in results if "answer" in r)
    print(f"\n✅ {ok}/{len(results)} ответов → {out}")
    print("Сравнить с прошлым прогоном: "
          f"python scripts/run_golden_set.py --compare <старый> {out.name}")


def compare(a: str, b: str) -> None:
    ra = json.loads((RUNS_DIR / a).read_text(encoding="utf-8"))
    rb = json.loads((RUNS_DIR / b).read_text(encoding="utf-8"))
    print(f"\n{'вопрос':60} | {'источн.':>14} | {'время,с':>14}")
    print("-" * 96)
    qa = {r["question"]: r for r in ra["results"]}
    for r in rb["results"]:
        old = qa.get(r["question"], {})
        s_old, s_new = old.get("sources_count", "?"), r.get("sources_count", "?")
        t_old, t_new = old.get("elapsed_s", "?"), r.get("elapsed_s", "?")
        print(f"{r['question'][:58]:60} | {s_old:>5} → {s_new:<5} | {t_old:>5} → {t_new:<5}")
    print(f"\nОтветы лежат в {a} / {b} — качество сравнивайте глазами "
          f"(или LLM-судьёй, следующий шаг).")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--user-id")
    p.add_argument("--strategy", default="standard")
    p.add_argument("--compare", nargs=2, metavar=("OLD", "NEW"))
    args = p.parse_args()
    if args.compare:
        compare(*args.compare)
    elif args.user_id:
        run(args.user_id, args.strategy)
    else:
        p.print_help()
        sys.exit(1)
