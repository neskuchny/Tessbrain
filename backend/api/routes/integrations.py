# -*- coding: utf-8 -*-
"""
API для управления интеграциями пользователя.

Работает с таблицей user_integrations в Supabase.
Ключи шифруются перед сохранением.
"""
import base64
import hashlib
import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List

from cryptography.fernet import Fernet
from litestar import Controller, Request, delete, get, post
from litestar.params import Parameter
from pydantic import BaseModel, Field


def _verify_user(request: Request, user_id: str) -> str:
    """Анти-IDOR: при валидном токене user_id берётся из него; спуфинг чужого
    → исключение → 403 (litestar). Без токена — query как есть (закрывается
    enable_strict_chat_auth в trusted_user_id)."""
    from litestar.exceptions import PermissionDeniedException
    try:
        from backend.core.auth.service_token import trusted_user_id
        uid, src = trusted_user_id(request.headers, user_id or "")
        if src != "unverified" and uid:
            return uid
    except PermissionError:
        raise PermissionDeniedException("token not authorized for requested user_id")
    except Exception:
        pass
    return user_id

logger = logging.getLogger(__name__)

# Encryption key from env (or generate one)
ENCRYPTION_KEY = os.getenv("INTEGRATIONS_ENCRYPTION_KEY", "")
if not ENCRYPTION_KEY:
    # Generate a key from a secret (not ideal for production, but works)
    secret = os.getenv("SUPABASE_KEY", "default-secret-key")
    ENCRYPTION_KEY = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
else:
    ENCRYPTION_KEY = ENCRYPTION_KEY.encode()

_fernet = Fernet(ENCRYPTION_KEY)


def encrypt_secret(secret: str) -> str:
    """Encrypt a secret value"""
    return _fernet.encrypt(secret.encode()).decode()


_decrypt_warned = False


def decrypt_secret(encrypted: str) -> str:
    """Decrypt a secret value.

    Возвращает "" при неудаче (обратная совместимость: вызывающие проверяют
    `if not key`). Спам-лог убран: раньше КАЖДЫЙ неразобравшийся секрет писал
    ERROR (список из 5 ключей → 5 одинаковых строк). Теперь — одно WARNING на
    процесс с подсказкой про смену ключа шифрования (INTEGRATIONS_ENCRYPTION_KEY)."""
    global _decrypt_warned
    try:
        return _fernet.decrypt(encrypted.encode()).decode()
    except Exception as e:
        if not _decrypt_warned:
            _decrypt_warned = True
            logger.warning(
                "Failed to decrypt integration secret (%s). Обычно это смена "
                "ключа шифрования: восстановите прежний INTEGRATIONS_ENCRYPTION_KEY "
                "или перезадайте ключи во вкладке «Интеграции». Платформенный "
                "Telegram-бот от этого НЕ зависит (токен берётся из env).", e)
        else:
            logger.debug("decrypt secret failed: %s", e)
        return ""


# ============ Provider Definitions ============

