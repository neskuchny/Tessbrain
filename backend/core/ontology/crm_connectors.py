# -*- coding: utf-8 -*-
"""
Нативные CRM-коннекторы для онтологии данных (docs/ru/DATA_ONTOLOGY.md).

«Подключил CRM → таблицы становятся датасетами»: вместо ручной выгрузки
по URL — типизированные коннекторы AmoCRM/Bitrix24/HubSpot/Pipedrive
с настроенной авторизацией и пагинацией. Каждый коннектор:
1) знает свои endpoints и поля;
2) возвращает таблицу (columns + rows) той же формы, что parse_tabular;
3) живёт в source.kind="crm" с провайдером и параметрами выгрузки;
4) обновляется через refresh — единая ручка, тот же сервис.

Учётные данные — в env (никогда не в коде/файлах данных). Тестируется
HTTP-инжекцией (любой callable вместо httpx.AsyncClient.get).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Стандартный тип fetch-функции: (url, headers, params) → JSON
FetchFn = Callable[..., Awaitable[Any]]

# Реестр коннекторов: provider → (fetch_fn, default_kind)
_PROVIDERS: Dict[str, "CrmConnector"] = {}


class CrmConnector:
    """Базовый коннектор: знает endpoints/поля и умеет тянуть «таблицу»
    (сделки/контакты/задачи) одной командой."""

    provider: str = ""
    kinds: Tuple[str, ...] = ()                # «деки» — что можно тянуть

    def env_check(self) -> Optional[str]:
        """Какие env обязательны. Возвращает текст ошибки или None."""
        return None

    async def fetch(self, kind: str, fetch_fn: Optional[FetchFn] = None,
                    *, limit: int = 200) -> Tuple[List[str], List[dict]]:
        """Сходить в API и вернуть (columns, rows). Реализуют наследники."""
        raise NotImplementedError

    # ── пер-пользовательские ключи (карточка «Интеграций») ─────────────
    # Дефолт делегирует в env-версии — существующие коннекторы работают
    # байт-в-байт как раньше. Коннектор, умеющий ключи из карточки юзера
    # (Roistat), переопределяет обе *_for_user-версии.
    async def env_check_for_user(self, user_id: Optional[str]) -> Optional[str]:
        return self.env_check()

    async def fetch_for_user(self, user_id: Optional[str], kind: str,
                             fetch_fn: Optional[FetchFn] = None,
                             *, limit: int = 200,
                             ) -> Tuple[List[str], List[dict]]:
        return await self.fetch(kind, fetch_fn, limit=limit)


def register(connector: "CrmConnector") -> "CrmConnector":
    _PROVIDERS[connector.provider] = connector
    return connector


async def user_integration_keys(user_id: Optional[str], provider: str) -> Dict[str, str]:
    """Ключи провайдера из карточки «Интеграций» пользователя ({} если нет).
    Общий помощник для *_for_user-версий коннекторов. Never-raise."""
    if not user_id:
        return {}
    try:
        from backend.core.integrations.user_keys_service import get_keys_service
        return await get_keys_service().get_keys(user_id, provider) or {}
    except Exception:
        logging.getLogger(__name__).debug(
            "%s: user keys lookup failed", provider, exc_info=True)
        return {}


def list_providers() -> List[dict]:
    out = []
    for name, c in _PROVIDERS.items():
        out.append({"provider": name, "kinds": list(c.kinds),
                    "ready": c.env_check() is None,
                    "env_hint": c.env_check()})
    return out


def get_connector(provider: str) -> Optional[CrmConnector]:
    return _PROVIDERS.get((provider or "").lower())


# ──────────────────────────────────────────────────────────────────────
# httpx-обёртка по умолчанию (легко подменяется в тестах)
# ──────────────────────────────────────────────────────────────────────

async def _default_fetch(url: str, *, headers: Optional[dict] = None,
                         params: Optional[dict] = None) -> Any:
    import httpx
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
        r = await c.get(url, headers=headers or {}, params=params or {})
        r.raise_for_status()
        return r.json()


# ──────────────────────────────────────────────────────────────────────
# Коннекторы
# ──────────────────────────────────────────────────────────────────────

class AmoCRMConnector(CrmConnector):
    """AmoCRM: leads/contacts/companies/tasks (Bearer OAuth-токен).

    Env: AMOCRM_SUBDOMAIN (`mycompany`), AMOCRM_TOKEN."""

    provider = "amocrm"
    kinds = ("leads", "contacts", "companies", "tasks")

    _FIELDS = {
        "leads": ["id", "name", "price", "status_id", "responsible_user_id",
                  "created_at", "updated_at", "pipeline_id"],
        "contacts": ["id", "name", "responsible_user_id",
                     "created_at", "updated_at"],
        "companies": ["id", "name", "responsible_user_id",
                      "created_at", "updated_at"],
        "tasks": ["id", "text", "complete_till", "is_completed",
                  "responsible_user_id", "entity_type", "entity_id"],
    }

    def env_check(self) -> Optional[str]:
        if not os.environ.get("AMOCRM_SUBDOMAIN"):
            return "AMOCRM_SUBDOMAIN не задан"
        if not os.environ.get("AMOCRM_TOKEN"):
            return "AMOCRM_TOKEN не задан"
        return None

    async def fetch(self, kind: str, fetch_fn=None, *, limit: int = 200):
        if kind not in self.kinds:
            raise ValueError(f"amocrm: kind должен быть из {self.kinds}")
        if (err := self.env_check()):
            raise ValueError(err)
        fetch_fn = fetch_fn or _default_fetch
        sub = os.environ["AMOCRM_SUBDOMAIN"]
        url = f"https://{sub}.amocrm.ru/api/v4/{kind}"
        headers = {"Authorization": f"Bearer {os.environ['AMOCRM_TOKEN']}"}
        rows: List[dict] = []
        page, per = 1, min(250, limit)
        while len(rows) < limit:
            data = await fetch_fn(url, headers=headers,
                                  params={"page": page, "limit": per})
            items = (data.get("_embedded") or {}).get(kind) or []
            if not items:
                break
            for it in items:
                # цена в копейках у leads — переводим в рубли (число —
                # читается профилем как number, профиль увидит масштаб)
                if kind == "leads" and "price" in it and it["price"]:
                    try:
                        it["price"] = float(it["price"])
                    except (TypeError, ValueError):
                        pass
                rows.append(it)
            if len(items) < per:
                break
            page += 1
        rows = rows[:limit]
        return self._FIELDS[kind], [
            {f: r.get(f) for f in self._FIELDS[kind]} for r in rows]


class Bitrix24Connector(CrmConnector):
    """Bitrix24: deals/contacts/companies/tasks через webhook-URL.

    Env: BITRIX_WEBHOOK_URL (полный путь вида
    `https://X.bitrix24.ru/rest/123/abc/`)."""

    provider = "bitrix24"
    kinds = ("deals", "contacts", "companies", "tasks")

    _METHODS = {
        "deals": "crm.deal.list",
        "contacts": "crm.contact.list",
        "companies": "crm.company.list",
        "tasks": "tasks.task.list",
    }
    _FIELDS = {
        "deals": ["ID", "TITLE", "OPPORTUNITY", "STAGE_ID", "ASSIGNED_BY_ID",
                  "DATE_CREATE", "CLOSEDATE"],
        "contacts": ["ID", "NAME", "LAST_NAME", "COMPANY_ID", "DATE_CREATE"],
        "companies": ["ID", "TITLE", "REVENUE", "ASSIGNED_BY_ID", "DATE_CREATE"],
        "tasks": ["id", "title", "status", "responsibleId", "deadline"],
    }

    def env_check(self) -> Optional[str]:
        if not os.environ.get("BITRIX_WEBHOOK_URL"):
            return "BITRIX_WEBHOOK_URL не задан"
        return None

    async def fetch(self, kind: str, fetch_fn=None, *, limit: int = 200):
        if kind not in self.kinds:
            raise ValueError(f"bitrix24: kind должен быть из {self.kinds}")
        if (err := self.env_check()):
            raise ValueError(err)
        fetch_fn = fetch_fn or _default_fetch
        base = os.environ["BITRIX_WEBHOOK_URL"].rstrip("/")
        url = f"{base}/{self._METHODS[kind]}"
        rows: List[dict] = []
        start = 0
        while len(rows) < limit:
            data = await fetch_fn(url, params={"start": start})
            items = data.get("result") or []
            if isinstance(items, dict) and "tasks" in items:
                items = items["tasks"]
            if not items:
                break
            rows.extend(items)
            if data.get("next") is None or len(items) < 50:
                break
            start = data["next"]
        rows = rows[:limit]
        return self._FIELDS[kind], [
            {f: r.get(f) for f in self._FIELDS[kind]} for r in rows]


class HubSpotConnector(CrmConnector):
    """HubSpot CRM: deals/contacts/companies/tasks.

    Env: HUBSPOT_TOKEN (private app access token)."""

    provider = "hubspot"
    kinds = ("deals", "contacts", "companies", "tasks")

    _CLEAN = {
        "deals": ["id", "dealname", "amount", "dealstage", "hubspot_owner_id",
                  "createdate", "closedate"],
        "contacts": ["id", "firstname", "lastname", "email", "company",
                     "hubspot_owner_id", "createdate"],
        "companies": ["id", "name", "domain", "annualrevenue",
                      "hubspot_owner_id", "createdate"],
        "tasks": ["id", "hs_task_subject", "hs_task_status",
                  "hubspot_owner_id", "hs_timestamp"],
    }

    def env_check(self) -> Optional[str]:
        if not os.environ.get("HUBSPOT_TOKEN"):
            return "HUBSPOT_TOKEN не задан"
        return None

    async def fetch(self, kind: str, fetch_fn=None, *, limit: int = 200):
        if kind not in self.kinds:
            raise ValueError(f"hubspot: kind должен быть из {self.kinds}")
        if (err := self.env_check()):
            raise ValueError(err)
        fetch_fn = fetch_fn or _default_fetch
        url = f"https://api.hubapi.com/crm/v3/objects/{kind}"
        headers = {"Authorization": f"Bearer {os.environ['HUBSPOT_TOKEN']}"}
        props = ",".join(c for c in self._CLEAN[kind] if c != "id")
        rows: List[dict] = []
        after = None
        while len(rows) < limit:
            params = {"limit": min(100, limit - len(rows)),
                      "properties": props}
            if after:
                params["after"] = after
            data = await fetch_fn(url, headers=headers, params=params)
            for it in data.get("results") or []:
                row = {"id": it.get("id"), **(it.get("properties") or {})}
                rows.append(row)
            after = ((data.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                break
        rows = rows[:limit]
        return self._CLEAN[kind], [
            {f: r.get(f) for f in self._CLEAN[kind]} for r in rows]


class PipedriveConnector(CrmConnector):
    """Pipedrive: deals/persons/organizations/activities.

    Env: PIPEDRIVE_DOMAIN (`mycompany`), PIPEDRIVE_TOKEN (api token)."""

    provider = "pipedrive"
    kinds = ("deals", "persons", "organizations", "activities")

    _FIELDS = {
        "deals": ["id", "title", "value", "currency", "status", "stage_id",
                  "user_id", "add_time", "close_time"],
        "persons": ["id", "name", "email", "phone", "org_id", "owner_id",
                    "add_time"],
        "organizations": ["id", "name", "owner_id", "add_time"],
        "activities": ["id", "subject", "type", "due_date", "done",
                       "user_id"],
    }

    def env_check(self) -> Optional[str]:
        if not os.environ.get("PIPEDRIVE_DOMAIN"):
            return "PIPEDRIVE_DOMAIN не задан"
        if not os.environ.get("PIPEDRIVE_TOKEN"):
            return "PIPEDRIVE_TOKEN не задан"
        return None

    async def fetch(self, kind: str, fetch_fn=None, *, limit: int = 200):
        if kind not in self.kinds:
            raise ValueError(f"pipedrive: kind должен быть из {self.kinds}")
        if (err := self.env_check()):
            raise ValueError(err)
        fetch_fn = fetch_fn or _default_fetch
        dom = os.environ["PIPEDRIVE_DOMAIN"]
        token = os.environ["PIPEDRIVE_TOKEN"]
        url = f"https://{dom}.pipedrive.com/api/v1/{kind}"
        rows: List[dict] = []
        start = 0
        while len(rows) < limit:
            params = {"api_token": token, "start": start,
                      "limit": min(100, limit - len(rows))}
            data = await fetch_fn(url, params=params)
            items = data.get("data") or []
            if not items:
                break
            rows.extend(items)
            more = ((data.get("additional_data") or {}).get("pagination")
                    or {}).get("more_items_in_collection")
            if not more:
                break
            start += len(items)
        rows = rows[:limit]
        return self._FIELDS[kind], [
            {f: r.get(f) for f in self._FIELDS[kind]} for r in rows]


class CallInsightConnector(CrmConnector):
    """CallInsight Pro (звонки/чаты КЦ) — docs/CALLINSIGHT_INTEGRATION.md
    мост 3: их bulk-API становится датасетом онтологии, и «мини-палантир»
    считает кодом конверсию по сегментам, средний чек по источнику,
    оценки по менеджерам — без копирования транскриптов.

    Env: CALLINSIGHT_API_URL, CALLINSIGHT_API_KEY (их mfk_*-ключ,
    scope=read; те же env, что у live-карточки клиента)."""

    provider = "callinsight"
    kinds = ("customers", "events", "services")

    # v2 (2026-06-14): добавлена аналитика по услугам (объём/конверсия/
    # средний чек/возражения) — отдельный путь, не /api/sync/.
    _PATHS = {
        "customers": "/api/sync/customers",
        "events": "/api/sync/events",
        "services": "/api/analytics/services",
    }

    _FIELDS = {
        # агрегат по клиенту (их /api/sync/customers)
        "customers": ["phone", "name_guess", "stage_guess", "total_calls",
                      "total_chats", "avg_score", "last_score",
                      "deal_value_total", "at_risk", "first_seen",
                      "last_seen"],
        # поток событий (их /api/sync/events)
        "events": ["ts", "type", "customer_phone", "agent", "score",
                   "summary"],
        # services: схема у источника не зафиксирована — колонки берём
        # динамически из ответа (union ключей), как parse_tabular.
    }

    def env_check(self) -> Optional[str]:
        if not os.environ.get("CALLINSIGHT_API_URL"):
            return ("CallInsight: нет адреса API — карточка «Интеграции → "
                    "CallInsight» (api_url + api_key) или CALLINSIGHT_API_URL")
        if not os.environ.get("CALLINSIGHT_API_KEY"):
            return "CallInsight: не задан api_key (карточка или CALLINSIGHT_API_KEY)"
        return None

    # ── ключи из карточки «Интеграций» пользователя, фолбэк — env ──────
    @staticmethod
    async def _user_creds(user_id: Optional[str]) -> Tuple[str, str]:
        keys = await user_integration_keys(user_id, "callinsight")
        return (str(keys.get("api_url") or "").strip().rstrip("/"),
                str(keys.get("api_key") or "").strip())

    async def env_check_for_user(self, user_id: Optional[str]) -> Optional[str]:
        base, key = await self._user_creds(user_id)
        if base and key:
            return None
        return self.env_check()

    async def fetch_for_user(self, user_id: Optional[str], kind: str,
                             fetch_fn=None, *, limit: int = 200):
        base, key = await self._user_creds(user_id)
        if not (base and key):
            base = os.environ.get("CALLINSIGHT_API_URL", "").rstrip("/")
            key = os.environ.get("CALLINSIGHT_API_KEY", "")
        return await self._fetch_with(base, key, kind, fetch_fn, limit=limit)

    async def fetch(self, kind: str, fetch_fn=None, *, limit: int = 200):
        return await self._fetch_with(
            os.environ.get("CALLINSIGHT_API_URL", "").rstrip("/"),
            os.environ.get("CALLINSIGHT_API_KEY", ""),
            kind, fetch_fn, limit=limit)

    async def _fetch_with(self, base: str, key: str, kind: str,
                          fetch_fn=None, *, limit: int = 200):
        if kind not in self.kinds:
            raise ValueError(f"callinsight: kind должен быть из {self.kinds}")
        if not (base and key):
            raise ValueError(self.env_check() or "CallInsight: ключи не заданы")
        fetch_fn = fetch_fn or _default_fetch
        headers = {"X-API-Key": key}
        # since нужен только bulk-синку (customers/events); аналитике — нет
        params = ({"since": "1970-01-01"}
                  if kind in ("customers", "events") else {})
        data = await fetch_fn(f"{base}{self._PATHS[kind]}", headers=headers,
                              params=params)
        # терпимы к обёртке: голый список ИЛИ {"<kind>"/"items": []}
        if isinstance(data, dict):
            items = (data.get(kind) or data.get("items") or [])
        else:
            items = data or []
        rows = [r for r in list(items)[:limit] if isinstance(r, dict)]
        fields = self._FIELDS.get(kind)
        if fields is None:
            # динамические колонки: union ключей строк (стабильный порядок)
            cols: List[str] = []
            for r in rows:
                for k in r:
                    if k not in cols:
                        cols.append(k)
            return cols, [{c: r.get(c) for c in cols} for r in rows]
        return fields, [{f: r.get(f) for f in fields} for r in rows]


register(AmoCRMConnector())
register(Bitrix24Connector())
register(HubSpotConnector())
register(PipedriveConnector())
register(CallInsightConnector())

# BI/БД-коннекторы (Postgres/Tableau/Looker) живут в bi_connectors и
# регистрируются в этот же реестр — единые ручки import/refresh/list.
# Импорт в конце файла: register уже определён, цикла нет.
from backend.core.ontology import bi_connectors as _bi
from backend.core.ontology import sheets_connector as _sheets  # noqa: E402,F401  (self-register)
# ERP-коннекторы (1С OData) — тот же реестр, те же ручки import/refresh/list.
from backend.core.ontology import erp_connectors as _erp  # noqa: E402,F401  (self-register)
# Маркетинг-аналитика (Roistat) — тот же реестр; ключи пер-пользовательски
# из карточки «Интеграций» с фолбэком на env.
from backend.core.ontology import marketing_connectors as _mkt  # noqa: E402,F401  (self-register)
