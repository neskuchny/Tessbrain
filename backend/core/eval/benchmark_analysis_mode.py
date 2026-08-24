# -*- coding: utf-8 -*-
"""
Analysis-mode эксперимент — проверяем гипотезу пользователя:
на ДЕШЁВОЙ модели multi-session/temporal вопросы лучше решаются, если СТРУКТУРНО
собрать хронологический таймлайн (даты!) и дать analysis-промпт (компилировать,
рассуждать), а НЕ строгий «NOT MENTIONED».

Фокус: только трудные категории, где мы сейчас проваливаемся (multi-session 1/6,
temporal-reasoning 3/9 на n=30 oracle).

3 руки:
  strict   — текущее поведение: top-k + строгий промпт («say NOT MENTIONED»).
  dump     — вся история, строгий промпт (потолок full-context).
  analysis — ВСЯ история В ХРОНОЛОГИИ с датами + analysis-промпт
             (рассуждай по шагам, соединяй сессии, дай лучший ответ; без NOT MENTIONED).

Если analysis > strict/dump на этих категориях → подтверждается:
  (а) структурный таймлайн помогает дешёвой модели на multi-session/temporal;
  (б) over-abstention реально снижал нам результат.
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
import time
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_MODEL = os.environ.get("AM_MODEL", "gpt-4o-mini")
_JUDGE = os.environ.get("AM_JUDGE", "gpt-4o-mini")
_HARD_TYPES = {"multi-session", "temporal-reasoning", "single-session-preference"}


def _llm(system, user, model=None, max_tokens=500):
    body = json.dumps({"model": model or _MODEL,
                       "messages": [{"role": "system", "content": system},
                                    {"role": "user", "content": user}],
                       "temperature": 0.0, "max_tokens": max_tokens}).encode()
    req = urllib.request.Request("https://api.openai.com/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
                 "Content-Type": "application/json"})
    for a in range(3):
        try:
            return json.load(urllib.request.urlopen(req, timeout=90))["choices"][0]["message"]["content"].strip()
        except Exception:
            time.sleep(2 * (a + 1))
    return ""


def load_hard(n: int) -> list:
    from backend.core.eval.datasets import load_dataset
    data = load_dataset("longmemeval_oracle")
    random.Random(42).shuffle(data)
    out = [x for x in data if x.get("question_type") in _HARD_TYPES]
    return out[:n]


def _turns_with_dates(item):
    """Ходы с датой сессии (для хронологии)."""
    dates = item.get("haystack_dates", []) or []
    out = []
    for si, sess in enumerate(item.get("haystack_sessions", [])):
        d = dates[si] if si < len(dates) else "0000/00/00"
        for t in sess:
            out.append({"role": t.get("role", ""), "content": t.get("content", ""),
                        "date": d})
    return out


_STRICT = ("You are an assistant with memory. Answer using ONLY the provided history. "
           "If the history doesn't contain the answer, say exactly: NOT MENTIONED.")

_ANALYSIS = (
    "You are an analyst with full timestamped memory of the user's history. "
    "The memory is given in CHRONOLOGICAL order with dates. Your job is to ANALYZE, "
    "not just look up: combine information ACROSS different sessions/dates, reason "
    "about ORDER and CHANGE over time, and give your BEST supported answer. "
    "Think step by step over the timeline. Always give a concrete answer derived "
    "from the data — do NOT refuse. End with a single concise final answer line.")


def arm_strict(item, topk=12):
    turns = _turns_with_dates(item)
    docs = [f"{t['role']}: {t['content']}" for t in turns]
    idx = _bm25(item["question"], docs, topk)
    picked = [turns[i] for i in sorted(idx)]
    hist = "\n".join(f"[{t['role']}] {t['content']}" for t in picked)[:120000]
    return _llm(_STRICT, f"MEMORY:\n{hist}\n\nQuestion: {item['question']}\nAnswer:")


def arm_dump(item):
    turns = _turns_with_dates(item)
    hist = "\n".join(f"[{t['role']}] {t['content']}" for t in turns)[:120000]
    return _llm(_STRICT, f"HISTORY:\n{hist}\n\nQuestion: {item['question']}\nAnswer:")


def arm_analysis(item):
    """ВСЯ история В ХРОНОЛОГИИ с датами + analysis-промпт."""
    turns = _turns_with_dates(item)
    turns.sort(key=lambda t: t["date"])
    hist = "\n".join(f"[{t['date']}] [{t['role']}] {t['content']}" for t in turns)[:120000]
    qdate = item.get("question_date", "")
    user = (f"Question asked on: {qdate}\n\nTIMESTAMPED MEMORY (chronological):\n{hist}\n\n"
            f"Question: {item['question']}\n\nAnalyze and answer:")
    out = _llm(_ANALYSIS, user, max_tokens=600)
    # берём последнюю непустую строку как финальный ответ (для судьи)
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return lines[-1] if lines else out


def _bm25(query, docs, k):
    import math
    from collections import Counter
    toks = [re.findall(r"[a-zа-я0-9]+", d.lower()) for d in docs]
    N = len(docs) or 1
    avgdl = sum(len(t) for t in toks) / N
    df = Counter()
    for t in toks:
        for w in set(t):
            df[w] += 1
    idf = {w: math.log(1 + (N - c + 0.5) / (c + 0.5)) for w, c in df.items()}
    q = re.findall(r"[a-zа-я0-9]+", query.lower())
    sc = []
    for i, t in enumerate(toks):
        tf = Counter(t); dl = len(t) or 1; s = 0.0
        for w in q:
            if w in tf:
                s += idf.get(w, 0.0) * (tf[w] * 2.5) / (tf[w] + 1.5 * (1 - 0.75 + 0.75 * dl / avgdl))
        sc.append((s, i))
    sc.sort(reverse=True)
    return [i for _s, i in sc[:k]]


def judge(item, pred):
    if str(item.get("question_id", "")).endswith("_abs"):
        return bool(re.search(r"not mentioned|don'?t know", (pred or "").lower()))
    v = _llm("Grade if model answer is consistent with gold (same key fact). Reply ONLY YES or NO.",
             f"Q: {item['question']}\nGold: {item['answer']}\nModel: {pred}\nYES/NO:",
             model=_JUDGE, max_tokens=5)
    return bool(re.search(r"\byes\b", (v or "").lower()))


def run(n):
    data = load_hard(n)
    arms = {"strict": arm_strict, "dump": arm_dump, "analysis": arm_analysis}
    res = {a: {"ok": 0, "by_type": {}} for a in arms}
    rows = []
    t0 = time.time()
    for i, item in enumerate(data, 1):
        qt = item["question_type"]
        print(f"  [{i}/{len(data)}] {qt} — {item['question'][:55]}", flush=True)
        row = {"id": item.get("question_id"), "type": qt, "q": item["question"], "gold": item["answer"]}
        for a, fn in arms.items():
            try:
                pred = fn(item)
            except Exception as e:
                pred = f"(err: {e})"
            ok = judge(item, pred)
            res[a]["ok"] += ok
            res[a]["by_type"].setdefault(qt, [0, 0])
            res[a]["by_type"][qt][0] += ok
            res[a]["by_type"][qt][1] += 1
            row[a] = {"ok": ok, "pred": pred[:200]}
            print(f"    {a:9s}: {'✅' if ok else '❌'} {pred[:70]}", flush=True)
        rows.append(row)
    N = len(data) or 1
    summary = {a: {"accuracy": round(res[a]["ok"] / N, 4),
                   "by_type": {t: round(c / m, 3) for t, (c, m) in res[a]["by_type"].items()}}
               for a in arms}
    return {"benchmark": "analysis_mode_hard", "n": N, "model": _MODEL,
            "elapsed_s": round(time.time() - t0, 1), "summary": summary, "rows": rows}


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    out = run(n)
    print(f"\n=== {out['benchmark']} | n={out['n']} (только трудные типы) | {out['model']} ===")
    for a in ("strict", "dump", "analysis"):
        print(f"  {a:9s}: {out['summary'][a]['accuracy']:.3f}  {out['summary'][a]['by_type']}")
    print(f"elapsed: {out['elapsed_s']:.1f}s")
    json.dump(out, open(os.path.join(_HERE, "benchmark_analysis_mode.json"), "w"),
              ensure_ascii=False, indent=2)
