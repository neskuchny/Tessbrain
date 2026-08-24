# -*- coding: utf-8 -*-
"""Регресс на фикс регистрации Google Sheets-коннектора.

Раньше @register стоял на КЛАССЕ → в реестр попадал класс, и вызовы
env_check()/fetch() падали unbound-method. Теперь регистрируется экземпляр
(как все прочие коннекторы). Тестируется инъекцией fetch_fn (без сети)."""
from __future__ import annotations

import asyncio
import inspect

import pytest


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_all_providers_are_instances_not_classes():
    """Весь реестр — экземпляры: list_providers() не должен падать unbound."""
    from backend.core.ontology.crm_connectors import _PROVIDERS
    for name, conn in _PROVIDERS.items():
        assert not inspect.isclass(conn), f"{name} зарегистрирован классом"


def test_list_providers_works_end_to_end():
    from backend.core.ontology.crm_connectors import list_providers
    provs = {p["provider"]: p for p in list_providers()}
    # sheets присутствует и «готов» (публичная ссылка — env не нужен)
    assert "sheets" in provs
    assert provs["sheets"]["ready"] is True
    assert provs["sheets"]["env_hint"] is None


def test_sheets_env_check_bound():
    from backend.core.ontology.crm_connectors import get_connector
    conn = get_connector("sheets")
    assert conn.env_check() is None  # раньше — TypeError (unbound)


def test_sheets_fetch_parses_csv_with_injected_fn():
    from backend.core.ontology.crm_connectors import get_connector
    conn = get_connector("sheets")
    calls = []

    async def fake_csv(url):
        calls.append(url)
        return "Месяц,Выручка\n2026-01,3100000\n2026-02,3400000\n"

    cols, rows = _run(conn.fetch("sheet:1AbCdEf_GhIjKlMnOpQrStUvWxYz012345", fake_csv, limit=200))
    assert cols == ["Месяц", "Выручка"]
    assert rows == [{"Месяц": "2026-01", "Выручка": "3100000"},
                    {"Месяц": "2026-02", "Выручка": "3400000"}]
    # export-URL собран из id
    assert "spreadsheets/d/1AbCdEf_GhIjKlMnOpQrStUvWxYz012345/export?format=csv" in calls[0]


def test_sheets_rejects_bad_id():
    from backend.core.ontology.crm_connectors import get_connector
    conn = get_connector("sheets")
    with pytest.raises(ValueError):
        _run(conn.fetch("sheet:../etc/passwd", lambda *a, **k: None))


def test_sheets_html_response_is_honest_error():
    from backend.core.ontology.crm_connectors import get_connector
    conn = get_connector("sheets")

    async def html(url):
        return "<!DOCTYPE html><html>Sign in</html>"

    with pytest.raises(ValueError, match="HTML"):
        _run(conn.fetch("sheet:1AbCdEf_GhIjKlMnOpQrStUvWxYz012345", html))
