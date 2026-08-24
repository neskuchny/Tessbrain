# -*- coding: utf-8 -*-
"""ERP-коннекторы для онтологии данных (A2 enterprise-роадмапа).

1С — самый частый запрос производств. Стандартный OData-интерфейс 1С
(«Публикация → REST/OData») отдаёт справочники/документы/регистры как REST:
    {base}/odata/standard.odata/{Сущность}?$format=json&$top=N
Ответ: {"value": [ {поле: значение, ...}, ... ]}.

Коннектор — по ТОЧНОМУ образцу CRM/BI (crm_connectors.CrmConnector): те же
контракт fetch(kind)->(columns, rows), env_check, инъекция fetch_fn для тестов,
регистрация в общий реестр. Активируется ТОЛЬКО при заданных env — иначе
env_check возвращает подсказку и провайдер числится «не готов». Ничего в
существующих потоках не меняется: это ещё одна запись в _PROVIDERS.

kind — имя OData-сущности как есть (динамическое, как table:<...> у Postgres):
    Catalog_Номенклатура | Document_ЗаказПокупателя | AccumulationRegisterBalance_...
Имя сущности валидируется по OData-грамматике (буквы/цифры/_ и один
разделитель '_'), произвольные query-параметры/инъекции исключены.

Env: ONEC_ODATA_URL (база публикации, напр. https://host/base или сразу
.../odata/standard.odata), ONEC_USERNAME, ONEC_PASSWORD (Basic-аутентификация).
"""
from __future__ import annotations

import base64
import logging
import os
import re
from typing import List, Optional

from backend.core.ontology.crm_connectors import CrmConnector, register

logger = logging.getLogger(__name__)

# OData-сущность 1С: латиница/цифры/подчёркивание + опц. один суффикс через '_'
# (Catalog_Номенклатура → латинский префикс тип_имя; кириллица имени допустима).
_ODATA_ENTITY = re.compile(r"^[A-Za-z][A-Za-z0-9]*_[\w\-]+$", re.UNICODE)


class OneCODataConnector(CrmConnector):
    """1С:Предприятие через стандартный OData-интерфейс.

    Env: ONEC_ODATA_URL, ONEC_USERNAME, ONEC_PASSWORD.
    kind: имя сущности OData (Catalog_… / Document_… / …)."""

    provider = "1c"
    # подсказки для UI (реальные имена сущностей зависят от конфигурации 1С)
    kinds = ("Catalog_<Справочник>", "Document_<Документ>",
             "AccumulationRegisterBalance_<Регистр>")

    def env_check(self) -> Optional[str]:
        if not os.environ.get("ONEC_ODATA_URL"):
            return "ONEC_ODATA_URL не задан (адрес OData-публикации 1С)"
        if not os.environ.get("ONEC_USERNAME"):
            return "ONEC_USERNAME не задан"
        if not os.environ.get("ONEC_PASSWORD"):
            return "ONEC_PASSWORD не задан"
        return None

    def _base(self) -> str:
        base = os.environ["ONEC_ODATA_URL"].rstrip("/")
        # допускаем как базу публикации, так и полный путь до standard.odata
        if not base.endswith("/odata/standard.odata"):
            base = f"{base}/odata/standard.odata"
        return base

    async def fetch(self, kind: str, fetch_fn=None, *, limit: int = 200):
        entity = (kind or "").strip()
        if not _ODATA_ENTITY.match(entity):
            raise ValueError(
                "1c: kind — имя OData-сущности вида Catalog_Номенклатура "
                "или Document_ЗаказПокупателя")
        if (err := self.env_check()):
            raise ValueError(err)
        from backend.core.ontology.crm_connectors import _default_fetch
        fetch_fn = fetch_fn or _default_fetch
        url = f"{self._base()}/{entity}"
        cred = base64.b64encode(
            f"{os.environ['ONEC_USERNAME']}:{os.environ['ONEC_PASSWORD']}"
            .encode("utf-8")).decode("ascii")
        headers = {"Authorization": f"Basic {cred}",
                   "Accept": "application/json"}
        rows: List[dict] = []
        skip, page = 0, min(1000, max(1, limit))
        while len(rows) < limit:
            data = await fetch_fn(url, headers=headers, params={
                "$format": "json", "$top": page, "$skip": skip})
            items = (data or {}).get("value") or []
            if not items:
                break
            rows.extend(items)
            if len(items) < page:
                break
            skip += page
        rows = rows[:limit]
        # OData отдаёт неоднородные объекты — собираем стабильный набор колонок
        # (метаданные вида odata.* отбрасываем), в порядке первого появления.
        columns: List[str] = []
        for r in rows[:50]:
            if not isinstance(r, dict):
                continue
            for k in r:
                sk = str(k)
                if not sk.startswith("odata.") and "@" not in sk and sk not in columns:
                    columns.append(sk)
        return columns, [
            {c: r.get(c) for c in columns} for r in rows if isinstance(r, dict)]


