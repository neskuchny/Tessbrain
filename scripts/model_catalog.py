# -*- coding: utf-8 -*-
"""Каталог моделей для адаптивного тиринга прогона встреч.

ВАЖНО про цены (src):
  - "verified"  — сверено (Anthropic из claude-api skill; Gemini из usage_tracker)
  - "estimate"  — ПЛЕЙСХОЛДЕР по паттерну провайдера, СВЕРИТЬ по прайсу перед
    финансовыми решениями (chatgpt/deepseek/kimi/glm/nemotron/qwen/gemma и др.
    на дату могли измениться или ещё не выйти публично)

Тиры мощности (кто с кем конкурирует):
  - lite     — дёшево/быстро, слабое следование сложной схеме → МНОГО запросов
  - mid      — баланс, держит 4-6 сгруппированных запросов
  - strong   — near-frontier, надёжно держит 2-3 запроса
  - frontier — 1 комбо-запрос на встречу (экстракция+верификация+синтез)

`runs` — плановое число запросов/встречу ПО ЛОГИКЕ лестницы (§ ниже и
docs/MODEL_TIERING_RUNBOOK.md): чем крупнее модель, тем меньше запросов.
Цены — $/1M токенов (in/out)."""
from __future__ import annotations

from typing import Any, Dict, List

# Лестница сокращения запросов (ПОЛНЫЙ пайплайн: извлечение + downstream).
# База — реальные ~30 вызовов (17 логических × под-вызовы/ретраи). Каждый шаг
# группирует и извлечение, и downstream.
LADDER = {
    "S0": {"runs": 30, "label": "full-split (ТЕКУЩЕЕ ~30)", "groups": [
        "извлечение: 9 секций посекционно (+под-вызовы) ~18",
        "downstream: 7 фаз по отдельности (+ретраи) ~12"]},
    "S1": {"runs": 12, "label": "layered", "groups": [
        "извлечение → 6 групп (facts·interpretation·verification + под-вызовы) ~6",
        "downstream → 6 сгруппированных фаз ~6"]},
    "S2": {"runs": 5, "label": "paired", "groups": [
        "извлечение → 2 (факты+интерпретация+верификация · доп) ~2",
        "downstream → 3 (синтез · артефакты · валидация) ~3"]},
    "S3": {"runs": 2, "label": "dual", "groups": [
        "извлечение+верификация+синтез — один структурный проход ~1",
        "артефакты/валидация (output-heavy — можно на модели дешевле) ~1"]},
    "S4": {"runs": 1, "label": "single", "groups": [
        "всё за один комбо-проход (извлечение+верификация+синтез)",
        "NB: генерацию длинных артефактов (ТЗ/док) на frontier лучше выносить "
        "отдельно (выход дорогой) → тогда фактически 2"]},
}

# tier → шаг лестницы (плановые запросы). lite ОСТАЁТСЯ на S0 (~30): слабой
# модели группировка не по силам (рвёт комбо-JSON, путается). Спускаются по
# лестнице (30→12→5→1) ТОЛЬКО модели покрупнее.
TIER_STEP = {"lite": "S0", "mid": "S1", "strong": "S2", "frontier": "S4"}
# грубая скорость вывода (tok/s), эвристика для сравнения латентности
TIER_SPEED = {"lite": 180, "mid": 120, "strong": 70, "frontier": 45}