INTEGRATION_PROVIDERS = {
    # Messaging & Communication
    "telegram": {
        "name": "Telegram",
        "icon": "💬",
        "category": "messaging",
        "keys": [
            {"key_name": "bot_token", "label": "Bot Token", "placeholder": "123456:ABC-DEF...", "required": True},
            {"key_name": "default_chat_id", "label": "Default Chat ID", "placeholder": "-100123456789", "required": False},
        ],
        "contacts_support": True,  # Can add user contacts (usernames/chat_ids)
        "description": "Отправка сообщений через Telegram бота"
    },
    "slack": {
        "name": "Slack",
        "icon": "💼",
        "category": "messaging",
        "keys": [
            {"key_name": "bot_token", "label": "Bot Token", "placeholder": "xoxb-...", "required": True},
            {"key_name": "webhook_url", "label": "Webhook URL", "placeholder": "https://hooks.slack.com/...", "required": False},
        ],
        "contacts_support": True,
        "description": "Интеграция со Slack"
    },

    # Email
    "gmail": {
        "name": "Gmail",
        "icon": "📧",
        "category": "email",
        "keys": [
            {"key_name": "oauth_token", "label": "OAuth Token (JSON)", "placeholder": '{"access_token": "...", "refresh_token": "..."}', "required": True, "multiline": True},
        ],
        "contacts_support": True,  # Email addresses
        "description": "Отправка и чтение писем через Gmail"
    },

    # Calendar
    "google_calendar": {
        "name": "Google Calendar",
        "icon": "📅",
        "category": "calendar",
        "keys": [
            {"key_name": "oauth_token", "label": "OAuth Token (JSON)", "placeholder": '{"access_token": "...", "refresh_token": "..."}', "required": True, "multiline": True},
        ],
        "description": "Управление событиями в Google Calendar"
    },

    # Task Management
    "trello": {
        "name": "Trello",
        "icon": "📋",
        "category": "tasks",
        "keys": [
            {"key_name": "api_key", "label": "API Key", "placeholder": "...", "required": True},
            {"key_name": "token", "label": "Token", "placeholder": "...", "required": True},
            {"key_name": "default_board_id", "label": "Default Board ID", "placeholder": "...", "required": False},
        ],
        "description": "Управление задачами в Trello"
    },
    "jira": {
        "name": "Jira",
        "icon": "🎫",
        "category": "tasks",
        "keys": [
            {"key_name": "domain", "label": "Jira Domain", "placeholder": "your-company.atlassian.net", "required": True},
            {"key_name": "email", "label": "Email", "placeholder": "your@email.com", "required": True},
            {"key_name": "api_token", "label": "API Token", "placeholder": "...", "required": True},
            {"key_name": "default_project", "label": "Default Project Key", "placeholder": "PROJ", "required": False},
        ],
        "description": "Управление задачами в Jira"
    },
    "yougile": {
        "name": "YouGile",
        "icon": "✅",
        "category": "tasks",
        "keys": [
            {"key_name": "api_key", "label": "API Key", "placeholder": "...", "required": True},
            {"key_name": "company_id", "label": "Company ID", "placeholder": "...", "required": False},
        ],
        "description": "Управление задачами в YouGile"
    },

    # Knowledge & Docs
    "notion": {
        "name": "Notion",
        "icon": "📝",
        "category": "knowledge",
        "keys": [
            {"key_name": "integration_token", "label": "Integration Token", "placeholder": "secret_...", "required": True},
            {"key_name": "default_page_id", "label": "Default Page ID", "placeholder": "...", "required": False},
        ],
        "description": "Создание страниц и баз данных в Notion"
    },
    "confluence": {
        "name": "Confluence",
        "icon": "📚",
        "category": "knowledge",
        "keys": [
            {"key_name": "domain", "label": "Confluence Domain", "placeholder": "your-company.atlassian.net", "required": True},
            {"key_name": "email", "label": "Email", "placeholder": "your@email.com", "required": True},
            {"key_name": "api_token", "label": "API Token", "placeholder": "...", "required": True},
        ],
        "description": "Работа с документами в Confluence"
    },

    # CRM & Analytics
    "hubspot": {
        "name": "HubSpot",
        "icon": "🧲",
        "category": "crm",
        "keys": [
            {"key_name": "api_key", "label": "Private App Token", "placeholder": "pat-...", "required": True},
        ],
        "description": "CRM и маркетинг автоматизация HubSpot"
    },
    "amocrm": {
        "name": "amoCRM",
        "icon": "📇",
        "category": "crm",
        "keys": [
            {"key_name": "base_url", "label": "Адрес аккаунта", "placeholder": "https://mycompany.amocrm.ru", "required": True},
            {"key_name": "access_token", "label": "Долгосрочный токен", "placeholder": "eyJ...", "required": True},
        ],
        "description": "Сделки и контакты amoCRM: чтение в датасеты мозга (регулярный синк) и запись результатов (write-back)"
    },
    "bitrix24": {
        "name": "Битрикс24",
        "icon": "🅱️",
        "category": "crm",
        "keys": [
            {"key_name": "webhook_url", "label": "Входящий вебхук", "placeholder": "https://mycompany.bitrix24.ru/rest/1/xxxx/", "required": True},
        ],
        "description": "Сделки, лиды и задачи Битрикс24: чтение в датасеты мозга и запись результатов"
    },
    "odata_1c": {
        "name": "1С (OData)",
        "icon": "1️⃣",
        "category": "crm",
        "keys": [
            {"key_name": "base_url", "label": "OData-адрес базы", "placeholder": "http://server/base/odata/standard.odata", "required": True},
            {"key_name": "username", "label": "Пользователь", "placeholder": "ivanov", "required": False},
            {"key_name": "password", "label": "Пароль", "placeholder": "••••••", "required": False},
        ],
        "description": "Справочники и документы 1С по OData: регулярная выгрузка в датасеты онтологии (коннектор за флагом на сервере)"
    },
    "yandex_direct": {
        "name": "Яндекс.Директ / Wordstat",
        "icon": "📏",
        "category": "analytics",
        "keys": [
            {"key_name": "oauth_token", "label": "OAuth-токен",
             "placeholder": "y0_AgA...", "required": True},
        ],
        "description": "Wordstat-оценка размера сегментов в исследованиях "
                       "Марка (OAuth-токен Яндекса с доступом к API Директа)"
    },
    "vk": {
        "name": "VK (парсинг групп)",
        "icon": "🔵",
        "category": "analytics",
        "keys": [
            {"key_name": "service_key", "label": "Сервисный ключ",
             "placeholder": "сервисный ключ доступа приложения VK",
             "required": True},
        ],
        "description": "Чтение открытых групп VK в исследованиях Марка "
                       "(создайте приложение на dev.vk.com → сервисный ключ)"
    },
    "calltouch": {
        "name": "Calltouch",
        "icon": "📞",
        "category": "analytics",
        "keys": [
            {"key_name": "api_key", "label": "API Key", "placeholder": "...", "required": True},
            {"key_name": "site_id", "label": "Site ID", "placeholder": "...", "required": True},
        ],
        "description": "Аналитика звонков Calltouch"
    },
    "roistat": {
        "name": "Roistat",
        "icon": "📊",
        "category": "analytics",
        "keys": [
            {"key_name": "api_key", "label": "API Key", "placeholder": "...", "required": True},
            {"key_name": "project_id", "label": "Project ID", "placeholder": "...", "required": True},
        ],
        "description": "Сквозная аналитика Roistat"
    },

    # Google Workspace
    "google_sheets": {
        "name": "Google Sheets",
        "icon": "📊",
        "category": "productivity",
        "keys": [
            {"key_name": "oauth_token", "label": "OAuth Token (JSON)", "placeholder": '{"access_token": "...", "refresh_token": "..."}', "required": True, "multiline": True},
        ],
        "description": "Чтение, запись и добавление данных в Google Таблицы"
    },
    "google_docs": {
        "name": "Google Docs",
        "icon": "📄",
        "category": "productivity",
        "keys": [
            {"key_name": "oauth_token", "label": "OAuth Token (JSON)", "placeholder": '{"access_token": "...", "refresh_token": "..."}', "required": True, "multiline": True},
        ],
        "description": "Чтение, создание и экспорт документов Google Docs"
    },
    "google_drive": {
        "name": "Google Drive",
        "icon": "📁",
        "category": "productivity",
        "keys": [
            {"key_name": "oauth_token", "label": "OAuth Token (JSON)", "placeholder": '{"access_token": "...", "refresh_token": "..."}', "required": True, "multiline": True},
        ],
        "description": "Поиск, список файлов и чтение из Google Drive"
    },

    # Messaging (additional)
    "whatsapp": {
        "name": "WhatsApp",
        "icon": "💚",
        "category": "messaging",
        "keys": [
            {"key_name": "access_token", "label": "Meta Business Token", "placeholder": "EAA...", "required": True},
            {"key_name": "phone_number_id", "label": "Phone Number ID", "placeholder": "1234567890", "required": True},
        ],
        "description": "Отправка сообщений через WhatsApp Business API"
    },

    # Task Management (additional)
    "linear": {
        "name": "Linear",
        "icon": "⚡",
        "category": "tasks",
        "keys": [
            {"key_name": "api_key", "label": "API Key", "placeholder": "lin_api_...", "required": True},
        ],
        "description": "Issues, проекты и циклы в Linear"
    },

    # Design
    "figma": {
        "name": "Figma",
        "icon": "🎨",
        "category": "design",
        "keys": [
            {"key_name": "access_token", "label": "Personal Access Token", "placeholder": "figd_...", "required": True},
        ],
        "description": "Доступ к файлам и проектам Figma"
    },

    # AI Video & Image Generation
    "runway": {
        "name": "Runway ML",
        "icon": "🎬",
        "category": "ai_generation",
        "keys": [
            {"key_name": "api_key", "label": "API Key", "placeholder": "...", "required": True},
        ],
        "description": "AI генерация видео (Gen-3)"
    },
    "kling": {
        "name": "Kling AI",
        "icon": "🎥",
        "category": "ai_generation",
        "keys": [
            {"key_name": "api_key", "label": "API Key", "placeholder": "...", "required": True},
        ],
        "description": "AI генерация видео Kling"
    },
    "luma": {
        "name": "Luma Dream Machine",
        "icon": "✨",
        "category": "ai_generation",
        "keys": [
            {"key_name": "api_key", "label": "API Key", "placeholder": "...", "required": True},
        ],
        "description": "AI генерация видео Luma"
    },
    "heygen": {
        "name": "HeyGen",
        "icon": "🎭",
        "category": "ai_generation",
        "keys": [
            {"key_name": "api_key", "label": "API Key", "placeholder": "...", "required": True},
        ],
        "description": "AI аватары и видео HeyGen"
    },
    "elevenlabs": {
        "name": "ElevenLabs",
        "icon": "🔊",
        "category": "ai_generation",
        "keys": [
            {"key_name": "api_key", "label": "API Key", "placeholder": "...", "required": True},
        ],
        "description": "AI озвучка ElevenLabs"
    },
    "gamma": {
        "name": "Gamma",
        "icon": "📊",
        "category": "ai_generation",
        "keys": [
            {"key_name": "api_key", "label": "API Key", "placeholder": "...", "required": True},
        ],
        "description": "AI презентации Gamma"
    },

    # Code & DevOps
    "github": {
        "name": "GitHub",
        "icon": "🐙",
        "category": "code",
        "keys": [
            {"key_name": "api_token", "label": "Personal Access Token", "placeholder": "ghp_...", "required": True},
            {"key_name": "default_owner", "label": "Default Owner/Org", "placeholder": "your-org", "required": False},
        ],
        "description": "GitHub PRs, issues, CI/CD статусы, поиск кода"
    },

    # Browser Automation
    "multion": {
        "name": "MultiOn",
        "icon": "🌐",
        "category": "automation",
        "keys": [
            {"key_name": "api_key", "label": "API Key", "placeholder": "...", "required": True},
        ],
        "description": "AI автоматизация браузера MultiOn"
    },
    "browseruse": {
        "name": "Browser Use",
        "icon": "🖥️",
        "category": "automation",
        "keys": [
            {"key_name": "api_key", "label": "API Key", "placeholder": "...", "required": False},
        ],
        "description": "Автоматизация браузера (локальная)"
    },

    # LLM Providers
    "openai": {
        "name": "OpenAI",
        "icon": "🤖",
        "category": "llm",
        "keys": [
            {"key_name": "api_key", "label": "API Key", "placeholder": "sk-...", "required": True},
            {"key_name": "org_id", "label": "Organization ID", "placeholder": "org-...", "required": False},
        ],
        "description": "GPT-4, GPT-4o, o1 и другие модели OpenAI"
    },
    "google_ai": {
        "name": "Google AI (Gemini)",
        "icon": "🔮",
        "category": "llm",
        "keys": [
            {"key_name": "api_key", "label": "API Key", "placeholder": "AIza...", "required": True},
        ],
        "description": "Gemini Pro, Gemini Flash и другие модели Google"
    },
    "anthropic": {
        "name": "Anthropic (Claude)",
        "icon": "🧠",
        "category": "llm",
        "keys": [
            {"key_name": "api_key", "label": "API Key", "placeholder": "sk-ant-...", "required": True},
        ],
        "description": "Claude 3.5 Sonnet, Claude 3 Opus и другие"
    },
    "openrouter": {
        "name": "OpenRouter",
        "icon": "🔀",
        "category": "llm",
        "keys": [
            {"key_name": "api_key", "label": "API Key", "placeholder": "sk-or-...", "required": True},
        ],
        "description": "Единый ключ ко многим моделям: DeepSeek, Qwen, Kimi, GLM, "
                       "Grok, Gemini и др. Удобно для тиринга прогона встреч."
    },
    "deepseek": {
        "name": "DeepSeek",
        "icon": "🐋",
        "category": "llm",
        "keys": [
            {"key_name": "api_key", "label": "API Key", "placeholder": "sk-...", "required": True},
        ],
        "description": "DeepSeek V4 Flash/Pro — дёшево, для извлечения"
    },
    "xai": {
        "name": "xAI (Grok)",
        "icon": "𝕏",
        "category": "llm",
        "keys": [
            {"key_name": "api_key", "label": "API Key", "placeholder": "xai-...", "required": True},
        ],
        "description": "Grok 4.5 и другие модели xAI"
    },
    "qwen": {
        "name": "Qwen (Alibaba)",
        "icon": "🌊",
        "category": "llm",
        "keys": [
            {"key_name": "api_key", "label": "API Key", "placeholder": "sk-...", "required": True},
        ],
        "description": "Qwen 3.6-27b / 235B-A22B через DashScope — для извлечения встреч"
    },
    "perplexity": {
        "name": "Perplexity",
        "icon": "🔍",
        "category": "llm",
        "keys": [
            {"key_name": "api_key", "label": "API Key", "placeholder": "pplx-...", "required": True},
        ],
        "description": "Perplexity AI с поиском в интернете"
    },
    "groq": {
        "name": "Groq",
        "icon": "⚡",
        "category": "llm",
        "keys": [
            {"key_name": "api_key", "label": "API Key", "placeholder": "gsk_...", "required": True},
        ],
        "description": "Быстрый инференс LLM (Llama, Mixtral)"
    },
    "together": {
        "name": "Together AI",
        "icon": "🤝",
        "category": "llm",
        "keys": [
            {"key_name": "api_key", "label": "API Key", "placeholder": "...", "required": True},
        ],
        "description": "Open source модели (Llama, Mistral, etc.)"
    },
    "deepseek": {
        "name": "DeepSeek",
        "icon": "🌊",
        "category": "llm",
        "keys": [
            {"key_name": "api_key", "label": "API Key", "placeholder": "sk-...", "required": True},
        ],
        "description": "DeepSeek Coder и Chat модели"
    },
    "mistral": {
        "name": "Mistral AI",
        "icon": "🌀",
        "category": "llm",
        "keys": [
            {"key_name": "api_key", "label": "API Key", "placeholder": "...", "required": True},
        ],
        "description": "Mistral Large, Medium, Small"
    },
    "cohere": {
        "name": "Cohere",
        "icon": "💎",
        "category": "llm",
        "keys": [
            {"key_name": "api_key", "label": "API Key", "placeholder": "...", "required": True},
        ],
        "description": "Command R+, Embed и Rerank модели"
    },

    # Search & Data
    "tavily": {
        "name": "Tavily",
        "icon": "🔎",
        "category": "search",
        "keys": [
            {"key_name": "api_key", "label": "API Key", "placeholder": "tvly-...", "required": True},
        ],
        "description": "AI поиск для агентов"
    },
    "serper": {
        "name": "Serper",
        "icon": "🌐",
        "category": "search",
        "keys": [
            {"key_name": "api_key", "label": "API Key", "placeholder": "...", "required": True},
        ],
        "description": "Google Search API"
    },
    "firecrawl": {
        "name": "Firecrawl",
        "icon": "🔥",
        "category": "search",
        "keys": [
            {"key_name": "api_key", "label": "API Key", "placeholder": "fc-...", "required": True},
        ],
        "description": "Web scraping для LLM"
    },
    "roistat": {
        "name": "Roistat",
        "icon": "📈",
        "category": "analytics",
        "keys": [
            {"key_name": "api_key", "label": "API Key",
             "placeholder": "ключ из профиля Roistat", "required": True},
            {"key_name": "project_id", "label": "Номер проекта",
             "placeholder": "12345", "required": True},
        ],
        "description": "Сквозная аналитика: каналы, лиды, выручка, ROI → датасет мозга"
    },
    "calltouch": {
        "name": "Calltouch",
        "icon": "📞",
        "category": "analytics",
        "keys": [
            {"key_name": "api_token", "label": "API-токен (clientApiId)",
             "placeholder": "токен из настроек API", "required": True},
            {"key_name": "site_id", "label": "ID сайта",
             "placeholder": "12345", "required": True},
        ],
        "description": "Коллтрекинг: журнал звонков с UTM → датасет мозга"
    },
    "callibri": {
        "name": "Callibri",
        "icon": "☎️",
        "category": "analytics",
        "keys": [
            {"key_name": "user_email", "label": "E-mail аккаунта",
             "placeholder": "you@company.ru", "required": True},
            {"key_name": "user_token", "label": "API-токен",
             "placeholder": "токен из настроек", "required": True},
            {"key_name": "site_id", "label": "ID проекта",
             "placeholder": "12345", "required": True},
        ],
        "description": "Коллтрекинг: обращения по каналам (звонки/чаты/заявки) → датасет мозга"
    },
    "comagic": {
        "name": "CoMagic / UIS",
        "icon": "📊",
        "category": "analytics",
        "keys": [
            {"key_name": "access_token", "label": "Access Token (Data API)",
             "placeholder": "токен Data API", "required": True},
            {"key_name": "api_host", "label": "Хост API (опц.)",
             "placeholder": "dataapi.comagic.ru или dataapi.uiscom.ru",
             "required": False},
        ],
        "description": "Коллтрекинг CoMagic/UIS: отчёт по звонкам → датасет мозга"
    },
    "callinsight": {
        "name": "CallInsight",
        "icon": "🎧",
        "category": "analytics",
        "keys": [
            {"key_name": "api_url", "label": "Адрес API",
             "placeholder": "https://callinsight.example.com", "required": True},
            {"key_name": "api_key", "label": "API Key",
             "placeholder": "mfk_...", "required": True},
        ],
        "description": "Звонки/чаты КЦ: клиенты, события, аналитика услуг → датасет мозга"
    },
}

