"""W43: backend i18n — pluggable translator with JSON catalogs.

Дизайн:
- Locale enum: RU (default, back-compat) + EN, легко расширяется
- Translator: загружает JSON каталоги на старте, cached
- t(key, locale=None, **vars) — sprintf-style interpolation `{name}`
- current_locale contextvar — set'ится middleware'ом per-request

Lookup priority при вызове t():
1. Явный locale аргумент
2. current_locale contextvar (set'ит middleware)
3. settings.default_locale
4. RU (hard fallback)

Missing key → возвращает сам key как dev-friendly fallback +
warning в лог (заметим что не перевели). Не raises.
"""
from __future__ import annotations

import contextvars
import json
import logging
import pathlib
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class Locale(str, Enum):
    RU = "ru"
    EN = "en"

    @classmethod
    def from_string(cls, value: Optional[str]) -> Optional["Locale"]:
        if not value:
            return None
        v = value.strip().lower().split("-")[0]   # "en-US" → "en"
        for loc in cls:
            if loc.value == v:
                return loc
        return None


DEFAULT_LOCALE = Locale.RU
SUPPORTED_LOCALES: tuple[Locale, ...] = (Locale.RU, Locale.EN)


# Per-request locale, set'ится middleware'ом.
_current_locale: contextvars.ContextVar[Optional[Locale]] = (
    contextvars.ContextVar("current_locale", default=None)
)


def set_current_locale(locale: Optional[Locale]) -> contextvars.Token:
    return _current_locale.set(locale)


def reset_current_locale(token: contextvars.Token) -> None:
    _current_locale.reset(token)


def get_current_locale() -> Locale:
    """Returns current request locale, или default если не set."""
    loc = _current_locale.get()
    if loc is not None:
        return loc
    try:
        from backend.config import get_settings
        s = get_settings()
        explicit = Locale.from_string(getattr(s, "default_locale", None))
        if explicit is not None:
            return explicit
    except Exception:
        pass
    return DEFAULT_LOCALE


_LOCALES_DIR = pathlib.Path(__file__).resolve().parent / "locales"


class Translator:
    """Loads JSON catalogs, lookups keys, interpolates vars."""

    def __init__(self, locales_dir: Optional[pathlib.Path] = None) -> None:
        self._dir = locales_dir or _LOCALES_DIR
        self._catalogs: dict[Locale, dict[str, Any]] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        for loc in SUPPORTED_LOCALES:
            path = self._dir / f"{loc.value}.json"
            if not path.exists():
                logger.warning("i18n: catalog %s not found, locale will fallback", path)
                self._catalogs[loc] = {}
                continue
            try:
                self._catalogs[loc] = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("i18n: failed to load %s: %s", path, exc)
                self._catalogs[loc] = {}
        self._loaded = True

    def _lookup_raw(self, key: str, locale: Locale) -> Optional[str]:
        """Walks dotted path like 'messenger.help.unlinked' через nested dicts."""
        self._load()
        catalog = self._catalogs.get(locale, {})
        node: Any = catalog
        for part in key.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return None
        return node if isinstance(node, str) else None

    def t(
        self,
        key: str,
        *,
        locale: Optional[Locale] = None,
        **vars: Any,
    ) -> str:
        """Translate `key` with sprintf-style `{name}` interpolation.

        Lookup:
        1. requested locale → if found, use
        2. DEFAULT_LOCALE (RU) → if found, use as fallback
        3. key itself (dev-friendly marker)
        """
        loc = locale or get_current_locale()
        raw = self._lookup_raw(key, loc)
        if raw is None and loc != DEFAULT_LOCALE:
            raw = self._lookup_raw(key, DEFAULT_LOCALE)
            if raw is not None:
                logger.debug("i18n: missing key %r in %s, fell back to %s", key, loc.value, DEFAULT_LOCALE.value)
        if raw is None:
            logger.warning("i18n: missing key %r in all locales", key)
            return key
        if not vars:
            return raw
        try:
            return raw.format(**vars)
        except (KeyError, IndexError, ValueError) as exc:
            logger.warning("i18n: interpolation failed for %r: %s", key, exc)
            return raw

    def available_keys(self, locale: Locale = DEFAULT_LOCALE) -> list[str]:
        """Flatten все ключи (для тестов / sanity check)."""
        self._load()
        out: list[str] = []

        def _walk(node: Any, prefix: str) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    sub = f"{prefix}.{k}" if prefix else k
                    if isinstance(v, dict):
                        _walk(v, sub)
                    elif isinstance(v, str):
                        out.append(sub)

        _walk(self._catalogs.get(locale, {}), "")
        return sorted(out)


_default_translator: Optional[Translator] = None


def get_translator() -> Translator:
    global _default_translator
    if _default_translator is None:
        _default_translator = Translator()
    return _default_translator


def t(key: str, *, locale: Optional[Locale] = None, **vars: Any) -> str:
    """Top-level shortcut — caller'ам не надо инстанцировать Translator."""
    return get_translator().t(key, locale=locale, **vars)


def reset_translator() -> None:
    """Test helper — сбросить cached translator."""
    global _default_translator
    _default_translator = None


__all__ = [
    "DEFAULT_LOCALE",
    "SUPPORTED_LOCALES",
    "Locale",
    "Translator",
    "get_current_locale",
    "get_translator",
    "reset_current_locale",
    "reset_translator",
    "set_current_locale",
    "t",
]
