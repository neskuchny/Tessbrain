# -*- coding: utf-8 -*-
"""Склейка выбора тенанта → готовый нативный вызыватель для извлечения встречи.

Читает per-user маршрут (user_llm_pref.get_extraction_route) + доступные ключи
(Интеграции пользователя / env платформы) → model_router.resolve → собирает
native_callers-вызыватель нативным API. Всё never-raise: при любой нехватке
(нет ключа/model-id) возвращаем caller=None — пайплайн откатывается на текущий
дефолтный LLM (логика извлечения не ломается).

Включается за флагом enable_tiered_extraction на стороне вызывающего."""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from backend.core.llm import model_router as R

logger = logging.getLogger(__name__)

# router-провайдер → (integration-провайдер для ключа пользователя, env платформы)
_PROVIDER_MAP = {
    "anthropic": ("anthropic", ("ANTHROPIC_API_KEY",)),
    "openai":    ("openai",    ("OPENAI_API_KEY",)),
    "xai":       ("xai",       ("XAI_API_KEY",)),
    "qwen":      ("qwen",      ("DASHSCOPE_API_KEY", "QWEN_API_KEY")),
    "deepseek":  ("deepseek",  ("DEEPSEEK_API_KEY",)),
    "google":    ("google_ai", ("GEMINI_API_KEY", "GOOGLE_API_KEY")),
    "moonshot":  ("moonshot",  ("MOONSHOT_API_KEY", "KIMI_API_KEY")),
}


def _platform_key(router_provider: Optional[str]) -> Optional[str]:
    """Ключ платформы (env) для нативного провайдера."""
    envs = (_PROVIDER_MAP.get(router_provider or "", (None, ()))[1])
    for e in envs:
        if os.environ.get(e):
            return os.environ[e]
    return None


async def _user_key(user_id: str, router_provider: str) -> Optional[str]:
    """Ключ пользователя из Интеграций для провайдера (None — если нет)."""
    integ = _PROVIDER_MAP.get(router_provider, (None, ()))[0]
    if not integ:
        return None
    try:
        from backend.api.routes.integrations import get_user_integration_keys
        keys = await get_user_integration_keys(user_id, integ)
        return (keys or {}).get("api_key") or None
    except Exception:
        logger.debug("user key lookup failed for %s", router_provider, exc_info=True)
        return None


async def _available_user_providers(user_id: str) -> set:
    """router-провайдеры, у которых реально лежит ключ пользователя."""
    out = set()
    for rp in _PROVIDER_MAP:
        if await _user_key(user_id, rp):
            out.add(rp)
    return out


async def build_extraction_route(user_id: str, *,
                                 level_override: Optional[str] = None) -> Dict[str, Any]:
    """Полный маршрут + готовый caller (или None → откат на дефолт).

    level_override — уровень по политике workload (см. workload_policy);
    None → берём выбор тенанта из ЛК. Провайдер/тумблер всегда из ЛК.
    Возврат: {..resolve(), 'model_id', 'caller', 'reason'}."""
    pref = None
    try:
        from backend.core.llm.user_llm_pref import get_extraction_route
        pref = get_extraction_route(user_id)
    except Exception:
        pref = {"provider": "platform", "level": "standard", "use_own_key": False}

    level = level_override or pref["level"]
    avail = await _available_user_providers(user_id) if pref["use_own_key"] else set()
    route = R.resolve(level, provider=pref["provider"],
                      use_own_key=pref["use_own_key"], available_keys=avail)

    # Платформенный маршрут: если для модели уровня нет серверного ключа
    # (напр. XAI_API_KEY ещё не добавили) — деградируем по цепочке уровня
    # (grok-4.5 → deepseek-4-pro → flash-lite), а не роняем качество сразу.
    if route["key_source"] == "platform":
        for cand in (R.platform_chain(route["level"]) or [route["model"]]):
            c_native = R.native_provider_for(cand)
            if c_native and R.native_model_id(cand) and _platform_key(c_native):
                if cand != route["model"]:
                    route["fallback"] = ((route.get("fallback") or "") +
                                         f" | платформа: {route['model']}→{cand} (нет ключа)").strip(" |")
                    route.update({"model": cand, "tier": R.MODEL_TIER.get(cand, "mid"),
                                  "native": c_native,
                                  "endpoint": R.NATIVE_ENDPOINT.get(c_native, {}).get("base"),
                                  "planned_calls": R.plan_calls(R.MODEL_TIER.get(cand, "mid"))})
                break

    native = route.get("native")
    model_id = R.native_model_id(route["model"])
    caller = None
    reason = None

    if not native:
        reason = f"{route['model']}: нет нативного провайдера (нужен OpenRouter/локально)"
    elif not model_id:
        reason = f"{route['model']}: не задан нативный model-id ({native})"
    else:
        # ключ: свой (если key_source=user) иначе платформенный env
        key = (await _user_key(user_id, native)) if route["key_source"] == "user" \
            else _platform_key(native)
        if not key:
            reason = f"{native}: нет ключа ({route['key_source']}) → откат на дефолт"
        else:
            try:
                from backend.core.llm.native_callers import make_caller
                caller = make_caller(native, model_id, key)
            except Exception as e:  # noqa: BLE001
                reason = f"caller build failed: {e}"

    return {**route, "model_id": model_id, "caller": caller, "reason": reason}


