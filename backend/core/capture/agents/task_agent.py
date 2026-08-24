# -*- coding: utf-8 -*-
"""
Агент для извлечения задач из встреч.
Адаптировано из tess_old/module_01_nlp_analysis/agents/enhanced_task_agent.py
"""
import logging
import uuid
from typing import Any, Dict, Optional

from .base_agent import BaseExtractionAgent

logger = logging.getLogger(__name__)


class TaskAgent(BaseExtractionAgent):
    """
    Агент извлечения задач с расширенным анализом.

    Извлекает:
    - Явные задачи и поручения
    - Ответственных и сроки
    - Приоритеты и зависимости
    - Критерии выполнения
    """

    def get_entity_type(self) -> str:
        return "task"

    def get_instruction(self) -> str:
        return '''Ты - TASK EXTRACTION AGENT, продвинутый извлекатель задач из корпоративных встреч.

Твоя задача — извлечь ВСЕ задачи, поручения и action items из транскрипта.

КРИТЕРИИ ЗАДАЧИ:
✅ Явные поручения ("нужно сделать", "займись", "подготовь")
✅ Обещания и обязательства ("я сделаю", "возьму на себя")
✅ Запланированные действия с конкретными шагами
✅ Дедлайны и временные рамки
✅ Назначенные ответственные
✅ СТАТУС-АПДЕЙТЫ по ранее поставленным задачам ("сделали X", "X готово",
   "закрыли", "уже отправил") — их ТОЖЕ извлекай как задачу с правильным статусом,
   даже если задача была поставлена на ПРОШЛОЙ встрече. Это критично: без этого
   система никогда не узнаёт, что задача выполнена (в 360-обзоре у всех 0 done).

СТАТУС ЗАДАЧИ (поле status) — определяй по языку:
- done: задача завершена. Маркеры: "сделал(и)", "готово", "выполнили",
  "закрыли", "завершили", "отправил(и)", "уже готово", "справились".
- in_progress: в работе. Маркеры: "работаю над", "начал(и)", "делаю",
  "в процессе", "почти готово".
- blocked: заблокирована. Маркеры: "застряли", "ждём", "не можем пока",
  "заблокировано", "нужно сначала".
- new: новое поручение без признаков выполнения (по умолчанию).
Название (title) для статус-апдейта формулируй по СУТИ задачи, а не по фразе
об обновлении — чтобы её можно было сопоставить с исходной задачей.

АТРИБУЦИЯ К СПИКЕРАМ (issue #112 T-1):
Если в транскрипте есть метки "Speaker 1:", "Speaker 2:" — это диаризация
(система не знает имена, только разделила голоса). Атрибутируй задачу
к Speaker N в поле assignee_speaker. Если в КОНТЕКСТЕ ВСТРЕЧИ передан
participants — попробуй сопоставить Speaker с конкретным именем
(по само-представлению "я Максим" или обращению "Максим, ты возьмёшь?").

ПРАВИЛО assignment_confidence:
- high: assignee явно из participants + однозначное обращение/обещание
- medium: assignee из participants, но обращение неоднозначное
- low: assignee выведен из контекста / не из participants / unknown speaker

ВАЖНО: если ассайни не из participants (значит, его не было на встрече) —
ОБЯЗАТЕЛЬНО ставь assignment_confidence='low' и needs_human_review=true.
Никогда не приписывай задачу человеку, которого не было на встрече, без флага.

ФОРМАТ ВЫВОДА (JSON массив):
```json
[
  {
    "task_id": "уникальный ID",
    "title": "Краткое название задачи",
    "description": "Подробное описание",
    "task_type": "action|review|decision|meeting|research|other",
    "assignee": {
      "name": "Имя ответственного или null если неясно",
      "role": "Роль"
    },
    "assignee_speaker": "Speaker N если есть метки и можно атрибутировать; null иначе",
    "assignment_confidence": "low|medium|high",
    "needs_human_review": false,
    "assigner": "Кто поставил задачу",
    "priority": "critical|high|medium|low",
    "status": "new|in_progress|blocked|done",
    "deadline": {
      "date": "YYYY-MM-DD или описание (конец недели)",
      "is_hard": true/false
    },
    "dependencies": ["Зависимости от других задач"],
    "related_decision": "ID связанного решения (если есть)",
    "related_project": "Название проекта",
    "acceptance_criteria": ["Критерии выполнения"],
    "blockers": ["Потенциальные блокеры"],
    "quote": "Прямая цитата из транскрипта",
    "confidence": 0.0-1.0
  }
]
```

ТИПЫ ЗАДАЧ:
- action: конкретное действие
- review: проверка, ревью
- decision: требуется принять решение
- meeting: организовать встречу
- research: исследование, анализ
- other: прочее

ПРИОРИТЕТЫ:
- critical: срочно, блокирует других
- high: важно, нужно сделать скоро
- medium: обычный приоритет
- low: можно отложить

ВАЖНО — ТОЛЬКО ИЗ ТЕКСТА:
- quote ОБЯЗАТЕЛЕН и дословен — фраза, где задача прозвучала. Нет цитаты → не задача.
- Извлекай ВСЕ задачи, включая неявные, но каждая должна реально быть в тексте.
- Дедлайн, ответственного, критерии заполняй ТОЛЬКО если они прозвучали. Не
  указано — оставь пустым, не выдумывай даты, имена и критерии.
- title формулируй по сути задачи (для сопоставления со статус-апдейтами).
- confidence честно отражает явность формулировки задачи.

Если задач нет — верни пустой массив `[]`. Пустой массив лучше выдуманной задачи.'''

    def normalize(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Нормализация извлеченной задачи"""
        if not isinstance(item, dict):
            return None

        # Проверяем обязательные поля
        title = item.get("title") or item.get("task_title") or item.get("name")
        if not title:
            return None

        # Генерируем ID если нет
        task_id = item.get("task_id") or item.get("id") or str(uuid.uuid4())

        # Нормализуем тип
        task_type = item.get("task_type") or item.get("type") or "action"
        valid_types = ["action", "review", "decision", "meeting", "research", "other"]
        if task_type not in valid_types:
            task_type = "action"

        # Нормализуем приоритет
        priority = item.get("priority") or "medium"
        valid_priorities = ["critical", "high", "medium", "low"]
        if priority not in valid_priorities:
            priority = "medium"

        # Нормализуем assignee
        assignee = item.get("assignee") or {}
        if isinstance(assignee, str):
            assignee = {"name": assignee, "role": ""}

        # issue #112 T-1: attribution fields. LLM может вернуть string
        # 'null' / 'None' — превращаем в реальный Python None.
        def _none_if_null(v):
            if isinstance(v, str) and v.strip().lower() in ("", "null", "none"):
                return None
            return v

        assignee_speaker = _none_if_null(item.get("assignee_speaker"))
        # Имя ассайни — если LLM не определил, делаем None (а не пустой
        # dict.name = "")
        assignee_name = assignee.get("name") if isinstance(assignee, dict) else None
        assignee_name = _none_if_null(assignee_name)

        assignment_conf = (item.get("assignment_confidence") or "low").lower().strip()
        if assignment_conf not in ("low", "medium", "high"):
            assignment_conf = "low"

        needs_review = bool(item.get("needs_human_review", False))

        # Защита от self-overconfidence: если LLM сказал 'high', но нет
        # ни speaker, ни named assignee — система сбрасывает на 'low'
        # и поднимает флаг review. Это страховка от ситуаций, когда LLM
        # самоуверенно атрибутирует «всем подряд» без подтверждения.
        if not assignee_speaker and not assignee_name:
            assignment_conf = "low"
            needs_review = True

        normalized = {
            "task_id": task_id,
            "title": title,
            "description": item.get("description") or item.get("details") or "",
            "task_type": task_type,
            "assignee": assignee,
            "assignee_speaker": assignee_speaker,
            "assignment_confidence": assignment_conf,
            "needs_human_review": needs_review,
            "assigner": item.get("assigner") or "",
            "priority": priority,
            "status": item.get("status") or "new",
            "deadline": item.get("deadline") or {},
            "dependencies": item.get("dependencies") or [],
            "related_decision": item.get("related_decision") or "",
            "related_project": item.get("related_project") or item.get("project") or "",
            "acceptance_criteria": item.get("acceptance_criteria") or [],
            "blockers": item.get("blockers") or [],
            "quote": item.get("quote") or "",
            "confidence": float(item.get("confidence") or item.get("confidence_score") or 0.8)
        }

        return super().normalize(normalized)


# Singleton
task_agent = TaskAgent()



