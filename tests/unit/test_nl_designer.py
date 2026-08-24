# -*- coding: utf-8 -*-
"""F: процесс из естественного языка — планировщик (LLM) предлагает,
КОД валидирует. Тесты валидатора/чинилки/лейаута с FakeLLM (без сети)."""
from __future__ import annotations

import asyncio

import pytest


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def generate_json(self, prompt, **kw):
        self.calls.append((prompt, kw))
        return self.payload


GOOD_PLAN = {
    "title": "Утренний дайджест картинкой",
    "summary": "Каждое утро мозг собирает дайджест, рисуется картинка, фото уходит в Telegram.",
    "nodes": [
        {"id": "t1", "type": "trigger", "comment": "запускает каждое утро",
         "data": {"label": "Ежедневно 06:00", "trigger_type": "schedule",
                  "schedule_kind": "daily", "daily_time": "06:00"}},
        {"id": "b1", "type": "ask_brain", "comment": "мозг достаёт факты",
         "data": {"label": "Дайджест", "prompt": "Собери дайджест за вчера, только факты."}},
        {"id": "p1", "type": "prompt", "comment": "текст → задание для картинки",
         "data": {"label": "Промпт", "prompt": "Инфографика по содержанию:\n{{input}}"}},
        {"id": "img1", "type": "nanoBanana", "comment": "сервер рисует",
         "data": {"label": "Картинка"}},
        {"id": "n1", "type": "notify", "comment": "фото в телеграм",
         "data": {"label": "В Telegram", "channel": "telegram", "text": "Дайджест"}},
    ],
    "edges": [
        {"source": "t1", "target": "b1"}, {"source": "b1", "target": "p1"},
        {"source": "p1", "target": "img1"}, {"source": "img1", "target": "n1"},
    ],
}


def test_good_plan_becomes_workflow():
    from backend.core.board.nl_designer import design_process
    res = _run(design_process("u", "каждое утро дайджест картинкой в телеграм",
                              llm=FakeLLM(GOOD_PLAN)))
    assert res["success"], res
    wf = res["workflow"]
    assert wf["version"] == 1 and wf["edgeStyle"] == "curved"
    types = [n["type"] for n in wf["nodes"]]
    # note-сводка + 5 блоков
    assert types[0] == "note" and len(wf["nodes"]) == 6
    # у каждого исполняемого блока есть комментарий «что/зачем»
    for n in wf["nodes"][1:]:
        assert n["data"].get("comment"), n
    # позиции разложены по слоям слева направо
    xs = [n["position"]["x"] for n in wf["nodes"][1:]]
    assert xs == sorted(xs)
    # nanoBanana добит дефолтами движка
    img = next(n for n in wf["nodes"] if n["type"] == "nanoBanana")
    assert img["data"]["model"] == "nano-banana" and img["data"]["status"] == "idle"
    # рёбра целы
    assert len(wf["edges"]) == 4
    # premium-тир запрошен
    assert res["blocks"] and res["summary"]
    assert not res["warnings"]


def test_planner_called_with_premium_tier():
    from backend.core.board.nl_designer import design_process
    fake = FakeLLM(GOOD_PLAN)
    _run(design_process("u", "каждое утро дайджест картинкой в телеграм", llm=fake))
    _, kw = fake.calls[0]
    tier = kw.get("model_tier")
    assert tier is not None and "premium" in str(tier).lower(), kw


def test_invalid_bits_are_fixed_or_dropped_with_warnings():
    from backend.core.board.nl_designer import design_process
    plan = {
        "title": "X", "summary": "s",
        "nodes": [
            {"id": "t1", "type": "trigger",
             "data": {"trigger_type": "schedule", "schedule_kind": "каждый час",
                      "interval_minutes": 60}},
            {"id": "g1", "type": "generate",
             "data": {"label": "Пусто", "prompt": "коротко"}},      # <10 симв — дроп
            {"id": "z1", "type": "квантовый_блок", "data": {}},      # неизвестный тип
            {"id": "n1", "type": "notify", "data": {"channel": "telegram"}},
        ],
        "edges": [
            {"source": "t1", "target": "n1"},
            {"source": "t1", "target": "ghost"},                     # в никуда
        ],
    }
    res = _run(design_process("u", "сделай что-нибудь полезное мне", llm=FakeLLM(plan)))
    assert res["success"], res
    w = " | ".join(res["warnings"])
    assert "неизвестный тип" in w and "промпт слишком короткий" in w
    assert "несуществующий блок" in w
    assert "расписание триггера не распознано" in w  # мусорный kind → manual
    trig = next(n for n in res["workflow"]["nodes"] if n["type"] == "trigger")
    assert trig["data"]["trigger_type"] == "manual"


def test_cycle_is_broken():
    from backend.core.board.nl_designer import design_process
    plan = {
        "title": "Цикл", "summary": "s",
        "nodes": [
            {"id": "a", "type": "generate", "data": {"prompt": "напиши длинный текст"}},
            {"id": "b", "type": "generate", "data": {"prompt": "перепиши этот текст"}},
        ],
        "edges": [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}],
    }
    res = _run(design_process("u", "зациклированный процесс генерации", llm=FakeLLM(plan)))
    assert res["success"]
    assert any("цикл" in w for w in res["warnings"])
    assert len(res["workflow"]["edges"]) == 1