CATEGORIES = {
    "llm": {"name": "LLM Провайдеры", "icon": "🤖"},
    "search": {"name": "Поиск и данные", "icon": "🔎"},
    "messaging": {"name": "Мессенджеры", "icon": "💬"},
    "email": {"name": "Почта", "icon": "📧"},
    "calendar": {"name": "Календарь", "icon": "📅"},
    "tasks": {"name": "Задачи", "icon": "✅"},
    "knowledge": {"name": "Знания и документы", "icon": "📚"},
    "crm": {"name": "CRM", "icon": "🧲"},
    "analytics": {"name": "Аналитика", "icon": "📊"},
    "design": {"name": "Дизайн", "icon": "🎨"},
    "ai_generation": {"name": "AI генерация", "icon": "🎬"},
    "automation": {"name": "Автоматизация", "icon": "⚡"},
}


# ============ Request/Response Models ============

class IntegrationKeyInput(BaseModel):
    """Input for adding/updating an integration key"""
    provider: str = Field(..., description="Provider ID (telegram, notion, etc.)")
    key_name: str = Field(..., description="Key name (bot_token, api_key, etc.)")
    secret_value: str = Field(..., description="The secret value to store")
    meta: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata")


class ContactInput(BaseModel):
    """Input for adding a contact to an integration"""
    provider: str = Field(..., description="Provider ID")
    contact_type: str = Field(..., description="Type: telegram_user, email, slack_channel, etc.")
    contact_value: str = Field(..., description="The contact value (username, email, channel_id)")
    display_name: str = Field(default="", description="Human-readable name")
    meta: Dict[str, Any] = Field(default_factory=dict)


