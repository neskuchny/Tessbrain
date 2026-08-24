# -*- coding: utf-8 -*-
"""BYOK для думающего слоя: LLM-вызовы Tessbrain (чат/ТЗ/анализ/SIMA) на
ЛИЧНОМ ключе пользователя из раздела «Интеграции».

Как работает: пользователь включает тумблер «использовать мой ключ»
(user_llm_pref.set_use_my_key) → роутер при выборе клиента, если запрос не
задал явный llm_profile_id, строит эфемерный ProfileBackedClient на его ключе
Anthropic/OpenAI из user_integrations. Приоритет цепочки:
  явный llm_profile_id → ЛИЧНЫЙ ключ (opt-in) → tenant default → env-клиенты.

Opt-in сознательно: автоматическое переключение думающего слоя на личный
биллинг было бы сюрпризом (в отличие от сборки, где «свой аккаунт» — сама
суть запроса). Учёт остаётся честным: usage_tracker и так пишет по user_id
из llm-контекста.

Кэш клиентов — по (user, provider, отпечаток ключа): смена ключа в
Интеграциях создаёт новый клиент, чужие ключи не пересекаются.
"""
from __future__ import annotations

import hashlib
import os
from typing import Any, Optional

import structlog

logger = structlog.get_logger(__name__)

# Модели по умолчанию для личного клиента (env-переопределяемо).
# openai — тот же дефолт, что TIER_MODELS роутера; anthropic в роутере
# дефолта не имеет, задаём свой (можно сменить BYOK_ANTHROPIC_MODEL).
_DEFAULT_MODELS = {
    "anthropic": ("BYOK_ANTHROPIC_MODEL", "claude-sonnet-4-5"),
    "openai": ("BYOK_OPENAI_MODEL", "gpt-4o-mini"),
}

_CACHE: dict[tuple, Any] = {}
_CACHE_CAP = 200


def _model_for(provider: str) -> str:
    env_var, default = _DEFAULT_MODELS[provider]
    return os.getenv(env_var, "").strip() or default


def _fingerprint(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


async def _integration_key(user_id: str, provider: str) -> str:
    """Расшифрованный api_key провайдера из Интеграций. Never-raise → ''."""
    try:
        from backend.api.routes.integrations import get_user_integration_keys
        keys = await get_user_integration_keys(user_id, provider) or {}
        return str(keys.get("api_key") or "").strip()
    except Exception:
        logger.debug("personal_client: integration key lookup failed", exc_info=True)
        return ""


async def get_personal_client(user_id: str) -> Optional[Any]:
    """Личный LLM-клиент пользователя (или None).

    None, если: тумблер выключен / ключа в Интеграциях нет / клиент не
    инициализировался. Никогда не поднимает исключение — цепочка роутера
    просто идёт дальше (tenant default)."""
    if not user_id:
        return None
    try:
        from backend.core.llm.user_llm_pref import get_use_my_key
        enabled, provider_pref = get_use_my_key(user_id)
        if not enabled:
            return None

        providers = [provider_pref] if provider_pref else ["anthropic", "openai"]
        for provider in providers:
            if provider not in _DEFAULT_MODELS:
                continue
            key = await _integration_key(user_id, provider)
            if not key:
                continue
            cache_key = (user_id, provider, _fingerprint(key))
            client = _CACHE.get(cache_key)
            if client is not None:
                return client
            from backend.core.llm.profile_client import ProfileBackedClient
            client = ProfileBackedClient(
                provider=provider, base_url=None,
                model=_model_for(provider), api_key=key)
            if not getattr(client, "enabled", False):
                logger.debug("personal_client: client for %s not enabled", provider)
                continue
            if len(_CACHE) >= _CACHE_CAP:
                _CACHE.clear()  # редкий сброс дешевле LRU-бухгалтерии
            _CACHE[cache_key] = client
            logger.info("personal_client: using user's own %s key (model=%s)",
                        provider, _model_for(provider))
            return client
        return None
    except Exception:
        logger.debug("personal_client resolve failed", exc_info=True)
        return None


async def personal_key_status(user_id: str) -> dict:
    """Для UI: включён ли тумблер и какой ключ реально найдётся (маска)."""
    from backend.core.llm.user_llm_pref import get_use_my_key
    enabled, provider_pref = get_use_my_key(user_id)
    found: dict[str, str] = {}
    for provider in ("anthropic", "openai"):
        key = await _integration_key(user_id, provider)
        if key:
            k = key
            found[provider] = (("•" * len(k)) if len(k) <= 8
                               else f"{k[:4]}…{k[-4:]}")
    return {"enabled": enabled, "provider": provider_pref,
            "available_keys": found,
            "effective_model": {p: _model_for(p) for p in found},
            }
