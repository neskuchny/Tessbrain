# -*- coding: utf-8 -*-
"""Токены подключения (MCP) — выпуск и отзыв прямо из интерфейса.

Зачем: раньше единственным способом получить токен была команда в консоли
сервера (`python -m mcp_token_store mint <user_id>`) — то есть подключить
Tessbrain к Claude/Cursor мог только администратор, да ещё и зная чужой UUID.
Здесь тот же стор, но за обычной авторизацией пользователя: человек нажимает
кнопку в «Интеграциях» и получает свой токен.

БЕЗОПАСНОСТЬ. Токен даёт постоянный доступ к данным аккаунта, поэтому выпуск
и отзыв требуют ПРОВЕРЕННОГО токена (user_jwt/service_jwt). ?user_id= без
подписи здесь не принимается — иначе кто угодно выпустил бы себе ключ к чужой
компании. Секрет показывается ОДИН раз, при выпуске: в сторе он лежит целиком,
но наружу список отдаёт только префикс.

Endpoints:
- GET    /mcp/tokens              — список своих токенов (префиксы, без секрета)
- POST   /mcp/tokens              — выпустить новый (секрет в ответе один раз)
- DELETE /mcp/tokens/{prefix}     — отозвать по префиксу из списка
- GET    /mcp/connect-info        — как подключить: URL и готовые команды
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from litestar import Request, Router, delete, get, post
from litestar.exceptions import HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[3]   # tessent_brain/


def _store():
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    import mcp_token_store
    return mcp_token_store


def _verified_user(request: Request) -> str:
    """Только подтверждённая подписью личность: выпуск ключа доступа — не то
    место, где можно доверять ?user_id= из запроса."""
    from backend.core.auth.service_token import trusted_user_id
    try:
        uid, source = trusted_user_id(request.headers, "")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    if source not in ("user_jwt", "service_jwt") or not uid:
        raise HTTPException(
            status_code=401,
            detail="нужен вход в аккаунт: токены подключения выпускаются "
                   "только владельцу данных")
    return uid


class MintTokenRequest(BaseModel):
    label: Optional[str] = None


@get("/tokens")
async def list_tokens(request: Request) -> Dict[str, Any]:
    """Свои токены — только префиксы, секрет не возвращается никогда."""
    uid = _verified_user(request)
    rows: List[Dict[str, Any]] = _store().list_for_user(uid)
    return {"tokens": rows, "count": len(rows)}


@post("/tokens")
async def mint_token(request: Request, data: MintTokenRequest) -> Dict[str, Any]:
    """Выпустить токен. `token` в ответе — единственный показ секрета."""
    uid = _verified_user(request)
    label = (data.label or "").strip()[:60] or "Claude / Cursor"
    st = _store()
    existing = st.list_for_user(uid)
    if len(existing) >= 20:
        raise HTTPException(
            status_code=409,
            detail="у аккаунта уже 20 токенов — отзовите неиспользуемые")
    token = st.mint(uid, label=label)
    logger.info("MCP token issued for user=%s label=%s", uid, label)
    return {"status": "success", "token": token, "label": label,
            "prefix": token[:16] + "…",
            "note": "Скопируйте сейчас — второй раз секрет не покажется."}


@delete("/tokens/{prefix:str}", status_code=200)
async def revoke_token(request: Request, prefix: str) -> Dict[str, Any]:
    """Отозвать по префиксу из списка (полный секрет клиенту неизвестен)."""
    uid = _verified_user(request)
    st = _store()
    head = (prefix or "").rstrip("…").strip()
    if len(head) < 12:
        raise HTTPException(status_code=400, detail="слишком короткий префикс")
    full = st.find_by_prefix(uid, head)
    if not full:
        raise HTTPException(status_code=404, detail="токен не найден")
    st.revoke(full)
    logger.info("MCP token revoked for user=%s", uid)
    return {"status": "success", "revoked": head + "…"}


@get("/connect-info")
async def connect_info(request: Request) -> Dict[str, Any]:
    """Куда подключаться и чем. Без секретов — только адрес и шаблоны команд."""
    _verified_user(request)
    base = (os.getenv("MCP_PUBLIC_URL", "").strip()
            or os.getenv("PUBLIC_API_URL", "").strip()
            or os.getenv("TESSENT_PUBLIC_URL", "").strip()).rstrip("/")
    if not base:
        # Собственный адрес запроса — честнее, чем localhost в подсказке.
        base = str(request.base_url).rstrip("/")
    url = f"{base}/api/v1/mcp"
    return {
        "url": url,
        "auth": "Bearer <ваш токен>",
        "clients": [
            {"id": "claude_code", "name": "Claude Code",
             "command": (f'claude mcp add --transport http tessent {url} '
                         f'-H "Authorization: Bearer <ВАШ_ТОКЕН>"')},
            {"id": "cursor", "name": "Cursor / любой MCP-клиент по HTTP",
             "config": {"mcpServers": {"tessent": {
                 "type": "http", "url": url,
                 "headers": {"Authorization": "Bearer <ВАШ_ТОКЕН>"}}}}},
        ],
    }


mcp_tokens_router = Router(path="/mcp",
                           route_handlers=[list_tokens, mint_token,
                                           revoke_token, connect_info],
                           tags=["MCP"])
