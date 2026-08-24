# -*- coding: utf-8 -*-
"""
BM25 Searcher - Лексический поиск по ключевым словам.

BM25 (Best Matching 25) - вероятностный алгоритм ранжирования,
который учитывает:
- TF (Term Frequency) - частоту терма в документе
- IDF (Inverse Document Frequency) - редкость терма в коллекции
- Длину документа (нормализация)

Используется для:
- Точного поиска по ключевым словам
- Поиска названий, имён, технических терминов
- Дополнение векторного поиска в Hybrid Search
"""
import logging
import math
import pickle
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class BM25Document:
    """Документ для BM25 индекса"""
    doc_id: str
    text: str
    tokens: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.tokens and self.text:
            self.tokens = tokenize_russian(self.text)


@dataclass
class BM25SearchResult:
    """Результат BM25 поиска"""
    doc_id: str
    score: float
    text: str
    metadata: Dict[str, Any]
    matched_terms: List[str] = field(default_factory=list)


# Стоп-слова для русского языка
RUSSIAN_STOPWORDS = {
    "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а", "то", "все",
    "она", "так", "его", "но", "да", "ты", "к", "у", "же", "вы", "за", "бы", "по",
    "только", "её", "мне", "было", "вот", "от", "меня", "ещё", "нет", "о", "из",
    "ему", "теперь", "когда", "уже", "вам", "ни", "быть", "был", "него", "до",
    "вас", "нибудь", "опять", "уж", "ведь", "там", "потом", "себя",
    "ничего", "ей", "может", "они", "тут", "где", "есть", "надо", "ней", "для",
    "мы", "тебя", "их", "чем", "была", "сам", "чтоб", "без", "будто", "чего",
    "раз", "тоже", "себе", "под", "будет", "ж", "тогда", "кто", "этот", "того",
    "потому", "этого", "какой", "совсем", "ним", "здесь", "этом", "один",
    "почти", "мой", "тем", "чтобы", "нее", "сейчас", "были", "куда", "зачем",
    "всех", "никогда", "можно", "при", "наконец", "два", "об", "другой", "хоть",
    "после", "над", "больше", "тот", "через", "эти", "нас", "про", "всего",
    "них", "какая", "много", "разве", "три", "эту", "моя", "впрочем", "хорошо",
    "свою", "этой", "перед", "иногда", "лучше", "чуть", "том", "нельзя",
    "такой", "им", "более", "всегда", "конечно", "всю", "между", "это",
}

# Английские стоп-слова
ENGLISH_STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "is", "was", "are", "were", "been",
    "be", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "must", "shall", "can", "need",
    "dare", "ought", "used", "it", "its", "this", "that", "these", "those",
    "i", "you", "he", "she", "we", "they", "me", "him", "her", "us", "them",
    "my", "your", "his", "our", "their", "mine", "yours", "hers", "ours",
    "what", "which", "who", "whom", "whose", "where", "when", "why", "how",
    "all", "each", "every", "both", "few", "more", "most", "other", "some",
    "such", "no", "nor", "not", "only", "own", "same", "so", "than", "too",
    "very", "just", "also", "now", "here", "there", "then", "once", "any",
}

ALL_STOPWORDS = RUSSIAN_STOPWORDS | ENGLISH_STOPWORDS


def tokenize_russian(text: str, remove_stopwords: bool = True) -> List[str]:
    """
    Токенизация текста для русского и английского языков.

    Args:
        text: Исходный текст
        remove_stopwords: Удалять стоп-слова

    Returns:
        Список токенов
    """
    if not text:
        return []

    # Приводим к нижнему регистру
    text = text.lower()

    # Удаляем спецсимволы, оставляем буквы, цифры и пробелы
    text = re.sub(r'[^\w\s]', ' ', text)

    # Разбиваем на токены
    tokens = text.split()

    # Фильтруем
    filtered = []
    for token in tokens:
        # Пропускаем короткие токены
        if len(token) < 2:
            continue

        # Пропускаем чисто цифровые токены (если не важны)
        # if token.isdigit():
        #     continue

        # Удаляем стоп-слова
        if remove_stopwords and token in ALL_STOPWORDS:
            continue

        filtered.append(token)

    return filtered


