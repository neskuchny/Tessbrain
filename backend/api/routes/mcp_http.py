# -*- coding: utf-8 -*-
"""Hosted MCP (Streamable HTTP) — «подключись к Tessbrain извне».

Закрывает разрыв «MCP только stdio локально»: тот же набор инструментов
(mcp_server.TOOLS, ноль дублирования) доступен по HTTP прямо из API —
без пакета `mcp`, без отдельного процесса, без запуска чего-либо у
пользователя. Подключение:

    claude mcp add --transport http tessent https://<host>/api/v1/mcp \
        -H "Authorization: Bearer <TESSENT_MCP_TOKEN>"

Codex (~/.codex/config.toml):
    [[servers]]
    type = "http"; name = "tessent"; url = "https://<host>/api/v1/mcp"

Auth (в порядке проверки):
  • Bearer tess_mcp_…        — персональный токен, выпущенный пользователем в
    интерфейсе (Интеграции → Подключение к Claude). Вызовы исполняются от его
    имени: один сервер обслуживает разных людей;
  • Bearer <TESSENT_MCP_TOKEN> — статический env-токен для одиночной/админской
    установки, пользователь берётся из TESSENT_USER_ID (как было).
Токена нет или он неизвестен → 401. Инструменты внутри ходят в этот же API
через TESSENT_API_URL (дефолт localhost) с TESSENT_API_TOKEN — обычный
auth-слой не обходится; без него эндпоинт честно отвечает 503.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

from litestar import Request, Router, post
from litestar.exceptions import HTTPException

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[3]   # tessent_brain/


def _stdio_module():
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    import mcp_server
    return mcp_server


def _expected_token() -> str:
    return (os.getenv("TESSENT_MCP_TOKEN", "").strip()
            or os.getenv("TESSENT_API_TOKEN", "").strip())


def _token_store():
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    import mcp_token_store
    return mcp_token_store


def _resolve_user(token: str) -> Optional[str]:
    """Bearer → user_id, от имени которого исполнять вызов.

    tess_mcp_… — персональный токен, выпущенный пользователем в интерфейсе
    («Интеграции → Подключение к Claude»): один сервер обслуживает разных
    людей, поэтому пользователь берётся из стора, а не из env. Статический
    env-токен остаётся для одиночной/админской установки и работает как
    раньше — от TESSENT_USER_ID. Возврат None = не авторизован."""
    tok = (token or "").strip()
    if not tok:
        return None
    try:
        uid = _token_store().validate(tok)
    except Exception:
        logger.debug("MCP token store unavailable", exc_info=True)
        uid = None
    if uid:
        return uid
    expected = _expected_token()
    if expected and tok == expected:
        return os.getenv("TESSENT_USER_ID", "").strip() or "__service__"
    return None


@post("/", status_code=200)
async def mcp_endpoint(request: Request, data: Any = None) -> Any:
    """JSON-RPC сообщение MCP (initialize / tools/list / tools/call)."""
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    user_id = _resolve_user(token)
    if not user_id:
        if not token:
            raise HTTPException(
                status_code=401,
                detail="нужен токен подключения: выпустите его в интерфейсе "
                       "(Интеграции → Подключение к Claude)")
        raise HTTPException(status_code=401, detail="bad MCP token")
    if not os.getenv("TESSENT_API_TOKEN", "").strip():
        # Инструменты ходят обратно в API сервисным токеном и исполняют от
        # имени user_id. Без него персональный токен молча работал бы «ни от
        # кого» — честнее сказать администратору, чего не хватает.
        raise HTTPException(
            status_code=503,
            detail="MCP не настроен на сервере: нет TESSENT_API_TOKEN")

    msg = data if isinstance(data, dict) else {}
    if not msg.get("method"):
        raise HTTPException(status_code=400, detail="JSON-RPC message required")
    mod = _stdio_module()

    def _run() -> Optional[dict]:
        # contextvar ставим ВНУТРИ рабочего потока: asyncio.to_thread копирует
        # контекст на входе, установка снаружи до to_thread тоже сработала бы,
        # но здесь reset гарантированно парный и поток не утекает чужим user_id.
        tok_ctx = mod.set_request_user(None if user_id == "__service__" else user_id)
        try:
            return mod.handle_message(msg)
        finally:
            mod._req_user.reset(tok_ctx)

    # tools/call внутри делает блокирующие HTTP-вызовы к API → в thread,
    # чтобы не держать event loop.
    resp: Optional[dict] = await asyncio.to_thread(_run)
    # Уведомления без ответа → 202-подобный пустой ok (Streamable HTTP
    # разрешает пустое тело; отдаём {} для простоты клиентов).
    return resp if resp is not None else {}


mcp_http_router = Router(path="/mcp", route_handlers=[mcp_endpoint],
                         tags=["MCP"])
