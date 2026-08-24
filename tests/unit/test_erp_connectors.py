# -*- coding: utf-8 -*-
"""A2: 1С OData-коннектор. Чисто аддитивный — тестируется инъекцией fetch_fn
(тот же приём, что у всех коннекторов), без сети. Активен только при env."""
from __future__ import annotations

import asyncio
import base64

import pytest


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def onec_env(monkeypatch):
    monkeypatch.setenv("ONEC_ODATA_URL", "https://1c.acme.ru/buh")
    monkeypatch.setenv("ONEC_USERNAME", "reader")
    monkeypatch.setenv("ONEC_PASSWORD", "secret")


def test_provider_registered_in_shared_registry():
    # импорт crm_connectors подтягивает erp_connectors (self-register)
    from backend.core.ontology.crm_connectors import (
        _PROVIDERS,
        get_connector,
        list_providers,
    )
    conn = get_connector("1c")
    assert conn is not None and conn.provider == "1c"
    # прежние коннекторы на месте — регистр не сломан добавлением 1С
    assert {"amocrm", "bitrix24", "postgres", "sheets", "1c"} <= set(_PROVIDERS)
    assert "1c" in {p["provider"] for p in list_providers()}


def test_env_check_gates_without_creds(monkeypatch):
    monkeypatch.delenv("ONEC_ODATA_URL", raising=False)
    from backend.core.ontology.crm_connectors import get_connector, list_providers
    conn = get_connector("1c")
    hint = conn.env_check()
    assert hint is not None and "ONEC_ODATA_URL" in hint  # не готов без env
    p1c = next(p for p in list_providers() if p["provider"] == "1c")
    assert p1c["ready"] is False and p1c["env_hint"]


def test_fetch_parses_odata_value_and_drops_metadata(onec_env):
    from backend.core.ontology.crm_connectors import get_connector
    conn = get_connector("1c")

    calls = []

    async def fake(url, *, headers=None, params=None):
        calls.append((url, headers, params))
        # OData 1С: value[], плюс служебные odata.*/@ поля — их отбрасываем
        return {"odata.metadata": "…", "value": [
            {"Ref_Key": "a1", "Наименование": "Болт М6", "Цена": 12.5,
             "Наименование@navigationLinkUrl": "x"},
            {"Ref_Key": "a2", "Наименование": "Гайка М6", "Цена": 8.0},
        ]}

    cols, rows = _run(conn.fetch("Catalog_Номенклатура", fake, limit=200))
    # колонки — без служебных odata.* и @-полей
    assert cols == ["Ref_Key", "Наименование", "Цена"]
    assert rows[0]["Наименование"] == "Болт М6" and rows[1]["Цена"] == 8.0
    # URL нормализован: база + /odata/standard.odata/<Сущность>
    url, headers, params = calls[0]
    assert url == "https://1c.acme.ru/buh/odata/standard.odata/Catalog_Номенклатура"
    assert params["$format"] == "json" and params["$top"] == 200
    # Basic-аутентификация собрана из env
    expect = "Basic " + base64.b64encode(b"reader:secret").decode()
    assert headers["Authorization"] == expect


def test_fetch_accepts_full_odata_base(monkeypatch):
    monkeypatch.setenv("ONEC_ODATA_URL",
                       "https://1c.acme.ru/buh/odata/standard.odata/")
    monkeypatch.setenv("ONEC_USERNAME", "u")
    monkeypatch.setenv("ONEC_PASSWORD", "p")
    from backend.core.ontology.crm_connectors import get_connector
    conn = get_connector("1c")

    async def fake(url, *, headers=None, params=None):
        fake.url = url
        return {"value": [{"Ref_Key": "1"}]}

    _run(conn.fetch("Document_ЗаказПокупателя", fake))
    # не удвоился /odata/standard.odata
    assert fake.url == ("https://1c.acme.ru/buh/odata/standard.odata/"
                        "Document_ЗаказПокупателя")


