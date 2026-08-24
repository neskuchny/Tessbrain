# -*- coding: utf-8 -*-
"""
TESSENT BRAIN - Chunking Service
Сервис для интеллектуального разбиения текста на смысловые фрагменты (чанки).
Используется для встреч и документов.
"""
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Настройки по умолчанию
DEFAULT_CHUNK_SIZE = 1500  # символов (~300-400 слов)
DEFAULT_CHUNK_OVERLAP = 200  # символов перекрытия

class ChunkingService:
    """Сервис для разбиения текста на чанки."""

    @staticmethod
    def split_text(
        text: str,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        overlap: int = DEFAULT_CHUNK_OVERLAP,
        metadata: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        Разбить текст на чанки с перекрытием, стараясь не разрывать предложения.

        Args:
            text: Исходный текст
            chunk_size: Желаемый размер чанка
            overlap: Размер перекрытия
            metadata: Общие метаданные для всех чанков

        Returns:
            Список словарей с данными чанков:
            {
                "index": int,
                "text": str,
                "char_start": int,
                "char_end": int,
                "metadata": dict
            }
        """
        if not text:
            return []

        text = text.strip()
        if len(text) <= chunk_size:
            return [{
                "index": 0,
                "text": text,
                "char_start": 0,
                "char_end": len(text),
                "metadata": metadata or {}
            }]

        chunks = []
        start = 0
        chunk_index = 0

        while start < len(text):
            # Предварительный конец чанка
            end = min(start + chunk_size, len(text))

            # Если это не конец текста, ищем лучшую точку разрыва
            if end < len(text):
                # Ищем разрывы в обратном порядке от конца чанка
                # Приоритет:
                # 1. Двойной перенос строки (абзац)
                # 2. Конец предложения (. ! ?)
                # 3. Перенос строки
                # 4. Пробел

                search_window_start = max(start + chunk_size // 2, end - overlap * 2)
                block_to_search = text[search_window_start:end]

                # Ищем абзац
                paragraph_break = block_to_search.rfind('\n\n')
                if paragraph_break != -1:
                    best_break = search_window_start + paragraph_break + 2
                else:
                    # Ищем конец предложения
                    sentence_break = -1
                    for sep in ['. ', '! ', '? ', '.\n', '!\n', '?\n']:
                        idx = block_to_search.rfind(sep)
                        if idx > sentence_break:
                            sentence_break = idx + len(sep)

                    if sentence_break != -1:
                        best_break = search_window_start + sentence_break
                    else:
                        # Ищем перенос строки
                        line_break = block_to_search.rfind('\n')
                        if line_break != -1:
                            best_break = search_window_start + line_break + 1
                        else:
                            # Ищем пробел
                            space_break = block_to_search.rfind(' ')
                            if space_break != -1:
                                best_break = search_window_start + space_break + 1
                            else:
                                # Если ничего не нашли - режем жестко
                                best_break = end

                end = best_break

            chunk_text = text[start:end].strip()

            if chunk_text:
                chunks.append({
                    "index": chunk_index,
                    "text": chunk_text,
                    "char_start": start,
                    "char_end": end,
                    "metadata": metadata or {}
                })
                chunk_index += 1

            # Следующий чанк начинается с учетом overlap, но не раньше текущего start + 1
            if end >= len(text):
                break

            # Отступаем назад на overlap от конца текущего чанка
            next_start = max(start + 1, end - overlap)

            # Корректируем начало следующего чанка, чтобы не разрывать слова (ищем пробел влево)
            if next_start < len(text) and text[next_start] != ' ' and text[next_start-1] != ' ':
                # Ищем ближайший пробел слева
                search_back_limit = max(start, next_start - 50)
                space_before = text.rfind(' ', search_back_limit, next_start)
                if space_before != -1:
                    next_start = space_before + 1

            start = next_start

        return chunks

    @staticmethod
    def find_chunk_for_quote(quote: str, chunks: List[Dict[str, Any]]) -> Optional[int]:
        """
        Найти индекс чанка, который содержит данную цитату.

        Args:
            quote: Текст цитаты (может быть неточным)
            chunks: Список чанков

        Returns:
            Индекс чанка или None
        """
        if not quote:
            return None

        # Нормализация для поиска (убираем лишние пробелы, пунктуацию)
        def normalize(s):
            return ' '.join(re.findall(r'\w+', s.lower()))

        norm_quote = normalize(quote)
        if len(norm_quote) < 10: # Слишком короткая цитата
            return None


        for chunk in chunks:
            norm_chunk = normalize(chunk["text"])

            # Точное вхождение
            if norm_quote in norm_chunk:
                return chunk["index"]

            # Если точного нет, ищем частичное (например, начало цитаты)
            # Это простая эвристика. Для точности можно использовать fuzzywuzzy,
            # но это медленно.

            # Пробуем найти первые 50 символов цитаты
            quote_start = norm_quote[:min(len(norm_quote), 50)]
            if quote_start in norm_chunk:
                return chunk["index"]

        return None

