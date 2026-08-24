# -*- coding: utf-8 -*-
"""Бенч прогона встречи разными моделями и способами (время/токены/$).

Оси (docs/MEMORY_COST_AND_CRYSTALLIZATION.md §6):
  - модель: flash-lite / flash / qwen-27b / sonnet-5 / opus-4.8
  - способ: multi (текущее: ~10 агентов + downstream, транскрипт ×10)
            single (адаптив: 1 комбо-извлечение, транскрипт ×1)

Два режима работы:
  - ПРОЕКЦИЯ (дефолт): меряем токен-профиль транскрипта и считаем $ по ценам
    моделей — работает без ключей/сети, для быстрой прикидки на реальных встречах.
  - --live: реально прогоняем CaptureOrchestrator на ВЫЗЫВАЕМЫХ моделях и берём
    факт (вызовы/токены/$/время) из usage_tracker; для остальных — проекция.

Запуск:
  python -m scripts.bench_meeting_models --file transcript.txt
  python -m scripts.bench_meeting_models --meeting-id <id> --meeting-id <id2>
  python -m scripts.bench_meeting_models --file t.txt --live
"""
from __future__ import annotations

import argparse
import sys
from typing import Any, Dict, List

# --- цены берём из единого источника (usage_tracker.MODEL_PRICING) ---
try:
    from backend.core.llm.usage_tracker import MODEL_PRICING
except Exception:  # pragma: no cover - на случай запуска вне пакета
    MODEL_PRICING = {}

# Грубые тайминги (сек), эвристика — уточняются --live.
_TIME_HINT = {
    ("gemini-flash-lite-latest", "multi"): 60,
    ("gemini-flash-latest", "multi"): 60,
    ("qwen-27b-local", "multi"): 180,
    ("claude-sonnet-5", "single"): 40,
    ("claude-sonnet-5", "multi"): 150,
    ("claude-opus-4-8", "single"): 120,
    ("claude-opus-4-8", "multi"): 240,
}

DEFAULT_STRATEGIES: List[Dict[str, str]] = [
    {"name": "flash-lite × multi", "model": "gemini-flash-lite-latest", "mode": "multi"},
    {"name": "flash × multi", "model": "gemini-flash-latest", "mode": "multi"},
    {"name": "qwen-27b × multi", "model": "qwen-27b-local", "mode": "multi"},
    {"name": "sonnet-5 × single", "model": "claude-sonnet-5", "mode": "single"},
    {"name": "sonnet-5 × multi", "model": "claude-sonnet-5", "mode": "multi"},
    {"name": "opus-4.8 × single", "model": "claude-opus-4-8", "mode": "single"},
    {"name": "opus-4.8 × multi", "model": "claude-opus-4-8", "mode": "multi"},
]


