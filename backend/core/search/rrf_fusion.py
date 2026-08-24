# -*- coding: utf-8 -*-
"""
RRF Fusion - Reciprocal Rank Fusion для объединения результатов поиска.

RRF - это метод слияния ранжированных списков, который:
- Не зависит от абсолютных значений score (только от рангов)
- Хорошо работает с разными типами поиска (vector + BM25)
- Устойчив к выбросам и различиям в масштабах оценок

Формула: RRF(d) = Σ 1 / (k + rank_i(d))
где k - константа (обычно 60), rank_i(d) - ранг документа d в списке i

Также поддерживает:
- Weighted RRF - с весами для каждого списка
- Score-based fusion - с учётом нормализованных scores
"""
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)


@dataclass
class FusedResult:
    """Результат после fusion"""
    doc_id: str
    rrf_score: float
    original_scores: Dict[str, float] = field(default_factory=dict)
    original_ranks: Dict[str, int] = field(default_factory=dict)
    data: Dict[str, Any] = field(default_factory=dict)
    sources: List[str] = field(default_factory=list)


class RRFFusion:
    """
    Reciprocal Rank Fusion для объединения результатов поиска.

    Использование:
    ```python
    fusion = RRFFusion(k=60)

    # Добавляем результаты из разных источников
    fusion.add_results("vector", vector_results, weight=1.0)
    fusion.add_results("bm25", bm25_results, weight=0.8)

    # Получаем объединённые результаты
    fused = fusion.fuse(top_k=20)
    ```
    """

    def __init__(self, k: int = 60):
        """
        Args:
            k: Константа RRF (обычно 60). Больше k = меньше влияние высоких рангов.
        """
        self.k = k

        # Результаты по источникам: {source: [(doc_id, score, data), ...]}
        self.results_by_source: Dict[str, List[Tuple[str, float, Dict]]] = {}

        # Веса источников
        self.weights: Dict[str, float] = {}

        # Все документы: {doc_id: data}
        self.all_docs: Dict[str, Dict[str, Any]] = {}

    def add_results(
        self,
        source: str,
        results: List[Dict[str, Any]],
        weight: float = 1.0,
        id_field: str = "id",
        score_field: str = "score"
    ) -> None:
        """
        Добавить результаты из источника.

        Args:
            source: Название источника (например, "vector", "bm25")
            results: Список результатов [{id: ..., score: ..., ...}, ...]
            weight: Вес источника (1.0 = стандартный)
            id_field: Название поля с ID документа
            score_field: Название поля со score
        """
        if not results:
            return

        processed = []
        for result in results:
            # Извлекаем ID
            doc_id = result.get(id_field) or result.get("doc_id") or result.get("id")
            if not doc_id:
                continue

            # Извлекаем score
            score = result.get(score_field, 0.0)
            if isinstance(score, str):
                try:
                    score = float(score)
                except ValueError:
                    score = 0.0

            # Сохраняем данные документа
            if doc_id not in self.all_docs:
                self.all_docs[doc_id] = result.copy()
            else:
                # Объединяем данные из разных источников
                self.all_docs[doc_id].update(result)

            processed.append((doc_id, score, result))

        # Сортируем по score (убывание) для определения рангов
        processed.sort(key=lambda x: x[1], reverse=True)

        self.results_by_source[source] = processed
        self.weights[source] = weight

        logger.debug(f"📊 RRF: добавлено {len(processed)} результатов из '{source}' (weight={weight})")

    def fuse(
        self,
        top_k: int = 20,
        min_sources: int = 1,
        normalize_scores: bool = False
    ) -> List[FusedResult]:
        """
        Выполнить fusion и вернуть объединённые результаты.

        Args:
            top_k: Количество результатов
            min_sources: Минимальное количество источников для документа
            normalize_scores: Нормализовать финальные scores в [0, 1]

        Returns:
            Список FusedResult отсортированный по rrf_score
        """
        if not self.results_by_source:
            return []

        # Вычисляем RRF score для каждого документа
        rrf_scores: Dict[str, float] = defaultdict(float)
        original_scores: Dict[str, Dict[str, float]] = defaultdict(dict)
        original_ranks: Dict[str, Dict[str, int]] = defaultdict(dict)
        doc_sources: Dict[str, List[str]] = defaultdict(list)

        for source, results in self.results_by_source.items():
            weight = self.weights.get(source, 1.0)

            for rank, (doc_id, score, _) in enumerate(results, start=1):
                # RRF формула: 1 / (k + rank)
                rrf_contribution = weight / (self.k + rank)
                rrf_scores[doc_id] += rrf_contribution

                original_scores[doc_id][source] = score
                original_ranks[doc_id][source] = rank
                doc_sources[doc_id].append(source)

        # Фильтруем по минимальному количеству источников
        filtered_docs = [
            doc_id for doc_id, sources in doc_sources.items()
            if len(sources) >= min_sources
        ]

        # Создаём результаты
        fused_results = []
        for doc_id in filtered_docs:
            fused_results.append(FusedResult(
                doc_id=doc_id,
                rrf_score=rrf_scores[doc_id],
                original_scores=dict(original_scores[doc_id]),
                original_ranks=dict(original_ranks[doc_id]),
                data=self.all_docs.get(doc_id, {}),
                sources=doc_sources[doc_id]
            ))

        # Сортируем по RRF score
        fused_results.sort(key=lambda x: x.rrf_score, reverse=True)

        # Нормализуем scores если нужно
        if normalize_scores and fused_results:
            max_score = fused_results[0].rrf_score
            if max_score > 0:
                for result in fused_results:
                    result.rrf_score /= max_score

        logger.info(
            f"🔀 RRF Fusion: {len(filtered_docs)} документов из "
            f"{len(self.results_by_source)} источников"
        )

        return fused_results[:top_k]

    def fuse_with_score_interpolation(
        self,
        top_k: int = 20,
        rrf_weight: float = 0.7,
        score_weight: float = 0.3
    ) -> List[FusedResult]:
        """
        Fusion с интерполяцией RRF и нормализованных scores.

        Финальный score = rrf_weight * RRF + score_weight * avg_normalized_score

        Args:
            top_k: Количество результатов
            rrf_weight: Вес RRF компоненты
            score_weight: Вес score компоненты

        Returns:
            Список FusedResult
        """
        # Сначала получаем RRF результаты
        rrf_results = self.fuse(top_k=top_k * 2, normalize_scores=True)

        if not rrf_results:
            return []

        # Нормализуем scores по источникам
        normalized_scores_by_source: Dict[str, Dict[str, float]] = {}

        for source, results in self.results_by_source.items():
            if not results:
                continue

            scores = [r[1] for r in results]
            max_score = max(scores) if scores else 1.0
            min_score = min(scores) if scores else 0.0
            score_range = max_score - min_score if max_score != min_score else 1.0

            normalized_scores_by_source[source] = {}
            for doc_id, score, _ in results:
                normalized = (score - min_score) / score_range
                normalized_scores_by_source[source][doc_id] = normalized

        # Вычисляем финальный score
        for result in rrf_results:
            # Средний нормализованный score по источникам
            norm_scores = []
            for source in result.sources:
                if source in normalized_scores_by_source:
                    ns = normalized_scores_by_source[source].get(result.doc_id, 0.0)
                    norm_scores.append(ns * self.weights.get(source, 1.0))

            avg_norm_score = sum(norm_scores) / len(norm_scores) if norm_scores else 0.0

            # Интерполяция
            result.rrf_score = (
                rrf_weight * result.rrf_score +
                score_weight * avg_norm_score
            )

        # Пересортируем
        rrf_results.sort(key=lambda x: x.rrf_score, reverse=True)

        return rrf_results[:top_k]

    def clear(self) -> None:
        """Очистить все результаты"""
        self.results_by_source.clear()
        self.weights.clear()
        self.all_docs.clear()

    def get_stats(self) -> Dict[str, Any]:
        """Получить статистику"""
        return {
            "sources": list(self.results_by_source.keys()),
            "weights": self.weights.copy(),
            "results_per_source": {
                source: len(results)
                for source, results in self.results_by_source.items()
            },
            "total_unique_docs": len(self.all_docs),
            "k": self.k
        }


