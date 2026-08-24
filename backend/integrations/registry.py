# -*- coding: utf-8 -*-
"""
IntegrationRegistry — реестр всех интеграций с динамической загрузкой.

Автоматически собирает tools из всех настроенных интеграций пользователя
и предоставляет их для FunctionGemma и SkillRouter.

Usage:
    registry = IntegrationRegistry()

    # Загрузить все интеграции пользователя
    await registry.load_for_user("user_123")

    # Получить все tools (для FunctionGemma catalog)
    all_tools = registry.get_all_tool_definitions()

    # Выполнить tool
    result = await registry.execute_tool("slack_send_message", channel="#general", text="Hello!")

    # Получить keywords для SkillRouter
    keywords = registry.get_keywords_by_category("messaging")
"""

import json
import logging
from typing import Any, Dict, List, Optional, Type

from backend.integrations.base import BaseIntegration

logger = logging.getLogger(__name__)


# Глобальный реестр классов интеграций
_INTEGRATION_CLASSES: Dict[str, Type[BaseIntegration]] = {}


def register_integration(cls: Type[BaseIntegration]) -> Type[BaseIntegration]:
    """
    Декоратор для регистрации класса интеграции.

    Usage:
        @register_integration
        class SlackIntegration(BaseIntegration):
            integration_id = "slack"
            ...
    """
    if cls.integration_id:
        _INTEGRATION_CLASSES[cls.integration_id] = cls
        logger.debug(f"Registered integration: {cls.integration_id}")
    return cls


class IntegrationRegistry:
    """
    Реестр интеграций для конкретного пользователя.

    Загружает только те интеграции, для которых пользователь настроил ключи.
    """

    def __init__(self):
        self._integrations: Dict[str, BaseIntegration] = {}
        self._user_id: str = ""
        self._loaded = False

    async def load_for_user(self, user_id: str) -> int:
        """
        Загрузить все настроенные интеграции для пользователя.

        Returns:
            Количество успешно загруженных интеграций
        """
        self._user_id = user_id
        self._integrations.clear()
        loaded_count = 0

        # Импортируем все модули интеграций (чтобы @register_integration сработал)
        _import_all_integrations()

        for integration_id, cls in _INTEGRATION_CLASSES.items():
            try:
                instance = cls(user_id)
                has_credentials = await instance.load_credentials()

                if has_credentials:
                    self._integrations[integration_id] = instance
                    loaded_count += 1
                    logger.info(f"[Registry] Loaded integration: {integration_id}")

            except Exception as e:
                logger.warning(f"[Registry] Failed to load {integration_id}: {e}")

        self._loaded = True
        logger.info(f"[Registry] Loaded {loaded_count}/{len(_INTEGRATION_CLASSES)} integrations for user {user_id}")
        return loaded_count

    def get_all_tool_definitions(self) -> List[Dict[str, Any]]:
        """Получить определения всех tools из всех загруженных интеграций."""
        all_tools = []
        for integration in self._integrations.values():
            all_tools.extend(integration.get_tool_definitions())
        return all_tools

    def get_all_tool_names(self) -> List[str]:
        """Список имён всех доступных tools."""
        names = []
        for integration in self._integrations.values():
            names.extend(integration.get_tool_names())
        return names

    def get_tool_names_by_category(self, category: str) -> List[str]:
        """Получить имена tools из интеграций определённой категории."""
        names = []
        for integration in self._integrations.values():
            if integration.category == category:
                names.extend(integration.get_tool_names())
        return names

    def get_keywords_by_category(self, category: str) -> List[str]:
        """Получить все keywords для SkillRouter из интеграций категории."""
        keywords = []
        for cls in _INTEGRATION_CLASSES.values():
            if cls.category == category:
                keywords.extend(cls.keywords)
        return keywords

    def get_all_keywords(self) -> Dict[str, List[str]]:
        """Получить keywords всех интеграций, сгруппированные по категории."""
        result: Dict[str, List[str]] = {}
        for cls in _INTEGRATION_CLASSES.values():
            cat = cls.category
            if cat not in result:
                result[cat] = []
            result[cat].extend(cls.keywords)
        return result

    def find_integration_for_tool(self, tool_name: str) -> Optional[BaseIntegration]:
        """Найти интеграцию, которой принадлежит tool."""
        for integration in self._integrations.values():
            if tool_name in integration.get_tool_names():
                return integration
        return None

    def get(self, integration_id: str) -> Optional[BaseIntegration]:
        """Получить загруженный экземпляр интеграции по integration_id.

        Используется Coffee delivery (gmail/notion) и Reactive load
        estimator (google_calendar), которым нужен прямой доступ к
        конкретной интеграции, не через tool-name lookup. Возвращает
        None если интеграция не загружена для юзера.
        """
        return self._integrations.get(integration_id)

    async def execute_tool(self, tool_name: str, **kwargs) -> str:
        """
        Выполнить tool, автоматически найдя нужную интеграцию.

        Returns:
            JSON-строка с результатом
        """
        integration = self.find_integration_for_tool(tool_name)
        if not integration:
            return json.dumps({"error": f"No integration found for tool: {tool_name}"}, ensure_ascii=False)

        try:
            return await integration.execute(tool_name, **kwargs)
        except Exception as e:
            logger.error(f"[Registry] Tool {tool_name} execution error: {e}")
            return json.dumps({"error": f"Tool execution error: {e!s}"}, ensure_ascii=False)

    def is_integration_loaded(self, integration_id: str) -> bool:
        """Проверить, загружена ли интеграция."""
        return integration_id in self._integrations

    def list_loaded(self) -> List[Dict[str, Any]]:
        """Список загруженных интеграций для UI."""
        return [
            {
                "id": i.integration_id,
                "name": i.integration_name,
                "category": i.category,
                "tools_count": len(i.get_tool_names()),
                "tools": i.get_tool_names(),
            }
            for i in self._integrations.values()
        ]

    @staticmethod
    def get_all_registered() -> List[Dict[str, str]]:
        """Список всех зарегистрированных классов интеграций (для UI, не зависит от пользователя)."""
        _import_all_integrations()
        return [
            {
                "id": cls.integration_id,
                "name": cls.integration_name,
                "category": cls.category,
            }
            for cls in _INTEGRATION_CLASSES.values()
        ]


def _import_all_integrations():
    """Импортировать все модули интеграций для срабатывания @register_integration."""
    _modules = [
        "slack_integration",
        "notion_integration",
        "jira_integration",
        "trello_integration",
        "gmail_integration",
        "github_integration",
        "google_sheets_integration",
        "google_docs_integration",
        "google_drive_integration",
        "hubspot_integration",
        "whatsapp_integration",
        "linear_integration",
        "confluence_integration",
        "figma_integration",
    ]
    for mod_name in _modules:
        try:
            __import__(f"backend.integrations.{mod_name}")
        except ImportError:
            logger.debug("suppressed exception", exc_info=True)
