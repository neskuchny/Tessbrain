# -*- coding: utf-8 -*-
"""Mark: «Пришли вывод в ТГ» обязан доходить, даже если групповой чат упал.

Прод-кейс: MarketingDirector предложил send_telegram_message с готовым
стратегическим отчётом, но авто-селектор следующего спикера пошёл в LLM с
историей, оканчивающейся ходом модели → Gemini 400 «Requests ending with
a model turn are not supported» → чат упал, отправка не выполнилась, а
API-ответ выглядел успешным. Пользователь так и не получил вывод.
"""
import asyncio
import json
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_MARK = _ROOT.parent / "mark001_async"
if str(_MARK) not in sys.path:
    sys.path.insert(0, str(_MARK))


def _tool_call_msg(name="send_telegram_message",
                   args='{"message": "Стратегический отчёт: …"}'):
    return {"role": "assistant", "name": "MarketingDirector", "content": None,
            "tool_calls": [{"id": "wfJdsHZe", "type": "function",
                            "function": {"name": name, "arguments": args}}]}


class _GC:
    def __init__(self, messages):
        self.messages = messages


def test_salvage_executes_pending_telegram(monkeypatch):
    import agents.marketing_agents as ma

    sent = []

    async def _send(message, chat_id=None):
        sent.append({"message": message, "chat_id": chat_id})
        return '{"success": true}'

    monkeypatch.setattr(ma, "send_telegram_message", _send)
    gc = _GC([{"role": "user", "content": "Пришли вывод в ТГ"},
              _tool_call_msg()])
    ok = asyncio.run(ma._salvage_pending_telegram(gc))
    assert ok is True
    assert sent and "Стратегический отчёт" in sent[0]["message"]
    # факт доставки отражён в переписке — get_chat_response его увидит
    assert any("Отправлено в Telegram" in str(m.get("content"))
               for m in gc.messages)


def test_salvage_ignores_other_tools_and_empty(monkeypatch):
    import agents.marketing_agents as ma

    sent = []

    async def _send(message, chat_id=None):
        sent.append(message)
        return "{}"

    monkeypatch.setattr(ma, "send_telegram_message", _send)
    gc = _GC([_tool_call_msg(name="create_task"),
              _tool_call_msg(args='{"message": ""}')])
    ok = asyncio.run(ma._salvage_pending_telegram(gc))
    assert ok is False and sent == [], \
        "чужие инструменты и пустые сообщения не исполняются"


def test_selector_keeps_speaker_after_tool_call():
    """После хода с tool call спикер остаётся прежним (исполнитель = caller)
    — селектор не ходит в LLM и 400 невозможен."""
    import agents.marketing_agents as ma
    import inspect
    src = inspect.getsource(ma)
    assert "_select_speaker_tool_aware" in src
    # достаём локальную функцию через созданный GroupChat нельзя без сборки
    # агентов (тянет LLM-конфиг) — проверяем логику на копии сигнатуры:
    ns = {}
    fn_src = src[src.index("def _select_speaker_tool_aware"):]
    fn_src = fn_src[:fn_src.index("group_chat = GroupChat(")]
    exec("def outer():\n" + "\n".join(
        "    " + l for l in fn_src.splitlines()) +
        "\n    return _select_speaker_tool_aware", ns)
    sel = ns["outer"]()
    speaker = object()
    assert sel(speaker, _GC([_tool_call_msg()])) is speaker
    assert sel(speaker, _GC([{"role": "assistant", "content": "обычный"}])) \
        == "auto"