class BM25Searcher:
    """
    BM25 поисковик для лексического поиска.

    Параметры BM25:
    - k1: насыщение TF (1.2-2.0, default 1.5)
    - b: нормализация по длине документа (0-1, default 0.75)

    Использование:
    ```python
    searcher = BM25Searcher()

    # Индексация
    searcher.add_document("doc1", "Текст документа", {"type": "meeting"})

    # Поиск
    results = searcher.search("ключевые слова", top_k=10)
    ```
    """

    def __init__(
        self,
        k1: float = 1.5,
        b: float = 0.75,
        storage_path: Optional[str] = None
    ):
        """
        Args:
            k1: Параметр насыщения TF (1.2-2.0)
            b: Параметр нормализации по длине (0-1)
            storage_path: Путь для сохранения индекса
        """
        self.k1 = k1
        self.b = b
        self.storage_path = Path(storage_path) if storage_path else None

        # Индекс документов
        self.documents: Dict[str, BM25Document] = {}

        # Инвертированный индекс: term -> {doc_id: term_frequency}
        self.inverted_index: Dict[str, Dict[str, int]] = {}

        # IDF кеш
        self.idf_cache: Dict[str, float] = {}

        # Средняя длина документа
        self.avg_doc_length: float = 0.0

        # Статистика
        self.total_docs: int = 0
        self.total_terms: int = 0

        # Загружаем индекс если существует
        if self.storage_path and self.storage_path.exists():
            self._load_index()

    def add_document(
        self,
        doc_id: str,
        text: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Добавить документ в индекс.

        Args:
            doc_id: Уникальный ID документа
            text: Текст документа
            metadata: Метаданные (тип, источник и т.д.)
        """
        if not text or not doc_id:
            return

        # Токенизируем
        tokens = tokenize_russian(text)

        if not tokens:
            return

        # Создаём документ
        doc = BM25Document(
            doc_id=doc_id,
            text=text,
            tokens=tokens,
            metadata=metadata or {}
        )

        # Удаляем старый документ если существует
        if doc_id in self.documents:
            self._remove_from_index(doc_id)

        # Добавляем в индекс
        self.documents[doc_id] = doc

        # Обновляем инвертированный индекс
        term_counts = Counter(tokens)
        for term, count in term_counts.items():
            if term not in self.inverted_index:
                self.inverted_index[term] = {}
            self.inverted_index[term][doc_id] = count

        # Обновляем статистику
        self.total_docs = len(self.documents)
        self.total_terms = sum(len(d.tokens) for d in self.documents.values())
        self.avg_doc_length = self.total_terms / self.total_docs if self.total_docs > 0 else 0

        # Инвалидируем IDF кеш
        self.idf_cache.clear()

    def add_documents_batch(
        self,
        documents: List[Tuple[str, str, Optional[Dict[str, Any]]]]
    ) -> int:
        """
        Добавить несколько документов.

        Args:
            documents: Список (doc_id, text, metadata)

        Returns:
            Количество добавленных документов
        """
        added = 0
        for doc_id, text, metadata in documents:
            if text and doc_id:
                self.add_document(doc_id, text, metadata)
                added += 1

        logger.info(f"📚 BM25: добавлено {added} документов, всего {self.total_docs}")
        return added

    def _remove_from_index(self, doc_id: str) -> None:
        """Удалить документ из индекса"""
        if doc_id not in self.documents:
            return

        doc = self.documents[doc_id]

        # Удаляем из инвертированного индекса
        for term in set(doc.tokens):
            if term in self.inverted_index:
                self.inverted_index[term].pop(doc_id, None)
                if not self.inverted_index[term]:
                    del self.inverted_index[term]

        del self.documents[doc_id]

    def _compute_idf(self, term: str) -> float:
        """
        Вычислить IDF для терма.

        IDF = log((N - n + 0.5) / (n + 0.5))
        где N - общее число документов, n - число документов с термом
        """
        if term in self.idf_cache:
            return self.idf_cache[term]

        n = len(self.inverted_index.get(term, {}))
        N = self.total_docs

        # BM25 IDF формула
        idf = math.log((N - n + 0.5) / (n + 0.5) + 1)

        self.idf_cache[term] = idf
        return idf

    def _score_document(
        self,
        doc_id: str,
        query_terms: List[str]
    ) -> Tuple[float, List[str]]:
        """
        Вычислить BM25 score для документа.

        Score = sum(IDF(qi) * (f(qi, D) * (k1 + 1)) / (f(qi, D) + k1 * (1 - b + b * |D| / avgdl)))

        Returns:
            (score, matched_terms)
        """
        if doc_id not in self.documents:
            return 0.0, []

        doc = self.documents[doc_id]
        doc_length = len(doc.tokens)

        score = 0.0
        matched_terms = []

        for term in query_terms:
            if term not in self.inverted_index:
                continue

            if doc_id not in self.inverted_index[term]:
                continue

            # Term frequency в документе
            tf = self.inverted_index[term][doc_id]

            # IDF
            idf = self._compute_idf(term)

            # BM25 формула
            numerator = tf * (self.k1 + 1)
            denominator = tf + self.k1 * (1 - self.b + self.b * doc_length / self.avg_doc_length)

            term_score = idf * (numerator / denominator)
            score += term_score
            matched_terms.append(term)

        return score, matched_terms

    def search(
        self,
        query: str,
        top_k: int = 10,
        min_score: float = 0.0,
        filters: Optional[Dict[str, Any]] = None
    ) -> List[BM25SearchResult]:
        """
        Поиск по запросу.

        Args:
            query: Поисковый запрос
            top_k: Количество результатов
            min_score: Минимальный score
            filters: Фильтры по метаданным

        Returns:
            Список результатов отсортированных по score
        """
        if not query or not self.documents:
            return []

        # Токенизируем запрос
        query_terms = tokenize_russian(query)

        if not query_terms:
            return []

        # Находим документы с хотя бы одним термом
        candidate_docs = set()
        for term in query_terms:
            if term in self.inverted_index:
                candidate_docs.update(self.inverted_index[term].keys())

        if not candidate_docs:
            return []

        # Вычисляем scores
        results = []
        for doc_id in candidate_docs:
            doc = self.documents[doc_id]

            # Применяем фильтры (список = membership, OR-группа между
            # списочными ключами — контракт чата «эти встречи ИЛИ документы»;
            # скаляры — прежний AND-equality)
            if filters:
                from backend.core.search.filter_match import (
                    metadata_matches_filters)
                if not metadata_matches_filters(doc.metadata, filters):
                    continue

            score, matched_terms = self._score_document(doc_id, query_terms)

            if score >= min_score:
                results.append(BM25SearchResult(
                    doc_id=doc_id,
                    score=score,
                    text=doc.text,
                    metadata=doc.metadata,
                    matched_terms=matched_terms
                ))

        # Сортируем по score
        results.sort(key=lambda x: x.score, reverse=True)

        return results[:top_k]

    def search_with_boost(
        self,
        query: str,
        top_k: int = 10,
        field_boosts: Optional[Dict[str, float]] = None
    ) -> List[BM25SearchResult]:
        """
        Поиск с бустами по полям метаданных.

        Args:
            query: Поисковый запрос
            top_k: Количество результатов
            field_boosts: {"type": {"meeting": 1.5, "task": 1.2}}

        Returns:
            Список результатов с применёнными бустами
        """
        results = self.search(query, top_k=top_k * 2)  # Берём больше для фильтрации

        if not field_boosts:
            return results[:top_k]

        # Применяем бусты
        for result in results:
            boost = 1.0
            for field, values in field_boosts.items():
                field_value = result.metadata.get(field)
                if field_value and field_value in values:
                    boost *= values[field_value]
            result.score *= boost

        # Пересортируем
        results.sort(key=lambda x: x.score, reverse=True)

        return results[:top_k]

    def get_stats(self) -> Dict[str, Any]:
        """Получить статистику индекса"""
        return {
            "total_documents": self.total_docs,
            "total_terms": self.total_terms,
            "unique_terms": len(self.inverted_index),
            "avg_doc_length": round(self.avg_doc_length, 2),
            "k1": self.k1,
            "b": self.b
        }

    def save_index(self, path: Optional[str] = None) -> None:
        """Сохранить индекс на диск"""
        save_path = Path(path) if path else self.storage_path

        if not save_path:
            logger.warning("⚠️ BM25: путь для сохранения не указан")
            return

        save_path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "k1": self.k1,
            "b": self.b,
            "documents": self.documents,
            "inverted_index": self.inverted_index,
            "avg_doc_length": self.avg_doc_length,
            "total_docs": self.total_docs,
            "total_terms": self.total_terms
        }

        with open(save_path, "wb") as f:
            pickle.dump(data, f)

        logger.info(f"💾 BM25: индекс сохранён ({self.total_docs} документов)")

    def _load_index(self) -> None:
        """Загрузить индекс с диска"""
        if not self.storage_path or not self.storage_path.exists():
            return

        try:
            with open(self.storage_path, "rb") as f:
                data = pickle.load(f)

            self.k1 = data.get("k1", self.k1)
            self.b = data.get("b", self.b)
            self.documents = data.get("documents", {})
            self.inverted_index = data.get("inverted_index", {})
            self.avg_doc_length = data.get("avg_doc_length", 0.0)
            self.total_docs = data.get("total_docs", 0)
            self.total_terms = data.get("total_terms", 0)

            logger.info(f"📂 BM25: индекс загружен ({self.total_docs} документов)")

        except Exception as e:
            logger.error(f"❌ BM25: ошибка загрузки индекса: {e}")

    def clear(self) -> None:
        """Очистить индекс"""
        self.documents.clear()
        self.inverted_index.clear()
        self.idf_cache.clear()
        self.avg_doc_length = 0.0
        self.total_docs = 0
        self.total_terms = 0


# Singleton instance
_bm25_instance: Optional[BM25Searcher] = None
# Кэш per-user индексов на процесс (см. get_bm25_searcher).
_bm25_by_user: dict = {}


def get_bm25_searcher(
    storage_path: Optional[str] = None,
    user_id: Optional[str] = None
) -> BM25Searcher:
    """
    Получить BM25 searcher (singleton или per-user).

    Args:
        storage_path: Путь для хранения индекса
        user_id: ID пользователя для per-user индекса

    Returns:
        BM25Searcher instance
    """
    global _bm25_instance

    if user_id:
        # Per-user индекс — КЭШИРУЕМ на процесс. Раньше каждый вызов создавал
        # новый BM25Searcher и распиковывал весь индекс (24k+ документов) с
        # диска заново — на каждый чат-запрос. Индекс мутируется in-place
        # (add_document) и явно сохраняется save_index() — общий инстанс
        # это сохраняет корректным.
        cached = _bm25_by_user.get(user_id)
        if cached is not None:
            return cached
        from backend.core.store.tenant_paths import bm25_index_path_for_user
        path = bm25_index_path_for_user(user_id)
        inst = BM25Searcher(storage_path=path)
        _bm25_by_user[user_id] = inst
        return inst

    if _bm25_instance is None:
        _bm25_instance = BM25Searcher(storage_path=storage_path)

    return _bm25_instance

