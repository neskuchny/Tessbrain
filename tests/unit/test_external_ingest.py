"""Unit-тесты для external_ingest (P3 — двунаправленная федерация).

Всё инъектируемо: graph_writer / audit — фейки, без графа/БД.
Модуль грузится через importlib со stub-пакетами, чтобы обойти
тяжёлый backend.core.store.__init__ (graph_builder/neo4j).
"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import types

_MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "backend" / "core" / "store" / "external_ingest.py"
)


def _load_module():
    for pkg in ("backend", "backend.core", "backend.core.store"):
        if pkg not in sys.modules:
            m = types.ModuleType(pkg)
            m.__path__ = []  # type: ignore[attr-defined]
            sys.modules[pkg] = m
    spec = importlib.util.spec_from_file_location(
        "backend.core.store.external_ingest", _MODULE_PATH,
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_mod = _load_module()
ingest_external_payload = _mod.ingest_external_payload
provenance_label = _mod.provenance_label
IngestResult = _mod.IngestResult


def _run(coro):
    return asyncio.run(coro)


# === Фейки ===============================================================

class _Consumer:
    def __init__(self, cid="c1", tenant="t1", name="Partner X"):
        self.id = cid
        self.tenant_id = tenant
        self.name = name


class _Policy:
    def __init__(self, *, allow_ingest=True, allowed=None, blocked=None,
                 max_level=3, max_items=100):
        self.allow_ingest = allow_ingest
        self.allowed_node_types = allowed if allowed is not None else ["*"]
        self.blocked_node_types = blocked or []
        self.max_access_level = max_level
        self.ingest_max_items_per_request = max_items


def _writer_ok():
    written: list = []

    async def w(*, node_id, label, properties, access_group):
        written.append({"id": node_id, "label": label,
                        "props": properties, "ag": access_group})
        return True

    return w, written


def _audit_collector():
    events: list = []

    async def a(*, action, consumer_id, detail):
        events.append({"action": action, "consumer_id": consumer_id,
                       "detail": detail})

    return a, events


# === Policy-gate =========================================================

def test_ingest_denied_when_policy_disallows() -> None:
    w, written = _writer_ok()
    a, events = _audit_collector()
    res = _run(ingest_external_payload(
        consumer=_Consumer(), policy=_Policy(allow_ingest=False),
        items=[{"type": "Project", "name": "X"}],
        graph_writer=w, audit=a,
    ))
    assert res.status_code == 403
    assert res.accepted == 0
    assert written == []
    assert any(e["action"] == "data_bus.ingest.denied" for e in events)


def test_ingest_denied_without_namespace() -> None:
    w, _ = _writer_ok()
    res = _run(ingest_external_payload(
        consumer=_Consumer(tenant=""), policy=_Policy(),
        items=[{"type": "Project", "name": "X"}], graph_writer=w,
    ))
    assert res.status_code == 403


def test_empty_payload_400() -> None:
    w, _ = _writer_ok()
    res = _run(ingest_external_payload(
        consumer=_Consumer(), policy=_Policy(),
        items=[], graph_writer=w,
    ))
    assert res.status_code == 400


# === Happy path + provenance + namespace =================================

def test_accept_writes_with_provenance_and_namespace() -> None:
    w, written = _writer_ok()
    a, events = _audit_collector()
    res = _run(ingest_external_payload(
        consumer=_Consumer(cid="abc", tenant="org9"),
        policy=_Policy(allowed=["Project"]),
        items=[{"type": "Project", "name": "JV Alpha",
                "description": "joint", "properties": {"status": "active"}}],
        graph_writer=w, audit=a,
    ))
    assert res.status_code == 200
    assert res.accepted == 1
    assert res.rejected == 0
    p = written[0]["props"]
    assert p["source"] == "external:partner:abc"
    assert p["user_id"] == "org9" and p["tenant_id"] == "org9"
    assert p["external_consumer_id"] == "abc"
    assert p["status"] == "active"  # пользовательские props сохранены
    assert written[0]["label"] == "Project"
    assert res.provenance == "external:partner:abc"
    assert any(e["action"] == "data_bus.ingest" for e in events)


def test_provenance_cannot_be_spoofed() -> None:
    w, written = _writer_ok()
    res = _run(ingest_external_payload(
        consumer=_Consumer(cid="real"), policy=_Policy(),
        items=[{"type": "Task", "name": "t",
                "properties": {"source": "internal:trusted",
                               "user_id": "victim_tenant",
                               "external_consumer_id": "fake"}}],
        graph_writer=w,
    ))
    p = written[0]["props"]
    assert p["source"] == "external:partner:real"
    assert p["user_id"] == "t1"  # consumer tenant, не из payload
    assert p["external_consumer_id"] == "real"


# === Тип-фильтрация ======================================================

def test_blocked_type_rejected() -> None:
    w, written = _writer_ok()
    res = _run(ingest_external_payload(
        consumer=_Consumer(), policy=_Policy(allowed=["*"], blocked=["Person"]),
        items=[{"type": "Person", "name": "spy"},
               {"type": "Project", "name": "ok"}],
        graph_writer=w,
    ))
    assert res.accepted == 1
    assert res.rejected == 1
    assert written[0]["label"] == "Project"


def test_type_not_in_allowlist_rejected() -> None:
    w, _ = _writer_ok()
    res = _run(ingest_external_payload(
        consumer=_Consumer(), policy=_Policy(allowed=["Project"]),
        items=[{"type": "Decision", "name": "secret"}],
        graph_writer=w,
    ))
    assert res.accepted == 0 and res.rejected == 1
    assert "not allowed" in res.rejections[0].reason


# === Валидация элементов =================================================

def test_missing_type_or_name_rejected() -> None:
    w, _ = _writer_ok()
    res = _run(ingest_external_payload(
        consumer=_Consumer(), policy=_Policy(),
        items=[{"name": "no type"}, {"type": "Project"},
               "not a dict", {"type": "Project", "name": "good"}],
        graph_writer=w,
    ))
    assert res.accepted == 1
    assert res.rejected == 3


# === access_level clamp ==================================================

def test_access_level_clamped_to_policy_ceiling() -> None:
    w, written = _writer_ok()
    res = _run(ingest_external_payload(
        consumer=_Consumer(), policy=_Policy(max_level=2),
        items=[{"type": "Project", "name": "x",
                "properties": {"access_level": 5}}],
        graph_writer=w,
    ))
    assert written[0]["props"]["access_level"] == 2


# === Bounded =============================================================

def test_payload_truncated_to_cap() -> None:
    w, written = _writer_ok()
    res = _run(ingest_external_payload(
        consumer=_Consumer(), policy=_Policy(max_items=3),
        items=[{"type": "Project", "name": f"p{i}"} for i in range(10)],
        graph_writer=w,
    ))
    assert res.accepted == 3
    assert len(written) == 3
    assert any("truncated" in r.reason for r in res.rejections)


# === Writer-сбой изолирован, не raises ===================================

def test_writer_failure_isolated_no_raise() -> None:
    async def flaky(*, node_id, label, properties, access_group):
        if "boom" in properties["name"]:
            raise RuntimeError("neo4j down")
        return True

    res = _run(ingest_external_payload(
        consumer=_Consumer(), policy=_Policy(),
        items=[{"type": "Project", "name": "boom"},
               {"type": "Project", "name": "fine"}],
        graph_writer=flaky,
    ))
    assert res.accepted == 1
    assert res.rejected == 1
    assert res.status_code == 200  # частичный успех


def test_audit_failure_does_not_break_ingest() -> None:
    w, written = _writer_ok()

    async def bad_audit(*, action, consumer_id, detail):
        raise RuntimeError("audit sink down")

    res = _run(ingest_external_payload(
        consumer=_Consumer(), policy=_Policy(),
        items=[{"type": "Project", "name": "x"}],
        graph_writer=w, audit=bad_audit,
    ))
    assert res.accepted == 1  # аудит упал — ingest всё равно прошёл


# === to_dict / provenance_label ==========================================

def test_to_dict_and_label_shape() -> None:
    assert provenance_label("p1") == "external:partner:p1"
    assert provenance_label("") == "external:partner:unknown"
    d = IngestResult(accepted=2, namespace="t", provenance="external:partner:p").to_dict()
    assert d["accepted"] == 2
    assert d["namespace"] == "t"
    assert d["status_code"] == 200
    assert "rejections" in d


def test_garbage_inputs_no_raise() -> None:
    res = _run(ingest_external_payload(
        consumer=_Consumer(), policy=_Policy(),
        items="not a list",  # type: ignore[arg-type]
        graph_writer=_writer_ok()[0],
    ))
    assert isinstance(res, IngestResult)
    assert res.status_code == 400
