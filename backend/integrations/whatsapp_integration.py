# -*- coding: utf-8 -*-
"""
WhatsApp Business Integration — отправка и чтение сообщений.

Использует WhatsApp Business Cloud API (Meta Graph API v18.0).
Аутентификация: System User Token + Phone Number ID.

Credentials (из user_integrations):
    - access_token: Meta system user / business token
    - phone_number_id: WhatsApp phone number ID
"""

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from backend.integrations.base import BaseIntegration
from backend.integrations.registry import register_integration

logger = logging.getLogger(__name__)

GRAPH_API = "https://graph.facebook.com/v18.0"


@register_integration
class WhatsAppIntegration(BaseIntegration):
    integration_id = "whatsapp"
    integration_name = "WhatsApp"
    category = "messaging"
    keywords = [
        # Русский
        "whatsapp", "ватсап", "вотсап", "вацап",
        "отправь в whatsapp", "напиши в ватсап",
        "сообщение whatsapp", "ватсап сообщение",
        # English
        "send whatsapp", "whatsapp message",
        "wa message", "text on whatsapp",
        # Транслит
        "vatsap", "votsap",
    ]

    def _headers(self) -> Dict[str, str]:
        token = self.get_key("access_token")
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _phone_number_id(self) -> str:
        return self.get_key("phone_number_id")

    async def _api(self, method: str, path: str, body: Optional[Dict] = None) -> Dict[str, Any]:
        url = f"{GRAPH_API}{path}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(method, url, headers=self._headers(), json=body)
            if resp.status_code >= 400:
                try:
                    err = resp.json()
                    msg = err.get("error", {}).get("message", str(err))
                except Exception:
                    msg = resp.text[:500]
                return {"error": True, "status": resp.status_code, "message": msg}
            return resp.json()

    # ── Tool implementations ──────────────────────────────────────

    async def send_message(self, to: str, text: str) -> str:
        """Отправить текстовое сообщение."""
        phone_id = self._phone_number_id()
        if not phone_id:
            return json.dumps({"success": False, "error": "WhatsApp phone_number_id not configured"}, ensure_ascii=False)

        # Нормализуем номер телефона
        to_clean = to.replace("+", "").replace(" ", "").replace("-", "").replace("(", "").replace(")", "")

        result = await self._api("POST", f"/{phone_id}/messages", body={
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_clean,
            "type": "text",
            "text": {"body": text},
        })
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        msg_id = ""
        messages = result.get("messages", [])
        if messages:
            msg_id = messages[0].get("id", "")

        return json.dumps({
            "success": True,
            "message_id": msg_id,
            "to": to,
            "text_preview": text[:100],
        }, ensure_ascii=False)

    async def send_template(self, to: str, template_name: str,
                            language: str = "en", parameters: List[str] | None = None) -> str:
        """Отправить шаблонное сообщение (для первого контакта)."""
        phone_id = self._phone_number_id()
        if not phone_id:
            return json.dumps({"success": False, "error": "WhatsApp phone_number_id not configured"}, ensure_ascii=False)

        to_clean = to.replace("+", "").replace(" ", "").replace("-", "")

        body_component: Dict[str, Any] = {"type": "body"}
        if parameters:
            body_component["parameters"] = [
                {"type": "text", "text": p} for p in parameters
            ]

        result = await self._api("POST", f"/{phone_id}/messages", body={
            "messaging_product": "whatsapp",
            "to": to_clean,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language},
                "components": [body_component] if parameters else [],
            },
        })
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        return json.dumps({
            "success": True,
            "to": to,
            "template": template_name,
        }, ensure_ascii=False)

    async def get_business_profile(self) -> str:
        """Получить бизнес-профиль WhatsApp."""
        phone_id = self._phone_number_id()
        result = await self._api("GET", f"/{phone_id}/whatsapp_business_profile?fields=about,address,description,email,websites,profile_picture_url")
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        data = result.get("data", [{}])
        profile = data[0] if data else {}
        return json.dumps({
            "success": True,
            "about": profile.get("about", ""),
            "description": profile.get("description", ""),
            "email": profile.get("email", ""),
            "websites": profile.get("websites", []),
        }, ensure_ascii=False)

    # ── BaseIntegration interface ─────────────────────────────────

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "whatsapp_send",
                "description": "Send a WhatsApp message. Use for: send whatsapp, отправь в ватсап, напиши в whatsapp.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "Phone number with country code (e.g. +79161234567)"},
                        "text": {"type": "string", "description": "Message text"},
                    },
                    "required": ["to", "text"],
                },
            },
            {
                "name": "whatsapp_send_template",
                "description": "Send a WhatsApp template message (for first contact/marketing). Use for: send template, шаблонное сообщение.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "Phone number with country code"},
                        "template_name": {"type": "string", "description": "Approved template name"},
                        "language": {"type": "string", "description": "Language code (default 'en')"},
                        "parameters": {"type": "array", "items": {"type": "string"},
                                       "description": "Template body parameters"},
                    },
                    "required": ["to", "template_name"],
                },
            },
            {
                "name": "whatsapp_profile",
                "description": "Get WhatsApp Business profile info. Use for: whatsapp profile, профиль whatsapp.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
        ]

    async def execute(self, tool_name: str, **kwargs) -> str:
        if not await self.load_credentials():
            return json.dumps({"error": "WhatsApp not configured. Add access_token and phone_number_id in integrations."}, ensure_ascii=False)

        dispatch = {
            "whatsapp_send": lambda: self.send_message(
                to=str(kwargs.get("to", "")),
                text=str(kwargs.get("text", "")),
            ),
            "whatsapp_send_template": lambda: self.send_template(
                to=str(kwargs.get("to", "")),
                template_name=str(kwargs.get("template_name", "")),
                language=str(kwargs.get("language", "en")),
                parameters=kwargs.get("parameters", []),
            ),
            "whatsapp_profile": lambda: self.get_business_profile(),
        }

        handler = dispatch.get(tool_name)
        if handler:
            return await handler()
        return json.dumps({"error": f"Unknown WhatsApp tool: {tool_name}"}, ensure_ascii=False)
