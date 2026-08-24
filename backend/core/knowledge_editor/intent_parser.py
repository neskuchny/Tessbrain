# -*- coding: utf-8 -*-
"""
Intent Parser - Парсер намерений для редактирования знаний.

Анализирует текст пользователя и определяет:
- Что нужно сделать (действие)
- С какой сущностью
- Какое поле изменить
- На какое значение
"""
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class EditAction(Enum):
    """Тип действия редактирования."""
    UPDATE = "update"           # Обновить значение
    DELETE = "delete"           # Удалить сущность
    ADD = "add"                 # Добавить новую сущность
    MERGE = "merge"             # Объединить сущности
    RENAME = "rename"           # Переименовать
    LINK = "link"               # Создать связь
    UNLINK = "unlink"           # Удалить связь
    RECLASSIFY = "reclassify"   # Изменить тип/категорию
    CLARIFY = "clarify"         # Уточнить информацию (нужно больше данных)
    UNKNOWN = "unknown"         # Не удалось определить


@dataclass
class EditIntent:
    """Намерение редактирования."""
    action: EditAction
    confidence: float  # 0.0 - 1.0

    # Целевая сущность
    entity_type: Optional[str] = None  # person, project, company, task, etc.
    entity_name: Optional[str] = None  # Имя/название для поиска
    entity_id: Optional[str] = None    # ID если известен

    # Что меняем
    target_field: Optional[str] = None  # Поле для изменения
    old_value: Optional[Any] = None
    new_value: Optional[Any] = None

    # Для связей
    target_entity_name: Optional[str] = None
    target_entity_type: Optional[str] = None
    relationship_type: Optional[str] = None

    # Контекст
    reason: Optional[str] = None
    original_text: str = ""

    # Найденные сущности (после поиска)
    found_entities: List[Dict[str, Any]] = field(default_factory=list)

    # Требуется подтверждение?
    requires_confirmation: bool = True
    clarification_needed: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action.value,
            "confidence": self.confidence,
            "entity_type": self.entity_type,
            "entity_name": self.entity_name,
            "entity_id": self.entity_id,
            "field": self.target_field,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "target_entity_name": self.target_entity_name,
            "target_entity_type": self.target_entity_type,
            "relationship_type": self.relationship_type,
            "reason": self.reason,
            "original_text": self.original_text,
            "found_entities": self.found_entities,
            "requires_confirmation": self.requires_confirmation,
            "clarification_needed": self.clarification_needed,
        }


