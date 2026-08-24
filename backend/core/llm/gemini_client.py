# -*- coding: utf-8 -*-
"""
Gemini LLM клиент с поддержкой кеширования контекста.
Адаптировано из tess_old/module_01_nlp_analysis/cached_llm_client.py
"""
import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional, Union

from .adaptive import is_rate_limit_error
from .base import BaseLLMClient
from .usage_tracker import get_usage_context, track_usage

logger = logging.getLogger(__name__)


def _get_llm_context():
    """Получить контекст из router и usage_tracker"""
    context = {"user_id": None, "session_id": None, "agent_mode": None, "request_type": None, "model_tier": None}

    # Сначала пробуем глобальный контекст из usage_tracker
    usage_ctx = get_usage_context()
    if usage_ctx:
        context.update(usage_ctx)

    # Затем пробуем контекст из router (может перезаписать)
    try:
        from .router import get_llm_context
        router_ctx = get_llm_context()
        for k, v in router_ctx.items():
            if v is not None:
                context[k] = v
    except ImportError:
        logger.debug("suppressed exception", exc_info=True)

    return context


# Модели по tier
TIER_MODELS = {
    "standard": "gemini-flash-lite-latest",
    "premium": "gemini-flash-latest"  # Новейшая модель Gemini 3 Flash (preview)
}


def _get_model_for_tier(model: Optional[str], ctx: Dict[str, Any]) -> str:
    """Выбрать модель на основе контекста model_tier"""
    # Если модель явно указана, используем её
    if model:
        return model

    # Если есть model_tier в контексте, выбираем соответствующую модель
    model_tier = ctx.get("model_tier")
    if model_tier and model_tier in TIER_MODELS:
        return TIER_MODELS[model_tier]

    # По умолчанию - стандартная модель
    return TIER_MODELS["standard"]

# Ленивый импорт google.generativeai
_genai = None


def _get_genai():
    """Ленивый импорт Gemini SDK"""
    global _genai
    if _genai is None:
        try:
            import google.generativeai as genai
            _genai = genai
        except ImportError:
            logger.warning("google-generativeai не установлен. pip install google-generativeai")
            _genai = False
    return _genai if _genai else None


def _real_usage(response) -> Optional[Dict[str, int]]:
    """Реальные токены из ответа API (usage_metadata), а не оценка по символам.

    Критично для честного биллинга: у Gemini 2.5 thinking-токены биллятся
    как output, но в тексте ответа их не видно — оценка len//2 их полностью
    теряла (плюс занижала русский input). Возвращает None, если метаданных
    нет (тогда caller падает обратно на estimate)."""
    try:
        um = getattr(response, "usage_metadata", None)
        if um is None:
            return None
        prompt = int(getattr(um, "prompt_token_count", 0) or 0)
        candidates = int(getattr(um, "candidates_token_count", 0) or 0)
        thoughts = int(getattr(um, "thoughts_token_count", 0) or 0)
        cached = int(getattr(um, "cached_content_token_count", 0) or 0)
        if prompt <= 0 and candidates <= 0:
            return None
        return {
            "input": max(0, prompt - cached),
            "output": candidates + thoughts,   # thinking биллится как output
            "cached": cached,
        }
    except Exception:
        return None


