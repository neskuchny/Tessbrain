# -*- coding: utf-8 -*-
"""
Chunking Module - Модуль разбиения текста на чанки.

Вдохновлено KAG (Knowledge Augmented Generation).

Компоненты:
- Chunk: Модель данных чанка
- SemanticSplitter: Семантическое разбиение текста
- KnowledgeExtractor: Извлечение структурированных знаний
"""

from .chunk_model import Chunk, ChunkType
from .knowledge_extractor import KnowledgeExtractor, KnowledgeItem, KnowledgeType
from .semantic_splitter import SemanticSplitter, SplitterConfig

__all__ = [
    "Chunk",
    "ChunkType",
    "KnowledgeExtractor",
    "KnowledgeItem",
    "KnowledgeType",
    "SemanticSplitter",
    "SplitterConfig",
]
