# -*- coding: utf-8 -*-
"""
BaseIntegration — единый интерфейс для всех внешних интеграций Tessbrain.

Каждая интеграция наследует BaseIntegration и реализует:
- get_tool_definitions() — описания tools для FunctionGemma
- execute(tool_name, **kwargs) — выполнение конкретного tool

Credentials автоматически загружаются из user_integrations (Supabase).

Usage:
    class SlackIntegration(BaseIntegration):
        integration_id = "slack"
        category = "messaging"
        keywords = ["slack", "слак"]

        def get_tool_definitions(self): ...
        async def execute(self, tool_name, **kwargs): ...
"""

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class BaseIntegration(ABC):
    """Базовый класс для всех интеграций Tessbrain."""

    # === Метаданные (переопределить в подклассах) ===
    integration_id: str = ""          # "slack", "jira", "notion"
    integration_name: str = ""        # "Slack", "Jira", "Notion"
    category: str = ""                # "messaging", "tasks", "knowledge", "crm", "code", "email"

    # Ключевые слова для SkillRouter — автоматически добавляются к соответствующему skill
    keywords: List[str] = []          # ["slack", "слак", "send to slack"]

    def __init__(self, user_id: str):
        self.user_id = user_id
        self._credentials: Dict[str, str] = {}
        self._loaded = False

    async def load_credentials(self) -> bool:
        """Загрузить и дешифровать ключи из user_integrations таблицы."""
        if self._loaded:
            return bool(self._credentials)

        try:
            from backend.api.routes.integrations import get_user_integration_keys
            self._credentials = await get_user_integration_keys(self.user_id, self.integration_id)
            self._loaded = True

            if self._credentials:
                logger.info(f"[{self.integration_id}] Credentials loaded for user {self.user_id}")
                return True
            else:
                logger.debug(f"[{self.integration_id}] No credentials for user {self.user_id}")
                return False

        except Exception as e:
            logger.warning(f"[{self.integration_id}] Failed to load credentials: {e}")
            self._loaded = True
            return False

    def is_configured(self) -> bool:
        """Проверить, настроена ли интеграция (есть ли ключи)."""
        return bool(self._credentials)

    def get_key(self, key_name: str, default: str = "") -> str:
        """Получить конкретный ключ."""
        return self._credentials.get(key_name, default)

    @abstractmethod
    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        """
        Вернуть список определений tools для FunctionGemma.

        Формат каждого tool:
        {
            "name": "slack_send_message",
            "description": "Send a message to a Slack channel",
            "parameters": {
                "type": "object",
                "properties": { ... },
                "required": [...]
            }
        }
        """
        ...

    @abstractmethod
    async def execute(self, tool_name: str, **kwargs) -> str:
        """
        Выполнить конкретный tool. Возвращает JSON-строку с результатом.

        Args:
            tool_name: Имя tool (как в get_tool_definitions)
            **kwargs: Аргументы tool (из FunctionGemma)

        Returns:
            JSON-строка с результатом
        """
        ...

    def get_tool_names(self) -> List[str]:
        """Список имён всех tools этой интеграции."""
        return [t["name"] for t in self.get_tool_definitions()]
