# -*- coding: utf-8 -*-
"""
Chunk Model - Модель данных чанка.

Вдохновлено KAG (Knowledge Augmented Generation).

Чанк - это атомарная единица текста с метаданными,
которая может быть проиндексирована и найдена.
"""

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class ChunkType(str, Enum):
    """Типы чанков."""
    TEXT = "text"                    # Обычный текст
    SPEAKER_TURN = "speaker_turn"    # Реплика спикера
    TOPIC_SEGMENT = "topic_segment"  # Тематический сегмент
    PARAGRAPH = "paragraph"          # Параграф
    SECTION = "section"              # Секция документа
    SUMMARY = "summary"              # Резюме
    CODE = "code"                    # Код
    TABLE = "table"                  # Таблица
    LIST = "list"                    # Список
    QUOTE = "quote"                  # Цитата


@dataclass
class Chunk:
    """
    Чанк - атомарная единица текста.

    Содержит:
    - Контент (текст)
    - Метаданные (источник, позиция, тип)
    - Связи (с другими чанками, сущностями)
    - Эмбеддинг (опционально)
    """

    # Идентификация
    id: str
    content: str
    source_id: str

    # Тип и позиция
    type: ChunkType = ChunkType.TEXT
    position: int = 0  # Порядковый номер в источнике

    # Границы в исходном тексте
    start_char: int = 0
    end_char: int = 0

    # Метаданные контента
    name: Optional[str] = None  # Название (заголовок секции, имя спикера)
    topic: Optional[str] = None  # Тема
    speaker: Optional[str] = None  # Спикер (для транскриптов)

    # Статистика
    char_count: int = 0
    word_count: int = 0
    sentence_count: int = 0

    # Связи
    parent_chunk_id: Optional[str] = None  # Родительский чанк
    child_chunk_ids: List[str] = field(default_factory=list)  # Дочерние чанки
    related_entity_ids: List[str] = field(default_factory=list)  # Связанные сущности

    # Эмбеддинг
    embedding: Optional[List[float]] = None
    embedding_model: Optional[str] = None

    # Временные метки
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    # Хеш контента (для дедупликации)
    content_hash: Optional[str] = None

    # Дополнительные метаданные
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        """Вычисление статистики после создания."""
        if self.content:
            self.char_count = len(self.content)
            self.word_count = len(self.content.split())
            self.sentence_count = self.content.count('.') + self.content.count('!') + self.content.count('?')

            if not self.content_hash:
                self.content_hash = hashlib.md5(self.content.encode('utf-8')).hexdigest()

            if self.end_char == 0:
                self.end_char = self.start_char + len(self.content)

    @classmethod
    def create(
        cls,
        content: str,
        source_id: str,
        chunk_type: ChunkType = ChunkType.TEXT,
        position: int = 0,
        **kwargs
    ) -> "Chunk":
        """
        Фабричный метод для создания чанка.

        Args:
            content: Текст чанка
            source_id: ID источника
            chunk_type: Тип чанка
            position: Позиция в источнике
            **kwargs: Дополнительные параметры

        Returns:
            Новый чанк
        """
        chunk_id = kwargs.pop('id', None) or str(uuid.uuid4())

        return cls(
            id=chunk_id,
            content=content,
            source_id=source_id,
            type=chunk_type,
            position=position,
            **kwargs
        )

    def to_dict(self) -> Dict[str, Any]:
        """Преобразование в словарь."""
        return {
            "id": self.id,
            "content": self.content,
            "source_id": self.source_id,
            "type": self.type.value if isinstance(self.type, ChunkType) else self.type,
            "position": self.position,
            "start_char": self.start_char,
            "end_char": self.end_char,
            "name": self.name,
            "topic": self.topic,
            "speaker": self.speaker,
            "char_count": self.char_count,
            "word_count": self.word_count,
            "sentence_count": self.sentence_count,
            "parent_chunk_id": self.parent_chunk_id,
            "child_chunk_ids": self.child_chunk_ids,
            "related_entity_ids": self.related_entity_ids,
            "embedding_model": self.embedding_model,
            "created_at": self.created_at,
            "content_hash": self.content_hash,
            "metadata": self.metadata
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Chunk":
        """Создание из словаря."""
        # Преобразование типа
        chunk_type = data.get("type", "text")
        if isinstance(chunk_type, str):
            try:
                chunk_type = ChunkType(chunk_type)
            except ValueError:
                chunk_type = ChunkType.TEXT

        return cls(
            id=data.get("id", str(uuid.uuid4())),
            content=data.get("content", ""),
            source_id=data.get("source_id", ""),
            type=chunk_type,
            position=data.get("position", 0),
            start_char=data.get("start_char", 0),
            end_char=data.get("end_char", 0),
            name=data.get("name"),
            topic=data.get("topic"),
            speaker=data.get("speaker"),
            char_count=data.get("char_count", 0),
            word_count=data.get("word_count", 0),
            sentence_count=data.get("sentence_count", 0),
            parent_chunk_id=data.get("parent_chunk_id"),
            child_chunk_ids=data.get("child_chunk_ids", []),
            related_entity_ids=data.get("related_entity_ids", []),
            embedding=data.get("embedding"),
            embedding_model=data.get("embedding_model"),
            created_at=data.get("created_at", datetime.now().isoformat()),
            content_hash=data.get("content_hash"),
            metadata=data.get("metadata", {})
        )

    def get_searchable_text(self) -> str:
        """Получить текст для поиска (включая метаданные)."""
        parts = [self.content]

        if self.name:
            parts.insert(0, f"[{self.name}]")

        if self.topic:
            parts.append(f"Тема: {self.topic}")

        if self.speaker:
            parts.insert(0, f"{self.speaker}:")

        return " ".join(parts)

    def is_similar_to(self, other: "Chunk", threshold: float = 0.9) -> bool:
        """Проверка схожести с другим чанком."""
        if self.content_hash and other.content_hash:
            return self.content_hash == other.content_hash

        # Простое сравнение по словам
        words1 = set(self.content.lower().split())
        words2 = set(other.content.lower().split())

        if not words1 or not words2:
            return False

        intersection = len(words1 & words2)
        union = len(words1 | words2)

        return (intersection / union) >= threshold if union > 0 else False
