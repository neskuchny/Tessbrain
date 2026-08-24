# -*- coding: utf-8 -*-
"""A1: регулярный inbound-sync датасетов — расписание, наблюдаемость, драйвер.

Коннекторы подменяются fetch_fn'ом (тот же приём инъекции, что в crm_connectors):
никаких сетевых вызовов, детерминированные строки.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    import backend.core.store.tenant_paths as tp
    monkeypatch.setattr(tp, "_DATA_ROOT", tmp_path)
    # онтология включена (иначе registry-операции откажут)
    from backend.core.config import feature_flags
    ff = feature_flags.get_feature_flags()
    monkeypatch.setattr(ff, "enable_data_ontology", True, raising=False)
    monkeypatch.setattr(ff, "enable_dataset_sync", True, raising=False)
    # метрики — sqlite-файл в tmp (не Postgres)
    monkeypatch.setattr(ff, "metric_store_backend", "sqlite", raising=False) if hasattr(ff, "metric_store_backend") else None
    yield


UID = "11111111-1111-4111-8111-111111111111"


class _FakeAmo:
    """Мини-коннектор, отдающий заданные строки без сети."""
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    async def __call__(self, url, headers=None, params=None):
        self.calls += 1
        return {"_embedded": {"leads": self.rows}}


def _register_crm(uid, fake, monkeypatch):
    # env для amocrm env_check
    monkeypatch.setenv("AMOCRM_SUBDOMAIN", "acme")
    monkeypatch.setenv("AMOCRM_TOKEN", "tok")
    from backend.core.ontology import dataset_service as ds
    return _run(ds.register_from_crm(uid, provider="amocrm", kind="leads",
                                     title="Сделки", limit=10, fetch_fn=fake))


def test_normalize_schedule_validation():
    from backend.core.ontology.dataset_sync import normalize_schedule
    assert normalize_schedule({"kind": "interval", "minutes": 60}) == {
        "kind": "interval", "minutes": 60}
    assert normalize_schedule({"kind": "daily", "time": "9:5"}) == {
        "kind": "daily", "time": "09:05"}
    assert normalize_schedule({"kind": "weekly", "time": "08:00", "weekday": 2}) == {
        "kind": "weekly", "time": "08:00", "weekday": 2}
    for bad in ({}, {"kind": "x"}, {"kind": "interval", "minutes": 1},
                {"kind": "daily", "time": "25:00"},
                {"kind": "weekly", "time": "08:00", "weekday": 9}):
        with pytest.raises(ValueError):
            normalize_schedule(bad)


def test_set_schedule_only_syncable_sources(monkeypatch):
    from backend.core.ontology import dataset_service as ds
    from backend.core.ontology.dataset_sync import set_schedule
    # inline-датасет (kind=csv) — refresh не поддержан → отказ
    rec = _run(ds.register_dataset(UID, title="Ручной",
                                   content="a,b\n1,2\n3,4", fmt="csv"))
    did = rec["dataset"]["dataset_id"]
    res = set_schedule(UID, did, {"kind": "interval", "minutes": 60})
    assert not res["success"] and "не поддерживает" in res["error"]


def test_schedule_and_sync_now_records_observability(monkeypatch):
    from backend.core.ontology.dataset_sync import (
        set_schedule, sync_now, sync_status,
    )
    fake = _FakeAmo([{"id": 1, "name": "A", "price": 100},
                     {"id": 2, "name": "B", "price": 200}])
    rec = _register_crm(UID, fake, monkeypatch)
    assert rec["success"], rec
    did = rec["dataset"]["dataset_id"]

    # назначить расписание
    r = set_schedule(UID, did, {"kind": "interval", "minutes": 30})
    assert r["success"] and r["schedule"]["minutes"] == 30

    # синк сейчас: коннектор снова отдаёт (уже 3 строки — refresh заменяет)
    fake.rows = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"},
                 {"id": 3, "name": "C"}]
    res = _run(sync_now(UID, did, trigger="manual", fetch_fn=fake))
    assert res["success"] and res["status"] == "ok", res
    assert res["rows"] == 3, res

    # наблюдаемость записана
    st = sync_status(UID)
    assert len(st) == 1
    s = st[0]["sync"]
    assert s["last_status"] == "ok" and s["last_rows"] == 3
    assert s["last_trigger"] == "manual" and s["last_error"] is None
    assert isinstance(s["last_duration_ms"], int)


def test_run_due_picks_due_and_records(monkeypatch):
    from backend.core.ontology.dataset_sync import (
        set_schedule, run_due_dataset_syncs, sync_status,
    )
    fake = _FakeAmo([{"id": 1, "name": "A"}])
    rec = _register_crm(UID, fake, monkeypatch)
    did = rec["dataset"]["dataset_id"]
    set_schedule(UID, did, {"kind": "interval", "minutes": 30})

    # драйвер зовёт refresh_dataset (env-креды + _default_fetch), поэтому
    # подменяем fetch на уровне коннектора через monkeypatch httpx-обёртки
    import backend.core.ontology.crm_connectors as cc
    monkeypatch.setattr(cc, "_default_fetch", fake)

    # ещё не синкали (last_at пуст) → due
    res = _run(run_due_dataset_syncs(datetime.now(timezone.utc)))
    assert res["enabled"] and res["scanned"] == 1 and res["ran"] == 1, res
    assert fake.calls >= 1

    # только что синкнули → НЕ due (интервал 30 мин)
    res2 = _run(run_due_dataset_syncs(datetime.now(timezone.utc)))
    assert res2["scanned"] == 1 and res2["ran"] == 0, res2

    # спустя 31 минуту → снова due
    later = datetime.now(timezone.utc) + timedelta(minutes=31)
    res3 = _run(run_due_dataset_syncs(later))
    assert res3["ran"] == 1, res3


def test_run_due_disabled_when_flag_off(monkeypatch):
    from backend.core.config import feature_flags
    from backend.core.ontology.dataset_sync import run_due_dataset_syncs
    ff = feature_flags.get_feature_flags()
    monkeypatch.setattr(ff, "enable_dataset_sync", False, raising=False)
    monkeypatch.delenv("ENABLE_DATASET_SYNC", raising=False)
    res = _run(run_due_dataset_syncs())
    assert res == {"enabled": False, "scanned": 0, "ran": 0, "errors": 0}


def test_sync_now_records_error(monkeypatch):
    from backend.core.ontology.dataset_sync import (
        set_schedule, sync_now, sync_status,
    )
    fake = _FakeAmo([{"id": 1, "name": "A"}])
    rec = _register_crm(UID, fake, monkeypatch)
    did = rec["dataset"]["dataset_id"]
    set_schedule(UID, did, {"kind": "interval", "minutes": 30})

    async def _boom(url, headers=None, params=None):
        raise RuntimeError("CRM 500")
    res = _run(sync_now(UID, did, trigger="manual", fetch_fn=_boom))
    assert not res["success"] and res["status"] == "error"
    s = sync_status(UID)[0]["sync"]
    assert s["last_status"] == "error" and "CRM 500" in (s["last_error"] or "")
