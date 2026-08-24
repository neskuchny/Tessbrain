"""Unit-тесты для core.i18n + middleware (W43)."""
from __future__ import annotations

import asyncio

import pytest
from backend.core.i18n import (
    DEFAULT_LOCALE,
    SUPPORTED_LOCALES,
    Locale,
    Translator,
    get_current_locale,
    get_translator,
    reset_current_locale,
    reset_translator,
    set_current_locale,
    t,
)

# === Locale enum =======================================================

def test_locale_from_string_basic() -> None:
    assert Locale.from_string("ru") == Locale.RU
    assert Locale.from_string("en") == Locale.EN
    assert Locale.from_string("EN") == Locale.EN


def test_locale_from_string_with_region() -> None:
    assert Locale.from_string("en-US") == Locale.EN
    assert Locale.from_string("ru-RU") == Locale.RU


def test_locale_from_string_unknown() -> None:
    assert Locale.from_string("de") is None
    assert Locale.from_string("") is None
    assert Locale.from_string(None) is None


def test_supported_locales_contains_both() -> None:
    assert Locale.RU in SUPPORTED_LOCALES
    assert Locale.EN in SUPPORTED_LOCALES


def test_default_locale_is_ru() -> None:
    assert DEFAULT_LOCALE == Locale.RU


# === Translator.t — basic lookup ======================================

def test_t_returns_ru_by_default() -> None:
    out = t("messenger.invalid_token")
    assert "невалид" in out.lower()


def test_t_with_explicit_en() -> None:
    out = t("messenger.invalid_token", locale=Locale.EN)
    assert "invalid" in out.lower()


def test_t_missing_key_returns_key() -> None:
    out = t("does.not.exist")
    assert out == "does.not.exist"


def test_t_interpolation_basic() -> None:
    out = t("messenger.linked_success", user_id="abc12345")
    assert "abc12345" in out


def test_t_interpolation_works_for_en() -> None:
    out = t("messenger.linked_success", locale=Locale.EN, user_id="xyz")
    assert "xyz" in out


def test_t_missing_interpolation_var_does_not_raise() -> None:
    """Если в шаблоне есть {var} а его не передали — не падаем, отдаём raw."""
    out = t("messenger.linked_success")  # user_id missing
    # Не raise — возвращает строку (raw template).
    assert isinstance(out, str)


# === Catalog completeness =============================================

def test_all_ru_keys_have_en_translation() -> None:
    """Каждый ключ из ru.json должен быть в en.json (no missing translations)."""
    translator = Translator()
    ru_keys = set(translator.available_keys(Locale.RU))
    en_keys = set(translator.available_keys(Locale.EN))
    missing = ru_keys - en_keys
    assert not missing, f"Missing EN translations for: {missing}"


def test_messenger_keys_exist() -> None:
    translator = Translator()
    ru_keys = translator.available_keys(Locale.RU)
    expected = [
        "messenger.help_unlinked",
        "messenger.linked_success",
        "messenger.invalid_token",
        "messenger.chat_unavailable",
        "messenger.chat_failed",
        "messenger.system_prompt",
    ]
    for k in expected:
        assert k in ru_keys, f"missing key: {k}"


def test_tz_keys_exist() -> None:
    translator = Translator()
    ru_keys = translator.available_keys(Locale.RU)
    assert "tz.template_block_header" in ru_keys
    assert "tz.template_structure_intro" in ru_keys


# === Fallback chain ===================================================

def test_missing_en_falls_back_to_ru() -> None:
    """Если ключа нет в EN catalogue, должен подтянуться RU fallback."""
    import json
    import pathlib
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = pathlib.Path(tmp)
        (tmp_path / "ru.json").write_text(
            json.dumps({"shared": {"only_ru": "Только RU"}}),
            encoding="utf-8",
        )
        (tmp_path / "en.json").write_text(json.dumps({}), encoding="utf-8")

        tr = Translator(locales_dir=tmp_path)
        out = tr.t("shared.only_ru", locale=Locale.EN)
        assert out == "Только RU"   # fallback to RU


# === contextvar ========================================================

def test_set_current_locale_affects_t() -> None:
    token = set_current_locale(Locale.EN)
    try:
        out = t("messenger.invalid_token")
        assert "invalid" in out.lower()
    finally:
        reset_current_locale(token)