# Каталог. price = (in, out) $/1M. competes = кто из списка в том же тире.
MODELS: List[Dict[str, Any]] = [
    # --- lite (дёшево/быстро, нужно много запросов) ---
    {"name": "gemini-3.1-flash-lite", "tier": "lite", "in": 0.10, "out": 0.40,
     "src": "verified", "competes": ["deepseek-4-flash", "gemma-4-31b"]},
    {"name": "deepseek-4-flash", "tier": "lite", "in": 0.14, "out": 0.28,
     "src": "verified", "competes": ["gemini-3.1-flash-lite", "gemma-4-31b"],
     "note": "сверено 14.08.2026 по api-docs.deepseek.com (cache miss); было "
             "0.09/0.18 — прайс изменился. Кэш-попадание дешевле в ~50 раз "
             "($0.0028), в расчёт не берём: доля попаданий не измерена"},
    {"name": "gemma-4-31b", "tier": "lite", "in": 0.12, "out": 0.35,
     "src": "verified", "competes": ["deepseek-4-flash", "gemini-3.1-flash-lite"],
     "note": "open: через провайдера $0.12/$0.35; self-host ≈ 0 ден. (GPU/время)"},

    # --- mid (баланс, 4-6 сгруппированных запросов) ---
    {"name": "gemini-3-flash", "tier": "mid", "in": 0.30, "out": 2.50,
     "src": "verified", "competes": ["qwen-3.7-plus", "deepseek-4-pro", "glm-5.2"]},
    {"name": "qwen-3.6-27b", "tier": "mid", "in": 0.285, "out": 2.40,
     "src": "verified", "competes": ["qwen-3.6-plus", "gemini-3-flash"],
     "note": "open: возможен self-host ≈ 0 ден."},
    {"name": "qwen-3.6-plus", "tier": "mid", "in": 0.325, "out": 1.95,
     "src": "verified", "competes": ["qwen-3.6-27b", "qwen-3.7-plus"]},
    {"name": "qwen-3.7-plus", "tier": "mid", "in": 0.32, "out": 1.28,
     "src": "verified", "competes": ["gemini-3-flash", "deepseek-4-pro"]},
    {"name": "deepseek-4-pro", "tier": "mid", "in": 0.435, "out": 0.870,
     "src": "verified", "competes": ["glm-5.2", "qwen-3.7-plus"],
     "note": "сверено 14.08.2026 — цена совпала. За алиасом deepseek-v4-pro "
             "версия DeepSeek-V4-Pro-0813 (обновляется провайдером без смены "
             "id). ВНИМАНИЕ: с 16.08.2026 провайдер вводит peak/off-peak "
             "тарификацию по UTC — эти числа станут одной из двух ставок"},
    {"name": "glm-5.2", "tier": "mid", "in": 0.56, "out": 1.76,
     "src": "verified", "competes": ["deepseek-4-pro", "gemini-3-flash"]},
    {"name": "kimi-k2.6", "tier": "mid", "in": 0.65, "out": 3.41,
     "src": "verified", "competes": ["kimi-k2.7-code", "gemini-3-flash"]},
    {"name": "kimi-k2.7-code", "tier": "mid", "in": 0.74, "out": 3.50,
     "src": "verified", "competes": ["kimi-k2.6", "gemini-3-flash"],
     "note": "код-профиль; на извлечении встреч сверить качество"},
    {"name": "minimax-m3", "tier": "mid", "in": 0.30, "out": 1.20,
     "src": "estimate", "competes": ["glm-5.2", "qwen-3.7-plus"],
     "note": "MoE; ставка ПЛЕЙСХОЛДЕР — сверить по прайсу OpenRouter"},
    {"name": "mimo-2.5-pro", "tier": "mid", "in": 0.30, "out": 1.20,
     "src": "estimate", "competes": ["qwen-3.6-plus", "glm-5.2"],
     "note": "Xiaomi MiMo; ставка ПЛЕЙСХОЛДЕР — сверить"},
    {"name": "qwen-3.5-122b-a10b", "tier": "mid", "in": 0.25, "out": 1.00,
     "src": "estimate", "competes": ["qwen-3.7-plus", "deepseek-4-pro"],
     "note": "open MoE 122B/10B-active; self-host на 1×H100@INT4; ставка ПЛЕЙСХОЛДЕР"},
    {"name": "hy3-preview", "tier": "mid", "in": 0.30, "out": 1.20,
     "src": "estimate", "competes": ["qwen-3.6-plus", "glm-5.2", "minimax-m3"],
     "note": "Tencent Hunyuan 3 (preview). CSV-прогон: РВЁТ JSON на комбо-группах "
             "(5 парс-фейлов, facts/verification пусто) — для извлечения НЕ брать"},
    {"name": "hy3-free", "tier": "mid", "in": 0.0, "out": 0.0,
     "src": "estimate", "competes": ["hy3-preview", "qwen-3.6-27b"],
     "note": "Tencent Hunyuan 3 (:free). Прогон: рабочий середняк (part=3, KPI с ед., "
             "fc=7), но медленный ~342с и KPI округляет; ден.~0, но лимиты :free"},

    # --- strong (near-frontier, 2-3 запроса) ---
    {"name": "qwen-3.7-max", "tier": "strong", "in": 1.20, "out": 2.40,
     "src": "measured", "competes": ["gemini-3.5-pro", "qwen-3.5-397b-a17b"],
     "note": "нативный Alibaba по CSV 2026-07: blended ~$1.6/1M (было est out 6.00)"},
    {"name": "qwen-3.5-397b-a17b", "tier": "strong", "in": 0.50, "out": 2.00,
     "src": "estimate", "competes": ["qwen-3.7-max", "nemotron-3.7-ultra", "glm-5.2"],
     "note": "open MoE 397B/17B-active; self-host на 2×H100@INT4; ставка ПЛЕЙСХОЛДЕР"},
    {"name": "qwen-235b-a22b", "tier": "strong", "in": 0.45, "out": 1.80,
     "src": "estimate", "competes": ["qwen-3.5-397b-a17b", "glm-5.2"],
     "note": "premium для qwen-ключа (Qwen3-235B-A22B); native Alibaba DashScope; ставка ПЛЕЙСХОЛДЕР"},
    {"name": "grok-4-fast", "tier": "mid", "in": 0.40, "out": 2.00,
     "src": "estimate", "competes": ["deepseek-4-pro", "gemini-3-flash"],
     "note": "standard для grok-ключа (меньше/быстрее grok-4.5); слаг/ставку СВЕРИТЬ"},
    {"name": "sonnet-5", "tier": "strong", "in": 3.00, "out": 15.00,
     "src": "verified", "competes": ["gemini-3.5-pro", "nemotron-3.7-ultra"],
     "note": "интро $2/$10 до 2026-08-31"},
    {"name": "gemini-3.5-pro", "tier": "strong", "in": 1.25, "out": 10.00,
     "src": "estimate", "competes": ["sonnet-5", "nemotron-3.7-ultra"]},
    {"name": "nemotron-3.7-ultra", "tier": "strong", "in": 1.00, "out": 4.00,
     "src": "estimate", "competes": ["sonnet-5", "gemini-3.5-pro"],
     "note": "open-large; часто self-host → ден. стоимость ниже"},

    # --- frontier (1 комбо-запрос/встреча) ---
    {"name": "opus-4.8", "tier": "frontier", "in": 5.00, "out": 25.00,
     "src": "verified", "competes": ["chatgpt-5.5", "grok-4.5", "fugu-ultra"]},
    {"name": "fable-5", "tier": "frontier", "in": 10.00, "out": 50.00,
     "src": "verified", "competes": ["opus-4.8", "fugu-ultra"],
     "note": "Anthropic, самая мощная; thinking always-on → медленнее/дороже"},
    {"name": "chatgpt-5", "tier": "frontier", "in": 1.25, "out": 10.00,
     "src": "estimate", "competes": ["opus-4.8", "chatgpt-5.5", "grok-4.5"]},
    {"name": "chatgpt-5.5", "tier": "frontier", "in": 5.00, "out": 30.00,
     "src": "verified", "competes": ["opus-4.8", "chatgpt-5", "grok-4.5"]},
    {"name": "grok-4.5", "tier": "frontier", "in": 3.00, "out": 3.00,
     "src": "measured", "competes": ["opus-4.8", "chatgpt-5.5"],
     "note": "нативный xAI по CSV 2026-07: ~$0.062/встреча (было est 3/15 → $0.127, завышено)"},
    {"name": "fugu-ultra", "tier": "frontier", "in": 5.00, "out": 25.00,
     "src": "estimate", "competes": ["opus-4.8", "fable-5"],
     "note": "происхождение/ставку сверить — неизвестный провайдер"},
]


