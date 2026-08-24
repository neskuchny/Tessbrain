# -*- coding: utf-8 -*-
"""
Базовый класс для агентов извлечения сущностей.
"""
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backend.core.llm.usage_tracker import UsageContext

logger = logging.getLogger(__name__)


class BaseExtractionAgent(ABC):
    """
    Абстрактный базовый класс для агентов извлечения.

    Все агенты наследуют от этого класса и реализуют:
    - get_instruction(): возвращает промпт для LLM
    - extract(): основной метод извлечения
    - normalize(): нормализация результатов
    """

    def __init__(self, llm_client=None):
        """
        Args:
            llm_client: Клиент LLM (GeminiClient, OpenAIClient или LLMRouter)
        """
        self.llm = llm_client
        self.name = self.__class__.__name__
        self.version = "1.0.0"
        # Маркер последнего extract(): False = была ОШИБКА (не путать с
        # честным «извлечено 0 сущностей»). Читает quality-gate встречи.
        self.last_extract_ok = True
        self.last_extract_error = None

        # Статистика
        self.stats = {
            "extractions": 0,
            "total_items": 0,
            "errors": 0,
            "avg_items_per_extraction": 0.0
        }

    def set_llm(self, llm_client):
        """Установить LLM клиент"""
        self.llm = llm_client

    @abstractmethod
    def get_instruction(self) -> str:
        """
        Возвращает инструкцию для LLM.
        Должна описывать формат извлечения и ожидаемый JSON.
        """
        pass

    @abstractmethod
    def get_entity_type(self) -> str:
        """Возвращает тип извлекаемой сущности (decision, task, participant, etc.)"""
        pass

    async def extract(
        self,
        transcript: str,
        meeting_context: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        Извлекает сущности из транскрипта.

        Args:
            transcript: Текст транскрипта встречи
            meeting_context: Дополнительный контекст встречи

        Returns:
            Список извлеченных сущностей
        """
        if not self.llm:
            logger.warning(f"[{self.name}] LLM client not set, skipping extraction")
            self.last_extract_ok = True
            self.last_extract_error = None
            return []

        # Маркеры последнего прогона: отличают ОШИБКУ от честного «пусто»
        # (раньше оба давали [] → частичный провал встречи был невидим).
        self.last_extract_ok = True
        self.last_extract_error = None
        try:
            self.stats["extractions"] += 1

            # Формируем промпт
            prompt = self._build_prompt(transcript, meeting_context)

            logger.info(f"[{self.name}] Extracting from transcript ({len(transcript)} chars)")

            # Вызываем LLM с трекингом
            async with UsageContext(agent_mode="capture", request_type=f"extract_{self.get_entity_type()}"):
                data = await self.llm.generate_json(prompt)

            # Извлекаем список из ответа
            items = self._extract_items_from_response(data)

            # Нормализуем результаты
            normalized = [self.normalize(item) for item in items]
            normalized = [item for item in normalized if item]  # Убираем None

            # Обновляем статистику
            self.stats["total_items"] += len(normalized)
            self.stats["avg_items_per_extraction"] = (
                self.stats["total_items"] / self.stats["extractions"]
            )

            logger.info(f"[{self.name}] Extracted {len(normalized)} items")
            return normalized

        except Exception as e:
            self.stats["errors"] += 1
            self.last_extract_ok = False
            self.last_extract_error = str(e)[:200]
            logger.error(f"[{self.name}] Error extracting: {e}")
            return []

    def _build_prompt(
        self,
        transcript: str,
        meeting_context: Optional[Dict[str, Any]] = None
    ) -> str:
        """Строит полный промпт для LLM"""
        import json

        # issue #112 T-6 integration: автоматически обогащаем meeting_context
        # speaker_mapping'ом из self-introductions, если в транскрипте есть
        # Speaker N: метки. Это работает для ВСЕХ агентов-наследников
        # (TaskAgent, ParticipantAgent, DecisionAgent, KPIAgent и т.д.),
        # которые увидят mapping в meeting_context при сериализации.
        #
        # Safety:
        #   - Не пересоздаём mapping, если уже передан (явный приоритет)
        #   - Не запускаем resolver на транскрипте без 'Speaker' меток
        #   - resolver не падает на пустых/невалидных входах
        enriched_context = dict(meeting_context or {})
        if "Speaker" in transcript and "speaker_mapping" not in enriched_context:
            try:
                from backend.core.transcripts import resolve_speakers_for_extraction
                mapping = resolve_speakers_for_extraction(
                    transcript,
                    known_participants=enriched_context.get("participants"),
                )
                if mapping:
                    enriched_context["speaker_mapping"] = mapping
            except Exception:
                # speaker_resolver — best-effort, не должен ломать extract
                pass

        prompt_parts = [
            self.get_instruction(),
            "\n\n=== ТРАНСКРИПТ ВСТРЕЧИ ===\n",
            transcript.strip(),
        ]

        if enriched_context:
            prompt_parts.extend([
                "\n\n=== КОНТЕКСТ ВСТРЕЧИ ===\n",
                json.dumps(enriched_context, ensure_ascii=False, indent=2)
            ])

        prompt_parts.append("\n\nВыведи ТОЛЬКО валидный JSON-массив, без пояснений.")

        return "".join(prompt_parts)

    def _extract_items_from_response(
        self,
        data: Any
    ) -> List[Dict[str, Any]]:
        """Извлекает список элементов из ответа LLM"""
        if isinstance(data, list):
            return data

        if isinstance(data, dict):
            # Ищем список в известных ключах
            entity_type = self.get_entity_type()
            possible_keys = [
                entity_type,
                f"{entity_type}s",
                f"extracted_{entity_type}s",
                "items",
                "results",
                "data"
            ]

            # Небольшие исключения для неправильных/нерегулярных множественных форм,
            # которые часто возвращает LLM.
            # Например, EntityAgent имеет entity_type="entity", но LLM почти всегда
            # возвращает ключ "entities" (а не "entitys").
            plural_overrides = {
                "entity": "entities",
            }
            override = plural_overrides.get(entity_type)
            if override and override not in possible_keys:
                possible_keys.insert(1, override)

            for key in possible_keys:
                if key in data and isinstance(data[key], list):
                    return data[key]

            # Если это единственный элемент, оборачиваем в список
            if data:
                return [data]

        return []

    def normalize(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Нормализует извлеченный элемент.
        Переопределяется в дочерних классах для специфичной нормализации.

        Args:
            item: Сырой элемент из LLM

        Returns:
            Нормализованный элемент или None если невалидный
        """
        if not isinstance(item, dict):
            return None

        # Базовая нормализация - добавляем метаданные
        normalized = {
            **item,
            "_extracted_at": datetime.now(timezone.utc).isoformat(),
            "_agent": self.name,
            "_agent_version": self.version,
            "_entity_type": self.get_entity_type()
        }

        return normalized

    def get_stats(self) -> Dict[str, Any]:
        """Получить статистику агента"""
        return {
            "agent": self.name,
            "version": self.version,
            "entity_type": self.get_entity_type(),
            "stats": self.stats
        }