class IntegrationsController(Controller):
    """Controller for user integrations"""

    path = "/integrations"
    tags = ["Integrations"]

    @get("/providers")
    async def list_providers(self) -> Dict[str, Any]:
        """
        Get list of available integration providers.
        """
        # Group by category
        by_category = {}
        for provider_id, provider in INTEGRATION_PROVIDERS.items():
            cat = provider.get("category", "other")
            if cat not in by_category:
                by_category[cat] = {
                    **CATEGORIES.get(cat, {"name": cat, "icon": "📦"}),
                    "providers": []
                }
            by_category[cat]["providers"].append({
                "id": provider_id,
                **provider
            })

        return {
            "categories": by_category,
            "providers": INTEGRATION_PROVIDERS
        }

    @post("/telegram/test")
    async def telegram_test(
        self,
        request: Request,
        user_id: str = Parameter(query="user_id"),
    ) -> Dict[str, Any]:
        """Реальная проба доставки в Telegram: резолвим токен+chat_id и шлём
        тестовое сообщение. Возвращаем ТОЧНУЮ причину провала (нет токена / нет
        chat_id / ответ Telegram), чтобы «ничего не происходит» стало понятным."""
        user_id = _verify_user(request, user_id)
        from backend.core.messengers.links import (
            resolve_telegram_bot_token,
            resolve_telegram_chat_id,
        )
        token = await resolve_telegram_bot_token(user_id)
        chat_id = await resolve_telegram_chat_id(user_id)
        if not token:
            return {"ok": False, "reason": "no_token",
                    "message": ("Не задан токен бота. Впишите Bot Token в «Интеграции "
                                "→ Telegram» (получить у @BotFather) или задайте "
                                "TELEGRAM_BOT_TOKEN в окружении бэкенда.")}
        if not chat_id:
            return {"ok": False, "reason": "no_chat",
                    "message": ("Не задан Chat ID. Впишите Default Chat ID в «Интеграции "
                                "→ Telegram» (ваш ID узнать у @userinfobot) или привяжите "
                                "Telegram кнопкой «Подключить».")}
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id,
                          "text": "✅ Tessbrain: тестовое сообщение. Доставка в Telegram работает."})
            payload = {}
            try:
                payload = r.json()
            except Exception:
                payload = {}
            if r.status_code == 200 and payload.get("ok"):
                return {"ok": True, "chat_id": chat_id,
                        "message": f"Отправлено в чат {chat_id}. Проверьте Telegram."}
            desc = str(payload.get("description") or r.text or "")[:300]
            return {"ok": False, "reason": "telegram_error", "chat_id": chat_id,
                    "message": (f"Telegram отклонил отправку: {desc}. Частые причины: бот "
                                "не добавлен в группу/канал, неверный Chat ID, или токен "
                                "невалиден.")}
        except Exception as e:
            return {"ok": False, "reason": "network_error",
                    "message": f"Ошибка сети при отправке: {e}"}

    @get("/")
    async def list_user_integrations(
        self,
        request: Request,
        user_id: str = Parameter(query="user_id"),
    ) -> Dict[str, Any]:
        """
        Get all integrations for a user. Returns configured providers with
        masked secrets.
        """
        user_id = _verify_user(request, user_id)
        from backend.db.supabase_client import get_supabase_client

        try:
            supabase = get_supabase_client()

            # Get all user integrations using _request method
            integrations = await supabase._request(
                "GET",
                "/rest/v1/user_integrations",
                params={"user_id": f"eq.{user_id}", "select": "*"}
            ) or []

            # Group by provider and mask secrets
            by_provider = {}
            for item in integrations:
                provider = item["provider"]
                if provider not in by_provider:
                    provider_info = INTEGRATION_PROVIDERS.get(provider, {"name": provider, "icon": "📦"})
                    by_provider[provider] = {
                        "provider": provider,
                        "name": provider_info.get("name", provider),
                        "icon": provider_info.get("icon", "📦"),
                        "category": provider_info.get("category", "other"),
                        "configured": True,
                        "keys": {},
                        "contacts": []
                    }

                key_name = item["key_name"]

                # Check if this is a contact entry
                if key_name.startswith("contact_"):
                    by_provider[provider]["contacts"].append({
                        "id": item["id"],
                        "type": key_name.replace("contact_", ""),
                        "value": decrypt_secret(item["secret_enc"]),  # Contacts are not super-secret
                        "display_name": item["meta"].get("display_name", ""),
                        "meta": item["meta"]
                    })
                else:
                    # Mask the secret
                    decrypted = decrypt_secret(item["secret_enc"])
                    masked = decrypted[:4] + "..." + decrypted[-4:] if len(decrypted) > 8 else "****"

                    by_provider[provider]["keys"][key_name] = {
                        "id": item["id"],
                        "key_name": key_name,
                        "masked_value": masked,
                        "is_set": bool(decrypted),
                        "updated_at": item["updated_at"],
                        "meta": item["meta"]
                    }

            # Add unconfigured providers
            for provider_id, provider_info in INTEGRATION_PROVIDERS.items():
                if provider_id not in by_provider:
                    by_provider[provider_id] = {
                        "provider": provider_id,
                        "name": provider_info.get("name", provider_id),
                        "icon": provider_info.get("icon", "📦"),
                        "category": provider_info.get("category", "other"),
                        "configured": False,
                        "keys": {},
                        "contacts": []
                    }

            return {
                "user_id": user_id,
                "integrations": list(by_provider.values()),
                "total_configured": sum(1 for p in by_provider.values() if p["configured"])
            }

        except Exception as e:
            logger.error(f"Failed to list integrations: {e}")
            return {"error": str(e), "integrations": []}

    @post("/keys")
    async def set_integration_key(
        self,
        data: IntegrationKeyInput,
        request: Request,
        user_id: str = Parameter(query="user_id"),
    ) -> Dict[str, Any]:
        """Set or update an integration key."""
        user_id = _verify_user(request, user_id)
        from backend.db.supabase_client import get_supabase_client

        try:
            supabase = get_supabase_client()

            # Validate provider
            if data.provider not in INTEGRATION_PROVIDERS:
                return {"success": False, "error": f"Unknown provider: {data.provider}"}

            # Encrypt the secret
            encrypted = encrypt_secret(data.secret_value)

            # Upsert (insert or update) - using POST with Prefer: resolution=merge-duplicates
            headers = {"Prefer": "resolution=merge-duplicates,return=representation"}
            await supabase._request(
                "POST",
                "/rest/v1/user_integrations",
                json_data={
                    "user_id": user_id,
                    "provider": data.provider,
                    "key_name": data.key_name,
                    "secret_enc": encrypted,
                    "meta": data.meta,
                    "updated_at": datetime.utcnow().isoformat()
                },
                headers=headers
            )

            logger.info(f"✅ Set integration key: {data.provider}/{data.key_name} for user {user_id}")

            # Мост к адаптерам задачников (Пульс исполнения, разрыв №1):
            # MeetFlow-адаптеры читают ОДНУ строку task_integration_config с
            # JSON-конфигом, а вкладка пишет по строке на ключ — без этой
            # пересборки ключи из UI для чтения задач невидимы.
            if data.provider in ("trello", "jira", "yougile"):
                try:
                    await _sync_task_integration_config(user_id, data.provider)
                except Exception:
                    logger.warning("task_integration_config sync skipped",
                                   exc_info=True)

            return {
                "success": True,
                "provider": data.provider,
                "key_name": data.key_name,
                "message": f"Ключ {data.key_name} для {data.provider} сохранён"
            }

        except Exception as e:
            logger.error(f"Failed to set integration key: {e}")
            return {"success": False, "error": str(e)}

    @delete("/keys/{integration_id:str}", status_code=200)
    async def delete_integration_key(
        self,
        integration_id: str,
        request: Request,
        user_id: str = Parameter(query="user_id"),
    ) -> Dict[str, Any]:
        """Delete an integration key."""
        user_id = _verify_user(request, user_id)
        from backend.db.supabase_client import get_supabase_client

        try:
            supabase = get_supabase_client()

            # Delete the key (with user_id check for security)
            await supabase._request(
                "DELETE",
                "/rest/v1/user_integrations",
                params={"id": f"eq.{integration_id}", "user_id": f"eq.{user_id}"}
            )

            logger.info(f"🗑️ Deleted integration key: {integration_id} for user {user_id}")

            return {"success": True, "message": "Ключ удалён"}

        except Exception as e:
            logger.error(f"Failed to delete integration key: {e}")
            return {"success": False, "error": str(e)}

    @post("/contacts")
    async def add_contact(
        self,
        data: ContactInput,
        request: Request,
        user_id: str = Parameter(query="user_id"),
    ) -> Dict[str, Any]:
        """Add a contact for an integration (Telegram user, email, etc.)"""
        user_id = _verify_user(request, user_id)
        from backend.db.supabase_client import get_supabase_client

        try:
            supabase = get_supabase_client()

            # Store as a special key with contact_ prefix
            key_name = f"contact_{data.contact_type}_{data.contact_value.replace('@', '').replace(' ', '_')}"
            encrypted = encrypt_secret(data.contact_value)

            meta = {
                **data.meta,
                "display_name": data.display_name,
                "contact_type": data.contact_type
            }

            headers = {"Prefer": "resolution=merge-duplicates,return=representation"}
            await supabase._request(
                "POST",
                "/rest/v1/user_integrations",
                json_data={
                    "user_id": user_id,
                    "provider": data.provider,
                    "key_name": key_name,
                    "secret_enc": encrypted,
                    "meta": meta,
                    "updated_at": datetime.utcnow().isoformat()
                },
                headers=headers
            )

            logger.info(f"✅ Added contact: {data.provider}/{data.contact_type} for user {user_id}")

            return {
                "success": True,
                "provider": data.provider,
                "contact_type": data.contact_type,
                "message": "Контакт добавлен"
            }

        except Exception as e:
            logger.error(f"Failed to add contact: {e}")
            return {"success": False, "error": str(e)}

    @get("/keys/{provider:str}")
    async def get_provider_keys(
        self,
        provider: str,
        request: Request,
        user_id: str = Parameter(query="user_id"),
    ) -> Dict[str, Any]:
        """Get decrypted keys for a provider (for internal use by agents).
        ⚠️ Returns actual secrets — IDOR закрыт через _verify_user."""
        user_id = _verify_user(request, user_id)
        from backend.db.supabase_client import get_supabase_client

        try:
            supabase = get_supabase_client()

            items = await supabase._request(
                "GET",
                "/rest/v1/user_integrations",
                params={"user_id": f"eq.{user_id}", "provider": f"eq.{provider}", "select": "*"}
            ) or []

            keys = {}
            contacts = []

            for item in items:
                key_name = item["key_name"]
                decrypted = decrypt_secret(item["secret_enc"])

                if key_name.startswith("contact_"):
                    contacts.append({
                        "type": item["meta"].get("contact_type", "unknown"),
                        "value": decrypted,
                        "display_name": item["meta"].get("display_name", ""),
                        "meta": item["meta"]
                    })
                else:
                    keys[key_name] = decrypted

            return {
                "provider": provider,
                "keys": keys,
                "contacts": contacts,
                "configured": bool(keys)
            }

        except Exception as e:
            logger.error(f"Failed to get provider keys: {e}")
            return {"provider": provider, "keys": {}, "contacts": [], "configured": False, "error": str(e)}


