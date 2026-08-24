# -*- coding: utf-8 -*-
"""Пульс исполнения, фаза 0-1: словарь статусов, ledger, детекторы, отчёт.

Проверяем механику виртуального трекинг-менеджера: дедуп задач между
источниками, статус-история между прогонами, событие просрочки один раз,
детекторы «без срока/владельца/висит месяц», сводка руководителю с
приоритетом «требует вмешательства».
"""
import asyncio
import pathlib
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.core.pulse import detectors, ledger  # noqa: E402
from backend.core.pulse.manager_report import compose_markdown  # noqa: E402
from backend.core.tasks.status_norm import normalize_status  # noqa: E402


# ── Единый словарь статусов ────────────────────────────────────────────────

def test_status_norm_covers_russian_trackers():
    """«выполнена»/«готово» из русских трекеров раньше считались открытыми."""
    for s in ("done", "Выполнена", "ГОТОВО", "closed", "cancelled", "отменена"):
        assert normalize_status(s) == "done", s
    for s in ("blocked", "заблокирована", "on hold"):
        assert normalize_status(s) == "blocked", s
    for s in ("in_progress", "в работе", "wip"):
        assert normalize_status(s) == "in_progress", s
    assert normalize_status("отложена") == "deferred"
    assert normalize_status("") == "todo"
    assert normalize_status("что-то странное") == "todo"


# ── Ledger: дедуп ──────────────────────────────────────────────────────────

def test_dedup_merges_same_task_across_sources():
    """Одна задача в графе и в Trello не должна считаться дважды."""
    merged = ledger.dedup_tasks([
        {"title": "Сделать лендинг для вебинара", "assignee": "Вася",
         "status": "todo", "source": "graph", "id": "g1"},
        {"title": "Сделать лендинг для вебинара!", "assignee": "Вася",
         "status": "done", "source": "trello", "tracker": "trello",
         "tracker_task_id": "abc", "deadline": "2026-07-30", "id": "t1"},
    ])
    assert len(merged) == 1, "дубль не схлопнулся"
    m = merged[0]
    assert len(m["sources"]) == 2
    assert m["tracker_task_id"] == "abc", "поля трекера должны домёрживаться"
    assert m["deadline"] == "2026-07-30"
    assert normalize_status(m["status"]) == "done", \
        "закрытый статус из трекера должен победить"


def test_dedup_respects_different_assignees():
    """Похожие задачи РАЗНЫХ людей — разные задачи."""
    merged = ledger.dedup_tasks([
        {"title": "Позвонить клиенту X", "assignee": "Вася", "status": "todo"},
        {"title": "Позвонить клиенту X", "assignee": "Петя", "status": "todo"},
    ])
    assert len(merged) == 2


# ── Ledger: статус-история и события ───────────────────────────────────────

def _run_ledger(monkeypatch, tmp_path, tasks_by_run):
    monkeypatch.setenv("PULSE_STATE_DIR", str(tmp_path))
    results = []
    for tasks in tasks_by_run:
        snapshot = [dict(t) for t in tasks]

        async def _graph(uid, _snap=snapshot):
            return _snap

        async def _trackers(uid):
            return []

        monkeypatch.setattr("backend.core.tasks.task_analysis.collect_tasks",
                            _graph)
        monkeypatch.setattr(
            "backend.core.tasks.task_analysis.collect_tasks_from_trackers",
            _trackers)
        results.append(asyncio.run(
            ledger.build_ledger("u-1", emit_events=False)))
    return results


def test_status_transition_recorded_and_completion_event(monkeypatch, tmp_path):
    """Прогон 1: todo. Прогон 2: done → история из двух записей + событие."""
    base = {"title": "Написать отчёт", "assignee": "Вася", "id": "x1"}
    r1, r2 = _run_ledger(monkeypatch, tmp_path, [
        [{**base, "status": "todo"}],
        [{**base, "status": "done"}],
    ])
    assert r1["events"] == []
    hist = r2["tasks"][0]["_pulse"]["status_history"]
    assert [h["status"] for h in hist] == ["todo", "done"]
    assert [e["type"] for e in r2["events"]] == ["task_completed"]


