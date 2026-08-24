# -*- coding: utf-8 -*-
"""
Base Agent - базовый класс для всех агентов системы ТЗ.
"""

import json
import logging
from abc import ABC
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class BaseTaskAgent(ABC):
    """Базовый класс для агентов системы ТЗ."""

    def __init__(self, name: str, storage=None, llm_router=None):
        self.name = name
        self.storage = storage
        self.llm_router = llm_router
        self.logger = logging.getLogger(f"task_spec.{name}")

    def log_start(self, message: str):
        """Логирование начала работы агента."""
        self.logger.info(f"🚀 [{self.name}] {message}")

    def log_complete(self, message: str):
        """Логирование завершения работы агента."""
        self.logger.info(f"✅ [{self.name}] {message}")

    def log_error(self, message: str):
        """Логирование ошибки."""
        self.logger.error(f"❌ [{self.name}] {message}")

    def log_info(self, message: str):
        """Логирование информации."""
        self.logger.info(f"ℹ️ [{self.name}] {message}")

    async def generate_llm_response(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096
    ) -> Dict[str, Any]:
        """Генерация ответа через LLM."""
        if not self.llm_router:
            self.log_error("LLM Router не инициализирован")
            return {}

        try:
            response = await self.llm_router.generate(
                prompt=prompt,
                system_prompt=system_prompt,
                temperature=temperature,
                max_tokens=max_tokens
            )

            # Пытаемся распарсить JSON
            return self._parse_json_response(response)

        except Exception as e:
            self.log_error(f"Ошибка генерации LLM: {e}")
            return {"error": str(e), "raw_response": response if 'response' in dir() else None}

    def _parse_json_response(self, response: str) -> Dict[str, Any]:
        """Парсинг JSON из ответа LLM."""
        if not response:
            return {}

        # Ищем JSON в ответе
        try:
            # Пробуем напрямую
            return json.loads(response)
        except json.JSONDecodeError:
            logger.debug("suppressed exception", exc_info=True)

        # Ищем JSON блок в markdown
        import re
        json_match = re.search(r'```(?:json)?\s*\n?([\s\S]*?)\n?```', response)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except json.JSONDecodeError:
                logger.debug("suppressed exception", exc_info=True)

        # Ищем JSON объект
        json_match = re.search(r'\{[\s\S]*\}', response)
        if json_match:
            try:
                return json.loads(json_match.group(0))
            except json.JSONDecodeError:
                logger.debug("suppressed exception", exc_info=True)

        # Возвращаем как текст
        return {"raw_text": response}

    async def search_vector(self, query: str, top_k: int = 10) -> List[Dict[str, Any]]:
        """Поиск по векторной базе."""
        if not self.storage:
            return []

        try:
            if hasattr(self.storage, 'vector_search'):
                return await self.storage.vector_search(query, limit=top_k)
            elif hasattr(self.storage, 'search'):
                # Пробуем limit, если не работает - top_k
                try:
                    return await self.storage.search(query, limit=top_k)
                except TypeError:
                    return await self.storage.search(query, top_k=top_k)
            return []
        except Exception as e:
            self.log_error(f"Ошибка векторного поиска: {e}")
            return []

    async def search_graph(self, query: str, depth: int = 2) -> List[Dict[str, Any]]:
        """Поиск по графу знаний."""
        if not self.storage:
            return []

        try:
            if hasattr(self.storage, 'graph_search'):
                return await self.storage.graph_search(query, depth=depth)
            elif hasattr(self.storage, 'graph') and hasattr(self.storage.graph, 'search'):
                return self.storage.graph.search(query, depth=depth)
            return []
        except Exception as e:
            self.log_error(f"Ошибка поиска по графу: {e}")
            return []

    def get_timestamp(self) -> str:
        """Получить текущий timestamp."""
        return datetime.utcnow().isoformat()

    def extract_entities(self, text: str) -> Dict[str, List[str]]:
        """Извлечение ключевых сущностей из текста."""
        entities = {
            "projects": [],
            "people": [],
            "tasks": [],
            "technologies": [],
            "concepts": []
        }

        # Простое извлечение на основе паттернов
        # В реальности можно использовать NER модель

        text_lower = text.lower()

        # Проекты (часто упоминаются с большой буквы или в кавычках)
        import re
        project_patterns = re.findall(r'проект[а-я]*\s+["\']?([А-Яа-яA-Za-z0-9_-]+)["\']?', text, re.IGNORECASE)
        entities["projects"].extend(project_patterns)

        # Люди (имена с большой буквы)
        name_patterns = re.findall(r'\b([А-Я][а-я]+(?:\s+[А-Я][а-я]+)?)\b', text)
        entities["people"].extend([n for n in name_patterns if len(n) > 3])

        # Технологии
        tech_keywords = ['api', 'python', 'javascript', 'react', 'vue', 'node', 'база данных',
                        'postgresql', 'mongodb', 'redis', 'docker', 'kubernetes', 'ai', 'ml',
                        'llm', 'gemini', 'openai', 'telegram', 'webhook']
        for tech in tech_keywords:
            if tech in text_lower:
                entities["technologies"].append(tech)

        return entities

