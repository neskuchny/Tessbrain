# -*- coding: utf-8 -*-
"""
TESSENT BRAIN - Idea Agent
===========================

Извлекает идеи и предложения с оценкой инновационного потенциала,
осуществимости и бизнес-ценности.
"""
from typing import Any, Dict, Optional

from .base_agent import BaseExtractionAgent


class IdeaAgent(BaseExtractionAgent):
    """
    Агент извлечения идей с инновационным анализом.

    Возможности:
    - Оценка инновационного потенциала идей
    - Анализ осуществимости и ресурсоемкости
    - Прогнозирование влияния на бизнес
    - Система приоритизации идей
    """

    def get_entity_type(self) -> str:
        return "idea"

    def get_instruction(self) -> str:
        return '''Ты - IDEA AGENT, эксперт по анализу инновационного потенциала идей.

Твоя задача - извлечь ВСЕ идеи, предложения и инновации из встречи.

ФОРМАТ ВЫВОДА - JSON массив:
```json
[
  {
    "idea_id": "idea_001",
    "author": "Автор идеи",
    "idea_summary": "Краткая суть идеи",
    "idea_category": "product|process|technology|business_model|strategy|marketing",
    "innovation_assessment": {
      "novelty_level": "incremental|substantial|breakthrough|disruptive",
      "creativity_score": 0.8,
      "uniqueness": "common|uncommon|rare|unprecedented",
      "inspiration_source": ["Источники вдохновения"]
    },
    "feasibility_analysis": {
      "technical_feasibility": "high|medium|low",
      "resource_requirements": {
        "financial": "Финансовые потребности",
        "human": "Человеческие ресурсы",
        "time": "Временные затраты"
      },
      "implementation_complexity": "simple|moderate|complex|very_complex",
      "risk_factors": ["Основные риски реализации"]
    },
    "business_potential": {
      "market_opportunity": "large|medium|small|niche",
      "competitive_advantage": "strong|moderate|weak|none",
      "revenue_impact": "high|medium|low|uncertain",
      "scalability": "highly_scalable|moderately_scalable|limited_scalability"
    },
    "stakeholder_impact": {
      "customers": "positive|neutral|negative|mixed",
      "employees": "positive|neutral|negative|mixed",
      "partners": "positive|neutral|negative|mixed"
    },
    "implementation_roadmap": {
      "quick_wins": ["Быстрые победы"],
      "pilot_opportunities": ["Возможности пилотирования"],
      "success_metrics": ["Метрики успеха"]
    },
    "priority_scoring": {
      "impact_score": 0.8,
      "effort_score": 0.5,
      "urgency_score": 0.6,
      "strategic_fit": 0.9,
      "overall_priority": "critical|high|medium|low"
    },
    "next_steps": ["Рекомендуемые следующие шаги"],
    "quote": "Прямая цитата с идеей",
    "confidence_score": 0.85
  }
]
```

ВАЖНО — ТОЛЬКО ИДЕИ ИЗ ТЕКСТА:
- Ищи как явные идеи ("давайте сделаем"), так и скрытые ("было бы хорошо") — но
  каждая должна реально прозвучать в транскрипте, не додумывай за участников.
- Автора идеи указывай, только если он назван; иначе оставь пустым.
- Оценку реалистичности/потенциала давай по тому, что сказано в обсуждении, не по
  собственным фантазиям о рынке.
- Если идей нет — верни `[]`. Пустой массив лучше выдуманной идеи.
'''

    def normalize(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Нормализует извлеченную идею"""
        if not isinstance(item, dict):
            return None

        # Проверяем обязательные поля
        if not item.get("idea_summary"):
            return None

        # Базовая нормализация
        normalized = super().normalize(item)
        if not normalized:
            return None

        # Генерируем ID если нет
        if not normalized.get("idea_id"):
            import hashlib
            content = f"{normalized.get('author', '')}_{normalized.get('idea_summary', '')}"
            normalized["idea_id"] = f"idea_{hashlib.md5(content.encode()).hexdigest()[:8]}"

        # Нормализуем категорию
        valid_categories = ["product", "process", "technology", "business_model", "strategy", "marketing"]
        if normalized.get("idea_category") not in valid_categories:
            normalized["idea_category"] = "process"

        # Нормализуем priority scoring
        priority = normalized.get("priority_scoring", {})
        if isinstance(priority, dict):
            priority.setdefault("impact_score", 0.5)
            priority.setdefault("effort_score", 0.5)
            priority.setdefault("overall_priority", "medium")
        else:
            normalized["priority_scoring"] = {
                "impact_score": 0.5,
                "effort_score": 0.5,
                "overall_priority": "medium"
            }

        return normalized