# ============ Helper function for agents ============

# Вкладка «Интеграции» → конфиг адаптеров задачников (task_adapters.py).
# Имена ключей в UI и в адаптерах различаются — маппинг здесь.
_TRACKER_CONFIG_FIELDS = {
    "trello": {"api_key": "api_key", "token": "token",
               "default_board_id": "board_id"},
    "jira": {"domain": "domain", "email": "email", "api_token": "api_token",
             "default_project": "project_key"},
    "yougile": {"api_key": "api_key", "company_id": "company_id",
                "default_board_id": "board_id"},
}


async def _sync_task_integration_config(user_id: str, provider: str) -> None:
    """Пересобрать строку task_integration_config из per-key ключей вкладки.

    Именно её ждут MeetFlow-адаптеры (automation_tools_async._get_task_adapter):
    без неё чтение/запись задачников из мозга молча отвечает «not configured»,
    хотя ключи в UI введены."""
    mapping = _TRACKER_CONFIG_FIELDS.get(provider)
    if not mapping:
        return
    keys = await get_user_integration_keys(user_id, provider)
    config = {dst: keys[src] for src, dst in mapping.items()
              if str(keys.get(src) or "").strip()}
    from backend.db.supabase_client import get_supabase_client
    supabase = get_supabase_client()
    if not config:
        # ключи стёрли — убираем и конфиг, чтобы адаптер честно сказал
        # «не настроено», а не работал на мёртвых кредах
        await supabase._request(
            "DELETE", "/rest/v1/user_integrations",
            params={"user_id": f"eq.{user_id}", "provider": f"eq.{provider}",
                    "key_name": "eq.task_integration_config"})
        return
    await supabase._request(
        "POST", "/rest/v1/user_integrations",
        json_data={
            "user_id": user_id,
            "provider": provider,
            "key_name": "task_integration_config",
            "secret_enc": encrypt_secret(json.dumps(config, ensure_ascii=False)),
            "meta": config,
            "updated_at": datetime.utcnow().isoformat(),
        },
        headers={"Prefer": "resolution=merge-duplicates,return=representation"})
    logger.info("🔗 task_integration_config rebuilt: %s (%d полей)",
                provider, len(config))


