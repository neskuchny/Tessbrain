# -*- coding: utf-8 -*-
"""
LongMemEval — рука C на «наш» гибридный ретрив (BM25 + векторные эмбеддинги + RRF).

Подключает АЛГОРИТМИЧЕСКУЮ суть нашей памяти (HybridSearchOrchestrator.search):
BM25 + sentence-transformer + Reciprocal Rank Fusion с нашими весами. Без
Qdrant/Neo4j инфраструктуры — fitting per-item на ходах одного айтема (как и
наивный BM25-стенд-ин). Это честное сравнение «BM25 один vs BM25+вектор+RRF»,
то есть наш реальный алгоритм гибридного ретрива, а не инфраструктура.

Зависимости: rank_bm25, numpy, sentence-transformers, scikit-learn (faiss опц.).
Эмбеддинг по умолчанию — all-MiniLM-L6-v2 (быстрый, multilingual вариант:
paraphrase-multilingual-MiniLM-L12-v2 — переключается env).

Используется как drop-in замена `arm_retrieval` в benchmark_longmemeval.py:
    from backend.core.eval.longmemeval_hybrid_arm import arm_hybrid
    arms = {"none": arm_none, "dump": arm_dump,
            "bm25_only": arm_retrieval, "hybrid": arm_hybrid}
"""
from __future__ import annotations

import os
import re
from typing import List

_EMBED_MODEL = os.environ.get("LME_EMBED", "sentence-transformers/all-MiniLM-L6-v2")
_HYBRID_TOPK = int(os.environ.get("LME_TOPK", "12"))

# Веса гибрида — как в HybridSearchOrchestrator.strategy_weights[BALANCED]
# (vector=1.0, bm25=1.0). Граф здесь не моделируем (нет графа на айтем).
_W_VEC = float(os.environ.get("LME_W_VEC", "1.0"))
_W_BM25 = float(os.environ.get("LME_W_BM25", "1.0"))
_RRF_K = int(os.environ.get("LME_RRF_K", "60"))

_model_cache = {"m": None}


def _get_model():
    if _model_cache["m"] is None:
        from sentence_transformers import SentenceTransformer
        _model_cache["m"] = SentenceTransformer(_EMBED_MODEL)
    return _model_cache["m"]


def _tok(s: str) -> List[str]:
    return re.findall(r"[a-zа-я0-9]+", (s or "").lower())


def _rrf_fuse(bm25_ranked: List[int], vec_ranked: List[int], topk: int) -> List[int]:
    """Reciprocal Rank Fusion двух ранжированных списков индексов. Идентично нашему
    backend/core/search/rrf_fusion.py:fuse — RRF(d) = Σ weight / (k + rank)."""
    scores: dict = {}
    for rank, idx in enumerate(bm25_ranked, start=1):
        scores[idx] = scores.get(idx, 0.0) + _W_BM25 / (_RRF_K + rank)
    for rank, idx in enumerate(vec_ranked, start=1):
        scores[idx] = scores.get(idx, 0.0) + _W_VEC / (_RRF_K + rank)
    return [idx for idx, _s in sorted(scores.items(), key=lambda x: -x[1])][:topk]


def hybrid_topk(query: str, docs: List[str], k: int) -> List[int]:
    """Гибридный ретрив (BM25 + dense + RRF) — возвращает индексы top-k docs.
    Per-item fit (как наш orchestrator с пустыми индексами): на ходах ОДНОГО
    айтема LongMemEval, БЕЗ кросс-айтемного индекса.

    BM25-проход — ЧИСТЫЙ stdlib (без rank_bm25, которое в песочнице эфемерно).
    Вектор-проход — sentence-transformers, ЕСЛИ доступен; иначе бросаем, чтобы
    arm_hybrid пометил деградацию ЯВНО (никаких тихих fallback)."""
    if not docs:
        return []
    pool = min(len(docs), max(k * 3, 40))
    bm25_ranked = _bm25_ranked_stdlib(query, docs, pool)  # stdlib, надёжно

    import numpy as np  # требует numpy + sentence-transformers; нет → исключение наверх
    model = _get_model()
    embs = model.encode(docs, convert_to_numpy=True, show_progress_bar=False,
                        normalize_embeddings=True)
    qemb = model.encode([query], convert_to_numpy=True, normalize_embeddings=True)[0]
    sims = embs @ qemb
    vec_ranked = [int(i) for i in np.argsort(-sims)[:pool]]
    return _rrf_fuse(bm25_ranked, vec_ranked, topk=k)


