# -*- coding: utf-8 -*-
"""
Trello Integration — полный CRUD для карточек, списков и досок.

Использует Trello REST API.
Аутентификация: API Key + Token.

Credentials (из user_integrations):
    - api_key: Trello API Key
    - token: Trello User Token
    - default_board_id: (опционально)
"""

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from backend.integrations.base import BaseIntegration
from backend.integrations.registry import register_integration

logger = logging.getLogger(__name__)

TRELLO_API = "https://api.trello.com/1"


@register_integration
class TrelloIntegration(BaseIntegration):
    integration_id = "trello"
    integration_name = "Trello"
    category = "tasks"
    keywords = [
        # Русский
        "trello", "трелло", "карточка trello",
        "доска trello", "создай карточку",
        "перемести карточку", "архивируй",
        # English
        "trello card", "trello board", "trello list",
        "create card", "move card", "archive card",
        "trello search", "my trello cards",
        # Транслит
        "trello", "karochka",
    ]

    def _auth_params(self) -> Dict[str, str]:
        return {"key": self.get_key("api_key"), "token": self.get_key("token")}

    async def _api(self, method: str, path: str, params: Optional[Dict] = None,
                   body: Optional[Dict] = None) -> Any:
        all_params = {**self._auth_params(), **(params or {})}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(
                method, f"{TRELLO_API}{path}",
                params=all_params,
                json=body if method in ("POST", "PUT") else None,
            )
            if resp.status_code >= 400:
                msg = resp.text[:500]
                logger.warning(f"[Trello] {method} {path} → {resp.status_code}: {msg}")
                return {"error": True, "status": resp.status_code, "message": msg}
            if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("application/json"):
                return resp.json()
            return {"success": True}

    # ── Tool implementations ──────────────────────────────────────

    async def get_boards(self) -> str:
        result = await self._api("GET", "/members/me/boards", {"fields": "name,url,closed"})
        if isinstance(result, dict) and result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)
        boards = [{"id": b["id"], "name": b["name"], "url": b.get("url", ""), "closed": b.get("closed", False)}
                  for b in result if not b.get("closed")]
        return json.dumps({"success": True, "count": len(boards), "boards": boards}, ensure_ascii=False)

    async def get_lists(self, board_id: str) -> str:
        if not board_id:
            board_id = self.get_key("default_board_id")
        if not board_id:
            return json.dumps({"success": False, "error": "No board_id provided"}, ensure_ascii=False)
        result = await self._api("GET", f"/boards/{board_id}/lists", {"fields": "name,pos,closed"})
        if isinstance(result, dict) and result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)
        lists = [{"id": l["id"], "name": l["name"]} for l in result if not l.get("closed")]
        return json.dumps({"success": True, "count": len(lists), "lists": lists}, ensure_ascii=False)

    async def get_cards(self, list_id: str = "", board_id: str = "") -> str:
        if list_id:
            path = f"/lists/{list_id}/cards"
        elif board_id:
            path = f"/boards/{board_id}/cards"
        else:
            board_id = self.get_key("default_board_id")
            if not board_id:
                return json.dumps({"success": False, "error": "No list_id or board_id provided"}, ensure_ascii=False)
            path = f"/boards/{board_id}/cards"

        result = await self._api("GET", path, {"fields": "name,desc,due,idList,labels,members,url,closed"})
        if isinstance(result, dict) and result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)
        cards = []
        for c in result:
            if c.get("closed"):
                continue
            cards.append({
                "id": c["id"],
                "name": c["name"],
                "description": (c.get("desc") or "")[:200],
                "due": c.get("due", ""),
                "list_id": c.get("idList", ""),
                "labels": [l.get("name", l.get("color", "")) for l in c.get("labels", [])],
                "url": c.get("url", ""),
            })
        return json.dumps({"success": True, "count": len(cards), "cards": cards}, ensure_ascii=False)

    async def create_card(self, list_id: str, name: str, description: str = "",
                          due: str = "", labels: str = "") -> str:
        params: Dict[str, Any] = {**self._auth_params(), "idList": list_id, "name": name}
        if description:
            params["desc"] = description
        if due:
            params["due"] = due
        if labels:
            params["idLabels"] = labels

        result = await self._api("POST", "/cards", params)
        if isinstance(result, dict) and result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)
        return json.dumps({
            "success": True,
            "card_id": result.get("id", ""),
            "name": name,
            "url": result.get("url", ""),
        }, ensure_ascii=False)

    async def move_card(self, card_id: str, target_list_id: str) -> str:
        result = await self._api("PUT", f"/cards/{card_id}", {"idList": target_list_id})
        if isinstance(result, dict) and result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)
        return json.dumps({"success": True, "card_id": card_id, "new_list_id": target_list_id}, ensure_ascii=False)

    async def add_comment(self, card_id: str, text: str) -> str:
        result = await self._api("POST", f"/cards/{card_id}/actions/comments", {"text": text})
        if isinstance(result, dict) and result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)
        return json.dumps({"success": True, "card_id": card_id}, ensure_ascii=False)

    async def archive_card(self, card_id: str) -> str:
        result = await self._api("PUT", f"/cards/{card_id}", {"closed": "true"})
        if isinstance(result, dict) and result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)
        return json.dumps({"success": True, "card_id": card_id, "archived": True}, ensure_ascii=False)

    async def search_cards(self, query: str, board_id: str = "") -> str:
        params: Dict[str, Any] = {**self._auth_params(), "query": query, "modelTypes": "cards", "cards_limit": 20}
        if board_id:
            params["idBoards"] = board_id
        elif self.get_key("default_board_id"):
            params["idBoards"] = self.get_key("default_board_id")

        result = await self._api("GET", "/search", params)
        if isinstance(result, dict) and result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)
        cards = []
        for c in result.get("cards", []):
            cards.append({
                "id": c["id"],
                "name": c["name"],
                "description": (c.get("desc") or "")[:200],
                "url": c.get("url", ""),
            })
        return json.dumps({"success": True, "count": len(cards), "cards": cards}, ensure_ascii=False)

    # ── BaseIntegration interface ─────────────────────────────────

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "trello_get_boards",
                "description": "List all Trello boards.",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "trello_get_lists",
                "description": "Get all lists (columns) on a Trello board.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "board_id": {"type": "string", "description": "Board ID (uses default if empty)"},
                    },
                },
            },
            {
                "name": "trello_get_cards",
                "description": "Get cards from a Trello list or board.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "list_id": {"type": "string", "description": "List ID (optional)"},
                        "board_id": {"type": "string", "description": "Board ID (optional, uses default)"},
                    },
                },
            },
            {
                "name": "trello_create_card",
                "description": "Create a new card on a Trello list.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "list_id": {"type": "string", "description": "Target list ID"},
                        "name": {"type": "string", "description": "Card title"},
                        "description": {"type": "string", "description": "Card description (optional)"},
                        "due": {"type": "string", "description": "Due date ISO (optional)"},
                    },
                    "required": ["list_id", "name"],
                },
            },
            {
                "name": "trello_move_card",
                "description": "Move a Trello card to another list (column).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "card_id": {"type": "string", "description": "Card ID to move"},
                        "target_list_id": {"type": "string", "description": "Target list ID"},
                    },
                    "required": ["card_id", "target_list_id"],
                },
            },
            {
                "name": "trello_add_comment",
                "description": "Add a comment to a Trello card.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "card_id": {"type": "string", "description": "Card ID"},
                        "text": {"type": "string", "description": "Comment text"},
                    },
                    "required": ["card_id", "text"],
                },
            },
            {
                "name": "trello_archive_card",
                "description": "Archive (close) a Trello card.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "card_id": {"type": "string", "description": "Card ID to archive"},
                    },
                    "required": ["card_id"],
                },
            },
            {
                "name": "trello_search_cards",
                "description": "Search for Trello cards by text query.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "board_id": {"type": "string", "description": "Limit to board (optional)"},
                    },
                    "required": ["query"],
                },
            },
        ]

    async def execute(self, tool_name: str, **kwargs) -> str:
        if not await self.load_credentials():
            return json.dumps({"error": "Trello not configured. Add api_key and token in integrations."}, ensure_ascii=False)

        dispatch = {
            "trello_get_boards": lambda: self.get_boards(),
            "trello_get_lists": lambda: self.get_lists(board_id=str(kwargs.get("board_id", ""))),
            "trello_get_cards": lambda: self.get_cards(
                list_id=str(kwargs.get("list_id", "")),
                board_id=str(kwargs.get("board_id", "")),
            ),
            "trello_create_card": lambda: self.create_card(
                list_id=str(kwargs.get("list_id", "")),
                name=str(kwargs.get("name", "")),
                description=str(kwargs.get("description", "")),
                due=str(kwargs.get("due", "")),
            ),
            "trello_move_card": lambda: self.move_card(
                card_id=str(kwargs.get("card_id", "")),
                target_list_id=str(kwargs.get("target_list_id", "")),
            ),
            "trello_add_comment": lambda: self.add_comment(
                card_id=str(kwargs.get("card_id", "")),
                text=str(kwargs.get("text", "")),
            ),
            "trello_archive_card": lambda: self.archive_card(card_id=str(kwargs.get("card_id", ""))),
            "trello_search_cards": lambda: self.search_cards(
                query=str(kwargs.get("query", "")),
                board_id=str(kwargs.get("board_id", "")),
            ),
        }

        handler = dispatch.get(tool_name)
        if handler:
            return await handler()
        return json.dumps({"error": f"Unknown Trello tool: {tool_name}"}, ensure_ascii=False)
