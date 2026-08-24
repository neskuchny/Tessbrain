"""Unit-тесты для core.llm.multimodal (W22)."""
from __future__ import annotations

import asyncio
import base64

import pytest
from backend.core.llm.multimodal import (
    MultimodalClient,
    MultimodalResponse,
    _png_to_data_url,
    _strip_code_fence,
)


def _run(coro):
    return asyncio.run(coro)


# === _png_to_data_url ==================================================

def test_png_to_data_url_basic() -> None:
    raw = b"\x89PNG\r\n\x1a\nfake"
    url = _png_to_data_url(raw)
    assert url.startswith("data:image/png;base64,")
    encoded = url.split(",", 1)[1]
    assert base64.b64decode(encoded) == raw


def test_png_to_data_url_too_large_raises() -> None:
    raw = b"x" * (6 * 1024 * 1024)   # 6MB > cap 5MB
    with pytest.raises(ValueError):
        _png_to_data_url(raw)


# === _strip_code_fence =================================================

def test_strip_no_fence() -> None:
    assert _strip_code_fence('{"x":1}') == '{"x":1}'


def test_strip_json_fence() -> None:
    text = '```json\n{"x":1}\n```'
    out = _strip_code_fence(text)
    assert out == '{"x":1}'


def test_strip_plain_fence() -> None:
    text = '```\n{"x":1}\n```'
    out = _strip_code_fence(text)
    assert out == '{"x":1}'


def test_strip_fence_with_trailing_text() -> None:
    """Если ``` без trailing — обрезаем только начало."""
    text = '```json\n{"x":1}'
    out = _strip_code_fence(text)
    assert "x" in out


# === MultimodalClient — config =========================================

def test_init_picks_up_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MULTIMODAL_BASE_URL", "http://vllm:8000/")
    monkeypatch.setenv("MULTIMODAL_MODEL", "Qwen/Qwen2-VL-7B-Instruct")
    monkeypatch.setenv("MULTIMODAL_API_KEY", "sk-test")
    c = MultimodalClient()
    assert c.base_url == "http://vllm:8000"  # trailing slash удалён
    assert c.model == "Qwen/Qwen2-VL-7B-Instruct"
    assert c.api_key == "sk-test"
    assert c.enabled is True


def test_init_falls_back_to_local_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MULTIMODAL_BASE_URL", raising=False)
    monkeypatch.delenv("MULTIMODAL_MODEL", raising=False)
    monkeypatch.setenv("LLM_LOCAL_BASE_URL", "http://local:8000")
    monkeypatch.setenv("LLM_LOCAL_MODEL", "qwen-vl")
    c = MultimodalClient()
    assert c.base_url == "http://local:8000"
    assert c.model == "qwen-vl"


def test_init_explicit_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MULTIMODAL_BASE_URL", "http://from-env:8000")
    c = MultimodalClient(base_url="http://override:8000", model="x")
    assert c.base_url == "http://override:8000"


def test_disabled_when_missing_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("MULTIMODAL_BASE_URL", "MULTIMODAL_MODEL",
                "LLM_LOCAL_BASE_URL", "LLM_LOCAL_MODEL"):
        monkeypatch.delenv(var, raising=False)
    c = MultimodalClient()
    assert c.enabled is False


# === judge() — без сети ================================================

def test_judge_disabled_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("MULTIMODAL_BASE_URL", "MULTIMODAL_MODEL",
                "LLM_LOCAL_BASE_URL", "LLM_LOCAL_MODEL"):
        monkeypatch.delenv(var, raising=False)
    c = MultimodalClient()
    out = _run(c.judge(images=[b"png"], user_prompt="hi"))
    assert out.success is False
    assert "not configured" in (out.error or "")


def test_judge_no_images_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MULTIMODAL_BASE_URL", "http://x")
    monkeypatch.setenv("MULTIMODAL_MODEL", "m")
    c = MultimodalClient()
    out = _run(c.judge(images=[], user_prompt="hi"))
    assert out.success is False
    assert "no images" in (out.error or "")


def test_judge_oversized_image_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MULTIMODAL_BASE_URL", "http://x")
    monkeypatch.setenv("MULTIMODAL_MODEL", "m")
    c = MultimodalClient()
    huge = b"x" * (6 * 1024 * 1024)
    out = _run(c.judge(images=[huge], user_prompt="hi"))
    assert out.success is False
    assert "too large" in (out.error or "")


# === MultimodalResponse =================================================

def test_response_to_dict_does_not_include_raw_text() -> None:
    """raw_text оставляем для debug, но to_dict — clean (без него)."""
    r = MultimodalResponse(success=True, content={"x": 1}, raw_text="foo")
    d = r.to_dict()
    assert "raw_text" not in d
    assert d["success"] is True
    assert d["content"] == {"x": 1}
