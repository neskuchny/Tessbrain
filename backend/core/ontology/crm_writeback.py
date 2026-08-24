# -*- coding: utf-8 -*-
"""
Запись обратно в CRM (write-back) — отдельный, изолированный от read-коннекторов
путь. «Создать сделку/лид» и «Добавить комментарий (история в CRM обновлена)».

БЕЗОПАСНОСТЬ (deny-by-default):
- Весь путь под env-гейтом ENABLE_CRM_WRITEBACK. По умолчанию ВЫКЛЮЧЕН — узел
  доски возвращает понятную ошибку, ни одного внешнего вызова не происходит.
- Учётные данные — только из env (те же, что у read-коннекторов), никогда в
  коде/данных/доске.
- Реальные документированные endpoints провайдеров; никаких моков. HTTP-запрос
  инжектируется (любой callable вместо httpx) — так тестируется форма payload
  без обращения к боевому CRM.
- Модуль НЕ трогает crm_connectors.py: read-путь остаётся неизменным.

Нормализованные операции (единый интерфейс для всех провайдеров):
- create: создать первичную запись (лид у AmoCRM, сделка у остальных).
          fields: {"name": str, "value": float|None}
- note:   добавить заметку/комментарий к записи (обновить историю).
          fields: {"entity_id": str, "text": str}
"""
from __future__ import annotations

import logging
import os
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

PostFn = Callable[..., Awaitable[Any]]

_WRITERS: Dict[str, "CrmWriter"] = {}


def writeback_enabled() -> bool:
    """Глобальный гейт. Пусто/0/false/no → выключено (безопасный дефолт)."""
    return str(os.environ.get("ENABLE_CRM_WRITEBACK", "")).strip().lower() \
        in ("1", "true", "yes", "on")


async def _default_post(url: str, *, headers: Optional[dict] = None,
                        json: Any = None, params: Optional[dict] = None) -> Any:
    import httpx
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
        r = await c.post(url, headers=headers or {}, json=json, params=params or {})
        r.raise_for_status()
        return r.json()


class CrmWriter:
    """Базовый writer: знает endpoints записи одного провайдера."""

    provider: str = ""
    ops: tuple = ("create", "note")

    def env_check(self) -> Optional[str]:
        return None

    async def write(self, op: str, fields: Dict[str, Any],
                    post_fn: Optional[PostFn] = None) -> Dict[str, Any]:
        """Выполнить операцию. Возвращает {"id":..., "url":...}. Реализуют наследники."""
        raise NotImplementedError


def register(writer: "CrmWriter") -> "CrmWriter":
    _WRITERS[writer.provider] = writer
    return writer


def get_writer(provider: str) -> Optional["CrmWriter"]:
    return _WRITERS.get((provider or "").lower())


def list_writers() -> list[dict]:
    return [{"provider": w.provider, "ops": list(w.ops),
             "ready": w.env_check() is None, "env_hint": w.env_check()}
            for w in _WRITERS.values()]


