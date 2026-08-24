"""Unit-тесты для NoopExecutor (W19)."""
from __future__ import annotations

import asyncio

import pytest
from backend.core.executors.backends.noop import NoopExecutor
from backend.core.executors.base import TaskStatus, TaskSubmission
from backend.core.executors.store import reset_memory_store


@pytest.fixture(autouse=True)
def _clear_store():
    reset_memory_store()
    yield
    reset_memory_store()


def _run(coro):
    return asyncio.run(coro)


def test_submit_returns_handle() -> None:
    executor = NoopExecutor()
    submission = TaskSubmission(tz_markdown="some TZ")
    handle = _run(executor.submit(submission))
    assert handle.backend == "noop"
    assert handle.status == TaskStatus.DONE   # noop сразу done


def test_submit_saves_handle_and_result() -> None:
    executor = NoopExecutor()
    submission = TaskSubmission(
        tz_markdown="my TZ content",
        task_type="landing",
        metadata={"user_id": "u-1"},
    )
    handle = _run(executor.submit(submission))
    result = _run(executor.get_result(handle))
    assert result is not None
    assert result.success is True
    assert result.handle_id == handle.id
    # Echo артефакт содержит исходный TZ.
    assert any("my TZ content" in str(a.get("content", "")) for a in result.artifacts)


def test_get_status_returns_done() -> None:
    executor = NoopExecutor()
    handle = _run(executor.submit(TaskSubmission(tz_markdown="x")))
    assert _run(executor.get_status(handle)) == TaskStatus.DONE


def test_get_result_unknown_handle() -> None:
    """Result для незнакомого handle = None."""
    from backend.core.executors.base import TaskHandle
    fake_handle = TaskHandle.new(backend="noop")
    executor = NoopExecutor()
    assert _run(executor.get_result(fake_handle)) is None


def test_cancel_returns_true() -> None:
    executor = NoopExecutor()
    handle = _run(executor.submit(TaskSubmission(tz_markdown="x")))
    assert _run(executor.cancel(handle)) is True


def test_metadata_user_id_propagated_to_handle() -> None:
    executor = NoopExecutor()
    submission = TaskSubmission(
        tz_markdown="x", metadata={"user_id": "u-42", "tenant_id": "t-1"},
    )
    handle = _run(executor.submit(submission))
    assert handle.user_id == "u-42"
    assert handle.tenant_id == "t-1"
