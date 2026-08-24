# -*- coding: utf-8 -*-
"""Минимальный OAuth-слой для удалённого MCP-коннектора Tessbrain (claude.ai).

Ровно то, что требует claude.ai для коннектора по URL:
  • discovery: RFC 9728 (protected-resource) + RFC 8414 (authorization-server);
  • Dynamic Client Registration (RFC 7591);
  • authorization_code + PKCE (S256).

Трюк (как в MeetFlow): на экране согласия пользователь вставляет свой токен
Tessbrain (Supabase access token). Мы валидируем его через API (/auth/me), получаем
user_id и ВЫПУСКАЕМ долгоживущий tess_mcp_-токен (mcp_token_store) — он и
становится OAuth access_token. Поэтому не нужны ни refresh-токены, ни хранение
секретов клиента: access_token самодостаточен (валидируется по стору).

Модуль фреймворк-независим: чистые функции + in-memory стор клиентов/кодов.
HTTP-обвязку даёт mcp_remote_server.py (Starlette). Тестируемо без сети.
"""
from __future__ import annotations

import base64
import hashlib
import html as _html
import secrets
import time
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlencode

import mcp_token_store

# In-memory (один процесс remote-сервера): динамически зарегистрированные клиенты
# и одноразовые authorization codes. Коды короткоживущие (600с), клиенты — на
# время жизни процесса (claude.ai повторно регистрируется при необходимости).
_CLIENTS: Dict[str, Dict[str, Any]] = {}
_CODES: Dict[str, Dict[str, Any]] = {}
_CODE_TTL = 600


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def protected_resource_metadata(public_base: str, mcp_path: str = "/mcp") -> Dict[str, Any]:
    """RFC 9728: где взять authorization server для этого ресурса."""
    base = public_base.rstrip("/")
    return {
        "resource": f"{base}{mcp_path}",
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
    }


def authorization_server_metadata(public_base: str) -> Dict[str, Any]:
    """RFC 8414: эндпоинты authorize/token/register + поддержка PKCE."""
    base = public_base.rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["mcp"],
    }


def register_client(body: Dict[str, Any]) -> Dict[str, Any]:
    """Dynamic Client Registration (RFC 7591). Публичный клиент (без секрета,
    PKCE обязателен). Возврат — метаданные клиента с client_id."""
    client_id = "mcpc_" + secrets.token_hex(12)
    redirect_uris = body.get("redirect_uris") or []
    rec = {
        "client_id": client_id,
        "redirect_uris": [str(u) for u in redirect_uris if u],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "client_name": str(body.get("client_name") or "claude.ai"),
        "created_at": int(time.time()),
    }
    _CLIENTS[client_id] = rec
    return rec


def _redirect_allowed(client_id: str, redirect_uri: str) -> bool:
    rec = _CLIENTS.get(client_id)
    if not rec:
        # Клиент неизвестен (процесс перезапущен) — допускаем https/claude redirect,
        # т.к. код всё равно связан PKCE и одноразовый. Строже — при желании.
        return redirect_uri.startswith("https://")
    uris = rec.get("redirect_uris") or []
    return (not uris) or (redirect_uri in uris)