def _bm25_ranked_stdlib(query, docs, pool):
    """Ранжирование BM25 чистым stdlib (без rank_bm25). Возвращает индексы."""
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
    return [i for _s, i in scores[:pool]]


def arm_hybrid(item: dict, llm_fn, answer_system: str) -> str:
    """Рука C-real: гибридный ретрив (BM25+вектор+RRF) + LLM-ответ. Подаёт в
    модель только top-k релевантных ходов (как наш hybrid_search_orchestrator)."""
    # Хелперы лежат в основном харнессе. Имя в sys.modules зависит от способа
    # запуска (пакетный импорт vs run-as-script) — берём, что есть.
    import sys
    host = (sys.modules.get("backend.core.eval.benchmark_longmemeval")
            or sys.modules.get("__main__"))
    _turns = host._turns
    _fmt_turns = host._fmt_turns
    _MAX_DUMP_CHARS = host._MAX_DUMP_CHARS
    turns = _turns(item)
    docs = [f"{t['role']}: {t['content']}" for t in turns]
    idx = hybrid_topk(item["question"], docs, _HYBRID_TOPK)
    picked = [turns[i] for i in sorted(idx)]
    hist = _fmt_turns(picked)[:_MAX_DUMP_CHARS]
    return llm_fn(answer_system,
                  f"RELEVANT MEMORY:\n{hist}\n\nQuestion: {item['question']}\nAnswer:")


_TEMPORAL_SYSTEM = (
    "You are an assistant with TIMESTAMPED memory of prior conversations. Each "
    "memory line is prefixed with the DATE it was said. The user's question is "
    "asked on a specific date. Reason carefully about ORDER and TIMING of events "
    "(first/last/before/after/most recent/when). Use ONLY the provided memory. If "
    "it does not contain the answer, say exactly: NOT MENTIONED. Answer in English."
)


def arm_temporal(item: dict, llm_fn, answer_system: str) -> str:
    """Рука D: гибридный ретрив + ТЕМПОРАЛЬНАЯ ось «когда сказано». Каждый ход
    помечается датой своей сессии (haystack_dates), ходы подаются в ХРОНОЛОГИЧЕСКОМ
    порядке, в промпт добавляется дата вопроса. Это лёгкий аналог valid_at-рёбер
    графа (Zep/Graphiti), которых нет в чистом hybrid."""
    import sys
    host = (sys.modules.get("backend.core.eval.benchmark_longmemeval")
            or sys.modules.get("__main__"))
    _turns = host._turns
    _MAX_DUMP_CHARS = host._MAX_DUMP_CHARS

    turns = _turns(item)
    dates = item.get("haystack_dates", []) or []

    def _tdate(t) -> str:
        si = t.get("session")
        return dates[si] if isinstance(si, int) and si < len(dates) else "0000/00/00"

    docs = [f"{t['role']}: {t['content']}" for t in turns]
    # ретрив — ЧИСТЫЙ stdlib BM25 (надёжно; вектор в песочнице эфемерен)
    idx = _bm25_ranked_stdlib(item["question"], docs, _HYBRID_TOPK)
    picked = [turns[i] for i in idx]
    # хронологический порядок (формат 'YYYY/MM/DD (Day) HH:MM' → лексикографика верна)
    picked.sort(key=_tdate)
    lines = "\n".join(f"[{_tdate(t)}] [{t['role']}] {t['content']}" for t in picked)
    hist = lines[:_MAX_DUMP_CHARS]
    qdate = item.get("question_date", "")
    user = (f"Question asked on: {qdate}\n\n"
            f"TIMESTAMPED MEMORY (chronological order):\n{hist}\n\n"
            f"Question: {item['question']}\nAnswer:")
    return llm_fn(_TEMPORAL_SYSTEM, user)



# ============================================================================
# Рука F: ИЗВЛЕЧЕНИЕ ФАКТОВ в стиле Mem0 (главный рычаг разрыва, MEM0_STUDY.md)
# ============================================================================
# Вместо ретрива по СЫРЫМ ходам — извлекаем атомарные контекстно-богатые факты с
# АБСОЛЮТНЫМИ датами (FACT_RETRIEVAL-подход), потом ретрив по чистым фактам.
# Извлечение кэшируется per-item (как ингест Mem0 — платим раз, ищем много).

import json as _json

_FACT_SYSTEM = (
    "You extract atomic but context-rich FACTS from a conversation snippet — about "
    "BOTH the user AND what the assistant said/recommended/reported to the user. "
    "Capture: user's preferences, personal details, plans, relationships, events with "
    "dates, professional/health context; AND assistant's concrete statements, "
    "recommendations, facts it provided. RULES: (1) Each fact is ONE self-contained "
    "sentence, marking who (User/Assistant). (2) Convert relative dates to ABSOLUTE "
    "using the [DATE] markers. (3) Preserve proper nouns, titles, exact quantities. "
    'Return STRICT JSON {"facts": ["...", ...]}. If nothing memorable: {"facts": []}.'
)

