# -*- coding: utf-8 -*-
"""Аудит чтений чувствительных знаний.

Два свойства, без которых журнал чтений бесполезен: он не должен тонуть
в шуме от обычной работы интерфейса, и он не должен ронять выдачу данных,
если сам сломался.
"""
from __future__ import annotations

import pytest

from backend.core.observability import read_audit as RA


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    RA.reset_window()
    monkeypatch.delenv("READ_AUDIT_ENABLED", raising=False)
    monkeypatch.delenv("READ_AUDIT_MIN_LEVEL", raising=False)
    monkeypatch.delenv("READ_AUDIT_WINDOW_SECONDS", raising=False)
    yield
    RA.reset_window()


@pytest.fixture
def emitted(monkeypatch):
    """Перехват записей аудита."""
    rows = []

    async def _emit(**kw):
        rows.append(kw)

    monkeypatch.setattr("backend.core.observability.audit_log.emit", _emit)
    return rows


# ── базовая запись ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_records_read(emitted):
    ok = await RA.record_read("u-1", resource_type="person_profile",
                              resource_id="p:ivan")
    assert ok is True
    assert len(emitted) == 1
    assert emitted[0]["action"] == "read.person_profile"
    assert emitted[0]["resource"] == "person_profile:p:ivan"
    assert emitted[0]["user_id"] == "u-1"


@pytest.mark.asyncio
async def test_no_user_no_record(emitted):
    assert await RA.record_read("", resource_type="person_profile") is False
    assert emitted == []


@pytest.mark.asyncio
async def test_can_be_disabled(emitted, monkeypatch):
    monkeypatch.setenv("READ_AUDIT_ENABLED", "0")
    assert await RA.record_read("u-1", resource_type="x") is False
    assert emitted == []


@pytest.mark.asyncio
async def test_enabled_by_default(emitted):
    assert RA.enabled() is True
    assert await RA.record_read("u-1", resource_type="x") is True


# ── подавление повторов ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_repeat_within_window_suppressed(emitted):
    """Обновил страницу трижды — в журнале одна запись, а не три."""
    for _ in range(3):
        await RA.record_read("u-1", resource_type="person_profile",
                             resource_id="p:ivan")
    assert len(emitted) == 1


@pytest.mark.asyncio
async def test_different_resource_not_suppressed(emitted):
    await RA.record_read("u-1", resource_type="person_profile",
                         resource_id="p:ivan")
    await RA.record_read("u-1", resource_type="person_profile",
                         resource_id="p:petr")
    assert len(emitted) == 2


@pytest.mark.asyncio
async def test_different_user_not_suppressed(emitted):
    """Два человека смотрят один профиль — это две разные записи."""
    await RA.record_read("u-1", resource_type="person_profile",
                         resource_id="p:ivan")
    await RA.record_read("u-2", resource_type="person_profile",
                         resource_id="p:ivan")
    assert len(emitted) == 2


@pytest.mark.asyncio
async def test_window_zero_disables_suppression(emitted, monkeypatch):
    monkeypatch.setenv("READ_AUDIT_WINDOW_SECONDS", "0")
    for _ in range(3):
        await RA.record_read("u-1", resource_type="p", resource_id="x")
    assert len(emitted) == 3


@pytest.mark.asyncio
async def test_window_expiry_allows_new_record(emitted, monkeypatch):
    """Возврат к данным через час — снова запись, дребезг не скрывает это."""
    await RA.record_read("u-1", resource_type="p", resource_id="x")
    assert len(emitted) == 1

    base = [1000.0]
    monkeypatch.setattr(RA.time, "monotonic", lambda: base[0])
    RA.reset_window()
    await RA.record_read("u-1", resource_type="p", resource_id="x")
    base[0] += 301          # окно 300с истекло
    await RA.record_read("u-1", resource_type="p", resource_id="x")
    assert len(emitted) == 3


def test_recent_dict_does_not_grow_unbounded():
    """Защита от роста словаря подавления."""
    for i in range(RA._MAX_RECENT + 100):
        RA._should_record("u", f"res-{i}")
    assert len(RA._recent) <= RA._MAX_RECENT


# ── чувствительность узлов ──────────────────────────────────────────


@pytest.mark.parametrize("level,sensitive", [
    (0, False), (2, False), (3, True), (4, True), (5, True),
])
def test_sensitivity_threshold(level, sensitive):
    assert RA.is_sensitive({"access_level": level}) is sensitive


def test_node_without_level_is_not_sensitive():
    assert RA.is_sensitive({"id": "x"}) is False


def test_broken_level_is_not_sensitive():
    assert RA.is_sensitive({"access_level": "секретно"}) is False


def test_threshold_configurable(monkeypatch):
    monkeypatch.setenv("READ_AUDIT_MIN_LEVEL", "5")
    assert RA.is_sensitive({"access_level": 4}) is False
    assert RA.is_sensitive({"access_level": 5}) is True


# ── списки узлов ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_public_nodes_produce_no_noise(emitted):
    """Обычная работа со списком публичных узлов журнал не засоряет."""
    nodes = [{"id": f"n{i}", "access_level": 0} for i in range(50)]
    assert await RA.record_sensitive_nodes("u-1", nodes) == 0
    assert emitted == []


@pytest.mark.asyncio
async def test_sensitive_nodes_one_aggregated_record(emitted):
    """Десять чувствительных узлов — одна строка, а не десять."""
    nodes = ([{"id": f"s{i}", "access_level": 4} for i in range(10)]
             + [{"id": "pub", "access_level": 0}])
    n = await RA.record_sensitive_nodes("u-1", nodes, resource_type="graph_goal")
    assert n == 10
    assert len(emitted) == 1
    md = emitted[0]["metadata"]
    assert md["count"] == 10
    assert md["access_level"] == 4, "запоминаем максимальный уровень"
    assert len(md["sample_ids"]) == 10


@pytest.mark.asyncio
async def test_sample_ids_are_capped(emitted):
    nodes = [{"id": f"s{i}", "access_level": 3} for i in range(100)]
    await RA.record_sensitive_nodes("u-1", nodes)
    md = emitted[0]["metadata"]
    assert md["count"] == 100
    assert len(md["sample_ids"]) == 20, "в метаданные кладём образец, не всё"


@pytest.mark.asyncio
async def test_empty_list_is_noop(emitted):
    assert await RA.record_sensitive_nodes("u-1", []) == 0
    assert await RA.record_sensitive_nodes("u-1", None) == 0
    assert emitted == []


# ── устойчивость ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_failure_does_not_break_read(monkeypatch):
    """Журнал — наблюдение за системой, а не её часть."""
    async def _boom(**kw):
        raise RuntimeError("audit db down")

    monkeypatch.setattr("backend.core.observability.audit_log.emit", _boom)
    assert await RA.record_read("u-1", resource_type="p") is False


@pytest.mark.asyncio
async def test_garbage_nodes_do_not_raise(monkeypatch):
    async def _emit(**kw):
        pass
    monkeypatch.setattr("backend.core.observability.audit_log.emit", _emit)
    assert await RA.record_sensitive_nodes(
        "u-1", ["строка", None, {"access_level": 4, "id": "ok"}]) == 1
