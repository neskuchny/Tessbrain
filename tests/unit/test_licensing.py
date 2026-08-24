# -*- coding: utf-8 -*-
"""Офлайн-лицензия Tessbrain (анти-кража, on-prem). Подпись подменяется
(_verify_signature) — реальный Ed25519 в этом test-env паникует в pyo3,
в проде работает; здесь тестируем логику payload/expiry/grace/enforce."""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

import pytest

import backend.core.licensing as lic


def _token(payload: dict) -> str:
    pb = base64.urlsafe_b64encode(
        json.dumps(payload).encode()).decode().rstrip("=")
    sig = base64.urlsafe_b64encode(b"sig").decode().rstrip("=")
    return f"{pb}.{sig}"


def _wire(monkeypatch, *, sig_ok=True):
    monkeypatch.setattr(lic, "_verify_signature", lambda p, s: sig_ok)


NOW = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
PAYLOAD = {"org": "ООО Ромашка", "deployment_id": "romashka-prod",
           "plan": "enterprise_onprem", "seats": 5000,
           "expires_at": "2027-07-01T23:59:59+00:00"}


def test_valid_license(monkeypatch):
    _wire(monkeypatch)
    info = lic.check_license(_token(PAYLOAD), now=NOW)
    assert info["valid"] is True and not info["grace"]
    assert info["org"] == "ООО Ромашка"
    assert info["deployment_id"] == "romashka-prod"   # watermark
    assert info["days_left"] > 300


def test_bad_signature_rejected(monkeypatch):
    _wire(monkeypatch, sig_ok=False)
    info = lic.check_license(_token(PAYLOAD), now=NOW)
    assert info["valid"] is False and "подпис" in info["reason"]


def test_expired_within_grace_still_valid_but_flagged(monkeypatch):
    _wire(monkeypatch)
    p = {**PAYLOAD, "expires_at": "2026-06-25T00:00:00+00:00"}  # 9 дней назад
    info = lic.check_license(_token(p), now=NOW)
    assert info["valid"] is True and info["grace"] is True


def test_expired_beyond_grace_invalid(monkeypatch):
    _wire(monkeypatch)
    p = {**PAYLOAD, "expires_at": "2026-06-01T00:00:00+00:00"}  # 33 дня назад
    info = lic.check_license(_token(p), now=NOW)
    assert info["valid"] is False and "истекла" in info["reason"]


def test_missing_and_garbage_tokens(monkeypatch):
    _wire(monkeypatch)
    assert lic.check_license("", now=NOW)["valid"] is False
    assert lic.check_license("не-токен", now=NOW)["valid"] is False


def test_enforce_off_is_noop(monkeypatch):
    monkeypatch.delenv("TESSENT_LICENSE_REQUIRED", raising=False)
    assert lic.enforce_license_on_startup() is None  # SaaS/dev — молчит


def test_enforce_on_blocks_boot_without_license(monkeypatch):
    monkeypatch.setenv("TESSENT_LICENSE_REQUIRED", "on")
    monkeypatch.delenv("TESSENT_LICENSE", raising=False)
    with pytest.raises(RuntimeError):
        lic.enforce_license_on_startup()


def test_enforce_on_passes_with_valid_license(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setenv("TESSENT_LICENSE_REQUIRED", "on")
    monkeypatch.setenv("TESSENT_LICENSE", _token(PAYLOAD))
    # check_license берёт now изнутри — payload валиден до 2027
    info = lic.enforce_license_on_startup()
    assert info and info["org"] == "ООО Ромашка"
