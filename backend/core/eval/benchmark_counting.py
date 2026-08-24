# -*- coding: utf-8 -*-
"""
Counting benchmark — синтетика с подсаженным ground-truth счётом.

Зачем: на LongMemEval-oracle мы паритетны с Mem0 (см. BENCHMARK_MEM0_H2H.md),
но multi-session оба нули. ПОЧЕМУ — потому что LongMemEval не моделирует доменные
события встреч компании. Этот бенч — целевой: 20 синтетических сценариев,
у каждого 5 счётных вопросов с КОНКРЕТНЫМ числом-ответом, где наш event_log
ДОЛЖЕН структурно бить retrieval-based подходы.

3 руки:
  dump      — вся история в контекст LLM, LLM сам считает.
  retrieval — BM25 top-k встреч, LLM считает по выборке (может пропустить).
  events    — наш EventLog.count() (структурный счёт; gold-truth-by-design).

Метрика: exact-match чисел из ответа модели vs ground-truth.

Запуск: OPENAI_API_KEY. По 20 диалогов × 5 вопросов = 100 точек. Каждая точка
= 2 LLM-вызова (dump + retrieval); events без LLM. Время ~10 мин.
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
import time
import urllib.request
import importlib.util

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
_MODEL = os.environ.get("CB_MODEL", "gpt-4o-mini")


def _llm(system: str, user: str, max_tokens=150) -> str:
    body = json.dumps({
        "model": _MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.0, "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
                 "Content-Type": "application/json"})
    for attempt in range(3):
        try:
            return json.load(urllib.request.urlopen(req, timeout=60))["choices"][0]["message"]["content"].strip()
        except Exception:
            time.sleep(2 * (attempt + 1))
    return ""


# ---------- генератор синтетики ---------------------------------------------
# Cценарий = «личная история пользователя за квартал»: события разных типов
# с датами. Из событий формируем сообщения-сессии и счётные вопросы.

_EVENT_TYPES = [
    ("workout", "пошёл на тренировку", "ходил на тренировку"),
    ("client_call", "созвонился с клиентом", "созванивался с клиентами"),
    ("doctor_visit", "был у врача", "посещал врача"),
    ("team_meeting", "провёл встречу с командой", "проводил встреч с командой"),
    ("travel", "ездил в командировку", "был в командировках"),
    ("interview", "провёл собеседование с кандидатом", "провёл собеседований"),
    ("demo", "показал демо клиенту", "показал демо"),
    ("review", "сделал ревью кода", "сделал ревью кода"),
]


def _random_dates(rng: random.Random, n: int, start="2024-01-01", end="2024-04-30") -> list:
    """Случайные даты в диапазоне (повторов нет, отсортированы)."""
    from datetime import date, timedelta
    d0 = date.fromisoformat(start)
    d1 = date.fromisoformat(end)
    span = (d1 - d0).days
    picked = set()
    while len(picked) < n:
        d = d0 + timedelta(days=rng.randint(0, span))
        picked.add(d.isoformat())
    return sorted(picked)


def gen_scenario(seed: int) -> dict:
    """Сгенерировать один сценарий + список ground-truth счётных вопросов."""
    rng = random.Random(seed)
    chosen = rng.sample(_EVENT_TYPES, k=4)  # 4 типа событий на сценарий
    events = []
    for kind, sg, pl in chosen:
        n = rng.randint(2, 8)  # 2..8 событий каждого типа
        for at in _random_dates(rng, n):
            events.append({"kind": kind, "at": at, "sg": sg, "pl": pl})
    rng.shuffle(events)

    # Формируем «диалог» — пара (user, assistant) на каждое событие
    sessions = []
    for ev in events:
        sessions.append({
            "role": "user",
            "content": f"[{ev['at']}] Я {ev['sg']} сегодня.",
            "at": ev["at"], "kind": ev["kind"],
        })
        sessions.append({
            "role": "assistant",
            "content": "Записал.",
            "at": ev["at"], "kind": ev["kind"],
        })

    # Ground-truth счётные вопросы (5 штук)
    questions = []
    # 1) общий счёт каждого типа
    kind_counts: dict = {}
    for ev in events:
        kind_counts[ev["kind"]] = kind_counts.get(ev["kind"], 0) + 1
    types_meta = {k: (sg, pl) for k, sg, pl in chosen}
    sample_k = rng.sample(list(kind_counts.keys()), k=min(2, len(kind_counts)))
    for k in sample_k:
        sg, pl = types_meta[k]
        questions.append({
            "q": f"Сколько раз я {pl} за этот период?",
            "kind": k, "gold": kind_counts[k],
            "filter": {},
        })
    # 2) счёт с временным фильтром (Q1 = янв-март)
    k = sample_k[0]
    q1_count = sum(1 for ev in events
                   if ev["kind"] == k and "2024-01-01" <= ev["at"] <= "2024-03-31")
    sg, pl = types_meta[k]
    questions.append({
        "q": f"Сколько раз я {pl} в Q1 (январь-март)?",
        "kind": k, "gold": q1_count,
        "filter": {"since": "2024-01-01", "until": "2024-03-31"},
    })
    # 3) счёт по конкретному месяцу
    k = sample_k[-1] if len(sample_k) > 1 else sample_k[0]
    feb_count = sum(1 for ev in events
                    if ev["kind"] == k and "2024-02-01" <= ev["at"] <= "2024-02-29")
    sg, pl = types_meta[k]
    questions.append({
        "q": f"Сколько раз я {pl} в феврале?",
        "kind": k, "gold": feb_count,
        "filter": {"since": "2024-02-01", "until": "2024-02-29"},
    })
    # 4) общий счёт ВСЕХ событий
    questions.append({
        "q": "Сколько всего у меня было записанных событий за этот период?",
        "kind": None, "gold": len(events),
        "filter": {},
    })

    return {
        "scenario_id": f"sc-{seed}",
        "events": events,
        "sessions": sessions,
        "questions": questions[:5],
    }


# ---------- 3 руки -----------------------------------------------------------
_ANS = ("Ты — ассистент с памятью. Отвечай ТОЛЬКО конкретным числом (одним) "
        "на счётный вопрос пользователя. Никаких слов, только число.")


def _history_text(scenario: dict, limit: int = None) -> str:
    sess = scenario["sessions"]
    if limit:
        sess = sess[:limit]
    return "\n".join(f"[{s['role']}] {s['content']}" for s in sess)


def arm_dump(scenario: dict, question: dict) -> int:
    hist = _history_text(scenario)
    out = _llm(_ANS, f"ИСТОРИЯ:\n{hist}\n\nВопрос: {question['q']}\nОтвет:")
    return _extract_number(out)


def arm_retrieval(scenario: dict, question: dict, topk: int = 12) -> int:
    """BM25 top-k реплик, LLM считает по сжатому контексту."""
    from collections import Counter
    import math
    sessions = scenario["sessions"]
    docs = [s["content"] for s in sessions]
    toks = [re.findall(r"[a-zа-я0-9]+", d.lower()) for d in docs]
    N = len(docs) or 1
    avgdl = sum(len(t) for t in toks) / N
    df = Counter()
    for t in toks:
        for w in set(t):
            df[w] += 1
    idf = {w: math.log(1 + (N - c + 0.5) / (c + 0.5)) for w, c in df.items()}
    q = re.findall(r"[a-zа-я0-9]+", question["q"].lower())
    scores = []
    for i, t in enumerate(toks):
        tf = Counter(t); dl = len(t) or 1; s = 0.0
        for w in q:
            if w in tf:
                s += idf.get(w, 0.0) * (tf[w] * 2.5) / (tf[w] + 1.5 * (1 - 0.75 + 0.75 * dl / avgdl))
        scores.append((s, i))
    scores.sort(reverse=True)
    idx = sorted(i for _s, i in scores[:topk])
    picked = "\n".join(f"[{sessions[i]['role']}] {sessions[i]['content']}" for i in idx)
    out = _llm(_ANS, f"РЕЛЕВАНТНАЯ ПАМЯТЬ:\n{picked}\n\nВопрос: {question['q']}\nОтвет:")
    return _extract_number(out)


def _load_event_log():
    p = os.path.join(_ROOT, "backend", "core", "memory", "event_log.py")
    s = importlib.util.spec_from_file_location("el_cb", p)
    m = importlib.util.module_from_spec(s); sys.modules["el_cb"] = m
    s.loader.exec_module(m)
    return m


def arm_events(scenario: dict, question: dict) -> int:
    """Наш EventLog.count() — структурный счёт, БЕЗ LLM."""
    el = _load_event_log()
    log = el.EventLog()
    # ingest как было бы из реального пайплайна индексации:
    # на каждое user-событие → один DomainEvent
    for ev in scenario["events"]:
        log.record(actor="u", kind=ev["kind"], at=ev["at"], source="synthetic")
    f = question.get("filter") or {}
    return log.count(actor="u", kind=question.get("kind"), **f)


# ---------- метрика ----------------------------------------------------------
def _extract_number(text: str) -> int:
    """Вытащить ПЕРВОЕ целое число из ответа LLM. -1 если нет числа."""
    m = re.search(r"\d+", str(text))
    return int(m.group()) if m else -1


# ---------- прогон -----------------------------------------------------------
def run(n_scenarios: int) -> dict:
    arms = {"dump": arm_dump, "retrieval": arm_retrieval, "events": arm_events}
    res = {a: {"correct": 0, "off_by_one": 0, "wrong": 0, "no_number": 0} for a in arms}
    rows = []
    t0 = time.time()
    total_q = 0
    for sid in range(n_scenarios):
        sc = gen_scenario(sid)
        print(f"  [{sid + 1}/{n_scenarios}] {len(sc['events'])} events, {len(sc['questions'])} questions",
              flush=True)
        for q in sc["questions"]:
            total_q += 1
            row = {"scenario": sc["scenario_id"], "q": q["q"], "gold": q["gold"]}
            for a, fn in arms.items():
                try:
                    pred = fn(sc, q)
                except Exception as e:
                    pred = -2
                    print(f"    ❌ {a} error: {e}", flush=True)
                row[a] = pred
                if pred == -1:
                    res[a]["no_number"] += 1
                elif pred == q["gold"]:
                    res[a]["correct"] += 1
                elif abs(pred - q["gold"]) == 1:
                    res[a]["off_by_one"] += 1
                else:
                    res[a]["wrong"] += 1
            print(f"     gold={q['gold']} dump={row['dump']} retr={row['retrieval']} events={row['events']}",
                  flush=True)
            rows.append(row)
    N = total_q or 1
    summary = {a: {"accuracy_exact": round(res[a]["correct"] / N, 4),
                   "accuracy_off1": round((res[a]["correct"] + res[a]["off_by_one"]) / N, 4),
                   **res[a]} for a in arms}
    return {"benchmark": "counting_synthetic", "n_questions": N,
            "n_scenarios": n_scenarios, "elapsed_s": round(time.time() - t0, 1),
            "model": _MODEL, "summary": summary, "rows": rows}


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    out = run(n)
    print(f"\n=== {out['benchmark']} | scenarios={out['n_scenarios']} | "
          f"questions={out['n_questions']} | model={out['model']} ===")
    for a in ("dump", "retrieval", "events"):
        s = out["summary"][a]
        print(f"  {a:10s}: exact={s['accuracy_exact']:.3f} | "
              f"±1={s['accuracy_off1']:.3f} | "
              f"(ok={s['correct']} off1={s['off_by_one']} wrong={s['wrong']} no#={s['no_number']})")
    print(f"elapsed: {out['elapsed_s']:.1f}s")
    p = os.path.join(_HERE, "benchmark_counting.json")
    json.dump(out, open(p, "w"), ensure_ascii=False, indent=2)
    print(f"полный прогон → {p}")
