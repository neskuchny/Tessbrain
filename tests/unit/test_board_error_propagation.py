# -*- coding: utf-8 -*-
"""Честный отказ доски: упавший вход не превращается в галлюцинацию.

Инцидент: meeting_data упал («не задана встреча»), но «Генерация» запустилась
на пустом входе, LLM сочинил шаблонное письмо с заглушками, а «Уведомление»
отправило его команде. Теперь узел, у которого ВСЕ активные входы упали,
пропускается с протяжкой ошибки по цепочке."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from backend.core.board.process_engine import run_graph


def _run(nodes, edges, handler):
    return asyncio.run(run_graph(nodes, edges, handler))


def test_error_input_skips_node_and_propagates():
    nodes = [
        {"id": "t", "type": "trigger", "data": {}},
        {"id": "m", "type": "meeting_data", "data": {}},
        {"id": "g", "type": "generate", "data": {}},
        {"id": "n", "type": "notify", "data": {}},
    ]
    edges = [
        {"source": "t", "target": "m"},
        {"source": "m", "target": "g"},
        {"source": "g", "target": "n"},
    ]
    called = []

    async def handler(node_type, data, inputs, ctx):
        called.append(node_type)
        if node_type == "trigger":
            return {"text": ""}
        if node_type == "meeting_data":
            return {"error": "не задана встреча"}
        if node_type == "generate":
            return {"text": "сочинённое письмо"}   # не должно случиться
        return {"ok": True, "sent": True}

    res = _run(nodes, edges, handler)
    assert res["status"] == "done"
    # generate и notify НЕ исполнялись
    assert called == ["trigger", "meeting_data"]
    # ошибка дотянулась до конца цепочки, без наслоения префиксов
    g_out = res["outputs"]["g"]
    n_out = res["outputs"]["n"]
    assert g_out["input_failed"] and "не задана встреча" in g_out["error"]
    assert n_out["input_failed"] and "не задана встреча" in n_out["error"]
    assert n_out["error"].count("пропущен из-за ошибки входа") == 1


def test_partial_good_input_still_runs():
    """Один вход упал, второй дал текст → узел работает (как раньше)."""
    nodes = [
        {"id": "a", "type": "src_ok", "data": {}},
        {"id": "b", "type": "src_fail", "data": {}},
        {"id": "g", "type": "generate", "data": {}},
    ]
    edges = [
        {"source": "a", "target": "g"},
        {"source": "b", "target": "g"},
    ]

    async def handler(node_type, data, inputs, ctx):
        if node_type == "src_ok":
            return {"text": "данные"}
        if node_type == "src_fail":
            return {"error": "упал"}
        return {"text": "сгенерировано из: " + " ".join(
            v.get("text", "") for v in inputs if isinstance(v, dict))}

    res = _run(nodes, edges, handler)
    assert res["outputs"]["g"]["text"].startswith("сгенерировано")


def test_no_inputs_node_unaffected():
    """Стартовые узлы без входов работают как раньше."""
    nodes = [{"id": "t", "type": "trigger", "data": {}}]

    async def handler(node_type, data, inputs, ctx):
        return {"text": "старт"}

    res = _run(nodes, [], handler)
    assert res["outputs"]["t"]["text"] == "старт"


def test_image_gen_names_reason_when_no_keys(monkeypatch):
    """Выбранная модель недоступна → в ошибке видно ПОЧЕМУ по каждой попытке
    (раньше: «failed:» с пустой причиной и молчаливый фолбэк)."""
    for var in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    from backend.core.board.image_gen import generate_image_png
    res = asyncio.run(generate_image_png(
        "нарисуй карту памяти по итогам встречи", None, preferred="gpt-image-2"))
    assert res["png"] is None
    assert "Детали" in (res["error"] or "")
    assert "нет ключа OpenAI" in res["error"]
