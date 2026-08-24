# -*- coding: utf-8 -*-
"""
Агент для извлечения KPI и числовых метрик.
Адаптировано из tess_old/module_01_nlp_analysis/agents/corporate_kpi_agent.py
"""
import logging
import uuid
from typing import Any, Dict, Optional

from .base_agent import BaseExtractionAgent

logger = logging.getLogger(__name__)


class KPIAgent(BaseExtractionAgent):
    """
    Агент извлечения KPI и числовых данных.

    Извлекает:
    - Финансовые показатели
    - Операционные метрики
    - Цели и план/факт
    - Тренды и изменения
    """

    def get_entity_type(self) -> str:
        return "kpi"

    def get_instruction(self) -> str:
        return '''Ты - KPI EXTRACTION AGENT, агент извлечения числовых показателей и метрик из встреч.

Твоя задача — извлечь ВСЕ числовые данные, KPI и метрики из транскрипта.

ТИПЫ ДАННЫХ:

1. **ФИНАНСОВЫЕ (financial)**
   - Выручка, прибыль, затраты
   - Бюджеты и инвестиции
   - ROI, маржа, LTV

2. **ОПЕРАЦИОННЫЕ (operational)**
   - Конверсии, retention
   - Время выполнения
   - Количество пользователей

3. **КАЧЕСТВЕННЫЕ (quality)**
   - NPS, CSAT
   - Количество багов
   - Uptime

4. **РЕСУРСНЫЕ (resource)**
   - Количество сотрудников
   - Загрузка команды
   - Бюджет проекта

ФОРМАТ ВЫВОДА (JSON массив):
```json
[
  {
    "kpi_id": "уникальный ID",
    "name": "Название метрики",
    "category": "financial|operational|quality|resource",
    "value": {
      "current": 100,
      "previous": 80,
      "target": 120,
      "unit": "единица измерения"
    },
    "trend": {
      "direction": "up|down|stable",
      "change_percent": 25.0,
      "period": "week|month|quarter|year"
    },
    "context": {
      "project": "связанный проект",
      "department": "отдел",
      "responsible": "ответственный"
    },
    "analysis": {
      "status": "on_track|at_risk|off_track",
      "insight": "аналитический вывод",
      "action_needed": true/false
    },
    "quote": "прямая цитата",
    "confidence": 0.0-1.0
  }
]
```

ВАЖНО — ЦИФРЫ ТОЛЬКО ИЗ ТЕКСТА, НИКОГДА НЕ ВЫДУМЫВАЙ:
- quote ОБЯЗАТЕЛЕН и дословен — фрагмент, где прозвучало число. Нет цитаты с
  числом → не включай метрику.
- Бери ТОЛЬКО те значения, что реально названы. previous/target/change_percent
  заполняй, лишь если сравнение прозвучало в тексте. Не вычисляй тренд, если не с
  чем сравнивать, и не подставляй «правдоподобные» цифры.
- Не путай метрику с общими словами («много», «выросли») без числа — это не KPI.
- unit — как в тексте (% / ₽ / $ / штуки / дни). Не конвертируй валюты.
- analysis.insight — вывод строго по названным числам, без домыслов о причинах.
- confidence: 0.9+ число и метрика названы прямо; ниже — если что-то выводится.

Как разбирать (значения — иллюстрация формата, НЕ данные для вывода):
- «Конверсия выросла на 20%» → trend.change_percent из текста, direction=up.
- «Выручка 5 млн рублей» → value.current и unit из текста.
- «Команда из 10 человек» → value.current из текста, category=resource.

Если числовых данных нет — верни пустой массив `[]`. Пустой массив лучше
выдуманной цифры.'''

    def normalize(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Нормализация извлеченного KPI"""
        if not isinstance(item, dict):
            return None

        # Проверяем обязательные поля
        name = item.get("name") or item.get("kpi_name") or item.get("metric_name")
        if not name:
            return None

        # Генерируем ID если нет
        kpi_id = item.get("kpi_id") or item.get("id") or str(uuid.uuid4())

        # Нормализуем категорию
        category = item.get("category") or "operational"
        valid_categories = ["financial", "operational", "quality", "resource"]
        if category not in valid_categories:
            category = "operational"

        # Нормализуем value
        value = item.get("value") or {}
        if not isinstance(value, dict):
            value = {"current": value, "unit": ""}

        # Нормализуем trend
        trend = item.get("trend") or {}
        if not isinstance(trend, dict):
            trend = {}

        # Нормализуем analysis
        analysis = item.get("analysis") or {}
        if not isinstance(analysis, dict):
            analysis = {}

        normalized = {
            "kpi_id": kpi_id,
            "name": name,
            "category": category,
            "value": {
                "current": value.get("current"),
                "previous": value.get("previous"),
                "target": value.get("target"),
                "unit": value.get("unit") or ""
            },
            "trend": {
                "direction": trend.get("direction") or "stable",
                "change_percent": trend.get("change_percent"),
                "period": trend.get("period") or ""
            },
            "context": item.get("context") or {},
            "analysis": {
                "status": analysis.get("status") or "on_track",
                "insight": analysis.get("insight") or "",
                "action_needed": analysis.get("action_needed", False)
            },
            "quote": item.get("quote") or "",
            "confidence": float(item.get("confidence") or item.get("confidence_score") or 0.8)
        }

        return super().normalize(normalized)


# Singleton
kpi_agent = KPIAgent()



