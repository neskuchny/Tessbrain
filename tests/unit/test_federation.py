# -*- coding: utf-8 -*-
"""Федеративная связь между организациями: рукопожатие и границы.

Шина умела отдать срез памяти внешнему потребителю по ключу — это
односторонняя выдача. «Компании обмениваются контекстом» — это отношение:
обе стороны есть в системе, каждая сама решает, что открывает, и ни одна
не может выдать себе доступ к другой.

Проверяются правила, ради которых модуль и написан:
  1. рукопожатие обязательно — принять может ТОЛЬКО вторая сторона;
  2. доступ по умолчанию закрыт: нет связи / нет согласия / сторона не
     объявила срез — доступа нет, и причина называется словами;
  3. разрыв односторонний и мгновенный;
  4. разорванная связь не воскресает;
  5. связь только между разными организациями, одна активная на пару;
  6. заводить и рвать может только основатель или администратор.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name: str, relpath: str):
    if name in sys.modules:
        return sys.modules[name]
    for pkg in ("backend", "backend.core", "backend.core.data_bus"):
        if pkg not in sys.modules:
            mod = types.ModuleType(pkg)
            mod.__path__ = [os.path.join(ROOT, *pkg.split("."))]
            sys.modules[pkg] = mod
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_f = _load("backend.core.data_bus.federation",
           "backend/core/data_bus/federation.py")


def _proposed(policy_id: str = ""):
    res = _f.propose_link(org_a="org-a", org_b="org-b",
                          proposed_by_user="u-a", role="admin",
                          purpose="совместный проект", policy_id=policy_id)
    assert res["ok"], res
    return res["link"]


def _active():
    link = _proposed()
    assert _f.decide_link(link, decider_org="org-b", decider_user="u-b",
                          role="admin", accept=True)["ok"]
    return link


# ── 1. Рукопожатие ──────────────────────────────────────────────────────

def test_proposer_cannot_accept_own_proposal():
    """Ядро правила: иначе связь была бы односторонним захватом."""
    link = _proposed()
    res = _f.decide_link(link, decider_org="org-a", decider_user="u-a",
                         role="admin", accept=True)
    assert not res["ok"]
    assert "только вторая сторона" in res["error"]
    assert link.status == _f.PROPOSED, "состояние не должно измениться"
    print("✅ предложивший не может принять сам себя")


def test_second_side_accepts():
    link = _active()
    assert link.status == _f.ACTIVE
    assert link.decided_by_user == "u-b"
    print("✅ вторая сторона принимает — связь становится действующей")


def test_outsider_cannot_decide():
    link = _proposed()
    res = _f.decide_link(link, decider_org="org-c", decider_user="u-c",
                         role="admin", accept=True)
    assert not res["ok"] and "не участвует" in res["error"]
    print("✅ посторонняя организация не может решать за стороны")


def test_only_admins_can_propose_and_decide():
    res = _f.propose_link(org_a="org-a", org_b="org-b",
                          proposed_by_user="u", role="employee")
    assert not res["ok"] and "только основатель или администратор" in res["error"]
    link = _proposed()
    bad = _f.decide_link(link, decider_org="org-b", decider_user="u-b",
                         role="employee", accept=True)
    assert not bad["ok"]
    print("✅ обмен данными между компаниями заводят только руководители")


def test_self_link_rejected():
    res = _f.propose_link(org_a="org-a", org_b="org-a",
                          proposed_by_user="u", role="admin")
    assert not res["ok"] and "с самой собой" in res["error"]
    print("✅ связь с самой собой отвергается")


# ── 2. Доступ закрыт по умолчанию ───────────────────────────────────────

def test_no_link_no_access():
    d = _f.resolve_access(None, requester_org="org-a", target_org="org-b")
    assert d["allowed"] is False
    assert "нет связи" in d["reason"]
    print("✅ нет связи — нет доступа")


def test_proposed_link_gives_no_access():
    """До согласия второй стороны доступа нет — это и есть смысл согласия."""
    link = _proposed(policy_id="pol-a")
    d = _f.resolve_access(link, requester_org="org-b", target_org="org-a")
    assert d["allowed"] is False and "не действует" in d["reason"]
    print("✅ предложенная, но не принятая связь доступа не даёт")


def test_active_link_without_policy_gives_no_access():
    """Согласие ≠ выдача: сторона отдаёт только объявленное."""
    link = _active()
    d = _f.resolve_access(link, requester_org="org-b", target_org="org-a")
    assert d["allowed"] is False
    assert "не объявила" in d["reason"]
    print("✅ действующая связь без объявленного среза не открывает данные")


def test_access_granted_only_for_declared_side():
    """Асимметрия: A объявила срез, B — нет. A отдаёт, B не отдаёт."""
    link = _active()
    assert _f.set_side_policy(link, org_id="org-a", role="admin",
                              policy_id="pol-a")["ok"]
    to_a = _f.resolve_access(link, requester_org="org-b", target_org="org-a")
    to_b = _f.resolve_access(link, requester_org="org-a", target_org="org-b")
    assert to_a["allowed"] is True and to_a["policy_id"] == "pol-a"
    assert to_b["allowed"] is False, (
        "сторона, не объявившая срез, ничего не отдаёт"
    )
    print("✅ каждая сторона отдаёт ровно то, что объявила сама")


def test_side_cannot_set_foreign_policy():
    link = _active()
    res = _f.set_side_policy(link, org_id="org-c", role="admin",
                             policy_id="pol-x")
    assert not res["ok"]
    print("✅ чужой срез не настраивается")


def test_empty_policy_closes_output_without_breaking_link():
    link = _active()
    _f.set_side_policy(link, org_id="org-a", role="admin", policy_id="pol-a")
    _f.set_side_policy(link, org_id="org-a", role="admin", policy_id="")
    d = _f.resolve_access(link, requester_org="org-b", target_org="org-a")
    assert d["allowed"] is False
    assert link.status == _f.ACTIVE, "связь остаётся, закрыта только выдача"
    print("✅ пустой срез закрывает выдачу, не разрывая связь")


# ── 3-4. Разрыв ─────────────────────────────────────────────────────────

def test_either_side_can_revoke_immediately():
    link = _active()
    _f.set_side_policy(link, org_id="org-a", role="admin", policy_id="pol-a")
    res = _f.revoke_link(link, org_id="org-b", role="admin", reason="закончили")
    assert res["ok"] and link.status == _f.REVOKED
    d = _f.resolve_access(link, requester_org="org-b", target_org="org-a")
    assert d["allowed"] is False, "после разрыва доступ пропадает сразу"
    print("✅ разрыв односторонний и мгновенный")


def test_revoked_link_cannot_be_resurrected():
    """Иначе отзыв доступа не был бы отзывом."""
    link = _active()
    _f.revoke_link(link, org_id="org-a", role="admin")
    for res in (
        _f.decide_link(link, decider_org="org-b", decider_user="u-b",
                       role="admin", accept=True),
        _f.set_side_policy(link, org_id="org-a", role="admin",
                           policy_id="pol-a"),
        _f.revoke_link(link, org_id="org-a", role="admin"),
    ):
        assert not res["ok"], "закрытая связь не должна меняться"
    assert link.status == _f.REVOKED
    print("✅ разорванная связь не воскресает — нужна новая")


def test_rejected_link_is_terminal():
    link = _proposed()
    _f.decide_link(link, decider_org="org-b", decider_user="u-b",
                   role="admin", accept=False)
    assert link.status == _f.REJECTED
    again = _f.decide_link(link, decider_org="org-b", decider_user="u-b",
                           role="admin", accept=True)
    assert not again["ok"]
    print("✅ отклонённое предложение не переигрывается")


# ── 5. Хранилище: одна незакрытая связь на пару ─────────────────────────

def _store():
    _stub_io()
    fs = _load("backend.core.data_bus.federation_store",
               "backend/core/data_bus/federation_store.py")
    path = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    os.unlink(path)
    return fs.FederationStore(path)


def _stub_io():
    """tenant_io без пакета store: его __init__ тянет numpy."""
    import contextlib
    import json as _json
    if "backend.core.store" not in sys.modules:
        pkg = types.ModuleType("backend.core.store")
        pkg.__path__ = [os.path.join(ROOT, "backend", "core", "store")]
        sys.modules["backend.core.store"] = pkg
    tio = types.ModuleType("backend.core.store.tenant_io")

    @contextlib.contextmanager
    def file_lock(path):
        yield

    def atomic_write_json(path, data):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False)

    tio.file_lock = file_lock
    tio.atomic_write_json = atomic_write_json
    sys.modules["backend.core.store.tenant_io"] = tio


def test_one_open_link_per_pair():
    s = _store()
    assert s.add(_proposed())["ok"]
    dup = s.add(_proposed())
    assert not dup["ok"] and "уже есть" in dup["error"]
    print("✅ вторая незакрытая связь на ту же пару не заводится")


def test_pair_key_is_order_independent():
    s = _store()
    s.add(_proposed())
    # та же пара в обратном порядке
    other = _f.propose_link(org_a="org-b", org_b="org-a",
                            proposed_by_user="u-b", role="admin")["link"]
    assert not s.add(other)["ok"], "A↔B и B↔A — одна пара"
    print("✅ порядок организаций не создаёт вторую связь")


def test_new_link_allowed_after_revoke():
    s = _store()
    link = _proposed()
    s.add(link)
    _f.revoke_link(link, org_id="org-a", role="admin")
    s.save(link)
    assert s.add(_proposed())["ok"], (
        "после разрыва можно договориться заново — новой связью"
    )
    print("✅ после разрыва пара может договориться заново")


def test_closed_links_kept_for_audit():
    s = _store()
    link = _proposed()
    s.add(link)
    _f.revoke_link(link, org_id="org-a", role="admin")
    s.save(link)
    assert s.list_for_org("org-a") == [], "закрытые не в основном списке"
    history = s.list_for_org("org-a", include_closed=True)
    assert len(history) == 1 and history[0]["status"] == _f.REVOKED
    print("✅ история отношений сохраняется для аудита")


def test_active_link_lookup():
    s = _store()
    link = _proposed()
    s.add(link)
    assert s.active_link_between("org-a", "org-b") is None, "ещё не принята"
    _f.decide_link(link, decider_org="org-b", decider_user="u-b",
                   role="admin", accept=True)
    s.save(link)
    assert s.active_link_between("org-b", "org-a") is not None
    print("✅ действующая связь находится по любой стороне")


# ── Роуты ───────────────────────────────────────────────────────────────

def test_routes_take_identity_from_membership():
    src = open(os.path.join(ROOT, "backend/api/routes/federation.py"),
               encoding="utf-8").read()
    assert "get_org_for_user" in src and "get_user_org_base_role" in src, (
        "организация и роль обязаны браться из членства, а не из тела запроса"
    )
    body = src[src.index("class ProposeBody"):src.index("@get(\"/links\")")]
    # partner_org_id — это ЦЕЛЬ связи, он допустим. Недопустимы поля,
    # задающие, ОТ ЧЬЕГО имени и С КАКИМИ правами действует запрос.
    import re as _re
    fields = set(_re.findall(r"^\s{4}(\w+)\s*:", body, _re.M))
    for forbidden in ("org_id", "org", "role", "acting_org", "as_org"):
        assert forbidden not in fields, (
            f"поле {forbidden!r} в теле запроса позволило бы действовать "
            f"от чужого имени"
        )
    assert "partner_org_id" in fields, "цель связи задаётся явно"
    app = open(os.path.join(ROOT, "backend/api/app.py"), encoding="utf-8").read()
    assert "federation.router" in app
    print("✅ личность и права берутся из членства, роутер подключён")


# ── Федеративный запрос через общий конвейер ────────────────────────────

def test_federated_query_uses_shared_pipeline():
    """Второй путь через границу разошёлся бы с первым — конвейер один."""
    src = open(os.path.join(ROOT, "backend/core/data_bus/bus_service.py"),
               encoding="utf-8").read()
    assert "async def _run_pipeline(" in src
    fq = src[src.index("async def federated_query("):]
    fq = fq[:fq.index("async def get_snapshot(")]
    assert "_run_pipeline(" in fq, (
        "федеративный запрос обязан идти через общий конвейер сит"
    )
    assert "resolve_access" in fq, "доступ решает отношение, а не ключ"
    assert "check_and_increment" in fq, (
        "квоты политики действуют и на федеративный путь"
    )
    # и ключевой путь тоже через конвейер
    qi = src[src.index("async def _query_inner("):src.index("async def _run_pipeline(")]
    assert "_run_pipeline(" in qi
    print("✅ оба пути — ключ и связь — идут через один конвейер")


def test_federated_query_fails_closed_on_missing_policy():
    """Связь ссылается на несуществующую политику → отказ, а не «всё»."""
    src = open(os.path.join(ROOT, "backend/core/data_bus/bus_service.py"),
               encoding="utf-8").read()
    fq = src[src.index("async def federated_query("):]
    fq = fq[:fq.index("async def get_snapshot(")]
    assert "политика не найдена" in fq and '"code": 403' in fq, (
        "ошибка конфигурации целевой стороны не должна открывать данные"
    )
    print("✅ несуществующая политика → отказ, а не открытая дверь")


def test_federated_audit_names_requester():
    """Журнал целевой стороны показывает, КАКАЯ компания спрашивала."""
    src = open(os.path.join(ROOT, "backend/core/data_bus/bus_service.py"),
               encoding="utf-8").read()
    fq = src[src.index("async def federated_query("):]
    fq = fq[:fq.index("async def get_snapshot(")]
    assert 'tenant_id=str(target_org)' in fq, "данные — целевой организации"
    assert 'Федерация: {requester_org}' in fq, (
        "имя consumer'а обязано называть запрашивающую организацию"
    )
    print("✅ аудит целевой стороны называет запрашивающую компанию")


def test_query_route_registered():
    src = open(os.path.join(ROOT, "backend/api/routes/federation.py"),
               encoding="utf-8").read()
    assert "async def federated_query(" in src
    router = src[src.index("router = Router("):]
    assert "federated_query" in router
    # личность — из членства, цель — параметром
    body = src[src.index("async def federated_query("):]
    body = body[:body.index("router = Router(")]
    assert "_actor(uid)" in body
    assert 'requester_org=actor["org"]' in body, (
        "запрашивающая организация берётся из членства, не из параметра"
    )
    print("✅ роут /federation/query подключён, личность из членства")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе тесты федерации прошли.")
