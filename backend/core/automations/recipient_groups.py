# -*- coding: utf-8 -*-
"""
Именованные группы получателей: «Отдел продаж» = список чатов/почт, чтобы
слать разом, а не перечислять адреса каждый раз.

Хранятся списком в user_integrations (provider='recipient_group',
meta.items=[{id, name, channel, recipients[]}]) — без миграций. Личные.
"""
import logging
from typing import Any, Dict, List, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

_PROVIDER = "recipient_group"
_MEM: Dict[str, List[Dict[str, Any]]] = {}


def _client():
    from backend.db.supabase_client import SupabaseClient
    return SupabaseClient()


def _missing(exc: Exception) -> bool:
    s = str(exc).lower()
    return "not configured" in s or "does not exist" in s or "pgrst" in s


async def _row(user_id: str):
    rows = await _client()._request(
        "GET", "/rest/v1/user_integrations",
        params={"user_id": f"eq.{user_id}", "provider": f"eq.{_PROVIDER}",
                "select": "meta", "limit": "1"})
    return rows[0] if rows else None


async def list_groups(user_id: str) -> List[Dict[str, Any]]:
    try:
        row = await _row(user_id)
        items = ((row or {}).get("meta") or {}).get("items") or []
        return [i for i in items if isinstance(i, dict) and i.get("id")]
    except Exception as e:
        if _missing(e):
            return list(_MEM.get(user_id, []))
        raise


async def _write(user_id: str, items: List[Dict[str, Any]]) -> None:
    sb = _client()
    await sb._request("DELETE", "/rest/v1/user_integrations",
                      params={"user_id": f"eq.{user_id}", "provider": f"eq.{_PROVIDER}"})
    if items:
        await sb._request("POST", "/rest/v1/user_integrations",
                          json_data={"user_id": user_id, "provider": _PROVIDER,
                                     "key_name": "items", "meta": {"items": items}, "secret_enc": ""})


async def add_group(user_id: str, name: str, channel: str,
                    recipients: List[str]) -> Dict[str, Any]:
    grp = {"id": f"grp_{uuid4().hex[:12]}", "name": (name or "Группа")[:80],
           "channel": (channel or "telegram"),
           "recipients": [str(r).strip() for r in (recipients or []) if str(r).strip()]}
    try:
        items = await list_groups(user_id)
        items.append(grp)
        await _write(user_id, items)
    except Exception as e:
        if _missing(e):
            _MEM.setdefault(user_id, []).append(grp)
        else:
            raise
    return grp


async def delete_group(user_id: str, group_id: str) -> bool:
    try:
        items = await list_groups(user_id)
        kept = [i for i in items if i.get("id") != group_id]
        if len(kept) == len(items):
            return False
        await _write(user_id, kept)
        return True
    except Exception as e:
        if _missing(e):
            arr = _MEM.get(user_id, [])
            new = [i for i in arr if i.get("id") != group_id]
            _MEM[user_id] = new
            return len(new) != len(arr)
        raise


async def resolve_group(user_id: str, group_id: str) -> Optional[Dict[str, Any]]:
    """Группа по id владельца → {channel, recipients} или None."""
    for g in await list_groups(user_id):
        if g.get("id") == group_id:
            return g
    return None
