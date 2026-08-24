# -*- coding: utf-8 -*-
"""
Template Registry - Реестр и управление шаблонами.

Функции:
- Хранение шаблонов
- Поиск по триггерам
- CRUD операции
- Фильтрация по категориям и видимости
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from .template_model import (
    BUILTIN_TEMPLATES,
    TemplateCategory,
    TemplateVisibility,
    WorkflowTemplate,
)

logger = logging.getLogger(__name__)


class TemplateRegistry:
    """
    Реестр шаблонов.
    """

    def __init__(self, db_session=None):
        self.db_session = db_session

        # In-memory хранилище
        self._templates: Dict[str, WorkflowTemplate] = {}

        # Загружаем встроенные шаблоны
        self._load_builtin_templates()

        logger.info(f"✅ TemplateRegistry initialized with {len(self._templates)} templates")

    def _load_builtin_templates(self):
        """Загружает встроенные шаблоны."""
        for template in BUILTIN_TEMPLATES:
            self._templates[template.template_id] = template
            logger.debug(f"   📋 Loaded: {template.name}")

    # ============================================
    # CRUD Operations
    # ============================================

    def create(
        self,
        template: WorkflowTemplate,
        user_id: Optional[str] = None
    ) -> WorkflowTemplate:
        """Создаёт новый шаблон."""
        if template.template_id in self._templates:
            raise ValueError(f"Template {template.template_id} already exists")

        template.created_by = user_id
        template.created_at = datetime.now().isoformat()
        template.updated_at = template.created_at

        self._templates[template.template_id] = template

        logger.info(f"📝 Created template: {template.name} ({template.template_id})")

        # TODO: сохранить в БД

        return template

    def get(self, template_id: str) -> Optional[WorkflowTemplate]:
        """Получает шаблон по ID."""
        return self._templates.get(template_id)

    def update(
        self,
        template_id: str,
        updates: Dict[str, Any],
        user_id: Optional[str] = None
    ) -> Optional[WorkflowTemplate]:
        """Обновляет шаблон."""
        template = self._templates.get(template_id)

        if not template:
            return None

        # Проверяем права (только создатель может редактировать)
        if template.created_by and user_id and template.created_by != user_id:
            if template.visibility == TemplateVisibility.PRIVATE:
                raise PermissionError("You can only edit your own private templates")

        # Обновляем поля
        for key, value in updates.items():
            if hasattr(template, key) and key not in ["template_id", "created_by", "created_at"]:
                setattr(template, key, value)

        template.updated_at = datetime.now().isoformat()

        logger.info(f"🔄 Updated template: {template.name}")

        # TODO: сохранить в БД

        return template

    def delete(
        self,
        template_id: str,
        user_id: Optional[str] = None
    ) -> bool:
        """Удаляет шаблон."""
        template = self._templates.get(template_id)

        if not template:
            return False

        # Проверяем права
        if template.created_by and user_id and template.created_by != user_id:
            raise PermissionError("You can only delete your own templates")

        # Не удаляем встроенные шаблоны
        if template.visibility == TemplateVisibility.PUBLIC and not template.created_by:
            raise PermissionError("Cannot delete builtin templates")

        del self._templates[template_id]

        logger.info(f"🗑️ Deleted template: {template.name}")

        # TODO: удалить из БД

        return True

    # ============================================
    # Query Operations
    # ============================================

    def list(
        self,
        user_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        category: Optional[TemplateCategory] = None,
        visibility: Optional[TemplateVisibility] = None,
        search: Optional[str] = None,
        tags: Optional[List[str]] = None,
        limit: int = 50,
        offset: int = 0
    ) -> List[WorkflowTemplate]:
        """
        Получает список шаблонов с фильтрацией.
        """
        templates = list(self._templates.values())

        # Фильтр по видимости
        if visibility:
            templates = [t for t in templates if t.visibility == visibility]
        else:
            # По умолчанию показываем доступные пользователю
            templates = [
                t for t in templates
                if t.visibility == TemplateVisibility.PUBLIC
                or (t.visibility == TemplateVisibility.ORGANIZATION and t.organization_id == organization_id)
                or (t.visibility == TemplateVisibility.PRIVATE and t.created_by == user_id)
            ]

        # Фильтр по категории
        if category:
            templates = [t for t in templates if t.category == category]

        # Поиск по названию и описанию
        if search:
            search_lower = search.lower()
            templates = [
                t for t in templates
                if search_lower in t.name.lower()
                or search_lower in t.description.lower()
            ]

        # Фильтр по тегам
        if tags:
            templates = [
                t for t in templates
                if any(tag in t.tags for tag in tags)
            ]

        # Сортировка по популярности
        templates.sort(key=lambda t: t.usage_count, reverse=True)

        # Пагинация
        return templates[offset:offset + limit]

    def find_by_trigger(
        self,
        query: str,
        user_id: Optional[str] = None,
        organization_id: Optional[str] = None
    ) -> Optional[WorkflowTemplate]:
        """
        Находит шаблон по триггеру.

        Returns:
            Первый подходящий шаблон или None
        """
        # Получаем доступные шаблоны
        available = self.list(user_id=user_id, organization_id=organization_id)

        for template in available:
            if template.matches_query(query):
                logger.info(f"🎯 Found template by trigger: {template.name}")
                return template

        return None

    def find_all_by_trigger(
        self,
        query: str,
        user_id: Optional[str] = None,
        organization_id: Optional[str] = None
    ) -> List[WorkflowTemplate]:
        """
        Находит все шаблоны, соответствующие триггеру.
        """
        available = self.list(user_id=user_id, organization_id=organization_id)

        matches = []
        for template in available:
            if template.matches_query(query):
                matches.append(template)

        return matches

    def get_popular(self, limit: int = 10) -> List[WorkflowTemplate]:
        """Получает популярные шаблоны."""
        templates = [t for t in self._templates.values() if t.visibility == TemplateVisibility.PUBLIC]
        templates.sort(key=lambda t: t.usage_count, reverse=True)
        return templates[:limit]

    def get_recent(self, limit: int = 10) -> List[WorkflowTemplate]:
        """Получает недавно использованные шаблоны."""
        templates = [t for t in self._templates.values() if t.last_used_at]
        templates.sort(key=lambda t: t.last_used_at or "", reverse=True)
        return templates[:limit]

    def get_by_category(self, category: TemplateCategory) -> List[WorkflowTemplate]:
        """Получает шаблоны по категории."""
        return [t for t in self._templates.values() if t.category == category]

    def get_user_templates(self, user_id: str) -> List[WorkflowTemplate]:
        """Получает шаблоны пользователя."""
        return [t for t in self._templates.values() if t.created_by == user_id]

    # ============================================
    # Statistics
    # ============================================

    def get_stats(self) -> Dict[str, Any]:
        """Получает статистику по шаблонам."""
        templates = list(self._templates.values())

        return {
            "total_templates": len(templates),
            "by_category": {
                cat.value: len([t for t in templates if t.category == cat])
                for cat in TemplateCategory
            },
            "by_visibility": {
                vis.value: len([t for t in templates if t.visibility == vis])
                for vis in TemplateVisibility
            },
            "total_executions": sum(t.usage_count for t in templates),
            "most_popular": [
                {"id": t.template_id, "name": t.name, "count": t.usage_count}
                for t in sorted(templates, key=lambda t: t.usage_count, reverse=True)[:5]
            ]
        }


# ============================================
# Singleton
# ============================================

_registry: Optional[TemplateRegistry] = None


def get_template_registry(db_session=None) -> TemplateRegistry:
    """Получает singleton экземпляр TemplateRegistry."""
    global _registry

    if _registry is None:
        _registry = TemplateRegistry(db_session=db_session)

    return _registry


