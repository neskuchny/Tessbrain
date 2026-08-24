"""Unit-тесты для core.retention.cleanup (W27)."""
from __future__ import annotations

import asyncio

import pytest
from backend.core.retention.cleanup import (
    CleanupResult,
    cleanup_executor_handles,
    cleanup_validation_results,
    run_retention_cycle,
)


def _run(coro):
    return asyncio.run(coro)


# === CleanupResult ====================================================

def test_cleanup_result_to_dict() -> None:
    r = CleanupResult(
        started_at="2026-01-01T00:00:00Z",
        completed_at="2026-01-01T00:01:00Z",
        validation_deleted=5,
        executor_handles_purged=2,
        errors=["x"],
    )
    d = r.to_dict()
    assert d["validation_deleted"] == 5
    assert d["executor_handles_purged"] == 2
    assert d["errors"] == ["x"]


# === cleanup_validation_results =======================================

class _FakeResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _FakeSession:
    def __init__(self, db) -> None:
        self.db = db
        self.last_sql: str | None = None
        self.last_params: dict | None = None

    async def execute(self, sql, params=None):
        self.last_sql = sql
        self.last_params = params or {}
        if sql.strip().lower().startswith("delete"):
            cutoff = self.last_params.get("cutoff")
            removed = 0
            kept = []
            for row in self.db["rows"]:
                if cutoff is not None and row["created_at"] < cutoff:
                    removed += 1
                else:
                    kept.append(row)
            self.db["rows"] = kept
            return _FakeResult(removed)
        return _FakeResult(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass


class _FakePostgres:
    def __init__(self) -> None:
        self.db = {"rows": []}

    def session(self):
        return _FakeSession(self.db)


def test_cleanup_validation_no_postgres_returns_zero() -> None:
    n = _run(cleanup_validation_results(postgres=None, older_than_days=30))
    assert n == 0


def test_cleanup_validation_zero_days_returns_zero() -> None:
    pg = _FakePostgres()
    n = _run(cleanup_validation_results(postgres=pg, older_than_days=0))
    assert n == 0


def test_cleanup_validation_deletes_only_old_rows() -> None:
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    pg = _FakePostgres()
    pg.db["rows"] = [
        {"id": "old1", "created_at": now - timedelta(days=100)},
        {"id": "old2", "created_at": now - timedelta(days=95)},
        {"id": "fresh", "created_at": now - timedelta(days=1)},
    ]
    n = _run(cleanup_validation_results(postgres=pg, older_than_days=90))
    assert n == 2
    assert [r["id"] for r in pg.db["rows"]] == ["fresh"]


def test_cleanup_validation_handles_session_failure() -> None:
    class _BrokenPg:
        def session(self):
            raise RuntimeError("db down")

    n = _run(cleanup_validation_results(postgres=_BrokenPg(), older_than_days=30))
    assert n == 0


# === cleanup_executor_handles =========================================

class _FakeRedis:
    def __init__(self, keys: dict[str, int]) -> None:
        # keys: {key: ttl_seconds (-1 для no-ttl, -2 для missing)}
        self.keys = dict(keys)
        self.deleted: list[str] = []

    async def scan_iter(self, match=None, count=500):
        for k in list(self.keys.keys()):
            if match is None or k.startswith(match.rstrip("*")):
                yield k

    async def ttl(self, key):
        return self.keys.get(key, -2)

    async def delete(self, key):
        self.deleted.append(key)
        self.keys.pop(key, None)


def test_cleanup_executor_no_redis_returns_zero() -> None:
    n = _run(cleanup_executor_handles(redis=None, older_than_days=30))
    assert n == 0


def test_cleanup_executor_purges_only_ttlless_keys() -> None:
    redis = _FakeRedis({
        "executor:a": -1,    # без TTL — должно удалиться
        "executor:b": 3600,  # имеет TTL — оставить
        "executor:c": -1,    # без TTL — должно удалиться
        "other:x": -1,       # не наш префикс — пропустить
    })
    n = _run(cleanup_executor_handles(redis=redis, older_than_days=30))
    assert n == 2
    assert sorted(redis.deleted) == ["executor:a", "executor:c"]


def test_cleanup_executor_handles_redis_error() -> None:
    class _BrokenRedis:
        def scan_iter(self, **kw):
            raise RuntimeError("connection lost")

    n = _run(cleanup_executor_handles(redis=_BrokenRedis(), older_than_days=30))
    assert n == 0


def test_cleanup_executor_skips_delete_failure() -> None:
    """delete() raises → ключ не считается, но цикл продолжается."""
    class _PartiallyBrokenRedis(_FakeRedis):
        async def delete(self, key):
            if key == "executor:bad":
                raise RuntimeError("nope")
            await super().delete(key)

    redis = _PartiallyBrokenRedis({
        "executor:bad": -1,
        "executor:ok": -1,
    })
    n = _run(cleanup_executor_handles(redis=redis, older_than_days=30))
    assert n == 1
    assert redis.deleted == ["executor:ok"]


# === run_retention_cycle ==============================================

def test_cycle_disabled_returns_empty_result() -> None:
    pg = _FakePostgres()
    redis = _FakeRedis({"executor:x": -1})
    r = _run(run_retention_cycle(
        postgres=pg, redis=redis,
        validation_retention_days=10, executor_retention_days=10,
        enabled=False,
    ))
    assert r.validation_deleted == 0
    assert r.executor_handles_purged == 0
    assert r.completed_at is not None


def test_cycle_runs_both_steps() -> None:
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    pg = _FakePostgres()
    pg.db["rows"] = [
        {"id": "old", "created_at": now - timedelta(days=200)},
    ]
    redis = _FakeRedis({"executor:a": -1, "executor:b": 100})
    r = _run(run_retention_cycle(
        postgres=pg, redis=redis,
        validation_retention_days=90, executor_retention_days=30,
        enabled=True,
    ))
    assert r.validation_deleted == 1
    assert r.executor_handles_purged == 1
    assert r.errors == []
    assert r.started_at <= r.completed_at


def test_cycle_isolates_step_failures() -> None:
    """Сбой одного шага не ломает другой."""
    class _BrokenPg:
        def session(self):
            raise RuntimeError("pg down")

    redis = _FakeRedis({"executor:a": -1})
    r = _run(run_retention_cycle(
        postgres=_BrokenPg(), redis=redis,
        validation_retention_days=90, executor_retention_days=30,
        enabled=True,
    ))
    # postgres path вернул 0 (handled inside helper), redis path отработал.
    assert r.validation_deleted == 0
    assert r.executor_handles_purged == 1


def test_cycle_with_no_backends() -> None:
    r = _run(run_retention_cycle(
        postgres=None, redis=None,
        validation_retention_days=90, executor_retention_days=30,
        enabled=True,
    ))
    assert r.validation_deleted == 0
    assert r.executor_handles_purged == 0
