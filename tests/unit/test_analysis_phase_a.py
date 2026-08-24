"""Тесты для Analysis Engine Фаза A — модель + store + CRUD endpoints.

Store работает на in-memory fallback (postgres=None). Endpoints
вызываются напрямую через handler.fn с JWT-моками (как orgs_routes).
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import sys
import types
import uuid
from pathlib import Path
from typing import Any

import jwt
import pytest
from litestar.exceptions import HTTPException

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.core.analysis.models import (
    AnalysisDimension,
    AnalysisPlaybook,
    AnalysisRun,
    RunStatus,
)
from backend.core.analysis.store import PlaybookStore, RunStore


def _run(coro):
    return asyncio.run(coro)


def _uid() -> str:
    return str(uuid.uuid4())


# =============================================================================
# Model
# =============================================================================


def test_dimension_roundtrip():
    d = AnalysisDimension(
        key="debt_load", label="Долговая нагрузка",
        rationale="Высокий долг → риск дефолта",
        extraction_hint="Найди обязательства, кредиты",
        kind="quantitative", computation="debt / equity",
        inputs=["debt", "equity"], benchmark={"red_flag": 2.0}, weight=2.0,
    )
    d2 = AnalysisDimension.from_dict(d.to_dict())
    assert d2.key == "debt_load"
    assert d2.computation == "debt / equity"
    assert d2.inputs == ["debt", "equity"]
    assert d2.benchmark["red_flag"] == 2.0
    assert d2.weight == 2.0


def test_playbook_roundtrip_with_dimensions():
    pb = AnalysisPlaybook(
        id=AnalysisPlaybook.new_id(),
        name="DD стандарт",
        analysis_type="due_diligence",
        dimensions=[
            AnalysisDimension(key="debt", label="Долг", kind="quantitative"),
            AnalysisDimension(key="legal", label="Юр-риски", kind="qualitative"),
        ],
        company_context={"use_snapshot": True, "custom": "Мы — PE-фонд"},
        reference_report="# Пример отчёта\n...",
    )
    pb2 = AnalysisPlaybook.from_dict(pb.to_dict())
    assert pb2.name == "DD стандарт"
    assert len(pb2.dimensions) == 2
    assert pb2.dimensions[0].key == "debt"
    assert pb2.company_context["custom"] == "Мы — PE-фонд"
    assert pb2.reference_report.startswith("# Пример")


def test_playbook_new_id_format():
    assert AnalysisPlaybook.new_id().startswith("apb_")
    assert AnalysisRun.new_id().startswith("arun_")


def test_run_add_step_and_terminal():
    r = AnalysisRun(id=AnalysisRun.new_id(), playbook_id="apb_x")
    r.add_step("parsing", "parsed doc", {"pages": 100})
    assert len(r.steps_log) == 1
    assert r.steps_log[0]["phase"] == "parsing"
    assert r.steps_log[0]["detail"]["pages"] == 100
    assert "ts" in r.steps_log[0]
    assert not r.is_terminal()
    r.status = RunStatus.DONE.value
    assert r.is_terminal()


def test_run_roundtrip():
    r = AnalysisRun(
        id=AnalysisRun.new_id(), playbook_id="apb_1",
        org_id="org1", user_id="u1",
        input_document_ids=["doc1", "doc2"],
        client_context="Обсудили рост на 20%",
        extracted_facts={"debt": [{"value": 100}]},
        computed_metrics={"debt_load": {"value": 1.5}},
    )
    r2 = AnalysisRun.from_dict(r.to_dict())
    assert r2.input_document_ids == ["doc1", "doc2"]
    assert r2.client_context == "Обсудили рост на 20%"
    assert r2.extracted_facts["debt"][0]["value"] == 100
    assert r2.computed_metrics["debt_load"]["value"] == 1.5


# =============================================================================
# Store (in-memory fallback)
# =============================================================================


def test_playbook_store_crud():
    store = PlaybookStore(postgres=None)
    pb = AnalysisPlaybook(id="", name="X", org_id="org1", created_by="u1")
    saved = _run(store.create(pb))
    assert saved.id.startswith("apb_")
    assert saved.created_at

    got = _run(store.get(saved.id))
    assert got is not None and got.name == "X"

    saved.name = "Y"
    _run(store.update(saved))
    got2 = _run(store.get(saved.id))
    assert got2.name == "Y"

    listed = _run(store.list_for_org("org1"))
    assert any(p.id == saved.id for p in listed)

    assert _run(store.delete(saved.id)) is True
    assert _run(store.get(saved.id)) is None


def test_playbook_store_org_isolation():
    store = PlaybookStore(postgres=None)
    _run(store.create(AnalysisPlaybook(id="", name="A", org_id="orgA", created_by="u")))
    _run(store.create(AnalysisPlaybook(id="", name="B", org_id="orgB", created_by="u")))
    org_a = _run(store.list_for_org("orgA"))
    assert len(org_a) == 1 and org_a[0].name == "A"


def test_run_store_crud():
    store = RunStore(postgres=None)
    r = AnalysisRun(id="", playbook_id="apb_1", org_id="org1", user_id="u1",
                    input_document_ids=["d1"])
    saved = _run(store.create(r))
    assert saved.id.startswith("arun_")

    saved.status = RunStatus.DONE.value
    saved.final_report = {"markdown": "# Report"}
    _run(store.save(saved))

    got = _run(store.get(saved.id))
    assert got.status == "done"
    assert got.final_report["markdown"] == "# Report"
    assert got.completed_at  # выставлен при terminal


def test_run_store_list_sorted_and_scoped():
    store = RunStore(postgres=None)
    for i in range(3):
        _run(store.create(AnalysisRun(id="", playbook_id="p", org_id="org1",
                                      user_id="u", input_document_ids=["d"])))
    _run(store.create(AnalysisRun(id="", playbook_id="p", org_id="other",
                                  user_id="u", input_document_ids=["d"])))
    runs = _run(store.list_for_org("org1"))
    assert len(runs) == 3


# =============================================================================
# Endpoints
# =============================================================================


def _load_route():
    """Загрузить analysis route напрямую (обход autogen-issue в routes/__init__)."""
    if "backend.api" not in sys.modules:
        m = types.ModuleType("backend.api")
        m.__path__ = [str(_ROOT / "backend" / "api")]
        sys.modules["backend.api"] = m
    if "backend.api.routes" not in sys.modules:
        m = types.ModuleType("backend.api.routes")
        m.__path__ = [str(_ROOT / "backend" / "api" / "routes")]
        sys.modules["backend.api.routes"] = m
    path = _ROOT / "backend" / "api" / "routes" / "analysis.py"
    spec = importlib.util.spec_from_file_location("backend.api.routes.analysis", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backend.api.routes.analysis"] = mod
    spec.loader.exec_module(mod)
    return mod


analysis_routes = _load_route()


@pytest.fixture(autouse=True)
def _stub_deps(monkeypatch):
    """Postgres → None (in-memory store); membership → solo; RBAC → INTERNAL.

    Важно: store-фабрики в route создают НОВЫЙ store на каждый вызов,
    а in-memory dict у каждого свой. Чтобы CRUD-тесты видели общие данные,
    подменяем фабрики на синглтоны.
    """
    shared_pb = PlaybookStore(postgres=None)
    shared_run = RunStore(postgres=None)

    async def _pb():
        return shared_pb

    async def _rs():
        return shared_run

    monkeypatch.setattr(analysis_routes, "_playbook_store", _pb)
    monkeypatch.setattr(analysis_routes, "_run_store", _rs)

    # membership → org by user (configurable per test через _ORG map)
    monkeypatch.setattr(analysis_routes, "_org_for", lambda uid: _ORG.get(uid))
    monkeypatch.setattr(analysis_routes, "_max_level_for", lambda uid: 2)

    # Изолируем CRUD-тесты от run_engine: dispatch → no-op (движок тестим
    # отдельно в test_analysis_phase_b). Подменяем в sys.modules, т.к.
    # route делает lazy import внутри create_run.
    fake_engine = types.ModuleType("backend.core.analysis.run_engine")

    async def _noop_dispatch(run_id, **kw):
        return None

    async def _noop_dispatch_bg(run_id, **kw):
        return "asyncio"  # имитируем фоновый канал

    fake_engine.dispatch_run = _noop_dispatch
    fake_engine.dispatch_run_background = _noop_dispatch_bg
    monkeypatch.setitem(sys.modules, "backend.core.analysis.run_engine", fake_engine)
    return {"pb": shared_pb, "run": shared_run}


_ORG: dict[str, str] = {}


def _token(user_id: str) -> str:
    return "Bearer " + jwt.encode({"sub": user_id}, "s", algorithm="HS256")


def _call(handler, **kwargs):
    fn = getattr(handler, "fn", handler)
    sig = inspect.signature(fn)
    for name, p in sig.parameters.items():
        if name in kwargs or p.default is inspect.Parameter.empty:
            continue
        kwargs[name] = getattr(p.default, "default", p.default)
    return asyncio.run(fn(**kwargs))


def test_create_playbook_requires_name():
    with pytest.raises(HTTPException) as e:
        _call(analysis_routes.create_playbook, data={}, authorization=_token(_uid()))
    assert e.value.status_code == 400


def test_create_and_get_playbook():
    user = _uid()
    res = _call(analysis_routes.create_playbook,
                data={"name": "DD", "analysis_type": "due_diligence",
                      "dimensions": [{"key": "debt", "label": "Долг"}]},
                authorization=_token(user))
    assert res["name"] == "DD"
    assert res["created_by"] == user
    pid = res["id"]

    got = _call(analysis_routes.get_playbook, playbook_id=pid, authorization=_token(user))
    assert got["id"] == pid
    assert len(got["dimensions"]) == 1


def test_get_playbook_404():
    with pytest.raises(HTTPException) as e:
        _call(analysis_routes.get_playbook, playbook_id="apb_nope",
              authorization=_token(_uid()))
    assert e.value.status_code == 404


def test_list_playbooks_org_scoped():
    owner = _uid()
    _ORG[owner] = "orgX"
    _call(analysis_routes.create_playbook, data={"name": "P1"}, authorization=_token(owner))
    _call(analysis_routes.create_playbook, data={"name": "P2"}, authorization=_token(owner))
    res = _call(analysis_routes.list_playbooks, authorization=_token(owner))
    assert res["count"] >= 2


def test_update_playbook():
    user = _uid()
    res = _call(analysis_routes.create_playbook, data={"name": "Old"},
                authorization=_token(user))
    pid = res["id"]
    upd = _call(analysis_routes.update_playbook, playbook_id=pid,
                data={"name": "New", "description": "desc"},
                authorization=_token(user))
    assert upd["name"] == "New"
    assert upd["description"] == "desc"


def test_delete_playbook():
    user = _uid()
    res = _call(analysis_routes.create_playbook, data={"name": "X"},
                authorization=_token(user))
    pid = res["id"]
    d = _call(analysis_routes.delete_playbook, playbook_id=pid, authorization=_token(user))
    assert d["deleted"] is True
    with pytest.raises(HTTPException):
        _call(analysis_routes.get_playbook, playbook_id=pid, authorization=_token(user))


def test_rbac_other_user_cannot_see_high_level_playbook(monkeypatch):
    """Playbook с access_level=4 не виден employee (max=2) из той же org."""
    owner = _uid()
    other = _uid()
    _ORG[owner] = "orgZ"
    _ORG[other] = "orgZ"
    res = _call(analysis_routes.create_playbook,
                data={"name": "Secret", "access_level": 4},
                authorization=_token(owner))
    pid = res["id"]
    # other (max_level=2) не видит RESTRICTED(4) playbook.
    with pytest.raises(HTTPException) as e:
        _call(analysis_routes.get_playbook, playbook_id=pid, authorization=_token(other))
    assert e.value.status_code == 403


# === Run endpoints ===


def test_create_run_requires_documents():
    user = _uid()
    pb = _call(analysis_routes.create_playbook, data={"name": "P"}, authorization=_token(user))
    with pytest.raises(HTTPException) as e:
        _call(analysis_routes.create_run,
              data={"playbook_id": pb["id"], "document_ids": []},
              authorization=_token(user))
    assert e.value.status_code == 400


def test_create_run_unknown_playbook_404():
    with pytest.raises(HTTPException) as e:
        _call(analysis_routes.create_run,
              data={"playbook_id": "apb_nope", "document_ids": ["d1"]},
              authorization=_token(_uid()))
    assert e.value.status_code == 404


def test_create_run_happy_path():
    user = _uid()
    pb = _call(analysis_routes.create_playbook, data={"name": "P"}, authorization=_token(user))
    run = _call(analysis_routes.create_run,
                data={"playbook_id": pb["id"], "document_ids": ["doc1", "doc2"],
                      "client_context": "рост 20%"},
                authorization=_token(user))
    assert run["status"] == "pending"
    assert run["input_document_ids"] == ["doc1", "doc2"]
    assert run["client_context"] == "рост 20%"
    assert len(run["steps_log"]) >= 1  # "created" step
    # По умолчанию — фоновый режим (dispatch_background замокан)
    assert run["dispatched"] is True
    assert run["mode"] == "background"
    assert run["dispatch_channel"] == "asyncio"


def test_create_run_wait_mode_sync():
    """wait=true → синхронный режим (mode=sync)."""
    user = _uid()
    pb = _call(analysis_routes.create_playbook, data={"name": "P"}, authorization=_token(user))
    run = _call(analysis_routes.create_run,
                data={"playbook_id": pb["id"], "document_ids": ["doc1"]},
                authorization=_token(user), wait=True)
    assert run["mode"] == "sync"
    assert run["dispatched"] is True


def test_get_run_and_list():
    user = _uid()
    _ORG[user] = "orgR"
    pb = _call(analysis_routes.create_playbook, data={"name": "P"}, authorization=_token(user))
    run = _call(analysis_routes.create_run,
                data={"playbook_id": pb["id"], "document_ids": ["d1"]},
                authorization=_token(user))
    got = _call(analysis_routes.get_run, run_id=run["id"], authorization=_token(user))
    assert got["id"] == run["id"]

    listed = _call(analysis_routes.list_runs, authorization=_token(user))
    assert listed["count"] >= 1
    assert any(r["id"] == run["id"] for r in listed["runs"])


def test_get_run_report_not_ready_409():
    """Run без final_report → 409."""
    user = _uid()
    pb = _call(analysis_routes.create_playbook, data={"name": "P"}, authorization=_token(user))
    run = _call(analysis_routes.create_run,
                data={"playbook_id": pb["id"], "document_ids": ["d1"]},
                authorization=_token(user))
    with pytest.raises(HTTPException) as e:
        _call(analysis_routes.get_run_report, run_id=run["id"], authorization=_token(user))
    assert e.value.status_code == 409


def test_get_run_report_markdown_and_json(_stub_deps):
    """Run с final_report → markdown + json форматы."""
    user = _uid()
    pb = _call(analysis_routes.create_playbook, data={"name": "P"}, authorization=_token(user))
    run = _call(analysis_routes.create_run,
                data={"playbook_id": pb["id"], "document_ids": ["d1"]},
                authorization=_token(user))
    # Вручную выставим final_report в shared run store.
    stored = _run(_stub_deps["run"].get(run["id"]))
    stored.final_report = {"markdown": "# Отчёт", "verdict": "ok",
                           "severity_summary": {"overall": "ok"}, "generated_by": "llm"}
    _run(_stub_deps["run"].save(stored))

    md = _call(analysis_routes.get_run_report, run_id=run["id"], authorization=_token(user))
    assert md["format"] == "markdown"
    assert md["markdown"] == "# Отчёт"
    assert md["verdict"] == "ok"

    js = _call(analysis_routes.get_run_report, run_id=run["id"],
               authorization=_token(user), fmt="json")
    assert js["format"] == "json"
    assert js["report"]["generated_by"] == "llm"


def test_get_run_report_404():
    with pytest.raises(HTTPException) as e:
        _call(analysis_routes.get_run_report, run_id="arun_nope", authorization=_token(_uid()))
    assert e.value.status_code == 404


# === Версионирование playbook ===


def test_playbook_version_starts_at_1():
    pb = AnalysisPlaybook(id="", name="X")
    assert pb.version == 1


def test_playbook_update_increments_version():
    store = PlaybookStore(postgres=None)
    pb = _run(store.create(AnalysisPlaybook(id="", name="X", created_by="u")))
    assert pb.version == 1
    pb.name = "Y"
    updated = _run(store.update(pb))
    assert updated.version == 2
    updated.name = "Z"
    updated2 = _run(store.update(updated))
    assert updated2.version == 3


def test_run_snapshots_playbook_version():
    """Run фиксирует version playbook'а на момент запуска."""
    user = _uid()
    pb = _call(analysis_routes.create_playbook, data={"name": "P"}, authorization=_token(user))
    # Обновим playbook → version=2.
    _call(analysis_routes.update_playbook, playbook_id=pb["id"],
          data={"name": "P2"}, authorization=_token(user))
    # Запустим run → должен зафиксировать version=2.
    run = _call(analysis_routes.create_run,
                data={"playbook_id": pb["id"], "document_ids": ["d1"]},
                authorization=_token(user))
    assert run["playbook_version"] == 2