def estimate_tokens(text: str) -> int:
    """Оценка токенов ~ len/2 (та же конвенция, что в коде: estimate_tokens)."""
    return max(1, len(text or "") // 2)


def profile(transcript_tokens: int, mode: str) -> Dict[str, int]:
    """Токен-профиль стратегии.

    multi: ~10 агентов пересылают транскрипт + ~7 downstream-фаз (частично).
    single: 1 комбо-извлечение (транскрипт ОДИН раз) — адаптивный путь.
    Числа — оценки; --live заменяет фактом из usage_tracker."""
    t = int(transcript_tokens)
    if mode == "single":
        return {"calls": 1, "input": t + 1200, "output": 6000}
    # multi (текущая архитектура)
    n_agents, n_downstream = 10, 7
    input_tokens = t * n_agents + t * 3 + n_agents * 300 + n_downstream * 1500
    output_tokens = n_agents * 500 + n_downstream * 1200
    return {"calls": n_agents + n_downstream, "input": input_tokens, "output": output_tokens}


def cost_usd(model: str, input_tokens: int, output_tokens: int, cached: int = 0) -> float:
    """$ по MODEL_PRICING (единый источник цен)."""
    p = MODEL_PRICING.get(model) or MODEL_PRICING.get("default") or {"input": 0, "output": 0, "cached": 0}
    return round(
        input_tokens / 1_000_000 * p["input"]
        + output_tokens / 1_000_000 * p["output"]
        + cached / 1_000_000 * p.get("cached", 0),
        4,
    )


def profile_by_runs(transcript_tokens: int, runs: int) -> Dict[str, int]:
    """Токен-профиль для произвольного числа запросов (лестница S0..S4).

    Транскрипт летит `runs` раз (каждый запрос его пересылает); чем меньше
    запросов — тем меньше редундантного входа. Выход растёт с числом запросов
    (больше обрамления/повторов), но слабее входа."""
    t = int(transcript_tokens)
    input_tokens = t * runs + runs * 800
    output_tokens = 4000 + runs * 600
    return {"calls": runs, "input": input_tokens, "output": output_tokens}


def cost_split(model: str, input_tokens: int, output_tokens: int) -> Dict[str, float]:
    p = MODEL_PRICING.get(model) or MODEL_PRICING.get("default") or {"input": 0, "output": 0}
    cin = round(input_tokens / 1_000_000 * p["input"], 4)
    cout = round(output_tokens / 1_000_000 * p["output"], 4)
    return {"in": cin, "out": cout, "total": round(cin + cout, 4)}


def _price_for(name: str) -> Dict[str, float]:
    """(in,out) $/1M для модели: verified из usage_tracker, иначе из каталога."""
    from scripts.model_catalog import by_name
    key = _CATALOG_TO_PRICEKEY.get(name)
    if key and key in MODEL_PRICING:
        p = MODEL_PRICING[key]
        return {"in": p["input"], "out": p["output"]}
    m = by_name(name) or {}
    return {"in": m.get("in", 0.0), "out": m.get("out", 0.0)}


def catalog_rows(transcript: str, only: List[str] | None = None) -> List[Dict[str, Any]]:
    """Таблица по каталогу: модель × плановый шаг лестницы → запросы/токены/$/скорость.

    only — список имён моделей для прогона (None = все)."""
    from scripts.model_catalog import MODELS, TIER_SPEED, planned_runs
    from backend.core.llm.usage_tracker import MODEL_PRICING as _MP  # noqa
    t_tok = estimate_tokens(transcript)
    rows = []
    for m in MODELS:
        if only and m["name"] not in only:
            continue
        runs = planned_runs(m)
        pr = profile_by_runs(t_tok, runs)
        # цену берём по имени в usage_tracker, если есть; иначе прямо из каталога
        key = _CATALOG_TO_PRICEKEY.get(m["name"])
        if key and key in MODEL_PRICING:
            c = cost_split(key, pr["input"], pr["output"])
        else:
            cin = round(pr["input"] / 1_000_000 * m["in"], 4)
            cout = round(pr["output"] / 1_000_000 * m["out"], 4)
            c = {"in": cin, "out": cout, "total": round(cin + cout, 4)}
        rows.append({
            "name": m["name"], "tier": m["tier"], "src": m["src"], "runs": runs,
            "in_tok": pr["input"], "out_tok": pr["output"],
            "cin": c["in"], "cout": c["out"], "ctot": c["total"],
            "speed": TIER_SPEED[m["tier"]], "quality": "—",
        })
    return rows


# имя каталога → ключ цены в usage_tracker (для verified-цен)
_CATALOG_TO_PRICEKEY = {
    "gemini-3.1-flash-lite": "gemini-flash-lite-latest",
    "gemini-3-flash": "gemini-flash-latest",
    "sonnet-5": "claude-sonnet-5",
    "opus-4.8": "claude-opus-4-8",
    "fable-5": "claude-fable-5",
    "qwen-3.6-27b": "qwen-27b-local",
    "gemma-4-31b": "qwen-27b-local",
}


def _fmt_catalog(rows: List[Dict[str, Any]]) -> str:
    hdr = (f"{'модель':<22}{'тир':<9}{'цена':<10}{'зап':>4}{'in_tok':>9}{'out_tok':>8}"
           f"{'$вход':>9}{'$выход':>9}{'$итого':>9}{'tok/s':>7}{'кач':>5}")
    lines = [hdr, "-" * len(hdr)]
    for r in rows:
        lines.append(
            f"{r['name']:<22}{r['tier']:<9}{r['src']:<10}{r['runs']:>4}{r['in_tok']:>9}{r['out_tok']:>8}"
            f"{('$'+format(r['cin'],'.4f')):>9}{('$'+format(r['cout'],'.4f')):>9}"
            f"{('$'+format(r['ctot'],'.4f')):>9}{r['speed']:>7}{r['quality']:>5}")
    return "\n".join(lines)


# ============================================================================
# ЖИВОЙ прогон: реально вызвать модели (native Claude / OpenRouter остальное),
# прогнать движком run_tiered, замерить время/токены/стоимость, СОХРАНИТЬ ответ.
# ============================================================================
_ANTHROPIC_ID = {"opus-4.8": "claude-opus-4-8", "sonnet-5": "claude-sonnet-5",
                 "fable-5": "claude-fable-5"}


def _env_keys() -> set:
    import os
    ks = set()
    if os.environ.get("OPENROUTER_API_KEY"): ks.add("openrouter")
    if os.environ.get("ANTHROPIC_API_KEY"): ks.add("anthropic")
    if os.environ.get("OPENAI_API_KEY"): ks.add("openai")
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"): ks.add("google")
    if os.environ.get("DEEPSEEK_API_KEY"): ks.add("deepseek")
    if os.environ.get("XAI_API_KEY"): ks.add("xai")
    return ks


class _ORCaller:
    """OpenRouter (OpenAI-совместимый). Копит токены из usage."""
    def __init__(self, slug: str, api_key: str):
        self.slug, self.key = slug, api_key
        self.total_in = self.total_out = 0
        self._c = None

    async def generate(self, prompt: str) -> str:
        import httpx
        if self._c is None:
            self._c = httpx.AsyncClient(timeout=180)
        r = await self._c.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
                     "HTTP-Referer": "https://tessent.local", "X-Title": "tessent-bench"},
            json={"model": self.slug, "temperature": 0,
                  "messages": [{"role": "user", "content": prompt}]})
        if r.status_code >= 400:  # показать ТЕЛО (обычно «нет такой модели»)
            raise RuntimeError(f"OpenRouter {r.status_code} для '{self.slug}': {r.text[:400]}")
        d = r.json()
        u = d.get("usage") or {}
        self.total_in += int(u.get("prompt_tokens") or 0)
        self.total_out += int(u.get("completion_tokens") or 0)
        return d["choices"][0]["message"]["content"]

    async def aclose(self):
        if self._c:
            await self._c.aclose()


