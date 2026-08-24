"""Unit-тесты для core.executors.base (W19)."""
from __future__ import annotations

from backend.core.executors.base import (
    TaskHandle,
    TaskResult,
    TaskStatus,
    TaskSubmission,
)

# === TaskStatus enum ====================================================

def test_status_values_stable() -> None:
    """Имена статусов — публичный contract; не меняем без миграций."""
    assert TaskStatus.QUEUED.value == "queued"
    assert TaskStatus.RUNNING.value == "running"
    assert TaskStatus.DONE.value == "done"
    assert TaskStatus.FAILED.value == "failed"
    assert TaskStatus.CANCELLED.value == "cancelled"


# === TaskHandle =========================================================

def test_handle_new_unique_ids() -> None:
    h1 = TaskHandle.new(backend="noop")
    h2 = TaskHandle.new(backend="noop")
    assert h1.id != h2.id
    assert h1.id.startswith("task_")


def test_handle_new_defaults_queued() -> None:
    h = TaskHandle.new(backend="noop")
    assert h.status == TaskStatus.QUEUED


def test_handle_to_dict_round_trip() -> None:
    h = TaskHandle.new(backend="claude_code_cli", user_id="u-1", task_type="landing")
    d = h.to_dict()
    assert d["backend"] == "claude_code_cli"
    assert d["user_id"] == "u-1"
    assert d["status"] == "queued"

    restored = TaskHandle.from_dict(d)
    assert restored.id == h.id
    assert restored.status == h.status
    assert restored.task_type == "landing"


def test_handle_from_dict_unknown_status() -> None:
    """Защита от испорченных записей в Redis."""
    h = TaskHandle.from_dict({
        "id": "task_abc", "backend": "noop", "submitted_at": "2026-01-01",
        "status": "garbage",
    })
    assert h.status == TaskStatus.UNKNOWN


def test_handle_from_dict_minimal() -> None:
    h = TaskHandle.from_dict({"id": "task_x", "backend": "noop"})
    assert h.id == "task_x"
    assert h.backend == "noop"


# === TaskResult =========================================================

def test_result_to_dict() -> None:
    r = TaskResult(
        handle_id="task_x",
        status=TaskStatus.DONE,
        success=True,
        summary="ok",
        artifacts=[{"name": "out.txt", "kind": "file"}],
    )
    d = r.to_dict()
    assert d["status"] == "done"
    assert d["success"] is True
    assert len(d["artifacts"]) == 1


def test_result_failed() -> None:
    r = TaskResult(
        handle_id="task_x",
        status=TaskStatus.FAILED,
        success=False,
        error_message="timeout",
    )
    assert r.success is False
    assert r.error_message == "timeout"


# === TaskSubmission =====================================================

def test_submission_minimal() -> None:
    s = TaskSubmission(tz_markdown="hello world")
    assert s.tz_markdown == "hello world"
    assert s.timeout_seconds == 3600
    assert s.metadata == {}


def test_submission_to_dict_does_not_leak_full_tz() -> None:
    """Безопасный сериализатор: отдаём только длину ТЗ для логов/audit."""
    s = TaskSubmission(tz_markdown="secret content", task_type="api")
    d = s.to_dict()
    assert "tz_markdown" not in d
    assert d["tz_markdown_length"] == len("secret content")
    assert d["task_type"] == "api"


def test_submission_passes_metadata() -> None:
    s = TaskSubmission(tz_markdown="x", metadata={"k": "v"})
    assert s.to_dict()["metadata"] == {"k": "v"}