def _num(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


# ──────────────────────────────────────────────────────────────────────
# AmoCRM: лид POST /api/v4/leads (тело — массив); заметка
# POST /api/v4/leads/{id}/notes. Bearer OAuth-токен.
# ──────────────────────────────────────────────────────────────────────
class AmoCRMWriter(CrmWriter):
    provider = "amocrm"

    def env_check(self) -> Optional[str]:
        if not os.environ.get("AMOCRM_SUBDOMAIN"):
            return "AMOCRM_SUBDOMAIN не задан"
        if not os.environ.get("AMOCRM_TOKEN"):
            return "AMOCRM_TOKEN не задан"
        return None

    async def write(self, op, fields, post_fn=None):
        if (err := self.env_check()):
            raise ValueError(err)
        post_fn = post_fn or _default_post
        sub = os.environ["AMOCRM_SUBDOMAIN"]
        headers = {"Authorization": f"Bearer {os.environ['AMOCRM_TOKEN']}"}
        base = f"https://{sub}.amocrm.ru/api/v4"
        if op == "create":
            name = str(fields.get("name") or "").strip()
            if not name:
                raise ValueError("amocrm create: пустое имя лида")
            body: Dict[str, Any] = {"name": name}
            price = _num(fields.get("value"))
            if price is not None:
                body["price"] = int(price)
            data = await post_fn(f"{base}/leads", headers=headers, json=[body])
            item = ((data or {}).get("_embedded") or {}).get("leads") or [{}]
            lid = item[0].get("id")
            return {"id": lid, "url": f"https://{sub}.amocrm.ru/leads/detail/{lid}" if lid else ""}
        if op == "note":
            eid = str(fields.get("entity_id") or "").strip()
            text = str(fields.get("text") or "").strip()
            if not eid or not text:
                raise ValueError("amocrm note: нужны entity_id и текст")
            body = [{"note_type": "common", "params": {"text": text[:2000]}}]
            await post_fn(f"{base}/leads/{eid}/notes", headers=headers, json=body)
            return {"id": eid, "url": f"https://{sub}.amocrm.ru/leads/detail/{eid}"}
        raise ValueError(f"amocrm: неизвестная операция {op}")


# ──────────────────────────────────────────────────────────────────────
# Bitrix24: сделка crm.deal.add; комментарий crm.timeline.comment.add.
# Через webhook-URL (в нём уже авторизация).
# ──────────────────────────────────────────────────────────────────────
class Bitrix24Writer(CrmWriter):
    provider = "bitrix24"

    def env_check(self) -> Optional[str]:
        if not os.environ.get("BITRIX_WEBHOOK_URL"):
            return "BITRIX_WEBHOOK_URL не задан"
        return None

    async def write(self, op, fields, post_fn=None):
        if (err := self.env_check()):
            raise ValueError(err)
        post_fn = post_fn or _default_post
        base = os.environ["BITRIX_WEBHOOK_URL"].rstrip("/")
        if op == "create":
            name = str(fields.get("name") or "").strip()
            if not name:
                raise ValueError("bitrix24 create: пустой заголовок сделки")
            f: Dict[str, Any] = {"TITLE": name}
            val = _num(fields.get("value"))
            if val is not None:
                f["OPPORTUNITY"] = val
            data = await post_fn(f"{base}/crm.deal.add", json={"fields": f})
            did = (data or {}).get("result")
            return {"id": did, "url": ""}
        if op == "note":
            eid = str(fields.get("entity_id") or "").strip()
            text = str(fields.get("text") or "").strip()
            if not eid or not text:
                raise ValueError("bitrix24 note: нужны entity_id и текст")
            payload = {"fields": {"ENTITY_ID": eid, "ENTITY_TYPE": "deal",
                                  "COMMENT": text[:2000]}}
            await post_fn(f"{base}/crm.timeline.comment.add", json=payload)
            return {"id": eid, "url": ""}
        raise ValueError(f"bitrix24: неизвестная операция {op}")


# ──────────────────────────────────────────────────────────────────────
# HubSpot: сделка POST /crm/v3/objects/deals. Bearer private-app token.
# Заметки требуют associations — оставляем create (сделку), note — не
# поддерживаем безопасно (чтобы не создать «висящую» заметку).
# ──────────────────────────────────────────────────────────────────────
class HubSpotWriter(CrmWriter):
    provider = "hubspot"
    ops = ("create",)

    def env_check(self) -> Optional[str]:
        if not os.environ.get("HUBSPOT_TOKEN"):
            return "HUBSPOT_TOKEN не задан"
        return None

    async def write(self, op, fields, post_fn=None):
        if (err := self.env_check()):
            raise ValueError(err)
        if op != "create":
            raise ValueError("hubspot: поддерживается только создание сделки")
        post_fn = post_fn or _default_post
        headers = {"Authorization": f"Bearer {os.environ['HUBSPOT_TOKEN']}"}
        name = str(fields.get("name") or "").strip()
        if not name:
            raise ValueError("hubspot create: пустое имя сделки")
        props: Dict[str, Any] = {"dealname": name}
        val = _num(fields.get("value"))
        if val is not None:
            props["amount"] = val
        data = await post_fn("https://api.hubapi.com/crm/v3/objects/deals",
                             headers=headers, json={"properties": props})
        did = (data or {}).get("id")
        return {"id": did, "url": ""}


# ──────────────────────────────────────────────────────────────────────
# Pipedrive: сделка POST /api/v1/deals; заметка POST /api/v1/notes.
# api_token — query-параметр.
# ──────────────────────────────────────────────────────────────────────
class PipedriveWriter(CrmWriter):
    provider = "pipedrive"

    def env_check(self) -> Optional[str]:
        if not os.environ.get("PIPEDRIVE_DOMAIN"):
            return "PIPEDRIVE_DOMAIN не задан"
        if not os.environ.get("PIPEDRIVE_TOKEN"):
            return "PIPEDRIVE_TOKEN не задан"
        return None

    async def write(self, op, fields, post_fn=None):
        if (err := self.env_check()):
            raise ValueError(err)
        post_fn = post_fn or _default_post
        dom = os.environ["PIPEDRIVE_DOMAIN"]
        token = os.environ["PIPEDRIVE_TOKEN"]
        base = f"https://{dom}.pipedrive.com/api/v1"
        if op == "create":
            name = str(fields.get("name") or "").strip()
            if not name:
                raise ValueError("pipedrive create: пустой заголовок сделки")
            body: Dict[str, Any] = {"title": name}
            val = _num(fields.get("value"))
            if val is not None:
                body["value"] = val
            data = await post_fn(f"{base}/deals", params={"api_token": token}, json=body)
            did = ((data or {}).get("data") or {}).get("id")
            return {"id": did, "url": f"https://{dom}.pipedrive.com/deal/{did}" if did else ""}
        if op == "note":
            eid = str(fields.get("entity_id") or "").strip()
            text = str(fields.get("text") or "").strip()
            if not eid or not text:
                raise ValueError("pipedrive note: нужны entity_id и текст")
            body = {"content": text[:2000], "deal_id": int(eid) if eid.isdigit() else eid}
            await post_fn(f"{base}/notes", params={"api_token": token}, json=body)
            return {"id": eid, "url": f"https://{dom}.pipedrive.com/deal/{eid}"}
        raise ValueError(f"pipedrive: неизвестная операция {op}")


register(AmoCRMWriter())
register(Bitrix24Writer())
register(HubSpotWriter())
register(PipedriveWriter())
