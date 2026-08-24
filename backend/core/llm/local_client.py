# -*- coding: utf-8 -*-
"""
Local LLM client (W8 enterprise/on-prem).

Подключается к OpenAI-compatible HTTP-эндпоинту: vLLM, Ollama, TGI,
LocalAI, LM Studio. Все они держат `POST /v1/chat/completions` с тем же
форматом messages/temperature/max_tokens, что и OpenAI.

Зачем отдельный клиент, а не просто `OpenAIClient(base_url=...)`:
1. Pricing — для локальных моделей цена 0 (compute already paid).
2. response_format={"type": "json_object"} — Ollama/llama.cpp его не
   поддерживают; пытаемся, но падаем мягко на extract_json().
3. Логи и статистика чётко маркируют local-вызовы (важно для аудита
   в air-gap режиме).

Конфигурация через env:
- LLM_LOCAL_BASE_URL  — http://vllm:8000/v1, http://ollama:11434/v1
- LLM_LOCAL_MODEL     — llama-3.3-70b-instruct, qwen2.5:32b, ...
- LLM_LOCAL_API_KEY   — (опционально) для proxy/gateway с auth
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional, Union

from .base import BaseLLMClient

logger = logging.getLogger(__name__)

_openai = None


def _get_openai():
    global _openai
    if _openai is None:
        try:
            import openai
            _openai = openai
        except ImportError:
            logger.warning("openai SDK not installed — LocalLLMClient disabled")
            _openai = False
    return _openai if _openai else None


class LocalLLMClient(BaseLLMClient):
    """OpenAI-compatible клиент для локальных серверов (vLLM/Ollama/TGI).

    В отличие от `OpenAIClient`:
    - `base_url` обязателен (это и есть смысл клиента),
    - pricing = 0 (себестоимость уже оплачена железом),
    - в `generate_json` не используем `response_format=json_object` (ломает Ollama).
    """

    def __init__(
        self,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> None:
        model = model or os.environ.get("LLM_LOCAL_MODEL", "")
        self.base_url = base_url or os.environ.get("LLM_LOCAL_BASE_URL", "")
        # Многие локальные серверы не требуют ключа — но OpenAI SDK падает
        # без api_key. Передаём пустой токен для совместимости.
        api_key = api_key or os.environ.get("LLM_LOCAL_API_KEY", "EMPTY")
        super().__init__(model=model, api_key=api_key)

        if not self.base_url or not model:
            logger.warning(
                "LocalLLMClient disabled: LLM_LOCAL_BASE_URL=%r, model=%r",
                self.base_url, model,
            )
            self._client = None
            return

        openai = _get_openai()
        if not openai:
            self._client = None
            return

        try:
            self._client = openai.AsyncOpenAI(
                api_key=self.api_key or "EMPTY",
                base_url=self.base_url,
            )
            self.enabled = True
            logger.info(
                "[OK] LocalLLMClient initialized: model=%s base_url=%s",
                model, self.base_url,
            )
        except Exception as e:
            logger.warning("Failed to init LocalLLMClient: %s", e)
            self._client = None

    def _calculate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Local inference — нулевая marginal cost для биллинга."""
        return 0.0

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs,
    ) -> str:
        if not self.enabled or not self._client:
            raise RuntimeError("LocalLLMClient is not enabled")

        self.stats["total_requests"] += 1

        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        input_tokens = self.estimate_tokens(prompt + (system_prompt or ""))
        # Роутер передаёт model= (для tier-выбора). Забираем из kwargs, чтобы
        # НЕ было дубля с model= ниже (это роняло КАЖДЫЙ локальный вызов).
        # Плейсхолдер "local"/пусто → фактическая модель клиента.
        _model = kwargs.pop("model", None) or self.model
        if _model in ("local", "", None):
            _model = self.model

        try:
            response = await self._client.chat.completions.create(
                model=_model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            )
            result = response.choices[0].message.content or ""

            output_tokens = self.estimate_tokens(result)
            if getattr(response, "usage", None):
                input_tokens = response.usage.prompt_tokens
                output_tokens = response.usage.completion_tokens

            self.stats["total_input_tokens"] += input_tokens
            self.stats["output_tokens"] += output_tokens
            return result

        except Exception as e:
            self.stats["errors"] += 1
            logger.error("Local LLM error: %s", e)
            raise

    async def generate_json(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        max_retries: int = 2,
        **kwargs,
    ) -> Union[Dict[str, Any], List[Any]]:
        if not self.enabled or not self._client:
            raise RuntimeError("LocalLLMClient is not enabled")

        self.stats["total_requests"] += 1

        # Подсказка модели вернуть JSON — компенсация отсутствующего
        # response_format на Ollama/llama.cpp серверах.
        json_hint = (
            "\n\nReturn ONLY a valid JSON object. "
            "No prose, no markdown fences."
        )
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt + json_hint})

        input_tokens = self.estimate_tokens(prompt + (system_prompt or ""))
        last_error: Optional[Exception] = None
        _model = kwargs.pop("model", None) or self.model
        if _model in ("local", "", None):
            _model = self.model

        for attempt in range(max_retries + 1):
            try:
                response = await self._client.chat.completions.create(
                    model=_model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    **kwargs,
                )
                text = response.choices[0].message.content or ""

                output_tokens = self.estimate_tokens(text)
                if getattr(response, "usage", None):
                    input_tokens = response.usage.prompt_tokens
                    output_tokens = response.usage.completion_tokens

                self.stats["total_input_tokens"] += input_tokens
                self.stats["output_tokens"] += output_tokens

                data = self.extract_json(text)
                if data is None:
                    logger.debug("Local LLM: JSON parse failed, returning {}")
                    data = {}
                return data

            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    logger.warning(
                        "Local LLM attempt %d failed: %s, retrying...",
                        attempt + 1, e,
                    )
                    await asyncio.sleep(1)

        self.stats["errors"] += 1
        raise RuntimeError(
            f"Local LLM failed after {max_retries + 1} attempts: {last_error}"
        )