def test_playbook_version_roundtrip():
    pb = AnalysisPlaybook(id="apb_x", name="X", version=5)
    pb2 = AnalysisPlaybook.from_dict(pb.to_dict())
    assert pb2.version == 5


# === Bundled templates + validation endpoints ===


def test_list_templates_endpoint():
    res = _call(analysis_routes.list_playbook_templates, authorization=_token(_uid()))
    assert len(res["templates"]) >= 3


def test_create_from_template():
    user = _uid()
    res = _call(analysis_routes.create_playbook_from_template,
                data={"template_id": "due_diligence_standard"},
                authorization=_token(user))
    assert res["created_from_template"] == "due_diligence_standard"
    assert res["name"] == "Due Diligence (стандарт)"
    assert len(res["dimensions"]) == 4
    assert res["created_by"] == user


def test_create_from_template_with_name_override():
    user = _uid()
    res = _call(analysis_routes.create_playbook_from_template,
                data={"template_id": "financial_quarter", "name": "Мой финразбор"},
                authorization=_token(user))
    assert res["name"] == "Мой финразбор"


def test_create_from_template_unknown_404():
    with pytest.raises(HTTPException) as e:
        _call(analysis_routes.create_playbook_from_template,
              data={"template_id": "nope"}, authorization=_token(_uid()))
    assert e.value.status_code == 404


def test_create_playbook_invalid_computation_400():
    """Валидация: битая формула → 400 с деталями."""
    with pytest.raises(HTTPException) as e:
        _call(analysis_routes.create_playbook,
              data={"name": "Bad",
                    "dimensions": [{"key": "x", "label": "X", "kind": "quantitative",
                                    "computation": "__import__('os')", "inputs": ["a"]}]},
              authorization=_token(_uid()))
    assert e.value.status_code == 400


def test_create_playbook_duplicate_key_400():
    with pytest.raises(HTTPException) as e:
        _call(analysis_routes.create_playbook,
              data={"name": "Dup",
                    "dimensions": [{"key": "k", "label": "A"}, {"key": "k", "label": "B"}]},
              authorization=_token(_uid()))
    assert e.value.status_code == 400
