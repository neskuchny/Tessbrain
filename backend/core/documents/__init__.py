# -*- coding: utf-8 -*-
"""
TESSENT BRAIN - Documents Module
Модуль работы с документами
"""

from backend.core.documents.document_indexer import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    DocumentIndexer,
    split_text_into_chunks,
    sync_documents_to_knowledge,
)

# Импортируем систему генерации документов если она есть
try:
    from backend.core.documents.document_generator import DocumentGenerationSystem
except ImportError as e:
    import logging
    logging.getLogger(__name__).warning(f"Could not import DocumentGenerationSystem: {e}")
    DocumentGenerationSystem = None

__all__ = [
    "DEFAULT_CHUNK_OVERLAP",
    "DEFAULT_CHUNK_SIZE",
    "DocumentGenerationSystem",
    "DocumentIndexer",
    "split_text_into_chunks",
    "sync_documents_to_knowledge",
]
