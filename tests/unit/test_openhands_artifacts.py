"""Unit-тесты для openhands_artifacts (W20)."""
from __future__ import annotations

from backend.core.executors.backends.openhands_artifacts import (
    CollectedArtifact,
    _is_skipped,
    _is_text_path,
)

# === _is_skipped ========================================================

def test_skip_dot_git() -> None:
    assert _is_skipped(".git/HEAD") is True
    assert _is_skipped("src/.git/config") is True


def test_skip_node_modules() -> None:
    assert _is_skipped("node_modules/react/index.js") is True
    assert _is_skipped("packages/x/node_modules/y") is True


def test_skip_pycache() -> None:
    assert _is_skipped("backend/__pycache__/foo.cpython-311.pyc") is True


def test_skip_venv() -> None:
    assert _is_skipped(".venv/lib/site-packages/x") is True
    assert _is_skipped("venv/bin/activate") is True


def test_skip_build_caches() -> None:
    assert _is_skipped("dist/main.js") is True
    assert _is_skipped("build/output.html") is True
    assert _is_skipped(".next/cache/x") is True


def test_does_not_skip_normal_files() -> None:
    assert _is_skipped("src/index.tsx") is False
    assert _is_skipped("README.md") is False
    assert _is_skipped("backend/api/app.py") is False


def test_does_not_skip_leading_slash() -> None:
    """Лидирующий слэш не должен фейлить лоgику."""
    assert _is_skipped("/src/index.js") is False
    assert _is_skipped("/.git/HEAD") is True


# === _is_text_path ======================================================

def test_text_path_known_extensions() -> None:
    for ext in (".md", ".py", ".js", ".json", ".yaml", ".html", ".css"):
        assert _is_text_path(f"file{ext}") is True


def test_text_path_binary_extensions() -> None:
    for ext in (".png", ".jpg", ".pdf", ".zip", ".tar"):
        assert _is_text_path(f"file{ext}") is False


def test_text_path_no_extension() -> None:
    """Файлы без extension — считаем текстовыми (часто это README, Dockerfile)."""
    assert _is_text_path("Dockerfile") is True
    assert _is_text_path("Makefile") is True


def test_text_path_case_insensitive() -> None:
    assert _is_text_path("README.MD") is True
    assert _is_text_path("Image.PNG") is False


# === CollectedArtifact.to_dict =========================================

def test_artifact_to_dict_text_file() -> None:
    a = CollectedArtifact(
        name="hero.tsx",
        kind="file",
        path="src/hero.tsx",
        content="export default Hero",
        size_bytes=20,
    )
    d = a.to_dict()
    assert d["name"] == "hero.tsx"
    assert d["kind"] == "file"
    assert d["content"] == "export default Hero"
    assert "truncated" not in d


def test_artifact_to_dict_binary() -> None:
    a = CollectedArtifact(
        name="image.png",
        kind="binary",
        size_bytes=5000,
    )
    d = a.to_dict()
    assert d["kind"] == "binary"
    assert "content" not in d


def test_artifact_to_dict_truncated() -> None:
    a = CollectedArtifact(
        name="big.log",
        size_bytes=2_000_000,
        content="part of file",
        truncated=True,
    )
    d = a.to_dict()
    assert d["truncated"] is True
    assert d["size_bytes"] == 2_000_000
