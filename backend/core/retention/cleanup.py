"""Scheduled retention cleanup (W27).

Удаляет данные, превысившие per-table retention-окно. Реализован
best-effort: если postgres / redis недоступны — возвращаем пустой
результат, не raises. Caller (nightly_consolidation или admin route)
решает что логировать.

Что чистится:
- ``validation_results`` (W23): диагностические артефакты, default 90 дней.
  Триггер immutable пропускает DELETE — он блокирует только UPDATE на
  immutable колонках.
- executor handles в Redis (W19/W20): SCAN+TTL — дропаем ключи без
  expire (защитный пол). У нормальных handle'ов уже стоит SETEX 7 дней.

Чего НЕ чистим:
- ``audit_events``: append-only по compliance, BEFORE DELETE триггер
  блокирует удаление (см. 040_audit_log.sql). Retention для audit'а —
  в WAL/архив снапшотах, не в primary table.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


_DELETE_VALIDATION_SQL = """
DELETE FROM public.validation_results
WHERE created_at < :cutoff
"""


@dataclass
class CleanupResult:
    """Сводка по одному прогону retention-cycle."""
    started_at: str
    completed_at: Optional[str] = None
    validation_deleted: int = 0
    executor_handles_purged: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "validation_deleted": self.validation_deleted,
            "executor_handles_purged": self.executor_handles_purged,
            "errors": list(self.errors),
        }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cutoff(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))


async def cleanup_validation_results(
    *,
    postgres: Any,
    older_than_days: int,
) -> int:
    """DELETE из validation_results всё что старше cutoff.

    Returns: число удалённых строк (0 при failure / postgres=None).
    """
    if postgres is None or older_than_days <= 0:
        return 0
    cutoff = _cutoff(older_than_days)
    try:
        # Audit-фикс (RLS foundation): retention — cross-tenant-by-design (admin
        # cron, итерирует ВСЕ tenant'ы). Без apply_tenant=False под strict-RLS
        # видели бы 0 строк (cron не выставляет app.tenant_id), и cleanup тихо
        # переставал бы работать. Явно opt-out из RLS, как другие admin-jobs.
        async with postgres.session(apply_tenant=False) as session:
            from sqlalchemy import text as _sql_text
            result = await session.execute(
                _sql_text(_DELETE_VALIDATION_SQL),
                {"cutoff": cutoff},
            )
        deleted = _rowcount(result)
        if deleted:
            logger.info(
                "retention.validation_results: deleted %d rows older than %s",
                deleted, cutoff.isoformat(),
            )
            try:
                from backend.core.observability import metrics as _m
                _m.record_retention_cleanup("validation_results", deleted)
            except Exception:
                pass
        return deleted
    except Exception as exc:
        logger.warning("retention.validation_results failed: %s", exc)
        return 0


async def cleanup_executor_handles(
    *,
    redis: Any,
    older_than_days: int,
    scan_count: int = 500,
) -> int:
    """Защитный sweep по executor:* ключам без TTL.

    Здоровые handle'ы выставляют SETEX, поэтому Redis сам их экспайрит.
    Этот sweep ловит keys, которые по какой-то причине остались без
    expire (старый код, ручной SET, миграция). Удаляем те, что не имеют
    TTL вообще.

    older_than_days сейчас не используется (Redis не хранит created_at
    рядом с ключом), но оставляем сигнатуру под будущее, если перейдём
    на key с timestamp в payload.

    Returns: число удалённых ключей.
    """
    if redis is None:
        return 0
    purged = 0
    try:
        # Поддерживаем как async-redis (scan_iter), так и наш _FakeRedis
        # из тестов (предоставляет async iter scan_iter).
        cursor = 0
        match = "executor:*"
        if hasattr(redis, "scan_iter"):
            iterator = redis.scan_iter(match=match, count=scan_count)
            async for key in _aiter(iterator):
                if await _missing_ttl(redis, key):
                    try:
                        await redis.delete(key)
                        purged += 1
                    except Exception:
                        continue
        else:
            # Низкоуровневый SCAN.
            while True:
                cursor, keys = await redis.scan(
                    cursor=cursor, match=match, count=scan_count,
                )
                for key in keys:
                    if await _missing_ttl(redis, key):
                        try:
                            await redis.delete(key)
                            purged += 1
                        except Exception:
                            continue
                if not cursor:
                    break
        if purged:
            logger.info(
                "retention.executor_handles: purged %d ttl-less keys", purged,
            )
            try:
                from backend.core.observability import metrics as _m
                _m.record_retention_cleanup("executor_handles", purged)
            except Exception:
                pass
        return purged
    except Exception as exc:
        logger.warning("retention.executor_handles failed: %s", exc)
        return purged


async def _missing_ttl(redis: Any, key: Any) -> bool:
    """True если ключ существует, но не имеет expire (TTL == -1)."""
    try:
        ttl = await redis.ttl(key)
    except Exception:
        return False
    # redis-py: -1 = no expire, -2 = key missing.
    return ttl == -1


async def _aiter(iterable: Any):
    """Адаптер: scan_iter может быть async-generator или async-iterable."""
    if hasattr(iterable, "__aiter__"):
        async for item in iterable:
            yield item
    elif hasattr(iterable, "__iter__"):
        for item in iterable:
            yield item


def _rowcount(result: Any) -> int:
    rc = getattr(result, "rowcount", None)
    if isinstance(rc, int) and rc >= 0:
        return rc
    return 0


async def run_retention_cycle(
    *,
    postgres: Any = None,
    redis: Any = None,
    validation_retention_days: int = 90,
    executor_retention_days: int = 30,
    enabled: bool = True,
) -> CleanupResult:
    """Выполнить полный cycle: validation_results + executor_handles.

    Best-effort: ошибки агрегируются в errors, но cycle продолжается.
    Если enabled=False — возвращаем пустой результат сразу.
    """
    result = CleanupResult(started_at=_utc_now_iso())
    if not enabled:
        result.completed_at = _utc_now_iso()
        return result

    try:
        result.validation_deleted = await cleanup_validation_results(
            postgres=postgres,
            older_than_days=validation_retention_days,
        )
    except Exception as exc:
        result.errors.append(f"validation: {exc}")

    try:
        result.executor_handles_purged = await cleanup_executor_handles(
            redis=redis,
            older_than_days=executor_retention_days,
        )
    except Exception as exc:
        result.errors.append(f"executor: {exc}")

    result.completed_at = _utc_now_iso()
    return result