# A.U.D.N — консолидация памяти (Mem0 DEFAULT_UPDATE_MEMORY_PROMPT, по сути).
_AUDN_SYSTEM = (
    "You maintain a consistent long-term MEMORY of facts. Given the CURRENT MEMORY "
    "and NEW candidate facts, merge them. For each new fact decide: ADD (new info), "
    "UPDATE (same topic but richer/changed — replace the old, keep the newer/correct "
    "value), DELETE (new fact contradicts an old one — drop the outdated), NONE "
    "(duplicate). Preserve absolute dates and who-said markers. Lose NO information "
    'that any question could need. Return STRICT JSON {"memory": ["...", ...]} — the '
    "FULL updated memory list."
)

_FACT_CHUNK_CHARS = int(os.environ.get("LME_FACT_CHUNK", "30000"))
_FACT_CACHE = os.environ.get("LME_FACT_CACHE", "/tmp/lme_facts_cache")
_FACT_VER = os.environ.get("LME_FACT_VER", "v2ua")  # scope user+assistant → новый кэш


def _model_tag():
    import sys
    host = (sys.modules.get("backend.core.eval.benchmark_longmemeval")
            or sys.modules.get("__main__"))
    return re.sub(r"[^a-z0-9]+", "", str(getattr(host, "_MODEL", "m")).lower())


