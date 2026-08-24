# -*- coding: utf-8 -*-
"""
Metrics Collector - Сбор метрик системы Tessbrain.

Собирает метрики:
- Обработки встреч (время, количество сущностей)
- Поисковых запросов (время, релевантность)
- Агентской памяти (размер, использование)
- Графа знаний (узлы, рёбра, веса)
- Исправлений (количество, типы)

Использование:
```python
from backend.core.monitoring.metrics_collector import get_metrics_collector

metrics = get_metrics_collector()

# Записать метрику
metrics.record("meeting_processed", value=1, tags={"user_id": "user_1"})

# Получить статистику
stats = metrics.get_stats()
```
"""
import json
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class MetricEntry:
    """Запись метрики."""
    name: str
    value: float
    timestamp: str
    tags: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class MetricsCollector:
    """
    Сборщик метрик для системы.

    Поддерживает:
    - Счётчики (counters)
    - Измерители (gauges)
    - Гистограммы (histograms)
    - Временные ряды (timeseries)
    """

    # Предопределённые метрики
    METRICS = {
        # Обработка встреч
        "meeting_processed": "counter",
        "meeting_processing_time_ms": "histogram",
        "entities_extracted": "counter",
        "decisions_extracted": "counter",
        "tasks_extracted": "counter",

        # Поиск
        "search_requests": "counter",
        "search_time_ms": "histogram",
        "search_results_count": "histogram",

        # Память агентов
        "agent_memory_size": "gauge",
        "agent_memory_recalls": "counter",
        "agent_memory_remembers": "counter",

        # Граф
        "graph_nodes_total": "gauge",
        "graph_edges_total": "gauge",
        "graph_avg_node_weight": "gauge",
        "graph_avg_edge_weight": "gauge",

        # Hebbian Learning
        "hebbian_connections_strengthened": "counter",
        "temporal_decay_applied": "counter",

        # Исправления
        "corrections_recorded": "counter",
        "corrections_approved": "counter",
        "corrections_rejected": "counter",

        # Снапшоты
        "snapshot_updates": "counter",
        "snapshot_generation_time_ms": "histogram",

        # Ошибки
        "errors_total": "counter",
        "validation_failures": "counter",
    }

    def __init__(self, storage_path: str = "data/metrics"):
        """
        Args:
            storage_path: Путь для хранения метрик
        """
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)

        # Хранилище метрик в памяти
        self._counters: Dict[str, float] = defaultdict(float)
        self._gauges: Dict[str, float] = {}
        self._histograms: Dict[str, List[float]] = defaultdict(list)
        self._timeseries: List[MetricEntry] = []

        # Максимум записей в timeseries (за последние 24 часа)
        self._max_timeseries = 10000

        # Загружаем существующие данные
        self._load_metrics()

        logger.info(f"✅ MetricsCollector initialized: {storage_path}")

    def _load_metrics(self):
        """Загрузить метрики из файла."""
        counters_file = self.storage_path / "counters.json"
        if counters_file.exists():
            try:
                with open(counters_file, "r", encoding="utf-8") as f:
                    self._counters = defaultdict(float, json.load(f))
            except Exception as e:
                logger.warning(f"Could not load counters: {e}")

    def _save_metrics(self):
        """Сохранить метрики в файл."""
        counters_file = self.storage_path / "counters.json"
        try:
            with open(counters_file, "w", encoding="utf-8") as f:
                json.dump(dict(self._counters), f, indent=2)
        except Exception as e:
            logger.warning(f"Could not save counters: {e}")

    def record(
        self,
        name: str,
        value: float = 1.0,
        tags: Optional[Dict[str, str]] = None
    ):
        """
        Записать метрику.

        Args:
            name: Название метрики
            value: Значение
            tags: Теги для фильтрации
        """
        try:
            from backend.core.config import flags
            if not flags.enable_detailed_metrics:
                return
        except Exception:
            logger.debug("suppressed exception", exc_info=True)

        metric_type = self.METRICS.get(name, "counter")

        if metric_type == "counter":
            self._counters[name] += value
        elif metric_type == "gauge":
            self._gauges[name] = value
        elif metric_type == "histogram":
            self._histograms[name].append(value)
            # Ограничиваем размер гистограммы
            if len(self._histograms[name]) > 1000:
                self._histograms[name] = self._histograms[name][-1000:]

        # Записываем в timeseries
        entry = MetricEntry(
            name=name,
            value=value,
            timestamp=datetime.now(timezone.utc).isoformat(),
            tags=tags or {}
        )
        self._timeseries.append(entry)

        # Ограничиваем размер timeseries
        if len(self._timeseries) > self._max_timeseries:
            self._timeseries = self._timeseries[-self._max_timeseries:]

        logger.debug(f"📊 Metric recorded: {name}={value}")

    def increment(self, name: str, tags: Optional[Dict[str, str]] = None):
        """Увеличить счётчик на 1."""
        self.record(name, value=1.0, tags=tags)

    def gauge(self, name: str, value: float, tags: Optional[Dict[str, str]] = None):
        """Установить значение gauge."""
        self._gauges[name] = value
        self.record(name, value=value, tags=tags)

    def timing(self, name: str, duration_ms: float, tags: Optional[Dict[str, str]] = None):
        """Записать время выполнения."""
        self.record(name, value=duration_ms, tags=tags)

    def get_counter(self, name: str) -> float:
        """Получить значение счётчика."""
        return self._counters.get(name, 0.0)

    def get_gauge(self, name: str) -> Optional[float]:
        """Получить значение gauge."""
        return self._gauges.get(name)

    def get_histogram_stats(self, name: str) -> Dict[str, float]:
        """Получить статистику гистограммы."""
        values = self._histograms.get(name, [])
        if not values:
            return {"count": 0, "min": 0, "max": 0, "avg": 0, "p50": 0, "p95": 0, "p99": 0}

        sorted_values = sorted(values)
        count = len(sorted_values)

        def percentile(p: float) -> float:
            idx = int(count * p / 100)
            return sorted_values[min(idx, count - 1)]

        return {
            "count": count,
            "min": min(values),
            "max": max(values),
            "avg": sum(values) / count,
            "p50": percentile(50),
            "p95": percentile(95),
            "p99": percentile(99),
        }

    def get_stats(self) -> Dict[str, Any]:
        """Получить полную статистику."""
        stats = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "counters": dict(self._counters),
            "gauges": dict(self._gauges),
            "histograms": {},
        }

        for name in self._histograms:
            stats["histograms"][name] = self.get_histogram_stats(name)

        return stats

    def get_timeseries(
        self,
        name: Optional[str] = None,
        since: Optional[datetime] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Получить временной ряд метрик.

        Args:
            name: Фильтр по названию
            since: Начиная с времени
            limit: Максимум записей

        Returns:
            Список записей
        """
        result = self._timeseries

        if name:
            result = [e for e in result if e.name == name]

        if since:
            since_str = since.isoformat()
            result = [e for e in result if e.timestamp >= since_str]

        return [e.to_dict() for e in result[-limit:]]

    def reset(self):
        """Сбросить все метрики."""
        self._counters.clear()
        self._gauges.clear()
        self._histograms.clear()
        self._timeseries.clear()
        logger.info("🗑️ Metrics reset")

    def flush(self):
        """Сохранить метрики на диск."""
        self._save_metrics()
        logger.debug("💾 Metrics flushed to disk")


# Singleton
_metrics_collector: Optional[MetricsCollector] = None


def get_metrics_collector(storage_path: str = "data/metrics") -> MetricsCollector:
    """Получить экземпляр MetricsCollector."""
    global _metrics_collector
    if _metrics_collector is None:
        _metrics_collector = MetricsCollector(storage_path)
    return _metrics_collector


