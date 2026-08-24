# -*- coding: utf-8 -*-
"""
BI/БД-коннекторы для онтологии данных (docs/ru/DATA_ONTOLOGY.md):
Postgres / Tableau / Looker как источники датасетов.

Регистрируются в ОБЩИЙ реестр коннекторов (crm_connectors.register) —
для системы это такие же датасеты: профиль, заземление, вопросы цифрами,
refresh тем же коннектором.

Отличие от CRM: «дека» здесь не фиксированный список, а параметризованный
идентификатор: `table:<имя>` (Postgres), `view:<luid>` (Tableau),
`look:<id>` (Looker). Никаких произвольных SQL от пользователя —
только валидированное имя таблицы/представления (анти-инъекция).

Учётные данные — только в env. Тестируется инжекцией query_fn/fetch_fn.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, List, Optional, Tuple

from backend.core.ontology.crm_connectors import CrmConnector, register

logger = logging.getLogger(__name__)

_PG_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")


async def _default_http(url: str, *, method: str = "GET",
                        headers: Optional[dict] = None,
                        params: Optional[dict] = None,
                        json_body: Optional[dict] = None,
                        text: bool = False) -> Any:
    """HTTP с методом и текстовым режимом (Tableau отдаёт CSV текстом)."""
    import httpx
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as c:
        r = await c.request(method, url, headers=headers or {},
                            params=params or {}, json=json_body)
        r.raise_for_status()
        return r.text if text else r.json()


class PostgresConnector(CrmConnector):
    """Внешний Postgres: таблица/представление как датасет.

    Env: BI_PG_DSN (`postgresql://user:pass@host:5432/db`).
    kind: `table:<schema.имя>` — только валидный идентификатор,
    произвольный SQL не принимается (инъекции исключены по построению)."""

    provider = "postgres"
    kinds = ("table:<schema.table>",)

    def env_check(self) -> Optional[str]:
        if not os.environ.get("BI_PG_DSN"):
            return "BI_PG_DSN не задан"
        return None

    async def fetch(self, kind: str, fetch_fn=None, *, limit: int = 200):
        if not kind.startswith("table:"):
            raise ValueError("postgres: kind должен быть вида table:<имя>")
        table = kind.split(":", 1)[1].strip()
        if not _PG_IDENT.match(table):
            raise ValueError(f"недопустимое имя таблицы: {table!r}")
        if (err := self.env_check()):
            raise ValueError(err)
        # fetch_fn(dsn, sql, limit) → list[dict]; по умолчанию asyncpg
        query_fn = fetch_fn or _default_pg_query
        sql = f'SELECT * FROM {table} LIMIT $1'   # имя проверено выше
        rows = await query_fn(os.environ["BI_PG_DSN"], sql, limit)
        rows = [dict(r) for r in rows][:limit]
        columns: List[str] = []
        for r in rows[:50]:
            for k in r:
                if k not in columns:
                    columns.append(str(k))
        return columns, [{c: r.get(c) for c in columns} for r in rows]


async def _default_pg_query(dsn: str, sql: str, limit: int) -> List[dict]:
    import asyncpg
    conn = await asyncpg.connect(dsn)
    try:
        return await conn.fetch(sql, limit)
    finally:
        await conn.close()


class TableauConnector(CrmConnector):
    """Tableau Server/Cloud: данные view как датасет (REST API + PAT).

    Env: TABLEAU_SERVER_URL, TABLEAU_SITE (пусто для Default),
    TABLEAU_PAT_NAME, TABLEAU_PAT_SECRET.
    kind: `view:<luid>` (LUID вьюхи из URL/REST)."""

    provider = "tableau"
    kinds = ("view:<luid>",)
    _API = "3.22"

    def env_check(self) -> Optional[str]:
        for var in ("TABLEAU_SERVER_URL", "TABLEAU_PAT_NAME",
                    "TABLEAU_PAT_SECRET"):
            if not os.environ.get(var):
                return f"{var} не задан"
        return None

    async def fetch(self, kind: str, fetch_fn=None, *, limit: int = 200):
        if not kind.startswith("view:"):
            raise ValueError("tableau: kind должен быть вида view:<luid>")
        view_id = kind.split(":", 1)[1].strip()
        if (err := self.env_check()):
            raise ValueError(err)
        http = fetch_fn or _default_http
        base = os.environ["TABLEAU_SERVER_URL"].rstrip("/")

        # 1) signin по PAT → токен + site id
        auth = await http(
            f"{base}/api/{self._API}/auth/signin", method="POST",
            headers={"Accept": "application/json"},
            json_body={"credentials": {
                "personalAccessTokenName": os.environ["TABLEAU_PAT_NAME"],
                "personalAccessTokenSecret": os.environ["TABLEAU_PAT_SECRET"],
                "site": {"contentUrl": os.environ.get("TABLEAU_SITE", "")},
            }})
        creds = (auth or {}).get("credentials") or {}
        token = creds.get("token")
        site_id = (creds.get("site") or {}).get("id")
        if not (token and site_id):
            raise ValueError("tableau signin не вернул token/site")

        # 2) данные вьюхи (CSV) → parse_tabular
        csv_text = await http(
            f"{base}/api/{self._API}/sites/{site_id}/views/{view_id}/data",
            headers={"X-Tableau-Auth": token}, text=True)
        from backend.core.ontology.dataset_registry import parse_tabular
        columns, rows = parse_tabular(csv_text or "")
        return columns, rows[:limit]


class LookerConnector(CrmConnector):
    """Looker: результат сохранённого Look как датасет (API 4.0).

    Env: LOOKER_BASE_URL (`https://company.looker.com`),
    LOOKER_CLIENT_ID, LOOKER_CLIENT_SECRET.
    kind: `look:<id>`."""

    provider = "looker"
    kinds = ("look:<id>",)

    def env_check(self) -> Optional[str]:
        for var in ("LOOKER_BASE_URL", "LOOKER_CLIENT_ID",
                    "LOOKER_CLIENT_SECRET"):
            if not os.environ.get(var):
                return f"{var} не задан"
        return None

    async def fetch(self, kind: str, fetch_fn=None, *, limit: int = 200):
        if not kind.startswith("look:"):
            raise ValueError("looker: kind должен быть вида look:<id>")
        look_id = kind.split(":", 1)[1].strip()
        if not look_id.isdigit():
            raise ValueError(f"look id должен быть числом: {look_id!r}")
        if (err := self.env_check()):
            raise ValueError(err)
        http = fetch_fn or _default_http
        base = os.environ["LOOKER_BASE_URL"].rstrip("/")

        # 1) login → access_token
        auth = await http(
            f"{base}/api/4.0/login", method="POST",
            params={"client_id": os.environ["LOOKER_CLIENT_ID"],
                    "client_secret": os.environ["LOOKER_CLIENT_SECRET"]})
        token = (auth or {}).get("access_token")
        if not token:
            raise ValueError("looker login не вернул access_token")

        # 2) run look → JSON-массив строк
        data = await http(
            f"{base}/api/4.0/looks/{look_id}/run/json",
            headers={"Authorization": f"Bearer {token}"},
            params={"limit": limit})
        rows = [dict(r) for r in (data or []) if isinstance(r, dict)][:limit]
        columns: List[str] = []
        for r in rows[:50]:
            for k in r:
                if k not in columns:
                    columns.append(str(k))
        return columns, [{c: r.get(c) for c in columns} for r in rows]


register(PostgresConnector())
register(TableauConnector())
register(LookerConnector())
