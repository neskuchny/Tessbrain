# -*- coding: utf-8 -*-
"""
HotpotQA (distractor) — публичный MULTI-HOP бенчмарк: ответ требует соединить
факты из 2 параграфов среди 10 (8 дистракторов). Узнаваемый стандарт для
«соединить факты» — как в запросе «когда пошло не так» нужно связать события.

2 руки:
  strict   — BM25 top-k параграфов + строгий ответ.
  analysis — ВСЕ параграфы + analysis-промпт (рассуждай по шагам, соединяй факты).

Метрика: match через судью (ответы короткие: yes/no/entity).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse

_HERE = os.path.dirname(os.path.abspath(__file__))
_MODEL = os.environ.get("HP_MODEL", "gpt-4o-mini")
_JUDGE = os.environ.get("HP_JUDGE", "gpt-4o-mini")


def _llm(system, user, model=None, max_tokens=400):
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


def load_hotpot(n: int) -> list:
    out = []
    off = 0
    while len(out) < n:
        url = (f"https://datasets-server.huggingface.co/rows?dataset=hotpotqa%2Fhotpot_qa"
               f"&config=distractor&split=validation&offset={off}&length=20")
        rows = json.load(urllib.request.urlopen(url, timeout=40)).get("rows", [])
        if not rows:
            break
        for r in rows:
            ex = r["row"]
            ctx = ex["context"]
            titles = ctx.get("title", [])
            sents = ctx.get("sentences", [])
            paras = []
            for i, ttl in enumerate(titles):
                body = " ".join(sents[i]) if i < len(sents) else ""
                paras.append(f"[{ttl}] {body}")
            out.append({"q": ex["question"], "gold": str(ex["answer"]),
                        "type": ex.get("type", ""), "paras": paras})
            if len(out) >= n:
                break
        off += 20
    return out


_STRICT = ("Answer the question using ONLY the provided paragraphs. Give a short, exact "
           "answer (entity or yes/no). If not answerable, say NOT MENTIONED.")
_ANALYSIS = ("You answer multi-hop questions. Reason STEP BY STEP, combining facts across "
             "the paragraphs to derive the answer. Then give one short final answer "
             "(entity or yes/no). If genuinely unanswerable, say CANNOT DETERMINE.")


def _bm25(query, docs, k):
    import math
    from collections import Counter
    toks = [re.findall(r"[a-z0-9]+", d.lower()) for d in docs]
    N = len(docs) or 1
    avgdl = sum(len(t) for t in toks) / N
    df = Counter()
    for t in toks:
        for w in set(t):
            df[w] += 1
    idf = {w: math.log(1 + (N - c + 0.5) / (c + 0.5)) for w, c in df.items()}
    q = re.findall(r"[a-z0-9]+", query.lower())
    sc = []
    for i, t in enumerate(toks):
        tf = Counter(t); dl = len(t) or 1; s = 0.0
        for w in q:
            if w in tf:
                s += idf.get(w, 0.0) * (tf[w] * 2.5) / (tf[w] + 1.5 * (1 - 0.75 + 0.75 * dl / avgdl))
        sc.append((s, i))
    sc.sort(reverse=True)
    return [i for _s, i in sc[:k]]


def arm_strict(item, topk=4):
    idx = sorted(_bm25(item["q"], item["paras"], topk))
    ctx = "\n\n".join(item["paras"][i] for i in idx)[:60000]
    return _llm(_STRICT, f"PARAGRAPHS:\n{ctx}\n\nQuestion: {item['q']}\nAnswer:")


def arm_analysis(item):
    ctx = "\n\n".join(item["paras"])[:60000]
    out = _llm(_ANALYSIS, f"PARAGRAPHS:\n{ctx}\n\nQuestion: {item['q']}\n\nReason and answer:",
               max_tokens=500)
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return lines[-1] if lines else out


def judge(item, pred):
    if not pred or pred.startswith("(err"):
        return False
    v = _llm("Grade if the model answer matches the gold answer (same entity / same yes-no). "
             "Reply ONLY YES or NO.",
             f"Q: {item['q']}\nGold: {item['gold']}\nModel: {pred}\nYES/NO:",
             model=_JUDGE, max_tokens=5)
    return bool(re.search(r"\byes\b", (v or "").lower()))


def run(n):
    qs = load_hotpot(n)
    arms = {"strict": arm_strict, "analysis": arm_analysis}
    res = {a: {"ok": 0, "by_type": {}} for a in arms}
    rows = []
    t0 = time.time()
    for i, item in enumerate(qs, 1):
        ty = item.get("type", "?")
        print(f"  [{i}/{len(qs)}] {ty} — {item['q'][:55]}", flush=True)
        row = {"type": ty, "q": item["q"], "gold": item["gold"]}
        for a, fn in arms.items():
            try:
                pred = fn(item)
            except Exception as e:
                pred = f"(err: {e})"
            ok = judge(item, pred)
            res[a]["ok"] += ok
            res[a]["by_type"].setdefault(ty, [0, 0])
            res[a]["by_type"][ty][0] += ok
            res[a]["by_type"][ty][1] += 1
            row[a] = {"ok": ok, "pred": pred[:150]}
            print(f"    {a:9s}: {'✅' if ok else '❌'} {pred[:60]}", flush=True)
        rows.append(row)
    N = len(qs) or 1
    summary = {a: {"accuracy": round(res[a]["ok"] / N, 4),
                   "by_type": {t: round(x / m, 3) for t, (x, m) in res[a]["by_type"].items()}}
               for a in arms}
    return {"benchmark": "hotpotqa", "n": N, "model": _MODEL,
            "elapsed_s": round(time.time() - t0, 1), "summary": summary, "rows": rows}


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    out = run(n)
    print(f"\n=== HotpotQA | n={out['n']} | {out['model']} ===")
    for a in ("strict", "analysis"):
        print(f"  {a:9s}: {out['summary'][a]['accuracy']:.3f}  {out['summary'][a]['by_type']}")
    print(f"elapsed: {out['elapsed_s']:.1f}s")
    json.dump(out, open(os.path.join(_HERE, "benchmark_hotpotqa.json"), "w"),
              ensure_ascii=False, indent=2)