def fuse_search_results(
    vector_results: List[Dict[str, Any]],
    bm25_results: List[Dict[str, Any]],
    vector_weight: float = 1.0,
    bm25_weight: float = 0.8,
    top_k: int = 20,
    k: int = 60
) -> List[FusedResult]:
    """
    Быстрая функция для fusion vector + BM25 результатов.

    Args:
        vector_results: Результаты векторного поиска
        bm25_results: Результаты BM25 поиска
        vector_weight: Вес векторного поиска
        bm25_weight: Вес BM25 поиска
        top_k: Количество результатов
        k: Константа RRF

    Returns:
        Объединённые результаты
    """
    fusion = RRFFusion(k=k)

    fusion.add_results("vector", vector_results, weight=vector_weight)
    fusion.add_results("bm25", bm25_results, weight=bm25_weight)

    return fusion.fuse(top_k=top_k)


def fuse_multiple_sources(
    sources: Dict[str, Tuple[List[Dict[str, Any]], float]],
    top_k: int = 20,
    k: int = 60,
    min_sources: int = 1
) -> List[FusedResult]:
    """
    Fusion для произвольного количества источников.

    Args:
        sources: {source_name: (results, weight)}
        top_k: Количество результатов
        k: Константа RRF
        min_sources: Минимум источников для документа

    Returns:
        Объединённые результаты
    """
    fusion = RRFFusion(k=k)

    for source_name, (results, weight) in sources.items():
        fusion.add_results(source_name, results, weight=weight)

    return fusion.fuse(top_k=top_k, min_sources=min_sources)

