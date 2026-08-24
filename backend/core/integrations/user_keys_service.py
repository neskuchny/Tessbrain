# -*- coding: utf-8 -*-
"""
User Integration Keys Service

Сервис для получения ключей интеграций пользователя из Supabase.
Используется агентами MeetFlow и Mark001 для доступа к внешним сервисам.

Usage:
    from backend.core.integrations.user_keys_service import UserKeysService

    keys_service = UserKeysService()

    # Get Telegram bot token for user
    telegram_keys = await keys_service.get_keys(user_id, "telegram")
    bot_token = telegram_keys.get("bot_token")

    # Get all contacts for Telegram
    contacts = await keys_service.get_contacts(user_id, "telegram")
"""
import base64
import contextvars
import hashlib
import logging
import os
from typing import Any, Dict, List, Optional

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

# Encryption key (same as in integrations.py)
ENCRYPTION_KEY = os.getenv("INTEGRATIONS_ENCRYPTION_KEY", "")
if not ENCRYPTION_KEY:
    secret = os.getenv("SUPABASE_KEY", "default-secret-key")
    ENCRYPTION_KEY = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
else:
    ENCRYPTION_KEY = ENCRYPTION_KEY.encode()

_fernet = Fernet(ENCRYPTION_KEY)


def decrypt_secret(encrypted: str) -> str:
    """Decrypt a secret value"""
    try:
        return _fernet.decrypt(encrypted.encode()).decode()
    except Exception as e:
        logger.error(f"Failed to decrypt secret: {e}")
        return ""


# Context var for current user (can be set by API routes)
_current_user_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("_current_user_id", default=None)


def set_current_user(user_id: str):
    """Set current user for context-aware key retrieval"""
    _current_user_id.set(user_id)


def get_current_user() -> Optional[str]:
    """Get current user from context"""
    return _current_user_id.get()


