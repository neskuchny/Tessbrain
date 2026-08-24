"""W42a: TurboQuant LLM client.

TurboQuant — наш fork с квантизацией (int4/int8) и кастомным API:
- Endpoint: POST {base_url}/v1/chat/completions
- Auth: Authorization: Bearer dev-token-...
- Body: {profile_id, messages, max_tokens, temperature, top_p}
  (profile_id передаётся ВМЕСТО model)
- Response: {"response": {choices: [...], usage: {...}}} — двойная упаковка,
  поэтому AsyncOpenAI SDK напрямую не работает (он ждёт choices на корне).

См. reference: turboquant_research/test_inference.py + meeting_bot_async.py
из meetflow_local — `_turboquant_unwrap_chat_completion_body`.

Этот client совместим с интерфейсом OpenAIClient (generate / generate_json),
чтобы LLMRouter мог использовать его как drop-in replacement когда
LLMProfile.provider == "turboquant".
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 300.0
DEFAULT_MAX_TOKENS = 2048
DEFAULT_TEMPERATURE = 0.3
DEFAULT_TOP_P = 0.95


@dataclass
class TurboQuantUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


def _unwrap_response(raw: Any) -> dict[str, Any]:
    """TurboQuant отдаёт {"response": {choices, usage}}.

    Если корень уже choices/usage — возвращаем как есть (defensive).
    """
    if not isinstance(raw, dict):
        return {}
    inner = raw.get("response", raw)
    return inner if isinstance(inner, dict) else {}


def _extract_message_content(message: Any) -> str:
    """OpenAI message может быть str или list[dict] с text-частями."""
    if isinstance(message, dict):
        content = message.get("content", "")
    else:
        content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(p for p in parts if p).strip()
    return str(content or "")


class TurboQuantClient:
    """Async client для TurboQuant inference API.

    Совместим с OpenAIClient interface для drop-in использования в LLMRouter.
    """

    def __init__(
        self,
        *,
        base_url: str,
        profile_id: str,
        api_key: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not base_url:
            raise ValueError("base_url required for TurboQuantClient")
        if not profile_id:
            raise ValueError("profile_id required for TurboQuantClient")
        self.base_url = base_url.rstrip("/")
        self.profile_id = profile_id
        self.api_key = api_key
        self.timeout = float(timeout)

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url}/v1/chat/completions"

    @property
    def health_url(self) -> str:
        return f"{self.base_url}/health/live"

    @property
    def models_url(self) -> str:
        return f"{self.base_url}/v1/models"

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        top_p: float = DEFAULT_TOP_P,
        presence_penalty: float = 0.0,
        **_ignored: Any,
    ) -> str:
        """Returns plain-text content. Совместим с OpenAIClient.generate signature."""
        try:
            import httpx
        except ImportError as exc:
            raise RuntimeError(f"httpx required: {exc}")

        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        body: dict[str, Any] = {
            "profile_id": self.profile_id,
            "model": self.profile_id,   # TurboQuant игнорирует, но MeetFlow передаёт оба
            "messages": messages,
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
            "top_p": float(top_p),
        }
        if presence_penalty != 0.0:
            body["presence_penalty"] = float(presence_penalty)

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    self.chat_completions_url,
                    headers=self._headers(),
                    json=body,
                )
                resp.raise_for_status()
                raw = resp.json()
        except Exception as exc:
            logger.warning("TurboQuantClient.generate failed: %s", exc)
            raise

        inner = _unwrap_response(raw)
        choices = inner.get("choices") or []
        if not choices:
            return ""
        first = choices[0] if isinstance(choices[0], dict) else {}
        msg = first.get("message")
        if not isinstance(msg, dict):
            return ""
        return _extract_message_content(msg)

    async def generate_json(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = 0.0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Same as generate but parses response as JSON.

        TurboQuant doesn't have native response_format=json_object — мы просим
        модель в system prompt'е и парсим. Толерантно к code-fence wrapper'у.
        """
        text = await self.generate(
            prompt=prompt, system_prompt=system_prompt,
            max_tokens=max_tokens, temperature=temperature, **kwargs,
        )
        return _parse_json_tolerantly(text)

    async def get_usage(self, raw_response: dict[str, Any]) -> TurboQuantUsage:
        """Extract usage from raw API response (для metrics)."""
        inner = _unwrap_response(raw_response)
        usage = inner.get("usage") if isinstance(inner.get("usage"), dict) else {}
        prompt_tok = int(usage.get("prompt_tokens", 0) or 0)
        completion_tok = int(usage.get("completion_tokens", 0) or 0)
        return TurboQuantUsage(
            prompt_tokens=prompt_tok,
            completion_tokens=completion_tok,
            total_tokens=prompt_tok + completion_tok,
        )

    async def health_check(self) -> tuple[bool, Optional[str]]:
        """GET /health/live — быстрая проверка без trafic токенов.

        Returns: (ok, error_message).
        """
        try:
            import httpx
        except ImportError as exc:
            return False, str(exc)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    self.health_url, headers=self._headers(),
                )
            if resp.status_code == 200:
                return True, None
            return False, f"http {resp.status_code}: {resp.text[:200]}"
        except Exception as exc:
            return False, str(exc)

    async def list_models(self) -> list[dict[str, Any]]:
        """GET /v1/models — для UI / debug."""
        try:
            import httpx
        except ImportError:
            return []
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    self.models_url, headers=self._headers(),
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.debug("TurboQuantClient.list_models failed: %s", exc)
            return []
        items = data.get("data") if isinstance(data, dict) else None
        return items if isinstance(items, list) else []


def _parse_json_tolerantly(text: str) -> dict[str, Any]:
    """Parse JSON-string toleating ```json fences."""
    if not text:
        return {}
    s = text.strip()
    # Strip ``` fence if present.
    if s.startswith("```"):
        # Remove first line + last ```
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        s = "\n".join(lines)
    try:
        out = json.loads(s)
    except Exception:
        return {}
    return out if isinstance(out, dict) else {}


# === Helper: build client from LLMProfile ==================================


def build_turboquant_client_from_profile(
    *,
    base_url: str,
    profile_id_or_model: str,
    api_key: Optional[str],
    config: Optional[dict[str, Any]] = None,
) -> TurboQuantClient:
    """Factory из полей LLMProfile (см. backend/core/llm/profiles.py).

    `profile_id_or_model` — это поле `model` из LLMProfile (там хранится
    profile_id для TurboQuant, например "gemma4_e4b_it_w4a16" или
    "turboquant_research").
    """
    cfg = config or {}
    return TurboQuantClient(
        base_url=base_url,
        profile_id=profile_id_or_model,
        api_key=api_key,
        timeout=float(cfg.get("timeout", DEFAULT_TIMEOUT_SECONDS)),
    )


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_TEMPERATURE",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_TOP_P",
    "TurboQuantClient",
    "TurboQuantUsage",
    "build_turboquant_client_from_profile",
]


async def _self_test() -> None:
    """Smoke test against fake server (не bound к prod)."""
    pass