class _AnthropicCaller:
    """Нативный Anthropic (надёжные model-id для Claude)."""
    def __init__(self, model_id: str, api_key: str):
        self.model, self.key = model_id, api_key
        self.total_in = self.total_out = 0
        self._c = None

    async def generate(self, prompt: str) -> str:
        import httpx
        if self._c is None:
            self._c = httpx.AsyncClient(timeout=180)
        r = await self._c.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": self.key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": self.model, "max_tokens": 8000,
                  "messages": [{"role": "user", "content": prompt}]})
        if r.status_code >= 400:
            raise RuntimeError(f"Anthropic {r.status_code} для '{self.model}': {r.text[:400]}")
        d = r.json()
        u = d.get("usage") or {}
        self.total_in += int(u.get("input_tokens") or 0)
        self.total_out += int(u.get("output_tokens") or 0)
        return "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")

    async def aclose(self):
        if self._c:
            await self._c.aclose()


def _resolve_caller(name: str, keys: set):
    """(caller, via_str) или (None, причина)."""
    import os
    from scripts.model_catalog import OPENROUTER_ID, native_provider
    nat = native_provider(name)
    if nat == "anthropic" and "anthropic" in keys and name in _ANTHROPIC_ID:
        return _AnthropicCaller(_ANTHROPIC_ID[name], os.environ["ANTHROPIC_API_KEY"]), \
            f"native:{_ANTHROPIC_ID[name]}"
    slug = OPENROUTER_ID.get(name)
    if "openrouter" in keys and slug:
        return _ORCaller(slug, os.environ["OPENROUTER_API_KEY"]), f"openrouter:{slug}"
    return None, f"нет ключа/слага (нужен OPENROUTER_API_KEY или native {nat})"


