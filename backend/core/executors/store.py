"""Handle store — где хранятся TaskHandle между запросами (W19).

API:
- save(handle) / get(id) / delete(id) / update_status(id, status)
- save_result(handle_id, result) / get_result(handle_id)

Дизайн:
- Если есть Redis (через core.observability.dist_lock или backend.config) —
  используем его (TTL 7 дней).
- Иначе — in-memory dict (для тестов и dev).
- Никаких raises на read/write — best-effort, ошибки logged.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from backend.core.executors.base import TaskHandle, TaskResult, TaskStatus

logger = logging.getLogger(__name__)


_KEY_PREFIX = "executor:handle:"
_RESULT_PREFIX = "executor:result:"
_TTL_SECONDS = 7 * 24 * 3600   # 7 дней


class _MemoryStore:
    """Простейший in-memory KV для тестов / dev."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def get(self, key: str) -> Optional[str]:
        return self._data.get(key)

    async def set(self, key: str, value: str, *, ex: Optional[int] = None) -> None:
        self._data[key] = value

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)


_memory_store = _MemoryStore()


async def _get_redis() -> Any:
    """Best-effort Redis client. None если недоступен."""
    try:
        import redis.asyncio as aioredis

        from backend.config import get_settings
        url = getattr(get_settings(), "redis_url", None) or "redis://localhost:6379/0"
        client = aioredis.from_url(url, decode_responses=True)
        # ping чтобы убедиться что доступен (короткий таймаут)
        await client.ping()
        return client
    except Exception as exc:
        logger.debug("executor.store: Redis unavailable, using memory store: %s", exc)
        return None


async def _store():
    """Вернуть лучший доступный backend."""
    redis = await _get_redis()
    return redis if redis is not None else _memory_store


# === Handle CRUD =========================================================

async def save_handle(handle: TaskHandle) -> None:
    store = await _store()
    try:
        await store.set(
            _KEY_PREFIX + handle.id,
            json.dumps(handle.to_dict(), ensure_ascii=False),
            ex=_TTL_SECONDS,
        )
    except Exception as exc:
        logger.warning("executor.store.save_handle failed: %s", exc)


async def get_handle(handle_id: str) -> Optional[TaskHandle]:
    if not handle_id:
        return None
    store = await _store()
    try:
        raw = await store.get(_KEY_PREFIX + handle_id)
    except Exception as exc:
        logger.warning("executor.store.get_handle failed: %s", exc)
        return None
    if not raw:
        return None
    try:
        return TaskHandle.from_dict(json.loads(raw))
    except Exception as exc:
        logger.warning("executor.store: handle parse failed: %s", exc)
        return None


async def update_status(handle_id: str, status: TaskStatus) -> bool:
    """Обновить статус handle. Возвращает True если запись существует."""
    handle = await get_handle(handle_id)
    if handle is None:
        return False
    handle.status = status
    await save_handle(handle)
    return True


async def delete_handle(handle_id: str) -> None:
    store = await _store()
    try:
        await store.delete(_KEY_PREFIX + handle_id)
        await store.delete(_RESULT_PREFIX + handle_id)
    except Exception as exc:
        logger.warning("executor.store.delete_handle failed: %s", exc)


# === Result CRUD =========================================================

async def save_result(result: TaskResult) -> None:
    store = await _store()
    try:
        await store.set(
            _RESULT_PREFIX + result.handle_id,
            json.dumps(result.to_dict(), ensure_ascii=False),
            ex=_TTL_SECONDS,
        )
    except Exception as exc:
        logger.warning("executor.store.save_result failed: %s", exc)


async def get_result(handle_id: str) -> Optional[dict[str, Any]]:
    """Вернуть raw dict (как сохранили). None если нет."""
    if not handle_id:
        return None
    store = await _store()
    try:
        raw = await store.get(_RESULT_PREFIX + handle_id)
    except Exception as exc:
        logger.warning("executor.store.get_result failed: %s", exc)
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception as exc:
        logger.warning("executor.store: result parse failed: %s", exc)
        return None