def test_overdue_event_fires_once(monkeypatch, tmp_path):
    """Просрочка — событие один раз, а не на каждом прогоне."""
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    task = {"title": "Просроченная", "assignee": "Петя", "id": "x2",
            "status": "todo", "deadline": yesterday}
    r1, r2 = _run_ledger(monkeypatch, tmp_path, [[task], [task]])
    assert [e["type"] for e in r1["events"]] == ["task_overdue"]
    assert r2["events"] == [], "повторный прогон не должен спамить событием"


# ── Детекторы ──────────────────────────────────────────────────────────────

def _mk(title, *, bucket="todo", assignee="Вася", deadline=None,
        idle_days=0):
    now = datetime.now(timezone.utc)
    return {"title": title, "assignee": assignee, "_pulse": {
        "bucket": bucket, "deadline": deadline,
        "last_activity_at": (now - timedelta(days=idle_days)).isoformat(),
        "status_history": []}}


def test_detectors_catalog():
    yesterday = (date.today() - timedelta(days=3)).isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    tasks = [
        _mk("Просрочена", deadline=yesterday),
        _mk("Без срока"),
        _mk("Без владельца", assignee="", deadline=tomorrow),
        _mk("Скоро дедлайн", deadline=tomorrow),
        _mk("Висит давно", idle_days=45),
        _mk("Заблокирована", bucket="blocked", deadline=tomorrow),
        _mk("Отложена", bucket="deferred"),
        _mk("Готова", bucket="done"),
    ]
    out = detectors.detect_signals(tasks)
    c = out["counts"]
    assert c["overdue"] == 1
    assert out["signals"]["overdue"][0]["_overdue_days"] == 3
    # «Без срока»: и явная, и висящая, и заблокированная без дедлайна
    assert c["no_deadline"] == 2   # «Без срока» + «Висит давно»
    assert c["no_owner"] == 1
    # «Без владельца» + «Скоро дедлайн» + «Заблокирована» (у неё дедлайн завтра
    # — блокировка не отменяет горящий срок)
    assert c["deadline_soon"] == 3
    assert c["stale"] == 1 and out["signals"]["stale"][0]["_idle_days"] == 45
    assert c["blocked"] == 1
    assert c["deferred"] == 1, "отложенное не в просрочке, отдельной строкой"
    assert out["done_total"] == 1
    assert out["open_total"] == 6


def test_overload_detection():
    tasks = ([_mk(f"Задача {i}", assignee="Вася") for i in range(8)]
             + [_mk("Одна", assignee="Петя")]
             + [_mk("Другая", assignee="Маша")])
    out = detectors.detect_signals(tasks)
    assert out["overloaded"] and out["overloaded"][0]["owner"] == "Вася"
    assert out["overloaded"][0]["open"] == 8


# ── Отчёт руководителю ─────────────────────────────────────────────────────

def test_report_leads_with_intervention():
    yesterday = (date.today() - timedelta(days=5)).isoformat()
    tasks = [_mk("Горит контракт", deadline=yesterday),
             _mk("Нет срока у задачи"),
             _mk("Отложили", bucket="deferred"),
             _mk("Готова", bucket="done")]
    analysis = detectors.detect_signals(tasks)
    md = compose_markdown(analysis)
    assert "Требует вмешательства" in md
    assert md.index("Требует вмешательства") < md.index("Не доставлены"), \
        "вмешательство — первым (аксиома A4)"
    assert "Горит контракт" in md and "5 дн." in md
    assert "Отложено сознательно" in md
    assert "Итого:" in md


def test_report_empty_is_honest():
    md = compose_markdown(detectors.detect_signals([]))
    assert "Интеграции" in md, \
        "пустой пульс должен подсказать про подключение задачников"


# ═══ ФАЗА 2: реестр людей + пуш-движок ═════════════════════════════════════

from backend.core.pulse import people_registry as pr  # noqa: E402
from backend.core.pulse import push_engine as pe  # noqa: E402


