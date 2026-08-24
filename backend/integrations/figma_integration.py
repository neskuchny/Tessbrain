# -*- coding: utf-8 -*-
"""
Figma Integration — файлы, комментарии, компоненты.

Использует Figma REST API v1.
Аутентификация: Personal Access Token.

Credentials (из user_integrations):
    - access_token: Figma Personal Access Token (figd_...)
"""

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from backend.integrations.base import BaseIntegration
from backend.integrations.registry import register_integration

logger = logging.getLogger(__name__)

FIGMA_API = "https://api.figma.com/v1"


@register_integration
class FigmaIntegration(BaseIntegration):
    integration_id = "figma"
    integration_name = "Figma"
    category = "design"
    keywords = [
        # Русский
        "figma", "фигма", "дизайн", "макет",
        "компонент figma", "файл figma",
        "комментар", "фрейм", "прототип",
        # English
        "figma file", "figma component", "design file",
        "figma comment", "frame", "prototype",
        "figma project", "style guide",
        # Транслит
        "figma maket",
    ]

    def _headers(self) -> Dict[str, str]:
        return {"X-Figma-Token": self.get_key("access_token")}

    async def _api(self, path: str, params: Optional[Dict] = None) -> Dict[str, Any]:
        url = f"{FIGMA_API}{path}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=self._headers(), params=params)
            if resp.status_code >= 400:
                try:
                    msg = resp.json().get("err", resp.text[:300])
                except Exception:
                    msg = resp.text[:500]
                return {"error": True, "status": resp.status_code, "message": msg}
            return resp.json()

    # ── Tool implementations ──────────────────────────────────────

    async def get_file(self, file_key: str) -> str:
        """Получить информацию о файле и его структуру."""
        result = await self._api(f"/files/{file_key}", params={"depth": 2})
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        document = result.get("document", {})

        # Извлекаем страницы и фреймы (depth=2)
        pages = []
        for child in document.get("children", []):
            frames = []
            for frame in child.get("children", [])[:20]:
                frames.append({
                    "id": frame.get("id", ""),
                    "name": frame.get("name", ""),
                    "type": frame.get("type", ""),
                })
            pages.append({
                "id": child.get("id", ""),
                "name": child.get("name", ""),
                "type": child.get("type", ""),
                "frames_count": len(child.get("children", [])),
                "frames": frames,
            })

        return json.dumps({
            "success": True,
            "file_key": file_key,
            "name": result.get("name", ""),
            "last_modified": result.get("lastModified", ""),
            "version": result.get("version", ""),
            "pages_count": len(pages),
            "pages": pages,
        }, ensure_ascii=False)

    async def get_comments(self, file_key: str) -> str:
        """Получить комментарии к файлу."""
        result = await self._api(f"/files/{file_key}/comments")
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        comments = []
        for c in result.get("comments", [])[:50]:
            comments.append({
                "id": c.get("id", ""),
                "message": c.get("message", ""),
                "user": c.get("user", {}).get("handle", ""),
                "created_at": c.get("created_at", ""),
                "resolved_at": c.get("resolved_at", ""),
                "order_id": c.get("order_id", ""),
            })

        return json.dumps({
            "success": True,
            "file_key": file_key,
            "count": len(comments),
            "comments": comments,
        }, ensure_ascii=False)

    async def post_comment(self, file_key: str, message: str,
                           node_id: str = "") -> str:
        """Добавить комментарий к файлу или ноде."""
        url = f"{FIGMA_API}/files/{file_key}/comments"
        body: Dict[str, Any] = {"message": message}
        if node_id:
            body["client_meta"] = {"node_id": node_id, "node_offset": {"x": 0, "y": 0}}

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, headers=self._headers(), json=body)
            if resp.status_code >= 400:
                return json.dumps({"success": False, "error": resp.text[:300]}, ensure_ascii=False)
            data = resp.json()

        return json.dumps({
            "success": True,
            "comment_id": data.get("id", ""),
            "message": message,
        }, ensure_ascii=False)

    async def get_components(self, file_key: str) -> str:
        """Получить компоненты файла."""
        result = await self._api(f"/files/{file_key}/components")
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        components = []
        meta = result.get("meta", {})
        for comp in meta.get("components", [])[:50]:
            components.append({
                "key": comp.get("key", ""),
                "name": comp.get("name", ""),
                "description": comp.get("description", ""),
                "containing_frame": comp.get("containing_frame", {}).get("name", ""),
            })

        return json.dumps({
            "success": True,
            "file_key": file_key,
            "count": len(components),
            "components": components,
        }, ensure_ascii=False)

    async def get_team_projects(self, team_id: str) -> str:
        """Получить проекты команды."""
        result = await self._api(f"/teams/{team_id}/projects")
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        projects = []
        for p in result.get("projects", []):
            projects.append({
                "id": p.get("id", ""),
                "name": p.get("name", ""),
            })

        return json.dumps({"success": True, "count": len(projects), "projects": projects}, ensure_ascii=False)

    # ── BaseIntegration interface ─────────────────────────────────

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "figma_get_file",
                "description": "Get Figma file info: pages, frames, structure. Use for: show figma file, файл в фигме, структура макета.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_key": {"type": "string", "description": "Figma file key (from URL: figma.com/file/{key}/...)"},
                    },
                    "required": ["file_key"],
                },
            },
            {
                "name": "figma_get_comments",
                "description": "Get comments on a Figma file. Use for: figma comments, комментарии в фигме.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_key": {"type": "string", "description": "Figma file key"},
                    },
                    "required": ["file_key"],
                },
            },
            {
                "name": "figma_post_comment",
                "description": "Add a comment to a Figma file. Use for: comment in figma, добавь комментарий в фигме.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_key": {"type": "string", "description": "Figma file key"},
                        "message": {"type": "string", "description": "Comment text"},
                        "node_id": {"type": "string", "description": "Node ID to comment on (optional)"},
                    },
                    "required": ["file_key", "message"],
                },
            },
            {
                "name": "figma_get_components",
                "description": "List components in a Figma file. Use for: figma components, компоненты фигмы, design system.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_key": {"type": "string", "description": "Figma file key"},
                    },
                    "required": ["file_key"],
                },
            },
            {
                "name": "figma_team_projects",
                "description": "List projects in a Figma team. Use for: figma projects, проекты команды в фигме.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "team_id": {"type": "string", "description": "Figma team ID"},
                    },
                    "required": ["team_id"],
                },
            },
        ]

    async def execute(self, tool_name: str, **kwargs) -> str:
        if not await self.load_credentials():
            return json.dumps({"error": "Figma not configured. Add Personal Access Token in integrations."}, ensure_ascii=False)

        dispatch = {
            "figma_get_file": lambda: self.get_file(str(kwargs.get("file_key", ""))),
            "figma_get_comments": lambda: self.get_comments(str(kwargs.get("file_key", ""))),
            "figma_post_comment": lambda: self.post_comment(
                file_key=str(kwargs.get("file_key", "")),
                message=str(kwargs.get("message", "")),
                node_id=str(kwargs.get("node_id", "")),
            ),
            "figma_get_components": lambda: self.get_components(str(kwargs.get("file_key", ""))),
            "figma_team_projects": lambda: self.get_team_projects(str(kwargs.get("team_id", ""))),
        }

        handler = dispatch.get(tool_name)
        if handler:
            return await handler()
        return json.dumps({"error": f"Unknown Figma tool: {tool_name}"}, ensure_ascii=False)
