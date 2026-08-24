# -*- coding: utf-8 -*-
"""Маршрутизация модели извлечения по выбору тенанта: провайдер × уровень.

Идея (запрошено продуктом): в ЛК тенант выбирает ЧЕРЕЗ ЧТО гоняем систему и,
если хочет, вводит свой ключ. Плюс уровень: standard | premium. Роутер отдаёт
конкретную модель, её ТИР (а тир уже определяет число запросов через
tiered_extraction.extraction_plan — frontier=1, lite≈9) и куда бить нативным API.

ГЛАВНОЕ: формат меняется под качество модели. Мы не гоняем frontier по 30
запросов — крупная модель берёт встречу за 1-2 комбо-вызова, слабая дробит.
Всё нативными API (не через OpenRouter).

Дефолты (без ключа/без «использовать мой токен» — на ключах платформы):
  standard → gemini-3.1-flash-lite   premium → deepseek-4-pro

С ключом провайдера (тумблер «использовать мой токен» + ключ в Интеграциях):
  anthropic  standard sonnet-5        premium opus-4.8
  openai     standard chatgpt-5       premium chatgpt-5.5
  xai        standard grok-4-fast     premium grok-4.5
  qwen       standard qwen-3.6-27b    premium qwen-235b-a22b
  deepseek   standard deepseek-4-flash premium deepseek-4-pro
  google     standard gemini-3.1-flash-lite premium gemini-3.5-pro

Модуль чистый и флаг-независимый; включение в пайплайн — за feature-флагом."""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

# Провайдер платформы (наши ключи), когда тенант не подставляет свой.
PLATFORM = "platform"
TIERS = ("fast", "standard", "premium")

# provider → {fast, standard, premium} → имя модели (из scripts/model_catalog).
# fast — молниеносный слот для чата/быстрого поиска (латентность важнее глубины);
# standard/premium — уровни анализа. У провайдеров без мелкой модели fast=standard.
ROUTING: Dict[str, Dict[str, str]] = {
    PLATFORM:    {"fast": "gemini-3.1-flash-lite", "standard": "deepseek-4-pro",
                  "premium": "grok-4.5"},
    "anthropic": {"fast": "sonnet-5",              "standard": "sonnet-5",
                  "premium": "opus-4.8"},
    "openai":    {"fast": "chatgpt-5",             "standard": "chatgpt-5",
                  "premium": "chatgpt-5.5"},
    "xai":       {"fast": "grok-4-fast",           "standard": "grok-4-fast",
                  "premium": "grok-4.5"},
    "qwen":      {"fast": "qwen-3.6-27b",          "standard": "qwen-3.6-27b",
                  "premium": "qwen-235b-a22b"},
    "deepseek":  {"fast": "deepseek-4-flash",      "standard": "deepseek-4-pro",
                  "premium": "deepseek-4-pro"},
    "google":    {"fast": "gemini-3.1-flash-lite", "standard": "gemini-3.1-flash-lite",
                  "premium": "gemini-3.5-pro"},
}

# Платформенная цепочка деградации по уровню: если для модели нет серверного
# ключа (напр. XAI_API_KEY ещё не положили) — берём следующую, а не роняем
# premium сразу в flash-lite. Ключ появился → уровень апгрейдится сам.
# Это ДЕФОЛТ; порядок/состав можно переопределить через ENV (см. platform_chain).
PLATFORM_CHAIN: Dict[str, List[str]] = {
    "premium":  ["grok-4.5", "deepseek-4-pro", "gemini-3.1-flash-lite"],
    "standard": ["deepseek-4-pro", "gemini-3.1-flash-lite"],
    "fast":     ["gemini-3.1-flash-lite"],
}

# ENV-переопределение платформенной цепочки — менять фолбэк БЕЗ правки кода.
# Формат: список моделей через запятую (или ';'), по одной переменной на уровень:
#   TESSENT_PLATFORM_CHAIN_PREMIUM="grok-4.5,deepseek-4-pro,gemini-3.1-flash-lite"
#   TESSENT_PLATFORM_CHAIN_STANDARD="deepseek-4-pro,gemini-3.1-flash-lite"
#   TESSENT_PLATFORM_CHAIN_FAST="gemini-3.1-flash-lite"
# Пустая/битая переменная (ни одной известной модели) → дефолт PLATFORM_CHAIN,
# т.е. фолбэк НИКОГДА не роняется даже при опечатке в env.
_PLATFORM_CHAIN_ENV: Dict[str, str] = {
    "premium":  "TESSENT_PLATFORM_CHAIN_PREMIUM",
    "standard": "TESSENT_PLATFORM_CHAIN_STANDARD",
    "fast":     "TESSENT_PLATFORM_CHAIN_FAST",
}


