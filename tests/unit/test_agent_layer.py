# -*- coding: utf-8 -*-
"""Слой внешних агентов: реестр, задачи, машинная приёмка.

Закрывает два разрыва из бизнес-карты: «самостоятельной регистрации
чужого агента нет» и «нет автоматической приёмки их результата».

Контракты под проверкой:
  1. агент без привязки к каналу не регистрируется — анонимных
     исполнителей нет; собственных прав агент не получает;
  2. списанный агент не возвращается — новое доверие = новая регистрация;
  3. задача адресная: чужой агент её не берёт и не сдаёт;
  4. приёмка трёхисходная, «нет доказательств ≠ готово»: без проверок
     результат не принимается автоматически;
  5. провал возвращает задачу с КОНКРЕТНЫМИ замечаниями; после предела
     возвратов автопересдача останавливается — решает человек;
  6. финальное закрытие всегда за человеком.
"""
from __future__ import annotations

import importlib.util
import os
import sys
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


_a = _load("backend.core.data_bus.agent_layer",
           "backend/core/data_bus/agent_layer.py")


def _agent():
    return _a.register_agent(
        org_id="org-1", name="Лендинг-бот", channel_kind="consumer",
        channel_id="dbc_123", role="admin", registered_by="u-1",
        capabilities=["landing", "copy"])["agent"]


def _task(agent=None, acceptance=None):
    agent = agent or _agent()
    res = _a.offer_task(
        org_id="org-1", agent=agent, title="Лендинг для запуска",
        spec_text="Собрать лендинг по позиционированию из памяти компании.",
        created_by="u-1", acceptance=acceptance)
    assert res["ok"], res
    return agent, res["task"]


# ── 1-2. Реестр ─────────────────────────────────────────────────────────

def test_agent_requires_channel():
    res = _a.register_agent(org_id="org-1", name="Бот", channel_kind="consumer",
                            channel_id="", role="admin", registered_by="u")
    assert not res["ok"] and "анонимных исполнителей нет" in res["error"]
    print("✅ агент без канала не регистрируется")


def test_agent_requires_admin():
    res = _a.register_agent(org_id="org-1", name="Бот", channel_kind="consumer",
                            channel_id="dbc_1", role="employee",
                            registered_by="u")
    assert not res["ok"]
    print("✅ регистрирует только основатель или администратор")


def test_retired_agent_stays_retired():
    """Возобновление доверия — новая регистрация, не смена флага."""
    agent = _agent()
    assert _a.set_agent_status(agent, role="admin",
                               status=_a.AGENT_RETIRED)["ok"]
    res = _a.set_agent_status(agent, role="admin", status=_a.AGENT_ACTIVE)
    assert not res["ok"] and "не возвращается" in res["error"]
    print("✅ списанный агент не воскресает")


def test_suspended_agent_gets_no_tasks():
    agent = _agent()
    _a.set_agent_status(agent, role="admin", status=_a.AGENT_SUSPENDED)
    res = _a.offer_task(org_id="org-1", agent=agent, title="Задача",
                        spec_text="Достаточно длинное описание задачи.",
                        created_by="u-1")
    assert not res["ok"]
    print("✅ приостановленный агент задач не получает")


# ── 3. Адресность ───────────────────────────────────────────────────────

def test_foreign_agent_cannot_take_or_submit():
    _, task = _task()
    assert not _a.take_task(task, agent_id="agent_чужой")["ok"]
    task.status = _a.TASK_IN_PROGRESS
    res = _a.submit_result(task, agent_id="agent_чужой", result_text="готово")
    assert not res["ok"]
    print("✅ чужой агент не берёт и не сдаёт задачу")


def test_foreign_org_agent_gets_no_task():
    agent = _agent()
    res = _a.offer_task(org_id="org-2", agent=agent, title="Задача",
                        spec_text="Достаточно длинное описание задачи.",
                        created_by="u-2")
    assert not res["ok"] and "другой организации" in res["error"]
    print("✅ агенту чужой организации задача не предлагается")


# ── 4. Приёмка: нет доказательств ≠ готово ──────────────────────────────

def test_no_checks_means_inconclusive_not_accepted():
    """Главный принцип, перенесённый из Kanon."""
    agent, task = _task(acceptance=None)
    _a.take_task(task, agent_id=agent.id)
    res = _a.submit_result(task, agent_id=agent.id,
                           result_text="Вот готовый лендинг: ...")
    assert res["ok"]
    assert res["verdict"]["verdict"] == "inconclusive"
    assert task.status == _a.TASK_SUBMITTED, (
        "без проверок задача идёт к человеку, а не в accepted"
    )
    print("✅ без проверок — «не доказано», к человеку, не принято")