def _extract_facts(item: dict, llm_fn) -> list:
    """Извлечь факты из ВСЕХ сессий айтема (Mem0-стиль), с кэшем per-item+МОДЕЛЬ+версия.
    Возвращает список строк-фактов (с абсолютными датами внутри)."""
    import sys
    host = (sys.modules.get("backend.core.eval.benchmark_longmemeval")
            or sys.modules.get("__main__"))
    qid = str(item.get("question_id", "noid"))
    os.makedirs(_FACT_CACHE, exist_ok=True)
    cpath = os.path.join(_FACT_CACHE, f"{qid}_{_model_tag()}_{_FACT_VER}.json")
    if os.path.exists(cpath):
        try:
            return _json.load(open(cpath, encoding="utf-8"))
        except Exception:
            pass

    dates = item.get("haystack_dates", []) or []
    # сгруппировать ходы по сессии, каждый блок с [DATE]-заголовком
    sessions = {}
    for si, sess in enumerate(item.get("haystack_sessions", [])):
        d = dates[si] if si < len(dates) else "unknown-date"
        block = f"[DATE {d}]\n" + "\n".join(
            f"{t.get('role')}: {t.get('content')}" for t in sess)
        sessions[si] = block

    # склеить блоки в чанки ~_FACT_CHUNK_CHARS
    chunks, cur, cur_len = [], [], 0
    for si in sorted(sessions):
        b = sessions[si]
        if cur and cur_len + len(b) > _FACT_CHUNK_CHARS:
            chunks.append("\n\n".join(cur)); cur, cur_len = [], 0
        cur.append(b); cur_len += len(b)
    if cur:
        chunks.append("\n\n".join(cur))

    facts: list = []
    for ch in chunks:
        try:
            raw = llm_fn(_FACT_SYSTEM, f"CONVERSATION SNIPPET:\n{ch}\n\nExtract facts JSON:")
            s, e = raw.find("{"), raw.rfind("}")
            obj = _json.loads(raw[s:e + 1]) if s >= 0 and e > s else {}
            for f in (obj.get("facts") or []):
                if isinstance(f, str) and f.strip():
                    facts.append(f.strip())
        except Exception:
            continue
    try:
        _json.dump(facts, open(cpath, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass
    return facts


_FACT_ANSWER_SYSTEM = (
    "You answer the user's question using ONLY the provided MEMORY FACTS (extracted "
    "from past conversations, each may carry an absolute date). Reason over dates for "
    "timing questions. If the facts do not contain the answer, say exactly: NOT "
    "MENTIONED. Answer in English, concise."
)


def arm_facts(item: dict, llm_fn, answer_system: str) -> str:
    """Рука F: извлечь факты (Mem0-стиль, кэш) → BM25-ретрив по фактам → ответ.
    Прямой тест: закрывает ли извлечение фактов разрыв с Mem0 vs ретрив по ходам."""
    facts = _extract_facts(item, llm_fn)
    if not facts:
        return "NOT MENTIONED.  [facts_empty]"
    topk = max(_HYBRID_TOPK, 15)
    idx = _bm25_ranked_stdlib(item["question"], facts, topk)
    picked = [facts[i] for i in idx]
    qdate = item.get("question_date", "")
    mem = "\n".join(f"- {f}" for f in picked)
    user = (f"Question asked on: {qdate}\n\nMEMORY FACTS:\n{mem}\n\n"
            f"Question: {item['question']}\nAnswer:")
    return llm_fn(_FACT_ANSWER_SYSTEM, user)


# ============================================================================
# Рука G: ИЗВЛЕЧЕНИЕ + A.U.D.N консолидация (Mem0 полнее) + scope user+assistant
# ============================================================================
_AUDN_CACHE_VER = os.environ.get("LME_AUDN_VER", "v1")


def _audn_merge_facts(item: dict, llm_fn) -> list:
    """Извлечь факты по чанкам И консолидировать через A.U.D.N (ADD/UPDATE/DELETE/
    NONE) последовательно. Кэш per-item+модель+версия. Это полнее arm_facts —
    добавлена фаза 2 Mem0 (merge без потери, обновление фактов)."""
    import sys
    host = (sys.modules.get("backend.core.eval.benchmark_longmemeval")
            or sys.modules.get("__main__"))
    qid = str(item.get("question_id", "noid"))
    os.makedirs(_FACT_CACHE, exist_ok=True)
    cpath = os.path.join(_FACT_CACHE, f"{qid}_{_model_tag()}_audn{_AUDN_CACHE_VER}.json")
    if os.path.exists(cpath):
        try:
            return _json.load(open(cpath, encoding="utf-8"))
        except Exception:
            pass

    dates = item.get("haystack_dates", []) or []
    sessions = {}
    for si, sess in enumerate(item.get("haystack_sessions", [])):
        d = dates[si] if si < len(dates) else "unknown-date"
        sessions[si] = f"[DATE {d}]\n" + "\n".join(
            f"{t.get('role')}: {t.get('content')}" for t in sess)
    # чанки
    chunks, cur, cl = [], [], 0
    for si in sorted(sessions):
        b = sessions[si]
        if cur and cl + len(b) > _FACT_CHUNK_CHARS:
            chunks.append("\n\n".join(cur)); cur, cl = [], 0
        cur.append(b); cl += len(b)
    if cur:
        chunks.append("\n\n".join(cur))

    memory: list = []
    for ch in chunks:
        # фаза 1: извлечь кандидатов (scope user+assistant)
        try:
            raw = llm_fn(_FACT_SYSTEM, f"CONVERSATION SNIPPET:\n{ch}\n\nExtract facts JSON:")
            s, e = raw.find("{"), raw.rfind("}")
            cand = (_json.loads(raw[s:e + 1]).get("facts") if s >= 0 and e > s else []) or []
            cand = [c.strip() for c in cand if isinstance(c, str) and c.strip()]
        except Exception:
            cand = []
        if not cand:
            continue
        # фаза 2: A.U.D.N merge кандидатов в память
        try:
            mem_str = "\n".join(f"- {m}" for m in memory) or "(empty)"
            cand_str = "\n".join(f"- {c}" for c in cand)
            raw = llm_fn(_AUDN_SYSTEM,
                         f"CURRENT MEMORY:\n{mem_str}\n\nNEW FACTS:\n{cand_str}\n\nUpdated memory JSON:")
            s, e = raw.find("{"), raw.rfind("}")
            merged = (_json.loads(raw[s:e + 1]).get("memory") if s >= 0 and e > s else None)
            if isinstance(merged, list) and merged:
                memory = [m.strip() for m in merged if isinstance(m, str) and m.strip()]
            else:
                memory.extend(cand)  # деградация merge → не теряем кандидатов
        except Exception:
            memory.extend(cand)
    try:
        _json.dump(memory, open(cpath, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass
    return memory


def arm_facts_audn(item: dict, llm_fn, answer_system: str) -> str:
    """Рука G: извлечение (user+assistant) + A.U.D.N консолидация → ретрив → ответ.
    Полнее arm_facts: добавлена фаза merge Mem0. Чисто flash-lite (без сильной модели)."""
    memory = _audn_merge_facts(item, llm_fn)
    if not memory:
        return "NOT MENTIONED.  [facts_empty]"
    idx = _bm25_ranked_stdlib(item["question"], memory, max(_HYBRID_TOPK, 15))
    picked = [memory[i] for i in idx]
    qdate = item.get("question_date", "")
    mem = "\n".join(f"- {f}" for f in picked)
    user = (f"Question asked on: {qdate}\n\nMEMORY FACTS:\n{mem}\n\n"
            f"Question: {item['question']}\nAnswer:")
    return llm_fn(_FACT_ANSWER_SYSTEM, user)


# ============================================================================
# Рука S: SWEEP — агрегирующий вопрос расширяет хиты до ЦЕЛЫХ сессий
# ============================================================================
# Живой прогон n=30 показал слабое место temporal-руки: multi-session 0.5.
# Разбор провалов: агрегирующие вопросы («total amount», «how many devices»,
# «how long») — сумма/счёт разбросаны по сессиям, top-K по релевантности
# не достаёт все вхождения (dump с полным контекстом решал то, что ретрив
# проваливал). Лечение НЕ «умнее ранжировать», а «не резать»: если вопрос
# агрегирующий (наш продовый enumerative_detect + англ. паттерны в нём же),
# каждая сессия, куда попал хоть один BM25-хит, подаётся ЦЕЛИКОМ, в
# хронологии, с датами. Объём ограничен естественно: сессий с хитами мало.
# Неагрегирующий вопрос → без изменений, arm_temporal.

_SWEEP_SYSTEM = (
    "You are an assistant with TIMESTAMPED memory of prior conversations, "
    "grouped by session. Each line is prefixed with the DATE it was said. "
    "The question requires AGGREGATING information scattered across "
    "sessions (totals, counts, durations). Carefully collect EVERY relevant "
    "mention across ALL sessions before computing sums, counts or time "
    "spans; for durations, anchor arithmetic to the line dates and the "
    "question date. Use ONLY the provided memory. If it does not contain "
    "the answer, say exactly: NOT MENTIONED. Answer in English."
)


def _is_aggregate_question(question: str) -> bool:
    """Детекция агрегации — ПРОДОВЫМ детектором, не локальной копией:
    харнесс обязан мерить тот же механизм, который поедет к клиентам."""
    try:
        import importlib.util as _u
        import os as _o
        import sys as _s
        import types as _t
        root = _o.path.dirname(_o.path.dirname(_o.path.dirname(
            _o.path.dirname(_o.path.abspath(__file__)))))
        for pkg in ("backend", "backend.core", "backend.core.search"):
            if pkg not in _s.modules:
                m = _t.ModuleType(pkg)
                m.__path__ = [_o.path.join(root, *pkg.split("."))]
                _s.modules[pkg] = m
        name = "backend.core.search.enumerative_detect"
        if name not in _s.modules:
            spec = _u.spec_from_file_location(name, _o.path.join(
                root, "backend/core/search/enumerative_detect.py"))
            mod = _u.module_from_spec(spec)
            _s.modules[name] = mod
            spec.loader.exec_module(mod)
        return bool(_s.modules[name].detect(question))
    except Exception:
        return False


def arm_sweep(item: dict, llm_fn, answer_system: str) -> str:
    """Рука S: sweep для агрегирующих вопросов, temporal для остальных."""
    if not _is_aggregate_question(item.get("question", "")):
        return arm_temporal(item, llm_fn, answer_system)

    import sys
    host = (sys.modules.get("backend.core.eval.benchmark_longmemeval")
            or sys.modules.get("__main__"))
    _turns = host._turns
    _MAX_DUMP_CHARS = host._MAX_DUMP_CHARS

    turns = _turns(item)
    dates = item.get("haystack_dates", []) or []

    def _sdate(si) -> str:
        return dates[si] if isinstance(si, int) and si < len(dates) else "0000/00/00"

    docs = [f"{t['role']}: {t['content']}" for t in turns]
    idx = _bm25_ranked_stdlib(item["question"], docs, _HYBRID_TOPK)
    hit_sessions = {turns[i].get("session") for i in idx}

    # Все ходы сессий с хитами — целиком, по сессиям, хронологически.
    sessions: dict = {}
    for t in turns:
        si = t.get("session")
        if si in hit_sessions:
            sessions.setdefault(si, []).append(t)
    parts = []
    for si in sorted(sessions, key=_sdate):
        lines = "\n".join(f"[{_sdate(si)}] [{t['role']}] {t['content']}"
                          for t in sessions[si])
        parts.append(f"=== SESSION of {_sdate(si)} ===\n{lines}")
    hist = "\n\n".join(parts)[:_MAX_DUMP_CHARS]
    qdate = item.get("question_date", "")
    user = (f"Question asked on: {qdate}\n\n"
            f"FULL SESSIONS containing relevant mentions (chronological):\n"
            f"{hist}\n\nQuestion: {item['question']}\nAnswer:")
    return llm_fn(_SWEEP_SYSTEM, user)
