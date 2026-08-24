# -*- coding: utf-8 -*-
"""
TESSENT BRAIN - Gemini Embeddings
Генерация эмбеддингов через Gemini API
"""
import asyncio
import logging
import os
from typing import List, Optional

logger = logging.getLogger(__name__)

# Ленивый импорт
_genai = None


def _get_genai():
    """Ленивый импорт Gemini SDK"""
    global _genai
    if _genai is None:
        try:
            import google.generativeai as genai
            _genai = genai
        except ImportError:
            logger.warning("google-generativeai не установлен")
            _genai = False
    return _genai if _genai else None


class GeminiEmbeddings:
    """
    Генератор эмбеддингов через Gemini API.

    Использует модель text-embedding-004 для создания 768-мерных векторов.
    """

    EMBEDDING_DIM = 768

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "models/text-embedding-004"
    ):
        """
        Args:
            api_key: API ключ (берётся из GOOGLE_API_KEY если не указан)
            model: Модель для эмбеддингов
        """
        self.api_key = api_key or os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
        self.model = model
        self.enabled = False

        genai = _get_genai()
        if genai and self.api_key:
            try:
                genai.configure(api_key=self.api_key)
                self.enabled = True
                logger.info(f"✅ GeminiEmbeddings initialized: model={model}")
            except Exception as e:
                logger.warning(f"⚠️ Failed to configure Gemini API: {e}")
        else:
            logger.warning("⚠️ GeminiEmbeddings disabled (missing SDK or API key)")

    async def embed(self, text: str) -> List[float]:
        """
        Создать эмбеддинг для текста.

        Args:
            text: Текст для эмбеддинга

        Returns:
            Список float значений (768-мерный вектор)
        """
        if not self.enabled:
            raise RuntimeError("GeminiEmbeddings is not enabled")

        genai = _get_genai()
        if not genai:
            raise RuntimeError("google-generativeai not available")

        def _embed_sync():
            result = genai.embed_content(
                model=self.model,
                content=text,
                task_type="retrieval_document"
            )
            return result["embedding"]

        return await asyncio.to_thread(_embed_sync)

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """
        Создать эмбеддинги для списка текстов.

        Args:
            texts: Список текстов

        Returns:
            Список эмбеддингов
        """
        if not self.enabled:
            raise RuntimeError("GeminiEmbeddings is not enabled")

        genai = _get_genai()
        if not genai:
            raise RuntimeError("google-generativeai not available")

        def _embed_batch_sync():
            results = []
            for text in texts:
                result = genai.embed_content(
                    model=self.model,
                    content=text,
                    task_type="retrieval_document"
                )
                results.append(result["embedding"])
            return results

        return await asyncio.to_thread(_embed_batch_sync)

    async def embed_query(self, query: str) -> List[float]:
        """
        Создать эмбеддинг для поискового запроса.

        Args:
            query: Поисковый запрос

        Returns:
            Эмбеддинг запроса
        """
        if not self.enabled:
            raise RuntimeError("GeminiEmbeddings is not enabled")

        genai = _get_genai()
        if not genai:
            raise RuntimeError("google-generativeai not available")

        def _embed_sync():
            result = genai.embed_content(
                model=self.model,
                content=query,
                task_type="retrieval_query"
            )
            return result["embedding"]

        return await asyncio.to_thread(_embed_sync)


# Singleton
_global_embeddings: Optional[GeminiEmbeddings] = None


def get_gemini_embeddings() -> GeminiEmbeddings:
    """Получить глобальный экземпляр GeminiEmbeddings"""
    global _global_embeddings
    if _global_embeddings is None:
        _global_embeddings = GeminiEmbeddings()
    return _global_embeddings

