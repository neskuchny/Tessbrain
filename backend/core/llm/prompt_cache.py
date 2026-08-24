"""Application-level exact-match prompt cache for LLM responses.

Когда `temperature == 0`, ответ детерминирован — один и тот же
(prompt, system_prompt, model, temperature) даёт одинаковый результат.
В таких случаях имеет смысл закешировать ответ в Redis на короткий TTL,
сэкономив 100% токенов на повторных запросах.

Гарантии качества:
- Только temperature == 0 → детерминированный путь, кэш не «портит» результаты.
- TTL по умолчанию = 1 час: достаточно, чтобы поймать повторы в рамках сессии,
  но не настолько долго, чтобы вернуть устаревший контекст после обновления
  данных.
- Хеш ключа включает модель и system_prompt → переключение модели или
  системного промпта инвалидирует кэш автоматически.
- Fail-open: при недоступности Redis кэш просто игнорируется.

Не используется (намеренно):
- Кэш для temperature > 0 — там пользователь явно просит вариативность.
- Кэш для запросов с user_id-специфичным контекстом, передающимся внутри
  prompt-а — он автоматически попадёт в хеш и будет уникален.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_CACHE_NS = "llm:prompt_cache:v1"
_DEFAULT_TTL_SECONDS = 3600


def _key(model: str, prompt: str, system_prompt: Optional[str], temperature: float) -> str:
    raw = json.dumps(
        {"m": model, "p": prompt, "s": system_prompt or "", "t": round(temperature, 4)},
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    h = hashlib.sha256(raw).hexdigest()
    return f"{_CACHE_NS}:{h}"


_stats = {"lookups": 0, "hits": 0, "stores": 0, "errors": 0}


async def _client():
    try:
        from backend.db.redis_client import get_redis
        redis = await get_redis()
        if not await redis.health_check():
            return None
        return redis.client
    except Exception as exc:
        logger.debug("prompt_cache: Redis unavailable: %s", exc)
        return None


async def get_cached_response(
    *,
    model: str,
    prompt: str,
    system_prompt: Optional[str],
    temperature: float,
) -> Optional[str]:
    """Вернуть закешированный ответ, если есть. Только для temperature == 0."""
    if temperature != 0:
        return None
    _stats["lookups"] += 1
    client = await _client()
    if not client:
        return None
    try:
        raw = await client.get(_key(model, prompt, system_prompt, temperature))
    except Exception as exc:
        _stats["errors"] += 1
        logger.debug("prompt_cache: get error: %s", exc)
        return None
    if raw is None:
        return None
    _stats["hits"] += 1
    if isinstance(raw, bytes):
        return raw.decode("utf-8")
    return raw


async def store_response(
    *,
    model: str,
    prompt: str,
    system_prompt: Optional[str],
    temperature: float,
    response: str,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> None:
    """Сохранить ответ в кэш. Только для temperature == 0."""
    if temperature != 0:
        return
    client = await _client()
    if not client:
        return
    try:
        await client.set(
            _key(model, prompt, system_prompt, temperature),
            response,
            ex=ttl_seconds,
        )
        _stats["stores"] += 1
    except Exception as exc:
        _stats["errors"] += 1
        logger.debug("prompt_cache: store error: %s", exc)


def get_cache_stats() -> dict[str, Any]:
    """Вернуть статистику кэша (для /api/v1/usage/quota и подобных эндпоинтов)."""
    lookups = _stats["lookups"] or 1
    return {
        **_stats,
        "hit_rate": round(_stats["hits"] / lookups, 4),
    }
