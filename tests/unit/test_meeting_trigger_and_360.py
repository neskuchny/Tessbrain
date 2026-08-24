# -*- coding: utf-8 -*-
"""Два прод-кейса одного дня.

1. «Инфографика по завершившейся встрече» → «Встреча не найдена»: узел
   «Данные встречи» предпочитал сохранённый на узле meeting_id (остаток
   ручной настройки шаблона) встрече из живого триггера. Теперь при
   событийном запуске побеждает встреча, запустившая триггер.

2. «Не нашел в 360 проект finai»: 360 открывал только ЛИЧНЫЙ граф и искал
   строго по tenant_id=user_id — проект из общих встреч (орг-граф, орг-
   тенант) был честно невидим. Теперь merged-вид + добор по своим оргам +
   подстрочное совпадение имени.
"""
import asyncio
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ── 1. Узел «Данные встречи»: триггер важнее сохранённого id ────────────

def _run_meeting_node(monkeypatch, node_mid, trigger_mid, fetched):
    from backend.core.board import process_engine as pe

    async def _fetch(user_id, mid, kind):
        fetched.append(mid)
        return {"kind": kind, "title": f"Встреча {mid}",
                "text": "стенограмма", "empty": False}

    monkeypatch.setattr("backend.core.board.meeting_artifacts.fetch_artifact",
                        _fetch)
    data = ({"meeting_id": node_mid, "kind": "report"}
            if node_mid else {"kind": "report"})
    ctx = {"user_id": "u1"}
    if trigger_mid:
        ctx["trigger_meeting_id"] = trigger_mid
    return asyncio.run(pe._process_handler("meeting_data", data, [], ctx))


def test_trigger_meeting_beats_stale_node_meeting(monkeypatch):
    fetched = []
    out = _run_meeting_node(monkeypatch, node_mid="stale-id-from-template",
                            trigger_mid="fresh-meeting-uuid", fetched=fetched)
    assert not out.get("error"), out
    assert fetched == ["fresh-meeting-uuid"], \
        "событийный запуск обязан брать встречу триггера, а не остаток шаблона"


def test_manual_run_keeps_node_meeting(monkeypatch):
    """Без триггера сохранённый на узле выбор работает как раньше."""
    fetched = []
    out = _run_meeting_node(monkeypatch, node_mid="picked-by-user",
                            trigger_mid="", fetched=fetched)
    assert not out.get("error"), out
    assert fetched == ["picked-by-user"]


# ── 1б. Орг-встреча коллеги добирается точным запросом ──────────────────

def test_meeting_row_org_fallback(monkeypatch):
    from backend.core.board import meeting_artifacts as ma

    class _SB:
        async def get_meeting_details(self, mid, include_transcript=False,
                                      user_id=None):
            return None  # scoped-поиск по своему user_id встречу не видит

        async def _request(self, method, path, params=None):
            assert params["id"] == "eq.m-42", "добор — ТОЛЬКО точный id"
            return [{"id": "m-42", "user_id": "colleague-1",
                     "title": "Планёрка отдела"}]

    monkeypatch.setattr("backend.db.supabase_client.get_supabase_client",
                        lambda: _SB())
    monkeypatch.setattr("backend.core.store.tenant_scope.allowed_tenants",
                        lambda uid: {"u1", "org-1", "colleague-1"})
    row = asyncio.run(ma._load_meeting_row("u1", "m-42"))
    assert row.get("title") == "Планёрка отдела"


def test_meeting_row_foreign_owner_rejected(monkeypatch):
    """Чужая встреча (владелец вне организации) НЕ отдаётся — анти-утечка."""
    from backend.core.board import meeting_artifacts as ma

    class _SB:
        async def get_meeting_details(self, mid, include_transcript=False,
                                      user_id=None):
            return None

        async def _request(self, method, path, params=None):
            return [{"id": "m-43", "user_id": "stranger",
                     "title": "Чужая встреча"}]

    monkeypatch.setattr("backend.db.supabase_client.get_supabase_client",
                        lambda: _SB())
    monkeypatch.setattr("backend.core.store.tenant_scope.allowed_tenants",
                        lambda uid: {"u1", "org-1"})
    row = asyncio.run(ma._load_meeting_row("u1", "m-43"))
    assert row == {}, "встреча постороннего владельца не должна отдаваться"


# ── 2. 360: merged-граф + добор по оргам + подстрочный матч ─────────────

class _GB:
    """Мини-двойник GraphBuilder: узлы с tenant_id, поиск CONTAINS."""
    connected = True

    def __init__(self, nodes):
        self._nodes = nodes  # id -> attrs (с tenant_id)

    async def search_nodes(self, query="", limit=15, tenant_id=None):
        q = (query or "").lower()
        out = []
        for nid, a in self._nodes.items():
            if a.get("tenant_id") != tenant_id:
                continue
            if q in str(a.get("name") or "").lower() or q in nid.lower():
                out.append({"id": nid, **a})
        return out[:limit]

    async def get_node_by_id(self, nid, tenant_id=None, strict_tenant=False):
        a = self._nodes.get(nid)
        if not a:
            return None
        if tenant_id is not None:
            tenants = ([tenant_id] if isinstance(tenant_id, str)
                       else list(tenant_id))
            if a.get("tenant_id") not in tenants:
                return None
        return {"id": nid, **a}

    async def get_node_relationships(self, nid):
        return {"outgoing": [], "incoming": []}

    async def close(self, save=False):
        pass


