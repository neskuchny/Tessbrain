"""Multimodal LLM client (W22) — image + text → structured JSON.

Поддерживает OpenAI-compatible vision API (то же что и LocalLLMClient
из W8): vLLM с Qwen2-VL / Llama-3.2-Vision, OpenAI gpt-4o, Anthropic
claude-3-5-sonnet-vision (через LiteLLM proxy), и т.д.

Конфигурация:
- `MULTIMODAL_BASE_URL` — endpoint, default берёт из LLM_LOCAL_BASE_URL
- `MULTIMODAL_MODEL` — имя модели (e.g. Qwen/Qwen2-VL-7B-Instruct)
- `MULTIMODAL_API_KEY` — fallback на LLM_LOCAL_API_KEY
- В enterprise mode endpoint обязан быть internal (validator из W8)

API: `multimodal_judge(images, prompt, system_prompt) → dict`
- images: list[bytes] (PNG/JPEG); base64'ятся в data: URL
- temperature=0
- timeout 60s
- Никаких raises — failure → возвращаем {"error": ...}
"""
from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


_DEFAULT_TIMEOUT = 60.0
_MAX_IMAGE_BYTES = 5 * 1024 * 1024


def _resolve_base_url() -> str:
    return (
        os.environ.get("MULTIMODAL_BASE_URL")
        or os.environ.get("LLM_LOCAL_BASE_URL")
        or ""
    ).rstrip("/")


def _resolve_model() -> str:
    return (
        os.environ.get("MULTIMODAL_MODEL")
        or os.environ.get("LLM_LOCAL_MODEL")
        or ""
    )


def _resolve_api_key() -> Optional[str]:
    return (
        os.environ.get("MULTIMODAL_API_KEY")
        or os.environ.get("LLM_LOCAL_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    )


def _png_to_data_url(png_bytes: bytes) -> str:
    """bytes → data:image/png;base64,XXX (для OpenAI vision API)."""
    if len(png_bytes) > _MAX_IMAGE_BYTES:
        # Усечение бы делали через resize, но это потащит Pillow.
        # Лучше явно сообщить о проблеме.
        raise ValueError(
            f"image too large: {len(png_bytes)} bytes (cap {_MAX_IMAGE_BYTES})"
        )
    b64 = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{b64}"


@dataclass
class MultimodalResponse:
    success: bool
    content: Optional[dict[str, Any]] = None       # parsed JSON
    raw_text: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "content": self.content,
            "error": self.error,
        }


class MultimodalClient:
    """Multimodal LLM client поверх OpenAI-compatible vision API."""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> None:
        self.base_url = (base_url or _resolve_base_url()).rstrip("/")
        self.model = model or _resolve_model()
        self.api_key = api_key or _resolve_api_key()

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.model)

    def perimeter_violation(self) -> Optional[str]:
        """Причина, по которой этот endpoint нельзя звать в закрытом контуре.

        Дыра, найденная аудитом: докстринг модуля обещал «в enterprise mode
        endpoint обязан быть internal», но проверки не было НИГДЕ — ни в
        валидаторе Settings, ни в префлайте. То есть MULTIMODAL_BASE_URL,
        указанный на публичное облако, выносил за периметр СКРИНШОТЫ
        экранов компании — а это чувствительнее текста: на них реальный
        интерфейс с реальными данными.

        Fail-closed: сомнение (сбой самой проверки) трактуется как нарушение.
        """
        try:
            from backend.core.security.perimeter import (
                enterprise_mode_enabled,
                is_internal_url,
            )
            if not enterprise_mode_enabled():
                return None
            if not is_internal_url(self.base_url):
                return (f"закрытый контур: MULTIMODAL_BASE_URL={self.base_url!r} "
                        "ведёт за периметр — скриншоты наружу не отправляются")
            return None
        except Exception as exc:
            return f"проверка периметра не выполнена ({exc})"

    async def judge(
        self,
        *,
        images: list[bytes],
        user_prompt: str,
        system_prompt: str = "",
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> MultimodalResponse:
        """Отправить картинки + prompt и получить JSON ответ.

        Никогда не raises — возвращает MultimodalResponse(success=False)
        с error.
        """
        _violation = self.perimeter_violation()
        if _violation:
            logger.error("[air-gap] multimodal call blocked: %s", _violation)
            return MultimodalResponse(success=False, error=_violation)
        if not self.enabled:
            return MultimodalResponse(
                success=False,
                error="multimodal client not configured (MULTIMODAL_BASE_URL/MODEL)",
            )

        if not images:
            return MultimodalResponse(success=False, error="no images supplied")

        try:
            data_urls = [_png_to_data_url(img) for img in images]
        except ValueError as exc:
            return MultimodalResponse(success=False, error=str(exc))

        try:
            import httpx
        except ImportError:
            return MultimodalResponse(success=False, error="httpx not installed")

        # OpenAI Chat Completions с vision content-parts.
        content_parts: list[dict[str, Any]] = [
            {"type": "text", "text": user_prompt},
        ]
        for url in data_urls:
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": url},
            })

        messages: list[dict[str, Any]] = []
        if system_prompt.strip():
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": content_parts})

        body = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": 1500,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        url = f"{self.base_url}/v1/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            return MultimodalResponse(
                success=False, error=f"network error: {exc}",
            )

        if resp.status_code >= 400:
            return MultimodalResponse(
                success=False,
                error=f"HTTP {resp.status_code}: {resp.text[:300]}",
            )

        try:
            data = resp.json()
        except Exception as exc:
            return MultimodalResponse(success=False, error=f"invalid JSON: {exc}")

        # OpenAI shape: choices[0].message.content
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return MultimodalResponse(
                success=False, error="unexpected response shape",
                raw_text=str(data)[:500],
            )

        text = (text or "").strip()
        # Парсим JSON; если модель обернула в ```json — вытаскиваем.
        json_text = _strip_code_fence(text)
        try:
            parsed = json.loads(json_text)
        except json.JSONDecodeError:
            return MultimodalResponse(
                success=True,    # модель ответила, но не JSON
                content=None,
                raw_text=text,
                error="response is not valid JSON",
            )

        return MultimodalResponse(success=True, content=parsed, raw_text=text)


def _strip_code_fence(text: str) -> str:
    """Убрать ```json … ``` обёртку если есть."""
    s = text.strip()
    if s.startswith("```"):
        # снимаем первую строку (```json или ```) и финальные ```
        lines = s.split("\n")
        if len(lines) >= 2:
            inner = "\n".join(lines[1:])
            if inner.rstrip().endswith("```"):
                inner = inner.rstrip()[:-3]
            return inner.strip()
    return s


__all__ = ["MultimodalClient", "MultimodalResponse"]