def consent_html(params: Dict[str, str], *, error: str = "") -> str:
    """HTML экрана согласия: пользователь вставляет свой токен Tessbrain."""
    hidden = "".join(
        f'<input type="hidden" name="{_html.escape(k)}" value="{_html.escape(v)}">'
        for k, v in params.items() if v)
    err = (f'<p style="color:#c0392b">{_html.escape(error)}</p>' if error else "")
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tessbrain — доступ для Claude</title>
<style>body{{font:16px/1.5 -apple-system,Segoe UI,Roboto,Arial,sans-serif;
background:#0f1621;color:#e8eef7;display:flex;min-height:100vh;align-items:center;
justify-content:center;margin:0}}.card{{background:#16202e;border:1px solid #23324a;
border-radius:14px;padding:28px;max-width:440px;width:92%}}h1{{font-size:19px;margin:0 0 6px}}
p{{color:#9fb0c6;font-size:14px}}input[type=password]{{width:100%;box-sizing:border-box;
padding:11px;border-radius:9px;border:1px solid #2b3c58;background:#0f1621;color:#e8eef7;
font-size:14px;margin:10px 0}}button{{width:100%;padding:12px;border:0;border-radius:9px;
background:#2f81f7;color:#fff;font-size:15px;font-weight:600;cursor:pointer}}
code{{color:#8fd0ff}}</style></head><body>
<form class="card" method="POST" action="/oauth/authorize">
<h1>🧠 Tessbrain → Claude</h1>
<p>Claude запрашивает доступ к вашему мозгу компании. Вставьте ваш токен Tessbrain
(из Аккаунта). Он останется только на вашем сервере.</p>
{err}
{hidden}
<input type="password" name="tessent_token" placeholder="Токен Tessbrain (Bearer)"
 autocomplete="off" required>
<button type="submit">Разрешить доступ</button>
<p style="margin-top:14px;font-size:12px">Доступ можно отозвать в любой момент,
удалив токен на сервере.</p>
</form></body></html>"""


def authorize_submit(params: Dict[str, str], tessent_token: str,
                     validate_user: Callable[[str], Optional[str]]
                     ) -> Tuple[Optional[str], Optional[str]]:
    """Обработать согласие: валидировать вставленный токен → user_id → выпустить
    tess_mcp_ + одноразовый code. Возврат (redirect_url, None) при успехе или
    (None, human_error) при ошибке. validate_user(token)->user_id|None (через API)."""
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    state = params.get("state", "")
    code_challenge = params.get("code_challenge", "")
    method = (params.get("code_challenge_method") or "S256")
    if not redirect_uri:
        return None, "нет redirect_uri"
    if method != "S256" or not code_challenge:
        return None, "нужен PKCE S256"
    if not _redirect_allowed(client_id, redirect_uri):
        return None, "redirect_uri не разрешён"
    user_id = None
    try:
        user_id = validate_user((tessent_token or "").strip())
    except Exception:
        user_id = None
    if not user_id:
        return None, "неверный токен Tessbrain — проверьте и вставьте снова"
    mcp_token = mcp_token_store.mint(user_id, label=f"claude/{client_id[:12]}")
    code = "mcpauth_" + secrets.token_hex(20)
    _CODES[code] = {
        "client_id": client_id, "redirect_uri": redirect_uri,
        "code_challenge": code_challenge, "mcp_token": mcp_token,
        "user_id": user_id, "exp": time.time() + _CODE_TTL, "used": False,
    }
    q = {"code": code}
    if state:
        q["state"] = state
    sep = "&" if ("?" in redirect_uri) else "?"
    return f"{redirect_uri}{sep}{urlencode(q)}", None


def _verify_pkce(verifier: str, challenge: str) -> bool:
    if not verifier or not challenge:
        return False
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return secrets.compare_digest(_b64url(digest), challenge)


def exchange_token(body: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    """POST /oauth/token: authorization_code + PKCE → access_token (= tess_mcp_).
    Возврат (http_status, json)."""
    if body.get("grant_type") != "authorization_code":
        return 400, {"error": "unsupported_grant_type"}
    code = body.get("code", "")
    rec = _CODES.get(code)
    if not rec:
        return 400, {"error": "invalid_grant", "error_description": "unknown code"}
    if rec.get("used") or time.time() > rec.get("exp", 0):
        _CODES.pop(code, None)
        return 400, {"error": "invalid_grant", "error_description": "code expired/used"}
    if body.get("redirect_uri", "") != rec["redirect_uri"]:
        return 400, {"error": "invalid_grant", "error_description": "redirect_uri mismatch"}
    if not _verify_pkce(body.get("code_verifier", ""), rec["code_challenge"]):
        return 400, {"error": "invalid_grant", "error_description": "PKCE verify failed"}
    rec["used"] = True
    _CODES.pop(code, None)
    return 200, {
        "access_token": rec["mcp_token"],
        "token_type": "Bearer",
        "scope": "mcp",
    }


__all__ = [
    "protected_resource_metadata", "authorization_server_metadata",
    "register_client", "consent_html", "authorize_submit", "exchange_token",
]
