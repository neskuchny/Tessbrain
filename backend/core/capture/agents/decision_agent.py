# -*- coding: utf-8 -*-
"""
Агент для извлечения решений из встреч.
Адаптировано из tess_old/module_01_nlp_analysis/agents/enhanced_decision_agent.py
"""
import logging
import uuid
from typing import Any, Dict, Optional

from .base_agent import BaseExtractionAgent

logger = logging.getLogger(__name__)


class DecisionAgent(BaseExtractionAgent):
    """
    Агент извлечения решений с расширенным анализом.

    Извлекает:
    - Явные решения и выводы
    - Ответственных и сроки
    - Бизнес-влияние и риски
    - Связанные действия
    """

    def get_entity_type(self) -> str:
        return "decision"

    def get_instruction(self) -> str:
        return '''Ты - DECISION EXTRACTION AGENT, продвинутый извлекатель решений из корпоративных встреч.

Твоя задача — ИНТЕЛЛЕКТУАЛЬНО ПРОАНАЛИЗИРОВАТЬ транскрипт и извлечь ВСЕ принятые решения.

КРИТЕРИИ РЕШЕНИЯ:
✅ Явные утверждения о принятых решениях ("решили", "договорились", "будем делать")
✅ Выбор конкретного варианта из альтернатив
✅ Назначение ответственных с полномочиями
✅ Согласованные выводы с временными рамками
✅ Финансовые обязательства и бюджетные решения
✅ Стратегические направления и приоритеты
✅ Технические решения по архитектуре/инструментам
✅ Процессные решения и изменения регламентов

ФОРМАТ ВЫВОДА (JSON массив):
```json
[
  {
    "decision_id": "уникальный ID",
    "decision_summary": "Краткая суть решения (1-2 предложения)",
    "decision_category": "strategic|operational|technical|financial|hr|process",
    "details": "Подробное описание решения и контекста",
    "participants": [
      {
        "name": "Имя участника",
        "role": "Роль в принятии решения"
      }
    ],
    "responsible": {
      "primary": "Основной ответственный",
      "secondary": ["Дополнительные ответственные"]
    },
    "reasoning": "Обоснование решения",
    "timeline": {
      "deadline": "Срок выполнения (если указан)",
      "milestones": ["Промежуточные этапы"]
    },
    "business_impact": {
      "scope": "local|department|company|external",
      "urgency": "high|medium|low",
      "risk_level": "high|medium|low"
    },
    "dependencies": ["Зависимости от других решений/процессов"],
    "action_items": [
      {
        "action": "Конкретное действие",
        "owner": "Ответственный",
        "deadline": "Срок"
      }
    ],
    "quote": "Прямая цитата из транскрипта",
    "confidence": 0.0-1.0
  }
]
```

КАТЕГОРИИ РЕШЕНИЙ:
- strategic: стратегические направления, видение
- operational: операционные процессы, ресурсы
- technical: технологии, архитектура, инструменты
- financial: бюджеты, инвестиции, затраты
- hr: кадровые решения, организационные изменения
- process: регламенты, процедуры, методологии

ВАЖНО — ТОЛЬКО РЕАЛЬНО ПРИНЯТЫЕ РЕШЕНИЯ ИЗ ТЕКСТА:
- quote ОБЯЗАТЕЛЕН и дословен — фрагмент транскрипта, где решение принято. Нет
  подходящей цитаты → это не решение, не включай.
- Различай РЕШЕНИЕ (выбор сделан) от намерения/идеи/обсуждения («надо бы»,
  «давайте подумаем», «а что если»). Последнее — не решение.
- Заполняй поле ТОЛЬКО если оно есть в тексте. Нет ответственного/срока/
  обоснования — пропусти поле, не выдумывай имена, даты и суммы.
- confidence честно: 0.9+ решение сформулировано прямо; 0.6–0.8 принято, но детали
  косвенные; <0.6 похоже на решение, но формулировка размыта.
- action_items — только те действия, что реально прозвучали, не додумывай план.

Если реально принятых решений нет — верни пустой массив `[]`. Пустой массив лучше,
чем намерение, выданное за решение.'''

    def normalize(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Нормализация извлеченного решения"""
        if not isinstance(item, dict):
            return None

        # Проверяем обязательные поля
        summary = item.get("decision_summary") or item.get("summary") or item.get("title")
        if not summary:
            return None

        # Генерируем ID если нет
        decision_id = item.get("decision_id") or item.get("id") or str(uuid.uuid4())

        # Нормализуем категорию
        category = item.get("decision_category") or item.get("category") or "operational"
        valid_categories = ["strategic", "operational", "technical", "financial", "hr", "process"]
        if category not in valid_categories:
            category = "operational"

        # Нормализуем business_impact
        business_impact = item.get("business_impact") or {}
        if not isinstance(business_impact, dict):
            business_impact = {}

        # Спикер-атрибуция (issue #112): кто принял решение. Берём явное поле,
        # иначе выводим из responsible.primary / первого участника. Это
        # связывает Decision с Person в графе («кто решил»).
        _responsible = item.get("responsible") or item.get("responsible_party") or {}
        _parts = item.get("participants") or []
        _dm = (item.get("decision_maker_resolved")
               or (_responsible.get("primary") if isinstance(_responsible, dict) else None)
               or (_parts[0].get("name") if _parts and isinstance(_parts[0], dict) else None))
        if isinstance(_dm, str) and _dm.strip().lower() in ("", "null", "none", "n/a"):
            _dm = None
        _attr_conf = (item.get("attribution_confidence")
                      or ("medium" if _dm else "low"))

        normalized = {
            "decision_id": decision_id,
            "decision_summary": summary,
            "decision_category": category,
            "details": item.get("details") or item.get("description") or "",
            "participants": item.get("participants") or [],
            "responsible": item.get("responsible") or item.get("responsible_party") or {},
            "decision_maker_resolved": _dm,
            "decision_maker_speaker": item.get("decision_maker_speaker") or "",
            "attribution_confidence": _attr_conf,
            "reasoning": item.get("reasoning") or "",
            "timeline": item.get("timeline") or {},
            "business_impact": {
                "scope": business_impact.get("scope", "local"),
                "urgency": business_impact.get("urgency", "medium"),
                "risk_level": business_impact.get("risk_level", "low")
            },
            "dependencies": item.get("dependencies") or [],
            "action_items": item.get("action_items") or [],
            "quote": item.get("quote") or "",
            "confidence": float(item.get("confidence") or item.get("confidence_score") or 0.8)
        }

        # Добавляем метаданные из базового класса
        return super().normalize(normalized)


# Singleton для удобства
decision_agent = DecisionAgent()



