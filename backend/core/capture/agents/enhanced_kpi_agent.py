# -*- coding: utf-8 -*-
"""
TESSENT BRAIN - Enhanced KPI Agent
===================================

Извлекает KPI и метрики с глубоким бизнес-анализом,
трендами и прогнозами.
"""
from typing import Any, Dict, Optional

from .base_agent import BaseExtractionAgent


class EnhancedKPIAgent(BaseExtractionAgent):
    """
    Улучшенный агент KPI с бизнес-аналитикой.

    Возможности:
    - Извлечение числовых показателей с контекстом
    - Анализ трендов и динамики
    - Сравнение план/факт
    - Выявление проблемных зон
    """

    def get_entity_type(self) -> str:
        return "enhanced_kpi"

    def get_instruction(self) -> str:
        return '''Ты - ENHANCED KPI AGENT, эксперт по бизнес-метрикам и показателям.

Твоя задача - извлечь ВСЕ числовые показатели, KPI и метрики из встречи.

ВАЖНО: НЕ ПРИДУМЫВАЙ цифры! Извлекай только то, что ЯВНО упомянуто.

ФОРМАТ ВЫВОДА - JSON массив:
```json
[
  {
    "kpi_id": "kpi_001",
    "metric_name": "Название метрики",
    "metric_category": "financial|operational|customer|growth|quality|hr",
    "values": {
      "current_value": "100",
      "target_value": "150",
      "previous_value": "80",
      "unit": "тыс. руб.|%|шт.|человек|etc"
    },
    "time_context": {
      "period": "месяц|квартал|год|неделя",
      "specific_date": "май 2025",
      "comparison_period": "апрель 2025"
    },
    "trend_analysis": {
      "direction": "up|down|stable",
      "change_percentage": 25,
      "trend_strength": "strong|moderate|weak",
      "seasonality": "Сезонные факторы"
    },
    "performance_assessment": {
      "vs_target": "above|on|below",
      "vs_previous": "better|same|worse",
      "gap_analysis": "Анализ разрыва",
      "performance_rating": "excellent|good|satisfactory|poor|critical"
    },
    "business_context": {
      "strategic_importance": "high|medium|low",
      "affected_areas": ["Затронутые области"],
      "root_causes": ["Причины изменений"],
      "external_factors": ["Внешние факторы"]
    },
    "action_items": {
      "immediate_actions": ["Немедленные действия"],
      "improvement_opportunities": ["Возможности улучшения"],
      "risks_if_unchanged": ["Риски без изменений"]
    },
    "related_metrics": ["Связанные метрики"],
    "data_quality": {
      "source_reliability": "high|medium|low",
      "data_freshness": "current|recent|outdated"
    },
    "quote": "Прямая цитата с метрикой",
    "confidence_score": 0.9
  }
]
```

ПРИНЦИПЫ:
- Извлекай ТОЛЬКО упомянутые числа
- Определяй контекст (план/факт, период)
- Анализируй тренды если есть сравнение
- Выявляй проблемные показатели
- Если метрик нет, верни `[]`
'''

    def normalize(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Нормализует извлеченную метрику"""
        if not isinstance(item, dict):
            return None

        # Проверяем обязательные поля
        if not item.get("metric_name"):
            return None

        # Базовая нормализация
        normalized = super().normalize(item)
        if not normalized:
            return None

        # Генерируем ID если нет
        if not normalized.get("kpi_id"):
            import hashlib
            content = normalized.get("metric_name", "")
            normalized["kpi_id"] = f"kpi_{hashlib.md5(content.encode()).hexdigest()[:8]}"

        # Нормализуем values
        values = normalized.get("values", {})
        if not isinstance(values, dict):
            normalized["values"] = {"current_value": str(values)}

        # Нормализуем категорию
        valid_categories = ["financial", "operational", "customer", "growth", "quality", "hr"]
        if normalized.get("metric_category") not in valid_categories:
            normalized["metric_category"] = "operational"

        return normalized


