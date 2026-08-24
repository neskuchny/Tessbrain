# -*- coding: utf-8 -*-
"""
TESSENT BRAIN - Ollama Embeddings
Генерация эмбеддингов через локальный Ollama
"""
import logging
import os
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)


class OllamaEmbeddings:
    """
    Генератор эмбеддингов через локальный Ollama.

    Использует модель nomic-embed-text или mxbai-embed-large для создания эмбеддингов.
    """

    DEFAULT_MODEL = "nomic-embed-text"
    EMBEDDING_DIM = 768  # Зависит от модели

    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None
    ):
        """
        Args:
            base_url: URL Ollama API (по умолчанию http://localhost:11434)
            model: Модель для эмбеддингов
        """
        self.base_url = base_url or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        self.model = model or os.environ.get("OLLAMA_EMBED_MODEL", self.DEFAULT_MODEL)
        self.enabled = False

        # Проверяем доступность Ollama
        self._check_availability()

    def _check_availability(self):
        """Проверить доступность Ollama"""
        try:
            with httpx.Client(timeout=5.0) as client:
                response = client.get(f"{self.base_url}/api/tags")
                if response.status_code == 200:
                    self.enabled = True
                    logger.info(f"✅ OllamaEmbeddings initialized: model={self.model}")
                else:
                    logger.warning(f"⚠️ Ollama returned status {response.status_code}")
        except Exception as e:
            logger.warning(f"⚠️ Ollama not available at {self.base_url}: {e}")

    async def embed(self, text: str) -> List[float]:
        """
        Создать эмбеддинг для текста.

        Args:
            text: Текст для эмбеддинга

        Returns:
            Список float значений
        """
        if not self.enabled:
            raise RuntimeError("OllamaEmbeddings is not enabled")

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.base_url}/api/embeddings",
                json={
                    "model": self.model,
                    "prompt": text
                }
            )
            response.raise_for_status()
            data = response.json()
            return data["embedding"]

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """
        Создать эмбеддинги для списка текстов.

        Args:
            texts: Список текстов

        Returns:
            Список эмбеддингов
        """
        results = []
        for text in texts:
            embedding = await self.embed(text)
            results.append(embedding)
        return results

    async def embed_query(self, query: str) -> List[float]:
        """
        Создать эмбеддинг для поискового запроса.

        Args:
            query: Поисковый запрос

        Returns:
            Эмбеддинг запроса
        """
        return await self.embed(query)


# Singleton
_global_embeddings: Optional[OllamaEmbeddings] = None


def get_ollama_embeddings() -> OllamaEmbeddings:
    """Получить глобальный экземпляр OllamaEmbeddings"""
    global _global_embeddings
    if _global_embeddings is None:
        _global_embeddings = OllamaEmbeddings()
    return _global_embeddings

