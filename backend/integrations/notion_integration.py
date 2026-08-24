# -*- coding: utf-8 -*-
"""
Notion Integration — поиск, создание и обновление страниц и баз данных.

Использует Notion API v2022-06-28.
Требуемый ключ: integration_token (Internal Integration Token).

Credentials (из user_integrations):
    - integration_token: secret_... (Notion Internal Integration Token)
    - default_page_id: (опционально — ID страницы-родителя по умолчанию)
"""

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from backend.integrations.base import BaseIntegration
from backend.integrations.registry import register_integration

logger = logging.getLogger(__name__)

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


@register_integration
class NotionIntegration(BaseIntegration):
    integration_id = "notion"
    integration_name = "Notion"
    category = "knowledge"
    keywords = [
        # Русский
        "notion", "ноушен", "ноушн",
        "запиши в notion", "создай страницу",
        "найди в notion", "база данных notion",
        "wiki",
        # English
        "notion page", "notion database", "create page",
        "write to notion", "search notion", "query database",
        "notion doc", "notion wiki",
        # Транслит
        "noushen", "noushn",
    ]

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.get_key('integration_token')}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    async def _request(self, method: str, path: str, body: Optional[Dict] = None) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(
                method,
                f"{NOTION_API}{path}",
                headers=self._headers(),
                json=body,
            )
            if resp.status_code >= 400:
                err = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"message": resp.text}
                logger.warning(f"[Notion] {method} {path} → {resp.status_code}: {err.get('message', '')}")
                return {"error": True, "status": resp.status_code, "message": err.get("message", str(err))}
            return resp.json()

    # ── Tool implementations ──────────────────────────────────────

    async def search(self, query: str, filter_type: str = "") -> str:
        """Поиск по Notion (страницы и базы данных)."""
        body: Dict[str, Any] = {"query": query, "page_size": 20}
        if filter_type in ("page", "database"):
            body["filter"] = {"value": filter_type, "property": "object"}
        result = await self._request("POST", "/search", body)
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        items = []
        for r in result.get("results", []):
            title = ""
            if r["object"] == "page":
                props = r.get("properties", {})
                for prop in props.values():
                    if prop.get("type") == "title":
                        title = "".join(t.get("plain_text", "") for t in prop.get("title", []))
                        break
            elif r["object"] == "database":
                title = "".join(t.get("plain_text", "") for t in r.get("title", []))
            items.append({
                "id": r["id"],
                "type": r["object"],
                "title": title,
                "url": r.get("url", ""),
                "last_edited": r.get("last_edited_time", ""),
            })
        return json.dumps({"success": True, "count": len(items), "results": items}, ensure_ascii=False)

    async def create_page(self, parent_id: str, title: str, content: str = "") -> str:
        """Создать страницу в Notion."""
        if not parent_id:
            parent_id = self.get_key("default_page_id")
        if not parent_id:
            return json.dumps({"success": False, "error": "No parent_id provided and no default configured"}, ensure_ascii=False)

        # Определяем тип parent (page или database)
        children = []
        if content:
            # Разбиваем на блоки по абзацам
            paragraphs = content.split("\n\n") if "\n\n" in content else content.split("\n")
            for p in paragraphs[:100]:  # Notion limit
                if p.strip():
                    children.append({
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {
                            "rich_text": [{"type": "text", "text": {"content": p.strip()[:2000]}}]
                        },
                    })

        body: Dict[str, Any] = {
            "parent": {"page_id": parent_id},
            "properties": {
                "title": {"title": [{"text": {"content": title}}]}
            },
        }
        if children:
            body["children"] = children

        result = await self._request("POST", "/pages", body)
        if result.get("error"):
            # Попробуем как database parent
            body["parent"] = {"database_id": parent_id}
            body["properties"] = {"Name": {"title": [{"text": {"content": title}}]}}
            result = await self._request("POST", "/pages", body)
            if result.get("error"):
                return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        return json.dumps({
            "success": True,
            "page_id": result.get("id", ""),
            "url": result.get("url", ""),
            "title": title,
        }, ensure_ascii=False)

    async def get_page(self, page_id: str) -> str:
        """Получить содержимое страницы."""
        page = await self._request("GET", f"/pages/{page_id}")
        if page.get("error"):
            return json.dumps({"success": False, "error": page["message"]}, ensure_ascii=False)

        # Получаем блоки контента
        blocks = await self._request("GET", f"/blocks/{page_id}/children?page_size=100")
        content_parts = []
        for block in blocks.get("results", []):
            btype = block.get("type", "")
            bdata = block.get(btype, {})
            texts = bdata.get("rich_text", [])
            text = "".join(t.get("plain_text", "") for t in texts)
            if text:
                content_parts.append(text)

        title = ""
        for prop in page.get("properties", {}).values():
            if prop.get("type") == "title":
                title = "".join(t.get("plain_text", "") for t in prop.get("title", []))
                break

        return json.dumps({
            "success": True,
            "page_id": page_id,
            "title": title,
            "url": page.get("url", ""),
            "content": "\n\n".join(content_parts),
            "last_edited": page.get("last_edited_time", ""),
        }, ensure_ascii=False)

    async def query_database(self, database_id: str, filter_json: str = "", sort_json: str = "") -> str:
        """Запросить данные из базы данных Notion."""
        body: Dict[str, Any] = {"page_size": 50}
        if filter_json:
            try:
                body["filter"] = json.loads(filter_json)
            except json.JSONDecodeError:
                logger.debug("suppressed exception", exc_info=True)
        if sort_json:
            try:
                body["sorts"] = json.loads(sort_json)
            except json.JSONDecodeError:
                logger.debug("suppressed exception", exc_info=True)

        result = await self._request("POST", f"/databases/{database_id}/query", body)
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        rows = []
        for page in result.get("results", []):
            row: Dict[str, Any] = {"id": page["id"]}
            for prop_name, prop_val in page.get("properties", {}).items():
                ptype = prop_val.get("type", "")
                if ptype == "title":
                    row[prop_name] = "".join(t.get("plain_text", "") for t in prop_val.get("title", []))
                elif ptype == "rich_text":
                    row[prop_name] = "".join(t.get("plain_text", "") for t in prop_val.get("rich_text", []))
                elif ptype == "select":
                    sel = prop_val.get("select")
                    row[prop_name] = sel["name"] if sel else ""
                elif ptype == "multi_select":
                    row[prop_name] = [s["name"] for s in prop_val.get("multi_select", [])]
                elif ptype == "number":
                    row[prop_name] = prop_val.get("number")
                elif ptype == "checkbox":
                    row[prop_name] = prop_val.get("checkbox", False)
                elif ptype == "date":
                    d = prop_val.get("date")
                    row[prop_name] = d.get("start", "") if d else ""
                elif ptype == "status":
                    st = prop_val.get("status")
                    row[prop_name] = st["name"] if st else ""
                elif ptype == "url":
                    row[prop_name] = prop_val.get("url", "")
            rows.append(row)

        return json.dumps({"success": True, "count": len(rows), "rows": rows}, ensure_ascii=False)

    async def update_page(self, page_id: str, properties_json: str) -> str:
        """Обновить свойства страницы."""
        try:
            props = json.loads(properties_json)
        except json.JSONDecodeError:
            return json.dumps({"success": False, "error": "Invalid properties JSON"}, ensure_ascii=False)

        result = await self._request("PATCH", f"/pages/{page_id}", {"properties": props})
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)
        return json.dumps({"success": True, "page_id": page_id, "url": result.get("url", "")}, ensure_ascii=False)

    # ── BaseIntegration interface ─────────────────────────────────

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "notion_search",
                "description": "Search pages and databases in Notion by text query.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search text"},
                        "filter_type": {"type": "string", "description": "Filter: 'page' or 'database' (optional)"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "notion_create_page",
                "description": "Create a new page in Notion with title and optional content.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "parent_id": {"type": "string", "description": "Parent page or database ID (uses default if empty)"},
                        "title": {"type": "string", "description": "Page title"},
                        "content": {"type": "string", "description": "Page content text (optional)"},
                    },
                    "required": ["title"],
                },
            },
            {
                "name": "notion_get_page",
                "description": "Get the content of a Notion page by ID.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "page_id": {"type": "string", "description": "Notion page ID"},
                    },
                    "required": ["page_id"],
                },
            },
            {
                "name": "notion_query_database",
                "description": "Query rows from a Notion database with optional filter and sort.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "database_id": {"type": "string", "description": "Notion database ID"},
                        "filter_json": {"type": "string", "description": "Notion filter object as JSON string (optional)"},
                        "sort_json": {"type": "string", "description": "Notion sorts array as JSON string (optional)"},
                    },
                    "required": ["database_id"],
                },
            },
            {
                "name": "notion_update_page",
                "description": "Update properties of an existing Notion page.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "page_id": {"type": "string", "description": "Notion page ID"},
                        "properties_json": {"type": "string", "description": "Properties to update as JSON string"},
                    },
                    "required": ["page_id", "properties_json"],
                },
            },
        ]

    async def execute(self, tool_name: str, **kwargs) -> str:
        if not await self.load_credentials():
            return json.dumps({"error": "Notion not configured. Add integration_token in integrations."}, ensure_ascii=False)

        if tool_name == "notion_search":
            return await self.search(query=str(kwargs.get("query", "")), filter_type=str(kwargs.get("filter_type", "")))
        elif tool_name == "notion_create_page":
            return await self.create_page(
                parent_id=str(kwargs.get("parent_id", "")),
                title=str(kwargs.get("title", "")),
                content=str(kwargs.get("content", "")),
            )
        elif tool_name == "notion_get_page":
            return await self.get_page(page_id=str(kwargs.get("page_id", "")))
        elif tool_name == "notion_query_database":
            return await self.query_database(
                database_id=str(kwargs.get("database_id", "")),
                filter_json=str(kwargs.get("filter_json", "")),
                sort_json=str(kwargs.get("sort_json", "")),
            )
        elif tool_name == "notion_update_page":
            return await self.update_page(
                page_id=str(kwargs.get("page_id", "")),
                properties_json=str(kwargs.get("properties_json", "")),
            )
        return json.dumps({"error": f"Unknown Notion tool: {tool_name}"}, ensure_ascii=False)
