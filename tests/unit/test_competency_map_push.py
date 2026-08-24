# -*- coding: utf-8 -*-
"""Исходящий пуш в карту компетенций — порядок, честность, fail-closed.

Проверяем ровно то, что подтвердила их сторона: отделы раньше людей,
external_id стабилен, cogni нормализуется из витков 1..4 в их 0..1,
fill_missing всегда false, люди без данных не пропадают молча.
"""
import asyncio
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.core.integrations import competency_map_push as cmp  # noqa: E402


def test_vitki_to_01_conversion():
    cogni = {"vitki": {
        "leadership": {"level": 4, "confidence": 0.7, "evidence": ["я решил сам"]},
        "professional": {"level": 1, "confidence": 0.5, "evidence": []},
        "technical": {"level": None, "confidence": 0.0, "evidence": []},
    }}
    signals, conf, ev = cmp.cogni_vitki_to_01(cogni)
    assert signals["leadership"] == 1.0     # (4-1)/3
    assert signals["professional"] == 0.0   # (1-1)/3
    assert "technical" not in signals, "нет уровня — нет ложной оценки 0.5"
    assert conf["leadership"] == 0.7
    assert ev["leadership"] == ["я решил сам"]
    assert cmp.cogni_vitki_to_01(None) == ({}, {}, {})


class _GB:
    def __init__(self, depts, persons):
        self._depts = depts
        self._persons = persons

    async def get_all_nodes_async(self, label=None, limit=5000,
                                  tenant_id=None, strict_tenant=False):
        if label == "Department":
            return self._depts
        if label == "Person":
            return self._persons
        return []

    async def close(self, save=False):
        pass


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.delenv("ENABLE_COMPETENCY_MAP_PUSH", raising=False)
    monkeypatch.setenv("COMPETENCY_MAP_URL", "https://competency.example")
    monkeypatch.setenv("COMPETENCY_MAP_TOKEN", "tok123")
    return tmp_path


def _patch_common(monkeypatch, *, members, cogni_by_person=None,
                  snapshot_by_person=None):
    gb = _GB(
        depts=[{"id": "dept_mkt", "name": "IT-отдел"}],
        persons=[{"id": "p1", "name": "Иван Петров"},
                 {"id": "p2", "name": "Мария Сидорова"}],
    )

    async def _mv(uid, use_networkx=None):
        return gb

    monkeypatch.setattr(
        "backend.core.store.graph_view.merged_graph_view_for_user", _mv)
    monkeypatch.setattr(
        "backend.core.ingest.membership.get_org_for_user", lambda uid: "org1")
    monkeypatch.setattr(
        "backend.core.ingest.membership.get_user_org_base_role",
        lambda uid: "founder")
    monkeypatch.setattr(
        "backend.core.ingest.membership.list_members", lambda org: members)

    cogni_by_person = cogni_by_person or {}

    async def _profile(person_entity_id, org_id, requester_uid):
        c = cogni_by_person.get(person_entity_id)
        return {"user_id": person_entity_id, "frame": None, "cogni": c} if c is not None else None

    monkeypatch.setattr(
        "backend.core.identity.person_profile.profile_for_person", _profile)

    snapshot_by_person = snapshot_by_person or {}

    class _Gen:
        # dry_run обязан быть read-only: код читает УЖЕ построенные снапшоты
        # из кэша, а не вызывает генерацию (она жжёт LLM на каждого человека)
        _person_snapshots = snapshot_by_person

        async def get_person_snapshot(self, pid, force_regenerate=False):
            raise AssertionError(
                "генерация снапшота внутри сборки payload'ов запрещена: "
                "это LLM-вызов на каждого сотрудника, в т.ч. на dry_run")

    monkeypatch.setattr(
        "backend.core.sleep.enhanced_snapshot.get_enhanced_snapshot_generator",
        lambda gb=None, user_id=None: _Gen())
    return gb


def test_departments_pushed_before_people(env, monkeypatch):
    _patch_common(monkeypatch, members=[
        {"user_id": "u2", "person_entity_id": "p2", "department": "IT-отдел"},
    ], cogni_by_person={"p2": {"vitki": {
        "leadership": {"level": 3, "confidence": 0.6, "evidence": []}}}})

    out = asyncio.run(cmp.push_to_competency_map("owner", dry_run=True))
    assert out["status"] == "dry_run"
    assert out["departments"][0]["kind"] == "department"
    assert out["departments"][0]["label"] == "IT-отдел", \
        ".title() ломал регистр: «IT-отдел» → «It-Отдел»"
    assert out["departments"][0]["external_id"] == "dept_mkt"
    person = out["people"][0]
    assert person["kind"] == "person" and person["external_id"] == "p2"
    assert person["parent"] == "dept_mkt", "parent должен резолвиться по имени отдела"
    assert person["label"] == "Мария Сидорова"
    assert person["fill_missing"] is False


def test_unstitched_person_is_skipped_with_reason(env, monkeypatch):
    _patch_common(monkeypatch, members=[
        {"user_id": "u3", "person_entity_id": None, "department": ""},
    ])
    out = asyncio.run(cmp.push_to_competency_map("owner", dry_run=True))
    assert out["people"] == []
    assert out["skipped"][0]["reason"] == "не подтверждена сшивка «это я»"


