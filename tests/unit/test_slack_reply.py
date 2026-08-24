# -*- coding: utf-8 -*-
"""Slack отвечает: разметка и адресация ответа.

Закрыта половина интеграции из бизнес-карты: «Slack принимает сообщения,
но ответ обратно не отправляет». Входящее работало, ответ упирался в
токен бота и в то, что Slack не понимает markdown.

Контракты:
  1. заголовки и жирный переводятся в разметку Slack, а не остаются
     сырыми звёздочками;
  2. таблица разворачивается в строки — моноширинная на телефоне
     разъезжается;
  3. ссылки в формате Slack; блоки кода не трогаются;
  4. длинный ответ режется по строкам, а не посреди слова;
  5. личность берётся из поля пользователя, адрес ответа — из поля
     канала: это разные вещи, и путать их нельзя;
  6. ответ уходит веткой под вопросом;
  7. ok=false от Slack (он отвечает 200 с ошибкой в теле) не считается
     успехом — молчаливой доставки «в никуда» быть не должно.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, relpath))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_sf = _load("slack_format_t", "backend/core/notifications/slack_format.py")


def _src(relpath: str) -> str:
    with open(os.path.join(ROOT, relpath), encoding="utf-8") as f:
        return f.read()


# ── 1-3. Разметка ───────────────────────────────────────────────────────

def test_headings_and_bold_converted():
    out = _sf.markdown_to_mrkdwn("## Итоги\n**Выручка** выросла")
    assert "*Итоги*" in out and "#" not in out
    assert "*Выручка*" in out and "**" not in out, (
        "сырые звёздочки в Slack видны пользователю"
    )
    print("✅ заголовки и жирный переведены в разметку Slack")


def test_table_becomes_readable_lines():
    md = "| Отдел | Статус |\n|---|---|\n| Продажи | ок |"
    out = _sf.markdown_to_mrkdwn(md)
    assert "|---|" not in out and "Продажи · ок" in out
    print("✅ таблица развёрнута в строки")


def test_links_and_code_blocks():
    out = _sf.markdown_to_mrkdwn("см. [отчёт](https://e.com/r)")
    assert "<https://e.com/r|отчёт>" in out
    code = "```\n## это код, не заголовок\n```"
    assert "## это код" in _sf.markdown_to_mrkdwn(code), (
        "содержимое блока кода не переписываем"
    )
    print("✅ ссылки в формате Slack, код не тронут")


def test_bullets_survive():
    out = _sf.markdown_to_mrkdwn("- первый\n- второй")
    assert out.count("•") == 2
    print("✅ списки читаются")


# ── 4. Длинный ответ ────────────────────────────────────────────────────

def test_long_reply_split_on_line_boundaries():
    text = "\n".join(f"строка номер {i}" for i in range(400))
    chunks = _sf.split_chunks(text, limit=500)
    assert len(chunks) > 1
    assert all(len(c) <= 500 for c in chunks)
    assert "".join(c.replace("\n", "") for c in chunks).count("строка") == 400, (
        "при разрезании не должно теряться содержимое"
    )
    print("✅ длинный ответ режется без потери текста")


# ── 5-6. Адресация ──────────────────────────────────────────────────────

def test_identity_and_reply_address_are_different_fields():
    src = _src("backend/api/routes/messengers.py")
    fn = src[src.index("async def slack_webhook"):]
    fn = fn[:fn.index("# === Outbound helper")]
    assert 'external_id = event.get("user")' in fn, "личность — из поля user"
    assert 'channel = event.get("channel")' in fn, "адрес ответа — из channel"
    assert "thread_ts" in fn, "ответ уходит веткой под вопросом"
    assert "_send_slack(bot_token, channel" in fn
    print("✅ личность из user, адрес ответа из channel, ответ веткой")


def test_missing_token_is_visible_not_silent():
    src = _src("backend/api/routes/messengers.py")
    fn = src[src.index("async def slack_webhook"):]
    fn = fn[:fn.index("# === Outbound helper")]
    assert 'event="reply_sent" if sent else "reply_failed"' in fn, (
        "неотправленный ответ обязан быть виден в метрике"
    )
    assert '"sent": sent' in fn
    cfg = _src("backend/config.py")
    assert "slack_bot_token: str = \"\"" in cfg
    print("✅ неотправленный ответ виден, токен вынесен в настройки")


# ── 7. Ошибка Slack не считается успехом ────────────────────────────────

def test_slack_error_response_is_not_success():
    class _Resp:
        content = b"{}"
        status_code = 200

        @staticmethod
        def json():
            return {"ok": False, "error": "channel_not_found"}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _Resp()

    import types as _t
    fake = _t.ModuleType("httpx")
    fake.AsyncClient = lambda *a, **k: _Client()
    sys.modules["httpx"] = fake
    try:
        res = asyncio.run(_sf.post_slack_text("xoxb-t", "C1", "привет"))
    finally:
        sys.modules.pop("httpx", None)
    assert res["ok"] is False and res["error"] == "channel_not_found", (
        "Slack отвечает 200 с ошибкой в теле — это не доставка"
    )
    print("✅ ошибка Slack не выдаётся за успешную отправку")


def test_empty_input_rejected():
    res = asyncio.run(_sf.post_slack_text("", "C1", "текст"))
    assert res["ok"] is False
    res = asyncio.run(_sf.post_slack_text("xoxb-t", "C1", "   "))
    assert res["ok"] is False
    print("✅ пустой ввод не отправляется")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе тесты ответа в Slack прошли.")
