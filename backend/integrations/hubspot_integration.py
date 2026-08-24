# -*- coding: utf-8 -*-
"""
HubSpot CRM Integration — контакты, сделки, заметки.

Использует HubSpot API v3 через REST.
Аутентификация: Private App Token (Bearer).

Credentials (из user_integrations):
    - api_key: Private App Token (pat-...)
"""

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from backend.integrations.base import BaseIntegration
from backend.integrations.registry import register_integration

logger = logging.getLogger(__name__)

HUBSPOT_API = "https://api.hubapi.com"


@register_integration
class HubSpotIntegration(BaseIntegration):
    integration_id = "hubspot"
    integration_name = "HubSpot"
    category = "crm"
    keywords = [
        # Русский
        "hubspot", "хабспот", "crm", "контакт", "сделк",
        "клиент", "лид", "pipeline", "воронк",
        "найди контакт", "создай сделку", "заметка",
        "карточка клиента",
        # English
        "contact", "deal", "lead", "pipeline", "note",
        "hubspot crm", "find contact", "create deal",
        "customer", "sales", "opportunity",
        # Транслит
        "kontakt", "sdelka", "klient",
    ]

    def _headers(self) -> Dict[str, str]:
        token = self.get_key("api_key")
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def _api(self, method: str, path: str, body: Optional[Dict] = None,
                   params: Optional[Dict] = None) -> Dict[str, Any]:
        url = f"{HUBSPOT_API}{path}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(method, url, headers=self._headers(),
                                        json=body, params=params)
            if resp.status_code >= 400:
                try:
                    err = resp.json()
                    msg = err.get("message", str(err))
                except Exception:
                    msg = resp.text[:500]
                return {"error": True, "status": resp.status_code, "message": msg}
            if resp.status_code == 204:
                return {"success": True}
            return resp.json()

    # ── Tool implementations ──────────────────────────────────────

    async def search_contacts(self, query: str, max_results: int = 20) -> str:
        """Поиск контактов по имени/email."""
        result = await self._api("POST", "/crm/v3/objects/contacts/search", body={
            "filterGroups": [{
                "filters": [{
                    "propertyName": "email",
                    "operator": "CONTAINS_TOKEN",
                    "value": f"*{query}*",
                }]
            }] if "@" in query else [],
            "query": query,
            "limit": min(max_results, 100),
            "properties": ["email", "firstname", "lastname", "phone", "company",
                           "lifecyclestage", "createdate", "lastmodifieddate"],
        })
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        contacts = []
        for r in result.get("results", []):
            props = r.get("properties", {})
            contacts.append({
                "id": r.get("id", ""),
                "email": props.get("email", ""),
                "name": f"{props.get('firstname', '')} {props.get('lastname', '')}".strip(),
                "phone": props.get("phone", ""),
                "company": props.get("company", ""),
                "stage": props.get("lifecyclestage", ""),
                "created": props.get("createdate", ""),
            })

        return json.dumps({
            "success": True,
            "query": query,
            "count": len(contacts),
            "contacts": contacts,
        }, ensure_ascii=False)

    async def get_contact(self, contact_id: str) -> str:
        """Получить детали контакта."""
        result = await self._api("GET", f"/crm/v3/objects/contacts/{contact_id}", params={
            "properties": "email,firstname,lastname,phone,company,lifecyclestage,jobtitle,city,country,hs_lead_status,notes_last_updated",
        })
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        props = result.get("properties", {})
        return json.dumps({
            "success": True,
            "id": result.get("id", ""),
            "email": props.get("email", ""),
            "name": f"{props.get('firstname', '')} {props.get('lastname', '')}".strip(),
            "phone": props.get("phone", ""),
            "company": props.get("company", ""),
            "job_title": props.get("jobtitle", ""),
            "stage": props.get("lifecyclestage", ""),
            "lead_status": props.get("hs_lead_status", ""),
            "city": props.get("city", ""),
            "country": props.get("country", ""),
        }, ensure_ascii=False)

    async def search_deals(self, query: str, max_results: int = 20) -> str:
        """Поиск сделок."""
        result = await self._api("POST", "/crm/v3/objects/deals/search", body={
            "query": query,
            "limit": min(max_results, 100),
            "properties": ["dealname", "amount", "dealstage", "pipeline",
                           "closedate", "createdate", "hubspot_owner_id"],
        })
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        deals = []
        for r in result.get("results", []):
            props = r.get("properties", {})
            deals.append({
                "id": r.get("id", ""),
                "name": props.get("dealname", ""),
                "amount": props.get("amount", ""),
                "stage": props.get("dealstage", ""),
                "pipeline": props.get("pipeline", ""),
                "close_date": props.get("closedate", ""),
                "created": props.get("createdate", ""),
            })

        return json.dumps({
            "success": True,
            "query": query,
            "count": len(deals),
            "deals": deals,
        }, ensure_ascii=False)

    async def create_deal(self, name: str, amount: str = "", stage: str = "",
                          pipeline: str = "default") -> str:
        """Создать сделку."""
        properties: Dict[str, Any] = {"dealname": name, "pipeline": pipeline}
        if amount:
            properties["amount"] = amount
        if stage:
            properties["dealstage"] = stage

        result = await self._api("POST", "/crm/v3/objects/deals", body={"properties": properties})
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        return json.dumps({
            "success": True,
            "deal_id": result.get("id", ""),
            "name": name,
            "amount": amount,
        }, ensure_ascii=False)

    async def create_note(self, body_text: str, contact_id: str = "",
                          deal_id: str = "") -> str:
        """Создать заметку и привязать к контакту/сделке."""
        result = await self._api("POST", "/crm/v3/objects/notes", body={
            "properties": {
                "hs_note_body": body_text,
                "hs_timestamp": "",
            }
        })
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        note_id = result.get("id", "")

        # Привязываем к контакту
        if contact_id and note_id:
            await self._api("PUT",
                f"/crm/v3/objects/notes/{note_id}/associations/contacts/{contact_id}/202")

        # Привязываем к сделке
        if deal_id and note_id:
            await self._api("PUT",
                f"/crm/v3/objects/notes/{note_id}/associations/deals/{deal_id}/214")

        return json.dumps({
            "success": True,
            "note_id": note_id,
            "associated_contact": contact_id,
            "associated_deal": deal_id,
        }, ensure_ascii=False)

    async def list_deals(self, limit: int = 20) -> str:
        """Список последних сделок."""
        result = await self._api("GET", "/crm/v3/objects/deals", params={
            "limit": min(limit, 100),
            "properties": "dealname,amount,dealstage,pipeline,closedate,createdate",
            "sorts": "-createdate",
        })
        if result.get("error"):
            return json.dumps({"success": False, "error": result["message"]}, ensure_ascii=False)

        deals = []
        for r in result.get("results", []):
            props = r.get("properties", {})
            deals.append({
                "id": r.get("id", ""),
                "name": props.get("dealname", ""),
                "amount": props.get("amount", ""),
                "stage": props.get("dealstage", ""),
                "close_date": props.get("closedate", ""),
            })

        return json.dumps({
            "success": True,
            "count": len(deals),
            "deals": deals,
        }, ensure_ascii=False)

    # ── BaseIntegration interface ─────────────────────────────────

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "hubspot_search_contacts",
                "description": "Search HubSpot contacts by name or email. Use for: find customer, search contact, найди клиента, найди контакт.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Name or email to search"},
                        "max_results": {"type": "integer", "description": "Max results (default 20)"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "hubspot_get_contact",
                "description": "Get detailed contact info by ID. Use for: contact details, карточка клиента.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "contact_id": {"type": "string", "description": "HubSpot contact ID"},
                    },
                    "required": ["contact_id"],
                },
            },
            {
                "name": "hubspot_search_deals",
                "description": "Search HubSpot deals. Use for: find deal, search pipeline, найди сделку, воронка продаж.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Deal name or keywords"},
                        "max_results": {"type": "integer", "description": "Max results (default 20)"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "hubspot_create_deal",
                "description": "Create a new deal in HubSpot. Use for: create deal, новая сделка, добавь сделку.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Deal name"},
                        "amount": {"type": "string", "description": "Deal amount (optional)"},
                        "stage": {"type": "string", "description": "Deal stage ID (optional)"},
                        "pipeline": {"type": "string", "description": "Pipeline ID (default: 'default')"},
                    },
                    "required": ["name"],
                },
            },
            {
                "name": "hubspot_create_note",
                "description": "Create a note and associate with contact/deal. Use for: add note, create note, добавь заметку к клиенту.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "body_text": {"type": "string", "description": "Note text"},
                        "contact_id": {"type": "string", "description": "Contact ID to associate (optional)"},
                        "deal_id": {"type": "string", "description": "Deal ID to associate (optional)"},
                    },
                    "required": ["body_text"],
                },
            },
            {
                "name": "hubspot_list_deals",
                "description": "List recent deals from HubSpot. Use for: show deals, мои сделки, список сделок.",
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
            return json.dumps({"error": "HubSpot not configured. Add Private App Token in integrations."}, ensure_ascii=False)

        dispatch = {
            "hubspot_search_contacts": lambda: self.search_contacts(
                query=str(kwargs.get("query", "")),
                max_results=int(kwargs.get("max_results", 20)),
            ),
            "hubspot_get_contact": lambda: self.get_contact(str(kwargs.get("contact_id", ""))),
            "hubspot_search_deals": lambda: self.search_deals(
                query=str(kwargs.get("query", "")),
                max_results=int(kwargs.get("max_results", 20)),
            ),
            "hubspot_create_deal": lambda: self.create_deal(
                name=str(kwargs.get("name", "")),
                amount=str(kwargs.get("amount", "")),
                stage=str(kwargs.get("stage", "")),
                pipeline=str(kwargs.get("pipeline", "default")),
            ),
            "hubspot_create_note": lambda: self.create_note(
                body_text=str(kwargs.get("body_text", "")),
                contact_id=str(kwargs.get("contact_id", "")),
                deal_id=str(kwargs.get("deal_id", "")),
            ),
            "hubspot_list_deals": lambda: self.list_deals(
                limit=int(kwargs.get("limit", 20)),
            ),
        }

        handler = dispatch.get(tool_name)
        if handler:
            return await handler()
        return json.dumps({"error": f"Unknown HubSpot tool: {tool_name}"}, ensure_ascii=False)
