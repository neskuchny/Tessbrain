# -*- coding: utf-8 -*-
"""
Хранилище именованных DOCX-шаблонов «Документов по встрече» (Режим A).

Таблица meeting_doc_templates (миграция 092). Graceful-fallback на in-memory
на процесс, если таблицы нет / Supabase не настроен — работает и до миграции.
"""
import logging
from typing import Any, Dict, List, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

_TABLE = "/rest/v1/meeting_doc_templates"
# Общий на процесс fallback (не per-request) — как в analysis store.
_MEM: Dict[str, Dict[str, Any]] = {}


class StoreUnavailable(Exception):
    """Таблицы нет / Supabase не настроен → фолбэк на in-memory."""


def _client():
    from backend.db.supabase_client import SupabaseClient
    return SupabaseClient()


def _missing(exc: Exception) -> bool:
    s = str(exc).lower()
    return ("could not find the table" in s or "pgrst205" in s
            or "does not exist" in s or "42p01" in s or "not configured" in s)


async def save_template(user_id: str, *, name: str, kind: str,
                        docx_base64: str, placeholders: List[str]) -> Dict[str, Any]:
    tid = f"mdt_{uuid4().hex[:16]}"
    row = {"id": tid, "user_id": user_id, "name": (name or "Шаблон")[:120],
           "kind": kind or "custom", "docx_base64": docx_base64,
           "placeholders": placeholders or []}
    try:
        await _client()._request("POST", _TABLE, json_data=row,
                                 headers={"Prefer": "return=minimal"})
    except Exception as e:
        if _missing(e):
            _MEM[tid] = row
        else:
            raise
    return {"id": tid, "name": row["name"], "kind": row["kind"]}


async def list_templates(user_id: str) -> List[Dict[str, Any]]:
    try:
        rows = await _client()._request(
            "GET", _TABLE,
            params={"user_id": f"eq.{user_id}",
                    "select": "id,name,kind,placeholders,created_at",
                    "order": "created_at.desc"})
        return [{"id": r["id"], "name": r.get("name", ""), "kind": r.get("kind", "custom"),
                 "placeholders": r.get("placeholders", [])} for r in (rows or [])]
    except Exception as e:
        if _missing(e):
            return [{"id": t["id"], "name": t["name"], "kind": t["kind"],
                     "placeholders": t.get("placeholders", [])}
                    for t in _MEM.values() if t["user_id"] == user_id]
        raise


async def get_template(user_id: str, template_id: str) -> Optional[Dict[str, Any]]:
    """Полный шаблон (с docx_base64) владельца."""
    try:
        rows = await _client()._request(
            "GET", _TABLE,
            params={"id": f"eq.{template_id}", "user_id": f"eq.{user_id}",
                    "select": "*", "limit": "1"})
        return rows[0] if rows else None
    except Exception as e:
        if _missing(e):
            t = _MEM.get(template_id)
            return t if (t and t["user_id"] == user_id) else None
        raise


async def delete_template(user_id: str, template_id: str) -> bool:
    try:
        await _client()._request(
            "DELETE", _TABLE,
            params={"id": f"eq.{template_id}", "user_id": f"eq.{user_id}"})
        return True
    except Exception as e:
        if _missing(e):
            t = _MEM.get(template_id)
            if t and t["user_id"] == user_id:
                _MEM.pop(template_id, None)
                return True
            return False
        raise


# ── Заготовки контекста (reusable-блоки) ────────────────────────────────────
# Хранятся списком в одной строке user_integrations (без миграций):
# provider='document_snippet', meta.items=[{id, name, text}]. Личные.

_SNIP_PROVIDER = "document_snippet"
_SNIP_MEM: Dict[str, List[Dict[str, str]]] = {}


async def _snip_row(user_id: str):
    rows = await _client()._request(
        "GET", "/rest/v1/user_integrations",
        params={"user_id": f"eq.{user_id}", "provider": f"eq.{_SNIP_PROVIDER}",
                "select": "meta", "limit": "1"})
    return rows[0] if rows else None


async def list_snippets(user_id: str) -> List[Dict[str, str]]:
    try:
        row = await _snip_row(user_id)
        items = ((row or {}).get("meta") or {}).get("items") or []
        return [i for i in items if isinstance(i, dict) and i.get("id")]
    except Exception as e:
        if _missing(e):
            return list(_SNIP_MEM.get(user_id, []))
        raise


async def _write_snippets(user_id: str, items: List[Dict[str, str]]) -> None:
    sb = _client()
    await sb._request("DELETE", "/rest/v1/user_integrations",
                      params={"user_id": f"eq.{user_id}", "provider": f"eq.{_SNIP_PROVIDER}"})
    if items:
        await sb._request("POST", "/rest/v1/user_integrations",
                          json_data={"user_id": user_id, "provider": _SNIP_PROVIDER,
                                     "key_name": "items", "meta": {"items": items}, "secret_enc": ""})


async def add_snippet(user_id: str, name: str, text: str) -> Dict[str, str]:
    item = {"id": f"snp_{uuid4().hex[:12]}", "name": (name or "Блок")[:80], "text": text or ""}
    try:
        items = await list_snippets(user_id)
        items.append(item)
        await _write_snippets(user_id, items)
    except Exception as e:
        if _missing(e):
            _SNIP_MEM.setdefault(user_id, []).append(item)
        else:
            raise
    return item


async def delete_snippet(user_id: str, snippet_id: str) -> bool:
    try:
        items = await list_snippets(user_id)
        kept = [i for i in items if i.get("id") != snippet_id]
        if len(kept) == len(items):
            return False
        await _write_snippets(user_id, kept)
        return True
    except Exception as e:
        if _missing(e):
            arr = _SNIP_MEM.get(user_id, [])
            new = [i for i in arr if i.get("id") != snippet_id]
            _SNIP_MEM[user_id] = new
            return len(new) != len(arr)
        raise
