# -*- coding: utf-8 -*-
"""Hermes MCP-client (P12h) — внешние tool-серверы по Model Context Protocol.

ПРОБЕЛ (карта переноса, #8): у Hermes есть MCP-интеграция — подключение
внешних tool-серверов БЕЗ кода. У нас не было.

Слои:
- ЧИСТЫЙ протокол (discover/normalize/dispatch) — транспорт
  инъектируем (async (method, params) -> dict), юнит-тест с фейком,
  never-raises. Имена namespace'ятся `mcp__<server>__<tool>` (как в
  индустрии) → нет коллизий с нашими инструментами; approval-gate
  (P12e) классифицирует их по подстроке (create/delete/read → W/D/S).
- РЕАЛЬНЫЙ stdio-транспорт (JSON-RPC по line-framing, init+tools/list+
  tools/call, таймауты) — защищён, never-raises; интеграционно (нужен
  процесс), поэтому не в unit-CI.

Careful rollout: без сконфигурированных серверов реестр пуст → нулевой
эффект (как слоты P12f). env `HERMES_MCP_ENABLED` + конфиг серверов.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# transport(method, params) -> dict (JSON-RPC result). Инъектируем.
MCPTransport = Callable[[str, dict], Awaitable[dict]]

_PREFIX = "mcp__"
_MAX_TOOLS_PER_SERVER = 64


def is_mcp_tool(name: str) -> bool:
    return isinstance(name, str) and name.startswith(_PREFIX)


def _qualify(server: str, tool: str) -> str:
    safe_s = "".join(c for c in str(server) if c.isalnum() or c in "_-") or "srv"
    safe_t = "".join(c for c in str(tool) if c.isalnum() or c in "_-.") or "tool"
    return f"{_PREFIX}{safe_s}__{safe_t}"


def _split(name: str) -> tuple[str, str]:
    """`mcp__server__tool` -> (server, tool). ('','') если не MCP."""
    if not is_mcp_tool(name):
        return "", ""
    rest = name[len(_PREFIX):]
    server, _, tool = rest.partition("__")
    return server, tool


def normalize_tool_defs(server: str, raw_list: Any) -> list[dict]:
    """MCP tools/list result -> наш формат tool-def. Не raises."""
    out: list[dict] = []
    try:
        items = raw_list if isinstance(raw_list, list) else []
        for it in items[:_MAX_TOOLS_PER_SERVER]:
            if not isinstance(it, dict):
                continue
            tname = it.get("name")
            if not tname:
                continue
            out.append({
                "name": _qualify(server, str(tname)),
                "description": str(it.get("description", "") or ""),
                "parameters": it.get("inputSchema")
                if isinstance(it.get("inputSchema"), dict)
                else {"type": "object", "properties": {}},
            })
    except Exception as exc:
        logger.debug("normalize_tool_defs failed: %s", exc)
    return out


@dataclass
class MCPRegistry:
    """Реестр серверов: {server_name: transport}. Не raises."""
    _servers: dict[str, MCPTransport] = field(default_factory=dict)

    def register(self, name: str, transport: MCPTransport) -> None:
        if name and callable(transport):
            self._servers[str(name)] = transport

    @property
    def server_names(self) -> list[str]:
        return list(self._servers)

    async def discover(self) -> list[dict]:
        """Агрегированные tool-defs со всех серверов. Не raises."""
        defs: list[dict] = []
        for sname, tr in self._servers.items():
            try:
                res = await tr("tools/list", {})
                tools = (res or {}).get("tools") if isinstance(res, dict) else None
                defs.extend(normalize_tool_defs(sname, tools))
            except Exception as exc:
                logger.debug("mcp discover '%s' failed: %s", sname, exc)
        return defs

    async def execute(self, name: str, args: dict) -> str:
        """Вызвать `mcp__server__tool`. JSON-строка. Не raises."""
        server, tool = _split(name)
        if not server or server not in self._servers:
            return json.dumps({"error": "mcp server not found",
                               "tool": name}, ensure_ascii=False)
        try:
            res = await self._servers[server](
                "tools/call", {"name": tool, "arguments": args or {}})
            return json.dumps(res, ensure_ascii=False, default=str) \
                if not isinstance(res, str) else res
        except Exception as exc:
            logger.debug("mcp execute '%s' failed: %s", name, exc)
            return json.dumps({"error": "mcp call failed", "tool": name},
                              ensure_ascii=False)


def make_stdio_transport(
    command: str,
    *,
    timeout: float = 20.0,
) -> MCPTransport:
    """РЕАЛЬНЫЙ транспорт: MCP-сервер как подпроцесс, JSON-RPC по
    stdin/stdout (line-framed). Защищён, never-raises (ошибка →
    {'error':..}). Интеграционный (нужен процесс) — не в unit-CI.

    command: напр. 'npx -y @modelcontextprotocol/server-filesystem /tmp'
    """
    state: dict[str, Any] = {"proc": None, "id": 0, "init": False}

    async def _ensure() -> Optional[Any]:
        if state["proc"] is not None and state["proc"].returncode is None:
            return state["proc"]
        try:
            argv = shlex.split(command)
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            state["proc"] = proc
            state["init"] = False
            return proc
        except Exception as exc:
            logger.debug("mcp stdio spawn failed: %s", exc)
            return None

    async def _rpc(proc: Any, method: str, params: dict) -> dict:
        state["id"] += 1
        req = {"jsonrpc": "2.0", "id": state["id"],
               "method": method, "params": params or {}}
        proc.stdin.write((json.dumps(req) + "\n").encode("utf-8"))
        await proc.stdin.drain()
        line = await asyncio.wait_for(proc.stdout.readline(), timeout)
        if not line:
            return {"error": "mcp: empty response"}
        msg = json.loads(line.decode("utf-8"))
        return msg.get("result", msg) if isinstance(msg, dict) else {}

    async def _transport(method: str, params: dict) -> dict:
        try:
            proc = await _ensure()
            if proc is None:
                return {"error": "mcp: server not available"}
            if not state["init"]:
                try:
                    await _rpc(proc, "initialize", {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "tessent-hermes",
                                       "version": "1"},
                    })
                except Exception:
                    pass
                state["init"] = True
            return await asyncio.wait_for(
                _rpc(proc, method, params), timeout + 5)
        except Exception as exc:
            logger.debug("mcp stdio transport failed: %s", exc)
            return {"error": "mcp transport error"}

    return _transport


def registry_from_config(
    servers: Optional[dict[str, str]] = None,
    *,
    transport_factory: Callable[[str], MCPTransport] = None,
) -> MCPRegistry:
    """{server_name: command} -> реестр. transport_factory инъектируем
    (тест → фейк; прод → make_stdio_transport). Пусто → пустой реестр
    (нулевой эффект). Не raises."""
    reg = MCPRegistry()
    if not servers:
        return reg
    factory = transport_factory or (lambda cmd: make_stdio_transport(cmd))
    for sname, cmd in servers.items():
        try:
            reg.register(str(sname), factory(str(cmd)))
        except Exception as exc:
            logger.debug("mcp register '%s' failed: %s", sname, exc)
    return reg


__all__ = [
    "MCPTransport",
    "MCPRegistry",
    "is_mcp_tool",
    "normalize_tool_defs",
    "make_stdio_transport",
    "registry_from_config",
]
