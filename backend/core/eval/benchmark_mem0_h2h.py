# -*- coding: utf-8 -*-
"""
Mem0 head-to-head на LongMemEval-oracle — впервые в сессии прямое сравнение
НАШЕГО ретрива и Mem0 на одних данных, тем же судьёй.

4 руки:
  none   — без памяти (baseline; должен быть низким → бенч валиден)
  dump   — вся история в контекст (long-context baseline)
  ours   — наш hybrid: BM25 stdlib + temporal_rerank
  mem0   — официальный Mem0 (extraction + векторный ретрив через chromadb)

Замер: accuracy через LLM-судью (gpt-4o-mini), одинаково для всех рук.

Запуск: OPENAI_API_KEY, n=10 (на больше нет времени — каждый Mem0-add это
LLM-вызов; ~500 ходов/айтем → ~10K вызовов на весь n=10).
"""
from __future__ import annotations

import importlib.util
import json
import os
import random
import re
import sys
import shutil
import time
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
_MODEL = os.environ.get("HTH_MODEL", "gpt-4o-mini")
_JUDGE = os.environ.get("HTH_JUDGE", "gpt-4o-mini")
_MAX_DUMP = int(os.environ.get("HTH_MAXDUMP", "120000"))
_TOPK = int(os.environ.get("HTH_TOPK", "12"))


# ---------- данные ----------
def load_oracle(n: int) -> list:
    # Раньше здесь был жёсткий путь /tmp: файл клали руками при прогоне, и
    # повторить опубликованное сравнение через месяц было нельзя.
    from backend.core.eval.datasets import load_dataset
    data = load_dataset("longmemeval_oracle")
    random.Random(42).shuffle(data)
    return data[:n]


def _turns(item: dict) -> list:
    out = []
    for si, sess in enumerate(item.get("haystack_sessions", [])):
        for t in sess:
            out.append({"role": t.get("role", ""), "content": t.get("content", ""),
                        "session": si})
    return out


