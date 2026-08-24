# -*- coding: utf-8 -*-
"""
Semantic Splitter - Семантическое разбиение текста на чанки.

Вдохновлено KAG (Knowledge Augmented Generation).

Поддерживает:
- Разбиение по длине с sliding window (overlap)
- Разбиение по спикерам (для транскриптов)
- Разбиение по темам через LLM
- Разбиение по структуре (заголовки, секции)
"""

import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from backend.core.llm.usage_tracker import UsageContext

from .chunk_model import Chunk, ChunkType

logger = logging.getLogger(__name__)


@dataclass
class SplitterConfig:
    """Конфигурация сплиттера."""

    # Длина чанка
    chunk_size: int = 500
    window_size: int = 100  # Overlap между чанками

    # Минимальная длина чанка
    min_chunk_size: int = 50

    # Максимальная длина предложения (для принудительного разбиения)
    max_sentence_length: int = 1000

    # Разделители предложений
    sentence_delimiters: str = ".。？?！!\n"

    # Язык
    language: str = "ru"


class SemanticSplitter:
    """
    Семантический сплиттер текста.

    Методы разбиения:
    1. split_by_length() - по длине с overlap
    2. split_by_speaker() - по спикерам (транскрипты)
    3. split_by_topic() - по темам (через LLM)
    4. split_by_structure() - по структуре документа
    """

    def __init__(
        self,
        config: Optional[SplitterConfig] = None,
        llm_router=None
    ):
        """
        Args:
            config: Конфигурация сплиттера
            llm_router: LLM роутер для семантического разбиения
        """
        self.config = config or SplitterConfig()
        self.llm = llm_router

        logger.info(f"✅ SemanticSplitter initialized (chunk_size={self.config.chunk_size}, "
                   f"window={self.config.window_size})")

    def split(
        self,
        text: str,
        source_id: str,
        method: str = "auto",
        **kwargs
    ) -> List[Chunk]:
        """
        Разбить текст на чанки.

        Args:
            text: Исходный текст
            source_id: ID источника
            method: Метод разбиения (auto, length, speaker, topic, structure)
            **kwargs: Дополнительные параметры

        Returns:
            Список чанков
        """
        if not text or not text.strip():
            return []

        # Автоматический выбор метода
        if method == "auto":
            method = self._detect_best_method(text)

        logger.info(f"📝 Splitting text ({len(text)} chars) using method: {method}")

        if method == "speaker":
            chunks = self.split_by_speaker(text, source_id, **kwargs)
        elif method == "topic":
            chunks = self.split_by_topic(text, source_id, **kwargs)
        elif method == "structure":
            chunks = self.split_by_structure(text, source_id, **kwargs)
        else:  # length
            chunks = self.split_by_length(text, source_id, **kwargs)

        logger.info(f"✅ Created {len(chunks)} chunks")
        return chunks

    def _detect_best_method(self, text: str) -> str:
        """Определить лучший метод разбиения."""
        # Проверяем наличие спикеров (формат "Имя: текст")
        speaker_pattern = r'^[А-ЯA-Z][а-яa-z]+\s*:'
        if len(re.findall(speaker_pattern, text, re.MULTILINE)) > 3:
            return "speaker"

        # Проверяем наличие заголовков (markdown или структура)
        header_pattern = r'^#{1,6}\s|^\d+\.\s|^[A-ZА-Я]{2,}\s*$'
        if len(re.findall(header_pattern, text, re.MULTILINE)) > 2:
            return "structure"

        # По умолчанию - по длине
        return "length"

    def split_by_length(
        self,
        text: str,
        source_id: str,
        chunk_size: Optional[int] = None,
        window_size: Optional[int] = None
    ) -> List[Chunk]:
        """
        Разбить по длине с sliding window (overlap).

        Args:
            text: Исходный текст
            source_id: ID источника
            chunk_size: Размер чанка (по умолчанию из config)
            window_size: Размер overlap (по умолчанию из config)

        Returns:
            Список чанков
        """
        chunk_size = chunk_size or self.config.chunk_size
        window_size = window_size or self.config.window_size

        # Разбиваем на предложения
        sentences = self._split_sentences(text)

        if not sentences:
            return []

        chunks = []
        current_sentences = []
        current_length = 0
        position = 0
        start_char = 0

        for sentence in sentences:
            sentence_len = len(sentence)

            # Если добавление предложения превысит лимит
            if current_length + sentence_len > chunk_size and current_sentences:
                # Создаём чанк
                content = " ".join(current_sentences)
                chunk = Chunk.create(
                    content=content,
                    source_id=source_id,
                    chunk_type=ChunkType.TEXT,
                    position=position,
                    start_char=start_char,
                    end_char=start_char + len(content)
                )
                chunks.append(chunk)
                position += 1

                # Sliding window - оставляем последние предложения для overlap
                overlap_sentences = []
                overlap_length = 0
                for s in reversed(current_sentences):
                    if overlap_length + len(s) <= window_size:
                        overlap_sentences.insert(0, s)
                        overlap_length += len(s)
                    else:
                        break

                start_char += len(content) - overlap_length
                current_sentences = overlap_sentences
                current_length = overlap_length

            current_sentences.append(sentence)
            current_length += sentence_len

        # Последний чанк
        if current_sentences:
            content = " ".join(current_sentences)
            chunk = Chunk.create(
                content=content,
                source_id=source_id,
                chunk_type=ChunkType.TEXT,
                position=position,
                start_char=start_char,
                end_char=start_char + len(content)
            )
            chunks.append(chunk)

        return chunks

    def split_by_speaker(
        self,
        text: str,
        source_id: str,
        merge_short: bool = True
    ) -> List[Chunk]:
        """
        Разбить по спикерам (для транскриптов).

        Args:
            text: Транскрипт
            source_id: ID источника
            merge_short: Объединять короткие реплики одного спикера

        Returns:
            Список чанков
        """
        # Паттерн для определения спикера
        speaker_pattern = r'^([А-ЯA-Z][а-яa-zА-ЯA-Z\s]+)\s*:\s*(.*)$'

        lines = text.split('\n')
        turns = []
        current_speaker = None
        current_text = []

        for line in lines:
            line = line.strip()
            if not line:
                continue

            match = re.match(speaker_pattern, line)
            if match:
                # Сохраняем предыдущую реплику
                if current_speaker and current_text:
                    turns.append({
                        "speaker": current_speaker,
                        "text": " ".join(current_text)
                    })

                current_speaker = match.group(1).strip()
                current_text = [match.group(2).strip()] if match.group(2).strip() else []
            else:
                # Продолжение текущей реплики
                if current_speaker:
                    current_text.append(line)

        # Последняя реплика
        if current_speaker and current_text:
            turns.append({
                "speaker": current_speaker,
                "text": " ".join(current_text)
            })

        # Объединяем короткие реплики одного спикера
        if merge_short:
            turns = self._merge_short_turns(turns)

        # Создаём чанки
        chunks = []
        for i, turn in enumerate(turns):
            content = f"{turn['speaker']}: {turn['text']}"

            # Если реплика слишком длинная, разбиваем
            if len(content) > self.config.chunk_size * 2:
                sub_chunks = self.split_by_length(turn['text'], source_id)
                for j, sub_chunk in enumerate(sub_chunks):
                    sub_chunk.speaker = turn['speaker']
                    sub_chunk.type = ChunkType.SPEAKER_TURN
                    sub_chunk.position = i * 100 + j
                    sub_chunk.name = f"{turn['speaker']} (часть {j+1})"
                    chunks.append(sub_chunk)
            else:
                chunk = Chunk.create(
                    content=content,
                    source_id=source_id,
                    name=f"{turn['speaker']}",
                    chunk_type=ChunkType.SPEAKER_TURN,
                    position=i,
                    speaker=turn['speaker']
                )
                chunks.append(chunk)

        return chunks

    def _merge_short_turns(
        self,
        turns: List[Dict[str, str]],
        min_length: int = 100
    ) -> List[Dict[str, str]]:
        """Объединить короткие реплики одного спикера."""
        if not turns:
            return turns

        merged = []
        current = turns[0].copy()

        for turn in turns[1:]:
            # Если тот же спикер и текущая реплика короткая
            if turn['speaker'] == current['speaker'] and len(current['text']) < min_length:
                current['text'] += " " + turn['text']
            else:
                merged.append(current)
                current = turn.copy()

        merged.append(current)
        return merged

    def split_by_structure(
        self,
        text: str,
        source_id: str
    ) -> List[Chunk]:
        """
        Разбить по структуре документа (заголовки, секции).

        Args:
            text: Текст документа
            source_id: ID источника

        Returns:
            Список чанков
        """
        # Паттерны для заголовков
        patterns = [
            r'^(#{1,6})\s+(.+)$',           # Markdown headers
            r'^(\d+\.(?:\d+\.)*)\s+(.+)$',  # Numbered sections
            r'^([A-ZА-ЯЁ][A-ZА-ЯЁ\s]{2,})$',  # ALL CAPS headers
        ]

        lines = text.split('\n')
        sections = []
        current_header = "Введение"
        current_content = []

        for line in lines:
            line_stripped = line.strip()
            is_header = False

            for pattern in patterns:
                match = re.match(pattern, line_stripped)
                if match:
                    # Сохраняем предыдущую секцию
                    if current_content:
                        sections.append({
                            "header": current_header,
                            "content": "\n".join(current_content)
                        })

                    # Новая секция
                    if len(match.groups()) >= 2:
                        current_header = match.group(2).strip()
                    else:
                        current_header = line_stripped
                    current_content = []
                    is_header = True
                    break

            if not is_header and line_stripped:
                current_content.append(line)

        # Последняя секция
        if current_content:
            sections.append({
                "header": current_header,
                "content": "\n".join(current_content)
            })

        # Создаём чанки
        chunks = []
        for i, section in enumerate(sections):
            content = section['content']

            # Если секция слишком длинная, разбиваем
            if len(content) > self.config.chunk_size * 2:
                sub_chunks = self.split_by_length(content, source_id)
                for j, sub_chunk in enumerate(sub_chunks):
                    sub_chunk.topic = section['header']
                    sub_chunk.position = i * 100 + j
                    sub_chunk.name = f"{section['header']} (часть {j+1})"
                    chunks.append(sub_chunk)
            else:
                chunk = Chunk.create(
                    content=content,
                    source_id=source_id,
                    name=section['header'],
                    chunk_type=ChunkType.TOPIC_SEGMENT,
                    position=i,
                    topic=section['header']
                )
                chunks.append(chunk)

        return chunks

    async def split_by_topic(
        self,
        text: str,
        source_id: str
    ) -> List[Chunk]:
        """
        Разбить по темам через LLM.

        Args:
            text: Исходный текст
            source_id: ID источника

        Returns:
            Список чанков
        """
        if not self.llm:
            logger.warning("⚠️ LLM not available, falling back to length splitting")
            return self.split_by_length(text, source_id)

        try:
            # Запрос к LLM для определения тем
            prompt = f"""Проанализируй текст и раздели его на тематические сегменты.

Для каждого сегмента укажи:
1. Название темы (краткое)
2. Начальную позицию в тексте (номер символа)
3. Краткое описание

Формат ответа (JSON):
{{
    "segments": [
        {{"topic": "Название темы", "start_position": 0, "description": "Описание"}},
        ...
    ]
}}

ТЕКСТ:
{text[:5000]}
"""

            # Трекинг использования LLM для chunking
            async with UsageContext(agent_mode="chunking", request_type="topic_segmentation"):
                response = await self.llm.generate(prompt)

            # Парсим ответ
            import json
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
                segments = result.get("segments", [])

                # Создаём чанки
                chunks = []
                for i, segment in enumerate(segments):
                    start = segment.get("start_position", 0)
                    end = segments[i + 1].get("start_position", len(text)) if i + 1 < len(segments) else len(text)

                    content = text[start:end].strip()
                    if content:
                        chunk = Chunk.create(
                            content=content,
                            source_id=source_id,
                            name=segment.get("topic", f"Тема {i+1}"),
                            chunk_type=ChunkType.TOPIC_SEGMENT,
                            position=i,
                            topic=segment.get("topic"),
                            start_char=start,
                            end_char=end,
                            metadata={"description": segment.get("description", "")}
                        )
                        chunks.append(chunk)

                return chunks

        except Exception as e:
            logger.error(f"❌ Topic splitting failed: {e}")

        # Fallback
        return self.split_by_length(text, source_id)

    def _split_sentences(self, text: str) -> List[str]:
        """Разбить текст на предложения."""
        # Разделители предложений
        delimiters = self.config.sentence_delimiters
        max_length = self.config.max_sentence_length

        sentences = []
        current = []

        for char in text:
            current.append(char)

            if char in delimiters:
                sentence = "".join(current).strip()
                if sentence:
                    # Если предложение слишком длинное, разбиваем принудительно
                    if len(sentence) > max_length:
                        sentences.extend(self._force_split(sentence, max_length))
                    else:
                        sentences.append(sentence)
                current = []
            elif len(current) >= max_length:
                # Принудительное разбиение
                sentence = "".join(current).strip()
                sentences.extend(self._force_split(sentence, max_length))
                current = []

        # Остаток
        if current:
            sentence = "".join(current).strip()
            if sentence:
                if len(sentence) > max_length:
                    sentences.extend(self._force_split(sentence, max_length))
                else:
                    sentences.append(sentence)

        return sentences

    def _force_split(self, text: str, max_length: int) -> List[str]:
        """Принудительное разбиение по длине."""
        result = []
        for i in range(0, len(text), max_length):
            chunk = text[i:i + max_length].strip()
            if chunk:
                result.append(chunk)
        return result


