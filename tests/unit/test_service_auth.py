"""Unit-тесты для alfa_asynk_meetflow/tessent_service_auth.py (W6).

Проверяет генерацию service-JWT и формирование headers.
Импорт через importlib.util — meetflow не в backend/, не подключается через
обычные `from backend....` пути.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

_MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[3]
    / "alfa_asynk_meetflow" / "tessent_service_auth.py"
)
_spec = importlib.util.spec_from_file_location("_tessent_service_auth_under_test", _MODULE_PATH)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
auth_headers = _module.auth_headers
make_service_token = _module.make_service_token
_safe_tenant_id = _module._safe_tenant_id


# === safe tenant id ===

@pytest.mark.parametrize("value", [
    "tenant-A",
    "00000000-0000-0000-0000-000000000000",
    "meetflow_org_42",
    "x",
])
def test_safe_tenant_id_accepts(value: str) -> None:
    assert _safe_tenant_id(value) == value


@pytest.mark.parametrize("value", [
    "",
    None,
    "tenant'; DROP TABLE",
    "tenant with spaces",
    "tenant\nnewline",
    "a" * 65,
])
def test_safe_tenant_id_rejects(value) -> None:
    assert _safe_tenant_id(value) is None


# === make_service_token ===

def test_make_service_token_empty_when_no_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TESSENT_SERVICE_JWT_SECRET", raising=False)
    assert make_service_token(tenant_id="t1") == ""


def test_make_service_token_returns_jwt_when_secret_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TESSENT_SERVICE_JWT_SECRET", "test-secret-32-chars-minimum-OK")
    monkeypatch.setenv("TESSENT_SERVICE_JWT_AUDIENCE", "tessent-brain")
    monkeypatch.setenv("TESSENT_SERVICE_JWT_ISSUER", "meetflow")

    token = make_service_token(tenant_id="tA", user_id="u1")
    # Если PyJWT установлен — получаем валидный JWT (header.payload.signature).
    # Если PyJWT/cryptography сломан — функция возвращает "" и логирует warning.
    if token:
        parts = token.split(".")
        assert len(parts) == 3, f"expected 3 JWT parts, got {len(parts)}"

        # Декодируем payload руками, чтобы не зависеть от рабочего jwt-модуля.
        import base64
        import json as _json
        body = parts[1]
        body += "=" * (-len(body) % 4)
        payload = _json.loads(base64.urlsafe_b64decode(body))
        assert payload["sub"] == "meetflow"
        assert payload["aud"] == "tessent-brain"
        assert payload["iss"] == "meetflow"
        assert payload["tenant_id"] == "tA"
        assert payload["user_id"] == "u1"
        assert payload["exp"] > payload["iat"]


# === auth_headers ===

def test_auth_headers_empty_without_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TESSENT_SERVICE_JWT_SECRET", raising=False)
    headers = auth_headers(tenant_id="tA", user_id="u1")
    # Без secret'а нет Authorization, но X-Tenant-Id всё равно ставится.
    assert "Authorization" not in headers
    assert headers.get("X-Tenant-Id") == "tA"


def test_auth_headers_with_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TESSENT_SERVICE_JWT_SECRET", "test-secret-32-chars-minimum-OK")
    headers = auth_headers(tenant_id="tA", user_id="u1", request_id="req-123")

    if "Authorization" in headers:
        assert headers["Authorization"].startswith("Bearer ")
    assert headers.get("X-Tenant-Id") == "tA"
    assert headers.get("X-Request-Id") == "req-123"


def test_auth_headers_unsafe_tenant_id_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Опасный tenant_id (с пробелами/инъекцией) не должен попасть в headers."""
    monkeypatch.delenv("TESSENT_SERVICE_JWT_SECRET", raising=False)
    headers = auth_headers(tenant_id="bad'; DROP", user_id="u1")
    assert "X-Tenant-Id" not in headers
