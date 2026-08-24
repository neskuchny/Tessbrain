# -*- coding: utf-8 -*-
"""
Knowledge Extractor - Извлечение структурированных знаний из чанков.

Вдохновлено KAG (Knowledge Augmented Generation).

Типы извлекаемых знаний:
- Факты (утверждения)
- Кейсы (примеры, истории успеха/неудач)
- Методологии (процессы, подходы)
- Шаблоны (templates)
- Регламенты (правила, политики)
- Лучшие практики
- Инсайты (выводы, наблюдения)
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class KnowledgeType(str, Enum):
    """Типы знаний."""
    FACT = "fact"                      # Факт, утверждение
    CASE = "case"                      # Кейс, пример
    METHODOLOGY = "methodology"        # Методология, процесс
    TEMPLATE = "template"              # Шаблон
    REGULATION = "regulation"          # Регламент, правило
    BEST_PRACTICE = "best_practice"    # Лучшая практика
    GUIDELINE = "guideline"            # Рекомендация
    INSIGHT = "insight"                # Инсайт, вывод
    STANDARD = "standard"              # Стандарт
    ACHIEVEMENT = "achievement"        # Достижение, результат
    EXPERT_KNOWLEDGE = "expert"        # Экспертное знание
    PLAYBOOK = "playbook"              # Playbook, сценарий
    ANTIPATTERN = "antipattern"        # Антипаттерн, что не делать
    WORKFLOW = "workflow"              # Рабочий процесс


@dataclass
class KnowledgeItem:
    """Элемент знания."""
    id: str
    type: KnowledgeType
    title: str
    content: str
    source_id: str
    chunk_id: str

    # Метаданные
    confidence: float = 0.8
    importance: float = 0.5

    # Связи
    related_entities: List[str] = field(default_factory=list)
    related_topics: List[str] = field(default_factory=list)

    # Теги и категории
    tags: List[str] = field(default_factory=list)
    category: Optional[str] = None

    # Контекст
    context: Optional[str] = None
    speaker: Optional[str] = None

    # Временные метки
    extracted_at: str = field(default_factory=lambda: datetime.now().isoformat())
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None

    # Дополнительные данные
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Преобразование в словарь."""
        return {
            "id": self.id,
            "type": self.type.value if isinstance(self.type, KnowledgeType) else self.type,
            "title": self.title,
            "content": self.content,
            "source_id": self.source_id,
            "chunk_id": self.chunk_id,
            "confidence": self.confidence,
            "importance": self.importance,
            "related_entities": self.related_entities,
            "related_topics": self.related_topics,
            "tags": self.tags,
            "category": self.category,
            "context": self.context,
            "speaker": self.speaker,
            "extracted_at": self.extracted_at,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "metadata": self.metadata
        }