@pytest.fixture()
def people(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSE_PEOPLE_DIR", str(tmp_path / "people"))
    monkeypatch.setenv("PULSE_PUSH_DIR", str(tmp_path / "push"))
    monkeypatch.delenv("ENABLE_EXECUTION_PULSE_PUSH", raising=False)
    return tmp_path


def test_registry_resolve_name_variants(people):
    pr.upsert_person("u-1", name="Василий Петров",
                     names=["Вася", "Vasya P."],
                     telegram_chat_id="12345")
    reg = pr.load_registry("u-1")
    assert pr.resolve(reg, "Вася")["telegram_chat_id"] == "12345"
    assert pr.resolve(reg, "василий петров") is not None
    assert pr.resolve(reg, "Петров Василий") is not None, \
        "перестановка имя/фамилия должна матчиться"
    assert pr.resolve(reg, "Пётр Иванов") is None, "чужой не должен матчиться"
    assert pr.resolve(reg, "") is None


def test_registry_upsert_does_not_erase(people):
    pr.upsert_person("u-1", name="Маша", email="masha@x.ru")
    pr.upsert_person("u-1", name="Маша", telegram_chat_id="777")
    rec = pr.resolve(pr.load_registry("u-1"), "Маша")
    assert rec["email"] == "masha@x.ru" and rec["telegram_chat_id"] == "777"


def test_autofill_creates_honest_stubs(people):
    pr.upsert_person("u-1", name="Вася", telegram_chat_id="1")
    out = pr.autofill_from_tasks("u-1", [
        {"title": "A", "assignee": "Вася"},
        {"title": "B", "assignee": "Новый Человек"},
        {"title": "C", "assignee": ""},
    ])
    assert out["created"] == 1
    assert out["without_channel"] == 1
    assert "Новый Человек" in out["without_channel_names"]


def _analysis(tasks_by_signal):
    signals = {"overdue": [], "blocked": [], "deadline_soon": [], "stale": []}
    signals.update(tasks_by_signal)
    return {"signals": signals}


def _task(title, assignee, *, key=None, overdue_days=3):
    return {"title": title, "assignee": assignee,
            "_overdue_days": overdue_days,
            "_pulse": {"key": key or f"k:{title}", "deadline": "2026-07-20"}}


def test_plan_batches_and_respects_budget(people):
    pr.upsert_person("u-1", name="Вася", telegram_chat_id="123")
    reg = pr.load_registry("u-1")
    tasks = [_task(f"Задача {i}", "Вася") for i in range(5)]
    plan = pe.plan_pushes(_analysis({"overdue": tasks}), reg, {})
    assert len(plan["pushes"]) == 1, "все задачи человека — ОДНИМ сообщением"
    p = plan["pushes"][0]
    assert p["channel"] == "telegram" and p["to"] == "123"
    assert len(p["fingerprints"]) == 3, "бюджет 3 задачи на сообщение"
    assert plan["skipped"]["budget"] == 2
    assert "готово" in p["text"] and "упёрся" in p["text"], \
        "пуш — вопрос с вариантами ответа, а не приказ сменить статус"


def test_plan_cadence_dedup(people):
    from datetime import datetime, timezone
    pr.upsert_person("u-1", name="Вася", telegram_chat_id="123")
    reg = pr.load_registry("u-1")
    t = _task("Горит", "Вася")
    now = datetime.now(timezone.utc)
    recent = {"sent": {pe._fp(t, "overdue"): {
        "last_sent": now.isoformat(), "count": 1}}}
    plan = pe.plan_pushes(_analysis({"overdue": [t]}), reg, recent, now=now)
    assert not plan["pushes"], "просрочка пушится раз в 3 дня, не каждый час"
    assert plan["skipped"]["cadence"] == 1


def test_plan_respects_mute_and_quiet_hours(people):
    from datetime import datetime, timezone
    pr.upsert_person("u-1", name="Вася", telegram_chat_id="1", muted=True)
    pr.upsert_person("u-1", name="Петя", telegram_chat_id="2",
                     quiet_hours=[0, 23])
    reg = pr.load_registry("u-1")
    plan = pe.plan_pushes(
        _analysis({"overdue": [_task("A", "Вася"), _task("B", "Петя")]}),
        reg, {}, now=datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc))
    assert not plan["pushes"]
    assert plan["skipped"]["muted"] == 1 and plan["skipped"]["quiet"] == 1


def test_plan_unmapped_visible_not_lost(people):
    reg = pr.load_registry("u-1")
    plan = pe.plan_pushes(_analysis({"overdue": [_task("X", "Незнакомец")]}),
                          reg, {})
    assert not plan["pushes"]
    assert plan["unmapped"][0]["assignee"] == "Незнакомец", \
        "человек без канала — сигнал владельцу, а не молчание"