def test_pagination_accumulates(onec_env):
    from backend.core.ontology.crm_connectors import get_connector
    conn = get_connector("1c")
    pages = [
        {"value": [{"Ref_Key": str(i)} for i in range(1000)]},
        {"value": [{"Ref_Key": str(i)} for i in range(1000, 1500)]},
    ]
    seq = iter(pages)

    async def fake(url, *, headers=None, params=None):
        return next(seq)

    cols, rows = _run(conn.fetch("Catalog_X", fake, limit=1500))
    assert len(rows) == 1500


def test_invalid_entity_rejected(onec_env):
    from backend.core.ontology.crm_connectors import get_connector
    conn = get_connector("1c")
    for bad in ("', DROP TABLE", "../secret", "Catalog Номенклатура", ""):
        with pytest.raises(ValueError):
            _run(conn.fetch(bad, lambda *a, **k: None))


def test_fetch_without_env_raises(monkeypatch):
    monkeypatch.delenv("ONEC_ODATA_URL", raising=False)
    from backend.core.ontology.crm_connectors import get_connector
    conn = get_connector("1c")
    with pytest.raises(ValueError):
        _run(conn.fetch("Catalog_X", lambda *a, **k: None))


# ── МойСклад (JSON API 1.2) — тот же безопасный паттерн ──

@pytest.fixture
def ms_env(monkeypatch):
    monkeypatch.setenv("MOYSKLAD_TOKEN", "tok-123")


def test_moysklad_registered():
    from backend.core.ontology.crm_connectors import get_connector
    conn = get_connector("moysklad")
    assert conn is not None and conn.provider == "moysklad"


def test_moysklad_env_gate(monkeypatch):
    monkeypatch.delenv("MOYSKLAD_TOKEN", raising=False)
    from backend.core.ontology.crm_connectors import get_connector
    conn = get_connector("moysklad")
    hint = conn.env_check()
    assert hint and "MOYSKLAD_TOKEN" in hint


def test_moysklad_fetch_parses_rows_and_flattens(ms_env):
    from backend.core.ontology.crm_connectors import get_connector
    conn = get_connector("moysklad")
    calls = []

    async def fake(url, *, headers=None, params=None):
        calls.append((url, headers, params))
        return {"rows": [
            {"name": "Болт М6", "code": "0001", "archived": False,
             "meta": {"type": "product"},          # dict — отбрасывается
             "salePrices": [{"value": 1250}]},      # list — отбрасывается
            {"name": "Гайка М6", "code": "0002", "archived": False},
        ], "meta": {"size": 2}}

    cols, rows = _run(conn.fetch("product", fake, limit=200))
    # только верхнеуровневые скаляры (meta/list убраны)
    assert cols == ["name", "code", "archived"]
    assert rows[0]["name"] == "Болт М6" and rows[1]["code"] == "0002"
    url, headers, params = calls[0]
    assert url == "https://api.moysklad.ru/api/remap/1.2/entity/product"
    assert headers["Authorization"] == "Bearer tok-123"
    assert params["limit"] == 200 and params["offset"] == 0


def test_moysklad_pagination(ms_env):
    from backend.core.ontology.crm_connectors import get_connector
    conn = get_connector("moysklad")
    pages = [
        {"rows": [{"name": str(i)} for i in range(1000)]},
        {"rows": [{"name": str(i)} for i in range(1000, 1200)]},
    ]
    seq = iter(pages)

    async def fake(url, *, headers=None, params=None):
        return next(seq)

    cols, rows = _run(conn.fetch("customerorder", fake, limit=1200))
    assert len(rows) == 1200


def test_moysklad_invalid_entity_rejected(ms_env):
    from backend.core.ontology.crm_connectors import get_connector
    conn = get_connector("moysklad")
    # «Product» нормализуется в lowercase → валиден; отбрасываем реально кривые
    for bad in ("../secret", "customer order", "prod/uct", ""):
        with pytest.raises(ValueError):
            _run(conn.fetch(bad, lambda *a, **k: None))


def test_moysklad_entity_case_normalized(ms_env):
    """Имя сущности приводится к нижнему регистру (Product → product)."""
    from backend.core.ontology.crm_connectors import get_connector
    conn = get_connector("moysklad")

    async def fake(url, *, headers=None, params=None):
        fake.url = url
        return {"rows": [{"name": "x"}]}

    _run(conn.fetch("Product", fake))
    assert fake.url.endswith("/entity/product")
