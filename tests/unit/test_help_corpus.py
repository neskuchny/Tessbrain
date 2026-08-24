"""Unit-тесты «Гид по Tessbrain»: корпус справки + чистые хелперы гид-роута.

Роут-модуль грузим напрямую через importlib (пакет backend.api.routes.__init__
тянет jwt/cryptography, недоступные в этом окружении).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from backend.core.help.help_corpus import get_help_corpus

_ROOT = Path(__file__).resolve().parents[2]  # tessent_brain/


@pytest.fixture(scope="module")
def guide():
    spec = importlib.util.spec_from_file_location(
        "guide_standalone", str(_ROOT / "backend/api/routes/guide.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# === Корпус ===============================================================

def test_corpus_loads() -> None:
    st = get_help_corpus().stats()
    assert st["docs"] >= 10 and st["chunks"] > st["docs"]


@pytest.mark.parametrize("query,expect_slug", [
    ("как загрузить таблицу и спросить с цифрами", "knowledge/datasets"),
    ("где сделать договор по встрече", "knowledge/meeting-docs"),
    ("что такое tessent", "getting-started/what-is-tessent"),
    ("дайджест рынка каждый день", "automations/classic-rules"),
])
def test_retrieval_relevance(query, expect_slug) -> None:
    r = get_help_corpus().retrieve(query, k=4)
    slugs = [c["meta"].get("slug") for c in r["chunks"]]
    assert expect_slug in slugs, f"{query!r} → {slugs}"


def test_broad_query_includes_parent() -> None:
    r = get_help_corpus().retrieve("что такое tessent", k=3)
    assert r["parents"], "широкий вопрос должен добавить обзор раздела"
    assert any(p["slug"] == "getting-started/what-is-tessent" for p in r["parents"])


def test_specific_query_no_parent_needed() -> None:
    # узкий конкретный вопрос — достаточно чанков (parents может быть пуст)
    r = get_help_corpus().retrieve("куда вставить bot token slack в интеграциях", k=3)
    assert r["chunks"]


def test_empty_query_safe() -> None:
    r = get_help_corpus().retrieve("", k=3)
    assert r["chunks"] == [] and r["parents"] == []


# === Хелперы гид-роута ====================================================

def test_render_snippets(guide) -> None:
    r = get_help_corpus().retrieve("где сделать договор по встрече", k=2)
    snip = guide._render_snippets(r["chunks"], r["parents"])
    assert "договор" in snip.lower() or "meeting-docs" in snip.lower()


def test_render_snippets_empty(guide) -> None:
    assert "нет подходящего" in guide._render_snippets([], [])


def test_history_text(guide) -> None:
    msgs = [guide.GuideMessage(role="user", content="привет"),
            guide.GuideMessage(role="assistant", content="здравствуйте")]
    h = guide._history_text(msgs)
    assert "Пользователь: привет" in h and "Гид: здравствуйте" in h
    assert guide._history_text(None) == ""


def test_system_prompt_scoped(guide) -> None:
    p = guide.GUIDE_SYSTEM_PROMPT
    assert "Tessbrain" in p and "Знания → Датасеты" in p
    # изоляция: гид не должен обещать данные компании
    assert "ТОЛЬКО на приведённую справку" in p


# === Топология (Phase 3) ==================================================

def test_topology_line_mentions_sources() -> None:
    from backend.core.help.topology import list_topology, topology_line
    line = topology_line()
    assert "MeetFlow" in line and "CallInsight" in line
    assert "Синхронизация" in line     # где искать (where)
    names = {t["name"] for t in list_topology()}
    assert "mini Tess" in names


def test_guide_topology_helper(guide) -> None:
    line = guide._topology_line()
    assert "Экосистема" in line and "MeetFlow" in line


def test_guide_has_personalization_helpers(guide) -> None:
    # Phase 2 хелперы существуют и не падают на импорте
    assert callable(guide._load_persona)
    assert callable(guide._capture_profile_bg)


def test_usage_advisor_generic(monkeypatch) -> None:
    """Без профиля → personalized=False, совет из мок-LLM, инструменты из корпуса."""
    import backend.core.llm.router as r
    from backend.core.help.usage_advisor import build_advice

    class FakeRouter:
        async def generate(self, **k):
            return "Начните с Brain и Автоматизаций."

    monkeypatch.setattr(r, "get_llm_router", lambda: FakeRouter())
    monkeypatch.setattr(r, "set_llm_context", lambda **k: None)

    import asyncio
    d = asyncio.run(build_advice(None))
    assert d["personalized"] is False
    assert "Brain" in d["advice"]
    assert d["tools"] and all("slug" in t for t in d["tools"])


def test_signals_collector_counts(monkeypatch) -> None:
    """Сбор сигналов по (фейковому) графу: счётчики + просрочка."""
    import backend.core.store.graph_builder as gbm
    import backend.core.store.tenant_paths as tp
    import backend.core.sleep.anomaly_detector as ad
    from backend.core.help.signals_collector import collect_company_signals

    class FakeGraph:
        def nodes(self, data=False):
            rows = [
                ("m1", {"_label": "Meeting"}),
                ("t1", {"_label": "Task", "status": "open", "due_date": "2020-01-01"}),
                ("t2", {"_label": "Task", "status": "done"}),
                ("p1", {"_label": "Person"}),
            ]
            return rows if data else [r[0] for r in rows]

    class FakeGB:
        connected = True
        nx_graph = FakeGraph()

        def __init__(self, **k):
            pass

        async def connect(self):
            pass

        async def close(self, save=False):
            pass

    class FakeDetector:
        def __init__(self, gb):
            pass

        async def detect_all(self):
            return {"stale_tasks": [1, 2], "orphans": []}

    monkeypatch.setattr(gbm, "GraphBuilder", FakeGB)
    monkeypatch.setattr(tp, "graph_path_for_user", lambda uid: "x")
    monkeypatch.setattr(ad, "AnomalyDetector", FakeDetector)

    import asyncio
    s = asyncio.run(collect_company_signals("bca75db7-8336-411a-a0f6-49c3b2adfbbb"))
    assert s["встреч в памяти"] == 1
    assert s["задач"] == 2
    assert s["открытых задач"] == 1        # t2 done не считается
    assert s["просроченных задач"] == 1    # t1 due 2020 < сегодня
    assert "аномалии (что не так)" in s and "stale_tasks: 2" in s["аномалии (что не так)"]


def test_advisor_grounded_by_signals(monkeypatch) -> None:
    """Явные сигналы делают совет предметным (personalized=True даже без профиля)."""
    import backend.core.llm.router as r
    from backend.core.help.usage_advisor import build_advice

    captured = {}

    class FakeRouter:
        async def generate(self, prompt=None, **k):
            captured["prompt"] = prompt
            return "Включите напоминания о задачах."

    monkeypatch.setattr(r, "get_llm_router", lambda: FakeRouter())
    monkeypatch.setattr(r, "set_llm_context", lambda **k: None)

    import asyncio
    d = asyncio.run(build_advice(None, signals={"просроченных задач": 5}))
    assert d["personalized"] is True
    assert "просроченных задач: 5" in captured["prompt"]


def test_usage_advisor_personalized(monkeypatch) -> None:
    """С профилем (роль) → personalized=True."""
    import backend.core.llm.router as r
    import backend.memory.user_profiles as up
    from backend.core.help.usage_advisor import build_advice

    class FakeProfile:
        role = "руководитель проектов"
        current_focus = "клиентские кампании"
        current_projects = []

    class FakeSvc:
        async def get_profile(self, uid):
            return FakeProfile()

    class FakeRouter:
        async def generate(self, **k):
            return "Вам подойдёт Клиенты и Документы по встрече."

    monkeypatch.setattr(up, "get_user_profile_service", lambda: FakeSvc())
    monkeypatch.setattr(r, "get_llm_router", lambda: FakeRouter())
    monkeypatch.setattr(r, "set_llm_context", lambda **k: None)

    import asyncio
    d = asyncio.run(build_advice("bca75db7-8336-411a-a0f6-49c3b2adfbbb"))
    assert d["personalized"] is True and d["advice"]


@pytest.mark.parametrize("msg,expect", [
    ("я руководитель проектов в перформанс-агентстве, веду клиентские кампании", True),
    ("мы небольшое смм-агентство, делаю таргет", True),
    ("я founder стартапа, отвечаю за продукт", True),
    ("как загрузить таблицу?", False),
    ("где найти договор", False),
    ("я не знаю", False),
    ("что это значит", False),
    ("привет", False),
])
def test_self_description_heuristic(guide, msg, expect) -> None:
    assert guide._looks_like_self_description(msg) is expect
