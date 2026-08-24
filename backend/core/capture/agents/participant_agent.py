# -*- coding: utf-8 -*-
"""
Агент для извлечения участников из встреч.
Адаптировано из tess_old/module_01_nlp_analysis/agents/enhanced_participant_agent.py
"""
import logging
import uuid
from typing import Any, Dict, Optional

from .base_agent import BaseExtractionAgent

logger = logging.getLogger(__name__)


class ParticipantAgent(BaseExtractionAgent):
    """
    Агент извлечения участников с профилированием.

    Извлекает:
    - Имена и роли участников
    - Уровень активности
    - Области экспертизы
    - Стиль коммуникации
    """

    def get_entity_type(self) -> str:
        return "participant"

    def get_instruction(self) -> str:
        return '''Ты - PARTICIPANT EXTRACTION AGENT, продвинутый агент профилирования участников встреч.

Твоя задача — извлечь ТОЛЬКО РЕАЛЬНЫХ участников встречи с НАСТОЯЩИМИ ИМЕНАМИ.

⚠️ КРИТИЧЕСКИ ВАЖНО - НЕ ИЗВЛЕКАЙ:
❌ "Speaker 1", "Speaker 2" и любые "Speaker N"
❌ "Неизвестный", "Неизвестно", "Unknown"
❌ "Пользователь", "User"
❌ "Ведущий", "Модератор" без имени
❌ "Сотрудник", "Сотрудники" без имени
❌ "Агент", "AI-агент"
❌ "Эксперт", "Аналитик" без имени
❌ "Директор по маркетингу" без имени (только с именем: "Иван Петров, директор по маркетингу")
❌ "Ты", "Вы", "Я"
❌ Любые роли/должности без конкретного имени

✅ ИЗВЛЕКАЙ ТОЛЬКО:
✅ Участников с реальными именами: "Иван", "Мария Петрова", "Антон"
✅ Участников упомянутых по имени в контексте ("Иван сказал", "по мнению Марии")
✅ Ответственных за задачи с именем ("Антон возьмёт задачу")

КРИТЕРИИ НАСТОЯЩЕГО ИМЕНИ:
- Содержит имя собственное (Иван, Мария, Антон, Олег и т.д.)
- Может включать фамилию (Иванов, Петрова)
- Может включать отчество (Иванович)
- НЕ является должностью или ролью

ФОРМАТ ВЫВОДА (JSON массив):
```json
[
  {
    "participant_id": "уникальный ID",
    "name": "Полное имя",
    "normalized_name": "Нормализованное имя (Иван Иванов)",
    "role": "Должность/роль в компании",
    "department": "Отдел/подразделение",
    "activity_level": "high|medium|low",
    "participation_metrics": {
      "speaking_turns": 0,
      "decisions_involved": 0,
      "tasks_assigned": 0,
      "questions_asked": 0
    },
    "expertise_areas": ["Области экспертизы"],
    "communication_style": {
      "style": "directive|collaborative|analytical|supportive",
      "traits": ["Характерные черты"]
    },
    "relationships": [
      {
        "with": "Имя другого участника",
        "type": "reports_to|collaborates|manages"
      }
    ],
    "key_contributions": ["Ключевые вклады в обсуждение"],
    "sentiment": "positive|neutral|negative",
    "confidence": 0.0-1.0
  }
]
```

УРОВНИ АКТИВНОСТИ:
- high: много говорит, принимает решения, ставит задачи
- medium: участвует в обсуждениях, комментирует
- low: упоминается, но мало говорит

СТИЛИ КОММУНИКАЦИИ:
- directive: командный, дает указания
- collaborative: командная работа, обсуждение
- analytical: аналитический, данные и факты
- supportive: поддерживающий, помогающий

ВАЖНО — ПРОФИЛЬ ТОЛЬКО ПО ПОВЕДЕНИЮ В ЭТОЙ ВСТРЕЧЕ:
- role/department заполняй, ТОЛЬКО если прозвучали или явно следуют из контекста;
  не угадывай должность по имени. Не указано — оставь пустым.
- communication_style, expertise_areas, traits, sentiment — выводи из того, что
  человек реально говорил и делал в транскрипте. Не приписывай черты, которых в
  тексте нет. Мало реплик → пустой стиль/черты, а не догадка.
- participation_metrics — по фактическим репликам, не выдумывай числа.
- нормализация имени допустима (Ваня → Иван), но не додумывай фамилию/отчество,
  которых нет в тексте.
- confidence отражает, насколько уверенно человек идентифицирован и описан.

Если реальных участников с именами нет — верни пустой массив `[]`.'''

    # Имена, которые нужно отфильтровывать
    INVALID_NAMES = {
        "speaker", "speaker 1", "speaker 2", "speaker 3", "speaker 4", "speaker 5",
        "неизвестный", "неизвестно", "unknown", "пользователь", "user",
        "ведущий", "модератор", "сотрудник", "сотрудники", "агент", "ai-агент",
        "эксперт", "аналитик", "директор", "менеджер", "маркетолог",
        "ты", "вы", "я", "мы", "они", "он", "она", "оно",
        "внешний аудитор", "аудитор", "аутсорс компания", "подрядчик",
        "заказчик", "клиент", "представитель"
    }

    def _is_valid_name(self, name: str) -> bool:
        """Проверяет, является ли имя настоящим именем человека"""
        if not name:
            return False

        name_lower = name.lower().strip()

        # Проверяем на запрещённые имена
        if name_lower in self.INVALID_NAMES:
            return False

        # Проверяем на паттерны Speaker N
        if name_lower.startswith("speaker"):
            return False

        # Проверяем на слишком короткие имена (1-2 символа)
        if len(name_lower) < 3:
            return False

        # Проверяем, что имя не является только должностью
        role_only_patterns = [
            "директор по", "менеджер по", "руководитель", "начальник",
            "специалист по", "ведущий", "старший", "младший"
        ]
        for pattern in role_only_patterns:
            if name_lower.startswith(pattern) and len(name_lower.split()) <= 3:
                # Если это только должность без имени
                return False

        return True

    def normalize(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Нормализация извлеченного участника"""
        if not isinstance(item, dict):
            return None

        # Проверяем обязательные поля
        name = item.get("name") or item.get("participant_name")
        if not name:
            return None

        # Фильтруем невалидные имена
        if not self._is_valid_name(name):
            logger.debug(f"Filtered out invalid participant name: {name}")
            return None

        # Генерируем ID если нет
        participant_id = item.get("participant_id") or item.get("id") or str(uuid.uuid4())

        # Нормализуем уровень активности
        activity_level = item.get("activity_level") or "medium"
        valid_levels = ["high", "medium", "low"]
        if activity_level not in valid_levels:
            activity_level = "medium"

        # Нормализуем communication_style
        comm_style = item.get("communication_style") or {}
        if isinstance(comm_style, str):
            comm_style = {"style": comm_style, "traits": []}

        normalized = {
            "participant_id": participant_id,
            "name": name,
            "normalized_name": item.get("normalized_name") or name,
            "role": item.get("role") or "",
            "department": item.get("department") or "",
            "activity_level": activity_level,
            "participation_metrics": item.get("participation_metrics") or {
                "speaking_turns": 0,
                "decisions_involved": 0,
                "tasks_assigned": 0,
                "questions_asked": 0
            },
            "expertise_areas": item.get("expertise_areas") or [],
            "communication_style": comm_style,
            "relationships": item.get("relationships") or [],
            "key_contributions": item.get("key_contributions") or [],
            "sentiment": item.get("sentiment") or "neutral",
            "confidence": float(item.get("confidence") or item.get("confidence_score") or 0.8)
        }

        return super().normalize(normalized)


# Singleton
participant_agent = ParticipantAgent()