def _has_content(res: Optional[Dict[str, Any]]) -> bool:
    """В результате есть хоть одна непустая секция?"""
    secs = (res or {}).get("sections") or {}
    return any(isinstance(v, list) and v for v in secs.values())


def google_fallback_caller():
    """Платформенный нативный Gemini как страховка (ключ из env). None — если
    ключа/model-id нет. Это тот же Google, что уже используется в проде."""
    key = _platform_key("google")
    if not key:
        return None
    import os
    model_id = os.environ.get("GEMINI_FALLBACK_MODEL") \
        or R.native_model_id("gemini-3.1-flash-lite")
    if not model_id:
        return None
    try:
        from backend.core.llm.native_callers import make_caller
        return make_caller("google", model_id, key)
    except Exception:
        return None


async def _run_with(caller, transcript: str, tier: str, strict_schema: bool,
                    meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Один прогон run_tiered данным вызывателем. None при исключении/пустом."""
    try:
        from backend.core.capture.tiered_extraction import run_tiered
        res = await run_tiered(transcript, llm=None, tier=tier,
                               generate=caller.generate, strict_schema=strict_schema)
    except Exception as e:  # noqa: BLE001
        logger.warning("run_tiered упал (%s): %s", meta.get("model"), e)
        res = None
    finally:
        try:
            await caller.aclose()
        except Exception:
            pass
    if not _has_content(res):
        return None
    res.setdefault("stats", {}).update(meta)
    return res


async def run_tiered_for_user(user_id: str, transcript: str,
                              *, strict_schema: bool = True,
                              level_override: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Прогнать извлечение по выбору тенанта нативным вызывателем.

    Врезка для CaptureOrchestrator ЗА флагом enable_tiered_extraction:
        if flags.enable_tiered_extraction:
            res = await run_tiered_for_user(user_id, transcript)
            if res: ...   # иначе — существующий 9-агентный путь (не ломаем)

    Три слоя защиты, чтобы НИЧЕГО не падало:
      1) выбранная тенантом модель нативным API;
      2) при сбое/пустом ответе — платформенный Google (Gemini), который уже
         используется в проде;
      3) если и Google не смог — None → штатный 9-агентный путь (тоже Gemini).
    Формат запросов задаётся ТИРОМ модели (frontier=1, lite≈9)."""
    route = await build_extraction_route(user_id, level_override=level_override)
    caller = route.get("caller")

    # Слой 1: выбранная модель
    if caller is not None:
        res = await _run_with(caller, transcript, route["tier"], strict_schema, {
            "model": route["model"], "provider": route["native"],
            "key_source": route["key_source"], "level": route["level"],
        })
        if res is not None:
            return res
        logger.info("extraction: выбранная модель не дала результат → Google-fallback")
    else:
        logger.info("extraction: нет нативного вызывателя (%s) → Google-fallback",
                    route.get("reason"))

    # Слой 2: платформенный Google (если он же и был выбран — не дублируем)
    if route.get("native") != "google":
        gcaller = google_fallback_caller()
        if gcaller is not None:
            res = await _run_with(gcaller, transcript, "lite", strict_schema, {
                "model": "gemini-3.1-flash-lite", "provider": "google",
                "key_source": "platform", "level": route.get("level", "standard"),
                "fallback": "google",
            })
            if res is not None:
                return res

    # Слой 3: сдаёмся → вызывающий остаётся на штатном (Gemini-агенты)
    logger.info("extraction: Google-fallback тоже пуст → штатный 9-агентный путь")
    return None