def planned_runs(model: Dict[str, Any]) -> int:
    return LADDER[TIER_STEP[model["tier"]]]["runs"]


def by_name(name: str) -> Dict[str, Any] | None:
    return next((m for m in MODELS if m["name"] == name), None)


# --- Маршрутизация: свой ключ провайдера ИЛИ OpenRouter ---------------------
# native_provider: у кого есть прямой API (свой ключ). Остальные (open/прочие) —
# только через OpenRouter. Провайдеры совпадают с user_credentials._PROVIDER_ENV.
def native_provider(name: str) -> str | None:
    if name.startswith(("opus", "sonnet", "fable")):
        return "anthropic"
    if name.startswith("chatgpt"):
        return "openai"
    if name.startswith("deepseek"):
        return "deepseek"
    if name.startswith("grok"):
        return "xai"
    if name.startswith("gemini"):
        return "google"
    if name.startswith("qwen"):
        return "qwen"        # Alibaba DashScope (OpenAI-совместимый)
    return None  # kimi/glm/gemma/nemotron/minimax/mimo/fugu — через OpenRouter


# OpenRouter-слаги. ПОДТВЕРЖДЕНЫ пользователем по openrouter.ai/api/v1/models
# (2026-07). Полный сырой список — scripts/openrouter_slugs.txt.
OPENROUTER_ID = {
    "gemini-3.1-flash-lite": "google/gemini-3.1-flash-lite",
    "gemini-3-flash": "google/gemini-3-flash-preview",
    "gemini-3.5-pro": "google/gemini-3.1-pro-preview",  # на OR pro = 3.1-pro-preview
    "deepseek-4-flash": "deepseek/deepseek-v4-flash",
    "deepseek-4-pro": "deepseek/deepseek-v4-pro",
    "qwen-3.6-27b": "qwen/qwen3.6-27b",
    "qwen-3.6-plus": "qwen/qwen3.6-plus",
    "qwen-3.7-plus": "qwen/qwen3.7-plus",
    "gemma-4-31b": "google/gemma-4-31b-it",
    "glm-5.2": "z-ai/glm-5.2",
    "kimi-k2.6": "moonshotai/kimi-k2.6",
    "kimi-k2.7-code": "moonshotai/kimi-k2.7-code",
    # платный эндпоинт (было :free — падал DEGRADED, не вызывался)
    "nemotron-3.7-ultra": "nvidia/nemotron-3-ultra-550b-a55b",
    "minimax-m3": "minimax/minimax-m3",
    "mimo-2.5-pro": "xiaomi/mimo-v2.5-pro",
    "qwen-3.7-max": "qwen/qwen3.7-max",
    "qwen-3.5-397b-a17b": "qwen/qwen3.5-397b-a17b",
    "qwen-3.5-122b-a10b": "qwen/qwen3.5-122b-a10b",
    "qwen-235b-a22b": "qwen/qwen3-235b-a22b-2507",
    "grok-4-fast": "x-ai/grok-4-fast",   # слаг СВЕРИТЬ
    "hy3-preview": "tencent/hy3-preview",
    "hy3-free": "tencent/hy3:free",
    "sonnet-5": "anthropic/claude-sonnet-5",  # обычно идёт native, это fallback
    "opus-4.8": "anthropic/claude-opus-4.8",
    "fable-5": "anthropic/claude-fable-5",
    "grok-4.5": "x-ai/grok-4.5",
    "chatgpt-5.5": "openai/gpt-5.5",
    # НЕ подтверждены на OR (нет в списке) — оставлены как есть/убраны:
    "chatgpt-5": "openai/gpt-5",   # gpt-5 в списке не было — сверить
    # fugu-ultra — на OpenRouter не найден, слага нет (пойдёт «нет маршрута»)
}


