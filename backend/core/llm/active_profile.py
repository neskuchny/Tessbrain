"""W42b: lazy resolver активного LLM profile из БД для LLMRouter.

Поток:
1. LLMRouter при первом `generate()` зовёт `get_active_client()` через resolver
2. Resolver lazy-загружает active profile из LLMProfileService и строит
   ProfileBackedClient
3. Client кэшируется по `profile.id`; пока profile не изменился — переиспользуем
4. Когда admin вызывает `POST /admin/llm-profiles/{id}/set-default`,
   route зовёт `invalidate_active_profile()` — следующий generate() перезагрузит

Best-effort:
- Postgres недоступен → resolver возвращает None → LLMRouter использует
  env-based clients (как W42 baseline)
- profile найден, но invalid (нет api_key где нужен) → возвращает None
- profile.provider не поддерживается → возвращает None
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from backend.core.llm.profile_client import ProfileBackedClient

logger = logging.getLogger(__name__)


class ActiveProfileResolver:
    """Resolver с кешем — один на процесс.

    Phase 1 multi-provider (per-user override): дополнительно кэшируем
    клиенты по profile_id — `resolve_by_id(profile_id)` строит клиент для
    конкретного профиля независимо от is_default. Используется когда юзер
    в chat-request передал `llm_profile_id`.
    """

    def __init__(self) -> None:
        self._cached_profile_id: Optional[str] = None
        self._cached_client: Optional[ProfileBackedClient] = None
        # Кэш per-profile_id для override-режима. Размер ограничен
        # количеством профилей у tenant (обычно ≤10) — простой dict без LRU.
        self._by_id_cache: dict[str, ProfileBackedClient] = {}
        self._lock = asyncio.Lock()

    async def get_active_client(self) -> Optional[ProfileBackedClient]:
        """Returns cached or freshly-built client. None если не сконфигурирован."""
        async with self._lock:
            if self._cached_client is not None:
                return self._cached_client

            profile, api_key = await self._load_active_profile_and_key()
            if profile is None:
                logger.debug("ActiveProfileResolver: no active profile available")
                return None
            try:
                client = ProfileBackedClient(
                    provider=profile.provider,
                    base_url=profile.base_url,
                    model=profile.model,
                    api_key=api_key,
                    config=profile.config or {},
                )
            except Exception as exc:
                logger.warning(
                    "ActiveProfileResolver: failed to build client for %s: %s",
                    profile.id, exc,
                )
                return None
            if not client.enabled:
                logger.info(
                    "ActiveProfileResolver: profile %s built but not enabled (config gap)",
                    profile.id,
                )
                return None
            self._cached_profile_id = profile.id
            self._cached_client = client
            logger.info(
                "ActiveProfileResolver: loaded active profile %s (provider=%s, model=%s)",
                profile.id, profile.provider, profile.model,
            )
            return client

    async def _load_active_profile_and_key(self) -> tuple[Optional[Any], Optional[str]]:
        try:
            from backend.core.llm.profiles import LLMProfileService
        except ImportError:
            return None, None
        try:
            from backend.db.postgres import get_postgres
            postgres = await get_postgres()
        except Exception:
            postgres = None
        svc = LLMProfileService(postgres=postgres)
        try:
            profile = await svc.get_default()
        except Exception as exc:
            logger.debug("get_default failed: %s", exc)
            return None, None
        if profile is None:
            return None, None
        api_key: Optional[str] = None
        if profile.has_api_key:
            try:
                api_key = await svc.get_decrypted_api_key(profile_id=profile.id)
            except Exception as exc:
                logger.debug("get_decrypted_api_key failed: %s", exc)
                api_key = None
        return profile, api_key

    async def resolve_by_id(self, profile_id: str) -> Optional[ProfileBackedClient]:
        """Phase 1 multi-provider: построить клиент для конкретного profile_id.

        Используется когда юзер передал `llm_profile_id` в chat-request —
        даёт per-user override глобального default'а.

        Возвращает None если профиль не найден / disabled / api_key missing.
        """
        if not profile_id:
            return None
        async with self._lock:
            cached = self._by_id_cache.get(profile_id)
            if cached is not None:
                return cached
            try:
                from backend.core.llm.profiles import LLMProfileService
            except ImportError:
                return None
            try:
                from backend.db.postgres import get_postgres
                postgres = await get_postgres()
            except Exception:
                postgres = None
            svc = LLMProfileService(postgres=postgres)
            try:
                profile = await svc.get_by_id(profile_id)
            except Exception as exc:
                logger.debug("get_by_id(%s) failed: %s", profile_id, exc)
                return None
            if profile is None:
                return None
            api_key: Optional[str] = None
            if profile.has_api_key:
                try:
                    api_key = await svc.get_decrypted_api_key(profile_id=profile.id)
                except Exception as exc:
                    logger.debug("get_decrypted_api_key(%s) failed: %s", profile_id, exc)
                    api_key = None
            try:
                client = ProfileBackedClient(
                    provider=profile.provider,
                    base_url=profile.base_url,
                    model=profile.model,
                    api_key=api_key,
                    config=profile.config or {},
                )
            except Exception as exc:
                logger.warning(
                    "ActiveProfileResolver.resolve_by_id: build failed for %s: %s",
                    profile_id, exc,
                )
                return None
            if not client.enabled:
                logger.info(
                    "ActiveProfileResolver.resolve_by_id: profile %s built but disabled",
                    profile_id,
                )
                return None
            self._by_id_cache[profile_id] = client
            logger.info(
                "ActiveProfileResolver: resolved by_id %s (provider=%s, model=%s)",
                profile_id, profile.provider, profile.model,
            )
            return client

    async def invalidate(self) -> None:
        """Сбросить cache — следующий get_active_client() перечитает БД.

        Также чистит per-profile_id cache: при изменении/удалении любого
        профиля admin route должен дёрнуть invalidate, чтобы юзеры с
        per-request override получили свежие данные.
        """
        async with self._lock:
            self._cached_profile_id = None
            self._cached_client = None
            self._by_id_cache.clear()
        logger.info("ActiveProfileResolver: cache invalidated")

    async def current_profile_id(self) -> Optional[str]:
        return self._cached_profile_id


_global_resolver: Optional[ActiveProfileResolver] = None


def get_active_profile_resolver() -> ActiveProfileResolver:
    global _global_resolver
    if _global_resolver is None:
        _global_resolver = ActiveProfileResolver()
    return _global_resolver


async def invalidate_active_profile() -> None:
    """Public hook — admin route зовёт после set_default / delete."""
    await get_active_profile_resolver().invalidate()


def reset_resolver() -> None:
    """Тестовый helper — сбрасывает global instance."""
    global _global_resolver
    _global_resolver = None


__all__ = [
    "ActiveProfileResolver",
    "get_active_profile_resolver",
    "invalidate_active_profile",
    "reset_resolver",
]