# ---------- LLM ----------
def _llm(system: str, user: str, model=None, max_tokens=400) -> str:
    body = json.dumps({
        "model": model or _MODEL,
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
            return json.load(urllib.request.urlopen(req, timeout=90))["choices"][0]["message"]["content"].strip()
        except Exception:
            time.sleep(2 * (attempt + 1))
    return "(llm_error)"


_ANS = ("You are a helpful assistant with memory of prior conversations. "
        "Answer using ONLY the provided history. If the history doesn't contain "
        "the answer, say exactly: NOT MENTIONED.")


# ---------- BM25 stdlib (как в нашем бенче) ----------
def _tok(s: str) -> list:
    return re.findall(r"[a-zа-я0-9]+", (s or "").lower())


def bm25_topk(query: str, docs: list, k: int) -> list:
    import math
    from collections import Counter
    toks = [_tok(d) for d in docs]
    N = len(docs) or 1
    avgdl = (sum(len(t) for t in toks) / N) or 1.0
    df = Counter()
    for t in toks:
        for w in set(t):
            df[w] += 1
    idf = {w: math.log(1 + (N - c + 0.5) / (c + 0.5)) for w, c in df.items()}
    q = _tok(query)
    scores = []
    for i, t in enumerate(toks):
        tf = Counter(t); dl = len(t) or 1; s = 0.0
        for w in q:
            if w in tf:
                s += idf.get(w, 0.0) * (tf[w] * 2.5) / (tf[w] + 1.5 * (1 - 0.75 + 0.75 * dl / avgdl))
        scores.append((s, i))
    scores.sort(reverse=True)
    return [i for _s, i in scores[:k]]


# ---------- наша temporal_rerank ----------
def _load_temporal():
    p = os.path.join(_ROOT, "backend", "core", "search", "temporal_rerank.py")
    s = importlib.util.spec_from_file_location("tr_hth", p)
    m = importlib.util.module_from_spec(s)
    sys.modules["tr_hth"] = m
    s.loader.exec_module(m)
    return m


# ---------- 4 руки ----------
def arm_none(item: dict) -> str:
    return _llm(_ANS, f"Question: {item['question']}\nAnswer:")


def arm_dump(item: dict) -> str:
    turns = _turns(item)
    hist = "\n".join(f"[{t['role']}] {t['content']}" for t in turns)[:_MAX_DUMP]
    return _llm(_ANS, f"CONVERSATION HISTORY:\n{hist}\n\nQuestion: {item['question']}\nAnswer:")


def arm_ours(item: dict) -> str:
    """BM25-ретрив + наш temporal_rerank (даты из haystack_dates).
    Если LongMemEval oracle даёт даты на уровень сессий — используем их."""
    turns = _turns(item)
    dates = item.get("haystack_dates", []) or []
    docs = [f"{t['role']}: {t['content']}" for t in turns]
    # сначала BM25 topk
    idx = bm25_topk(item["question"], docs, _TOPK * 2)
    # обогатим dates через "data" — для temporal-rerank
    tr = _load_temporal()

    class _Fused:
        __slots__ = ("doc_id", "rrf_score", "data", "sources")

        def __init__(self, doc_id, score, data):
            self.doc_id, self.rrf_score, self.data, self.sources = doc_id, score, data, []

    fused = []
    for rank, i in enumerate(idx):
        si = turns[i]["session"]
        dt = dates[si] if si < len(dates) else ""
        # форматируем дату для парсера (он понимает slash-формат)
        fused.append(_Fused(str(i), 1.0 / (1 + rank),
                            {"metadata": {"date": dt}}))
    # ререрив
    boosted = tr.apply_temporal_rerank(fused)
    final = [int(b.doc_id) for b in boosted[:_TOPK]]
    final.sort()
    picked = [turns[i] for i in final]
    hist = "\n".join(f"[{t['role']}] {t['content']}" for t in picked)[:_MAX_DUMP]
    return _llm(_ANS, f"RELEVANT MEMORY:\n{hist}\n\nQuestion: {item['question']}\nAnswer:")


# --- Mem0 ---
_mem0_instance = {"m": None, "uid_counter": [0]}


def _get_mem0(item_qid: str):
    from mem0 import Memory
    workdir = f"/tmp/mem0_hth_{item_qid}"
    shutil.rmtree(workdir, ignore_errors=True)
    config = {
        "vector_store": {"provider": "chroma",
                         "config": {"path": workdir,
                                    "collection_name": "lme"}},
        "llm": {"provider": "openai",
                "config": {"model": _MODEL, "temperature": 0.0}},
        "embedder": {"provider": "openai",
                     "config": {"model": "text-embedding-3-small"}},
    }
    return Memory.from_config(config), workdir


def arm_mem0(item: dict) -> str:
    """Реальный Mem0: ingest всех ходов → search → отдать как контекст."""
    qid = str(item.get("question_id", "x"))
    user_id = f"u_{qid}"
    try:
        m, workdir = _get_mem0(qid)
        # ingest: каждый ход как сообщение от роли (Mem0 извлекает факты)
        turns = _turns(item)
        # Mem0 принимает messages-list — отдадим всё одной сессией
        # (если очень длинно — режем по 50 ходов batch'ами)
        for i in range(0, len(turns), 50):
            chunk = turns[i:i + 50]
            messages = [{"role": t["role"], "content": t["content"]} for t in chunk
                        if t["content"]]
            try:
                m.add(messages, user_id=user_id)
            except Exception as e:
                # если на одном ходу падает (rate limit и пр.) — продолжаем
                if "rate" in str(e).lower():
                    time.sleep(5)
                continue
        # search: достаём top релевантных фактов
        res = m.search(item["question"], filters={"user_id": user_id}, limit=_TOPK)
        memories = res.get("results", []) if isinstance(res, dict) else (res or [])
        if not memories:
            return _llm(_ANS, f"MEMORY: (empty)\n\nQuestion: {item['question']}\nAnswer:")
        mem_text = "\n".join(f"- {x.get('memory', '')}" for x in memories)
        return _llm(_ANS, f"MEMORY:\n{mem_text}\n\nQuestion: {item['question']}\nAnswer:")
    except Exception as e:
        return f"(mem0_error: {type(e).__name__}: {str(e)[:120]})"
    finally:
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except Exception:
            pass


# ---------- судья ----------
_JUDGE_SYS = ("You grade whether a model answer is correct given the gold answer. "
              "Reply ONLY 'YES' if consistent (same key fact), else 'NO'. Ignore phrasing.")


def judge(item: dict, pred: str) -> bool:
    if str(item.get("question_id", "")).endswith("_abs"):
        return bool(re.search(r"not mentioned|don'?t know|no information", (pred or "").lower()))
    if pred.startswith("(mem0_error") or pred.startswith("(llm_error"):
        return False
    v = _llm(_JUDGE_SYS,
             f"Question: {item['question']}\nGold: {item['answer']}\nModel: {pred}\nYES/NO:",
             model=_JUDGE, max_tokens=5)
    return bool(re.search(r"\byes\b", (v or "").lower()))


# ---------- прогон ----------
def run(n: int) -> dict:
    data = load_oracle(n)
    arms = {"none": arm_none, "dump": arm_dump, "ours": arm_ours, "mem0": arm_mem0}
    res = {a: {"correct": 0, "by_type": {}} for a in arms}
    rows = []
    t0 = time.time()
    for idx, item in enumerate(data, 1):
        qt = item.get("question_type", "?")
        print(f"  [{idx}/{len(data)}] {qt} — {item['question'][:60]}", flush=True)
        row = {"id": item.get("question_id"), "type": qt, "question": item["question"],
               "gold": item["answer"]}
        for a, fn in arms.items():
            try:
                pred = fn(item)
            except Exception as e:
                pred = f"(error: {e})"
            ok = judge(item, pred)
            res[a]["correct"] += ok
            res[a]["by_type"].setdefault(qt, [0, 0])
            res[a]["by_type"][qt][0] += ok
            res[a]["by_type"][qt][1] += 1
            row[a] = {"ok": ok, "pred": pred[:300]}
            print(f"    {a:6s}: {'✅' if ok else '❌'} {pred[:80]}", flush=True)
        rows.append(row)
    N = len(data) or 1
    summary = {a: {"accuracy": round(res[a]["correct"] / N, 4),
                   "by_type": {t: round(c / n2, 3) for t, (c, n2) in res[a]["by_type"].items()}}
               for a in arms}
    return {"benchmark": "longmemeval_oracle_h2h", "model": _MODEL, "judge": _JUDGE,
            "n": N, "elapsed_s": round(time.time() - t0, 1),
            "summary": summary, "rows": rows}


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    out = run(n)
    print(f"\n=== {out['benchmark']} | n={out['n']} | model={out['model']} ===")
    for a, s in out["summary"].items():
        print(f"  {a:6s}: {s['accuracy']:.4f}")
    print(f"elapsed: {out['elapsed_s']:.1f}s")
    p = os.path.join(_HERE, "benchmark_mem0_h2h.json")
    json.dump(out, open(p, "w"), ensure_ascii=False, indent=2)
    print(f"полный прогон → {p}")