def resolve_route(name: str, available_keys: set[str]) -> Dict[str, Any]:
    """Как звать модель: свой ключ провайдера (приоритет) ИЛИ OpenRouter.

    available_keys — множество провайдеров, у которых есть ключ (из Интеграций):
    напр. {"anthropic","openrouter","deepseek"}."""
    nat = native_provider(name)
    if nat and nat in available_keys:
        return {"via": "native", "provider": nat, "model_id": name}
    if "openrouter" in available_keys and name in OPENROUTER_ID:
        return {"via": "openrouter", "provider": "openrouter",
                "model_id": OPENROUTER_ID[name]}
    return {"via": "none", "provider": None, "model_id": None,
            "need": ([nat] if nat else []) + ["openrouter"]}


# --- Шаги-для-теста: не только дефолт, но и соседние (нащупать границу качества) ---
def test_steps(tier: str) -> List[str]:
    """Какие шаги лестницы прогнать для модели этого тира.

    Дефолт + соседний «на шаг агрессивнее» (меньше запросов) — чтобы увидеть,
    где ломается качество/JSON. lite тоже пробуем сгруппировать (S1)."""
    return {
        "lite": ["S0", "S1"],        # текущее + попытка группировки
        "mid": ["S1", "S2"],         # 12 и 5 запросов
        "strong": ["S2", "S3"],      # 5 и 2
        "frontier": ["S4", "S3"],    # 1 и 2 (fallback если рвётся)
    }.get(tier, ["S0"])
