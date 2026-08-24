# -*- coding: utf-8 -*-
"""
Gmail Integration — поиск, чтение, отправка и ответ на письма.

Использует Gmail API v1 через REST.
Аутентификация: OAuth2 (access_token + refresh_token).

Credentials (из user_integrations):
    - oauth_token: JSON строка с access_token, refresh_token, client_id, client_secret
"""

import base64
import json
import logging
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

import httpx

from backend.integrations.base import BaseIntegration
from backend.integrations.registry import register_integration

logger = logging.getLogger(__name__)

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"


@register_integration
class GmailIntegration(BaseIntegration):
    integration_id = "gmail"
    integration_name = "Gmail"
    category = "email"
    keywords = [
        # Русский
        "gmail", "почта", "письмо", "email",
        "отправь письмо", "напиши письмо",
        "входящие", "непрочитанные",
        "почтовый ящик", "отправь email",
        "переписка", "ответь на письмо",
        # English
        "send email", "email message", "inbox",
        "unread emails", "read email", "compose email",
        "reply email", "draft email", "mail",
        "gmail search", "gmail send",
        # Транслит
        "pochta", "pismo", "otprav pismo",
    ]

    _access_token: str = ""

    async def _ensure_token(self) -> bool:
        """Получить или обновить access_token."""
        if self._access_token:
            return True

        oauth_str = self.get_key("oauth_token")
        if not oauth_str:
            return False

        try:
            oauth = json.loads(oauth_str)
        except json.JSONDecodeError:
            logger.warning("[Gmail] Invalid oauth_token JSON")
            return False

        # Если есть access_token — пробуем его
        if oauth.get("access_token"):
            self._access_token = oauth["access_token"]
            return True

        # Если есть refresh_token — обновляем
        if oauth.get("refresh_token") and oauth.get("client_id") and oauth.get("client_secret"):
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(TOKEN_URL, data={
                    "grant_type": "refresh_token",
                    "refresh_token": oauth["refresh_token"],
                    "client_id": oauth["client_id"],
                    "client_secret": oauth["client_secret"],
                })
                if resp.status_code == 200:
                    data = resp.json()
                    self._access_token = data["access_token"]
                    return True
                logger.warning(f"[Gmail] Token refresh failed: {resp.text[:200]}")
        return False

    async def _api(self, method: str, path: str, body: Optional[Dict] = None,
                   raw_body: Optional[str] = None) -> Dict[str, Any]:
        if not await self._ensure_token():
            return {"error": True, "message": "Gmail OAuth not configured or token expired"}

        headers = {"Authorization": f"Bearer {self._access_token}"}
        if raw_body:
            headers["Content-Type"] = "message/rfc822"
        elif body:
            headers["Content-Type"] = "application/json"

        url = f"{GMAIL_API}{path}"
        async with httpx.AsyncClient(timeout=30) as client:
            if raw_body:
                resp = await client.request(method, url, headers=headers, content=raw_body.encode())
            else:
                resp = await client.request(method, url, headers=headers, json=body)

            if resp.status_code >= 400:
                try:
                    err = resp.json()
                    msg = err.get("error", {}).get("message", str(err))
                except Exception:
                    msg = resp.text[:500]
                logger.warning(f"[Gmail] {method} {path} → {resp.status_code}: {msg}")
                return {"error": True, "status": resp.status_code, "message": msg}
            if resp.status_code == 204:
                return {"success": True}
            return resp.json()

    # ── Helpers ─────────────────────────────────────────────────────

    async def get_user_email(self) -> str:
        """Get authenticated user's email via Gmail getProfile."""
        result = await self._api("GET", "/profile")
        if result.get("error"):
            return ""
        return result.get("emailAddress", "")

    # ── Tool implementations ──────────────────────────────────────

    async def search(self, query: str, max_results: int = 20) -> str:
        """Поиск писем по Gmail query syntax."""
        result = await self._api("GET", f"/messages?q={query}&maxResults={max_results}")
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        messages = result.get("messages", [])
        items = []
        # Получаем детали для каждого сообщения (до 10 для скорости)
        for msg in messages[:10]:
            detail = await self._api("GET", f"/messages/{msg['id']}?format=metadata&metadataHeaders=Subject&metadataHeaders=From&metadataHeaders=Date")
            if detail.get("error"):
                continue
            headers_list = detail.get("payload", {}).get("headers", [])
            h = {h["name"]: h["value"] for h in headers_list}
            items.append({
                "id": msg["id"],
                "thread_id": msg.get("threadId", ""),
                "subject": h.get("Subject", ""),
                "from": h.get("From", ""),
                "date": h.get("Date", ""),
                "snippet": detail.get("snippet", "")[:200],
            })
        return json.dumps({"success": True, "total": result.get("resultSizeEstimate", 0), "count": len(items), "messages": items}, ensure_ascii=False)

    async def read_message(self, message_id: str) -> str:
        """Прочитать письмо."""
        result = await self._api("GET", f"/messages/{message_id}?format=full")
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        headers_list = result.get("payload", {}).get("headers", [])
        h = {hdr["name"]: hdr["value"] for hdr in headers_list}

        # Извлекаем текст
        body_text = ""
        payload = result.get("payload", {})
        if payload.get("body", {}).get("data"):
            body_text = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
        elif payload.get("parts"):
            for part in payload["parts"]:
                if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                    body_text = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
                    break

        return json.dumps({
            "success": True,
            "id": message_id,
            "subject": h.get("Subject", ""),
            "from": h.get("From", ""),
            "to": h.get("To", ""),
            "date": h.get("Date", ""),
            "body": body_text[:5000],
        }, ensure_ascii=False)

    async def send_email(self, to: str, subject: str, body: str, cc: str = "") -> str:
        """Отправить письмо."""
        msg = MIMEText(body, "plain", "utf-8")
        msg["to"] = to
        msg["subject"] = subject
        if cc:
            msg["cc"] = cc

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        result = await self._api("POST", "/messages/send", {"raw": raw})
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)
        return json.dumps({
            "success": True,
            "message_id": result.get("id", ""),
            "thread_id": result.get("threadId", ""),
            "to": to,
            "subject": subject,
        }, ensure_ascii=False)

    async def reply(self, message_id: str, body: str) -> str:
        """Ответить на письмо."""
        # Получаем оригинальное сообщение для thread_id и headers
        original = await self._api("GET", f"/messages/{message_id}?format=metadata&metadataHeaders=Subject&metadataHeaders=From&metadataHeaders=Message-ID")
        if original.get("error"):
            return json.dumps({"success": False, "error": original["message"]}, ensure_ascii=False)

        headers_list = original.get("payload", {}).get("headers", [])
        h = {hdr["name"]: hdr["value"] for hdr in headers_list}

        msg = MIMEText(body, "plain", "utf-8")
        msg["to"] = h.get("From", "")
        msg["subject"] = f"Re: {h.get('Subject', '')}"
        msg["In-Reply-To"] = h.get("Message-ID", "")
        msg["References"] = h.get("Message-ID", "")

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        result = await self._api("POST", "/messages/send", {
            "raw": raw,
            "threadId": original.get("threadId", ""),
        })
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)
        return json.dumps({
            "success": True,
            "message_id": result.get("id", ""),
            "reply_to": msg["to"],
            "subject": msg["subject"],
        }, ensure_ascii=False)

    async def create_draft(self, to: str, subject: str, body: str) -> str:
        """Создать черновик."""
        msg = MIMEText(body, "plain", "utf-8")
        msg["to"] = to
        msg["subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

        result = await self._api("POST", "/drafts", {"message": {"raw": raw}})
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)
        return json.dumps({
            "success": True,
            "draft_id": result.get("id", ""),
            "to": to,
            "subject": subject,
        }, ensure_ascii=False)

    # ── BaseIntegration interface ─────────────────────────────────

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "gmail_search",
                "description": "Search emails in Gmail. Supports Gmail query syntax: from:, to:, subject:, after:, before:, is:unread, etc.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Gmail search query (e.g. 'from:john@example.com after:2026/01/01 is:unread')"},
                        "max_results": {"type": "integer", "description": "Max results (default 20)"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "gmail_read",
                "description": "Read the full content of a specific email by message ID.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message_id": {"type": "string", "description": "Gmail message ID"},
                    },
                    "required": ["message_id"],
                },
            },
            {
                "name": "gmail_send",
                "description": "Send a new email.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "Recipient email address"},
                        "subject": {"type": "string", "description": "Email subject"},
                        "body": {"type": "string", "description": "Email body text"},
                        "cc": {"type": "string", "description": "CC email addresses, comma-separated (optional)"},
                    },
                    "required": ["to", "subject", "body"],
                },
            },
            {
                "name": "gmail_reply",
                "description": "Reply to an existing email by message ID.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message_id": {"type": "string", "description": "Original message ID to reply to"},
                        "body": {"type": "string", "description": "Reply text"},
                    },
                    "required": ["message_id", "body"],
                },
            },
            {
                "name": "gmail_draft",
                "description": "Create an email draft (not sent).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "Recipient email address"},
                        "subject": {"type": "string", "description": "Email subject"},
                        "body": {"type": "string", "description": "Email body text"},
                    },
                    "required": ["to", "subject", "body"],
                },
            },
        ]

    async def execute(self, tool_name: str, **kwargs) -> str:
        if not await self.load_credentials():
            return json.dumps({"error": "Gmail not configured. Add oauth_token in integrations."}, ensure_ascii=False)

        dispatch = {
            "gmail_search": lambda: self.search(
                query=str(kwargs.get("query", "")),
                max_results=int(kwargs.get("max_results", 20)),
            ),
            "gmail_read": lambda: self.read_message(message_id=str(kwargs.get("message_id", ""))),
            "gmail_send": lambda: self.send_email(
                to=str(kwargs.get("to", "")),
                subject=str(kwargs.get("subject", "")),
                body=str(kwargs.get("body", "")),
                cc=str(kwargs.get("cc", "")),
            ),
            "gmail_reply": lambda: self.reply(
                message_id=str(kwargs.get("message_id", "")),
                body=str(kwargs.get("body", "")),
            ),
            "gmail_draft": lambda: self.create_draft(
                to=str(kwargs.get("to", "")),
                subject=str(kwargs.get("subject", "")),
                body=str(kwargs.get("body", "")),
            ),
        }

        handler = dispatch.get(tool_name)
        if handler:
            return await handler()
        return json.dumps({"error": f"Unknown Gmail tool: {tool_name}"}, ensure_ascii=False)