def test_escalation_after_repeated_pushes(people):
    from datetime import datetime, timedelta, timezone
    pr.upsert_person("u-1", name="Шеф", telegram_chat_id="900")
    pr.upsert_person("u-1", name="Вася", telegram_chat_id="123",
                     reports_to="Шеф")
    reg = pr.load_registry("u-1")
    t = _task("Застряла", "Вася")
    now = datetime.now(timezone.utc)
    state = {"sent": {pe._fp(t, "overdue"): {
        "last_sent": (now - timedelta(days=4)).isoformat(), "count": 2}}}
    plan = pe.plan_pushes(_analysis({"overdue": [t]}), reg, state, now=now)
    assert plan["escalations"], "2 напоминания без движения → руководителю"
    esc = plan["escalations"][0]
    assert esc["manager"]["telegram_chat_id"] == "900"
    assert esc["pushes"] == 2
    # и сам Вася при этом тоже получает очередное напоминание (каденция прошла)
    assert plan["pushes"] and plan["pushes"][0]["to"] == "123"


def test_send_pushes_fail_closed_but_dry_run_allowed(people, monkeypatch):
    async def _ledger(uid, **kw):
        return {"tasks": [], "events": [], "stats": {}}

    monkeypatch.setattr("backend.core.pulse.ledger.build_ledger", _ledger)
    res = asyncio.run(pe.send_pushes("u-1"))
    assert res["status"] == "disabled", "без флага рассылка запрещена"
    res2 = asyncio.run(pe.send_pushes("u-1", dry_run=True))
    assert res2["status"] == "dry_run", "предпросмотр работает без флага"


def test_send_pushes_records_state(people, monkeypatch):
    monkeypatch.setenv("ENABLE_EXECUTION_PULSE_PUSH", "1")
    pr.upsert_person("u-1", name="Вася", telegram_chat_id="123")
    t = {"title": "Горит", "assignee": "Вася", "status": "todo",
         "deadline": "2020-01-01", "id": "z1"}

    async def _graph(uid):
        return [t]

    async def _trackers(uid):
        return []

    sent_msgs = []

    async def _tg(uid, chat, text):
        sent_msgs.append((chat, text))
        return True

    monkeypatch.setenv("PULSE_STATE_DIR", str(people / "ledger"))
    monkeypatch.setattr("backend.core.tasks.task_analysis.collect_tasks", _graph)
    monkeypatch.setattr(
        "backend.core.tasks.task_analysis.collect_tasks_from_trackers", _trackers)
    monkeypatch.setattr(pe, "_send_telegram", _tg)

    res = asyncio.run(pe.send_pushes("u-1"))
    assert res["status"] == "success"
    assert sent_msgs and sent_msgs[0][0] == "123"
    assert "Горит" in sent_msgs[0][1]
    state = pe._load_push_state("u-1")
    assert state["sent"], "успешная доставка фиксируется для каденции"
    assert list(state["sent"].values())[0]["count"] == 1


# ═══ Входящая петля: ответ в TG → комментарий в задачник ═══════════════════

from backend.core.pulse import inbound  # noqa: E402


@pytest.fixture()
def inbound_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSE_PUSH_DIR", str(tmp_path / "push"))
    return tmp_path


def test_classify_reply_variants():
    assert inbound.classify_reply("готово ✅") == "done"
    assert inbound.classify_reply("Упёрся в доступы к API") == "blocked"
    assert inbound.classify_reply("нужен перенос на пятницу") == "postpone"
    assert inbound.classify_reply("в работе") == "in_progress"
    assert inbound.classify_reply("созвонимся завтра") == "comment"


def test_select_task_by_number_or_single():
    tasks = [{"title": "A"}, {"title": "B"}]
    assert inbound.select_task(tasks, "2 готово")["title"] == "B"
    assert inbound.select_task(tasks, "готово") is None, \
        "несколько задач без номера — надо переспросить"
    assert inbound.select_task([{"title": "X"}], "готово")["title"] == "X"
    assert inbound.select_task(tasks, "7 готово") is None


