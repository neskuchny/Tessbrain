#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Удалённый MCP-сервер Tessbrain с OAuth — коннектор по URL для claude.ai.

Аналог MeetFlow MCP: отдельный процесс (свой pm2/systemd + nginx), НЕ трогает
middleware основного API. Отдаёт:
  • /mcp                                  — JSON-RPC MCP (те же mcp_server.TOOLS);
  • /oauth/authorize, /oauth/token, /oauth/register — OAuth (PKCE);
  • /.well-known/oauth-protected-resource, /.well-known/oauth-authorization-server.

Авторизация /mcp:
  • Bearer tess_mcp_…  → user_id из стора → инструменты исполняются от его имени
    (mcp_server: service-токен TESSENT_API_TOKEN + этот user_id);
  • Bearer <TESSENT_MCP_TOKEN> (статический env) → совместимость с Claude Code CLI
    (пользователь берётся из TESSENT_USER_ID).
Нет токена и OAuth включён → 401 + WWW-Authenticate (claude.ai запустит OAuth).

Запуск:
    pip install starlette uvicorn httpx
    export MCP_PUBLIC_URL=https://brain.example.com   # внешний адрес (для discovery)
    export TESSENT_API_URL=http://127.0.0.1:8000        # локальный API
    export TESSENT_API_TOKEN=<service-token>            # service-auth для impersonation
    export ENABLE_MCP_OAUTH=1
    python mcp_remote_server.py                          # слушает MCP_HOST:MCP_PORT (8770)

Финальное рукопожатие именно с claude.ai здесь не воспроизвести — проверяется на
деплое; всё остальное (discovery/DCR/authorize/token/PKCE/401) покрыто smoke-тестами.
"""
from __future__ import annotations

import json
import os

import mcp_oauth
import mcp_token_store


def _public_base() -> str:
    return (os.environ.get("MCP_PUBLIC_URL", "").strip()
            or os.environ.get("MCP_PUBLIC_BASE", "").strip()
            or "http://localhost:8770").rstrip("/")


def _oauth_enabled() -> bool:
    return os.environ.get("ENABLE_MCP_OAUTH", "").strip().lower() in ("1", "on", "true", "yes")


def _static_token() -> str:
    return (os.environ.get("TESSENT_MCP_TOKEN", "").strip()
            or os.environ.get("TESSENT_API_TOKEN", "").strip())


def _validate_tessent_token(token: str):
    """Вставленный на экране согласия токен Tessbrain → user_id (через /auth/me)."""
    import httpx
    base = os.environ.get("TESSENT_API_URL", "http://localhost:8000").rstrip("/")
    try:
        r = httpx.get(f"{base}/api/v1/auth/me",
                      headers={"Authorization": f"Bearer {token}"}, timeout=15.0)
        if r.status_code == 200:
            return ((r.json() or {}).get("user") or {}).get("id")
    except Exception:
        return None
    return None


def _resolve_mcp_user(bearer: str):
    """Bearer с /mcp → user_id. tess_mcp_ через стор; статический env-токен →
    TESSENT_USER_ID; иначе None (неавторизован)."""
    tok = (bearer or "").strip()
    if not tok:
        return None
    uid = mcp_token_store.validate(tok)
    if uid:
        return uid
    st = _static_token()
    if st and tok == st:
        return os.environ.get("TESSENT_USER_ID", "").strip() or "__service__"
    return None


def build_app():
    from starlette.applications import Starlette
    from starlette.concurrency import run_in_threadpool
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
    from starlette.routing import Route

    import mcp_server

    def _bearer(request: Request) -> str:
        a = request.headers.get("authorization", "")
        return a[7:].strip() if a.lower().startswith("bearer ") else ""

    # ── discovery ──────────────────────────────────────────────────────────
    async def well_known_prm(request: Request):
        return JSONResponse(mcp_oauth.protected_resource_metadata(_public_base()))

    async def well_known_as(request: Request):
        return JSONResponse(mcp_oauth.authorization_server_metadata(_public_base()))

    # ── DCR ────────────────────────────────────────────────────────────────
    async def register(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        return JSONResponse(mcp_oauth.register_client(body or {}), status_code=201)

    # ── authorize (consent) ────────────────────────────────────────────────
    _AUTHZ_KEYS = ("client_id", "redirect_uri", "state", "code_challenge",
                   "code_challenge_method", "scope", "response_type")

    async def authorize_get(request: Request):
        params = {k: request.query_params.get(k, "") for k in _AUTHZ_KEYS}
        return HTMLResponse(mcp_oauth.consent_html(params))

    async def authorize_post(request: Request):
        form = await request.form()
        params = {k: str(form.get(k, "")) for k in _AUTHZ_KEYS}
        token = str(form.get("tessent_token", ""))
        redirect, err = mcp_oauth.authorize_submit(params, token, _validate_tessent_token)
        if err:
            return HTMLResponse(mcp_oauth.consent_html(params, error=err), status_code=400)
        return RedirectResponse(redirect, status_code=302)

    # ── token ──────────────────────────────────────────────────────────────
    async def token(request: Request):
        try:
            form = await request.form()
            body = {k: str(v) for k, v in form.items()}
        except Exception:
            body = {}
        status, js = mcp_oauth.exchange_token(body)
        return JSONResponse(js, status_code=status)

    # ── MCP (JSON-RPC) ─────────────────────────────────────────────────────
    def _challenge() -> Response:
        base = _public_base()
        hdr = (f'Bearer resource_metadata="{base}/.well-known/'
               f'oauth-protected-resource"')
        return JSONResponse({"error": "unauthorized"}, status_code=401,
                            headers={"WWW-Authenticate": hdr})

    async def mcp(request: Request):
        user_id = _resolve_mcp_user(_bearer(request))
        if user_id is None:
            if _oauth_enabled():
                return _challenge()
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            msg = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON-RPC"}, status_code=400)
        if not isinstance(msg, dict) or not msg.get("method"):
            return JSONResponse({"error": "JSON-RPC message required"}, status_code=400)

        def _run():
            mcp_server.set_request_user(None if user_id == "__service__" else user_id)
            try:
                return mcp_server.handle_message(msg)
            finally:
                mcp_server.set_request_user(None)

        resp = await run_in_threadpool(_run)
        return JSONResponse(resp if resp is not None else {})

    async def health(request: Request):
        return JSONResponse({"ok": True, "oauth": _oauth_enabled(),
                             "public": _public_base()})

    return Starlette(routes=[
        Route("/.well-known/oauth-protected-resource", well_known_prm),
        Route("/.well-known/oauth-authorization-server", well_known_as),
        Route("/oauth/register", register, methods=["POST"]),
        Route("/oauth/authorize", authorize_get, methods=["GET"]),
        Route("/oauth/authorize", authorize_post, methods=["POST"]),
        Route("/oauth/token", token, methods=["POST"]),
        Route("/mcp", mcp, methods=["POST"]),
        Route("/health", health),
    ])


def main():
    import uvicorn
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8770"))
    print(f"[tessent-mcp] listening {host}:{port} | OAuth={'on' if _oauth_enabled() else 'off'} "
          f"| public={_public_base()}")
    uvicorn.run(build_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
