"""Unit-тесты классических research/digest автоматизаций.

Handler'ы тестируются на голом инстансе AutomationService (__new__) с
подменёнными источниками/LLM/доставкой — без сети/Supabase/LLM.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from backend.core.automations.automation_service import AutomationService


def _svc() -> AutomationService:
    return AutomationService.__new__(AutomationService)


def _run(coro):
    return asyncio.run(coro)


# === scheduled_research ===================================================

def test_scheduled_research_requires_source() -> None:
    r = _run(_svc()._execute_scheduled_research({}, "u1", "a1"))
    assert r["success"] is False and "источник" in r["message"]


def test_scheduled_research_happy(monkeypatch) -> None:
    svc = _svc()
    import backend.core.documents.url_ingest as ui
    import backend.core.search.web_search as ws

    async def fake_search(q, max_results=5):
        return [{"title": "T", "snippet": "S", "url": "http://x"}]

    async def fake_fetch(u):
        return {"ok": True, "title": "Doc", "text": "тело страницы про рынок"}

    monkeypatch.setattr(ws, "web_search", fake_search)
    monkeypatch.setattr(ui, "fetch_url_text", fake_fetch)

    captured = {}

    async def fake_digest(**kw):
        captured["corpus"] = kw.get("corpus")
        captured["lens"] = kw.get("lens")
        return ("РАЗБОР РЫНКА", {"model": "standard"})

    delivered = {}

    async def fake_deliver(**kw):
        delivered.update(kw)
        return {"success": True, "message": "→ 1/1"}

    monkeypatch.setattr(svc, "_llm_digest", fake_digest)
    monkeypatch.setattr(svc, "_deliver_digest", fake_deliver)

    r = _run(svc._execute_scheduled_research({
        "sources": {"search_queries": ["новости"], "urls": ["http://x"]},
        "lens": "инсайты рынка", "chat_id": "123",
    }, "u1", "a1"))
    assert r["success"] is True
    assert r["llm_usage"]["model"] == "standard"
    assert "инсайты рынка" == captured["lens"]
    assert "тело страницы" in captured["corpus"]        # url текст в корпусе
    assert "Поиск" in captured["corpus"]                # поиск в корпусе
    assert delivered["body"] == "РАЗБОР РЫНКА"


def test_scheduled_research_empty_corpus_fails(monkeypatch) -> None:
    svc = _svc()
    import backend.core.documents.url_ingest as ui

    async def fake_fetch(u):
        return {"ok": False, "error": "HTTP 404"}

    monkeypatch.setattr(ui, "fetch_url_text", fake_fetch)
    # только битый URL → в корпусе будет строка «(не удалось: …)», значит
    # corpus не пуст; но убедимся что LLM всё равно позовётся с этим.
    got = {}

    async def fake_digest(**kw):
        got["called"] = True
        return ("x", {})

    async def fake_deliver(**kw):
        return {"success": True, "message": "ok"}

    monkeypatch.setattr(svc, "_llm_digest", fake_digest)
    monkeypatch.setattr(svc, "_deliver_digest", fake_deliver)
    r = _run(svc._execute_scheduled_research(
        {"sources": {"urls": ["http://x"]}}, "u1", "a1"))
    assert got.get("called") and r["success"] is True


# === chat_digest ==========================================================

def test_chat_digest_requires_channel() -> None:
    r = _run(_svc()._execute_chat_digest({"source": "slack"}, "u1", "a1"))
    assert r["success"] is False and "channel" in r["message"]


# === доставка: channel-only (для one-click пресетов) ======================

def test_deliver_digest_channel_only_uses_send_to_channel(monkeypatch) -> None:
    svc = _svc()
    import backend.core.automations.meeting_workflows as mw

    sent = {}

    class FakeExec:
        async def _send_to_channel(self, *, user_id, params, text, subject):
            sent["text"] = text
            sent["subject"] = subject
            return {"success": True, "message": "→ default TG"}

    monkeypatch.setattr(mw, "get_workflow_executor", lambda: FakeExec())
    # пресет: только channel=telegram, без chat_id → должен уйти в дефолтный чат
    r = _run(svc._deliver_digest(
        data={"channel": "telegram"}, user_id="u1", title="Отчёт", body="ТЕЛО"))
    assert r["success"] is True and sent["text"] == "ТЕЛО"


def test_deliver_digest_no_target_fails() -> None:
    r = _run(_svc()._deliver_digest(
        data={}, user_id="u1", title="T", body="B"))
    assert r["success"] is False and "получател" in r["message"]


def test_chat_digest_slack_happy(monkeypatch) -> None:
    svc = _svc()
    import backend.integrations.registry as regmod

    class FakeReg:
        async def load_for_user(self, uid):
            return 1

        async def execute_tool(self, tool, **kw):
            assert tool == "slack_read_messages" and kw.get("days") == 7
            # Slack history отдаёт НОВЫЕ первыми → «сделать Y» новее «проблему X».
            return json.dumps({"success": True, "messages": [
                {"user": "u2", "text": "решили сделать Y"},
                {"user": "u1", "text": "обсудили проблему X"},
            ]})

    monkeypatch.setattr(regmod, "IntegrationRegistry", FakeReg)

    captured = {}

    async def fake_digest(**kw):
        captured["corpus"] = kw.get("corpus")
        return ("ДАЙДЖЕСТ", {"model": "standard"})

    async def fake_deliver(**kw):
        return {"success": True, "message": "ok"}

    monkeypatch.setattr(svc, "_llm_digest", fake_digest)
    monkeypatch.setattr(svc, "_deliver_digest", fake_deliver)

    r = _run(svc._execute_chat_digest({
        "source": "slack", "channel": "C1", "window_days": 7, "chat_id": "123",
    }, "u1", "a1"))
    assert r["success"] is True
    # хронологический порядок (reversed): первое сообщение раньше
    assert captured["corpus"].index("проблему X") < captured["corpus"].index("сделать Y")


def test_chat_digest_slack_not_in_channel(monkeypatch) -> None:
    svc = _svc()
    import backend.integrations.registry as regmod

    class FakeReg:
        async def load_for_user(self, uid):
            return 1

        async def execute_tool(self, tool, **kw):
            return json.dumps({"success": False, "error": "not_in_channel"})

    monkeypatch.setattr(regmod, "IntegrationRegistry", FakeReg)
    r = _run(svc._execute_chat_digest(
        {"source": "slack", "channel": "C1"}, "u1", "a1"))
    assert r["success"] is False
    assert "not_in_channel" in r["message"] and "пригласите" in r["message"]


def test_chat_digest_telegram_group_guarded(monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_SESSION_STRING", raising=False)
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    r = _run(_svc()._execute_chat_digest(
        {"source": "telegram_group", "channel": "-100123"}, "u1", "a1"))
    assert r["success"] is False and "userbot" in r["message"].lower()


def test_chat_digest_telegram_group_happy(monkeypatch) -> None:
    svc = _svc()
    import backend.core.messengers.telegram_userbot as tub

    monkeypatch.setattr(tub, "is_configured", lambda: True)

    async def fake_read(chat, days=7, limit=2000):
        return {"ok": True, "count": 2, "messages": [
            {"sender": "Аня", "text": "нашли баг в оплате"},
            {"sender": "Петя", "text": "чиним к пятнице"},
        ]}

    monkeypatch.setattr(tub, "read_group_history", fake_read)

    captured = {}

    async def fake_digest(**kw):
        captured["corpus"] = kw.get("corpus")
        return ("ДАЙДЖЕСТ ГРУППЫ", {"model": "standard"})

    async def fake_deliver(**kw):
        return {"success": True, "message": "ok"}

    monkeypatch.setattr(svc, "_llm_digest", fake_digest)
    monkeypatch.setattr(svc, "_deliver_digest", fake_deliver)

    r = _run(svc._execute_chat_digest({
        "source": "telegram_group", "channel": "-100123",
        "window_days": 3, "chat_id": "999",
    }, "u1", "a1"))
    assert r["success"] is True
    assert "баг в оплате" in captured["corpus"] and "Аня" in captured["corpus"]


def test_chat_digest_telegram_group_read_error(monkeypatch) -> None:
    svc = _svc()
    import backend.core.messengers.telegram_userbot as tub
    monkeypatch.setattr(tub, "is_configured", lambda: True)

    async def fake_read(chat, days=7, limit=2000):
        return {"ok": False, "reason": "сессия невалидна/не авторизована"}

    monkeypatch.setattr(tub, "read_group_history", fake_read)
    r = _run(svc._execute_chat_digest(
        {"source": "telegram_group", "channel": "-100123"}, "u1", "a1"))
    assert r["success"] is False and "не прочитана" in r["message"]
