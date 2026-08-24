# -*- coding: utf-8 -*-
"""
TESSENT BRAIN - Memory Module
=============================

Модуль памяти и персонализации:
- User Profiles: персонализация ответов
- Semantic Cache: кеширование похожих запросов
"""

from .semantic_cache import CacheEntry, SemanticCache, get_semantic_cache
from .user_profiles import (
    CommunicationStyle,
    DetailLevel,
    QueryPattern,
    UserProfile,
    UserProfileService,
    get_user_profile_service,
)

__all__ = [
    "CacheEntry",
    "CommunicationStyle",
    "DetailLevel",
    "QueryPattern",
    # Semantic Cache
    "SemanticCache",
    # User Profiles
    "UserProfile",
    "UserProfileService",
    "get_semantic_cache",
    "get_user_profile_service"
]