def platform_chain(level: str) -> List[str]:
    """Платформенная цепочка деградации для уровня: ENV-переопределение →
    дефолт PLATFORM_CHAIN. Из ENV берём только известные модели (есть тир И
    нативный провайдер) — незнакомые/опечатки отбрасываем; если после фильтра
    пусто, возвращаем дефолт. Читается на каждый вызов (env применяется после
    рестарта, код трогать не надо). Never-raise."""
    default = PLATFORM_CHAIN.get(level, [])
    env_name = _PLATFORM_CHAIN_ENV.get(level)
    raw = os.environ.get(env_name, "") if env_name else ""
    if not (raw or "").strip():
        return list(default)
    names = [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]
    valid = [n for n in names if n in MODEL_TIER and native_provider_for(n)]
    return valid or list(default)

# Тир модели → определяет ПЛАН запросов (число вызовов на встречу). Держим тут
# копию для маршрутизируемых моделей, чтобы не тянуть scripts/ в backend.
MODEL_TIER: Dict[str, str] = {
    "gemini-3.1-flash-lite": "lite",
    "deepseek-4-flash": "lite",
    "deepseek-4-pro": "mid",
    "qwen-3.6-27b": "mid",
    "grok-4-fast": "mid",
    "qwen-235b-a22b": "strong",
    "sonnet-5": "strong",
    "gemini-3.5-pro": "strong",
    "opus-4.8": "frontier",
    "chatgpt-5": "frontier",
    "chatgpt-5.5": "frontier",
    "grok-4.5": "frontier",
}

# Тир → плановое число вызовов (справочно для UI; факт даёт extraction_plan).
TIER_CALLS = {"lite": 9, "mid": 3, "strong": 2, "frontier": 1}

# Нативные эндпоинты (НЕ OpenRouter). auth: 'x-api-key' (Anthropic),
# 'bearer' (OpenAI-совместимые), 'google' (ключ в query ?key=).
NATIVE_ENDPOINT: Dict[str, Dict[str, str]] = {
    "anthropic": {"base": "https://api.anthropic.com/v1/messages", "auth": "x-api-key",
                  "env": "ANTHROPIC_API_KEY"},
    "openai":    {"base": "https://api.openai.com/v1/chat/completions", "auth": "bearer",
                  "env": "OPENAI_API_KEY"},
    "xai":       {"base": "https://api.x.ai/v1/chat/completions", "auth": "bearer",
                  "env": "XAI_API_KEY"},
    "deepseek":  {"base": "https://api.deepseek.com/v1/chat/completions", "auth": "bearer",
                  "env": "DEEPSEEK_API_KEY"},
    "qwen":      {"base": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions",
                  "auth": "bearer", "env": "DASHSCOPE_API_KEY"},
    "google":    {"base": "https://generativelanguage.googleapis.com/v1beta/models",
                  "auth": "google", "env": "GEMINI_API_KEY"},
    # Moonshot (Kimi) — OpenAI-совместимый; базовый URL можно переопределить env
    # (KIMI_BASE_URL/MOONSHOT_BASE_URL), т.к. регион/домен у тенанта может отличаться.
    "moonshot":  {"base": (os.environ.get("KIMI_BASE_URL")
                           or os.environ.get("MOONSHOT_BASE_URL")
                           or "https://api.moonshot.ai/v1/chat/completions"),
                  "auth": "bearer", "env": "MOONSHOT_API_KEY"},
}

# Куда бить нативно для данной модели (провайдер API). Совпадает с
# scripts.model_catalog.native_provider, но без зависимости от scripts/.
_NATIVE_BY_PREFIX = (
    (("opus", "sonnet", "fable"), "anthropic"),
    (("chatgpt", "gpt"), "openai"),
    (("grok",), "xai"),
    (("deepseek",), "deepseek"),
    (("qwen",), "qwen"),
    (("gemini",), "google"),
    (("kimi", "moonshot"), "moonshot"),
)


def native_provider_for(model_name: str) -> Optional[str]:
    for prefixes, prov in _NATIVE_BY_PREFIX:
        if model_name.startswith(prefixes):
            return prov
    return None  # напр. gemma/kimi/glm/minimax/nemotron — нет прямого нативного тут