def test_get_current_locale_default_when_unset() -> None:
    # default — RU (без settings overrides в test environment)
    assert get_current_locale() in SUPPORTED_LOCALES


def test_explicit_locale_overrides_contextvar() -> None:
    token = set_current_locale(Locale.EN)
    try:
        out = t("messenger.invalid_token", locale=Locale.RU)
        assert "невалид" in out.lower()
    finally:
        reset_current_locale(token)


# === Translator singleton ============================================

def test_get_translator_returns_singleton() -> None:
    a = get_translator()
    b = get_translator()
    assert a is b


def test_reset_translator() -> None:
    a = get_translator()
    reset_translator()
    b = get_translator()
    assert a is not b


# === Middleware: locale resolution =====================================


def test_middleware_query_string_wins() -> None:
    from backend.api.middleware.locale_resolver import resolve_locale_from_scope
    scope = {
        "type": "http",
        "query_string": b"lang=en",
        "headers": [(b"accept-language", b"ru")],
    }
    assert resolve_locale_from_scope(scope) == Locale.EN


def test_middleware_accept_language_when_no_query() -> None:
    from backend.api.middleware.locale_resolver import resolve_locale_from_scope
    scope = {
        "type": "http",
        "query_string": b"",
        "headers": [(b"accept-language", b"en-US,en;q=0.9,ru;q=0.8")],
    }
    assert resolve_locale_from_scope(scope) == Locale.EN


def test_middleware_accept_language_falls_back_to_supported() -> None:
    """`de` не supported → берём след supported из list."""
    from backend.api.middleware.locale_resolver import resolve_locale_from_scope
    scope = {
        "type": "http",
        "query_string": b"",
        "headers": [(b"accept-language", b"de,fr,ru,en")],
    }
    assert resolve_locale_from_scope(scope) == Locale.RU


def test_middleware_unknown_lang_param_falls_through() -> None:
    """?lang=de → не valid, идём дальше по chain."""
    from backend.api.middleware.locale_resolver import resolve_locale_from_scope
    scope = {
        "type": "http",
        "query_string": b"lang=de",
        "headers": [(b"accept-language", b"en")],
    }
    assert resolve_locale_from_scope(scope) == Locale.EN


def test_middleware_default_when_nothing_set() -> None:
    from backend.api.middleware.locale_resolver import resolve_locale_from_scope
    scope = {"type": "http", "query_string": b"", "headers": []}
    assert resolve_locale_from_scope(scope) == DEFAULT_LOCALE


# === ASGI integration =================================================


def test_middleware_sets_and_resets_contextvar() -> None:
    """Middleware устанавливает contextvar на время request'а и сбрасывает."""
    from backend.api.middleware.locale_resolver import LocaleResolverMiddleware

    captured: dict = {}

    async def _inner_app(scope, receive, send):
        captured["locale_during_request"] = get_current_locale()

    middleware = LocaleResolverMiddleware(_inner_app)
    scope = {
        "type": "http",
        "query_string": b"lang=en",
        "headers": [],
    }

    async def _noop_receive():
        return {}

    async def _noop_send(_):
        return None

    asyncio.run(middleware(scope, _noop_receive, _noop_send))
    assert captured["locale_during_request"] == Locale.EN
    # После middleware — contextvar reset'нут, get_current_locale() возвращает default.
    assert get_current_locale() == DEFAULT_LOCALE


def test_middleware_passes_through_non_http() -> None:
    """WebSocket / lifespan не должны крашить middleware."""
    from backend.api.middleware.locale_resolver import LocaleResolverMiddleware

    called: dict = {}

    async def _inner_app(scope, receive, send):
        called["inner"] = True

    mw = LocaleResolverMiddleware(_inner_app)
    asyncio.run(mw({"type": "websocket"}, None, None))
    assert called["inner"] is True


# === Refactored modules use t() =======================================


def test_messenger_router_uses_t() -> None:
    """Smoke test: модуль импортирует i18n и ключи на месте."""
    from backend.core.messengers.router import _ERR_INVALID_TOKEN_KEY, _HELP_TEMPLATE_KEY
    # Проверка что catalog знает эти ключи.
    out_help = t(_HELP_TEMPLATE_KEY)
    out_err = t(_ERR_INVALID_TOKEN_KEY)
    assert "Tessbrain" in out_help or "tessent" in out_help.lower()
    assert "невалид" in out_err.lower() or "expired" in out_err.lower()
