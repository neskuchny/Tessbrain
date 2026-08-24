# -*- coding: utf-8 -*-
"""Маркетинг-аналитика как датасеты онтологии: Roistat (сквозная аналитика).

Тот же реестр CrmConnector, те же ручки import/refresh/list и регулярный
синк — «подключил Roistat → каналы с расходами/лидами/ROI потекли в мозг»,
дальше вопросы словами, Excel, дашборды и доски-алерты («CPL вырос»).

Ключи — ПЕР-ПОЛЬЗОВАТЕЛЬСКИ из карточки «Интеграции → Roistat»
(api_key + project_id) через fetch_for_user; фолбэк — env ROISTAT_API_KEY /
ROISTAT_PROJECT_ID (серверный уровень, как у остальных коннекторов).

API: POST https://cloud.roistat.com/api/v1/project/analytics/data?project=<id>
     заголовок Api-key, тело {dimensions, metrics, period}.
Формат запроса/ответа — по официальной документации
https://help-en.roistat.com/API/methods/analytics/ (дана в docstring, чтобы
при смене API было видно, от чего отталкивались).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Tuple

from backend.core.ontology.crm_connectors import (
    CrmConnector,
    register,
    user_integration_keys,
)

logger = logging.getLogger(__name__)

_BASE = "https://cloud.roistat.com/api/v1"

# Метрики из официального списка /project/analytics/metrics — базовый набор
# сквозной аналитики. Колонки датасета = эти же имена (числа для профиля).
_METRICS = ["visits", "leads", "sales", "revenue", "marketing_cost", "roi"]


async def _post_json(url: str, *, headers: Optional[dict] = None,
                     json_body: Optional[dict] = None) -> Any:
    import httpx
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(url, headers=headers or {}, json=json_body or {})
        r.raise_for_status()
        return r.json()


class RoistatConnector(CrmConnector):
    """Roistat: каналы/кампании со сквозными метриками за последние 30 дней.

    kinds:
      channels  — разрез по каналам (dimension marker_level_1);
      campaigns — разрез по UTM-кампаниям (dimension utm_campaign).
    """

    provider = "roistat"
    kinds = ("channels", "campaigns")

    _DIMENSION = {"channels": "marker_level_1", "campaigns": "utm_campaign"}
    _FIRST_COL = {"channels": "channel", "campaigns": "campaign"}

    # ── ключи: карточка пользователя → env ──────────────────────────────
    @staticmethod
    def _env_creds() -> Tuple[str, str]:
        return (os.environ.get("ROISTAT_API_KEY", "").strip(),
                os.environ.get("ROISTAT_PROJECT_ID", "").strip())

    @staticmethod
    async def _user_creds(user_id: Optional[str]) -> Tuple[str, str]:
        if not user_id:
            return "", ""
        try:
            from backend.core.integrations.user_keys_service import (
                get_keys_service,
            )
            keys = await get_keys_service().get_keys(user_id, "roistat") or {}
            return (str(keys.get("api_key") or "").strip(),
                    str(keys.get("project_id") or "").strip())
        except Exception:
            logger.debug("roistat: user keys lookup failed", exc_info=True)
            return "", ""

    def env_check(self) -> Optional[str]:
        key, project = self._env_creds()
        if not key:
            return ("Roistat: нет ключа — заполните карточку «Интеграции → "
                    "Roistat» (api_key + project_id) или ROISTAT_API_KEY в env")
        if not project:
            return "Roistat: не задан project_id (карточка или ROISTAT_PROJECT_ID)"
        return None

    async def env_check_for_user(self, user_id: Optional[str]) -> Optional[str]:
        key, project = await self._user_creds(user_id)
        if key and project:
            return None
        return self.env_check()

    # ── fetch ───────────────────────────────────────────────────────────
    async def fetch(self, kind: str, fetch_fn=None, *, limit: int = 200):
        key, project = self._env_creds()
        return await self._fetch_with(key, project, kind, fetch_fn, limit=limit)

    async def fetch_for_user(self, user_id: Optional[str], kind: str,
                             fetch_fn=None, *, limit: int = 200):
        key, project = await self._user_creds(user_id)
        if not (key and project):
            key, project = self._env_creds()
        return await self._fetch_with(key, project, kind, fetch_fn, limit=limit)

    async def _fetch_with(self, key: str, project: str, kind: str,
                          fetch_fn=None, *, limit: int = 200):
        if kind not in self.kinds:
            raise ValueError(f"roistat: kind должен быть из {self.kinds}")
        if not (key and project):
            raise ValueError(self.env_check() or "Roistat: ключи не заданы")
        fetch_fn = fetch_fn or _post_json
        now = datetime.now(timezone.utc)
        body = {
            "dimensions": [self._DIMENSION[kind]],
            "metrics": _METRICS,
            "period": {
                "from": (now - timedelta(days=30)).strftime("%Y-%m-%dT00:00:00+0000"),
                "to": now.strftime("%Y-%m-%dT%H:%M:%S+0000"),
            },
        }
        data = await fetch_fn(
            f"{_BASE}/project/analytics/data?project={project}",
            headers={"Api-key": key, "Content-type": "application/json"},
            json_body=body)
        if isinstance(data, dict) and str(data.get("status") or "") not in ("", "success"):
            raise ValueError(f"roistat: {data.get('error') or data.get('status')}")

        first = self._FIRST_COL[kind]
        columns = [first] + _METRICS
        rows: List[dict] = []
        dim = self._DIMENSION[kind]
        for block in (data or {}).get("data") or []:
            for it in (block or {}).get("items") or []:
                dv = ((it.get("dimensions") or {}).get(dim) or {})
                name = str(dv.get("title") or dv.get("value") or "").strip()
                if not name:
                    continue
                row: dict = {first: name}
                mts = it.get("metrics") or {}
                for m in _METRICS:
                    raw = (mts.get(m) or {}).get("value")
                    try:
                        row[m] = float(raw) if raw not in (None, "") else None
                    except (TypeError, ValueError):
                        row[m] = None
                rows.append(row)
                if len(rows) >= limit:
                    break
            if len(rows) >= limit:
                break
        if not rows:
            raise ValueError("roistat: пустой ответ — проверьте project_id, "
                             "ключ и что в проекте есть данные за 30 дней")
        return columns, rows


def _dynamic_columns(rows: List[dict]) -> Tuple[List[str], List[dict]]:
    """Колонки = union ключей строк (стабильный порядок появления) — для
    API, где схема записи богатая и меняется (журналы звонков)."""
    cols: List[str] = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    return cols, [{c: r.get(c) for c in cols} for r in rows]


async def _get_json(url: str, *, headers: Optional[dict] = None,
                    params: Optional[dict] = None) -> Any:
    import httpx
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
        r = await c.get(url, headers=headers or {}, params=params or {})
        r.raise_for_status()
        return r.json()


class CalltouchConnector(CrmConnector):
    """Calltouch: журнал звонков за последние 7 дней.

    GET api.calltouch.ru/calls-service/RestAPI/{site_id}/calls-diary/calls
    ?clientApiId=<token>&dateFrom=dd/MM/yyyy&dateTo=dd/MM/yyyy (официальный
    метод выгрузки журнала звонков). Ключи: карточка «Интеграции → Calltouch»
    (api_token + site_id), фолбэк — env CALLTOUCH_API_TOKEN/CALLTOUCH_SITE_ID."""

    provider = "calltouch"
    kinds = ("calls",)

    @staticmethod
    def _env_creds() -> Tuple[str, str]:
        return (os.environ.get("CALLTOUCH_API_TOKEN", "").strip(),
                os.environ.get("CALLTOUCH_SITE_ID", "").strip())

    @staticmethod
    async def _user_creds(user_id: Optional[str]) -> Tuple[str, str]:
        keys = await user_integration_keys(user_id, "calltouch")
        return (str(keys.get("api_token") or "").strip(),
                str(keys.get("site_id") or "").strip())

    def env_check(self) -> Optional[str]:
        token, site = self._env_creds()
        if not (token and site):
            return ("Calltouch: заполните карточку «Интеграции → Calltouch» "
                    "(api_token + site_id) или CALLTOUCH_API_TOKEN/"
                    "CALLTOUCH_SITE_ID в env")
        return None

    async def env_check_for_user(self, user_id: Optional[str]) -> Optional[str]:
        token, site = await self._user_creds(user_id)
        return None if (token and site) else self.env_check()

    async def fetch(self, kind: str, fetch_fn=None, *, limit: int = 200):
        token, site = self._env_creds()
        return await self._fetch_with(token, site, kind, fetch_fn, limit=limit)

    async def fetch_for_user(self, user_id: Optional[str], kind: str,
                             fetch_fn=None, *, limit: int = 200):
        token, site = await self._user_creds(user_id)
        if not (token and site):
            token, site = self._env_creds()
        return await self._fetch_with(token, site, kind, fetch_fn, limit=limit)

    async def _fetch_with(self, token: str, site: str, kind: str,
                          fetch_fn=None, *, limit: int = 200):
        if kind not in self.kinds:
            raise ValueError(f"calltouch: kind должен быть из {self.kinds}")
        if not (token and site):
            raise ValueError(self.env_check() or "Calltouch: ключи не заданы")
        fetch_fn = fetch_fn or _get_json
        now = datetime.now(timezone.utc)
        data = await fetch_fn(
            f"https://api.calltouch.ru/calls-service/RestAPI/{site}"
            f"/calls-diary/calls",
            params={"clientApiId": token,
                    "dateFrom": (now - timedelta(days=7)).strftime("%d/%m/%Y"),
                    "dateTo": now.strftime("%d/%m/%Y"),
                    "page": 1, "limit": min(500, max(1, limit))})
        records = (data.get("records") if isinstance(data, dict)
                   else data) or []
        rows = [r for r in list(records)[:limit] if isinstance(r, dict)]
        if not rows:
            raise ValueError("calltouch: звонков за 7 дней не найдено — "
                             "проверьте site_id/токен")
        return _dynamic_columns(rows)


class CallibriConnector(CrmConnector):
    """Callibri: статистика обращений по каналам за последние 7 дней
    (их лимит — неделя на запрос).

    GET api.callibri.ru/site_get_statistics?user_email&user_token&site_id
    &date1=dd.mm.yyyy&date2=dd.mm.yyyy → {channels_statistics: [...]}.
    Ключи: карточка «Интеграции → Callibri» (user_email + user_token +
    site_id), фолбэк — env CALLIBRI_USER_EMAIL/CALLIBRI_USER_TOKEN/
    CALLIBRI_SITE_ID."""

    provider = "callibri"
    kinds = ("channels",)

    @staticmethod
    def _env_creds() -> Tuple[str, str, str]:
        return (os.environ.get("CALLIBRI_USER_EMAIL", "").strip(),
                os.environ.get("CALLIBRI_USER_TOKEN", "").strip(),
                os.environ.get("CALLIBRI_SITE_ID", "").strip())

    @staticmethod
    async def _user_creds(user_id: Optional[str]) -> Tuple[str, str, str]:
        keys = await user_integration_keys(user_id, "callibri")
        return (str(keys.get("user_email") or "").strip(),
                str(keys.get("user_token") or "").strip(),
                str(keys.get("site_id") or "").strip())

    def env_check(self) -> Optional[str]:
        email, token, site = self._env_creds()
        if not (email and token and site):
            return ("Callibri: заполните карточку «Интеграции → Callibri» "
                    "(user_email + user_token + site_id) или env-переменные "
                    "CALLIBRI_*")
        return None

    async def env_check_for_user(self, user_id: Optional[str]) -> Optional[str]:
        email, token, site = await self._user_creds(user_id)
        return None if (email and token and site) else self.env_check()

    async def fetch(self, kind: str, fetch_fn=None, *, limit: int = 200):
        return await self._fetch_with(*self._env_creds(), kind, fetch_fn,
                                      limit=limit)

    async def fetch_for_user(self, user_id: Optional[str], kind: str,
                             fetch_fn=None, *, limit: int = 200):
        email, token, site = await self._user_creds(user_id)
        if not (email and token and site):
            email, token, site = self._env_creds()
        return await self._fetch_with(email, token, site, kind, fetch_fn,
                                      limit=limit)

    async def _fetch_with(self, email: str, token: str, site: str, kind: str,
                          fetch_fn=None, *, limit: int = 200):
        if kind not in self.kinds:
            raise ValueError(f"callibri: kind должен быть из {self.kinds}")
        if not (email and token and site):
            raise ValueError(self.env_check() or "Callibri: ключи не заданы")
        fetch_fn = fetch_fn or _get_json
        now = datetime.now(timezone.utc)
        data = await fetch_fn(
            "https://api.callibri.ru/site_get_statistics",
            params={"user_email": email, "user_token": token,
                    "site_id": site,
                    "date1": (now - timedelta(days=7)).strftime("%d.%m.%Y"),
                    "date2": now.strftime("%d.%m.%Y")})
        items = ((data.get("channels_statistics")
                  or data.get("statistics") or [])
                 if isinstance(data, dict) else (data or []))
        rows = [r for r in list(items)[:limit] if isinstance(r, dict)]
        if not rows:
            raise ValueError("callibri: статистики за 7 дней нет — проверьте "
                             "site_id/токен")
        return _dynamic_columns(rows)


class CoMagicConnector(CrmConnector):
    """CoMagic / UIS: отчёт по звонкам за последние 7 дней (Data API,
    JSON-RPC 2.0: POST /v2.0 method=get.calls_report).

    Ключи: карточка «Интеграции → CoMagic» (access_token; api_host —
    dataapi.comagic.ru или dataapi.uiscom.ru), фолбэк — env
    COMAGIC_ACCESS_TOKEN / COMAGIC_API_HOST."""

    provider = "comagic"
    kinds = ("calls",)

    _DEFAULT_HOST = "dataapi.comagic.ru"

    @staticmethod
    def _env_creds() -> Tuple[str, str]:
        return (os.environ.get("COMAGIC_ACCESS_TOKEN", "").strip(),
                os.environ.get("COMAGIC_API_HOST", "").strip())

    @staticmethod
    async def _user_creds(user_id: Optional[str]) -> Tuple[str, str]:
        keys = await user_integration_keys(user_id, "comagic")
        return (str(keys.get("access_token") or "").strip(),
                str(keys.get("api_host") or "").strip())

    def env_check(self) -> Optional[str]:
        token, _ = self._env_creds()
        if not token:
            return ("CoMagic: заполните карточку «Интеграции → CoMagic» "
                    "(access_token) или COMAGIC_ACCESS_TOKEN в env")
        return None

    async def env_check_for_user(self, user_id: Optional[str]) -> Optional[str]:
        token, _ = await self._user_creds(user_id)
        return None if token else self.env_check()

    async def fetch(self, kind: str, fetch_fn=None, *, limit: int = 200):
        token, host = self._env_creds()
        return await self._fetch_with(token, host, kind, fetch_fn, limit=limit)

    async def fetch_for_user(self, user_id: Optional[str], kind: str,
                             fetch_fn=None, *, limit: int = 200):
        token, host = await self._user_creds(user_id)
        if not token:
            token, host = self._env_creds()
        return await self._fetch_with(token, host, kind, fetch_fn, limit=limit)

    async def _fetch_with(self, token: str, host: str, kind: str,
                          fetch_fn=None, *, limit: int = 200):
        if kind not in self.kinds:
            raise ValueError(f"comagic: kind должен быть из {self.kinds}")
        if not token:
            raise ValueError(self.env_check() or "CoMagic: ключи не заданы")
        fetch_fn = fetch_fn or _post_json
        host = (host or self._DEFAULT_HOST).replace("https://", "").strip("/")
        now = datetime.now(timezone.utc)
        data = await fetch_fn(
            f"https://{host}/v2.0",
            headers={"Content-type": "application/json"},
            json_body={
                "jsonrpc": "2.0", "id": 1, "method": "get.calls_report",
                "params": {
                    "access_token": token,
                    "date_from": (now - timedelta(days=7)).strftime(
                        "%Y-%m-%d 00:00:00"),
                    "date_till": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "limit": min(10000, max(1, limit)),
                }})
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            raise ValueError(f"comagic: {(err or {}).get('message') or err}")
        items = (((data or {}).get("result") or {}).get("data")
                 if isinstance(data, dict) else None) or []
        rows = [r for r in list(items)[:limit] if isinstance(r, dict)]
        if not rows:
            raise ValueError("comagic: звонков за 7 дней не найдено — "
                             "проверьте access_token (и api_host: comagic/"
                             "uiscom)")
        return _dynamic_columns(rows)


register(RoistatConnector())
register(CalltouchConnector())
register(CallibriConnector())
register(CoMagicConnector())