def reset_memory_store() -> None:
    """Очистить in-memory store (используется в тестах)."""
    _memory_store._data.clear()


# === Листинг + reaper ====================================================

async def list_handles() -> list[TaskHandle]:
    """Все TaskHandle из стора (для reaper/листинга). Best-effort, no-raise."""
    store = await _store()
    out: list[TaskHandle] = []
    try:
        if store is _memory_store:
            for k, raw in list(store._data.items()):
                if k.startswith(_KEY_PREFIX) and raw:
                    try:
                        out.append(TaskHandle.from_dict(json.loads(raw)))
                    except Exception:
                        continue
        else:
            # aioredis (decode_responses=True): scan_iter отдаёт ключи-строки.
            async for key in store.scan_iter(match=_KEY_PREFIX + "*", count=200):
                raw = await store.get(key)
                if raw:
                    try:
                        out.append(TaskHandle.from_dict(json.loads(raw)))
                    except Exception:
                        continue
    except Exception as exc:
        logger.warning("executor.store.list_handles failed: %s", exc)
    return out


async def reap_stale_running(max_age_minutes: int = 90) -> int:
    """Пометить зависшие RUNNING (старше max_age_minutes) как FAILED.

    После рестарта процесса fire-and-forget-раннера (claude_code:
    asyncio.create_task) больше нет — задача осталась бы RUNNING навсегда.
    Reaper по submitted_at (единственная метка времени на handle; QUEUED→
    RUNNING почти мгновенно после submit) закрывает такие «зомби».
    Возвращает число закрытых. Никогда не raises (best-effort).

    ВАЖНО: max_age_minutes должен превышать самый большой timeout задачи
    (TaskSubmission.timeout_seconds, default 3600s=60m) — иначе можно закрыть
    задачу, которую живой раннер ещё легитимно выполняет.
    """
    from datetime import datetime, timezone
    cutoff = datetime.now(timezone.utc).timestamp() - max_age_minutes * 60
    reaped = 0
    for h in await list_handles():
        if h.status != TaskStatus.RUNNING:
            continue
        try:
            dt = datetime.fromisoformat(h.submitted_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ts = dt.timestamp()
        except Exception:
            continue
        if ts >= cutoff:
            continue
        # Liveness: если backend_ref — pid живого процесса (CLI-бэкенд на этом
        # хосте), задача РЕАЛЬНО выполняется дольше порога — не трогаем. Reap
        # только если процесс мёртв, или ref не pid (openhands: conversation_id
        # — проверить нельзя, полагаемся на возраст).
        ref = h.backend_ref
        if ref:
            try:
                pid = int(ref)
            except (TypeError, ValueError):
                pid = None
            if pid is not None:
                import os
                try:
                    os.kill(pid, 0)
                    continue          # процесс жив → не зомби, пропускаем
                except ProcessLookupError:
                    pass              # мёртв → закрываем
                except PermissionError:
                    continue          # существует, но не наш → жив, пропускаем
                except Exception:
                    pass              # не смогли проверить → закрываем по возрасту
        try:
            h.status = TaskStatus.FAILED
            await save_handle(h)
            await save_result(TaskResult(
                handle_id=h.id, status=TaskStatus.FAILED, success=False,
                summary="reaped: stale RUNNING (нет живого раннера после рестарта)",
                error_message=f"stale RUNNING > {max_age_minutes}m"))
            reaped += 1
        except Exception as exc:
            logger.warning("reap_stale_running: mark failed for %s: %s", h.id, exc)
    return reaped


__all__ = [
    "delete_handle",
    "get_handle",
    "get_result",
    "list_handles",
    "reap_stale_running",
    "reset_memory_store",
    "save_handle",
    "save_result",
    "update_status",
]
