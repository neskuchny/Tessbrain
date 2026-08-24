# -*- coding: utf-8 -*-
"""
Slack Integration — отправка, чтение и поиск сообщений в Slack.

Использует Slack Web API (Bot Token).
Требуемые scopes: chat:write, channels:history, channels:read, search:read, reactions:write

Credentials (из user_integrations):
    - bot_token: xoxb-... (Bot User OAuth Token)
    - webhook_url: https://hooks.slack.com/... (опционально, для webhook-only)
"""

import json
import logging
from typing import Any, Dict, List

import httpx

from backend.integrations.base import BaseIntegration
from backend.integrations.registry import register_integration

logger = logging.getLogger(__name__)

SLACK_API_BASE = "https://slack.com/api"


@register_integration
class SlackIntegration(BaseIntegration):
    integration_id = "slack"
    integration_name = "Slack"
    category = "messaging"
    keywords = [
        # Русский
        "слак", "slack", "канал slack", "отправь в слак",
        "пошли в slack", "напиши в слак",
        # English
        "send to slack", "slack channel", "slack message",
        "post to slack", "read slack",
        # Транслит
        "slak",
    ]

    async def _api(self, method: str, **kwargs) -> Dict[str, Any]:
        """Вызов Slack Web API."""
        token = self.get_key("bot_token")
        if not token:
            return {"ok": False, "error": "no_bot_token"}

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{SLACK_API_BASE}/{method}",
                headers=headers,
                json=kwargs,
            )
            data = resp.json()
            if not data.get("ok"):
                logger.warning(f"[Slack] API {method} error: {data.get('error')}")
            return data

    # ── Tool implementations ──────────────────────────────────────

    async def send_message(self, channel: str, text: str, thread_ts: str = "") -> str:
        payload: Dict[str, Any] = {"channel": channel, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        result = await self._api("chat.postMessage", **payload)
        if result.get("ok"):
            ts = result.get("ts", "")
            return json.dumps({"success": True, "message_ts": ts, "channel": channel}, ensure_ascii=False)
        return json.dumps({"success": False, "error": result.get("error", "unknown")}, ensure_ascii=False)

    async def read_messages(self, channel: str, limit: int = 20, *,
                            days: int = 0, oldest: str = "", latest: str = "",
                            max_messages: int = 1000, text_cap: int = 2000) -> str:
        """Прочитать историю канала.

        days>0 → окно последних N дней (для дайджестов) с пагинацией по
        next_cursor (неделя активного канала не влезает в одну страницу —
        раньше молча обрезалось). Иначе — последние `limit` сообщений.
        """
        import time as _time
        params: Dict[str, Any] = {
            "channel": channel,
            "limit": 200 if days > 0 else min(200, max(1, limit)),
        }
        if days and days > 0:
            params["oldest"] = str(oldest or (int(_time.time()) - days * 86400))
        elif oldest:
            params["oldest"] = str(oldest)
        if latest:
            params["latest"] = str(latest)

        messages: List[Dict[str, Any]] = []
        cursor = ""
        pages = 0
        while True:
            call = dict(params)
            if cursor:
                call["cursor"] = cursor
            result = await self._api("conversations.history", **call)
            if not result.get("ok"):
                return json.dumps({"success": False, "error": result.get("error", "unknown")}, ensure_ascii=False)
            for m in result.get("messages", []):
                messages.append({
                    "user": m.get("user", ""),
                    "text": m.get("text", "")[:text_cap],
                    "ts": m.get("ts", ""),
                    "type": m.get("subtype", "message"),
                })
            pages += 1
            cursor = (result.get("response_metadata") or {}).get("next_cursor") or ""
            # окно по дням → идём по страницам; иначе — одна страница.
            if days <= 0 or not cursor or len(messages) >= max_messages or pages >= 20:
                break
        truncated = len(messages) >= max_messages
        return json.dumps({"success": True, "count": len(messages),
                           "truncated": truncated, "pages": pages,
                           "messages": messages}, ensure_ascii=False)

    async def search_messages(self, query: str, count: int = 20) -> str:
        result = await self._api("search.messages", query=query, count=count)
        if result.get("ok"):
            matches = result.get("messages", {}).get("matches", [])
            items = []
            for m in matches[:count]:
                items.append({
                    "text": m.get("text", "")[:500],
                    "channel": m.get("channel", {}).get("name", ""),
                    "user": m.get("username", ""),
                    "ts": m.get("ts", ""),
                    "permalink": m.get("permalink", ""),
                })
            return json.dumps({"success": True, "count": len(items), "results": items}, ensure_ascii=False)
        return json.dumps({"success": False, "error": result.get("error", "unknown")}, ensure_ascii=False)

    async def list_channels(self, limit: int = 100) -> str:
        result = await self._api("conversations.list", limit=limit, types="public_channel,private_channel")
        if result.get("ok"):
            channels = [
                {"id": c["id"], "name": c.get("name", ""), "topic": c.get("topic", {}).get("value", "")}
                for c in result.get("channels", [])
            ]
            return json.dumps({"success": True, "count": len(channels), "channels": channels}, ensure_ascii=False)
        return json.dumps({"success": False, "error": result.get("error", "unknown")}, ensure_ascii=False)

    async def add_reaction(self, channel: str, timestamp: str, emoji: str) -> str:
        result = await self._api("reactions.add", channel=channel, timestamp=timestamp, name=emoji)
        if result.get("ok"):
            return json.dumps({"success": True, "emoji": emoji}, ensure_ascii=False)
        return json.dumps({"success": False, "error": result.get("error", "unknown")}, ensure_ascii=False)

    # ── BaseIntegration interface ─────────────────────────────────

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "slack_send_message",
                "description": "Send a message to a Slack channel or DM. Use channel ID (e.g. C01234) or name (#general).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "channel": {"type": "string", "description": "Channel ID or name (#general)"},
                        "text": {"type": "string", "description": "Message text"},
                        "thread_ts": {"type": "string", "description": "Thread timestamp for reply (optional)"},
                    },
                    "required": ["channel", "text"],
                },
            },
            {
                "name": "slack_read_messages",
                "description": "Read recent messages from a Slack channel.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "channel": {"type": "string", "description": "Channel ID"},
                        "limit": {"type": "integer", "description": "Number of messages (default 20)"},
                    },
                    "required": ["channel"],
                },
            },
            {
                "name": "slack_search_messages",
                "description": "Search messages in Slack by text query.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "count": {"type": "integer", "description": "Max results (default 20)"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "slack_list_channels",
                "description": "List all Slack channels the bot has access to.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "Max channels (default 100)"},
                    },
                },
            },
            {
                "name": "slack_add_reaction",
                "description": "Add an emoji reaction to a Slack message.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "channel": {"type": "string", "description": "Channel ID"},
                        "timestamp": {"type": "string", "description": "Message timestamp"},
                        "emoji": {"type": "string", "description": "Emoji name without colons (e.g. thumbsup)"},
                    },
                    "required": ["channel", "timestamp", "emoji"],
                },
            },
        ]

    async def execute(self, tool_name: str, **kwargs) -> str:
        if not await self.load_credentials():
            return json.dumps({"error": "Slack not configured. Add bot_token in integrations."}, ensure_ascii=False)

        if tool_name == "slack_send_message":
            return await self.send_message(
                channel=str(kwargs.get("channel", "")),
                text=str(kwargs.get("text", "")),
                thread_ts=str(kwargs.get("thread_ts", "")),
            )
        elif tool_name == "slack_read_messages":
            return await self.read_messages(
                channel=str(kwargs.get("channel", "")),
                limit=int(kwargs.get("limit", 20)),
                days=int(kwargs.get("days", 0)),
                oldest=str(kwargs.get("oldest", "")),
                latest=str(kwargs.get("latest", "")),
            )
        elif tool_name == "slack_search_messages":
            return await self.search_messages(
                query=str(kwargs.get("query", "")),
                count=int(kwargs.get("count", 20)),
            )
        elif tool_name == "slack_list_channels":
            return await self.list_channels(limit=int(kwargs.get("limit", 100)))
        elif tool_name == "slack_add_reaction":
            return await self.add_reaction(
                channel=str(kwargs.get("channel", "")),
                timestamp=str(kwargs.get("timestamp", "")),
                emoji=str(kwargs.get("emoji", "")),
            )
        return json.dumps({"error": f"Unknown Slack tool: {tool_name}"}, ensure_ascii=False)
