# -*- coding: utf-8 -*-
"""Биллинговые окна квот (день/неделя/месяц) + месячный кап тарифа.

Гарантия «пользователь не потратит больше, чем мы заработали»: дневные капы
(были) + недельные (новые) + энфорс месячного $-пула тарифа."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone

import pytest

import backend.core.llm.quota as q


def _run(coro):
    return asyncio.run(coro)


# ── чистые окна ──────────────────────────────────────────────────────────

def test_window_since_pure():
    now = datetime(2026, 7, 4, 15, 0, tzinfo=timezone.utc)  # суббота
    assert q._window_since("day", now) is None               # store сам берёт сегодня
    assert q._window_since("week", now) == "2026-06-29"      # понедельник
    assert q._window_since("month", now) == "2026-07-01"
    # понедельник → неделя начинается сегодня
    mon = datetime(2026, 6, 29, 1, 0, tzinfo=timezone.utc)
    assert q._window_since("week", mon) == "2026-06-29"


def test_window_reset_pure():
    now = datetime(2026, 7, 4, 15, 0, tzinfo=timezone.utc)
    assert q._window_reset_iso("week", now).startswith("2026-07-06")   # след. пн
    assert q._window_reset_iso("month", now).startswith("2026-08-01")


# ── SQLite store: since_date ─────────────────────────────────────────────

def _mk_store(tmp_path):
    from backend.core.llm.usage_stores import SqliteUsageStore
    st = SqliteUsageStore(str(tmp_path / "usage.db"))
    st.init_schema()
    with sqlite3.connect(st.db_path) as c:
        for ts, tokens in (("2026-07-04T10:00:00", 100),
                           ("2026-06-30T10:00:00", 40),
                           ("2026-06-10T10:00:00", 7)):
            c.execute(
                "INSERT INTO llm_usage (timestamp, provider, model, "
                "total_tokens, total_cost, user_id) "
                "VALUES (?, 'x', 'm', ?, ?, 'u1')", (ts, tokens, tokens / 100))
    return st


def test_store_since_date_windows(tmp_path):
    st = _mk_store(tmp_path)
    # окно «с 1 июля» — только свежая строка
    assert st.sum_metric_for_quota("user", "u1", "total_tokens",
                                   since_date="2026-07-01") == 100
    # окно «с 29 июня» — плюс строка за 30 июня
    assert st.sum_metric_for_quota("user", "u1", "total_tokens",
                                   since_date="2026-06-29") == 140
    # окно «с 1 июня» — всё
    assert st.sum_metric_for_quota("user", "u1", "total_tokens",
                                   since_date="2026-06-01") == 147
    # $-метрика тем же окном
    assert st.sum_metric_for_quota("user", "u1", "total_cost",
                                   since_date="2026-06-29") == pytest.approx(1.4)


# ── недельная квота бросает 429-исключение ───────────────────────────────

def _wire_metric(monkeypatch, used_by_window: dict):
    """_daily_metric возвращает used по since_date (None=день)."""
    def fake_metric(db_path, *, scope, subject_id, metric, since_date=None):
        return float(used_by_window.get(since_date, 0.0))
    monkeypatch.setattr(q, "_daily_metric", fake_metric)


def test_weekly_limit_enforced(monkeypatch):
    week_since = q._window_since("week")
    _wire_metric(monkeypatch, {None: 10.0, week_since: 999_999.0})
    with pytest.raises(q.QuotaExceeded) as e:
        q._check_one(db_path="", scope="user", subject_id="u1",
                     metric="total_tokens", limit=500_000,
                     near_ratio=0.8, window="week")
    assert e.value.used == 999_999
    # reset — следующий понедельник, не полночь
    assert e.value.reset_at == q._window_reset_iso("week")


def test_day_ok_but_week_blown(monkeypatch):
    week_since = q._window_since("week")
    _wire_metric(monkeypatch, {None: 100.0, week_since: 600_000.0})
    # день проходит
    q._check_one(db_path="", scope="user", subject_id="u1",
                 metric="total_tokens", limit=2_000_000,
                 near_ratio=0.8, window="day")
    # неделя — нет
    with pytest.raises(q.QuotaExceeded):
        q._check_one(db_path="", scope="user", subject_id="u1",
                     metric="total_tokens", limit=500_000,
                     near_ratio=0.8, window="week")


# ── месячный кап тарифа ──────────────────────────────────────────────────

def test_tenant_monthly_cap_cached(monkeypatch):
    calls = {"n": 0}

    async def fake_limits(tenant_id):
        calls["n"] += 1
        return {"plan": "start", "monthly_tokens_usd": 25.0}
    import backend.core.auth.tenant_limits as tl
    monkeypatch.setattr(tl, "get_tenant_limits", fake_limits)
    monkeypatch.setattr(q, "_plan_cap_cache", {})
    assert _run(q._tenant_monthly_cap_cached("t1")) == 25.0
    assert _run(q._tenant_monthly_cap_cached("t1")) == 25.0
    assert calls["n"] == 1  # второй раз — из кэша (не БД-хит на каждый вызов)


def test_tenant_monthly_cap_absent_is_zero(monkeypatch):
    async def fake_limits(tenant_id):
        return {"plan": "free", "monthly_tokens_usd": None}
    import backend.core.auth.tenant_limits as tl
    monkeypatch.setattr(tl, "get_tenant_limits", fake_limits)
    monkeypatch.setattr(q, "_plan_cap_cache", {})
    assert _run(q._tenant_monthly_cap_cached("t2")) == 0.0  # нет капа → 0 (без проверки)
