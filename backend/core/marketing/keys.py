# -*- coding: utf-8 -*-
"""Ключи для исследований Марка: сначала личный ключ пользователя из
вкладки «Интеграции» (user_integrations, шифрованно), затем фолбэк на
серверный env. Так владелец добавляет токены сам, без доступа к серверу."""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


async def get_key(user_id: Optional[str], provider: str, key_name: str,
                  env_var: str) -> str:
    """Личный ключ из Интеграций → env. Пусто — значит не настроено."""
    if user_id:
        try:
            from backend.api.routes.integrations import (
                get_user_integration_keys)
            keys = await get_user_integration_keys(user_id, provider)
            val = (keys.get(key_name) or "").strip()
            if val:
                return val
        except Exception as e:
            logger.debug(f"user key {provider}/{key_name} skipped: {e}")
    return os.getenv(env_var, "").strip()


async def yandex_direct_token(user_id: Optional[str] = None) -> str:
    return await get_key(user_id, "yandex_direct", "oauth_token",
                         "YANDEX_DIRECT_TOKEN")


async def vk_service_key(user_id: Optional[str] = None) -> str:
    return await get_key(user_id, "vk", "service_key", "VK_SERVICE_KEY")