def test_no_data_person_is_skipped_not_silently(env, monkeypatch):
    _patch_common(monkeypatch, members=[
        {"user_id": "u1", "person_entity_id": "p1", "department": ""},
    ], cogni_by_person={})   # profile_for_person -> None (нет cogni)
    out = asyncio.run(cmp.push_to_competency_map("owner", dry_run=True))
    assert out["people"] == []
    assert "нет накопленных" in out["skipped"][0]["reason"]


def test_role_gate_refuses_non_manager(env, monkeypatch):
    _patch_common(monkeypatch, members=[])
    monkeypatch.setattr(
        "backend.core.ingest.membership.get_user_org_base_role",
        lambda uid: "employee")
    out = asyncio.run(cmp.push_to_competency_map("rando", dry_run=True))
    assert out["status"] == "error" and "роль" in out["message"]


def test_fail_closed_without_flag_but_dry_run_allowed(env, monkeypatch):
    _patch_common(monkeypatch, members=[])
    real = asyncio.run(cmp.push_to_competency_map("owner", dry_run=False))
    assert real["status"] == "disabled"
    preview = asyncio.run(cmp.push_to_competency_map("owner", dry_run=True))
    assert preview["status"] == "dry_run"


def test_actual_post_sends_departments_then_people(env, monkeypatch):
    monkeypatch.setenv("ENABLE_COMPETENCY_MAP_PUSH", "1")
    _patch_common(monkeypatch, members=[
        {"user_id": "u2", "person_entity_id": "p2", "department": "IT-отдел"},
    ], cogni_by_person={"p2": {"vitki": {
        "leadership": {"level": 3, "confidence": 0.6, "evidence": []}}}})

    calls = []

    class _Resp:
        status_code = 200

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            calls.append((url, json, headers))
            return _Resp()

    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: _Client())
    out = asyncio.run(cmp.push_to_competency_map("owner", dry_run=False))
    assert out["status"] == "success"
    assert len(calls) == 2, "отдел + человек"
    assert calls[0][1]["kind"] == "department", "отдел уходит первым"
    assert calls[1][1]["kind"] == "person"
    assert calls[0][0] == "https://competency.example/api/v1/ingest/tessent"
    assert calls[1][2]["Authorization"] == "Bearer tok123"
    assert [x["label"] for x in out["sent"]] == ["IT-отдел", "Мария Сидорова"]
    assert out["sent"][0]["external_id"] == "dept_mkt", "тёзок различаем по id"


class _PersonSnap:
    """PersonSnapshot как в enhanced_snapshot: роль, достижения, решения."""
    role = "Директор по маркетингу"
    recent_achievements = ["внедрил регламенты продаж"]
    decisions = [{"summary": "перешли на контент-маркетинг",
                  "category": "strategic"}]
    strengths = ["системное мышление"]


def test_facts_come_from_person_snapshot(env, monkeypatch):
    """Регрессия: facts обещаны карте компетенций как достижения/решения из
    PersonSnapshot. Раньше код брал цели из Persona — документ обещал одно,
    код делал другое."""
    _patch_common(monkeypatch, members=[
        {"user_id": "u2", "person_entity_id": "p2", "department": ""},
    ], cogni_by_person={"p2": {"vitki": {
        "leadership": {"level": 3, "confidence": 0.6, "evidence": []}}}},
        snapshot_by_person={"p2": _PersonSnap()})

    out = asyncio.run(cmp.push_to_competency_map("owner", dry_run=True))
    facts = out["people"][0]["facts"]
    assert facts[0] == "роль: Директор по маркетингу"
    assert "внедрил регламенты продаж" in facts
    assert "решение: перешли на контент-маркетинг" in facts
    assert "сильная сторона: системное мышление" in facts


def test_no_invented_marketing_signal(env, monkeypatch):
    """Отзыв обещания: поля signals в payload быть не должно — источника
    оценки человека по маркетингу у нас нет, выдумывать нельзя."""
    _patch_common(monkeypatch, members=[
        {"user_id": "u2", "person_entity_id": "p2", "department": ""},
    ], cogni_by_person={"p2": {"vitki": {
        "leadership": {"level": 3, "confidence": 0.6, "evidence": []}}}},
        snapshot_by_person={"p2": _PersonSnap()})

    out = asyncio.run(cmp.push_to_competency_map("owner", dry_run=True))
    person = out["people"][0]
    assert "signals" not in person, "signals без источника — выдумка"
    assert "cogni" in person, "cogni (реальные витки) при этом должен быть"


def test_person_with_snapshot_but_no_cogni_still_sent(env, monkeypatch):
    """Человек без cogni, но с фактами — уезжает (частичный профиль норма),
    а не попадает в skipped."""
    _patch_common(monkeypatch, members=[
        {"user_id": "u2", "person_entity_id": "p2", "department": ""},
    ], cogni_by_person={}, snapshot_by_person={"p2": _PersonSnap()})

    out = asyncio.run(cmp.push_to_competency_map("owner", dry_run=True))
    assert len(out["people"]) == 1
    assert "cogni" not in out["people"][0]
    assert out["people"][0]["facts"], "поехали одни facts — это допустимо"
    assert out["skipped"] == []
