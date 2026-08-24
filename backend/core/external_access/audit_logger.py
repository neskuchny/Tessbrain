# -*- coding: utf-8 -*-
"""
Audit Logger - Логирование всех внешних запросов.

Каждый запрос от подрядчика записывается для аудита и безопасности.
"""
import json
import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class AuditEntry:
    """Запись аудита."""
    id: str
    timestamp: str
    contractor_id: str
    contractor_type: str
    query: str
    categories_requested: List[str]
    categories_accessed: List[str]
    data_items_returned: int
    data_items_filtered: int
    response_size_bytes: int
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    duration_ms: int = 0
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class AuditLogger:
    """
    Логгер аудита для внешнего доступа.

    Функции:
    1. Запись всех запросов
    2. Хранение истории
    3. Генерация отчётов
    4. Обнаружение аномалий
    """

    def __init__(self, storage_path: str = "data/audit"):
        """
        Args:
            storage_path: Путь для хранения логов
        """
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)

        self._entries: List[AuditEntry] = []
        self._load_recent_entries()

    def _load_recent_entries(self, days: int = 7):
        """Загрузить недавние записи."""
        try:
            for log_file in self.storage_path.glob("audit_*.json"):
                with open(log_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for entry_data in data.get("entries", []):
                        self._entries.append(AuditEntry(**entry_data))
        except Exception as e:
            logger.error(f"Error loading audit entries: {e}")

    async def log(
        self,
        contractor_id: str,
        contractor_type: str,
        query: str,
        categories_requested: List[str],
        categories_accessed: List[str],
        data_items_returned: int,
        data_items_filtered: int,
        response_size_bytes: int,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        duration_ms: int = 0,
        error: Optional[str] = None
    ) -> AuditEntry:
        """
        Записать событие аудита.

        Returns:
            Созданная запись аудита
        """
        entry = AuditEntry(
            id=f"audit_{uuid.uuid4().hex[:12]}",
            timestamp=datetime.now(timezone.utc).isoformat(),
            contractor_id=contractor_id,
            contractor_type=contractor_type,
            query=query,
            categories_requested=categories_requested,
            categories_accessed=categories_accessed,
            data_items_returned=data_items_returned,
            data_items_filtered=data_items_filtered,
            response_size_bytes=response_size_bytes,
            ip_address=ip_address,
            user_agent=user_agent,
            duration_ms=duration_ms,
            error=error
        )

        self._entries.append(entry)
        await self._save_entry(entry)

        logger.info(
            f"📝 Audit: {contractor_type}/{contractor_id} - "
            f"query='{query[:50]}...' returned={data_items_returned} filtered={data_items_filtered}"
        )

        return entry

    async def _save_entry(self, entry: AuditEntry):
        """Сохранить запись в файл."""
        try:
            date_str = datetime.now().strftime("%Y%m%d")
            log_file = self.storage_path / f"audit_{date_str}.json"

            # Загружаем существующие записи
            entries = []
            if log_file.exists():
                with open(log_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    entries = data.get("entries", [])

            # Добавляем новую
            entries.append(entry.to_dict())

            # Сохраняем
            with open(log_file, "w", encoding="utf-8") as f:
                json.dump({"entries": entries}, f, ensure_ascii=False, indent=2)

        except Exception as e:
            logger.error(f"Error saving audit entry: {e}")

    def get_entries(
        self,
        contractor_id: Optional[str] = None,
        contractor_type: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        limit: int = 100
    ) -> List[AuditEntry]:
        """
        Получить записи аудита с фильтрацией.

        Args:
            contractor_id: Фильтр по ID подрядчика
            contractor_type: Фильтр по типу
            from_date: Начальная дата (ISO format)
            to_date: Конечная дата (ISO format)
            limit: Максимальное количество записей

        Returns:
            Список записей
        """
        filtered = self._entries

        if contractor_id:
            filtered = [e for e in filtered if e.contractor_id == contractor_id]

        if contractor_type:
            filtered = [e for e in filtered if e.contractor_type == contractor_type]

        if from_date:
            filtered = [e for e in filtered if e.timestamp >= from_date]

        if to_date:
            filtered = [e for e in filtered if e.timestamp <= to_date]

        # Сортируем по времени (новые первые)
        filtered.sort(key=lambda e: e.timestamp, reverse=True)

        return filtered[:limit]

    def get_stats(
        self,
        contractor_id: Optional[str] = None,
        days: int = 7
    ) -> Dict[str, Any]:
        """
        Получить статистику.

        Args:
            contractor_id: ID подрядчика (опционально)
            days: Количество дней для анализа

        Returns:
            Статистика
        """
        datetime.now(timezone.utc).isoformat()[:10]  # Упрощённо

        entries = self.get_entries(
            contractor_id=contractor_id,
            limit=10000
        )

        if not entries:
            return {"total_requests": 0}

        total_items_returned = sum(e.data_items_returned for e in entries)
        total_items_filtered = sum(e.data_items_filtered for e in entries)
        total_bytes = sum(e.response_size_bytes for e in entries)

        # Группировка по типам
        by_type: Dict[str, int] = {}
        for e in entries:
            by_type[e.contractor_type] = by_type.get(e.contractor_type, 0) + 1

        # Топ категорий
        categories: Dict[str, int] = {}
        for e in entries:
            for cat in e.categories_accessed:
                categories[cat] = categories.get(cat, 0) + 1

        top_categories = sorted(
            categories.items(),
            key=lambda x: x[1],
            reverse=True
        )[:10]

        return {
            "total_requests": len(entries),
            "total_items_returned": total_items_returned,
            "total_items_filtered": total_items_filtered,
            "filter_rate": round(total_items_filtered / max(total_items_returned + total_items_filtered, 1) * 100, 2),
            "total_bytes_transferred": total_bytes,
            "by_contractor_type": by_type,
            "top_categories": dict(top_categories),
            "errors_count": len([e for e in entries if e.error]),
            "avg_duration_ms": round(sum(e.duration_ms for e in entries) / max(len(entries), 1), 2)
        }

    def detect_anomalies(self) -> List[Dict[str, Any]]:
        """
        Обнаружение аномалий в запросах.

        Returns:
            Список обнаруженных аномалий
        """
        anomalies = []

        # Группируем по подрядчикам
        by_contractor: Dict[str, List[AuditEntry]] = {}
        for e in self._entries:
            if e.contractor_id not in by_contractor:
                by_contractor[e.contractor_id] = []
            by_contractor[e.contractor_id].append(e)

        for contractor_id, entries in by_contractor.items():
            # Проверяем частоту запросов (более 100 в час)
            recent = [e for e in entries if e.timestamp > datetime.now(timezone.utc).isoformat()[:13]]
            if len(recent) > 100:
                anomalies.append({
                    "type": "high_frequency",
                    "contractor_id": contractor_id,
                    "requests_per_hour": len(recent),
                    "severity": "warning"
                })

            # Проверяем объём данных (более 10MB за сессию)
            total_bytes = sum(e.response_size_bytes for e in recent)
            if total_bytes > 10 * 1024 * 1024:
                anomalies.append({
                    "type": "high_data_volume",
                    "contractor_id": contractor_id,
                    "bytes_transferred": total_bytes,
                    "severity": "warning"
                })

            # Проверяем ошибки (более 10%)
            errors = [e for e in entries if e.error]
            if len(entries) > 10 and len(errors) / len(entries) > 0.1:
                anomalies.append({
                    "type": "high_error_rate",
                    "contractor_id": contractor_id,
                    "error_rate": round(len(errors) / len(entries) * 100, 2),
                    "severity": "info"
                })

        return anomalies


# Singleton
_audit_logger: Optional[AuditLogger] = None


def get_audit_logger(storage_path: str = "data/audit") -> AuditLogger:
    """Получить экземпляр AuditLogger."""
    global _audit_logger
    if _audit_logger is None:
        _audit_logger = AuditLogger(storage_path)
    return _audit_logger


