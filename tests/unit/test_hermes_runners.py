"""Unit-тесты P12i-IND: реальные runners (executor code / brain delegate).

Backend и answer_fn инъектируем — без OpenHands/LLM/сети.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import sys
import types

_ROOT = pathlib.Path(__file__).resolve().parents[2]

for pkg in ("backend", "backend.core", "backend.core.hermes"):
    if pkg not in sys.modules:
        m = types.ModuleType(pkg)
        m.__path__ = []  # type: ignore[attr-defined]
        sys.modules[pkg] = m

# НЕ стабим backend.core.executors (изоляция: иначе ломаем
# test_executor_noop). runners.py сам имеет fallback для
# TaskSubmission, когда реальный executors-пакет недоступен.

_spec = importlib.util.spec_from_file_location(
    "backend.core.hermes.runners",
    _ROOT / "backend/core/hermes/runners.py",
)
_r = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _r
_spec.loader.exec_module(_r)

make_executor_code_runner = _r.make_executor_code_runner
make_brain_delegate = _r.make_brain_delegate


def _run(c):
    return asyncio.run(c)


class _Handle:
    id = "h1"


class _Result:
    def __init__(self, ok=True):
        self.success = ok
        self.summary = "ran ok"
        self.logs_excerpt = "out: 42"
        self.error_message = "" if ok else "boom"


class _Backend:
    name = "fake"

    def __init__(self, result=None, never=False):
        self._result = result
        self._never = never
        self.submitted = []

    async def submit(self, sub):
        self.submitted.append(sub.tz_markdown)
        return _Handle()

    async def get_result(self, handle):
        return None if self._never else self._result


# === code runner =======================================================

def test_code_runner_success() -> None:
    be = _Backend(result=_Result(ok=True))
    run = make_executor_code_runner(backend=be)
    out = json.loads(_run(run("print(1)", "python")))
    assert out["success"] is True and out["backend"] == "fake"
    assert "print(1)" in be.submitted[0] and "python" in be.submitted[0]


def test_code_runner_failure_result() -> None:
    run = make_executor_code_runner(backend=_Backend(result=_Result(False)))
    out = json.loads(_run(run("x", "python")))
    assert out["success"] is False and out["error"] == "boom"


def test_code_runner_pending_when_no_result() -> None:
    run = make_executor_code_runner(backend=_Backend(never=True),
                                    max_wait_s=0.05, poll_s=0.01)
    out = json.loads(_run(run("x", "python")))
    assert out["status"] == "pending" and out["handle"] == "h1"


def test_code_runner_backend_exception_safe() -> None:
    class _Boom:
        name = "b"

        async def submit(self, s):
            raise RuntimeError("backend down")

    out = json.loads(_run(make_executor_code_runner(backend=_Boom())(
        "x", "py")))
    assert out["error"] == "code execution failed"  # never raises


# === brain delegate ====================================================

def test_delegate_calls_answer_fn_for_brain() -> None:
    async def answer(q):
        return f"brain: {q}"

    d = make_brain_delegate(answer)
    assert _run(d("brain", "summarize X")) == "brain: summarize X"


def test_delegate_unsupported_target() -> None:
    async def answer(q):
        return "x"

    out = json.loads(_run(make_brain_delegate(answer)("mark", "q")))
    assert "error" in out and "brain" in out["supported"]


def test_delegate_none_answer_fn_safe() -> None:
    out = json.loads(_run(make_brain_delegate(None)("brain", "q")))
    assert "error" in out


def test_delegate_answer_fn_failure_safe() -> None:
    async def boom(q):
        raise RuntimeError("llm down")

    out = json.loads(_run(make_brain_delegate(boom)("brain", "q")))
    assert out["error"] == "delegate failed"  # never raises


def test_delegate_custom_supported_targets() -> None:
    async def answer(q):
        return "ok"

    d = make_brain_delegate(answer, supported=("brain", "mark"))
    assert _run(d("mark", "q")) == "ok"