class UserKeysService:
    """
    Service for retrieving user integration keys from Supabase.

    Caches keys in memory for performance.
    """

    def __init__(self):
        self._cache: Dict[str, Dict[str, Dict[str, str]]] = {}  # user_id -> provider -> keys
        self._contacts_cache: Dict[str, Dict[str, List[Dict]]] = {}  # user_id -> provider -> contacts

    async def get_keys(self, user_id: str, provider: str, force_refresh: bool = False) -> Dict[str, str]:
        """
        Get decrypted keys for a provider.

        Args:
            user_id: User ID
            provider: Provider ID (telegram, notion, etc.)
            force_refresh: Force refresh from database

        Returns:
            Dict with key_name -> decrypted_value
        """

        # Check cache
        if not force_refresh and user_id in self._cache and provider in self._cache[user_id]:
            return self._cache[user_id][provider]

        try:
            from backend.db.supabase_client import get_supabase_client
            supabase = get_supabase_client()

            result = await supabase.client.table("user_integrations").select(
                "key_name, secret_enc"
            ).eq("user_id", user_id).eq("provider", provider).execute()

            keys = {}
            for item in result.data or []:
                if not item["key_name"].startswith("contact_"):
                    keys[item["key_name"]] = decrypt_secret(item["secret_enc"])

            # Cache
            if user_id not in self._cache:
                self._cache[user_id] = {}
            self._cache[user_id][provider] = keys

            return keys

        except Exception as e:
            logger.error(f"Failed to get keys for {provider}: {e}")
            return {}

    async def get_contacts(self, user_id: str, provider: str, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        Get contacts for a provider.

        Args:
            user_id: User ID
            provider: Provider ID
            force_refresh: Force refresh from database

        Returns:
            List of contacts with type, value, display_name
        """
        if not force_refresh and user_id in self._contacts_cache and provider in self._contacts_cache[user_id]:
            return self._contacts_cache[user_id][provider]

        try:
            from backend.db.supabase_client import get_supabase_client
            supabase = get_supabase_client()

            result = await supabase.client.table("user_integrations").select(
                "key_name, secret_enc, meta"
            ).eq("user_id", user_id).eq("provider", provider).like("key_name", "contact_%").execute()

            contacts = []
            for item in result.data or []:
                contacts.append({
                    "type": item["meta"].get("contact_type", "unknown"),
                    "value": decrypt_secret(item["secret_enc"]),
                    "display_name": item["meta"].get("display_name", ""),
                    "meta": item["meta"]
                })

            # Cache
            if user_id not in self._contacts_cache:
                self._contacts_cache[user_id] = {}
            self._contacts_cache[user_id][provider] = contacts

            return contacts

        except Exception as e:
            logger.error(f"Failed to get contacts for {provider}: {e}")
            return []

    async def get_all_providers(self, user_id: str) -> Dict[str, Dict[str, str]]:
        """
        Get all configured providers for a user.

        Returns:
            Dict with provider -> keys
        """
        try:
            from backend.db.supabase_client import get_supabase_client
            supabase = get_supabase_client()

            result = await supabase.client.table("user_integrations").select(
                "provider, key_name, secret_enc"
            ).eq("user_id", user_id).execute()

            providers = {}
            for item in result.data or []:
                provider = item["provider"]
                if provider not in providers:
                    providers[provider] = {}

                if not item["key_name"].startswith("contact_"):
                    providers[provider][item["key_name"]] = decrypt_secret(item["secret_enc"])

            # Cache all
            self._cache[user_id] = providers

            return providers

        except Exception as e:
            logger.error(f"Failed to get all providers: {e}")
            return {}

    async def has_provider(self, user_id: str, provider: str) -> bool:
        """Check if user has configured a provider"""
        keys = await self.get_keys(user_id, provider)
        return bool(keys)

    def clear_cache(self, user_id: Optional[str] = None):
        """Clear cache for user or all"""
        if user_id:
            self._cache.pop(user_id, None)
            self._contacts_cache.pop(user_id, None)
        else:
            self._cache.clear()
            self._contacts_cache.clear()


# Singleton instance
_keys_service: Optional[UserKeysService] = None


def get_keys_service() -> UserKeysService:
    """Get singleton instance of UserKeysService"""
    global _keys_service
    if _keys_service is None:
        _keys_service = UserKeysService()
    return _keys_service


# ============ Convenience functions for agents ============

async def get_telegram_config(user_id: str) -> Dict[str, Any]:
    """
    Get Telegram configuration for user.

    Returns:
        {
            "bot_token": "...",
            "default_chat_id": "...",
            "contacts": [{"type": "user_id", "value": "123", "display_name": "John"}]
        }
    """
    service = get_keys_service()
    keys = await service.get_keys(user_id, "telegram")
    contacts = await service.get_contacts(user_id, "telegram")

    return {
        **keys,
        "contacts": contacts,
        "configured": bool(keys.get("bot_token"))
    }


async def get_gmail_config(user_id: str) -> Dict[str, Any]:
    """Get Gmail configuration for user"""
    service = get_keys_service()
    keys = await service.get_keys(user_id, "gmail")
    contacts = await service.get_contacts(user_id, "gmail")

    return {
        **keys,
        "contacts": contacts,
        "configured": bool(keys.get("oauth_token"))
    }


async def get_notion_config(user_id: str) -> Dict[str, Any]:
    """Get Notion configuration for user"""
    service = get_keys_service()
    keys = await service.get_keys(user_id, "notion")

    return {
        **keys,
        "configured": bool(keys.get("integration_token"))
    }


async def get_calendar_config(user_id: str) -> Dict[str, Any]:
    """Get Google Calendar configuration for user"""
    service = get_keys_service()
    keys = await service.get_keys(user_id, "google_calendar")

    return {
        **keys,
        "configured": bool(keys.get("oauth_token"))
    }


async def get_jira_config(user_id: str) -> Dict[str, Any]:
    """Get Jira configuration for user"""
    service = get_keys_service()
    keys = await service.get_keys(user_id, "jira")

    return {
        **keys,
        "configured": bool(keys.get("api_token") and keys.get("domain"))
    }


async def get_trello_config(user_id: str) -> Dict[str, Any]:
    """Get Trello configuration for user"""
    service = get_keys_service()
    keys = await service.get_keys(user_id, "trello")

    return {
        **keys,
        "configured": bool(keys.get("api_key") and keys.get("token"))
    }


async def get_yougile_config(user_id: str) -> Dict[str, Any]:
    """Get YouGile configuration for user"""
    service = get_keys_service()
    keys = await service.get_keys(user_id, "yougile")

    return {
        **keys,
        "configured": bool(keys.get("api_key"))
    }


async def get_slack_config(user_id: str) -> Dict[str, Any]:
    """Get Slack configuration for user"""
    service = get_keys_service()
    keys = await service.get_keys(user_id, "slack")
    contacts = await service.get_contacts(user_id, "slack")

    return {
        **keys,
        "contacts": contacts,
        "configured": bool(keys.get("bot_token"))
    }


async def get_ai_video_config(user_id: str, provider: str) -> Dict[str, Any]:
    """
    Get AI video generation config (runway, kling, luma, heygen)
    """
    service = get_keys_service()
    keys = await service.get_keys(user_id, provider)

    return {
        **keys,
        "configured": bool(keys.get("api_key"))
    }


async def get_elevenlabs_config(user_id: str) -> Dict[str, Any]:
    """Get ElevenLabs configuration for user"""
    service = get_keys_service()
    keys = await service.get_keys(user_id, "elevenlabs")

    return {
        **keys,
        "configured": bool(keys.get("api_key"))
    }

