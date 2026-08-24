# -*- coding: utf-8 -*-
"""Устойчивость сбора: повторы + журнал неудач.

Главный инвариант, ради которого это писалось: кусок памяти компании не
должен теряться молча. Либо доехал, либо повторили, либо это видно в
журнале и в счётчике.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ingest import resilience as R


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("INGEST_FAILURES_DIR", str(tmp_path / "failures"))
    # Задержки в тестах не нужны: проверяем логику, а не таймеры.
    monkeypatch.setenv("INGEST_RETRY_BASE_DELAY", "0")
    yield


# ── что считается временным сбоем ───────────────────────────────────


@pytest.mark.parametrize("exc", [
    asyncio.TimeoutError(),
    TimeoutError(),
    ConnectionError("reset by peer"),
    OSError("network unreachable"),
])
def test_network_errors_are_retryable(exc):
    assert R.is_retryable(exc) is True


def test_cancellation_is_never_retried():
    """Отмена задачи — не сбой сети; повтор здесь ломал бы выключение."""
    assert R.is_retryable(asyncio.CancelledError()) is False


@pytest.mark.parametrize("status,expected", [
    (429, True),    # притормозили — имеет смысл подождать
    (503, True),    # у той стороны временно плохо
    (500, True),
    (401, False),   # токен не станет валидным от повтора
    (403, False),
    (404, False),
    (400, False),
])
def test_http_status_decides_retry(status, expected):
    class HttpErr(Exception):
        def __init__(self, code): self.status_code = code
    assert R.is_retryable(HttpErr(status)) is expected


def test_status_read_from_nested_response():
    """Клиенты кладут код по-разному — читаем и из .response."""
    class Resp: status_code = 429
    class Err(Exception): response = Resp()
    assert R.is_retryable(Err()) is True


def test_unknown_error_by_class_name():
    class ReadTimeoutError(Exception): pass
    class ValidationError(Exception): pass
    assert R.is_retryable(ReadTimeoutError()) is True
    assert R.is_retryable(ValidationError()) is False


# ── повторы ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_succeeds_without_retry():
    calls = []

    async def ok():
        calls.append(1)
        return "done"

    assert await R.with_retries(ok, attempts=3) == "done"
    assert len(calls) == 1, "успех не должен вызывать повторов"


@pytest.mark.asyncio
async def test_retries_until_success():
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("boom")
        return "recovered"

    assert await R.with_retries(flaky, attempts=3, base_delay=0) == "recovered"
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_raises_after_exhausting_attempts():
    calls = []

    async def always_fails():
        calls.append(1)
        raise ConnectionError("still down")

    with pytest.raises(ConnectionError):
        await R.with_retries(always_fails, attempts=3, base_delay=0)
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_non_retryable_fails_fast():
    """401 не чинится повтором — не жжём лимиты провайдера впустую."""
    calls = []

    class Unauthorized(Exception):
        status_code = 401

    async def denied():
        calls.append(1)
        raise Unauthorized()

    with pytest.raises(Unauthorized):
        await R.with_retries(denied, attempts=5, base_delay=0)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_attempts_one_means_no_retry():
    calls = []

    async def fails():
        calls.append(1)
        raise ConnectionError("x")

    with pytest.raises(ConnectionError):
        await R.with_retries(fails, attempts=1)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cancellation_propagates_immediately():
    calls = []

    async def cancelled():
        calls.append(1)
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await R.with_retries(cancelled, attempts=3, base_delay=0)
    assert len(calls) == 1


def test_attempts_from_env(monkeypatch):
    monkeypatch.setenv("INGEST_RETRY_ATTEMPTS", "5")
    assert R.default_attempts() == 5
    monkeypatch.setenv("INGEST_RETRY_ATTEMPTS", "мусор")
    assert R.default_attempts() == 3, "кривое значение → безопасный дефолт"
    monkeypatch.setenv("INGEST_RETRY_ATTEMPTS", "999")
    assert R.default_attempts() == 8, "верхняя граница защищает от залипания"


# ── журнал неудач ───────────────────────────────────────────────────


def test_record_and_list():
    R.record_failure("u-1", source_key="slack:C123", stage="slack_sync",
                     error=ConnectionError("timeout"), attempts=3,
                     platform="slack")
    rows = R.list_failures("u-1")
    assert len(rows) == 1
    assert rows[0]["source_key"] == "slack:C123"
    assert rows[0]["stage"] == "slack_sync"
    assert rows[0]["attempts"] == 3
    assert "ConnectionError" in rows[0]["error"]


def test_failures_are_per_user():
    R.record_failure("u-1", source_key="a", stage="digest", error="x")
    assert R.list_failures("u-2") == [], "журнал одного не виден другому"


def test_summary_groups_by_source_and_stage():
    R.record_failure("u-1", source_key="slack:A", stage="analyze", error="e1")
    R.record_failure("u-1", source_key="slack:A", stage="digest", error="e2")
    R.record_failure("u-1", source_key="tg:B", stage="analyze", error="e3")
    s = R.failures_summary("u-1")
    assert s["total"] == 3
    assert s["last_24h"] == 3
    assert s["sources"][0] == {"source_key": "slack:A", "count": 2}
    assert {x["stage"] for x in s["stages"]} == {"analyze", "digest"}


def test_empty_summary_is_zero_not_error():
    s = R.failures_summary("u-nobody")
    assert s["total"] == 0 and s["sources"] == []


def test_clear_returns_count_and_empties():
    for i in range(3):
        R.record_failure("u-1", source_key=f"s{i}", stage="digest", error="e")
    assert R.clear_failures("u-1") == 3
    assert R.list_failures("u-1") == []


def test_record_never_raises_on_bad_input():
    """Журнал не имеет права уронить сам сбор."""
    R.record_failure("u-1", source_key="s", stage="digest",
                     error=ValueError("x" * 5000), attempts=-1)
    row = R.list_failures("u-1")[0]
    assert len(row["error"]) <= 500


# ── связка с auto_cycle ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_cycle_reports_failures_not_silence(monkeypatch, tmp_path):
    """Раньше упавший шаг выглядел как «нечего было делать».

    Теперь провал виден: status=partial, счётчик failed, запись в журнале."""
    from backend.core.messengers import chat_ingest as ci

    monkeypatch.setenv("ENABLE_CHAT_INGEST", "1")
    monkeypatch.setenv("INGEST_RETRY_ATTEMPTS", "2")
    monkeypatch.setattr(ci, "list_sources", lambda uid: [
        {"key": "slack:C1", "platform": "slack", "chat_id": "C1",
         "enabled": True, "last_message_at": "2026-08-04T10:00:00Z",
         "last_digest_at": "", "mode": "full"},
    ])

    async def _boom(*a, **kw):
        raise ConnectionError("slack unreachable")

    monkeypatch.setattr(ci, "sync_slack", _boom)
    monkeypatch.setattr(ci, "analyze_source", _boom)
    monkeypatch.setattr(ci, "digest_to_brain", _boom)

    res = await ci.auto_cycle("u-1")

    assert res["status"] == "partial", "провал не должен выглядеть успехом"
    assert res["failed"] == 3, "все три шага упали и посчитаны"
    assert res["digested"] == 0

    rows = R.list_failures("u-1")
    assert {r["stage"] for r in rows} == {"slack_sync", "analyze", "digest"}
    assert all(r["attempts"] == 2 for r in rows)


@pytest.mark.asyncio
async def test_auto_cycle_clean_run_stays_success(monkeypatch):
    """Без сбоев поведение прежнее — status success, failed=0."""
    from backend.core.messengers import chat_ingest as ci

    monkeypatch.setenv("ENABLE_CHAT_INGEST", "1")
    monkeypatch.setattr(ci, "list_sources", lambda uid: [
        {"key": "tg:G1", "platform": "telegram", "chat_id": "G1",
         "enabled": True, "last_message_at": "2026-08-04T10:00:00Z",
         "last_digest_at": "", "mode": "full"},
    ])

    async def _ok(*a, **kw):
        return {"status": "success"}

    monkeypatch.setattr(ci, "analyze_source", _ok)
    monkeypatch.setattr(ci, "digest_to_brain", _ok)

    res = await ci.auto_cycle("u-2")
    assert res["status"] == "success"
    assert res["failed"] == 0
    assert res["analyzed"] == 1 and res["digested"] == 1
    assert R.list_failures("u-2") == []


@pytest.mark.asyncio
async def test_auto_cycle_recovers_on_retry(monkeypatch):
    """Первая попытка упала, вторая прошла — данные доехали, журнал пуст."""
    from backend.core.messengers import chat_ingest as ci

    monkeypatch.setenv("ENABLE_CHAT_INGEST", "1")
    monkeypatch.setenv("INGEST_RETRY_ATTEMPTS", "3")
    monkeypatch.setattr(ci, "list_sources", lambda uid: [
        {"key": "tg:G1", "platform": "telegram", "chat_id": "G1",
         "enabled": True, "last_message_at": "2026-08-04T10:00:00Z",
         "last_digest_at": "", "mode": "full"},
    ])

    calls = []

    async def _flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("blip")
        return {"status": "success"}

    async def _ok(*a, **kw):
        return {"status": "success"}

    monkeypatch.setattr(ci, "analyze_source", _flaky)
    monkeypatch.setattr(ci, "digest_to_brain", _ok)

    res = await ci.auto_cycle("u-3")
    assert res["status"] == "success", "восстановились — это не частичный сбой"
    assert res["analyzed"] == 1
    assert R.list_failures("u-3") == []


@pytest.mark.asyncio
async def test_context_only_source_not_digested(monkeypatch):
    """Режим «только контекст» по-прежнему не выгружает в мозг."""
    from backend.core.messengers import chat_ingest as ci

    monkeypatch.setenv("ENABLE_CHAT_INGEST", "1")
    monkeypatch.setattr(ci, "list_sources", lambda uid: [
        {"key": "tg:G1", "platform": "telegram", "chat_id": "G1",
         "enabled": True, "last_message_at": "2026-08-04T10:00:00Z",
         "last_digest_at": "", "mode": "context_only"},
    ])

    async def _ok(*a, **kw):
        return {"status": "success"}

    called = []

    async def _digest(*a, **kw):
        called.append(1)
        return {"status": "success"}

    monkeypatch.setattr(ci, "analyze_source", _ok)
    monkeypatch.setattr(ci, "digest_to_brain", _digest)

    res = await ci.auto_cycle("u-4")
    assert res["analyzed"] == 1
    assert res["digested"] == 0
    assert not called, "в мозг ничего уходить не должно"
