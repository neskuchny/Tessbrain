# -*- coding: utf-8 -*-
"""Пользовательские модели для уровней Стандарт/Премиум (per-tenant override).

«Стандарт = моя модель X со своим ключом, Премиум = моя модель Y; сброс —
возвращаются модели платформы». Хранение: data/llm_tiers/{uid}.json (0600).
Провайдеры — все, что умеет ProfileBackedClient (openai/deepseek/qwen/xai/
anthropic/gemini/glm через openai-совместимый endpoint/…). Пусто → поведение
платформы байт-в-байт прежнее. fast-уровень сознательно не переопределяется.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

LEVELS = ("standard", "premium")
# GLM — OpenAI-совместимый endpoint Zhipu; добавляем дефолт, остальные берёт
# ProfileBackedClient._DEFAULT_BASE_URLS.
_EXTRA_BASE_URLS = {"glm": "https://open.bigmodel.cn/api/paas/v4"}


def _path(user_id: str) -> str:
    safe = "".join(c for c in str(user_id or "anon")
                   if c.isalnum() or c == "-")[:40] or "anon"
    d = os.path.join("data", "llm_tiers")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{safe}.json")


def get_overrides(user_id: str) -> Dict[str, Any]:
    """{standard?: {provider, model, api_key?, base_url?}, premium?: {...}}."""
    try:
        p = _path(user_id)
        if not os.path.exists(p):
            return {}
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items()
                if k in LEVELS and isinstance(v, dict) and v.get("provider")}
    except Exception:
        logger.debug("tier overrides read failed", exc_info=True)
        return {}


def set_override(user_id: str, level: str,
                 cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Задать/очистить уровень. cfg=None → сброс уровня на платформу."""
    if level not in LEVELS:
        return {"status": "error", "message": f"level must be one of {LEVELS}"}
    data = get_overrides(user_id)
    if not cfg or not cfg.get("provider"):
        data.pop(level, None)
    else:
        data[level] = {
            "provider": str(cfg.get("provider")).strip().lower(),
            "model": str(cfg.get("model") or "").strip(),
            "api_key": str(cfg.get("api_key") or "").strip(),
            "base_url": str(cfg.get("base_url") or "").strip(),
        }
    p = _path(user_id)
    if not data:
        try:
            os.path.exists(p) and os.remove(p)
        except Exception:
            logger.debug("tier overrides clear failed", exc_info=True)
    else:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        try:
            os.chmod(p, 0o600)  # в файле ключ пользователя
        except Exception:
            pass
    return {"status": "success", "levels": public_view(user_id)}


def reset_all(user_id: str) -> Dict[str, Any]:
    """Полный сброс к моделям и ключам платформы."""
    try:
        p = _path(user_id)
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        logger.debug("tier overrides reset failed", exc_info=True)
    return {"status": "success", "levels": {}}


def public_view(user_id: str) -> Dict[str, Any]:
    """Для UI: без ключей (только маска наличия)."""
    out: Dict[str, Any] = {}
    for lvl, cfg in get_overrides(user_id).items():
        out[lvl] = {"provider": cfg.get("provider"),
                    "model": cfg.get("model"),
                    "base_url": cfg.get("base_url") or "",
                    "has_key": bool(cfg.get("api_key"))}
    return out


def caller_for_level(user_id: str, level: str):
    """ProfileBackedClient по override уровня или None (нет/не собрался).

    Возвращает (client, provider). Ключ: свой из настройки → провайдер
    из «Интеграций» не дергаем (это явная настройка уровня)."""
    cfg = get_overrides(user_id).get(level)
    if not cfg:
        return None, None
    # Подписка через CLI-агента (claude/codex/gemini/qwen): фикс-цена за
    # тяжёлые вызовы. Синхронизация ей тоже по силам — tiered extraction на
    # сильной модели идёт ОДНИМ пакетным вызовом вместо 30 (существующая
    # разбивка моделей по числу вызовов сохраняется — override меняет только
    # ИСПОЛНИТЕЛЯ вызова, не форму конвейера).
    if cfg.get("provider") == "subscription_cli":
        if level != "premium":
            # Подписка — только Премиум (тяжёлые/пакетные вызовы). Стандарт —
            # частые мелкие интерактивные вызовы, CLI там убьёт латентность
            # и лимит подписки.
            logger.info("subscription_cli допустим только на premium-уровне")
            return None, None
        try:
            from backend.core.llm.cli_caller import SubscriptionCliCaller
            c = SubscriptionCliCaller(cfg.get("model") or "claude")
            return (c, "subscription_cli") if c.enabled else (None, None)
        except Exception:
            logger.warning("subscription cli caller failed", exc_info=True)
            return None, None
    try:
        from backend.core.llm.profile_client import ProfileBackedClient
        provider = cfg["provider"]
        base_url = cfg.get("base_url") or _EXTRA_BASE_URLS.get(provider)
        eff_provider = "openai" if provider == "glm" else provider
        client = ProfileBackedClient(
            provider=eff_provider, base_url=base_url,
            model=cfg.get("model") or "", api_key=cfg.get("api_key") or "")
        if not getattr(client, "enabled", False):
            logger.info("tier override %s/%s: клиент не собрался (нет ключа?)",
                        level, provider)
            return None, None
        return client, provider
    except Exception:
        logger.warning("tier override caller failed", exc_info=True)
        return None, None


__all__ = ["LEVELS", "get_overrides", "set_override", "reset_all",
           "public_view", "caller_for_level"]
