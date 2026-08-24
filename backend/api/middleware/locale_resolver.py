"""ASGI middleware: resolve request locale → set i18n contextvar (W43).

Источники (по приоритету):
1. `?lang=en` query param (явный override от пользователя)
2. `Accept-Language` header (стандартный browser flow)
3. JWT-claim `preferred_language` (per-user setting сохранённый в profile)
4. settings.default_locale (per-deployment fallback)
5. DEFAULT_LOCALE (RU hard fallback)

Если ничего не подходит из supported — silently fallback на default.
Middleware не raise'ит — invalid locale просто игнорируется.
"""
from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import parse_qs

from backend.core.i18n import (
    DEFAULT_LOCALE,
    SUPPORTED_LOCALES,
    Locale,
    reset_current_locale,
    set_current_locale,
)

logger = logging.getLogger(__name__)


def _parse_query_lang(query_string: bytes) -> Optional[Locale]:
    if not query_string:
        return None
    try:
        qs = parse_qs(query_string.decode("ascii", errors="ignore"))
    except Exception:
        return None
    values = qs.get("lang") or qs.get("locale") or []
    if not values:
        return None
    return Locale.from_string(values[0])


def _parse_accept_language(header: Optional[str]) -> Optional[Locale]:
    """Берёт первый supported locale из Accept-Language header.

    Format: "en-US,en;q=0.9,ru;q=0.8" — мы упрощённо берём первый supported.
    """
    if not header:
        return None
    parts = [p.strip() for p in header.split(",") if p.strip()]
    for raw in parts:
        # Strip ";q=..." suffix.
        code = raw.split(";", 1)[0].strip()
        loc = Locale.from_string(code)
        if loc is not None and loc in SUPPORTED_LOCALES:
            return loc
    return None


def _parse_jwt_preferred_language(headers: dict[str, str]) -> Optional[Locale]:
    auth = headers.get("authorization", "") or headers.get("Authorization", "")
    if not auth or not auth.startswith("Bearer "):
        return None
    token = auth.split(" ", 1)[1]
    # issue #107 T-1: verify-first. Локаль из недоверенного токена в strict-
    # режиме игнорируется (claims={}), падая на дефолтную локаль — безопасно.
    try:
        from backend.core.auth.service_token import decode_claims_guarded
        claims = decode_claims_guarded(token)
    except Exception:
        return None
    raw = (
        claims.get("preferred_language")
        or claims.get("locale")
        or claims.get("lang")
    )
    if isinstance(raw, str):
        return Locale.from_string(raw)
    # Supabase nested: app_metadata.preferred_language
    app_meta = claims.get("app_metadata") or {}
    if isinstance(app_meta, dict):
        nested = app_meta.get("preferred_language") or app_meta.get("locale")
        if isinstance(nested, str):
            return Locale.from_string(nested)
    return None


def _settings_default() -> Optional[Locale]:
    try:
        from backend.config import get_settings
        s = get_settings()
        raw = getattr(s, "default_locale", None)
        return Locale.from_string(raw)
    except Exception:
        return None


def resolve_locale_from_scope(scope: dict[str, Any]) -> Locale:
    """Apply priority chain to ASGI scope, return final Locale."""
    # 1. Query
    qs = scope.get("query_string", b"") or b""
    loc = _parse_query_lang(qs)
    if loc is not None and loc in SUPPORTED_LOCALES:
        return loc

    # Headers: list of (bytes, bytes) tuples в ASGI.
    raw_headers = scope.get("headers") or []
    headers_dict: dict[str, str] = {}
    for name, value in raw_headers:
        try:
            key = name.decode("latin1").lower()
            val = value.decode("latin1")
            # Если уже есть (Cookie/Accept-* могут повторяться) — берём первый.
            if key not in headers_dict:
                headers_dict[key] = val
        except Exception:
            continue

    # 2. Accept-Language
    loc = _parse_accept_language(headers_dict.get("accept-language"))
    if loc is not None:
        return loc

    # 3. JWT preferred_language
    loc = _parse_jwt_preferred_language(headers_dict)
    if loc is not None and loc in SUPPORTED_LOCALES:
        return loc

    # 4. Settings default
    loc = _settings_default()
    if loc is not None and loc in SUPPORTED_LOCALES:
        return loc

    # 5. Hard fallback
    return DEFAULT_LOCALE


class LocaleResolverMiddleware:
    """ASGI middleware: set request locale в contextvar, очистить после."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        locale = resolve_locale_from_scope(scope)
        token = set_current_locale(locale)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_current_locale(token)


__all__ = [
    "LocaleResolverMiddleware",
    "resolve_locale_from_scope",
]