class GeminiClient(BaseLLMClient):
    """
    Gemini LLM клиент с поддержкой Context Caching.

    Context Caching позволяет экономить до 90% на повторных запросах
    с одним и тем же контекстом (например, транскриптом встречи).

    Pricing (Gemini 2.5 Flash-Lite per 1M tokens, актуально на 2026):
    - Input: $0.10
    - Cached input: $0.025 (75% discount)
    - Output: $0.40 (thinking-токены биллятся как output!)
    """

    # Pricing per 1M tokens
    INPUT_PRICE = 0.10
    CACHED_PRICE = 0.025
    OUTPUT_PRICE = 0.40

    def __init__(
        self,
        model: str = "gemini-flash-lite-latest",
        api_key: Optional[str] = None,
        context_for_caching: Optional[str] = None
    ):
        """
        Args:
            model: Модель Gemini (gemini-flash-lite-latest, gemini-1.5-pro, etc.)
            api_key: API ключ (если не указан, берется из GOOGLE_API_KEY или GEMINI_API_KEY)
            context_for_caching: Контекст для кеширования (например, транскрипт)
        """
        api_key = api_key or os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
        super().__init__(model=model, api_key=api_key)

        self.context_for_caching = context_for_caching
        self._cached_context_tokens = 0

        # Инициализация SDK
        genai = _get_genai()
        if genai and self.api_key:
            try:
                genai.configure(api_key=self.api_key)
                self.enabled = True
                logger.info(f"[OK] GeminiClient initialized: model={model}, caching={'enabled' if context_for_caching else 'disabled'}")
            except Exception as e:
                logger.warning(f"[Warning] Failed to configure Gemini API: {e}")
                self.enabled = False
        else:
            logger.warning("[Warning] GeminiClient disabled (missing SDK or API key)")

    def set_context_for_caching(self, context: str):
        """Установить контекст для кеширования"""
        self.context_for_caching = context
        self._cached_context_tokens = self.estimate_tokens(context)
        # Снимок кумулятивных счётчиков — чтобы в clear_cache() посчитать
        # ЭФФЕКТИВНОСТЬ кеша именно за эту встречу (замер экономии).
        self._cache_snapshot = {
            "input": self.stats.get("total_input_tokens", 0),
            "cached": self.stats.get("cached_input_tokens", 0),
            "cost": self.stats.get("estimated_cost", 0.0),
            "cost_wo": self.stats.get("estimated_cost_without_cache", 0.0),
        }
        logger.info(f"📦 Context set for caching: {self._cached_context_tokens} tokens")

    def clear_cache(self):
        """Очистить кешированный контекст + залогировать эффективность кеша."""
        snap = getattr(self, "_cache_snapshot", None)
        if snap:
            d_in = self.stats.get("total_input_tokens", 0) - snap["input"]
            d_cached = self.stats.get("cached_input_tokens", 0) - snap["cached"]
            d_cost = self.stats.get("estimated_cost", 0.0) - snap["cost"]
            d_cost_wo = self.stats.get("estimated_cost_without_cache", 0.0) - snap["cost_wo"]
            total_ctx = d_in + d_cached
            hit = (d_cached / total_ctx * 100.0) if total_ctx else 0.0
            saved = d_cost_wo - d_cost
            # d_cost_wo — сколько стоило бы БЕЗ кеша; saved — реальная экономия;
            # low hit% при большом d_in = потенциал (транскрипт бьётся по полной).
            logger.info(
                "📊 Кеш встречи: вход %d ток (из них закешировано %d = %.0f%%); "
                "стоимость $%.4f (без кеша было бы $%.4f, сэкономлено $%.4f)",
                total_ctx, d_cached, hit, d_cost, d_cost_wo, saved)
            self._cache_snapshot = None
        self.context_for_caching = None
        self._cached_context_tokens = 0

    def _calculate_cost(
        self,
        input_tokens: int,
        cached_tokens: int,
        output_tokens: int
    ) -> tuple[float, float]:
        """
        Расчет стоимости запроса.
        Returns: (actual_cost, cost_without_cache)
        """
        input_cost = (input_tokens / 1_000_000) * self.INPUT_PRICE
        cached_cost = (cached_tokens / 1_000_000) * self.CACHED_PRICE
        output_cost = (output_tokens / 1_000_000) * self.OUTPUT_PRICE

        actual_cost = input_cost + cached_cost + output_cost
        cost_without_cache = ((input_tokens + cached_tokens) / 1_000_000) * self.INPUT_PRICE + output_cost

        return actual_cost, cost_without_cache

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 8192,
        use_cache: bool = True,
        model: Optional[str] = None,
        **kwargs
    ) -> str:
        """Генерация текста с backoff-ретраем на rate-limit (429/перегрузка).

        Сама генерация — в _generate_once(); здесь только повтор при
        rate-limit/перегрузке провайдера с экспоненциальным backoff, чтобы
        одиночные агенты не падали при транзиентном 429. Прочие ошибки
        (auth, bad request) пробрасываются сразу.
        """
        _max_rate_retries = 4
        for _attempt in range(_max_rate_retries + 1):
            try:
                return await self._generate_once(
                    prompt,
                    system_prompt=system_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    use_cache=use_cache,
                    model=model,
                    **kwargs,
                )
            except Exception as exc:
                if is_rate_limit_error(exc) and _attempt < _max_rate_retries:
                    delay = min(8.0, 1.0 * (2 ** _attempt))
                    logger.warning(
                        f"[RateLimit] Gemini 429/перегрузка, попытка "
                        f"{_attempt + 1}/{_max_rate_retries}, жду {delay:.1f}s"
                    )
                    await asyncio.sleep(delay)
                    continue
                raise

    async def _generate_once(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 8192,
        use_cache: bool = True,
        model: Optional[str] = None,
        **kwargs
    ) -> str:
        """
        Генерация текста.

        Args:
            prompt: Промпт
            system_prompt: Системный промпт
            temperature: Температура генерации
            max_tokens: Максимум токенов в ответе
            use_cache: Использовать кешированный контекст
            model: Модель для использования (если не указана, используется self.model)
        """
        if not self.enabled:
            raise RuntimeError("GeminiClient is not enabled")

        genai = _get_genai()
        if not genai:
            raise RuntimeError("google-generativeai not available")

        # Получаем контекст для выбора модели по tier
        ctx = _get_llm_context()

        # Используем переданную модель, или модель из контекста tier, или дефолтную
        model_to_use = model or _get_model_for_tier(None, ctx) or self.model

        self.stats["total_requests"] += 1

        # Формируем полный промпт с кешированным контекстом
        full_prompt = prompt
        cached_tokens = 0

        if use_cache and self.context_for_caching:
            full_prompt = f"CONTEXT:\n{self.context_for_caching}\n\n{prompt}"
            # Начиная со второго запроса, контекст кешируется
            if self.stats["total_requests"] > 1:
                cached_tokens = self._cached_context_tokens
                self.stats["cached_requests"] += 1

        input_tokens = self.estimate_tokens(full_prompt) - cached_tokens
        start_time = time.time()

        try:
            def _call_sync():
                # system_instruction передаётся в конструктор модели
                model_kwargs = {"model_name": model_to_use}
                if system_prompt:
                    model_kwargs["system_instruction"] = system_prompt

                model_instance = genai.GenerativeModel(**model_kwargs)

                generation_config = {
                    "temperature": temperature,
                    "top_p": 0.95,
                    "top_k": 40,
                    "max_output_tokens": max_tokens,
                }

                safety_settings = [
                    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
                ]

                contents = [{"role": "user", "parts": [{"text": full_prompt}]}]

                kwargs_call = {
                    "contents": contents,
                    "generation_config": generation_config,
                    "safety_settings": safety_settings
                }

                response = model_instance.generate_content(**kwargs_call)

                # Извлечение текста
                text = getattr(response, "text", None)
                if not text and hasattr(response, "candidates"):
                    try:
                        text = response.candidates[0].content.parts[0].text
                    except (IndexError, AttributeError):
                        text = ""

                return text or "", _real_usage(response)

            result, real_usage = await asyncio.to_thread(_call_sync)

            # Обновляем статистику: реальные токены из API, оценка — только
            # как fallback (у старых SDK usage_metadata может отсутствовать)
            if real_usage:
                input_tokens = real_usage["input"]
                output_tokens = real_usage["output"]
                cached_tokens = real_usage["cached"]
            else:
                output_tokens = self.estimate_tokens(result)
            self.stats["total_input_tokens"] += input_tokens
            self.stats["cached_input_tokens"] += cached_tokens
            self.stats["output_tokens"] += output_tokens

            actual_cost, cost_without_cache = self._calculate_cost(
                input_tokens, cached_tokens, output_tokens
            )
            self.stats["estimated_cost"] += actual_cost
            self.stats["estimated_cost_without_cache"] += cost_without_cache

            # Трекинг использования
            latency_ms = int((time.time() - start_time) * 1000)
            ctx = _get_llm_context()
            track_usage(
                provider="gemini",
                model=model_to_use,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens,
                request_type=ctx.get("request_type") or "generate",
                latency_ms=latency_ms,
                user_id=ctx.get("user_id"),
                session_id=ctx.get("session_id"),
                agent_mode=ctx.get("agent_mode"),
            )

            return result

        except Exception as e:
            self.stats["errors"] += 1
            latency_ms = int((time.time() - start_time) * 1000)
            # Трекинг ошибки
            ctx = _get_llm_context()
            track_usage(
                provider="gemini",
                model=model_to_use,
                input_tokens=input_tokens,
                output_tokens=0,
                cached_tokens=cached_tokens,
                request_type=ctx.get("request_type") or "generate",
                success=False,
                error=str(e),
                latency_ms=latency_ms,
                user_id=ctx.get("user_id"),
                session_id=ctx.get("session_id"),
                agent_mode=ctx.get("agent_mode"),
            )
            logger.error(f"[Error] Gemini API error: {e}")
            raise

    async def generate_json(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 8192,
        use_cache: bool = True,
        max_retries: int = 2,
        model: Optional[str] = None,
        **kwargs
    ) -> Union[Dict[str, Any], List[Any]]:
        """
        Генерация JSON с автоматическим парсингом.

        Args:
            prompt: Промпт
            system_prompt: Системный промпт
            temperature: Температура генерации
            max_tokens: Максимум токенов в ответе
            use_cache: Использовать кешированный контекст
            max_retries: Количество попыток при ошибке
            model: Модель для использования (если не указана, используется self.model)
        """
        if not self.enabled:
            raise RuntimeError("GeminiClient is not enabled")

        genai = _get_genai()
        if not genai:
            raise RuntimeError("google-generativeai not available")

        # Получаем контекст для выбора модели по tier
        ctx = _get_llm_context()

        # Используем переданную модель, или модель из контекста tier, или дефолтную
        model_to_use = model or _get_model_for_tier(None, ctx) or self.model

        self.stats["total_requests"] += 1

        # Формируем полный промпт с кешированным контекстом
        full_prompt = prompt
        cached_tokens = 0

        if use_cache and self.context_for_caching:
            full_prompt = f"CONTEXT:\n{self.context_for_caching}\n\n{prompt}"
            if self.stats["total_requests"] > 1:
                cached_tokens = self._cached_context_tokens
                self.stats["cached_requests"] += 1

        input_tokens = self.estimate_tokens(full_prompt) - cached_tokens
        start_time = time.time()

        last_error: Optional[Exception] = None

        for attempt in range(max_retries + 1):
            try:
                def _call_sync():
                    # system_instruction передаётся в конструктор модели
                    model_kwargs = {"model_name": model_to_use}
                    if system_prompt:
                        model_kwargs["system_instruction"] = system_prompt

                    model_instance = genai.GenerativeModel(**model_kwargs)

                    generation_config = {
                        "temperature": temperature,
                        "top_p": 0.95,
                        "top_k": 40,
                        "max_output_tokens": max_tokens,
                        "response_mime_type": "application/json"  # Запрашиваем JSON
                    }

                    safety_settings = [
                        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
                    ]

                    contents = [{"role": "user", "parts": [{"text": full_prompt}]}]

                    kwargs_call = {
                        "contents": contents,
                        "generation_config": generation_config,
                        "safety_settings": safety_settings
                    }

                    response = model_instance.generate_content(**kwargs_call)

                    # Извлечение текста
                    text = getattr(response, "text", None)
                    if not text and hasattr(response, "candidates"):
                        try:
                            text = response.candidates[0].content.parts[0].text
                        except (IndexError, AttributeError):
                            text = ""

                    if not text:
                        raise ValueError("Empty response from Gemini")

                    # Парсинг JSON
                    data = self.extract_json(text)
                    if data is None:
                        # Возвращаем пустой dict вместо ошибки
                        logger.debug("Failed to parse JSON, returning empty dict")
                        data = {}

                    return data, _real_usage(response)

                result, real_usage = await asyncio.to_thread(_call_sync)

                # Обновляем статистику: реальные токены из API, оценка — fallback
                if real_usage:
                    input_tokens = real_usage["input"]
                    output_tokens = real_usage["output"]
                    cached_tokens = real_usage["cached"]
                else:
                    output_tokens = self.estimate_tokens(str(result))
                self.stats["total_input_tokens"] += input_tokens
                self.stats["cached_input_tokens"] += cached_tokens
                self.stats["output_tokens"] += output_tokens

                actual_cost, cost_without_cache = self._calculate_cost(
                    input_tokens, cached_tokens, output_tokens
                )
                self.stats["estimated_cost"] += actual_cost
                self.stats["estimated_cost_without_cache"] += cost_without_cache

                # Трекинг использования
                latency_ms = int((time.time() - start_time) * 1000)
                ctx = _get_llm_context()
                track_usage(
                    provider="gemini",
                    model=model_to_use,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cached_tokens,
                    request_type=ctx.get("request_type") or "generate_json",
                    latency_ms=latency_ms,
                    user_id=ctx.get("user_id"),
                    session_id=ctx.get("session_id"),
                    agent_mode=ctx.get("agent_mode"),
                )

                return result

            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    # На rate-limit/перегрузку — экспоненциальный backoff;
                    # на прочих ошибках (парсинг и т.п.) — короткая пауза.
                    delay = min(8.0, 1.0 * (2 ** attempt)) if is_rate_limit_error(e) else 1.0
                    logger.warning(f"[Warning] Attempt {attempt + 1} failed: {e}, retrying in {delay:.1f}s...")
                    await asyncio.sleep(delay)

        self.stats["errors"] += 1
        # Трекинг ошибки
        latency_ms = int((time.time() - start_time) * 1000)
        ctx = _get_llm_context()
        track_usage(
            provider="gemini",
            model=model_to_use,
            input_tokens=input_tokens,
            output_tokens=0,
            cached_tokens=cached_tokens,
            request_type=ctx.get("request_type") or "generate_json",
            success=False,
            error=str(last_error),
            latency_ms=latency_ms,
            user_id=ctx.get("user_id"),
            session_id=ctx.get("session_id"),
            agent_mode=ctx.get("agent_mode"),
        )
        raise RuntimeError(f"Failed after {max_retries + 1} attempts: {last_error}")


# Singleton для глобального использования
_global_client: Optional[GeminiClient] = None


def get_gemini_client(
    model: str = "gemini-flash-lite-latest",
    api_key: Optional[str] = None
) -> GeminiClient:
    """Получить глобальный экземпляр GeminiClient"""
    global _global_client
    if _global_client is None:
        _global_client = GeminiClient(model=model, api_key=api_key)
    return _global_client


def reset_global_client():
    """Сбросить глобальный клиент"""
    global _global_client
    _global_client = None



