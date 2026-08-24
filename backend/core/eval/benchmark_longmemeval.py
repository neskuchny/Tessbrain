# -*- coding: utf-8 -*-
"""
LongMemEval — 3-рукий харнесс памяти (стдлиб-загрузчик, без тяжёлых зависимостей).

LongMemEval (ICLR'25) — отраслевой бенчмарк памяти ассистента над длинной
мульти-сессионной историей. На нём меряют Mem0/Zep/ByteRover. Здесь — честное
3-рукое сравнение (см. MEMORY_TEST_PLAN.md §0):

  A (none)       — чистый LLM, БЕЗ истории (только параметрика).
  B (dump)       — вся история свалена в контекст (long-context baseline).
  C (retrieval)  — извлекаем релевантные ходы и подаём только их.
                   ЗДЕСЬ — стдлиб-BM25 как СТЕНД-ИН «памяти» (песочница без
                   networkx/эмбеддингов). В среде с deps сюда подключается НАШ
                   стек (graph_builder + hybrid_search_orchestrator) — функция
                   `arm_retrieval` заменяется одной строкой.

Оценка — LLM-судья (как в оригинальном LongMemEval: ответы свободной формы).
Абстенция (question_id …_abs): верно = модель ВОЗДЕРЖАЛАСЬ.

Данные: HF xiaowu0162/longmemeval (oracle 15MB по умолчанию; s — 278MB, опц.).
Запуск: GATE0_MODEL=gpt-4o-mini|gemini-3.1-flash-lite|gpt-4o; OPENAI/GEMINI ключи.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
_MODEL = os.environ.get("GATE0_MODEL", "gpt-4o-mini")
_JUDGE_MODEL = os.environ.get("LME_JUDGE", "gpt-4o-mini")
# Кэш датасета переживает перезагрузку: /tmp чистится, и опубликованные
# числа переставали воспроизводиться.
try:
    from backend.core.eval.datasets import cache_dir as _cache_dir
    _CACHE = _cache_dir()
except Exception:
    _CACHE = "/tmp"
_MAX_DUMP_CHARS = int(os.environ.get("LME_MAXDUMP", "120000"))
_TOPK = int(os.environ.get("LME_TOPK", "12"))


# ---------- загрузка ----------
def _download(variant: str) -> str:
    path = os.path.join(_CACHE, f"longmemeval_{variant}.json")
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return path
    url = f"https://huggingface.co/datasets/xiaowu0162/longmemeval/resolve/main/longmemeval_{variant}"
    urllib.request.urlretrieve(url, path)
    return path


def load(variant: str, n: int) -> list:
    with open(_download(variant), "r", encoding="utf-8") as f:
        data = json.load(f)
    # Файл СГРУППИРОВАН по question_type (первые 133 — temporal-reasoning и т.д.).
    # Детерминированно перемешиваем (seed=42), чтобы выборка покрывала ВСЕ типы
    # (temporal/multi-session/knowledge-update/single-session*/abstention).
    import random
    random.Random(42).shuffle(data)
    # LME_TYPE=temporal-reasoning — фильтр по одному типу (дешёвый таргет-прогон).
    qtype = os.environ.get("LME_TYPE")
    if qtype:
        data = [x for x in data if x.get("question_type") == qtype]
    return data[:n]


def _turns(item: dict) -> list:
    """Плоский список ходов истории: [{'role','content','session','has_answer'}]."""
    out = []
    for si, sess in enumerate(item.get("haystack_sessions", [])):
        for t in sess:
            out.append({"role": t.get("role", ""), "content": t.get("content", ""),
                        "session": si, "has_answer": bool(t.get("has_answer"))})
    return out


# ---------- BM25 (стдлиб) — стенд-ин «памяти» для руки C ----------
def _tok(s: str) -> list:
    return re.findall(r"[a-zа-я0-9]+", (s or "").lower())


def bm25_topk(query: str, docs: list, k: int, k1=1.5, b=0.75) -> list:
    """Вернуть индексы top-k docs по BM25 к query. docs: list[str]. Чистый stdlib."""
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
        tf = Counter(t)
        dl = len(t) or 1
        s = 0.0
        for w in q:
            if w in tf:
                s += idf.get(w, 0.0) * (tf[w] * (k1 + 1)) / (tf[w] + k1 * (1 - b + b * dl / avgdl))
        scores.append((s, i))
    scores.sort(reverse=True)
    return [i for _s, i in scores[:k]]


# ---------- LLM ----------
# Учёт токенов прогона. Раньше прогоны НЕ писали usage — и «цена за
# правильный ответ» считалась только в вызовах, без денег. Ответ API
# несёт usage бесплатно — просто складываем. Пишется в результат прогона
# (поле usage), чтобы точность больше не публиковалась без цены.
USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}


def _count_usage(u: dict) -> None:
    if not isinstance(u, dict):
        return
    USAGE["prompt_tokens"] += int(u.get("prompt_tokens")
                                  or u.get("promptTokenCount") or 0)
    USAGE["completion_tokens"] += int(u.get("completion_tokens")
                                      or u.get("candidatesTokenCount") or 0)
    USAGE["calls"] += 1


def _llm_openai(system, user, model, max_tokens=400):
    key = os.environ["OPENAI_API_KEY"]
    body = json.dumps({"model": model, "messages": [
        {"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.0, "max_tokens": max_tokens}).encode()
    req = urllib.request.Request("https://api.openai.com/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    r = json.load(urllib.request.urlopen(req, timeout=120))
    _count_usage(r.get("usage") or {})
    return r["choices"][0]["message"]["content"].strip()


def _llm_gemini(system, user, model, max_tokens=800):
    key = os.environ.get("GEMINI_API_KEY") or os.environ["GOOGLE_API_KEY"]
    body = json.dumps({"system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": max_tokens}}).encode()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    r = json.load(urllib.request.urlopen(req, timeout=150))
    _count_usage(r.get("usageMetadata") or {})
    cands = r.get("candidates") or []
    if not cands:
        return ""
    parts = (cands[0].get("content") or {}).get("parts", []) or []
    return " ".join(p.get("text", "") for p in parts).strip()


def _llm_compat(system, user, model, max_tokens=400):
    """OpenAI-совместимый endpoint по ЛЮБОМУ base_url.

    Нужен, чтобы мерить прогоны на локальной модели (vLLM/Ollama в
    закрытом контуре) и на открытых моделях у совместимых провайдеров —
    тем же харнессом и теми же формулами, что облачные. Иначе вопрос
    «сколько качества теряет закрытый контур» остаётся без данных.

    Конфигурация: LME_COMPAT_BASE_URL (+ LME_COMPAT_KEY). Учёт токенов
    тот же — ответ несёт usage, значит цена считается и здесь.
    """
    base = (os.environ.get("LME_COMPAT_BASE_URL") or "").rstrip("/")
    key = os.environ.get("LME_COMPAT_KEY", "")
    body = json.dumps({"model": model, "messages": [
        {"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.0, "max_tokens": max_tokens}).encode()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(f"{base}/chat/completions", data=body,
                                 headers=headers)
    r = json.load(urllib.request.urlopen(req, timeout=180))
    _count_usage(r.get("usage") or {})
    return r["choices"][0]["message"]["content"].strip()


def _llm(system, user, model=None, max_tokens=400):
    m = model or _MODEL
    last = ""
    for attempt in range(3):  # устойчивость к таймаутам/перегрузке — не роняем долгий прогон
        try:
            if os.environ.get("LME_COMPAT_BASE_URL"):
                return _llm_compat(system, user, m, max_tokens=max_tokens)
            if m.startswith("gemini") or m.startswith("gemma"):
                return _llm_gemini(system, user, m, max_tokens=max(800, max_tokens))
            return _llm_openai(system, user, m, max_tokens=max_tokens)
        except Exception as e:
            last = str(e)
            import time as _t
            _t.sleep(2 * (attempt + 1))
    return f"(llm_error: {last[:80]})"


_ANSWER_SYSTEM = (
    "You are a helpful assistant with memory of prior conversations with the user. "
    "Answer the user's question concisely using ONLY the provided history. If the "
    "history does not contain the answer, say exactly: NOT MENTIONED."
)


def _fmt_turns(turns: list) -> str:
    return "\n".join(f"[{t['role']}] {t['content']}" for t in turns)


# ---------- три руки ----------
def arm_none(item: dict) -> str:
    return _llm(_ANSWER_SYSTEM, f"Question: {item['question']}\nAnswer:")


def arm_dump(item: dict) -> str:
    hist = _fmt_turns(_turns(item))[:_MAX_DUMP_CHARS]
    return _llm(_ANSWER_SYSTEM, f"CONVERSATION HISTORY:\n{hist}\n\nQuestion: {item['question']}\nAnswer:")


def arm_retrieval(item: dict) -> str:
    """СТЕНД-ИН памяти: BM25 top-k ходов. Заменить на наш hybrid_search в env с deps."""
    turns = _turns(item)
    docs = [f"{t['role']}: {t['content']}" for t in turns]
    idx = bm25_topk(item["question"], docs, _TOPK)
    picked = [turns[i] for i in sorted(idx)]
    hist = _fmt_turns(picked)[:_MAX_DUMP_CHARS]
    return _llm(_ANSWER_SYSTEM, f"RELEVANT MEMORY:\n{hist}\n\nQuestion: {item['question']}\nAnswer:")


_hybrid_mod = {"m": None, "tried": False}


def _load_hybrid():
    """Ленивая загрузка longmemeval_hybrid_arm БЕЗ зависимости от пакета backend.*
    (скрипт запускается из tessent_brain/, абсолютный импорт не находит пакет)."""
    if _hybrid_mod["tried"]:
        return _hybrid_mod["m"]
    _hybrid_mod["tried"] = True
    try:
        import importlib.util
        p = os.path.join(_HERE, "longmemeval_hybrid_arm.py")
        s = importlib.util.spec_from_file_location("lme_hybrid", p)
        m = importlib.util.module_from_spec(s)
        sys.modules["lme_hybrid"] = m
        # модулю нужны _turns/_fmt_turns/_MAX_DUMP_CHARS из нас — пробросим через sys.modules
        sys.modules["backend.core.eval.benchmark_longmemeval"] = sys.modules[__name__]
        s.loader.exec_module(m)
        _hybrid_mod["m"] = m
    except Exception as e:
        _hybrid_mod["err"] = e
    return _hybrid_mod["m"]


def arm_hybrid_optional(item: dict) -> str:
    """РУКА C-real: гибридный ретрив (BM25+вектор+RRF) — алгоритм нашего
    HybridSearchOrchestrator. Доступна, когда установлены rank_bm25+numpy+
    sentence-transformers. Иначе → fallback на BM25-стенд-ин."""
    m = _load_hybrid()
    if m is None:
        return arm_retrieval(item) + f"  [hybrid_fallback: {_hybrid_mod.get('err','?')}]"
    try:
        return m.arm_hybrid(item, _llm, _ANSWER_SYSTEM)
    except Exception as e:
        return arm_retrieval(item) + f"  [hybrid_runtime_err: {e}]"


def arm_temporal_optional(item: dict) -> str:
    """РУКА D: гибрид + темпоральная ось «когда сказано». Доступна с теми же deps."""
    m = _load_hybrid()
    if m is None:
        return arm_retrieval(item) + f"  [temporal_fallback: {_hybrid_mod.get('err','?')}]"
    try:
        return m.arm_temporal(item, _llm, _ANSWER_SYSTEM)
    except Exception as e:
        return arm_retrieval(item) + f"  [temporal_runtime_err: {e}]"


def arm_facts_optional(item: dict) -> str:
    """РУКА F: извлечение фактов в стиле Mem0 → ретрив по фактам (pure-stdlib ретрив,
    извлечение через LLM с кэшем). Главный рычаг разрыва с Mem0."""
    m = _load_hybrid()
    if m is None:
        return arm_retrieval(item) + f"  [facts_fallback: {_hybrid_mod.get('err','?')}]"
    try:
        return m.arm_facts(item, _llm, _ANSWER_SYSTEM)
    except Exception as e:
        return arm_retrieval(item) + f"  [facts_runtime_err: {e}]"


def arm_sweep_optional(item: dict) -> str:
    """РУКА S: агрегирующий вопрос → сессии с хитами ЦЕЛИКОМ (лечение
    multi-session 0.5 из живого прогона); остальные → temporal."""
    m = _load_hybrid()
    if m is None:
        return arm_retrieval(item) + f"  [sweep_fallback: {_hybrid_mod.get('err','?')}]"
    try:
        return m.arm_sweep(item, _llm, _ANSWER_SYSTEM)
    except Exception as e:
        return arm_retrieval(item) + f"  [sweep_runtime_err: {e}]"


def arm_facts_audn_optional(item: dict) -> str:
    """РУКА G: извлечение (user+assistant) + A.U.D.N консолидация → ретрив → ответ."""
    m = _load_hybrid()
    if m is None:
        return arm_retrieval(item) + f"  [facts_fallback: {_hybrid_mod.get('err','?')}]"
    try:
        return m.arm_facts_audn(item, _llm, _ANSWER_SYSTEM)
    except Exception as e:
        return arm_retrieval(item) + f"  [facts_runtime_err: {e}]"


# ---------- судья ----------
_JUDGE_SYSTEM = (
    "You grade whether a model answer is correct given the gold answer. Reply ONLY "
    "'YES' if the model answer is consistent with the gold answer (same key fact), "
    "else 'NO'. Ignore phrasing differences."
)


def _is_abstention(item: dict) -> bool:
    return str(item.get("question_id", "")).endswith("_abs")


# Вырожденный ответ — не ответ, а строительные леса промпта. Ловушка,
# найденная живым прогоном: модель с «рассуждающим» стилем вывода сначала
# пересказывает задание («Question: … Constraint 1: …»), упирается в лимит
# токенов и до ответа не доходит. Судья видит переформулированный вопрос и
# нередко says YES — и рука получает завышенную точность при fallbacks=0.
# Так прогон открытой модели дал «без памяти 0.40» там, где физически
# должно быть около нуля. Молчаливое завышение опаснее падения: падение
# видно, завышение уходит в документ как факт.
_SCAFFOLD_RE = re.compile(
    r"constraint\s*\d?\s*:|^\s*\*?\s*(?:user\s+)?question\s*:", re.I | re.M)


def _looks_degenerate(pred: str, question: str = "") -> bool:
    p = (pred or "").strip()
    if len(p) < 3:
        return True
    if p.startswith("(llm_error") or p.startswith("(error:"):
        return True
    if _SCAFFOLD_RE.search(p):
        return True
    # ответ — это сам вопрос и ничего сверх него
    q = (question or "").strip().lower()
    if q and len(q) > 15 and q[:40] in p.lower() and len(p) < len(q) * 1.5:
        return True
    return False


def judge(item: dict, pred: str) -> bool:
    if _is_abstention(item):
        # верно = модель воздержалась
        return bool(re.search(r"not mentioned|don'?t (know|have)|no information|не (упомина|знаю)",
                              (pred or "").lower()))
    user = (f"Question: {item['question']}\nGold answer: {item['answer']}\n"
            f"Model answer: {pred}\nIs the model answer correct? YES/NO:")
    v = _llm(_JUDGE_SYSTEM, user, model=_JUDGE_MODEL, max_tokens=5)
    return bool(re.search(r"\byes\b", (v or "").lower()))


# ---------- прогон ----------
def run(variant: str, n: int) -> dict:
    data = load(variant, n)
    all_arms = {"none": arm_none, "dump": arm_dump,
                "retrieval": arm_retrieval, "hybrid": arm_hybrid_optional,
                "temporal": arm_temporal_optional, "sweep": arm_sweep_optional,
                "facts": arm_facts_optional,
                "facts_audn": arm_facts_audn_optional}
    # LME_ARMS=retrieval,facts,temporal — выбрать руки (не гонять дорогой dump/
    # невалидный hybrid зря). По умолчанию — все.
    sel = os.environ.get("LME_ARMS")
    arms = ({a: all_arms[a] for a in sel.split(",") if a in all_arms} if sel else all_arms)
    res = {a: {"correct": 0, "by_type": {}} for a in arms}
    rows = []
    for item in data:
        qt = item.get("question_type", "?")
        row = {"id": item.get("question_id"), "type": qt}
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
            row[a] = {"ok": ok, "pred": pred[:200]}
        rows.append(row)
    N = len(data) or 1
    # ВАЛИДНОСТЬ: считаем тихие fallback/ошибки на руку (чтобы НИКОГДА снова не
    # принять деградировавшую руку за рабочую — урок n=200).
    import re as _re
    fallbacks = {a: sum(1 for r in rows if _re.search(r"\[(hybrid|temporal|facts)_(fallback|runtime_err)|^\(error:", r[a]["pred"]))
                 for a in arms}
    # Вырожденные ответы на руку: если их много, точность руки не значит
    # ничего — судья мог засчитать леса промпта как ответ.
    degen = {a: sum(1 for r, item in zip(rows, data)
                    if _looks_degenerate(r[a]["pred"], item.get("question", "")))
             for a in arms}
    summary = {a: {"accuracy": round(res[a]["correct"] / N, 4),
                   "fallbacks": fallbacks[a],
                   "degenerate": degen[a],
                   "valid": degen[a] <= N * 0.1 and fallbacks[a] == 0,
                   "by_type": {t: round(c / n2, 3) for t, (c, n2) in res[a]["by_type"].items()}}
               for a in arms}
    return {"benchmark": f"longmemeval_{variant}", "model": _MODEL, "judge": _JUDGE_MODEL,
            "n": N, "arm_C_note": "BM25 stdlib stand-in (swap for our hybrid_search in env with deps)",
            "fallbacks": fallbacks, "summary": summary,
            # Цена прогона: без usage точность публиковалась без стоимости.
            # Токены суммарные по прогону (все руки + судья) — по-рукам API
            # не разделяет, и врать раскладкой мы не будем.
            "usage": dict(USAGE),
            "rows": rows}


if __name__ == "__main__":
    variant = sys.argv[1] if len(sys.argv) > 1 else "oracle"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 50
    out = run(variant, n)
    print(f"=== {out['benchmark']} | model={out['model']} | judge={out['judge']} | n={out['n']} ===")
    print("Точность по рукам (⚠ рука невалидна — число не значит ничего):")
    for a in out["summary"]:
        s_ = out["summary"][a]
        warn = ""
        if s_["fallbacks"]:
            warn = f"  ⚠ FALLBACK {s_['fallbacks']}/{out['n']} — рука НЕВАЛИДНА"
        elif not s_["valid"]:
            warn = (f"  ⚠ ВЫРОЖДЕННЫХ ОТВЕТОВ {s_['degenerate']}/{out['n']} — "
                    "модель выдаёт леса промпта, а не ответ; рука НЕВАЛИДНА")
        print(f"  {a:10s}: {s_['accuracy']:.4f}{warn}")
    print("\nРука C = " + out["arm_C_note"])
    safe = re.sub(r"[^a-z0-9.]+", "_", out["model"].lower())
    # суффикс имени файла: LME_TAG (явный) ИЛИ LME_TYPE — чтобы не клоббить др. прогоны
    tag = os.environ.get("LME_TAG") or os.environ.get("LME_TYPE", "")
    tsuffix = ("_" + re.sub(r"[^a-z0-9]+", "", tag.lower())) if tag else ""
    p = os.path.join(_HERE, f"benchmark_longmemeval_{variant}{tsuffix}_{safe}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\nполный прогон → {p}")
