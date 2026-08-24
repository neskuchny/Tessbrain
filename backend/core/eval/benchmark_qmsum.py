# -*- coding: utf-8 -*-
"""
Бенчмарк QMSum (query-focused meeting summarization) — «наша система на прочность»
на рыночном бенчмарке нашего домена (встреча→смысл).

Что меряем честно:
1) СТАНДАРТНАЯ метрика QMSum — ROUGE-1/2/L (F1) сгенерированного query-focused
   резюме против золотого. Это сопоставимо с публичными бейзлайнами.
2) НАШ угол — дисциплина «думать как человек» (specify) ON vs OFF на том же
   бенчмарке: помогает/мешает/нейтрально. (Скелет-заземление в граф здесь не
   применимо — нет графа компании; меряем ровно вклад промпт-дисциплины.)

Данные: pszemraj/qmsum-cleaned (no-prefix, validation — там есть золото).
Запуск: GATE0_MODEL=gpt-4o-mini|gemini-3.1-flash-lite, OPENAI/GEMINI ключи.
ROUGE — чистый stdlib (без внешних пакетов), считается локально.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_MODEL = os.environ.get("GATE0_MODEL", "gpt-4o-mini")
_MAX_CHARS = int(os.environ.get("QMSUM_MAXCHARS", "80000"))  # обрезка длинных транскриптов


# ---------- данные ----------
def fetch_qmsum(n: int, split: str = "validation") -> list:
    out = []
    off = 0
    while len(out) < n:
        url = (f"https://datasets-server.huggingface.co/rows?dataset=pszemraj%2Fqmsum-cleaned"
               f"&config=no-prefix&split={split}&offset={off}&length=20")
        r = json.load(urllib.request.urlopen(url, timeout=40))
        rows = r.get("rows", [])
        if not rows:
            break
        for row in rows:
            ex = row["row"]
            if ex.get("output") and ex.get("prompt") and ex.get("input"):
                out.append({"id": ex["id"], "query": ex["prompt"],
                            "transcript": ex["input"], "gold": ex["output"]})
            if len(out) >= n:
                break
        off += 20
    return out


# ---------- ROUGE (stdlib) ----------
def _toks(s: str) -> list:
    return re.findall(r"[a-zа-я0-9]+", (s or "").lower())


def _f1(overlap: int, npred: int, ngold: int) -> float:
    if npred == 0 or ngold == 0:
        return 0.0
    p, r = overlap / npred, overlap / ngold
    return 0.0 if p + r == 0 else round(2 * p * r / (p + r), 4)


def _ngram_overlap(a: list, b: list, n: int) -> tuple:
    from collections import Counter
    ga = Counter(tuple(a[i:i + n]) for i in range(len(a) - n + 1))
    gb = Counter(tuple(b[i:i + n]) for i in range(len(b) - n + 1))
    overlap = sum((ga & gb).values())
    return overlap, sum(ga.values()), sum(gb.values())


def _lcs(a: list, b: list) -> int:
    # классический LCS длиной — для ROUGE-L
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            cur[j] = prev[j - 1] + 1 if ai == b[j - 1] else max(prev[j], cur[j - 1])
        prev = cur
    return prev[len(b)]


def rouge(pred: str, gold: str) -> dict:
    p, g = _toks(pred), _toks(gold)
    o1, np1, ng1 = _ngram_overlap(p, g, 1)
    o2, np2, ng2 = _ngram_overlap(p, g, 2)
    lcs = _lcs(p, g)
    return {"rouge1": _f1(o1, np1, ng1), "rouge2": _f1(o2, np2, ng2),
            "rougeL": _f1(lcs, len(p), len(g))}


# ---------- LLM ----------
def _llm_openai(system: str, user: str, model: str) -> str:
    key = os.environ["OPENAI_API_KEY"]
    body = json.dumps({"model": model, "messages": [
        {"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.2, "max_tokens": 400}).encode()
    req = urllib.request.Request("https://api.openai.com/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=120))["choices"][0]["message"]["content"].strip()


def _llm_gemini(system: str, user: str, model: str) -> str:
    key = os.environ.get("GEMINI_API_KEY") or os.environ["GOOGLE_API_KEY"]
    body = json.dumps({"system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 900}}).encode()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    r = json.load(urllib.request.urlopen(req, timeout=150))
    parts = r["candidates"][0]["content"].get("parts", [])
    return " ".join(p.get("text", "") for p in parts).strip()


def _llm(system: str, user: str) -> str:
    if _MODEL.startswith("gemini") or _MODEL.startswith("gemma"):
        return _llm_gemini(system, user, _MODEL)
    return _llm_openai(system, user, _MODEL)


_BASE_SYSTEM = (
    "You are an assistant that writes a SHORT query-focused summary based on a "
    "meeting transcript. Use ONLY what is actually said in the transcript. "
    "2-4 sentences, focused on the question. Respond in English only."
)
# Англоязычная директива в КОНЕЦ (и для ON тоже) — бенчмарк англоязычный, иначе
# ответ на русском даёт near-zero ROUGE против английского золота (артефакт, не
# качество). Ставим последней, чтобы перебить язык дисциплины.
_EN_TAIL = "\n\nIMPORTANT: Write the summary in English only."


def _load_discipline():
    import importlib.util
    p = os.path.join(_ROOT, "backend", "core", "think", "human_thinking.py")
    s = importlib.util.spec_from_file_location("ht_bench", p)
    m = importlib.util.module_from_spec(s)
    sys.modules["ht_bench"] = m
    s.loader.exec_module(m)
    return m


def summarize(query: str, transcript: str, system: str) -> str:
    user = (f"ВОПРОС: {query}\n\nТРАНСКРИПТ ВСТРЕЧИ:\n{transcript[:_MAX_CHARS]}\n\n"
            f"Краткое резюме по вопросу (только из транскрипта):")
    return _llm(system, user)


def run(n: int = 20) -> dict:
    ht = _load_discipline()
    off_system = _BASE_SYSTEM + _EN_TAIL
    on_system = ht.with_thinking_discipline(_BASE_SYSTEM, "specify") + _EN_TAIL
    data = fetch_qmsum(n)
    rows = []
    for ex in data:
        off = summarize(ex["query"], ex["transcript"], off_system)
        on = summarize(ex["query"], ex["transcript"], on_system)
        rows.append({"id": ex["id"], "query": ex["query"], "gold": ex["gold"],
                     "OFF": off, "ON": on,
                     "rouge_OFF": rouge(off, ex["gold"]), "rouge_ON": rouge(on, ex["gold"])})

    def _mean(key, metric):
        vals = [r[key][metric] for r in rows]
        return round(sum(vals) / len(vals), 4) if vals else 0.0

    summary = {m: {"OFF": _mean("rouge_OFF", m), "ON": _mean("rouge_ON", m)}
               for m in ("rouge1", "rouge2", "rougeL")}
    return {"model": _MODEL, "n": len(rows), "max_chars": _MAX_CHARS,
            "summary": summary, "rows": rows}


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    res = run(n)
    print(f"QMSum query-focused | модель={res['model']} | n={res['n']} | обрезка={res['max_chars']}c")
    print("\nROUGE (F1), среднее:")
    for m, v in res["summary"].items():
        print(f"  {m}: OFF={v['OFF']:.4f}  ON={v['ON']:.4f}  Δ(ON-OFF)={v['ON']-v['OFF']:+.4f}")
    safe = re.sub(r"[^a-z0-9.]+", "_", res["model"].lower())
    p = os.path.join(_HERE, f"benchmark_qmsum_{safe}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(f"\nполный прогон → {p}")
