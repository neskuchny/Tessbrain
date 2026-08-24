import base64
import logging
import os
import time
from typing import Any, Dict, Optional

import httpx

from .usage_tracker import track_usage

logger = logging.getLogger(__name__)

# Ленивый импорт google.generativeai для fallback
_genai = None

def _get_genai():
    """Ленивый импорт Gemini SDK для fallback"""
    global _genai
    if _genai is None:
        try:
            import google.generativeai as genai
            _genai = genai
        except ImportError:
            logger.warning("google-generativeai не установлен. pip install google-generativeai")
            _genai = False
    return _genai if _genai else None


class GemmaClient:
    """
    Client for interacting with Gemma 3n API (SynLabs).
    Provides transcription and chat capabilities.

    При недоступности локальной LLM автоматически переключается на Gemini Flash 2.0.
    Поддерживает model_tier: standard (gemini-flash-lite-latest) и premium (gemini-flash-latest).
    """

    FALLBACK_MODEL = "gemini-flash-lite-latest"
    PREMIUM_MODEL = "gemini-flash-latest"  # Новейшая модель Gemini 3 Flash (preview)

    # Адрес шлюза — конфигурация установки, а не константа кода:
    # значение по умолчанию указывало на конкретную инфраструктуру.
    DEFAULT_API_BASE = "https://api.example.com"

    def __init__(self, api_key: Optional[str] = None,
                 api_base: Optional[str] = None):
        api_base = (api_base or os.getenv("SYNLABS_API_BASE")
                    or self.DEFAULT_API_BASE)
        self.api_key = api_key or os.getenv("SYNLABS_API_KEY") or os.getenv("GEMINI_API_KEY")
        self.api_base = api_base
        self._fallback_enabled = False
        self._gemini_configured = False

        if not self.api_key:
            logger.warning("[Warning] API key not found. Set SYNLABS_API_KEY or GEMINI_API_KEY")

        # Инициализация Gemini SDK для fallback
        self._init_gemini_fallback()

    def _init_gemini_fallback(self):
        """Инициализация Gemini SDK для fallback"""
        genai = _get_genai()
        if genai and self.api_key:
            try:
                genai.configure(api_key=self.api_key)
                self._gemini_configured = True
                logger.info(f"[OK] Gemini fallback configured (model: {self.FALLBACK_MODEL})")
            except Exception as e:
                logger.warning(f"[Warning] Failed to configure Gemini fallback: {e}")
                self._gemini_configured = False

    async def transcribe_audio(self, audio_bytes: bytes, mime_type: str) -> Dict[str, Any]:
        """
        Transcribe audio using Gemma 3n API.
        """
        start_time = time.time()

        try:
            logger.info(f"🎤 [Private LLM] Transcribing audio ({len(audio_bytes)} bytes, {mime_type})...")

            # Determine format
            audio_format = "wav"
            if "mp3" in mime_type:
                audio_format = "mp3"
            elif "m4a" in mime_type or "mp4" in mime_type:
                audio_format = "m4a"
            elif "ogg" in mime_type:
                audio_format = "ogg"
            elif "webm" in mime_type:
                audio_format = "webm"

            # Encode to base64
            audio_base64 = base64.b64encode(audio_bytes).decode('utf-8')

            # Request headers
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    f"{self.api_base}/v1/transcribe",
                    json={
                        "audio_data": audio_base64,
                        "format": audio_format,
                        "language": "ru"
                    },
                    headers=headers
                )

                response.raise_for_status()
                result = response.json()

            # Extract text
            transcription = ""
            if isinstance(result, dict):
                transcription = (
                    result.get("text") or
                    result.get("transcription") or
                    result.get("data", {}).get("text") or
                    result.get("result", {}).get("text") or
                    str(result)
                )
            else:
                transcription = str(result)

            elapsed = time.time() - start_time
            logger.info(f"[OK] [Private LLM] Transcription completed in {elapsed:.2f}s")

            return {
                "success": True,
                "transcription": transcription,
                "time": elapsed,
                "length": len(transcription),
                "model": "gemma-3n",
                "raw_response": result
            }

        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(f"[Error] [Private LLM] Transcription error: {e}")
            return {
                "success": False,
                "error": str(e),
                "time": elapsed,
                "model": "gemma-3n"
            }

    async def chat(self, message: str, context: str = "", system_prompt: str = "", model_tier: str = "standard") -> Dict[str, Any]:
        """
        Chat with Gemma 3n using provided context (transcripts).
        При недоступности локальной LLM автоматически переключается на Gemini.

        Args:
            message: Сообщение пользователя
            context: Контекст (транскрипты, документы)
            system_prompt: Системный промпт
            model_tier: "standard" (gemini-flash-lite-latest) или "premium" (gemini-flash-latest)
        """
        start_time = time.time()

        # Construct prompt
        full_prompt = message
        if context:
            full_prompt = f"CONTEXT:\n{context}\n\nUSER REQUEST:\n{message}"

        if system_prompt:
            full_prompt = f"SYSTEM INSTRUCTION:\n{system_prompt}\n\n{full_prompt}"

        # Сохраняем tier для использования в fallback
        self._current_model_tier = model_tier

        # Сначала пробуем локальную LLM
        try:
            logger.info(f"🧠 [Private LLM] Generating content for message: {message[:50]}... (tier: {model_tier})")

            # Request headers
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            async with httpx.AsyncClient(timeout=30.0) as client:  # Уменьшен таймаут для быстрого fallback
                response = await client.post(
                    f"{self.api_base}/v1/models/gemma-3n/generateContent",
                    json={
                        "contents": [{
                            "role": "user",
                            "parts": [{"text": full_prompt}]
                        }],
                        "generationConfig": {
                            "maxOutputTokens": 4000,
                            "temperature": 0.7,
                            "topP": 0.95,
                            "topK": 40
                        }
                    },
                    headers=headers
                )

                response.raise_for_status()
                result = response.json()

            # Extract text
            analysis = ""
            if isinstance(result, dict):
                candidates = result.get("candidates", [])
                if candidates:
                    content = candidates[0].get("content", {})
                    parts = content.get("parts", [])
                    if parts:
                        analysis = parts[0].get("text", "")

            if not analysis:
                analysis = str(result)

            elapsed = time.time() - start_time
            logger.info(f"[OK] [Private LLM] Chat completed in {elapsed:.2f}s")

            # Трекинг использования локальной LLM
            input_tokens = len(full_prompt.split()) * 1.3  # Примерная оценка
            output_tokens = len(analysis.split()) * 1.3
            track_usage(
                provider="local",
                model="gemma-3n",
                input_tokens=int(input_tokens),
                output_tokens=int(output_tokens),
                request_type="chat",
                agent_mode="transcripts",
                latency_ms=int(elapsed * 1000),
            )

            return {
                "success": True,
                "response": analysis,
                "time": elapsed,
                "length": len(analysis),
                "model": "gemma-3n"
            }

        except Exception as e:
            logger.warning(f"[Warning] [Private LLM] Local LLM failed: {e}. Trying Gemini fallback...")

            # Fallback на Gemini
            return await self._chat_with_gemini_fallback(full_prompt, message, start_time)

    async def _chat_with_gemini_fallback(self, full_prompt: str, original_message: str, start_time: float) -> Dict[str, Any]:
        """
        Fallback на Gemini при недоступности локальной LLM.
        Использует модель в зависимости от _current_model_tier.
        """
        if not self._gemini_configured:
            elapsed = time.time() - start_time
            logger.error("[Error] [Fallback] Gemini not configured, cannot fallback")
            return {
                "success": False,
                "error": "Local LLM unavailable and Gemini fallback not configured",
                "time": elapsed,
                "model": "gemma-3n"
            }

        try:
            # Выбираем модель в зависимости от tier
            model_tier = getattr(self, '_current_model_tier', 'standard')
            model_name = self.PREMIUM_MODEL if model_tier == 'premium' else self.FALLBACK_MODEL

            logger.info(f"🔄 [Fallback] Using {model_name} (tier: {model_tier}) for message: {original_message[:50]}...")

            genai = _get_genai()
            if not genai:
                raise Exception("google-generativeai not available")

            # Создаём модель Gemini
            model = genai.GenerativeModel(model_name)

            # Генерируем ответ (синхронно, но оборачиваем в asyncio)
            import asyncio
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: model.generate_content(
                    full_prompt,
                    generation_config={
                        "max_output_tokens": 4000,
                        "temperature": 0.7,
                        "top_p": 0.95,
                        "top_k": 40
                    }
                )
            )

            # Извлекаем текст
            analysis = response.text if hasattr(response, 'text') else str(response)

            elapsed = time.time() - start_time
            logger.info(f"[OK] [Fallback] {model_name} completed in {elapsed:.2f}s")

            # Трекинг использования Gemini fallback
            input_tokens = len(full_prompt.split()) * 1.3  # Примерная оценка
            output_tokens = len(analysis.split()) * 1.3
            track_usage(
                provider="gemini",
                model=model_name,
                model_tier=model_tier,
                input_tokens=int(input_tokens),
                output_tokens=int(output_tokens),
                request_type="chat_fallback",
                agent_mode="transcripts",
                latency_ms=int(elapsed * 1000),
            )

            return {
                "success": True,
                "response": analysis,
                "time": elapsed,
                "length": len(analysis),
                "model": model_name,
                "model_tier": model_tier,
                "fallback": True
            }

        except Exception as e:
            elapsed = time.time() - start_time
            model_name = self.PREMIUM_MODEL if getattr(self, '_current_model_tier', 'standard') == 'premium' else self.FALLBACK_MODEL
            logger.error(f"[Error] [Fallback] Gemini error: {e}")

            # Трекинг ошибки
            track_usage(
                provider="gemini",
                model=model_name,
                input_tokens=int(len(full_prompt.split()) * 1.3),
                output_tokens=0,
                request_type="chat_fallback",
                agent_mode="transcripts",
                success=False,
                error=str(e),
                latency_ms=int(elapsed * 1000),
            )

            return {
                "success": False,
                "error": f"Both local LLM and Gemini fallback failed: {e}",
                "time": elapsed,
                "model": model_name
            }

# Singleton
_global_gemma_client: Optional[GemmaClient] = None

def get_gemma_client() -> GemmaClient:
    global _global_gemma_client
    if _global_gemma_client is None:
        _global_gemma_client = GemmaClient()
    return _global_gemma_client