class KnowledgeExtractor:
    """
    Извлечение структурированных знаний из текста.

    Использует LLM для глубокого анализа и извлечения
    различных типов знаний.
    """

    def __init__(self, llm_router=None):
        """
        Args:
            llm_router: LLM роутер для извлечения знаний
        """
        self.llm = llm_router
        self._knowledge_counter = 0

        logger.info("✅ KnowledgeExtractor initialized")

    async def extract(
        self,
        text: str,
        source_id: str,
        chunk_id: str,
        context: Optional[str] = None,
        knowledge_types: Optional[List[KnowledgeType]] = None
    ) -> List[KnowledgeItem]:
        """
        Извлечь знания из текста.

        Args:
            text: Текст для анализа
            source_id: ID источника
            chunk_id: ID чанка
            context: Дополнительный контекст
            knowledge_types: Типы знаний для извлечения (None = все)

        Returns:
            Список извлечённых знаний
        """
        if not text or not text.strip():
            return []

        if not self.llm:
            logger.warning("⚠️ LLM not available, using rule-based extraction")
            return self._extract_rules_based(text, source_id, chunk_id)

        try:
            # Определяем типы знаний для извлечения
            types_to_extract = knowledge_types or list(KnowledgeType)
            types_str = ", ".join([t.value for t in types_to_extract])

            prompt = self._build_extraction_prompt(text, types_str, context)
            response = await self.llm.generate(prompt)

            # Парсим ответ
            knowledge_items = self._parse_llm_response(
                response, source_id, chunk_id
            )

            logger.info(f"✅ Extracted {len(knowledge_items)} knowledge items from chunk {chunk_id}")
            return knowledge_items

        except Exception as e:
            logger.error(f"❌ Knowledge extraction failed: {e}")
            return self._extract_rules_based(text, source_id, chunk_id)

    def _build_extraction_prompt(
        self,
        text: str,
        types: str,
        context: Optional[str]
    ) -> str:
        """Построить промпт для извлечения знаний."""
        context_str = f"\n\nКОНТЕКСТ:\n{context}" if context else ""

        return f"""Проанализируй текст и извлеки структурированные знания.

ТИПЫ ЗНАНИЙ для извлечения: {types}

Описание типов:
- fact: Конкретный факт, утверждение, данные
- case: Кейс, пример, история успеха или неудачи
- methodology: Методология, процесс, подход к решению задач
- template: Шаблон, форма, структура
- regulation: Регламент, правило, политика
- best_practice: Лучшая практика, рекомендация
- guideline: Рекомендация, совет
- insight: Инсайт, вывод, наблюдение
- standard: Стандарт, норматив
- achievement: Достижение, результат, метрика
- expert: Экспертное знание, профессиональный совет
- playbook: Playbook, сценарий действий
- antipattern: Антипаттерн, что НЕ нужно делать
- workflow: Рабочий процесс, последовательность шагов

ТЕКСТ для анализа:
{text}{context_str}

ФОРМАТ ОТВЕТА (JSON):
{{
    "knowledge_items": [
        {{
            "type": "тип знания",
            "title": "Краткий заголовок (до 100 символов)",
            "content": "Полное описание знания",
            "confidence": 0.0-1.0,
            "importance": 0.0-1.0,
            "related_entities": ["сущность1", "сущность2"],
            "related_topics": ["тема1", "тема2"],
            "tags": ["тег1", "тег2"],
            "category": "категория",
            "speaker": "спикер (если есть)"
        }}
    ]
}}

ПРАВИЛА:
1. Извлекай ТОЛЬКО явно присутствующие знания, не придумывай. content — по
   сказанному в тексте, без домыслов и без фактов извне.
2. Confidence отражает уверенность в правильности извлечения (честно).
3. Importance отражает важность знания для организации.
4. related_entities/speaker указывай, ТОЛЬКО если они названы в тексте (speaker
   пусто, если непонятно кто).
5. Если знаний нет, верни пустой массив. Пустой массив лучше выдуманного знания.

Ответь ТОЛЬКО JSON без дополнительного текста."""

    def _parse_llm_response(
        self,
        response: str,
        source_id: str,
        chunk_id: str
    ) -> List[KnowledgeItem]:
        """Парсинг ответа LLM."""
        try:
            # Ищем JSON в ответе
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if not json_match:
                return []

            data = json.loads(json_match.group())
            items = data.get("knowledge_items", [])

            knowledge_items = []
            for item in items:
                try:
                    knowledge_type = KnowledgeType(item.get("type", "fact"))
                except ValueError:
                    knowledge_type = KnowledgeType.FACT

                self._knowledge_counter += 1
                knowledge_id = f"knowledge_{self._knowledge_counter}_{chunk_id[:8]}"

                knowledge_item = KnowledgeItem(
                    id=knowledge_id,
                    type=knowledge_type,
                    title=item.get("title", "Без названия"),
                    content=item.get("content", ""),
                    source_id=source_id,
                    chunk_id=chunk_id,
                    confidence=float(item.get("confidence", 0.8)),
                    importance=float(item.get("importance", 0.5)),
                    related_entities=item.get("related_entities", []),
                    related_topics=item.get("related_topics", []),
                    tags=item.get("tags", []),
                    category=item.get("category"),
                    speaker=item.get("speaker")
                )

                knowledge_items.append(knowledge_item)

            return knowledge_items

        except json.JSONDecodeError as e:
            logger.error(f"❌ Failed to parse LLM response: {e}")
            return []

    def _extract_rules_based(
        self,
        text: str,
        source_id: str,
        chunk_id: str
    ) -> List[KnowledgeItem]:
        """
        Извлечение знаний на основе правил (fallback).

        Использует паттерны для определения типов знаний.
        """
        knowledge_items = []

        # Паттерны для разных типов знаний
        patterns = {
            KnowledgeType.FACT: [
                r'(?:составляет|равняется|достиг[а-я]*|выросл[а-я]*|снизил[а-я]*)\s+\d+',
                r'\d+%',
                r'(?:в|за)\s+\d{4}\s+году',
            ],
            KnowledgeType.BEST_PRACTICE: [
                r'(?:рекомендуется|следует|лучше всего|эффективно)',
                r'best practice',
                r'лучшая практика',
            ],
            KnowledgeType.ANTIPATTERN: [
                r'(?:не следует|нельзя|избегать|не рекомендуется)',
                r'ошибк[а-я]+',
                r'проблем[а-я]+',
            ],
            KnowledgeType.METHODOLOGY: [
                r'(?:процесс|методология|подход|алгоритм)',
                r'(?:шаг\s+\d|этап\s+\d)',
            ],
            KnowledgeType.INSIGHT: [
                r'(?:выяснилось|оказалось|интересно|важно отметить)',
                r'(?:вывод|заключение)',
            ],
        }

        sentences = re.split(r'[.!?]\s+', text)

        for sentence in sentences:
            sentence = sentence.strip()
            if len(sentence) < 20:
                continue

            for knowledge_type, type_patterns in patterns.items():
                for pattern in type_patterns:
                    if re.search(pattern, sentence, re.IGNORECASE):
                        self._knowledge_counter += 1
                        knowledge_id = f"knowledge_{self._knowledge_counter}_{chunk_id[:8]}"

                        knowledge_item = KnowledgeItem(
                            id=knowledge_id,
                            type=knowledge_type,
                            title=sentence[:100],
                            content=sentence,
                            source_id=source_id,
                            chunk_id=chunk_id,
                            confidence=0.6,  # Ниже, так как rule-based
                            importance=0.5
                        )

                        knowledge_items.append(knowledge_item)
                        break

        return knowledge_items

    async def extract_from_meeting(
        self,
        transcript: str,
        meeting_id: str,
        participants: Optional[List[str]] = None
    ) -> List[KnowledgeItem]:
        """
        Извлечь знания из транскрипта встречи.

        Специализированный метод для встреч с учётом:
        - Участников
        - Решений
        - Задач
        - Договорённостей
        """
        if not self.llm:
            return []

        try:
            participants_str = ", ".join(participants) if participants else "неизвестны"

            prompt = f"""Проанализируй транскрипт встречи и извлеки структурированные знания.

УЧАСТНИКИ: {participants_str}

ТРАНСКРИПТ:
{transcript[:8000]}

Извлеки следующие типы знаний:
1. РЕШЕНИЯ (decisions) - что было решено
2. ЗАДАЧИ (tasks) - что нужно сделать
3. ДОГОВОРЁННОСТИ (agreements) - о чём договорились
4. ИНСАЙТЫ (insights) - важные наблюдения и выводы
5. КЕЙСЫ (cases) - упомянутые примеры и истории
6. ЛУЧШИЕ ПРАКТИКИ (best_practices) - рекомендации и советы
7. ПРОБЛЕМЫ (problems) - выявленные проблемы
8. МЕТРИКИ (metrics) - упомянутые числа и показатели

ФОРМАТ ОТВЕТА (JSON):
{{
    "knowledge_items": [
        {{
            "type": "тип",
            "title": "Краткий заголовок",
            "content": "Полное описание",
            "speaker": "Кто сказал (если известно)",
            "confidence": 0.0-1.0,
            "importance": 0.0-1.0,
            "related_entities": ["сущности"],
            "tags": ["теги"]
        }}
    ]
}}

Извлекай ТОЛЬКО явно присутствующую информацию. Не выдумывай решений, задач,
договорённостей и выводов, которых нет в тексте. МЕТРИКИ (числа) бери дословно —
не подставляй правдоподобные цифры. speaker указывай, только если понятно кто.
Ничего не найдено по типу — просто не добавляй элементы этого типа."""

            response = await self.llm.generate(prompt)

            # Маппинг типов из промпта в KnowledgeType
            type_mapping = {
                "decisions": KnowledgeType.FACT,
                "tasks": KnowledgeType.WORKFLOW,
                "agreements": KnowledgeType.REGULATION,
                "insights": KnowledgeType.INSIGHT,
                "cases": KnowledgeType.CASE,
                "best_practices": KnowledgeType.BEST_PRACTICE,
                "problems": KnowledgeType.ANTIPATTERN,
                "metrics": KnowledgeType.ACHIEVEMENT,
            }

            # Парсим ответ
            try:
                json_match = re.search(r'\{.*\}', response, re.DOTALL)
                if json_match:
                    data = json.loads(json_match.group())
                    items = data.get("knowledge_items", [])

                    knowledge_items = []
                    for item in items:
                        item_type = item.get("type", "fact")
                        knowledge_type = type_mapping.get(item_type, KnowledgeType.FACT)

                        self._knowledge_counter += 1
                        knowledge_id = f"meeting_knowledge_{self._knowledge_counter}_{meeting_id[:8]}"

                        knowledge_item = KnowledgeItem(
                            id=knowledge_id,
                            type=knowledge_type,
                            title=item.get("title", "Без названия"),
                            content=item.get("content", ""),
                            source_id=meeting_id,
                            chunk_id=meeting_id,
                            confidence=float(item.get("confidence", 0.8)),
                            importance=float(item.get("importance", 0.5)),
                            related_entities=item.get("related_entities", []),
                            tags=item.get("tags", []),
                            speaker=item.get("speaker"),
                            metadata={"source_type": "meeting"}
                        )

                        knowledge_items.append(knowledge_item)

                    logger.info(f"✅ Extracted {len(knowledge_items)} knowledge items from meeting {meeting_id}")
                    return knowledge_items

            except json.JSONDecodeError:
                logger.debug("suppressed exception", exc_info=True)

        except Exception as e:
            logger.error(f"❌ Meeting knowledge extraction failed: {e}")

        return []


