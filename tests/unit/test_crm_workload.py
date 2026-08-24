# -*- coding: utf-8 -*-
"""CRM-нагрузка сотрудника: маппинг owner→человек + generic-агрегация.

Разрыв из аудита: коннекторы отдают owner_id, но не было связи с Person.
Проверяем: маппинг хранится per-user, агрегация считает сделки/суммы/
просроченные CRM-задачи только для owner_id этого человека, разные схемы
колонок (amocrm responsible_user_id+unix-ts vs pipedrive owner_id+ISO)."""
import asyncio
import time
from datetime import date, timedelta

import backend.core.reports.crm_workload as cw

_UID = "11111111-1111-4111-8111-111111111111"


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(cw, "_map_path", lambda uid: tmp_path / f"{uid}.json")


def test_owner_map_roundtrip(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    r = cw.set_owner_mapping(_UID, "amocrm", "101", "Антон Иванов")
    assert r["status"] == "success"
    cw.set_owner_mapping(_UID, "pipedrive", "7", "Антон Иванов")
    assert cw.get_owner_map(_UID) == {
        "amocrm": {"101": "Антон Иванов"}, "pipedrive": {"7": "Антон Иванов"}}
    # отвязка пустым person
    cw.set_owner_mapping(_UID, "amocrm", "101", "")
    assert cw.get_owner_map(_UID)["amocrm"] == {}
    # валидация
    assert cw.set_owner_mapping(_UID, "", "1", "x")["status"] == "error"


def test_workload_aggregates_only_mapped_owner(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    cw.set_owner_mapping(_UID, "amocrm", "101", "Антон")

    past_ts = int(time.time()) - 86400          # вчера, unix (amocrm)
    future_iso = (date.today() + timedelta(days=3)).isoformat()

    class _FakeAmo:
        provider = "amocrm"
        kinds = ("leads", "tasks")

        async def fetch(self, kind, fetch_fn=None, *, limit=200):
            if kind == "leads":
                return (["id"], [
                    {"id": 1, "responsible_user_id": 101, "price": 50000},
                    {"id": 2, "responsible_user_id": 101, "price": 30000,
                     "is_completed": True},
                    {"id": 3, "responsible_user_id": 999, "price": 999999},
                ])
            return (["id"], [
                {"id": 4, "responsible_user_id": 101,
                 "complete_till": past_ts, "is_completed": False},
                {"id": 5, "responsible_user_id": 101,
                 "complete_till": future_iso, "is_completed": False},
                {"id": 6, "responsible_user_id": 101,
                 "complete_till": past_ts, "is_completed": True},
            ])

    monkeypatch.setattr(
        "backend.core.ontology.crm_connectors.list_providers",
        lambda: [{"provider": "amocrm", "ready": True, "env_hint": None}])
    monkeypatch.setattr(
        "backend.core.ontology.crm_connectors.get_connector",
        lambda p: _FakeAmo())

    out = asyncio.run(cw.crm_workload_for_person(_UID, "Антон Иванов"))
    assert out["deals_open"] == 1 and out["deals_done"] == 1
    assert out["deals_value"] == 80000.0        # чужая сделка не посчитана
    assert out["crm_tasks_open"] == 2           # done-задача исключена
    assert out["crm_tasks_overdue"] == 1        # только с прошедшим дедлайном


def test_workload_no_mapping_is_honest(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    out = asyncio.run(cw.crm_workload_for_person(_UID, "Кто-то"))
    assert out["providers"] == [] and out["deals_open"] == 0
    assert any("маппинг" in n for n in out["notes"])
