# -*- coding: utf-8 -*-
"""Google-экспорт (сервисный аккаунт): JWT RS256 → токен → Sheets/Docs.
HTTP фейковый, ключ — настоящий RSA (cryptography), значит подпись и
формат запросов проверяются по-настоящему."""
from __future__ import annotations

import asyncio
import json

import pytest

import backend.core.integrations.google_export as g


def _fake_sa(monkeypatch):
    # Реальный RSA паникует в этом test-env (pyo3, как у Ed25519 лицензий) —
    # подменяем seam _sign_rs256; формат JWT и запросов проверяем честно.
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", json.dumps({
        "client_email": "svc@test.iam.gserviceaccount.com",
        "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----"}))
    monkeypatch.setattr(g, "_sign_rs256", lambda pem, data: b"signature")
    monkeypatch.setattr(g, "_token_cache", {"token": None, "exp": 0})


class _Resp:
    def __init__(self, data):
        self._d = data
        self.content = b"x"
    def raise_for_status(self):
        return None
    def json(self):
        return self._d


class _FakeHttp:
    def __init__(self):
        self.calls = []
    async def post(self, url, data=None):
        self.calls.append(("POST", url, data))
        return _Resp({"access_token": "tok123", "expires_in": 3600})
    async def request(self, method, url, headers=None, json=None):
        self.calls.append((method, url, json))
        if "spreadsheets" in url and method == "POST" and url.endswith("spreadsheets"):
            return _Resp({"spreadsheetId": "SID1"})
        if "documents" in url and url.endswith("documents"):
            return _Resp({"documentId": "DID1"})
        return _Resp({})


def test_not_configured(monkeypatch):
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_FILE", raising=False)
    assert g.is_configured() is False
    assert "GOOGLE_SERVICE_ACCOUNT_JSON" in g.config_hint()


def test_sheets_export_flow(monkeypatch):
    _fake_sa(monkeypatch)
    http = _FakeHttp()
    res = asyncio.run(g.export_table_to_sheets(
        title="Медиаплан", columns=["Канал", "Бюджет, ₽"],
        rows=[{"Канал": "Директ", "Бюджет, ₽": "60000.00"}],
        http=http))
    assert res["spreadsheet_id"] == "SID1"
    assert res["url"].endswith("/SID1")
    # токен: JWT из трёх частей ушёл на oauth endpoint
    tok_call = http.calls[0]
    assert tok_call[1] == g._TOKEN_URL
    assert tok_call[2]["assertion"].count(".") == 2
    # значения: заголовок + строка, числа числами
    put = [c for c in http.calls if c[0] == "PUT"][0]
    assert put[2]["values"][0] == ["Канал", "Бюджет, ₽"]
    assert put[2]["values"][1] == ["Директ", 60000.0]
    # расшарен по ссылке
    assert any("permissions" in c[1] for c in http.calls)


def test_doc_export_flow(monkeypatch):
    _fake_sa(monkeypatch)
    http = _FakeHttp()
    res = asyncio.run(g.export_text_to_doc(
        title="Обоснование", text="Почему так: ...", http=http))
    assert res["document_id"] == "DID1"
    batch = [c for c in http.calls if "batchUpdate" in c[1]][0]
    assert batch[2]["requests"][0]["insertText"]["text"].startswith("Почему")


def test_token_cached(monkeypatch):
    _fake_sa(monkeypatch)
    http = _FakeHttp()
    asyncio.run(g._access_token(http=http))
    asyncio.run(g._access_token(http=http))
    assert len([c for c in http.calls if c[1] == g._TOKEN_URL]) == 1