def test_passing_checks_accept_machine_side():
    agent, task = _task(acceptance=[
        {"kind": "contains", "target": "тариф"},
        {"kind": "min_len", "n": 30},
    ])
    _a.take_task(task, agent_id=agent.id)
    res = _a.submit_result(
        task, agent_id=agent.id,
        result_text="Лендинг: герой, выгоды, ТАРИФ и призыв к действию.")
    assert res["verdict"]["verdict"] == "pass"
    assert task.status == _a.TASK_ACCEPTED, "машина приняла — ждёт человека"
    print("✅ прошедшие проверки → машинное «принято», финал за человеком")


# ── 5. Возврат с замечаниями и предел пересдач ──────────────────────────

def test_fail_returns_with_concrete_remarks():
    agent, task = _task(acceptance=[
        {"kind": "contains", "target": "тариф"},
        {"kind": "contains", "target": "призыв"},
    ])
    _a.take_task(task, agent_id=agent.id)
    res = _a.submit_result(task, agent_id=agent.id,
                           result_text="Просто текст без нужных блоков.")
    assert res["verdict"]["verdict"] == "fail"
    assert task.status == _a.TASK_RETURNED
    failed = [c["detail"] for c in res["verdict"]["checks"]
              if c["verdict"] == "fail"]
    assert "тариф" in failed and "призыв" in failed, (
        "замечания обязаны называть, что именно не прошло"
    )
    # история несёт замечания
    assert any("тариф" in h.get("detail", "") for h in task.history)
    print("✅ провал возвращает задачу с конкретными замечаниями")


def test_returned_task_can_be_retaken_until_cap():
    agent, task = _task(acceptance=[{"kind": "contains", "target": "тариф"}])
    for i in range(_a.MAX_RETURNS):
        _a.take_task(task, agent_id=agent.id)
        _a.submit_result(task, agent_id=agent.id, result_text=f"мимо {i}")
    assert task.returns_count == _a.MAX_RETURNS
    assert task.status == _a.TASK_SUBMITTED, (
        "после предела возвратов — к человеку, а не бесконечная пересдача"
    )
    assert not _a.take_task(task, agent_id=agent.id)["ok"], (
        "автоматическая пересдача остановлена"
    )
    print("✅ после предела возвратов решает человек, а не цикл")


def test_regex_check_and_bad_regex_dropped():
    agent, task = _task(acceptance=[
        {"kind": "regex", "pattern": r"\d{2,}"},
        {"kind": "regex", "pattern": "[битый("},   # молча отбрасывается
    ])
    assert len(task.acceptance) == 1, "битый regex не должен стать проверкой"
    _a.take_task(task, agent_id=agent.id)
    res = _a.submit_result(task, agent_id=agent.id,
                           result_text="Конверсия выросла на 25 процентов")
    assert res["verdict"]["verdict"] == "pass"
    print("✅ regex-проверка работает, битый шаблон отброшен")


# ── 6. Финал за человеком ───────────────────────────────────────────────

def test_human_closes_finally():
    agent, task = _task(acceptance=[{"kind": "min_len", "n": 5}])
    _a.take_task(task, agent_id=agent.id)
    _a.submit_result(task, agent_id=agent.id, result_text="Готовый результат")
    assert task.status == _a.TASK_ACCEPTED
    res = _a.close_task(task, closed_by="u-boss", approve=True)
    assert res["ok"] and task.status == _a.TASK_CLOSED
    assert task.closed_by == "u-boss"
    # закрытую не переоткрыть
    assert not _a.close_task(task, closed_by="u-2", approve=False)["ok"]
    print("✅ финальное закрытие за человеком, закрытая не переоткрывается")


def test_empty_result_rejected():
    agent, task = _task()
    _a.take_task(task, agent_id=agent.id)
    res = _a.submit_result(task, agent_id=agent.id, result_text="   ")
    assert not res["ok"]
    print("✅ пустой результат не принимается")


# ── Роуты: две стороны аутентификации ───────────────────────────────────

def test_routes_separate_org_and_agent_auth():
    src = open(os.path.join(ROOT, "backend/api/routes/agent_layer.py"),
               encoding="utf-8").read()
    # агентские действия — только по ключу канала
    for h in ("async def agent_inbox", "async def take_task_route",
              "async def submit_task_route"):
        body = src[src.index(h):src.index(h) + 900]
        assert "_agent_from_key" in body, f"{h}: агент опознаётся по каналу"
    # регистрация проверяет, что канал существует и принадлежит организации
    reg = src[src.index("async def register_agent_route"):]
    reg = reg[:reg.index("async def agent_status")]
    assert "get_consumer" in reg and 'str(c.tenant_id) != actor["org"]' in reg, (
        "канал обязан принадлежать организации регистрирующего"
    )
    app = open(os.path.join(ROOT, "backend/api/app.py"), encoding="utf-8").read()
    assert "agent_layer.router" in app
    print("✅ организация — по членству, агент — по каналу; роутер подключён")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе тесты слоя агентов прошли.")