async def get_user_integration_keys(user_id: str, provider: str) -> Dict[str, str]:
    """
    Get integration keys for a user and provider.
    Used by agents to get API keys.

    Returns:
        Dict with key_name -> decrypted_value
    """
    from backend.db.supabase_client import get_supabase_client

    try:
        supabase = get_supabase_client()
        items = await supabase._request(
            "GET",
            "/rest/v1/user_integrations",
            params={"user_id": f"eq.{user_id}", "provider": f"eq.{provider}", "select": "key_name,secret_enc"}
        ) or []

        keys = {}
        for item in items:
            if not item["key_name"].startswith("contact_"):
                keys[item["key_name"]] = decrypt_secret(item["secret_enc"])

        return keys

    except Exception as e:
        logger.error(f"Failed to get integration keys for {provider}: {e}")
        return {}


async def get_user_contacts(user_id: str, provider: str) -> List[Dict[str, Any]]:
    """
    Get contacts for a user and provider (Telegram users, emails, etc.)

    Returns:
        List of contacts with type, value, display_name
    """
    from backend.db.supabase_client import get_supabase_client

    try:
        supabase = get_supabase_client()
        items = await supabase._request(
            "GET",
            "/rest/v1/user_integrations",
            params={
                "user_id": f"eq.{user_id}",
                "provider": f"eq.{provider}",
                "key_name": "like.contact_*",
                "select": "key_name,secret_enc,meta"
            }
        ) or []

        contacts = []
        for item in items:
            contacts.append({
                "type": item["meta"].get("contact_type", "unknown"),
                "value": decrypt_secret(item["secret_enc"]),
                "display_name": item["meta"].get("display_name", ""),
                "meta": item["meta"]
            })

        return contacts

    except Exception as e:
        logger.error(f"Failed to get contacts for {provider}: {e}")
        return []