def test_condition_edges_get_source_handle():
    from backend.core.board.nl_designer import design_process
    plan = {
        "title": "Ветвление", "summary": "s",
        "nodes": [
            {"id": "c1", "type": "condition", "data": {"op": "contains", "contains": "риск"}},
            {"id": "n1", "type": "notify", "data": {"channel": "telegram"}},
            {"id": "n2", "type": "notify", "data": {"channel": "telegram"}},
        ],
        "edges": [
            {"source": "c1", "target": "n1", "sourceHandle": "true"},
            {"source": "c1", "target": "n2"},   # ветка не указана → true + warning
        ],
    }
    res = _run(design_process("u", "если в дайджесте риск — предупреди", llm=FakeLLM(plan)))
    assert res["success"]
    handles = {e["target"]: e.get("sourceHandle") for e in res["workflow"]["edges"]}
    assert handles["n1"] == "true" and handles["n2"] == "true"
    assert any("ветка условия" in w for w in res["warnings"])


def test_interval_field_translated_for_engine():
    """Планировщик мог назвать interval_minutes — движок читает interval_min."""
    from backend.core.board.nl_designer import design_process
    plan = {
        "title": "Интервал", "summary": "s",
        "nodes": [
            {"id": "t1", "type": "trigger",
             "data": {"trigger_type": "schedule", "schedule_kind": "interval",
                      "interval_minutes": 30}},
            {"id": "n1", "type": "notify", "data": {"channel": "telegram"}},
        ],
        "edges": [{"source": "t1", "target": "n1"}],
    }
    res = _run(design_process("u", "каждые 30 минут пингуй меня в телеграм", llm=FakeLLM(plan)))
    trig = next(n for n in res["workflow"]["nodes"] if n["type"] == "trigger")
    assert trig["data"]["interval_min"] == 30
    assert "interval_minutes" not in trig["data"]
    # и schedule_spec движка его понимает
    from backend.core.board.triggers import schedule_spec
    spec = schedule_spec(res["workflow"])
    assert spec == {"kind": "interval", "minutes": 30}, spec


def test_workflow_is_runnable_by_engine_dry_run(monkeypatch):
    """Смоук замыкания: построенный workflow принимается process_engine
    (dry-run без исполнения узлов)."""
    monkeypatch.setenv("BOARD_PROCESS_EXEC", "off")
    from backend.core.board.nl_designer import design_process
    from backend.core.board.process_engine import run_process_board
    res = _run(design_process("u", "каждое утро дайджест картинкой в телеграм",
                              llm=FakeLLM(GOOD_PLAN)))
    out = _run(run_process_board(res["workflow"],
                                 user_id="11111111-1111-4111-8111-111111111111"))
    assert isinstance(out, dict) and out.get("status") in ("ok", "dry_run", "success"), out


def test_too_short_request_rejected():
    from backend.core.board.nl_designer import design_process
    res = _run(design_process("u", "сделай"))
    assert not res["success"] and "подробнее" in res["error"]


def test_empty_plan_is_honest_error():
    from backend.core.board.nl_designer import design_process
    res = _run(design_process("u", "процесс из ничего, но запрос длинный",
                              llm=FakeLLM({"title": "x", "nodes": [], "edges": []})))
    assert not res["success"]


# ── F.2: правка существующей схемы словами ──

def _existing_wf():
    """Готовая схема на холсте (id + позиции)."""
    from backend.core.board.nl_designer import design_process
    res = _run(design_process("u", "каждое утро дайджест картинкой в телеграм",
                              llm=FakeLLM(GOOD_PLAN)))
    return res["workflow"]


def test_edit_mode_sees_current_graph_and_keeps_positions():
    from backend.core.board.nl_designer import design_process
    wf = _existing_wf()
    # найдём позицию неизменного блока
    b1_pos = next(n["position"] for n in wf["nodes"] if n["id"] == "b1")

    # LLM-правка: та же схема + новый email-notify в конце
    edited_plan = {
        "title": "Утренний дайджест картинкой + email",
        "summary": "Как раньше, плюс копия в почту.",
        "nodes": GOOD_PLAN["nodes"] + [
            {"id": "mail1", "type": "notify", "comment": "копия дайджеста на почту",
             "data": {"label": "На почту", "channel": "email", "text": "Дайджест"}},
        ],
        "edges": GOOD_PLAN["edges"] + [{"source": "img1", "target": "mail1"}],
    }
    fake = FakeLLM(edited_plan)
    res = _run(design_process("u", "добавь ещё копию на почту",
                              llm=fake, current_workflow=wf))
    assert res["success"] and res["edited"] is True
    # промпт правки увидел текущий граф
    assert "ТЕКУЩАЯ СХЕМА" in fake.calls[0][0]
    assert "ЧТО ИЗМЕНИТЬ" in fake.calls[0][0]
    # позиция неизменного блока b1 сохранена
    new_b1 = next(n["position"] for n in res["workflow"]["nodes"] if n["id"] == "b1")
    assert new_b1 == b1_pos, (new_b1, b1_pos)
    # новый блок появился
    assert any(n["id"] == "mail1" for n in res["workflow"]["nodes"])
    types = [n["type"] for n in res["workflow"]["nodes"] if n["type"] == "notify"]
    assert len(types) == 2  # telegram + email


def test_edit_allows_short_instruction():
    """В режиме правки короткая инструкция допустима (не 10 символов)."""
    from backend.core.board.nl_designer import design_process
    wf = _existing_wf()
    res = _run(design_process("u", "убери план",
                              llm=FakeLLM(GOOD_PLAN), current_workflow=wf))
    assert res["success"] and res["edited"] is True


def test_edit_empty_instruction_rejected():
    from backend.core.board.nl_designer import design_process
    wf = _existing_wf()
    res = _run(design_process("u", "", current_workflow=wf))
    assert not res["success"] and "изменить" in res["error"]
