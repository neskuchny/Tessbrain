"""Unit-тесты для core.security.encryption (W11 pgcrypto helper)."""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import pytest

_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "backend" / "core" / "security" / "encryption.py"
)
# Изолированная загрузка чтобы не тащить backend.core.* heavy deps.
_pkg = sys.modules.setdefault("_enc_isolated", types.ModuleType("_enc_isolated"))
_pkg.__path__ = []  # type: ignore[attr-defined]
_spec = importlib.util.spec_from_file_location("_enc_isolated.encryption", _PATH)
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)

FieldKeyMissingError = _module.FieldKeyMissingError
get_field_key = _module.get_field_key
encrypt_expr = _module.encrypt_expr
decrypt_expr = _module.decrypt_expr
decrypt_safe_expr = _module.decrypt_safe_expr
is_enabled = _module.is_enabled
generate_test_key = _module.generate_test_key


def test_get_field_key_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PGCRYPTO_FIELD_KEY", raising=False)
    with pytest.raises(FieldKeyMissingError) as exc:
        get_field_key()
    assert "PGCRYPTO_FIELD_KEY" in str(exc.value)


def test_get_field_key_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGCRYPTO_FIELD_KEY", "  secret-key-value  ")
    assert get_field_key() == "secret-key-value"


def test_get_field_key_treats_blank_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGCRYPTO_FIELD_KEY", "   ")
    with pytest.raises(FieldKeyMissingError):
        get_field_key()


def test_encrypt_expr_uses_default_placeholder() -> None:
    sql = encrypt_expr(":email")
    assert sql == "public.tessent_encrypt(:email, :pgc_key)"


def test_encrypt_expr_custom_key_placeholder() -> None:
    sql = encrypt_expr(":phone", key_placeholder=":kk")
    assert sql == "public.tessent_encrypt(:phone, :kk)"


def test_decrypt_expr() -> None:
    assert decrypt_expr("email_enc") == "public.tessent_decrypt(email_enc, :pgc_key)"


def test_decrypt_safe_expr() -> None:
    assert decrypt_safe_expr("email_enc") == "public.tessent_decrypt_safe(email_enc, :pgc_key)"


def test_is_enabled_true_when_key_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGCRYPTO_FIELD_KEY", "anything")
    assert is_enabled() is True


def test_is_enabled_false_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PGCRYPTO_FIELD_KEY", raising=False)
    assert is_enabled() is False


def test_generate_test_key_constant() -> None:
    """generate_test_key — детерминистичен; не для прода."""
    assert generate_test_key() == "test-key-do-not-use-in-prod"
    assert "do-not-use-in-prod" in generate_test_key()