def _patch_360(monkeypatch, nodes, allowed):
    from backend.core.ontology import object360 as o

    async def _view(user_id, use_networkx=None):
        return _GB(nodes)

    monkeypatch.setattr("backend.core.store.graph_view.merged_graph_view_for_user",
                        _view)
    monkeypatch.setattr("backend.core.store.tenant_scope.allowed_tenants",
                        lambda uid: allowed)
    return o


def test_360_finds_org_tenant_project(monkeypatch):
    """Проект, поднятый на орг-тенант, находится добором (кейс finai)."""
    o = _patch_360(
        monkeypatch,
        nodes={"proj-finai": {"name": "FinAI", "_label": "Project",
                              "tenant_id": "org-1"}},
        allowed={"u1", "org-1"})
    got = asyncio.run(o._graph_view_for("u1", "finai"))
    assert got and got["node"]["id"] == "proj-finai"


def test_360_substring_match(monkeypatch):
    """«finai» находит узел «Проект FinAI» — точного совпадения не требуем."""
    o = _patch_360(
        monkeypatch,
        nodes={"p2": {"name": "Проект FinAI", "_label": "Project",
                      "tenant_id": "u1"}},
        allowed={"u1"})
    got = asyncio.run(o._graph_view_for("u1", "finai"))
    assert got and got["node"]["id"] == "p2"


def test_360_does_not_leak_foreign_tenant(monkeypatch):
    """Узел чужого тенанта не находится даже при совпадении имени."""
    o = _patch_360(
        monkeypatch,
        nodes={"p3": {"name": "FinAI", "_label": "Project",
                      "tenant_id": "someone-else"}},
        allowed={"u1", "org-1"})
    got = asyncio.run(o._graph_view_for("u1", "finai"))
    assert got is None


# ── 3. Задачи встречи: таблица MeetFlow ∪ граф мозга ────────────────────

def test_meeting_tasks_merged_from_both_sources(monkeypatch):
    """Кейс «не находит все задачи»: таблица meeting_tasks и Task-узлы графа
    пополняются разными пайплайнами — агент обязан видеть объединение."""
    import json as _json

    from backend.integrations.tessent_brain_tools import TessentBrainTools

    tools = TessentBrainTools.__new__(TessentBrainTools)
    tools.user_id = "u1"

    class _MF:
        pass

    async def _mf_tasks(mid, user_id=""):
        return _json.dumps({"tasks": [
            {"title": "Запуск проекта по загородной недвижимости",
             "status": "todo"},
            {"title": "Декомпозиция проекта", "status": "todo"},
        ]}, ensure_ascii=False)

    _mf = _MF()
    _mf.get_meeting_tasks = _mf_tasks
    monkeypatch.setattr(tools, "_setup_meetflow_context", lambda: _mf)

    async def _graph_tasks(uid, mid):
        return [
            # дубль таблицы (другой регистр) — не должен задвоиться
            {"title": "ДЕКОМПОЗИЦИЯ ПРОЕКТА", "status": "todo",
             "source": "meeting"},
            {"title": "Найти руководителя направления", "status": "todo",
             "source": "meeting"},
            {"title": "Написать скрипты продаж", "status": "todo",
             "source": "meeting"},
        ]

    monkeypatch.setattr(
        "backend.core.tasks.task_analysis.collect_meeting_tasks", _graph_tasks)

    out = _json.loads(asyncio.run(tools.get_meeting_tasks_meetflow("m-1")))
    titles = [t["title"] for t in out["tasks"]]
    assert len(titles) == 4, f"2 из таблицы + 2 новых из графа: {titles}"
    assert "Найти руководителя направления" in titles
    assert "Написать скрипты продаж" in titles
    assert sum("декомпозиция" in t.lower() for t in titles) == 1, \
        "дубль по названию не задваивается"
    assert "+2" in out.get("note", ""), "честная пометка о доборе из графа"
    graph_marked = [t for t in out["tasks"]
                    if t.get("source") == "brain_graph"]
    assert len(graph_marked) == 2, "добранные помечены источником"


def test_meeting_tasks_graph_failure_keeps_base(monkeypatch):
    """Сломался граф — агент всё равно получает задачи из таблицы."""
    import json as _json

    from backend.integrations.tessent_brain_tools import TessentBrainTools

    tools = TessentBrainTools.__new__(TessentBrainTools)
    tools.user_id = "u1"

    class _MF:
        pass

    async def _mf_tasks(mid, user_id=""):
        return _json.dumps({"tasks": [{"title": "Одна задача"}]})

    _mf = _MF()
    _mf.get_meeting_tasks = _mf_tasks
    monkeypatch.setattr(tools, "_setup_meetflow_context", lambda: _mf)

    async def _boom(uid, mid):
        raise RuntimeError("graph down")

    monkeypatch.setattr(
        "backend.core.tasks.task_analysis.collect_meeting_tasks", _boom)
    out = _json.loads(asyncio.run(tools.get_meeting_tasks_meetflow("m-1")))
    assert [t["title"] for t in out["tasks"]] == ["Одна задача"]
