"""Unit-тесты для core.llm.quota (W2 phase 4a + W4 phase 7d).

Подделываем UsageTracker через temporary SQLite, чтобы проверить и
per-user, и per-tenant scope без реального трекера.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile

import pytest
from backend.core.llm.quota import (
    QuotaExceeded,
    _next_utc_midnight_iso,
    _utc_today_iso,
    assert_within_quota,
    get_quota_status,
)


@pytest.fixture()
def fake_tracker(monkeypatch: pytest.MonkeyPatch):
    """Подложить временную SQLite в качестве БД UsageTracker.

    В неё пишем по одной записи на user/tenant и проверяем, что quota
    их видит.
    """
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE llm_usage ("
        "  timestamp TEXT, user_id TEXT, tenant_id TEXT, total_tokens INTEGER"
        ")",
    )
    conn.commit()
    conn.close()

    # issue #110 пакет 4: quota читает расход через tracker.store
    # (UsageStore-делегат), а не напрямую по db_path. Стаб даёт реальный
    # SqliteUsageStore поверх той же временной БД.
    from backend.core.llm.usage_stores import SqliteUsageStore

    class StubTracker:
        def __init__(self) -> None:
            self.db_path = db
            self.store = SqliteUsageStore(db)

    import backend.core.llm.usage_tracker as ut
    monkeypatch.setattr(ut, "get_usage_tracker", lambda: StubTracker())

    yield db
    try:
        os.unlink(db)
    except OSError:
        pass


def _insert(db_path: str, *, user_id: str | None = None, tenant_id: str | None = None, tokens: int = 0) -> None:
    today = _utc_today_iso() + "T10:00:00"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO llm_usage (timestamp, user_id, tenant_id, total_tokens) VALUES (?, ?, ?, ?)",
        (today, user_id, tenant_id, tokens),
    )
    conn.commit()
    conn.close()


def test_helpers_format_iso() -> None:
    today = _utc_today_iso()
    assert len(today) == 10  # YYYY-MM-DD
    midnight = _next_utc_midnight_iso()
    assert midnight.endswith("00+00:00") or "+00:00" in midnight


def test_under_user_limit_passes(fake_tracker, monkeypatch):
    _insert(fake_tracker, user_id="u1", tokens=50_000)
    import backend.config as cfg
    monkeypatch.setattr(cfg.settings, "llm_user_daily_token_limit", 100_000)
    monkeypatch.setattr(cfg.settings, "llm_tenant_daily_token_limit", 0)
    monkeypatch.setattr(cfg.settings, "llm_quota_enabled", True)
    asyncio.run(assert_within_quota(user_id="u1"))


def test_over_user_limit_raises(fake_tracker, monkeypatch):
    _insert(fake_tracker, user_id="u1", tokens=200_000)
    import backend.config as cfg
    monkeypatch.setattr(cfg.settings, "llm_user_daily_token_limit", 100_000)
    monkeypatch.setattr(cfg.settings, "llm_tenant_daily_token_limit", 0)
    monkeypatch.setattr(cfg.settings, "llm_quota_enabled", True)

    with pytest.raises(QuotaExceeded) as exc_info:
        asyncio.run(assert_within_quota(user_id="u1"))
    err = exc_info.value
    assert err.scope == "user"
    assert err.subject_id == "u1"
    assert err.used == 200_000
    assert err.limit == 100_000


def test_tenant_scope_aggregates_across_users(fake_tracker, monkeypatch):
    _insert(fake_tracker, user_id="u1", tenant_id="tA", tokens=2_000_000)
    _insert(fake_tracker, user_id="u2", tenant_id="tA", tokens=3_500_000)
    _insert(fake_tracker, user_id="u3", tenant_id="tB", tokens=999)  # noise
    import backend.config as cfg
    monkeypatch.setattr(cfg.settings, "llm_user_daily_token_limit", 0)         # disabled
    monkeypatch.setattr(cfg.settings, "llm_tenant_daily_token_limit", 5_000_000)
    monkeypatch.setattr(cfg.settings, "llm_quota_enabled", True)

    with pytest.raises(QuotaExceeded) as exc_info:
        asyncio.run(assert_within_quota(tenant_id="tA"))
    err = exc_info.value
    assert err.scope == "tenant"
    assert err.used == 5_500_000
    assert err.limit == 5_000_000


def test_quota_disabled_skips(fake_tracker, monkeypatch):
    _insert(fake_tracker, user_id="u1", tokens=999_999_999)
    import backend.config as cfg
    monkeypatch.setattr(cfg.settings, "llm_user_daily_token_limit", 1)
    monkeypatch.setattr(cfg.settings, "llm_quota_enabled", False)  # ← disabled
    # Должна пройти даже с огромным расходом.
    asyncio.run(assert_within_quota(user_id="u1"))


def test_status_reports_used_and_remaining(fake_tracker, monkeypatch):
    _insert(fake_tracker, user_id="u1", tokens=120_000)
    _insert(fake_tracker, tenant_id="tA", tokens=1_000_000)
    import backend.config as cfg
    monkeypatch.setattr(cfg.settings, "llm_user_daily_token_limit", 200_000)
    monkeypatch.setattr(cfg.settings, "llm_tenant_daily_token_limit", 5_000_000)
    monkeypatch.setattr(cfg.settings, "llm_quota_enabled", True)

    status = asyncio.run(get_quota_status(user_id="u1", tenant_id="tA"))
    assert status["enabled"] is True
    assert status["user"]["used"] == 120_000
    assert status["user"]["remaining"] == 80_000
    assert status["tenant"]["used"] == 1_000_000
    assert status["tenant"]["remaining"] == 4_000_000


def test_status_zero_limit_means_unlimited(fake_tracker, monkeypatch):
    _insert(fake_tracker, user_id="u1", tokens=999_999_999)
    import backend.config as cfg
    monkeypatch.setattr(cfg.settings, "llm_user_daily_token_limit", 0)
    monkeypatch.setattr(cfg.settings, "llm_tenant_daily_token_limit", 0)
    monkeypatch.setattr(cfg.settings, "llm_quota_enabled", True)

    status = asyncio.run(get_quota_status(user_id="u1"))
    assert status["user"]["limit"] == 0
    assert status["user"]["remaining"] is None  # 0 = unlimited
