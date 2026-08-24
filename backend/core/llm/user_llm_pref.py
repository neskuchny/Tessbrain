# -*- coding: utf-8 -*-
"""
User LLM Preference — ПЕРСИСТЕНТНЫЙ per-user выбор LLM-профиля.

Закрывает остаток аудита: default-профиль был tenant-wide (один на всех,
admin), а per-request `llm_profile_id` работал только если клиент сам его
передавал в каждом запросе. Теперь пользователь один раз выбирает «мой
профиль» — и чат подхватывает его автоматически на каждый запрос.

Хранение — per-user JSON через _DATA_ROOT (паттерн остальных примитивов:
file_lock + atomic write, без миграций БД). Цепочка выбора в роутере не
меняется: contextvar(llm_profile_id) → tenant default → env-клиенты; мы
лишь заполняем contextvar сохранённым предпочтением, когда запрос его не
передал явно. Невалидный/удалённый профиль безопасен: resolve_by_id вернёт
None → warning + fallback на default (existing behaviour).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def _pref_path(user_id: str) -> str:
    from backend.core.store.tenant_paths import _DATA_ROOT, _require_uuid
    user_id = _require_uuid(user_id, "user_id")
    d = _DATA_ROOT / "user_prefs"
    d.mkdir(parents=True, exist_ok=True)
    return str(d / f"{user_id}.json")


def _load(path: str) -> dict:
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        logger.debug("user_llm_pref load failed", exc_info=True)
    return {}


def get_user_llm_profile(user_id: str) -> Optional[str]:
    """Сохранённый llm_profile_id пользователя или None. Never-raise."""
    try:
        return _load(_pref_path(user_id)).get("llm_profile_id") or None
    except Exception:
        return None


def set_user_llm_profile(user_id: str, profile_id: Optional[str]) -> bool:
    """Сохранить (или сбросить при None/'') выбор профиля. Never-raise."""
    try:
        from backend.core.store.tenant_io import atomic_write_json, file_lock
        path = _pref_path(user_id)
        with file_lock(path):
            prefs = _load(path)
            if profile_id:
                prefs["llm_profile_id"] = str(profile_id)
            else:
                prefs.pop("llm_profile_id", None)
            atomic_write_json(path, prefs)
        return True
    except Exception as e:
        logger.warning(f"set_user_llm_profile failed: {e}")
        return False


# ── BYOK: «использовать МОЙ ключ из Интеграций для ИИ Tessbrain» ────────────
# Opt-in (не сюрприз): включив тумблер, пользователь переводит думающий слой
# (чат/ТЗ/анализ) на свой ключ Anthropic/OpenAI из раздела «Интеграции» —
# биллинг его, а не сервера. provider: "anthropic" | "openai" | None (авто:
# что найдётся первым). Хранится в том же per-user pref JSON.

def get_use_my_key(user_id: str) -> tuple[bool, Optional[str]]:
    """(включён ли «мой ключ», выбранный провайдер|None=авто). Never-raise."""
    try:
        prefs = _load(_pref_path(user_id))
        return bool(prefs.get("use_my_key")), (prefs.get("my_key_provider") or None)
    except Exception:
        return False, None


def set_use_my_key(user_id: str, enabled: bool,
                   provider: Optional[str] = None) -> bool:
    """Включить/выключить «мой ключ» (+ опц. провайдер). Never-raise."""
    if provider not in (None, "", "anthropic", "openai"):
        return False
    try:
        from backend.core.store.tenant_io import atomic_write_json, file_lock
        path = _pref_path(user_id)
        with file_lock(path):
            prefs = _load(path)
            if enabled:
                prefs["use_my_key"] = True
                if provider:
                    prefs["my_key_provider"] = provider
                else:
                    prefs.pop("my_key_provider", None)
            else:
                prefs.pop("use_my_key", None)
                prefs.pop("my_key_provider", None)
            atomic_write_json(path, prefs)
        return True
    except Exception as e:
        logger.warning(f"set_use_my_key failed: {e}")
        return False


# ── Маршрут ИЗВЛЕЧЕНИЯ встречи: провайдер + уровень (standard|premium) ──────
# Отдельно от «профиля/моего ключа» для чата: тут выбор МОДЕЛИ обработки встреч
# (backend.core.llm.model_router). Без выбора — платформенные дефолты. При
# use_own_key + ключе провайдера в Интеграциях — своя модель нативным API.
# Хранится в том же per-user pref JSON.

def get_extraction_route(user_id: str) -> dict:
    """{'provider','level','use_own_key'} с дефолтами. Never-raise.

    level по умолчанию 'premium': анализ встречи — тяжёлая нагрузка, крупная
    модель задаёт качество всей базы (workload_policy). Тенант может понизить."""
    try:
        p = _load(_pref_path(user_id))
        return {
            "provider": p.get("extraction_provider") or "platform",
            "level": p.get("extraction_level") or "premium",
            "use_own_key": bool(p.get("extraction_use_own_key")),
        }
    except Exception:
        return {"provider": "platform", "level": "premium", "use_own_key": False}


def set_extraction_route(user_id: str, *, provider: Optional[str] = None,
                         level: Optional[str] = None,
                         use_own_key: Optional[bool] = None) -> bool:
    """Сохранить выбор маршрута извлечения (частично). Never-raise."""
    if level is not None and level not in ("fast", "standard", "premium"):
        return False
    try:
        from backend.core.store.tenant_io import atomic_write_json, file_lock
        path = _pref_path(user_id)
        with file_lock(path):
            prefs = _load(path)
            if provider is not None:
                if provider:
                    prefs["extraction_provider"] = str(provider)
                else:
                    prefs.pop("extraction_provider", None)
            if level is not None:
                prefs["extraction_level"] = level
            if use_own_key is not None:
                if use_own_key:
                    prefs["extraction_use_own_key"] = True
                else:
                    prefs.pop("extraction_use_own_key", None)
            atomic_write_json(path, prefs)
        return True
    except Exception as e:
        logger.warning(f"set_extraction_route failed: {e}")
        return False
