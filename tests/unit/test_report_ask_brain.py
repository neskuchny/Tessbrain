# -*- coding: utf-8 -*-
"""Дно воронки визуального отчёта: «Спросить мозг» (VISUAL_REPORTS §2.2).

До фикса картинка в Telegram была тупиком. Теперь: контекст отчёта
запоминается при доставке, приглашение — в текст-дубль, следующий вопрос
боту получает контекст и понимается без уточнений."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import backend.core.board.report_context as rc

_UID = "11111111-1111-4111-8111-111111111111"


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(rc, "_path", lambda uid: tmp_path / f"{uid}.json")


def test_remember_and_context_block(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert rc.context_block(_UID) == ""          # отчётов не было → пусто

    rc.remember_report(_UID, "Пульс недели",
                       "Динамит у отдела продаж: срыв срока по клиенту X.")
    block = rc.context_block(_UID)
    assert "Пульс недели" in block
    assert "Динамит у отдела продаж" in block
    assert "иначе игнорируй контекст" in block   # защита нерелевантных вопросов


def test_stale_report_not_injected(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    p = tmp_path / f"{_UID}.json"
    old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    p.write_text(json.dumps({"title": "Старый", "facts": "факты", "at": old}),
                 encoding="utf-8")
    assert rc.recent_report(_UID) is None        # 72ч > 48ч → протух
    assert rc.context_block(_UID) == ""


def test_chat_handler_injects_fresh_report(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    rc.remember_report(_UID, "Пульс", "Взрыв в отделе продаж ×3.")

    captured = {}

    class _FakeRouter:
        async def generate(self, *, prompt, system_prompt, temperature,
                           max_tokens):
            captured["prompt"] = prompt
            return "ответ"

    from backend.core.messengers.chat_handler import MessengerChatHandler
    h = MessengerChatHandler(llm_router=_FakeRouter())
    reply = asyncio.run(h(_UID, "что за взрыв у продаж?"))
    assert reply == "ответ"
    assert "Взрыв в отделе продаж" in captured["prompt"]   # контекст подмешан
    assert "что за взрыв у продаж?" in captured["prompt"]  # вопрос сохранён


def test_chat_handler_unchanged_without_report(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    captured = {}

    class _FakeRouter:
        async def generate(self, *, prompt, system_prompt, temperature,
                           max_tokens):
            captured["prompt"] = prompt
            return "ок"

    from backend.core.messengers.chat_handler import MessengerChatHandler
    h = MessengerChatHandler(llm_router=_FakeRouter())
    asyncio.run(h(_UID, "просто вопрос"))
    assert captured["prompt"] == "просто вопрос"  # байт-в-байт прежнее


def test_engine_appends_invite_and_remembers(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    from backend.core.board.process_engine import _with_ask_brain
    out = _with_ask_brain(_UID, {"label": "Пульс недели"},
                          "Факты: всё ровно.", "ru")
    assert out.startswith("Факты: всё ровно.")
    assert "напишите его боту" in out            # приглашение добавлено
    rep = rc.recent_report(_UID)
    assert rep and rep["title"] == "Пульс недели"
    # en-вариант
    out_en = _with_ask_brain(_UID, {"label": "Pulse"}, "Facts.", "en")
    assert "message the bot" in out_en
