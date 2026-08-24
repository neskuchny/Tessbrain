"""Unit-тесты для core.executors.store (W19).

Используем in-memory fallback (Redis недоступен в test env).
"""
from __future__ import annotations

import asyncio

import pytest
from backend.core.executors.base import TaskHandle, TaskResult, TaskStatus
from backend.core.executors.store import (
    delete_handle,
    get_handle,
    get_result,
    list_handles,
    reap_stale_running,
    reset_memory_store,
    save_handle,
    save_result,
    update_status,
)


@pytest.fixture(autouse=True)
def _clear_store():
    reset_memory_store()
    yield
    reset_memory_store()


def _run(coro):
    return asyncio.run(coro)


def test_save_and_get_roundtrip() -> None:
    h = TaskHandle.new(backend="noop", user_id="u-1")
    _run(save_handle(h))
    out = _run(get_handle(h.id))
    assert out is not None
    assert out.id == h.id
    assert out.user_id == "u-1"


def test_get_handle_missing_returns_none() -> None:
    out = _run(get_handle("task_does_not_exist"))
    assert out is None


def test_get_handle_empty_id() -> None:
    assert _run(get_handle("")) is None


def test_update_status_changes_handle() -> None:
    h = TaskHandle.new(backend="noop")
    _run(save_handle(h))
    ok = _run(update_status(h.id, TaskStatus.RUNNING))
    assert ok is True
    fresh = _run(get_handle(h.id))
    assert fresh.status == TaskStatus.RUNNING


def test_update_status_missing_handle() -> None:
    ok = _run(update_status("task_missing", TaskStatus.DONE))
    assert ok is False


def test_delete_removes_both_handle_and_result() -> None:
    h = TaskHandle.new(backend="noop")
    r = TaskResult(handle_id=h.id, status=TaskStatus.DONE, success=True)
    _run(save_handle(h))
    _run(save_result(r))
    _run(delete_handle(h.id))
    assert _run(get_handle(h.id)) is None
    assert _run(get_result(h.id)) is None


def test_save_and_get_result() -> None:
    r = TaskResult(
        handle_id="task_x",
        status=TaskStatus.DONE,
        success=True,
        summary="ok",
        artifacts=[{"name": "out.md", "kind": "file"}],
    )
    _run(save_result(r))
    raw = _run(get_result("task_x"))
    assert raw is not None
    assert raw["handle_id"] == "task_x"
    assert raw["success"] is True


def test_get_result_missing() -> None:
    assert _run(get_result("task_unknown")) is None
    assert _run(get_result("")) is None


# === Reaper (L4: зависшие RUNNING) =======================================

def _handle_at(hid: str, status: TaskStatus, minutes_ago: int) -> TaskHandle:
    from datetime import datetime, timedelta, timezone
    return TaskHandle(
        id=hid, backend="claude_code_cli",
        submitted_at=(datetime.now(timezone.utc)
                      - timedelta(minutes=minutes_ago)).isoformat(),
        status=status)


def test_list_handles_returns_all() -> None:
    _run(save_handle(_handle_at("h1", TaskStatus.RUNNING, 5)))
    _run(save_handle(_handle_at("h2", TaskStatus.DONE, 5)))
    handles = _run(list_handles())
    assert {h.id for h in handles} == {"h1", "h2"}


def test_reap_marks_stale_running_failed() -> None:
    _run(save_handle(_handle_at("stale", TaskStatus.RUNNING, 120)))
    n = _run(reap_stale_running(90))
    assert n == 1
    h = _run(get_handle("stale"))
    assert h.status == TaskStatus.FAILED
    res = _run(get_result("stale"))
    assert res and "reaped" in (res.get("summary") or "")
    assert res["success"] is False


def test_reap_skips_fresh_running() -> None:
    _run(save_handle(_handle_at("fresh", TaskStatus.RUNNING, 5)))
    assert _run(reap_stale_running(90)) == 0
    assert _run(get_handle("fresh")).status == TaskStatus.RUNNING


def test_reap_ignores_terminal_and_is_idempotent() -> None:
    _run(save_handle(_handle_at("done_old", TaskStatus.DONE, 300)))
    _run(save_handle(_handle_at("failed_old", TaskStatus.FAILED, 300)))
    _run(save_handle(_handle_at("stale", TaskStatus.RUNNING, 300)))
    assert _run(reap_stale_running(90)) == 1        # only the RUNNING one
    assert _run(get_handle("done_old")).status == TaskStatus.DONE
    assert _run(reap_stale_running(90)) == 0        # second pass finds nothing
