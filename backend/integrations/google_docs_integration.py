# -*- coding: utf-8 -*-
"""
Google Docs Integration — чтение, создание и экспорт документов.

Использует Google Docs API v1 через REST.
Аутентификация: OAuth2 (access_token + refresh_token).

Credentials (из user_integrations):
    - oauth_token: JSON с access_token, refresh_token, client_id, client_secret
"""

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from backend.integrations.base import BaseIntegration
from backend.integrations.registry import register_integration

logger = logging.getLogger(__name__)

DOCS_API = "https://docs.googleapis.com/v1/documents"
DRIVE_API = "https://www.googleapis.com/drive/v3/files"
TOKEN_URL = "https://oauth2.googleapis.com/token"


@register_integration
class GoogleDocsIntegration(BaseIntegration):
    integration_id = "google_docs"
    integration_name = "Google Docs"
    category = "productivity"
    keywords = [
        # Русский
        "google docs", "гугл док", "документ",
        "создай документ", "прочитай документ", "текст документа",
        "гугл документ", "гдок", "gdoc",
        # English
        "google document", "create doc", "read doc",
        "document text", "export doc", "gdocs",
        # Транслит
        "dokument", "sozday dokument",
    ]

    _access_token: str = ""

    async def _ensure_token(self) -> bool:
        if self._access_token:
            return True

        oauth_str = self.get_key("oauth_token")
        if not oauth_str:
            return False

        try:
            oauth = json.loads(oauth_str)
        except json.JSONDecodeError:
            return False

        if oauth.get("access_token"):
            self._access_token = oauth["access_token"]
            return True

        if oauth.get("refresh_token") and oauth.get("client_id") and oauth.get("client_secret"):
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(TOKEN_URL, data={
                    "grant_type": "refresh_token",
                    "refresh_token": oauth["refresh_token"],
                    "client_id": oauth["client_id"],
                    "client_secret": oauth["client_secret"],
                })
                if resp.status_code == 200:
                    self._access_token = resp.json()["access_token"]
                    return True
        return False

    async def _api(self, method: str, url: str, body: Optional[Dict] = None,
                   params: Optional[Dict] = None) -> Dict[str, Any]:
        if not await self._ensure_token():
            return {"error": True, "message": "Google Docs OAuth not configured or token expired"}

        headers = {"Authorization": f"Bearer {self._access_token}"}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(method, url, headers=headers, json=body, params=params)
            if resp.status_code >= 400:
                try:
                    msg = resp.json().get("error", {}).get("message", resp.text[:300])
                except Exception:
                    msg = resp.text[:500]
                return {"error": True, "status": resp.status_code, "message": msg}
            if resp.status_code == 204:
                return {"success": True}
            return resp.json()

    # ── Tool implementations ──────────────────────────────────────

    async def read_document(self, document_id: str) -> str:
        """Прочитать текст документа."""
        result = await self._api("GET", f"{DOCS_API}/{document_id}")
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        title = result.get("title", "")
        # Извлекаем текст из body.content
        text_parts = []
        for elem in result.get("body", {}).get("content", []):
            paragraph = elem.get("paragraph", {})
            for pe in paragraph.get("elements", []):
                text_run = pe.get("textRun", {})
                content = text_run.get("content", "")
                if content:
                    text_parts.append(content)

        full_text = "".join(text_parts)
        return json.dumps({
            "success": True,
            "document_id": document_id,
            "title": title,
            "text": full_text[:10000],
            "text_length": len(full_text),
        }, ensure_ascii=False)

    async def create_document(self, title: str, body_text: str = "") -> str:
        """Создать новый документ."""
        # Создаём пустой документ
        result = await self._api("POST", DOCS_API, body={"title": title})
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        doc_id = result.get("documentId", "")

        # Если есть текст — вставляем
        if body_text and doc_id:
            update_url = f"{DOCS_API}/{doc_id}:batchUpdate"
            await self._api("POST", update_url, body={
                "requests": [{
                    "insertText": {
                        "location": {"index": 1},
                        "text": body_text,
                    }
                }]
            })

        return json.dumps({
            "success": True,
            "document_id": doc_id,
            "title": title,
            "url": f"https://docs.google.com/document/d/{doc_id}/edit",
        }, ensure_ascii=False)

    async def export_document(self, document_id: str, mime_type: str = "text/plain") -> str:
        """Экспортировать документ в указанном формате."""
        export_url = f"{DRIVE_API}/{document_id}/export"
        if not await self._ensure_token():
            return json.dumps({"error": "Google Docs OAuth not configured"}, ensure_ascii=False)

        headers = {"Authorization": f"Bearer {self._access_token}"}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(export_url, headers=headers, params={"mimeType": mime_type})
            if resp.status_code >= 400:
                return json.dumps({"success": False, "error": f"Export failed: {resp.status_code}"}, ensure_ascii=False)

            content = resp.text[:10000]

        return json.dumps({
            "success": True,
            "document_id": document_id,
            "mime_type": mime_type,
            "content": content,
            "content_length": len(resp.text),
        }, ensure_ascii=False)

    async def append_text(self, document_id: str, text: str) -> str:
        """Добавить текст в конец документа."""
        # Сначала получаем длину документа
        doc = await self._api("GET", f"{DOCS_API}/{document_id}",
                              params={"fields": "body.content"})
        if doc.get("error"):
            return json.dumps({"success": False, "error": doc["message"]}, ensure_ascii=False)

        # Находим конечный индекс
        end_index = 1
        for elem in doc.get("body", {}).get("content", []):
            if "endIndex" in elem:
                end_index = elem["endIndex"]

        # Вставляем текст перед последним символом (newline)
        insert_index = max(1, end_index - 1)
        update_url = f"{DOCS_API}/{document_id}:batchUpdate"
        result = await self._api("POST", update_url, body={
            "requests": [{
                "insertText": {
                    "location": {"index": insert_index},
                    "text": "\n" + text,
                }
            }]
        })
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        return json.dumps({
            "success": True,
            "document_id": document_id,
            "appended_length": len(text),
        }, ensure_ascii=False)

    # ── BaseIntegration interface ─────────────────────────────────

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "docs_read",
                "description": "Read the text content of a Google Doc. Use for: read document, get doc text, прочитай документ.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "document_id": {"type": "string", "description": "Google Doc ID (from URL)"},
                    },
                    "required": ["document_id"],
                },
            },
            {
                "name": "docs_create",
                "description": "Create a new Google Doc with optional text. Use for: create document, write doc, создай документ.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Document title"},
                        "body_text": {"type": "string", "description": "Initial text content (optional)"},
                    },
                    "required": ["title"],
                },
            },
            {
                "name": "docs_export",
                "description": "Export Google Doc as plain text, HTML, or PDF. Use for: export document, download doc, экспорт документа.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "document_id": {"type": "string", "description": "Google Doc ID"},
                        "mime_type": {"type": "string", "description": "Export format: text/plain, text/html, application/pdf"},
                    },
                    "required": ["document_id"],
                },
            },
            {
                "name": "docs_append",
                "description": "Append text to the end of a Google Doc. Use for: add text to doc, дополни документ.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "document_id": {"type": "string", "description": "Google Doc ID"},
                        "text": {"type": "string", "description": "Text to append"},
                    },
                    "required": ["document_id", "text"],
                },
            },
        ]

    async def execute(self, tool_name: str, **kwargs) -> str:
        if not await self.load_credentials():
            return json.dumps({"error": "Google Docs not configured. Add OAuth token in integrations."}, ensure_ascii=False)

        dispatch = {
            "docs_read": lambda: self.read_document(str(kwargs.get("document_id", ""))),
            "docs_create": lambda: self.create_document(
                title=str(kwargs.get("title", "Untitled")),
                body_text=str(kwargs.get("body_text", "")),
            ),
            "docs_export": lambda: self.export_document(
                document_id=str(kwargs.get("document_id", "")),
                mime_type=str(kwargs.get("mime_type", "text/plain")),
            ),
            "docs_append": lambda: self.append_text(
                document_id=str(kwargs.get("document_id", "")),
                text=str(kwargs.get("text", "")),
            ),
        }

        handler = dispatch.get(tool_name)
        if handler:
            return await handler()
        return json.dumps({"error": f"Unknown Docs tool: {tool_name}"}, ensure_ascii=False)