def test_handle_reply_comments_tracker_and_logs(inbound_env, monkeypatch):
    """Ответ сотрудника уходит комментарием в карточку и в лог ответов."""
    inbound.record_chat_context("555", "u-1", "вася", [
        {"title": "Лендинг", "_pulse": {"key": "k1"},
         "tracker": "trello", "tracker_task_id": "abc"}])

    comments = []

    async def _comment(uid, system, task_id, text):
        comments.append((uid, system, task_id, text))
        return {"success": True}

    monkeypatch.setattr("backend.core.tasks.task_actions.comment_task", _comment)
    res = asyncio.run(inbound.handle_reply("555", "упёрся в дизайнера"))
    assert res["matched"] and res["kind"] == "blocked"
    assert res["tracker_commented"] is True
    assert comments[0][1] == "trello" and comments[0][2] == "abc"
    assert "упёрся в дизайнера" in comments[0][3]
    # лог ответа — для отчёта руководителю
    from backend.core.pulse.push_engine import _load_push_state
    resp = _load_push_state("u-1")["responses"]
    assert resp[0]["kind"] == "blocked" and resp[0]["task_title"] == "Лендинг"


def test_handle_reply_asks_number_when_ambiguous(inbound_env, monkeypatch):
    inbound.record_chat_context("777", "u-1", "вася", [
        {"title": "Первая", "_pulse": {"key": "a"}},
        {"title": "Вторая", "_pulse": {"key": "b"}}])
    res = asyncio.run(inbound.handle_reply("777", "готово"))
    assert res["matched"] is False
    assert "1. Первая" in res["reply"] and "2. Вторая" in res["reply"]
    # с номером — попадает во вторую (трекера нет → без комментария, но лог есть)
    res2 = asyncio.run(inbound.handle_reply("777", "2 готово"))
    assert res2["matched"] and res2["task_title"] == "Вторая"
    assert res2["tracker_commented"] is False


def test_handle_reply_unknown_chat(inbound_env):
    res = asyncio.run(inbound.handle_reply("999", "привет"))
    assert res["matched"] is False
    assert "нет открытых вопросов" in res["reply"]


def test_done_never_autocloses(inbound_env, monkeypatch):
    """«готово» — комментарий и пометка, но НЕ закрытие карточки."""
    inbound.record_chat_context("111", "u-1", "вася", [
        {"title": "X", "_pulse": {"key": "k"},
         "tracker": "jira", "tracker_task_id": "J-1"}])
    called = {"close": 0}

    async def _comment(uid, system, task_id, text):
        return {"success": True}

    async def _close(*a, **k):
        called["close"] += 1
        return {"success": True}

    monkeypatch.setattr("backend.core.tasks.task_actions.comment_task", _comment)
    monkeypatch.setattr("backend.core.tasks.task_actions.close_task", _close)
    res = asyncio.run(inbound.handle_reply("111", "готово"))
    assert res["kind"] == "done"
    assert called["close"] == 0, "авто-закрытие по слову в чате запрещено"
    assert "подтвердит" in res["reply"]


def test_push_records_chat_context_for_replies(inbound_env, monkeypatch):
    """После доставки пуша чат знает, о каких задачах спрашивали."""
    monkeypatch.setenv("ENABLE_EXECUTION_PULSE_PUSH", "1")
    monkeypatch.setenv("PULSE_PEOPLE_DIR", str(inbound_env / "people"))
    monkeypatch.setenv("PULSE_STATE_DIR", str(inbound_env / "ledger"))
    pr.upsert_person("u-1", name="Вася", telegram_chat_id="42")
    t = {"title": "Отчёт за квартал", "assignee": "Вася", "status": "todo",
         "deadline": "2020-01-01", "id": "q1", "tracker": "trello",
         "tracker_task_id": "tr9"}

    async def _graph(uid):
        return [t]

    async def _trackers(uid):
        return []

    async def _tg(uid, chat, text):
        return True

    monkeypatch.setattr("backend.core.tasks.task_analysis.collect_tasks", _graph)
    monkeypatch.setattr(
        "backend.core.tasks.task_analysis.collect_tasks_from_trackers", _trackers)
    monkeypatch.setattr(pe, "_send_telegram", _tg)
    asyncio.run(pe.send_pushes("u-1"))

    ctx = inbound._load_index().get("42")
    assert ctx and ctx["owner_uid"] == "u-1"
    assert ctx["tasks"][0]["title"].startswith("Отчёт")
    assert ctx["tasks"][0]["tracker_task_id"] == "tr9", \
        "ссылка на карточку должна доехать до контекста ответов"


# ── сверка встреча↔задачник + merged-источник графа ─────────────────────