# Имя модели каталога → нативный model-id провайдера. Публичные id (openai/xai/
# qwen/gemini/deepseek) — здесь; СВЕРИТЬ по докам провайдеров. Anthropic-id НЕ
# зашиваем в репозиторий — берём из env (ANTHROPIC_STANDARD_MODEL/_PREMIUM_MODEL).
_PUBLIC_MODEL_ID = {
    "chatgpt-5": "gpt-5",
    "chatgpt-5.5": "gpt-5.5",
    "grok-4.5": "grok-4.5",
    "grok-4-fast": "grok-4-fast",
    "qwen-3.6-27b": "qwen3.6-27b",
    "qwen-235b-a22b": "qwen3-235b-a22b",
    "deepseek-4-flash": "deepseek-v4-flash",
    "deepseek-4-pro": "deepseek-v4-pro",
    # Google: сверено с живым /v1beta/models. gemini-3.1-flash-lite существует;
    # «gemini-3.5-pro» как id в API НЕТ → используем алиас gemini-pro-latest
    # (паттерн легаси-роутера: *-latest всегда указывает на актуальную модель).
    "gemini-3.1-flash-lite": "gemini-3.1-flash-lite",
    "gemini-3.5-pro": "gemini-pro-latest",
}
# Переопределение id через env (реальный id может отличаться от публичного).
# Anthropic-id в репозиторий не зашиваем вовсе — только env.
_MODEL_ID_ENV = {
    "sonnet-5": "ANTHROPIC_STANDARD_MODEL",
    "opus-4.8": "ANTHROPIC_PREMIUM_MODEL",
    "gemini-3.1-flash-lite": "GEMINI_STANDARD_MODEL",
    "gemini-3.5-pro": "GEMINI_PREMIUM_MODEL",
}
_ANTHROPIC_ENV = {k: v for k, v in _MODEL_ID_ENV.items() if v.startswith("ANTHROPIC")}


def native_model_id(model_name: str) -> Optional[str]:
    """Нативный id модели для API провайдера (None — если неизвестен).

    Порядок: env-переопределение → публичная таблица. Для Anthropic env —
    единственный источник (в код id не зашиваем)."""
    import os
    env = _MODEL_ID_ENV.get(model_name)
    if env and os.environ.get(env):
        return os.environ[env]
    if model_name in _ANTHROPIC_ENV:
        return None  # anthropic без env — нет id
    return _PUBLIC_MODEL_ID.get(model_name)


def _model_tier(model_name: str) -> str:
    return MODEL_TIER.get(model_name, "mid")


def plan_calls(tier: str) -> int:
    """Число вызовов на встречу для тира (факт — из extraction_plan)."""
    try:
        from backend.core.capture.tiered_extraction import extraction_plan
        return len(extraction_plan(tier))
    except Exception:  # noqa: BLE001 — справочная величина
        return TIER_CALLS.get(tier, 3)


def resolve(
    tier: str = "standard",
    *,
    provider: str = PLATFORM,
    use_own_key: bool = False,
    available_keys: Optional[set] = None,
) -> Dict[str, Any]:
    """Как гонять систему для этого тенанта.

    tier            — 'standard' | 'premium' (по умолчанию standard);
    provider        — что выбрал тенант в ЛК ('platform' или anthropic/openai/…);
    use_own_key     — тумблер «использовать мой токен»;
    available_keys  — провайдеры, у которых реально лежит ключ тенанта.

    Возврат: {provider, key_source, model, tier(модели), native, endpoint,
    planned_calls, fallback}. Если выбранный провайдер недоступен (нет ключа /
    тумблер выключен) — откат на PLATFORM (наши ключи)."""
    tier = tier if tier in TIERS else "standard"
    available_keys = available_keys or set()
    fallback = None

    # Своим ключом гоняем, только если: тумблер включён, провайдер известен,
    # он не платформа и ключ реально есть.
    want_own = use_own_key and provider in ROUTING and provider != PLATFORM
    if want_own and provider not in available_keys:
        want_own = False
        fallback = f"нет ключа {provider} → платформа"

    if want_own:
        model = ROUTING[provider][tier]
        key_source = "user"
        eff_provider = provider
    else:
        # Голова платформенной цепочки уровня (учитывает ENV-переопределение);
        # при пустом фильтре — жёсткий дефолт из ROUTING.
        chain = platform_chain(tier)
        model = chain[0] if chain else ROUTING[PLATFORM][tier]
        key_source = "platform"
        eff_provider = PLATFORM

    m_tier = _model_tier(model)
    native = native_provider_for(model)
    endpoint = NATIVE_ENDPOINT.get(native, {}).get("base") if native else None
    return {
        "provider": eff_provider,
        "requested_provider": provider,
        "key_source": key_source,          # 'user' | 'platform'
        "model": model,
        "tier": m_tier,                    # тир МОДЕЛИ → план запросов
        "level": tier,                     # standard | premium
        "native": native,                  # провайдер нативного API
        "endpoint": endpoint,              # нативный URL (НЕ OpenRouter)
        "planned_calls": plan_calls(m_tier),
        "fallback": fallback,
    }


def options_for_ui() -> List[Dict[str, Any]]:
    """Для ЛК: список провайдеров и что включится на fast/standard/premium."""
    out = []
    for prov, lv in ROUTING.items():
        row: Dict[str, Any] = {"provider": prov, "needs_key": prov != PLATFORM}
        for t in TIERS:
            # Для платформы показываем реально активную модель (голову цепочки
            # с учётом ENV-переопределения), чтобы вкладка «Модели» не врала.
            model = (platform_chain(t) or [lv[t]])[0] if prov == PLATFORM else lv[t]
            row[t] = {"model": model, "tier": _model_tier(model),
                      "calls": plan_calls(_model_tier(model))}
        out.append(row)
    return out
