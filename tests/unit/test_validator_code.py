"""Unit-тесты для validator.code (W21).

Проверяем pure logic — для real-tool вызовов нужны бинарники в env,
которые есть не везде. Поэтому в тестах либо без code artifacts
(должен no-op), либо с ошибкой syntax.
"""
from __future__ import annotations

import asyncio

from backend.core.validator.code import CodeValidator, _detect_language


def _run(coro):
    return asyncio.run(coro)


# === _detect_language ==================================================

def test_detect_python() -> None:
    assert _detect_language("foo.py") == "python"


def test_detect_typescript() -> None:
    assert _detect_language("App.tsx") == "typescript"
    assert _detect_language("util.ts") == "typescript"


def test_detect_javascript() -> None:
    assert _detect_language("index.js") == "javascript"
    assert _detect_language("Comp.jsx") == "javascript"


def test_detect_shell() -> None:
    assert _detect_language("deploy.sh") == "shell"


def test_detect_unknown() -> None:
    assert _detect_language("README.md") is None
    assert _detect_language("data.json") is None


def test_detect_case_insensitive() -> None:
    assert _detect_language("MAIN.PY") == "python"


# === CodeValidator end-to-end ==========================================

def test_no_code_artifacts_returns_full_score() -> None:
    """Если нет code-files — validator нейтрален (10/10)."""
    v = CodeValidator()
    r = _run(v.validate(
        task_description="x", tz_markdown="y",
        artifacts=[{"name": "out.md", "content": "doc", "kind": "file"}],
    ))
    assert r.score == 10.0
    assert r.metadata["checked_files"] == 0


def test_skips_artifacts_without_content() -> None:
    v = CodeValidator()
    r = _run(v.validate(
        task_description="x", tz_markdown="y",
        artifacts=[
            {"name": "x.py", "kind": "file"},     # no content
            {"name": "y.py", "kind": "file", "content": ""},   # empty
        ],
    ))
    assert r.metadata["checked_files"] == 0


def test_python_syntax_error_detected() -> None:
    """Невалидный Python → ERROR."""
    v = CodeValidator()
    r = _run(v.validate(
        task_description="x", tz_markdown="y",
        artifacts=[{
            "name": "broken.py",
            "kind": "file",
            "content": "def foo(\n    pass",   # синтаксис сломан
        }],
    ))
    # py_compile должен поймать — есть python в test env.
    assert r.metadata["checked_files"] == 1
    has_syntax_error = any(i.category == "syntax_error" for i in r.issues)
    assert has_syntax_error


def test_python_valid_code_no_syntax_error() -> None:
    """Валидный Python → нет syntax_error issues."""
    v = CodeValidator()
    r = _run(v.validate(
        task_description="x", tz_markdown="y",
        artifacts=[{
            "name": "ok.py",
            "kind": "file",
            "content": "def foo():\n    return 42\n",
        }],
    ))
    assert r.metadata["checked_files"] == 1
    has_syntax_error = any(i.category == "syntax_error" for i in r.issues)
    assert has_syntax_error is False


def test_validates_multiple_files() -> None:
    v = CodeValidator()
    r = _run(v.validate(
        task_description="x", tz_markdown="y",
        artifacts=[
            {"name": "a.py", "kind": "file", "content": "x = 1\n"},
            {"name": "b.py", "kind": "file", "content": "y = 2\n"},
            {"name": "c.txt", "kind": "file", "content": "not code"},
        ],
    ))
    assert r.metadata["checked_files"] == 2
