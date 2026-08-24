# -*- coding: utf-8 -*-
"""Удалённый MCP-сервер Tessbrain + OAuth: discovery, DCR, PKCE, 401-challenge.

Проверяет всё, что тестируемо без claude.ai и без реального API-бэкенда
(validate-токена и /mcp tools/list — локальные). Финальное рукопожатие именно
с claude.ai здесь не воспроизводится (проверяется на деплое)."""
import base64
import hashlib
import os
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

starlette = pytest.importorskip("starlette")
from starlette.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_TOKENS_FILE", str(tmp_path / "mcp_tokens.json"))
    monkeypatch.setenv("ENABLE_MCP_OAUTH", "1")
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://tessent.example")
    import importlib
    import mcp_token_store
    importlib.reload(mcp_token_store)
    import mcp_oauth
    importlib.reload(mcp_oauth)
    import mcp_remote_server as srv
    importlib.reload(srv)
    # mock валидации вставленного токена Tessbrain (иначе ходил бы в /auth/me)
    srv._validate_tessent_token = lambda t: "user-777" if t == "good" else None
    return TestClient(srv.build_app())


def _pkce():
    v = base64.urlsafe_b64encode(os.urandom(40)).rstrip(b"=").decode()
    ch = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    return v, ch


def test_discovery(client):
    r = client.get("/.well-known/oauth-protected-resource")
    assert r.status_code == 200 and r.json()["resource"] == "https://tessent.example/mcp"
    r = client.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    assert r.json()["code_challenge_methods_supported"] == ["S256"]


def test_full_pkce_flow_and_mcp(client):
    reg = client.post("/oauth/register",
                      json={"redirect_uris": ["https://claude.ai/cb"]})
    assert reg.status_code == 201
    cid = reg.json()["client_id"]
    v, ch = _pkce()
    q = {"client_id": cid, "redirect_uri": "https://claude.ai/cb", "state": "s1",
         "code_challenge": ch, "code_challenge_method": "S256", "response_type": "code"}

    assert "Разрешить доступ" in client.get("/oauth/authorize", params=q).text

    bad = client.post("/oauth/authorize", data={**q, "tessent_token": "bad"})
    assert bad.status_code == 400

    ok = client.post("/oauth/authorize", data={**q, "tessent_token": "good"},
                     follow_redirects=False)
    assert ok.status_code == 302
    code = ok.headers["location"].split("code=")[1].split("&")[0]

    # неверный verifier → отказ
    bad_tok = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": "https://claude.ai/cb", "code_verifier": "wrong"})
    assert bad_tok.status_code == 400

    ok2 = client.post("/oauth/authorize", data={**q, "tessent_token": "good"},
                      follow_redirects=False)
    code2 = ok2.headers["location"].split("code=")[1].split("&")[0]
    tok = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code2,
        "redirect_uri": "https://claude.ai/cb", "code_verifier": v})
    assert tok.status_code == 200
    access = tok.json()["access_token"]
    assert access.startswith("tess_mcp_")

    # код одноразовый
    again = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code2,
        "redirect_uri": "https://claude.ai/cb", "code_verifier": v})
    assert again.status_code == 400

    # /mcp без токена → 401 + WWW-Authenticate
    no = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert no.status_code == 401 and "resource_metadata" in no.headers.get("WWW-Authenticate", "")

    # /mcp с токеном → список инструментов (без сети)
    yes = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                      headers={"Authorization": f"Bearer {access}"})
    assert yes.status_code == 200
    tools = (yes.json().get("result") or {}).get("tools") or []
    assert len(tools) > 0