async def _live_catalog(transcript: str, model_names: List[str], out_dir: str,
                        dry: bool = False, strict: bool = True) -> List[Dict[str, Any]]:
    import json
    import os
    import time as _t
    from backend.core.capture.tiered_extraction import run_tiered
    from scripts.model_catalog import by_name
    keys = _env_keys()
    os.makedirs(out_dir, exist_ok=True)
    rows = []
    for name in model_names:
        m = by_name(name)
        if not m:
            rows.append({"name": name, "via": "—", "err": "нет в каталоге"})
            continue
        caller, via = _resolve_caller(name, keys)
        if caller is None:
            rows.append({"name": name, "via": via, "err": "нет маршрута"})
            continue
        if dry:  # только показать маршрут, не звать
            rows.append({"name": name, "via": via, "tier": m["tier"], "err": None})
            continue
        t0 = _t.monotonic()
        try:
            res = await run_tiered(transcript, llm=None, tier=m["tier"],
                                   generate=caller.generate, strict_schema=strict)
        except Exception as e:  # noqa: BLE001
            rows.append({"name": name, "via": via, "err": str(e)[:80]})
            await caller.aclose()
            continue
        dt = _t.monotonic() - t0
        await caller.aclose()
        pr = _price_for(name)
        usd = round(caller.total_in / 1e6 * pr["in"] + caller.total_out / 1e6 * pr["out"], 4)
        path = os.path.join(out_dir, f"{name}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(res["sections"], f, ensure_ascii=False, indent=2)
        rows.append({"name": name, "via": via, "calls": res["stats"]["actual_calls"],
                     "in": caller.total_in, "out": caller.total_out,
                     "time": round(dt, 1), "usd": usd, "saved": path, "err": None})
    return rows


def _fmt_live(rows: List[Dict[str, Any]], dry: bool) -> str:
    if dry:
        lines = [f"{'модель':<22}{'маршрут вызова':<40}"]
        lines.append("-" * len(lines[0]))
        for r in rows:
            mark = r["via"] if not r.get("err") else f"{r['via']} — {r['err']}"
            lines.append(f"{r['name']:<22}{mark:<40}")
        return "\n".join(lines)
    hdr = f"{'модель':<22}{'вызов':>6}{'in_tok':>9}{'out_tok':>9}{'время,с':>9}{'$итого':>10}  файл/ошибка"
    lines = [hdr, "-" * len(hdr)]
    for r in rows:
        if r.get("err"):
            lines.append(f"{r['name']:<22}{'':>6}{'':>9}{'':>9}{'':>9}{'':>10}  ❌ {r['err']}")
        else:
            lines.append(f"{r['name']:<22}{r['calls']:>6}{r['in']:>9}{r['out']:>9}"
                         f"{r['time']:>9}{('$'+format(r['usd'],'.4f')):>10}  {r['saved']}")
    return "\n".join(lines)


def matrix_rows(only: List[str] | None = None) -> List[Dict[str, Any]]:
    """Матрица тестирования: модель × какие шаги прогоняем × маршрут вызова."""
    from scripts.model_catalog import (LADDER, MODELS, native_provider,
                                        resolve_route, test_steps)
    rows = []
    for m in MODELS:
        if only and m["name"] not in only:
            continue
        steps = test_steps(m["tier"])
        runs = "/".join(str(LADDER[s]["runs"]) for s in steps)
        nat = native_provider(m["name"])
        # маршрут: если есть свой ключ — native, иначе openrouter
        route = "свой ключ (%s) или OpenRouter" % nat if nat else "OpenRouter"
        rows.append({"name": m["name"], "tier": m["tier"],
                     "steps": " ".join(steps), "runs": runs, "route": route})
    return rows


def _fmt_matrix(rows: List[Dict[str, Any]]) -> str:
    hdr = f"{'модель':<22}{'тир':<9}{'шаги теста':<12}{'запросов':<12}{'маршрут вызова':<32}"
    lines = [hdr, "-" * len(hdr)]
    for r in rows:
        lines.append(f"{r['name']:<22}{r['tier']:<9}{r['steps']:<12}{r['runs']:<12}{r['route']:<32}")
    return "\n".join(lines)


# --- ОПЕРАЦИОННАЯ стоимость: ночная консолидация + поиск brain по уровням ---
# Профили (число вызовов и токены на вызов) — оценка по коду:
#   ночная (per-user/ночь): curiosity 1 + competitor до 3 + profile 1 +
#     кристаллизация ~1 на каждые 5 НОВЫХ встреч (map-reduce по кристаллам).
#   поиск brain: quick ~1 вызов, standard ~2, deep ~6 (recon+чтение+синтез).
OPS = {
    "nightly_fixed": [  # (вызовов, in_tok, out_tok) — базовая часть ночи
        ("curiosity", 1, 2000, 500),
        ("competitor", 3, 3000, 800),
        ("profile", 1, 3000, 700),
    ],
    "crystal_per5": ("crystallization", 8000, 2000),  # 1 вызов на каждые 5 встреч
    "search": {  # уровень → (вызовов, in_tok_всего, out_tok_всего)
        "quick": (1, 4000, 800),
        "standard": (2, 8000, 1500),
        "deep": (6, 115000, 4000),
    },
}


def ops_rows(n_new_meetings: int, model_name: str) -> List[Dict[str, Any]]:
    """Строки ops: ночная консолидация (на N новых встреч) + поиск по уровням."""
    import math
    pr = _price_for(model_name)

    def _cost(in_t, out_t):
        return round(in_t / 1_000_000 * pr["in"] + out_t / 1_000_000 * pr["out"], 4)

    rows: List[Dict[str, Any]] = []
    # ночная: фикс + кристаллизация(N)
    calls = in_t = out_t = 0
    for _name, c, i, o in OPS["nightly_fixed"]:
        calls += c; in_t += c * i; out_t += c * o
    cn = math.ceil(max(0, n_new_meetings) / 5)
    _cname, ci, co = OPS["crystal_per5"]
    calls += cn; in_t += cn * ci; out_t += cn * co
    rows.append({"op": f"ночная (+{n_new_meetings} встреч)", "calls": calls,
                 "in": in_t, "out": out_t, "usd": _cost(in_t, out_t)})
    # поиск по уровням (за 1 запрос)
    for lvl, (c, i, o) in OPS["search"].items():
        rows.append({"op": f"поиск: {lvl} (1 запрос)", "calls": c,
                     "in": i, "out": o, "usd": _cost(i, o)})
    return rows


def _fmt_ops(rows: List[Dict[str, Any]], model_name: str) -> str:
    hdr = f"{'операция':<28}{'вызовов':>8}{'in_tok':>9}{'out_tok':>9}{'$':>10}"
    lines = [f"Операционная стоимость на модели: {model_name}", hdr, "-" * len(hdr)]
    for r in rows:
        lines.append(f"{r['op']:<28}{r['calls']:>8}{r['in']:>9}{r['out']:>9}{('$'+format(r['usd'],'.4f')):>10}")
    return "\n".join(lines)


def project(transcript: str, strategies: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    t_tok = estimate_tokens(transcript)
    rows = []
    for s in strategies:
        pr = profile(t_tok, s["mode"])
        rows.append({
            "name": s["name"], "model": s["model"], "mode": s["mode"],
            "source": "ПРОЕКЦИЯ",
            "calls": pr["calls"], "in": pr["input"], "out": pr["output"],
            "time_s": _TIME_HINT.get((s["model"], s["mode"]), None),
            "usd": cost_usd(s["model"], pr["input"], pr["output"]),
        })
    return rows


def _fmt_table(rows: List[Dict[str, Any]]) -> str:
    hdr = f"{'стратегия':<20}{'источник':<11}{'вызов':>6}{'in_tok':>10}{'out_tok':>9}{'время,с':>9}{'$/встреча':>11}"
    lines = [hdr, "-" * len(hdr)]
    for r in rows:
        t = "?" if r["time_s"] is None else str(r["time_s"])
        lines.append(
            f"{r['name']:<20}{r['source']:<11}{r['calls']:>6}{r['in']:>10}{r['out']:>9}{t:>9}{('$'+format(r['usd'],'.4f')):>11}")
    return "\n".join(lines)


async def _fetch_transcript(meeting_id: str) -> str:
    """Транскрипт встречи из Supabase (поле transcription_text/transcript).
    Нужны env SUPABASE_URL/SUPABASE_KEY (те же, что у backend)."""
    try:
        from backend.db.supabase_client import get_supabase_client
        sb = get_supabase_client()
        rows = await sb._request("GET", "/rest/v1/meetings", params={
            "id": f"eq.{meeting_id}",
            "select": "id,title,transcription_text,transcript", "limit": "1"})
        if rows:
            r = rows[0]
            return r.get("transcription_text") or r.get("transcript") or ""
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ supabase: {e}", file=sys.stderr)
    return ""


async def _or_models(query: str) -> list:
    """Реальные слаги моделей OpenRouter, содержащие query (для правки OPENROUTER_ID)."""
    import os
    import httpx
    key = os.environ.get("OPENROUTER_API_KEY", "")
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get("https://openrouter.ai/api/v1/models",
                        headers={"Authorization": f"Bearer {key}"} if key else {})
        r.raise_for_status()
        data = r.json().get("data", [])
    q = (query or "").lower()
    out = []
    for m in data:
        mid = m.get("id", "")
        if q in mid.lower() or q in str(m.get("name", "")).lower():
            pr = m.get("pricing", {}) or {}
            out.append((mid, pr.get("prompt"), pr.get("completion")))
    return sorted(out)


async def _list_meetings(user_id: str, limit: int = 30) -> list:
    from backend.db.supabase_client import get_supabase_client
    sb = get_supabase_client()
    rows = await sb._request("GET", "/rest/v1/meetings", params={
        "user_id": f"eq.{user_id}",
        "select": "id,title,created_at,status", "order": "created_at.desc",
        "limit": str(limit)})
    return rows or []


def _load_transcript(args) -> str:
    import os
    if args.file:
        if not os.path.exists(args.file):
            print(f"❌ Файл не найден: {args.file}\n"
                  f"   Текущая папка: {os.getcwd()}\n"
                  f"   → Положи транскрипт в этот файл ЗДЕСЬ, ИЛИ дай полный путь "
                  f"(--file E:\\путь\\meeting.txt),\n"
                  f"   ИЛИ используй встречу из системы: сперва\n"
                  f"     py -m scripts.bench_meeting_models --list-meetings --user-id <твой_uuid>\n"
                  f"   потом --meeting-id <id>.", file=sys.stderr)
            sys.exit(2)
        with open(args.file, "r", encoding="utf-8") as f:
            return f.read()
    if args.meeting_id:
        import asyncio
        t = asyncio.run(_fetch_transcript(args.meeting_id[0]))
        if not t:
            print(f"❌ Пустой/не найден транскрипт встречи {args.meeting_id[0]}. "
                  "Проверь SUPABASE_URL/SUPABASE_KEY в окружении (те же, что у backend).",
                  file=sys.stderr)
            sys.exit(2)
        return t
    # sample-транскрипт для прикидки (~2K токенов)
    return ("Встреча по проекту Acme. Иван: переносим релиз на след. неделю. "
            "Мария: риск по интеграции VK, нужен ключ. Решили: нанять DevOps. "
            "KPI: конверсия 3.2%, цель 5%. Задача: подготовить ТЗ к пятнице.\n") * 40


async def _live_row(transcript: str, strategy: Dict[str, str]) -> Dict[str, Any] | None:
    """Реальный прогон CaptureOrchestrator на вызываемой модели (best-effort).

    Возвращает факт из usage_tracker или None, если модель невызываема здесь."""
    import time as _t
    try:
        from backend.core.capture.orchestrator import CaptureOrchestrator
        from backend.core.llm.usage_tracker import UsageTracker
        ut = UsageTracker.get_instance()
        before = dict(ut._session_stats)  # noqa: SLF001 — снимок до
        orch = CaptureOrchestrator()
        # single → гоняем один агент (комбо), multi → все
        orch.config["parallel_execution"] = True
        agents = None if strategy["mode"] == "multi" else [list(orch.agents)[0]]
        t0 = _t.monotonic()
        await orch._run_agents_parallel(transcript, {}, agents or list(orch.agents))  # noqa: SLF001
        dt = _t.monotonic() - t0
        after = ut._session_stats  # noqa: SLF001
        d_in = after["total_input_tokens"] - before["total_input_tokens"]
        d_out = after["total_output_tokens"] - before["total_output_tokens"]
        d_cost = after["total_cost"] - before["total_cost"]
        d_calls = after["requests_count"] - before["requests_count"]
        return {"name": strategy["name"], "model": strategy["model"], "mode": strategy["mode"],
                "source": "LIVE", "calls": d_calls, "in": d_in, "out": d_out,
                "time_s": round(dt, 1), "usd": round(d_cost, 4)}
    except Exception as e:  # pragma: no cover
        print(f"   (live недоступен для {strategy['name']}: {e})", file=sys.stderr)
        return None


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Бенч прогона встречи разными моделями/способами")
    ap.add_argument("--file", help="путь к файлу транскрипта")
    ap.add_argument("--meeting-id", action="append", help="id встречи (можно несколько)")
    ap.add_argument("--live", action="store_true", help="реальный прогон на вызываемых моделях")
    ap.add_argument("--catalog", action="store_true",
                    help="таблица по каталогу (тир→плановый шаг лестницы)")
    ap.add_argument("--models", help="только эти модели (через запятую), напр. opus-4.8,sonnet-5")
    ap.add_argument("--ops", type=int, metavar="N",
                    help="ops-стоимость: ночная консолидация на N новых встреч + поиск по уровням")
    ap.add_argument("--model", default="gemini-3.1-flash-lite",
                    help="модель для --ops (дефолт flash-lite)")
    ap.add_argument("--matrix", action="store_true",
                    help="матрица: модель × шаги теста × маршрут вызова (OpenRouter/свой ключ)")
    ap.add_argument("--out", default="bench_outputs", help="куда сохранять ответы моделей (--live)")
    ap.add_argument("--dry", action="store_true", help="с --live: показать маршруты, НЕ звать модели")
    ap.add_argument("--no-strict", action="store_true",
                    help="с --live: выключить 4 правила схемы (strict_schema=False) для сравнения до/после")
    ap.add_argument("--list-meetings", action="store_true", help="список встреч (нужен --user-id)")
    ap.add_argument("--user-id", help="твой user_id (uuid) для --list-meetings")
    ap.add_argument("--or-models", metavar="QUERY",
                    help="найти реальные слаги OpenRouter по подстроке (для правки OPENROUTER_ID)")
    args = ap.parse_args(argv)

    # автозагрузка .env (SUPABASE_*, *_API_KEY) — как у backend, best-effort
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    only = [s.strip() for s in args.models.split(",")] if args.models else None

    if args.or_models:
        import asyncio
        hits = asyncio.run(_or_models(args.or_models))
        if not hits:
            print(f"нет моделей OpenRouter с '{args.or_models}'")
        for mid, pin, pout in hits:
            print(f"{mid:<48} in={pin} out={pout}")
        print(f"\nВпиши точный id в scripts/model_catalog.py → OPENROUTER_ID.")
        return

    if args.list_meetings:
        import asyncio
        if not args.user_id:
            print("нужен --user-id <uuid>. Взять можно из URL/логов backend.", file=sys.stderr)
            return
        for m in asyncio.run(_list_meetings(args.user_id)):
            print(f"{m.get('id')}  {str(m.get('title') or '')[:60]:<60}  [{m.get('status', '')}]")
        return

    if args.matrix:
        print(_fmt_matrix(matrix_rows(only=only)))
        print("\nШаги теста = дефолт + соседний (нащупать границу качества). "
              "Маршрут: свой ключ провайдера в приоритете, иначе OpenRouter "
              "(один ключ ко всем). Логика — docs/MODEL_TIERING_RUNBOOK.md")
        return

    if args.ops is not None:
        print(_fmt_ops(ops_rows(args.ops, args.model), args.model))
        print("\nПрофили вызовов — оценка по коду (nightly_consolidation + уровни "
              "поиска). deep ~115K токенов/запрос. Сверить --live.")
        return

    transcript = _load_transcript(args)
    t_tok = estimate_tokens(transcript)
    print(f"Транскрипт: ~{t_tok} токенов ({len(transcript)} симв.)\n")

    if args.catalog and args.live:
        import asyncio
        from scripts.model_catalog import MODELS
        names = only or [m["name"] for m in MODELS]
        rows = asyncio.run(_live_catalog(transcript, names, args.out, dry=args.dry,
                                         strict=not args.no_strict))
        print(_fmt_live(rows, dry=args.dry))
        if not args.dry:
            print(f"\nОтветы сохранены в {args.out}/<модель>.json — прочти и оцени "
                  "качество (полнота decisions/tasks/KPI, валидность, галлюцинации).")
        else:
            print("\n(--dry: маршруты показаны, модели НЕ вызывались. Слаги OpenRouter "
                  "в model_catalog.OPENROUTER_ID — сверить по openrouter.ai/models.)")
        return

    if args.catalog:
        print(_fmt_catalog(catalog_rows(transcript, only=only)))
        print("\ncena: verified=сверено · estimate=ПЛЕЙСХОЛДЕР (сверить прайс) · "
              "self-host=≈0 ден. · кач: заполнить после чтения ответов. "
              "Логика запросов — docs/MODEL_TIERING_RUNBOOK.md")
        return

    rows = project(transcript, DEFAULT_STRATEGIES)

    if args.live:
        import asyncio
        for i, s in enumerate(DEFAULT_STRATEGIES):
            live = asyncio.run(_live_row(transcript, s))
            if live:
                rows[i] = live  # заменяем проекцию фактом

    print(_fmt_table(rows))
    print("\nПримечание: ПРОЕКЦИЯ — оценка по токен-профилю × цены; LIVE — факт "
          "usage_tracker. Qwen self-host: денежная стоимость ≈ 0 (GPU/время).")


if __name__ == "__main__":
    main()
