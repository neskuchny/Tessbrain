# -*- coding: utf-8 -*-
"""
TESSENT BRAIN - Project Status Agent
=====================================

Извлекает информацию о проектах с полным анализом статуса,
рисков и прогресса.
"""
from typing import Any, Dict, Optional

from .base_agent import BaseExtractionAgent


class ProjectStatusAgent(BaseExtractionAgent):
    """
    Агент извлечения статуса проектов с полным жизненным циклом.

    Возможности:
    - Анализ зрелости и стадии проекта
    - Оценка рисков и успешности
    - Отслеживание ресурсов и бюджета
    - Прогнозирование сроков и результатов
    """

    def get_entity_type(self) -> str:
        return "project_status"

    def get_instruction(self) -> str:
        return '''Ты - PROJECT STATUS AGENT, эксперт по управлению проектами и их анализу.

Твоя задача - извлечь информацию о ВСЕХ упоминаемых проектах и их статусе.

ФОРМАТ ВЫВОДА - JSON массив:
```json
[
  {
    "project_id": "project_001",
    "project_name": "Название проекта",
    "description": "Описание и цели проекта",
    "project_category": "infrastructure|product|process|strategic|operational",
    "lifecycle_stage": "conception|planning|execution|monitoring|closure",
    "status_assessment": {
      "current_status": "not_started|planning|in_progress|on_hold|completed|cancelled",
      "health_indicator": "green|yellow|red",
      "progress_percentage": 60,
      "last_update": "Последнее обновление статуса"
    },
    "scope_analysis": {
      "primary_objectives": ["Основные цели"],
      "deliverables": ["Ожидаемые результаты"],
      "success_criteria": ["Критерии успеха"],
      "scope_clarity": "clear|moderate|unclear"
    },
    "stakeholder_mapping": {
      "project_sponsor": "Спонсор проекта",
      "project_manager": "Менеджер проекта",
      "core_team": ["Основная команда"],
      "key_stakeholders": ["Ключевые стейкхолдеры"]
    },
    "resource_analysis": {
      "budget_info": {
        "estimated_budget": "Планируемый бюджет",
        "budget_status": "under|on|over"
      },
      "human_resources": {
        "team_size": "Размер команды",
        "skill_gaps": ["Пробелы в навыках"]
      }
    },
    "timeline_analysis": {
      "estimated_duration": "Планируемая длительность",
      "start_date": "Дата начала",
      "target_completion": "Целевая дата завершения",
      "critical_milestones": ["Критические этапы"],
      "timeline_risk": "low|medium|high"
    },
    "risk_assessment": {
      "identified_risks": [
        {
          "risk_description": "Описание риска",
          "probability": "low|medium|high",
          "impact": "low|medium|high",
          "mitigation_strategy": "Стратегия снижения"
        }
      ],
      "overall_risk_level": "low|medium|high|critical"
    },
    "business_value": {
      "strategic_alignment": "high|medium|low",
      "expected_roi": "Ожидаемый ROI",
      "business_benefits": ["Бизнес-выгоды"]
    },
    "dependencies": {
      "internal_dependencies": ["Внутренние зависимости"],
      "external_dependencies": ["Внешние зависимости"],
      "blocking_factors": ["Блокирующие факторы"]
    },
    "current_issues": ["Текущие проблемы"],
    "recent_achievements": ["Недавние достижения"],
    "quote": "Прямая цитата о проекте",
    "confidence_score": 0.85
  }
]
```

ВАЖНО — СТАТУС ТОЛЬКО ПО СКАЗАННОМУ О ПРОЕКТЕ:
- Извлекай ВСЕ упомянутые проекты, но статус/риски/блокеры оценивай строго по
  тому, что о проекте прозвучало в тексте. Ничего не выдумывай.
- Прогресс, сроки, метрики бери только если названы; нет данных — не подставляй.
- Статус (yellow/red) ставь по реальным сигналам из обсуждения, а не по общему
  ощущению.
- Если проектов не упоминается — верни `[]`.
'''

    def normalize(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Нормализует извлеченный статус проекта"""
        if not isinstance(item, dict):
            return None

        # Проверяем обязательные поля
        if not item.get("project_name"):
            return None

        # Базовая нормализация
        normalized = super().normalize(item)
        if not normalized:
            return None

        # Генерируем ID если нет
        if not normalized.get("project_id"):
            import hashlib
            content = normalized.get("project_name", "")
            normalized["project_id"] = f"proj_{hashlib.md5(content.encode()).hexdigest()[:8]}"

        # Нормализуем status_assessment
        status = normalized.get("status_assessment", {})
        if isinstance(status, dict):
            status.setdefault("current_status", "in_progress")
            status.setdefault("health_indicator", "yellow")
            status.setdefault("progress_percentage", 0)
        else:
            normalized["status_assessment"] = {
                "current_status": "in_progress",
                "health_indicator": "yellow",
                "progress_percentage": 0
            }

        # Нормализуем risk_assessment
        risk = normalized.get("risk_assessment", {})
        if isinstance(risk, dict):
            risk.setdefault("overall_risk_level", "medium")
            risk.setdefault("identified_risks", [])
        else:
            normalized["risk_assessment"] = {
                "overall_risk_level": "medium",
                "identified_risks": []
            }

        return normalized


