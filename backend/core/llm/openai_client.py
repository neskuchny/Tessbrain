# -*- coding: utf-8 -*-
"""
OpenAI LLM клиент (GPT-4, GPT-4o, etc.)
"""
import asyncio
import logging
import os
from typing import Any, Dict, List, Optional, Union

from .base import BaseLLMClient

logger = logging.getLogger(__name__)

# Ленивый импорт openai
_openai = None


def _get_openai():
    """Ленивый импорт OpenAI SDK"""
    global _openai
    if _openai is None:
        try:
            import openai
            _openai = openai
        except ImportError:
            logger.warning("openai не установлен. pip install openai")
            _openai = False
    return _openai if _openai else None


class OpenAIClient(BaseLLMClient):
    """
    OpenAI LLM клиент.

    Поддерживает GPT-4, GPT-4o, GPT-4o-mini и другие модели.

    Pricing (per 1M tokens, примерные):
    - GPT-4o: Input $2.50, Output $10.00
    - GPT-4o-mini: Input $0.15, Output $0.60
    - GPT-4-turbo: Input $10.00, Output $30.00
    """

    # Pricing per 1M tokens
    PRICING = {
        "gpt-4o": {"input": 2.50, "output": 10.00},
        "gpt-4o-mini": {"input": 0.15, "output": 0.60},
        "gpt-4-turbo": {"input": 10.00, "output": 30.00},
        "gpt-4": {"input": 30.00, "output": 60.00},
        "gpt-3.5-turbo": {"input": 0.50, "output": 1.50},
    }

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        api_key: Optional[str] = None
    ):
        """
        Args:
            model: Модель OpenAI (gpt-4o, gpt-4o-mini, etc.)
            api_key: API ключ (если не указан, берется из OPENAI_API_KEY)
        """
        api_key = api_key or os.environ.get("OPENAI_API_KEY")
        super().__init__(model=model, api_key=api_key)

        # Инициализация SDK
        openai = _get_openai()
        if openai and self.api_key:
            try:
                self._client = openai.AsyncOpenAI(api_key=self.api_key)
                self.enabled = True
                logger.info(f"✅ OpenAIClient initialized: model={model}")
            except Exception as e:
                logger.warning(f"⚠️ Failed to configure OpenAI API: {e}")
                self.enabled = False
        else:
            logger.warning("⚠️ OpenAIClient disabled (missing SDK or API key)")
            self._client = None

    def _get_pricing(self) -> Dict[str, float]:
        """Получить pricing для текущей модели"""
        for model_prefix, pricing in self.PRICING.items():
            if self.model.startswith(model_prefix):
                return pricing
        # Default pricing
        return {"input": 1.00, "output": 3.00}

    def _calculate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Расчет стоимости запроса"""
        pricing = self._get_pricing()
        input_cost = (input_tokens / 1_000_000) * pricing["input"]
        output_cost = (output_tokens / 1_000_000) * pricing["output"]
        return input_cost + output_cost

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs
    ) -> str:
        """Генерация текста"""
        if not self.enabled or not self._client:
            raise RuntimeError("OpenAIClient is not enabled")

        self.stats["total_requests"] += 1

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        input_tokens = self.estimate_tokens(prompt + (system_prompt or ""))

        try:
            response = await self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs
            )

            result = response.choices[0].message.content or ""

            # Обновляем статистику
            output_tokens = self.estimate_tokens(result)
            if response.usage:
                input_tokens = response.usage.prompt_tokens
                output_tokens = response.usage.completion_tokens

            self.stats["total_input_tokens"] += input_tokens
            self.stats["output_tokens"] += output_tokens

            cost = self._calculate_cost(input_tokens, output_tokens)
            self.stats["estimated_cost"] += cost
            self.stats["estimated_cost_without_cache"] += cost

            return result

        except Exception as e:
            self.stats["errors"] += 1
            logger.error(f"❌ OpenAI API error: {e}")
            raise

    async def generate_json(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        max_retries: int = 2,
        **kwargs
    ) -> Union[Dict[str, Any], List[Any]]:
        """Генерация JSON с автоматическим парсингом"""
        if not self.enabled or not self._client:
            raise RuntimeError("OpenAIClient is not enabled")

        self.stats["total_requests"] += 1

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        input_tokens = self.estimate_tokens(prompt + (system_prompt or ""))

        last_error: Optional[Exception] = None

        for attempt in range(max_retries + 1):
            try:
                response = await self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format={"type": "json_object"},
                    **kwargs
                )

                text = response.choices[0].message.content or ""

                # Обновляем статистику
                output_tokens = self.estimate_tokens(text)
                if response.usage:
                    input_tokens = response.usage.prompt_tokens
                    output_tokens = response.usage.completion_tokens

                self.stats["total_input_tokens"] += input_tokens
                self.stats["output_tokens"] += output_tokens

                cost = self._calculate_cost(input_tokens, output_tokens)
                self.stats["estimated_cost"] += cost
                self.stats["estimated_cost_without_cache"] += cost

                # Парсинг JSON
                data = self.extract_json(text)
                if data is None:
                    logger.debug("Failed to parse JSON, returning empty dict")
                    data = {}

                return data

            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    logger.warning(f"⚠️ Attempt {attempt + 1} failed: {e}, retrying...")
                    await asyncio.sleep(1)

        self.stats["errors"] += 1
        raise RuntimeError(f"Failed after {max_retries + 1} attempts: {last_error}")


# Singleton для глобального использования
_global_client: Optional[OpenAIClient] = None


def get_openai_client(
    model: str = "gpt-4o-mini",
    api_key: Optional[str] = None
) -> OpenAIClient:
    """Получить глобальный экземпляр OpenAIClient"""
    global _global_client
    if _global_client is None:
        _global_client = OpenAIClient(model=model, api_key=api_key)
    return _global_client


def reset_global_client():
    """Сбросить глобальный клиент"""
    global _global_client
    _global_client = None



