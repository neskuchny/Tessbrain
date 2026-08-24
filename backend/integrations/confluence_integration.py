# -*- coding: utf-8 -*-
"""
Confluence Integration — поиск, чтение и создание страниц.

Использует Confluence REST API v2 (Atlassian Cloud).
Аутентификация: Basic Auth (email + API token).

Credentials (из user_integrations):
    - domain: your-company.atlassian.net
    - email: user email
    - api_token: Atlassian API token
"""

import base64
import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from backend.integrations.base import BaseIntegration
from backend.integrations.registry import register_integration

logger = logging.getLogger(__name__)


@register_integration
class ConfluenceIntegration(BaseIntegration):
    integration_id = "confluence"
    integration_name = "Confluence"
    category = "knowledge"
    keywords = [
        # Русский
        "confluence", "конфлюенс", "конфлюэнс",
        "вики", "wiki", "документация", "база знаний",
        "найди в конфлюенсе", "создай страницу confluence",
        "пространство confluence",
        # English
        "confluence page", "confluence search",
        "wiki page", "knowledge base", "space",
        "create page", "find in confluence",
        # Транслит
        "konfluens",
    ]

    def _base_url(self) -> str:
        domain = self.get_key("domain")
        return f"https://{domain}/wiki"

    def _headers(self) -> Dict[str, str]:
        email = self.get_key("email")
        token = self.get_key("api_token")
        auth = base64.b64encode(f"{email}:{token}".encode()).decode()
        return {
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _api(self, method: str, path: str, body: Optional[Dict] = None,
                   params: Optional[Dict] = None) -> Dict[str, Any]:
        url = f"{self._base_url()}{path}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(method, url, headers=self._headers(),
                                        json=body, params=params)
            if resp.status_code >= 400:
                try:
                    msg = resp.json().get("message", resp.text[:300])
                except Exception:
                    msg = resp.text[:500]
                return {"error": True, "status": resp.status_code, "message": msg}
            if resp.status_code == 204:
                return {"success": True}
            return resp.json()

    # ── Tool implementations ──────────────────────────────────────

    async def search_pages(self, query: str, limit: int = 20) -> str:
        """Поиск страниц по тексту (CQL)."""
        cql = f'type = page AND text ~ "{query}"'
        result = await self._api("GET", "/rest/api/content/search", params={
            "cql": cql,
            "limit": min(limit, 50),
            "expand": "metadata.labels,space,version",
        })
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        pages = []
        for r in result.get("results", []):
            pages.append({
                "id": r.get("id", ""),
                "title": r.get("title", ""),
                "space": r.get("space", {}).get("name", ""),
                "space_key": r.get("space", {}).get("key", ""),
                "version": r.get("version", {}).get("number", 0),
                "url": f"{self._base_url()}{r.get('_links', {}).get('webui', '')}",
            })

        return json.dumps({
            "success": True,
            "query": query,
            "count": len(pages),
            "pages": pages,
        }, ensure_ascii=False)

    async def get_page(self, page_id: str) -> str:
        """Прочитать страницу."""
        result = await self._api("GET", f"/rest/api/content/{page_id}", params={
            "expand": "body.storage,space,version,metadata.labels",
        })
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        body_html = result.get("body", {}).get("storage", {}).get("value", "")
        # Простая очистка HTML -> текст
        import re
        text = re.sub(r'<[^>]+>', ' ', body_html)
        text = re.sub(r'\s+', ' ', text).strip()

        labels = [l.get("name", "") for l in result.get("metadata", {}).get("labels", {}).get("results", [])]

        return json.dumps({
            "success": True,
            "id": page_id,
            "title": result.get("title", ""),
            "space": result.get("space", {}).get("name", ""),
            "version": result.get("version", {}).get("number", 0),
            "labels": labels,
            "text": text[:8000],
            "text_length": len(text),
            "url": f"{self._base_url()}{result.get('_links', {}).get('webui', '')}",
        }, ensure_ascii=False)

    async def create_page(self, title: str, body_text: str,
                          space_key: str, parent_id: str = "") -> str:
        """Создать страницу."""
        body_storage = f"<p>{body_text}</p>"
        page_data: Dict[str, Any] = {
            "type": "page",
            "title": title,
            "space": {"key": space_key},
            "body": {
                "storage": {
                    "value": body_storage,
                    "representation": "storage",
                }
            },
        }
        if parent_id:
            page_data["ancestors"] = [{"id": parent_id}]

        result = await self._api("POST", "/rest/api/content", body=page_data)
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        return json.dumps({
            "success": True,
            "page_id": result.get("id", ""),
            "title": title,
            "space_key": space_key,
            "url": f"{self._base_url()}{result.get('_links', {}).get('webui', '')}",
        }, ensure_ascii=False)

    async def list_spaces(self, limit: int = 20) -> str:
        """Список пространств."""
        result = await self._api("GET", "/rest/api/space", params={
            "limit": min(limit, 50),
            "type": "global",
        })
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        spaces = []
        for s in result.get("results", []):
            spaces.append({
                "key": s.get("key", ""),
                "name": s.get("name", ""),
                "type": s.get("type", ""),
                "url": f"{self._base_url()}/spaces/{s.get('key', '')}",
            })

        return json.dumps({"success": True, "count": len(spaces), "spaces": spaces}, ensure_ascii=False)

    # ── BaseIntegration interface ─────────────────────────────────

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "confluence_search",
                "description": "Search Confluence pages by text. Use for: find in wiki, search confluence, найди в конфлюенсе.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search text"},
                        "limit": {"type": "integer", "description": "Max results (default 20)"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "confluence_get_page",
                "description": "Read a Confluence page content. Use for: read wiki page, прочитай страницу confluence.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "page_id": {"type": "string", "description": "Confluence page ID"},
                    },
                    "required": ["page_id"],
                },
            },
            {
                "name": "confluence_create_page",
                "description": "Create a new Confluence page. Use for: create wiki page, создай страницу в confluence.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Page title"},
                        "body_text": {"type": "string", "description": "Page body text"},
                        "space_key": {"type": "string", "description": "Space key (e.g. 'DEV', 'TEAM')"},
                        "parent_id": {"type": "string", "description": "Parent page ID (optional)"},
                    },
                    "required": ["title", "body_text", "space_key"],
                },
            },
            {
                "name": "confluence_list_spaces",
                "description": "List Confluence spaces. Use for: show spaces, пространства confluence.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "Max results (default 20)"},
                    },
                },
            },
        ]

    async def execute(self, tool_name: str, **kwargs) -> str:
        if not await self.load_credentials():
            return json.dumps({"error": "Confluence not configured. Add domain, email, and API token in integrations."}, ensure_ascii=False)

        dispatch = {
            "confluence_search": lambda: self.search_pages(
                query=str(kwargs.get("query", "")),
                limit=int(kwargs.get("limit", 20)),
            ),
            "confluence_get_page": lambda: self.get_page(str(kwargs.get("page_id", ""))),
            "confluence_create_page": lambda: self.create_page(
                title=str(kwargs.get("title", "")),
                body_text=str(kwargs.get("body_text", "")),
                space_key=str(kwargs.get("space_key", "")),
                parent_id=str(kwargs.get("parent_id", "")),
            ),
            "confluence_list_spaces": lambda: self.list_spaces(
                limit=int(kwargs.get("limit", 20)),
            ),
        }

        handler = dispatch.get(tool_name)
        if handler:
            return await handler()
        return json.dumps({"error": f"Unknown Confluence tool: {tool_name}"}, ensure_ascii=False)