register(OneCODataConnector())


# ── МойСклад (JSON API 1.2) ────────────────────────────────────────────────
# Популярная у производств/торговли учётная система. REST JSON:
#   https://api.moysklad.ru/api/remap/1.2/entity/{сущность}?limit=1000&offset=N
# Ответ: {"rows": [ {...}, ... ], "meta": {"size": N}}.
# Сущность — валидный slug (product/customerorder/demand/counterparty/store/…),
# проверяется по грамматике (инъекции исключены). Токен-авторизация из env.

_MS_ENTITY = re.compile(r"^[a-z][a-z0-9]{1,40}$")


class MoyskladConnector(CrmConnector):
    """МойСклад через JSON API 1.2.

    Env: MOYSKLAD_TOKEN (токен доступа; создаётся в МойСклад → Настройки →
    Обмен данными → Токен). kind — имя сущности: product (номенклатура),
    customerorder (заказы покупателей), demand (отгрузки), counterparty
    (контрагенты), store (склады) и т.п."""

    provider = "moysklad"
    kinds = ("product", "customerorder", "demand", "counterparty", "store")

    _BASE = "https://api.moysklad.ru/api/remap/1.2/entity"

    def env_check(self) -> Optional[str]:
        if not os.environ.get("MOYSKLAD_TOKEN"):
            return "MOYSKLAD_TOKEN не задан (токен доступа МойСклад)"
        return None

    async def fetch(self, kind: str, fetch_fn=None, *, limit: int = 200):
        entity = (kind or "").strip().lower()
        if not _MS_ENTITY.match(entity):
            raise ValueError(
                "moysklad: kind — имя сущности (product / customerorder / "
                "demand / counterparty / store)")
        if (err := self.env_check()):
            raise ValueError(err)
        from backend.core.ontology.crm_connectors import _default_fetch
        fetch_fn = fetch_fn or _default_fetch
        url = f"{self._BASE}/{entity}"
        headers = {"Authorization": f"Bearer {os.environ['MOYSKLAD_TOKEN']}",
                   "Accept": "application/json;charset=utf-8",
                   # МойСклад требует gzip, иначе 429 (httpx распакует сам)
                   "Accept-Encoding": "gzip"}
        rows: List[dict] = []
        offset, page = 0, min(1000, max(1, limit))
        while len(rows) < limit:
            data = await fetch_fn(url, headers=headers, params={
                "limit": page, "offset": offset})
            items = (data or {}).get("rows") or []
            if not items:
                break
            rows.extend(items)
            if len(items) < page:
                break
            offset += page
        rows = rows[:limit]
        # rows МойСклад — вложенные объекты; берём верхнеуровневые скалярные
        # поля (meta/dict/list отбрасываем), в порядке первого появления.
        columns: List[str] = []
        for r in rows[:50]:
            if not isinstance(r, dict):
                continue
            for k, v in r.items():
                if k not in columns and not isinstance(v, (dict, list)):
                    columns.append(str(k))
        return columns, [
            {c: r.get(c) for c in columns} for r in rows if isinstance(r, dict)]


register(MoyskladConnector())