class IntentParser:
    """
    Парсер намерений для редактирования знаний.

    Использует LLM для понимания естественного языка и
    извлечения структурированного намерения.
    """

    def __init__(self, llm_router=None):
        from ..llm.router import get_llm_router
        self.llm = llm_router or get_llm_router()

        # Паттерны для быстрого определения действия
        self.action_patterns = {
            EditAction.UPDATE: [
                r"измени(?:ть)?",
                r"обнови(?:ть)?",
                r"поменя(?:й|ть)",
                r"исправ(?:ь|ить)",
                r"установи(?:ть)?",
                r"замени(?:ть)?",
                r"change",
                r"update",
                r"set",
                r"fix",
            ],
            EditAction.DELETE: [
                r"удали(?:ть)?",
                r"убери(?:ть)?",
                r"убра(?:ть)?",
                r"сотри(?:ть)?",
                r"delete",
                r"remove",
            ],
            EditAction.ADD: [
                r"добав(?:ь|ить)",
                r"созда(?:й|ть)",
                r"внеси(?:ть)?",
                r"add",
                r"create",
            ],
            EditAction.MERGE: [
                r"объедини(?:ть)?",
                r"слей(?:ть)?",
                r"merge",
                r"combine",
            ],
            EditAction.RENAME: [
                r"переименуй(?:ть)?",
                r"rename",
            ],
            EditAction.LINK: [
                r"свяжи(?:ть)?",
                r"соедини(?:ть)?",
                r"добав(?:ь|ить)\s+связь",
                r"link",
                r"connect",
            ],
            EditAction.UNLINK: [
                r"отвяжи(?:ть)?",
                r"разъедини(?:ть)?",
                r"убери(?:ть)?\s+связь",
                r"удали(?:ть)?\s+связь",
                r"unlink",
                r"disconnect",
            ],
            EditAction.RECLASSIFY: [
                r"перекласифицируй(?:ть)?",
                r"измени(?:ть)?\s+тип",
                r"смени(?:ть)?\s+категорию",
                r"reclassify",
            ],
        }

        # Типы сущностей
        self.entity_type_patterns = {
            "person": [r"сотрудник", r"человек", r"участник", r"person", r"employee", r"member"],
            "project": [r"проект", r"project"],
            "company": [r"компани[яю]", r"фирм[ау]", r"company", r"organization"],
            "task": [r"задач[ау]", r"таск", r"task"],
            "decision": [r"решени[ея]", r"decision"],
            "meeting": [r"встреч[ау]", r"совещани[ея]", r"meeting"],
            "department": [r"отдел", r"департамент", r"department"],
            "skill": [r"навык", r"скилл", r"skill"],
            "goal": [r"цель", r"goal", r"objective"],
            "kpi": [r"kpi", r"метрик[ау]", r"показатель"],
        }

        # Поля сущностей
        self.field_patterns = {
            "name": [r"им[яе]", r"название", r"name", r"title"],
            "role": [r"роль", r"должность", r"позици[яю]", r"role", r"position"],
            "department": [r"отдел", r"департамент", r"department"],
            "email": [r"email", r"почт[ау]", r"e-mail"],
            "phone": [r"телефон", r"номер", r"phone"],
            "status": [r"статус", r"status", r"состояни[ея]"],
            "description": [r"описани[ея]", r"description"],
            "priority": [r"приоритет", r"priority"],
            "deadline": [r"дедлайн", r"срок", r"deadline"],
            "manager": [r"руководител[ья]", r"менеджер", r"manager", r"boss"],
            "skills": [r"навык[иа]", r"скилл[ыа]", r"skills"],
        }

    async def parse(self, text: str, context: Optional[Dict[str, Any]] = None) -> EditIntent:
        """
        Парсит текст пользователя и возвращает намерение.

        Args:
            text: Текст от пользователя
            context: Дополнительный контекст (текущий проект, пользователь и т.д.)

        Returns:
            EditIntent с распарсенным намерением
        """
        text_lower = text.lower().strip()

        # 1. Быстрый анализ паттернами
        quick_action = self._detect_action_quick(text_lower)
        quick_entity_type = self._detect_entity_type_quick(text_lower)
        quick_field = self._detect_field_quick(text_lower)

        # 2. LLM анализ для точного понимания
        llm_result = await self._parse_with_llm(text, context, {
            "quick_action": quick_action.value if quick_action else None,
            "quick_entity_type": quick_entity_type,
            "quick_field": quick_field,
        })

        # 3. Собираем результат
        intent = EditIntent(
            action=llm_result.get("action", quick_action or EditAction.UNKNOWN),
            confidence=llm_result.get("confidence", 0.5),
            entity_type=llm_result.get("entity_type", quick_entity_type),
            entity_name=llm_result.get("entity_name"),
            target_field=llm_result.get("field", quick_field),
            old_value=llm_result.get("old_value"),
            new_value=llm_result.get("new_value"),
            target_entity_name=llm_result.get("target_entity_name"),
            target_entity_type=llm_result.get("target_entity_type"),
            relationship_type=llm_result.get("relationship_type"),
            reason=llm_result.get("reason"),
            original_text=text,
            requires_confirmation=llm_result.get("requires_confirmation", True),
            clarification_needed=llm_result.get("clarification_needed"),
        )

        # 4. Определяем нужно ли уточнение
        if intent.action == EditAction.UNKNOWN:
            intent.clarification_needed = "Не удалось понять, что вы хотите сделать. Уточните, пожалуйста."
        elif not intent.entity_name and not intent.entity_id:
            intent.clarification_needed = "Укажите, пожалуйста, какую именно сущность вы хотите изменить."

        logger.info(f"Parsed intent: action={intent.action.value}, entity={intent.entity_name}, confidence={intent.confidence}")
        return intent

    def _detect_action_quick(self, text: str) -> Optional[EditAction]:
        """Быстрое определение действия по паттернам."""
        for action, patterns in self.action_patterns.items():
            for pattern in patterns:
                if re.search(pattern, text, re.IGNORECASE):
                    return action
        return None

    def _detect_entity_type_quick(self, text: str) -> Optional[str]:
        """Быстрое определение типа сущности."""
        for entity_type, patterns in self.entity_type_patterns.items():
            for pattern in patterns:
                if re.search(pattern, text, re.IGNORECASE):
                    return entity_type
        return None

    def _detect_field_quick(self, text: str) -> Optional[str]:
        """Быстрое определение поля."""
        for field_name, patterns in self.field_patterns.items():
            for pattern in patterns:
                if re.search(pattern, text, re.IGNORECASE):
                    return field_name
        return None

    async def _parse_with_llm(
        self,
        text: str,
        context: Optional[Dict[str, Any]],
        hints: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Точный парсинг с помощью LLM."""

        context_str = ""
        if context:
            context_str = f"""
Контекст:
- Текущий пользователь: {context.get('user_name', 'неизвестно')}
- Текущий проект: {context.get('current_project', 'не выбран')}
- Последние сущности: {', '.join(context.get('recent_entities', [])[:5])}
"""

        hints_str = ""
        if hints.get("quick_action"):
            hints_str += f"- Предполагаемое действие: {hints['quick_action']}\n"
        if hints.get("quick_entity_type"):
            hints_str += f"- Предполагаемый тип сущности: {hints['quick_entity_type']}\n"
        if hints.get("quick_field"):
            hints_str += f"- Предполагаемое поле: {hints['quick_field']}\n"

        prompt = f"""Ты - парсер намерений для системы редактирования знаний компании.

Проанализируй запрос пользователя и извлеки структурированное намерение.

ЗАПРОС ПОЛЬЗОВАТЕЛЯ:
"{text}"

{context_str}

{f"ПОДСКАЗКИ (могут быть неточными):{chr(10)}{hints_str}" if hints_str else ""}

ТИПЫ ДЕЙСТВИЙ:
- update: обновить значение поля
- delete: удалить сущность
- add: добавить новую сущность
- merge: объединить две сущности в одну
- rename: переименовать сущность
- link: создать связь между сущностями
- unlink: удалить связь между сущностями
- reclassify: изменить тип/категорию сущности
- clarify: нужно уточнение от пользователя
- unknown: не удалось понять намерение

ТИПЫ СУЩНОСТЕЙ:
person, project, company, task, decision, meeting, department, skill, goal, kpi

ПОЛЯ СУЩНОСТЕЙ:
name, role, department, email, phone, status, description, priority, deadline, manager, skills

Ответь в формате JSON:
{{
    "action": "тип_действия",
    "confidence": 0.0-1.0,
    "entity_type": "тип_сущности или null",
    "entity_name": "имя/название сущности для поиска или null",
    "field": "поле для изменения или null",
    "old_value": "старое значение если известно или null",
    "new_value": "новое значение или null",
    "target_entity_name": "для связей - вторая сущность или null",
    "target_entity_type": "тип второй сущности или null",
    "relationship_type": "тип связи или null",
    "reason": "причина изменения если указана или null",
    "requires_confirmation": true/false,
    "clarification_needed": "вопрос для уточнения или null"
}}

ВАЖНО:
- Если пользователь указывает конкретное имя (Иван, Петров, проект Alpha), извлеки его в entity_name
- Если значение нужно изменить на конкретное, укажи его в new_value
- Если не уверен в чем-то, поставь requires_confirmation: true
- Если нужно уточнение, укажи вопрос в clarification_needed

JSON:"""

        try:
            # Проверяем что LLM доступен
            if self.llm is None:
                logger.warning("LLM router not available, using pattern-based parsing only")
                return self._fallback_parse(text, hints)

            response = await self.llm.generate(
                prompt=prompt,
                max_tokens=500,
                temperature=0.1,
            )

            # Извлекаем JSON из ответа
            import json
            json_match = re.search(r'\{[\s\S]*\}', response)
            if json_match:
                result = json.loads(json_match.group())

                # Конвертируем action в enum
                action_str = result.get("action", "unknown")
                try:
                    result["action"] = EditAction(action_str)
                except ValueError:
                    result["action"] = EditAction.UNKNOWN

                return result

        except Exception as e:
            logger.error(f"LLM parsing error: {e}")
            return self._fallback_parse(text, hints)

        return {
            "action": EditAction.UNKNOWN,
            "confidence": 0.0,
            "clarification_needed": "Произошла ошибка при анализе запроса. Попробуйте переформулировать.",
        }

    def _fallback_parse(self, text: str, hints: Dict[str, Any]) -> Dict[str, Any]:
        """Fallback парсинг на основе паттернов без LLM."""
        import re

        action = hints.get("quick_action")
        if action:
            action = EditAction(action) if isinstance(action, str) else action
        else:
            action = EditAction.UNKNOWN

        # Пытаемся извлечь имя сущности из текста
        entity_name = None
        # Ищем имена с большой буквы
        name_match = re.search(r'([А-ЯA-Z][а-яa-z]+(?:\s+[А-ЯA-Z][а-яa-z]+)?)', text)
        if name_match:
            entity_name = name_match.group(1)

        # Ищем новое значение после "на" или "в"
        new_value = None
        value_match = re.search(r'(?:на|в|to)\s+["\']?([^"\']+)["\']?$', text, re.IGNORECASE)
        if value_match:
            new_value = value_match.group(1).strip()

        return {
            "action": action,
            "confidence": 0.5,
            "entity_type": hints.get("quick_entity_type"),
            "entity_name": entity_name,
            "target_field": hints.get("quick_field"),
            "new_value": new_value,
            "requires_confirmation": True,
        }