def test_not_in_tracker_signal():
    """«То, что человек не поставил задачу в задачник, не значит, что её не
    было на встрече»: открытая graph-only задача при живом трекере → сигнал."""
    from backend.core.pulse.detectors import detect_signals
    tasks = [
        {"title": "Позвонить поставщику", "assignee": "Иван",
         "source": "graph", "sources": [{"source": "graph", "id": "g1"}],
         "_pulse": {"bucket": "todo", "deadline": None,
                    "last_activity_at": "2026-07-27T00:00:00+00:00"}},
        {"title": "Собрать отчёт", "assignee": "Иван", "tracker": "trello",
         "source": "trello",
         "sources": [{"source": "trello", "id": "t1"}],
         "_pulse": {"bucket": "todo", "deadline": None,
                    "last_activity_at": "2026-07-27T00:00:00+00:00"}},
    ]
    out = detect_signals(tasks, trackers_configured=True)
    flagged = [t["title"] for t in out["signals"]["not_in_tracker"]]
    assert flagged == ["Позвонить поставщику"]
    # без трекеров сигнал молчит — иначе «не в трекере» было бы у всех
    out2 = detect_signals(tasks, trackers_configured=False)
    assert out2["signals"]["not_in_tracker"] == []


def test_not_in_tracker_quiet_for_merged_task():
    """Задача, сматченная дедупом со своей трекерной копией, сигнал не даёт."""
    from backend.core.pulse.detectors import detect_signals
    from backend.core.pulse.ledger import dedup_tasks
    merged = dedup_tasks([
        {"title": "Собрать отчёт по продажам", "assignee": "Иван",
         "source": "graph", "status": "todo"},
        {"title": "Собрать отчёт по продажам", "assignee": "Иван",
         "source": "trello", "tracker": "trello", "tracker_task_id": "t1",
         "status": "todo"},
    ])
    assert len(merged) == 1
    merged[0]["_pulse"] = {"bucket": "todo", "deadline": None,
                           "last_activity_at": "2026-07-27T00:00:00+00:00"}
    out = detect_signals(merged, trackers_configured=True)
    assert out["signals"]["not_in_tracker"] == []


def test_report_shows_meeting_vs_tracker_section():
    from backend.core.pulse.detectors import detect_signals
    from backend.core.pulse.manager_report import compose_markdown
    tasks = [
        {"title": "Позвонить поставщику", "assignee": "Иван",
         "source": "graph", "sources": [{"source": "graph", "id": "g1"}],
         "_pulse": {"bucket": "todo", "deadline": None,
                    "last_activity_at": "2026-07-27T00:00:00+00:00"}},
        {"title": "Задача из трекера", "assignee": "Пётр",
         "tracker": "jira", "source": "jira",
         "sources": [{"source": "jira", "id": "j1"}],
         "_pulse": {"bucket": "todo", "deadline": None,
                    "last_activity_at": "2026-07-27T00:00:00+00:00"}},
    ]
    md = compose_markdown(detect_signals(tasks, trackers_configured=True))
    assert "На встрече прозвучало — в задачнике нет" in md
    assert "Позвонить поставщику" in md


def test_collect_tasks_reads_merged_graph(monkeypatch):
    """Регрессия «открыто 0»: задачи встреч у org-пользователей лежат в
    орг-графе — collect_tasks обязан читать ОБЪЕДИНЁННЫЙ вид, а не только
    личный networkx-граф."""
    import asyncio

    from backend.core.tasks import task_analysis as ta

    class _NX:
        def nodes(self, data=True):
            return [
                ("t1", {"_label": "Task", "title": "Задача из встречи",
                        "status": "todo", "assignee": "Иван",
                        "meeting_id": "mtg_1"}),
                ("p1", {"_label": "Person", "name": "Иван"}),
            ]

    class _GB:
        nx_graph = _NX()

        async def close(self, save=False):
            assert save is False

    async def _mv(uid, use_networkx=None):
        return _GB()

    monkeypatch.setattr(
        "backend.core.store.graph_view.merged_graph_view_for_user", _mv)
    tasks = asyncio.run(ta.collect_tasks("u1"))
    assert len(tasks) == 1
    assert tasks[0]["title"] == "Задача из встречи"
    assert tasks[0]["source"] == "graph"
    assert tasks[0]["meeting_id"] == "mtg_1"
