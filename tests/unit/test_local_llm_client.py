"""Unit-тесты для LocalLLMClient (W8).

Без реального HTTP-вызова: проверяем только конфигурацию и graceful
disable. Полноценный e2e-test требует поднятого vLLM/Ollama —
это в integration-suite, не здесь.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "backend" / "core" / "llm" / "local_client.py"
)
# Грузим local_client под отдельным namespace `_lc_isolated.*`, чтобы
# не подмешивать заглушки в реальный `backend.core.llm` namespace
# (который нужен другим тестам, например test_enterprise_mode.py).
_base_path = _PATH.parent / "base.py"
_base_spec = importlib.util.spec_from_file_location(
    "_lc_isolated.base", _base_path
)
_base = importlib.util.module_from_spec(_base_spec)
sys.modules[_base_spec.name] = _base
_base_spec.loader.exec_module(_base)

# local_client.py использует `from .base import BaseLLMClient` — относительный
# импорт. Резолвится через __package__, поэтому запускаем под нужным пакетом.
import types as _types

_pkg = _types.ModuleType("_lc_isolated")
_pkg.__path__ = []  # type: ignore[attr-defined]
sys.modules.setdefault("_lc_isolated", _pkg)

_spec = importlib.util.spec_from_file_location(
    "_lc_isolated.local_client",
    _PATH,
    submodule_search_locations=None,
)
_module = importlib.util.module_from_spec(_spec)
_module.__package__ = "_lc_isolated"
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)
LocalLLMClient = _module.LocalLLMClient


def test_disabled_when_base_url_missing(monkeypatch) -> None:
    monkeypatch.delenv("LLM_LOCAL_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_LOCAL_MODEL", raising=False)
    c = LocalLLMClient()
    assert c.enabled is False
    assert c._client is None


def test_disabled_when_model_missing(monkeypatch) -> None:
    monkeypatch.setenv("LLM_LOCAL_BASE_URL", "http://vllm:8000/v1")
    monkeypatch.delenv("LLM_LOCAL_MODEL", raising=False)
    c = LocalLLMClient()
    assert c.enabled is False


def test_zero_cost(monkeypatch) -> None:
    """Local inference — нулевая marginal cost."""
    monkeypatch.setenv("LLM_LOCAL_BASE_URL", "http://vllm:8000/v1")
    monkeypatch.setenv("LLM_LOCAL_MODEL", "llama-3.3-70b")
    c = LocalLLMClient()
    assert c._calculate_cost(1_000_000, 1_000_000) == 0.0


def test_explicit_args_override_env(monkeypatch) -> None:
    monkeypatch.delenv("LLM_LOCAL_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_LOCAL_MODEL", raising=False)
    c = LocalLLMClient(model="qwen2.5", base_url="http://ollama:11434/v1")
    assert c.model == "qwen2.5"
    assert c.base_url == "http://ollama:11434/v1"


def test_generate_raises_when_disabled() -> None:
    import asyncio
    c = LocalLLMClient(model="", base_url="")
    assert c.enabled is False
    with __import__("pytest").raises(RuntimeError):
        asyncio.run(c.generate("hi"))


def test_generate_json_raises_when_disabled() -> None:
    import asyncio
    c = LocalLLMClient(model="", base_url="")
    with __import__("pytest").raises(RuntimeError):
        asyncio.run(c.generate_json("hi"))
