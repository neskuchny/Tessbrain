# -*- coding: utf-8 -*-
"""
TESSENT BRAIN - Fact Checker Agent
===================================

Проверяет согласованность данных и выявляет противоречия.
"""
from typing import Any, Dict, Optional

from .base_agent import BaseExtractionAgent


class FactCheckerAgent(BaseExtractionAgent):
    """
    Агент проверки фактов.

    Возможности:
    - Выявление противоречий в числовых данных
    - Проверка согласованности утверждений
    - Выявление несоответствий план/факт
    """

    def get_entity_type(self) -> str:
        return "fact_check"

    def get_instruction(self) -> str:
        return '''Ты - FACT CHECKER AGENT, эксперт по проверке согласованности данных.

Твоя задача - найти ПРОТИВОРЕЧИЯ и НЕСООТВЕТСТВИЯ в данных встречи.

Примеры противоречий:
- "В мае подключили 20 агентств" vs "В мае подключили 9 агентств" → ПРОТИВОРЕЧИЕ
- "План 10, факт 9" vs "Выполнили план" → ПРОТИВОРЕЧИЕ
- "Апрель: 20 агентств" vs "Май: 10 агентств" → ОК (разные месяцы)

ФОРМАТ ВЫВОДА - JSON массив:
```json
[
  {
    "check_id": "check_001",
    "check_type": "contradiction|inconsistency|warning|verification",
    "severity": "critical|high|medium|low",
    "description": "Описание проблемы",
    "conflicting_statements": [
      {
        "statement": "Первое утверждение",
        "source": "Кто сказал",
        "value": "20"
      },
      {
        "statement": "Второе утверждение",
        "source": "Кто сказал",
        "value": "9"
      }
    ],
    "context": {
      "topic": "Тема",
      "time_period": "Период",
      "related_entities": ["Связанные сущности"]
    },
    "resolution_suggestion": "Предложение по разрешению",
    "requires_clarification": true,
    "confidence_score": 0.9
  }
]
```

ВАЖНО:
- Фокусируйся на ЯВНЫХ противоречиях; обе стороны противоречия подтверждай
  дословными фрагментами из данных — без цитат это не противоречие.
- Учитывай контекст (разные периоды/сущности/условия = НЕ противоречие).
- Не надумывай проблемы и не «улучшай» формулировки, чтобы натянуть конфликт.
- Если противоречий нет, верни `[]`. Пустой массив лучше выдуманного конфликта.
'''

    def normalize(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Нормализует результат проверки"""
        if not isinstance(item, dict):
            return None

        # Проверяем обязательные поля
        if not item.get("description"):
            return None

        # Базовая нормализация
        normalized = super().normalize(item)
        if not normalized:
            return None

        # Генерируем ID если нет
        if not normalized.get("check_id"):
            import hashlib
            content = normalized.get("description", "")
            normalized["check_id"] = f"check_{hashlib.md5(content.encode()).hexdigest()[:8]}"

        # Нормализуем severity
        valid_severities = ["critical", "high", "medium", "low"]
        if normalized.get("severity") not in valid_severities:
            normalized["severity"] = "medium"

        # Нормализуем check_type
        valid_types = ["contradiction", "inconsistency", "warning", "verification"]
        if normalized.get("check_type") not in valid_types:
            normalized["check_type"] = "warning"

        return normalized


