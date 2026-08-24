#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tessbrain MCP over Streamable HTTP — те же инструменты, что stdio `mcp_server.py`,
но по HTTP. Нужно ТОЛЬКО для дистрибуции через каталог Anthropic (Directory) и
HTTP-конфига Codex `[[servers]] type="http"`. Для локального Claude Code/Codex
CLI достаточно stdio-сервера (`mcp_server.py`) — HTTP не обязателен.

Переиспользует `mcp_server.TOOLS` (ноль дублирования логики инструментов).

── Запуск ──────────────────────────────────────────────────────────────────
    pip install "mcp[cli]" uvicorn starlette
    MCP_HOST=0.0.0.0 MCP_PORT=8770 python mcp_http_server.py
    # эндпоинт: http://<host>:8770/mcp

── Подключить ──────────────────────────────────────────────────────────────
    Claude Code:  claude mcp add --transport http tessent http://<host>:8770/mcp
    Codex (~/.codex/config.toml):
        [[servers]]
        type = "http"
        name = "tessent"
        url  = "http://<host>:8770/mcp"

Доступ к Brain — те же env, что у stdio-сервера/CLI: TESSENT_API_URL /
TESSENT_API_TOKEN / TESSENT_USER_ID.

ПРИМЕЧАНИЕ: требует пакет `mcp` (+ uvicorn/starlette) и smoke-тест на деплое —
здесь он синтаксически проверен, но офлайн-окружение пакета не содержит.
"""
import contextlib
import os
import sys


def build_app():
    """Собрать ASGI-приложение Streamable HTTP поверх низкоуровневого MCP Server."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import mcp_server as stdio  # переиспользуем TOOLS + _api (тот же HTTP-слой)

    import mcp.types as types
    from mcp.server.lowlevel import Server
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Mount

    server = Server("tessent-brain")

    @server.list_tools()
    async def _list_tools():
        return [types.Tool(name=name, description=desc, inputSchema=schema)
                for name, (desc, schema, _fn) in stdio.TOOLS.items()]

    @server.call_tool()
    async def _call_tool(name, arguments):
        entry = stdio.TOOLS.get(name)
        if not entry:
            return [types.TextContent(type="text", text=f"unknown tool: {name}")]
        try:
            text = entry[2](arguments or {})
        except Exception as e:
            text = f"Ошибка: {e}"
        return [types.TextContent(type="text", text=str(text))]

    session_manager = StreamableHTTPSessionManager(
        app=server, json_response=False, stateless=True)

    async def _handle(scope, receive, send):
        await session_manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def _lifespan(app):
        async with session_manager.run():
            yield

    return Starlette(routes=[Mount("/mcp", app=_handle)], lifespan=_lifespan)


def main() -> None:
    try:
        import uvicorn
    except ImportError:
        sys.stderr.write("Требуется: pip install 'mcp[cli]' uvicorn starlette\n")
        sys.exit(1)
    app = build_app()
    uvicorn.run(app, host=os.getenv("MCP_HOST", "127.0.0.1"),
                port=int(os.getenv("MCP_PORT", "8770")))


if __name__ == "__main__":
    main()
