# -*- coding: utf-8 -*-
"""
LoCoMo — публичный multi-session memory бенчмарк (snap-research/locomo).

Проверяем гипотезу analysis-mode на УЗНАВАЕМОМ бенчмарке (главная забота —
multi-session) + БАЛАНС с adversarial (cat 5: вопрос без ответа → должен
воздержаться, а не выдумать — проверка риска over-compile).

Категории LoCoMo: 1=multi-hop, 2=temporal, 3=open-domain, 4=single-hop,
5=adversarial. Фокус: 1, 2, 5.

2 руки:
  strict   — top-k BM25 + строгий промпт («NOT MENTIONED если нет»).
  analysis — хроно-таймлайн всех сессий с датами + analysis-промпт (компилируй,
             рассуждай о порядке/изменениях; но если данных НЕТ — скажи «cannot
             determine» — баланс для adversarial).

Метрика: для cat 1-4 — match vs answer; cat 5 (adversarial) — верно = воздержание.
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
_MODEL = os.environ.get("LC_MODEL", "gpt-4o-mini")
_JUDGE = os.environ.get("LC_JUDGE", "gpt-4o-mini")
_CATS = {1: "multi-hop", 2: "temporal", 5: "adversarial"}


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


def _sessions(conv: dict) -> list:
    """Все ходы всех сессий: [{date, speaker, text}], сорт по номеру сессии."""
    out = []
    snums = sorted(int(k.split("_")[1]) for k in conv
                   if k.startswith("session_") and not k.endswith("date_time"))
    for n in snums:
        date = conv.get(f"session_{n}_date_time", "")
        for t in conv.get(f"session_{n}", []):
            out.append({"date": date, "speaker": t.get("speaker", ""),
                        "text": t.get("text", "")})
    return out


def load_questions(n_per_cat: int) -> list:
    from backend.core.eval.datasets import load_dataset
    data = load_dataset("locomo10")
    by_cat = {c: [] for c in _CATS}
    for di, item in enumerate(data):
        turns = _sessions(item["conversation"])
        for qa in item.get("qa", []):
            c = qa.get("category")
            if c in _CATS:
                gold = qa.get("answer", qa.get("adversarial_answer", ""))
                by_cat[c].append({"q": qa["question"], "gold": str(gold),
                                  "category": c, "turns": turns, "dialogue": di})
    rng = random.Random(42)
    out = []
    for c in _CATS:
        rng.shuffle(by_cat[c])
        out.extend(by_cat[c][:n_per_cat])
    return out


_STRICT = ("You are an assistant with memory of conversations. Answer using ONLY the "
           "provided memory. If it doesn't contain the answer, say exactly: NOT MENTIONED.")

_ANALYSIS = (
    "You are an analyst with full timestamped memory of two people's conversations. "
    "Memory is CHRONOLOGICAL with dates. ANALYZE: combine info across sessions, reason "
    "about ORDER and CHANGE over time. Give your BEST supported answer derived from the "
    "data. BUT if the data genuinely does NOT support any answer, say 'CANNOT DETERMINE' "
    "— do not invent. "
    # TEMPORAL RESOLUTION — измерено +0.20 на temporal-подмножестве (0.10→0.30):
    # модель якорилась на дату сессии и НЕ применяла смещение. Перенесено в продукт.
    "TEMPORAL RESOLUTION: each item has a DATE. Relative expressions ('yesterday', "
    "'last week', 'last month', 'last summer', 'next month') are relative to THAT item's "
    "date — APPLY THE OFFSET (yesterday = date minus 1 day; last week ~7 days before; "
    "last month/summer/year = the previous one) to get the ABSOLUTE date; do NOT output "
    "the item's own date unchanged. For 'now/current/latest' rely on the MOST RECENT "
    "dated items. End with one concise final answer line.")


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


def arm_strict(item, topk=15):
    turns = item["turns"]
    docs = [f"{t['speaker']}: {t['text']}" for t in turns]
    idx = sorted(_bm25(item["q"], docs, topk))
    mem = "\n".join(f"{turns[i]['speaker']}: {turns[i]['text']}" for i in idx)[:120000]
    return _llm(_STRICT, f"MEMORY:\n{mem}\n\nQuestion: {item['q']}\nAnswer:")


def arm_analysis(item):
    turns = item["turns"]  # уже хронологический (по сессиям)
    mem = "\n".join(f"[{t['date']}] {t['speaker']}: {t['text']}" for t in turns)[:120000]
    out = _llm(_ANALYSIS, f"TIMESTAMPED MEMORY (chronological):\n{mem}\n\n"
                          f"Question: {item['q']}\n\nAnalyze and answer:", max_tokens=600)
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return lines[-1] if lines else out


def _product_resolver():
    """ПРОДУКТОВЫЙ temporal_resolver (тот же код, что в _assemble_context) —
    бенчмарк меряет именно продуктовое поведение, не копию логики."""
    import importlib.util as ilu
    p = os.path.join(os.path.dirname(_HERE), "search", "temporal_resolver.py")
    spec = ilu.spec_from_file_location("_tessent_temporal_resolver", p)
    mod = ilu.module_from_spec(spec)
    sys.modules["_tessent_temporal_resolver"] = mod
    spec.loader.exec_module(mod)
    return mod


_TR_CACHE: list = []  # загруженный продуктовый resolver (один на прогон)


def arm_product(item):
    """ПРОДУКТ: analysis-промпт + code-resolved аннотации дат (как в проде:
    temporal_resolver.annotate против даты сессии). Замер целиком — что реально
    получит пользователь при включённом enable_answer_mode_router."""
    if not _TR_CACHE:
        _TR_CACHE.append(_product_resolver())
    tr = _TR_CACHE[0]
    turns = item["turns"]
    parts = []
    for t in turns:
        anchor = tr.parse_anchor_date(t["date"])
        parts.append(f"[{t['date']}] {t['speaker']}: {tr.annotate(t['text'], anchor)}")
    mem = "\n".join(parts)[:120000]
    out = _llm(_ANALYSIS, f"TIMESTAMPED MEMORY (chronological):\n{mem}\n\n"
                          f"Question: {item['q']}\n\nAnalyze and answer:", max_tokens=600)
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return lines[-1] if lines else out


# gated analysis — калиброванный (сильная дисциплина опоры). Промпт из роутера.
_ANALYSIS_GATED = (
    "You are an analyst with full timestamped memory (CHRONOLOGICAL, with dates). "
    "ANALYZE: combine info ACROSS sessions and reason about ORDER and CHANGE over time. "
    "EVIDENCE RULE (critical): answer ONLY if the memory contains DIRECT supporting "
    "evidence (explicit statement, or facts combinable by clear multi-hop reasoning). "
    "If the specific thing asked (value, feeling, opinion, fact, number) is NOT "
    "explicitly present — even if RELATED topics are discussed — say exactly 'CANNOT "
    "DETERMINE'. Do NOT infer unstated beliefs/preferences/motivations. Do NOT guess. "
    "End with one concise final answer.")


def arm_analysis_gated(item):
    turns = item["turns"]
    mem = "\n".join(f"[{t['date']}] {t['speaker']}: {t['text']}" for t in turns)[:120000]
    out = _llm(_ANALYSIS_GATED, f"TIMESTAMPED MEMORY (chronological):\n{mem}\n\n"
                                f"Question: {item['q']}\n\nAnalyze and answer:", max_tokens=600)
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return lines[-1] if lines else out


# guarded analysis — АРХИТЕКТУРНЫЙ гейт (а не промпт). Урок: единый промпт не может
# одновременно «жёстко не выдумывать» и «смело компилировать мультихоп» (gated
# провалился: multi-hop 0.6→0.3). Решение — РАЗДЕЛИТЬ решение и ответ:
#   шаг 1: отдельный дешёвый evidence-gate (YES/NO — есть ли опора, мультихоп=YES);
#   шаг 2: при YES — ХОРОШИЙ analysis-компилятор (_ANALYSIS); при NO — CANNOT DETERMINE.
# Цель: adversarial обратно к ~1.0, сохранив multi-hop/temporal выигрыш analysis.
_EVIDENCE_GATE = (
    "You are a STRICT evidence checker. Given timestamped memory of conversations and a "
    "question, decide if the memory contains enough to answer THIS SPECIFIC question. "
    "MULTI-HOP COUNTS: if the answer can be derived by COMBINING explicitly stated facts "
    "across sessions, that is YES. But if the specific thing asked (a value, feeling, "
    "opinion, name, number, date, event) is NEVER stated and cannot be derived from "
    "stated facts — even if RELATED topics are discussed — answer NO. Reply ONLY YES or NO.")


def arm_analysis_guarded(item):
    turns = item["turns"]
    mem = "\n".join(f"[{t['date']}] {t['speaker']}: {t['text']}" for t in turns)[:120000]
    verdict = _llm(_EVIDENCE_GATE, f"MEMORY:\n{mem}\n\nQuestion: {item['q']}\nYES/NO:",
                   max_tokens=5)
    if not re.search(r"\byes\b", (verdict or "").lower()):
        return "CANNOT DETERMINE."
    out = _llm(_ANALYSIS, f"TIMESTAMPED MEMORY (chronological):\n{mem}\n\n"
                          f"Question: {item['q']}\n\nAnalyze and answer:", max_tokens=600)
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return lines[-1] if lines else out


def _abstained(pred: str) -> bool:
    return bool(re.search(r"not mentioned|cannot determine|don'?t know|no information|"
                          r"not enough|unanswerable|insufficient", (pred or "").lower()))


def judge(item, pred):
    if item["category"] == 5:
        # adversarial: верно = воздержался (не выдал adversarial_answer/выдумку)
        return _abstained(pred)
    if not pred or pred.startswith("(err"):
        return False
    v = _llm("Grade if model answer is consistent with the gold answer (same key fact). "
             "Reply ONLY YES or NO.",
             f"Q: {item['q']}\nGold: {item['gold']}\nModel: {pred}\nYES/NO:",
             model=_JUDGE, max_tokens=5)
    return bool(re.search(r"\byes\b", (v or "").lower()))


def run(n_per_cat):
    qs = load_questions(n_per_cat)
    # gated (промпт-гейт) уже измерен и провалился (multi-hop 0.6→0.3) — оставлен в
    # модуле как контр-пример. В прогоне сравниваем strict/analysis с АРХИТЕКТУРНЫМ
    # guarded (отдельный evidence-gate + хороший компилятор).
    # gated/guarded уже измерены (контр-примеры, см. doc). Сравниваем ПРОДУКТ
    # (analysis + code-resolved даты, как при включённом enable_answer_mode_router)
    # против strict и голого analysis.
    arms = {"strict": arm_strict, "analysis": arm_analysis, "product": arm_product}
    res = {a: {"ok": 0, "by_cat": {}} for a in arms}
    rows = []
    t0 = time.time()
    for i, item in enumerate(qs, 1):
        cat = _CATS[item["category"]]
        print(f"  [{i}/{len(qs)}] {cat} — {item['q'][:55]}", flush=True)
        row = {"category": cat, "q": item["q"], "gold": item["gold"]}
        for a, fn in arms.items():
            try:
                pred = fn(item)
            except Exception as e:
                pred = f"(err: {e})"
            ok = judge(item, pred)
            res[a]["ok"] += ok
            res[a]["by_cat"].setdefault(cat, [0, 0])
            res[a]["by_cat"][cat][0] += ok
            res[a]["by_cat"][cat][1] += 1
            row[a] = {"ok": ok, "pred": pred[:200]}
            print(f"    {a:9s}: {'✅' if ok else '❌'} {pred[:65]}", flush=True)
        rows.append(row)
    N = len(qs) or 1
    summary = {a: {"accuracy": round(res[a]["ok"] / N, 4),
                   "by_cat": {c: round(x / m, 3) for c, (x, m) in res[a]["by_cat"].items()}}
               for a in arms}
    return {"benchmark": "locomo", "n": N, "model": _MODEL,
            "elapsed_s": round(time.time() - t0, 1), "summary": summary, "rows": rows}


if __name__ == "__main__":
    npc = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    out = run(npc)
    print(f"\n=== LoCoMo | n={out['n']} | {out['model']} ===")
    for a in ("strict", "analysis", "product"):
        print(f"  {a:9s}: {out['summary'][a]['accuracy']:.3f}  {out['summary'][a]['by_cat']}")
    print(f"elapsed: {out['elapsed_s']:.1f}s")
    json.dump(out, open(os.path.join(_HERE, "benchmark_locomo.json"), "w"),
              ensure_ascii=False, indent=2)
