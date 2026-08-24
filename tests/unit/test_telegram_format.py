# -*- coding: utf-8 -*-
"""Markdown → Telegram: конвертация, чанки, отправка с фолбэком.

Регрессии на жалобы из продакшена: «В тг нет форматирования» (сырые
`## 🫀 Пульс исполнения` и `**Итого:**` в сообщении) и «Не вывел это как
таблицу, просто текст» (markdown-таблица сырыми |---| в Telegram).
"""
import asyncio
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.core.notifications.telegram_format import (  # noqa: E402
    html_to_plain,
    markdown_to_telegram,
    post_telegram_text,
    split_chunks,
)


# ── конвертация ─────────────────────────────────────────────────────────────

def test_heading_and_bold_become_html():
    md = "## 🫀 Пульс исполнения\n\n**Итого:** открыто 0"
    out = markdown_to_telegram(md, mode="html")
    assert "<b>🫀 Пульс исполнения</b>" in out
    assert "<b>Итого:</b> открыто 0" in out
    assert "##" not in out and "**" not in out, \
        "сырые маркеры markdown — ровно то, на что жаловались"


def test_plain_mode_strips_markers():
    md = "## Заголовок\n**жирный** и `код`\n- пункт"
    out = markdown_to_telegram(md, mode="plain")
    assert "<" not in out and "#" not in out and "**" not in out
    assert "Заголовок" in out and "жирный и код" in out
    assert "• пункт" in out


def test_table_rendered_with_bold_header_and_no_separator():
    md = ("| Утверждение | Статус |\n"
          "|---|---|\n"
          "| выручка 1М | подтверждено |")
    out = markdown_to_telegram(md, mode="html")
    assert "<b>Утверждение · Статус</b>" in out, "шапка таблицы — жирным"
    assert "выручка 1М · подтверждено" in out
    assert "---" not in out, "разделитель |---| не должен доехать до чата"


def test_html_escaping_of_user_text():
    out = markdown_to_telegram("сравни a<b и b>c & **x**", mode="html")
    assert "a&lt;b" in out and "b&gt;c" in out and "&amp;" in out
    assert "<b>x</b>" in out, "наши теги живут рядом с экранированным текстом"


def test_heading_with_inline_bold_has_no_nested_tags():
    out = markdown_to_telegram("## Итог: **важно**", mode="html")
    assert out.count("<b>") == 1, "вложенный <b> внутри <b> Telegram отвергает"
    assert "важно" in out


def test_horizontal_rule_becomes_blank():
    out = markdown_to_telegram("до\n---\nпосле", mode="plain")
    assert "---" not in out


def test_never_raises_on_garbage():
    assert markdown_to_telegram(None) == ""          # type: ignore[arg-type]
    assert markdown_to_telegram("") == ""


# ── чанки ───────────────────────────────────────────────────────────────────

def test_split_chunks_respects_line_boundaries():
    text = "\n".join(f"строка {i}" for i in range(400))
    chunks = split_chunks(text, limit=200)
    assert all(len(c) <= 200 for c in chunks)
    assert "\n".join(chunks) == text, "ничего не потеряно и не переупорядочено"


def test_split_chunks_long_line_keeps_order():
    """Регрессия: сверхдлинная строка не должна вставать ПЕРЕД уже
    накопленным буфером."""
    text = "короткая\n" + "х" * 500
    chunks = split_chunks(text, limit=200)
    assert chunks[0] == "короткая"
    assert "".join(chunks[1:]) == "х" * 500


def test_html_to_plain_roundtrip():
    html = markdown_to_telegram("**жирный** и a<b", mode="html")
    assert html_to_plain(html) == "жирный и a<b"


# ── отправка ────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status_code=200, ok=True, message_id=7):
        self.status_code = status_code
        self._ok = ok
        self._mid = message_id

    def json(self):
        return {"ok": self._ok, "result": {"message_id": self._mid}}


def _client_factory(calls, responses):
    """httpx.AsyncClient-мок: пишет каждый POST в calls, отвечает по очереди."""
    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            calls.append(json)
            return responses.pop(0) if responses else _Resp()

    return lambda **kw: _Client()


def test_post_sends_html_parse_mode(monkeypatch):
    calls = []
    monkeypatch.setattr("httpx.AsyncClient", _client_factory(calls, []))
    res = asyncio.run(post_telegram_text(
        "tok", "42", "## Отчёт\n**Итого:** ок"))
    assert res["ok"] and res["parts"] == 1 and res["message_id"] == 7
    assert calls[0]["parse_mode"] == "HTML"
    assert "<b>Отчёт</b>" in calls[0]["text"]


def test_post_falls_back_to_plain_on_400(monkeypatch):
    """Telegram отверг HTML (например, битые сущности) → тот же кусок уходит
    чистым текстом: доставка важнее вида."""
    calls = []
    monkeypatch.setattr(
        "httpx.AsyncClient",
        _client_factory(calls, [_Resp(status_code=400, ok=False), _Resp()]))
    res = asyncio.run(post_telegram_text("tok", "42", "**жирный** текст"))
    assert res["ok"] and res["parts"] == 1
    assert len(calls) == 2
    assert "parse_mode" not in calls[1]
    assert calls[1]["text"] == "жирный текст", "фолбэк без тегов"


def test_post_stops_after_double_failure(monkeypatch):
    """Чат недоступен (HTML и plain оба не ушли) — не долбим остальные куски."""
    calls = []
    monkeypatch.setattr(
        "httpx.AsyncClient",
        _client_factory(calls, [_Resp(400, False), _Resp(400, False)]))
    long_text = "\n".join("строка %d" % i for i in range(2000))
    res = asyncio.run(post_telegram_text("tok", "42", long_text))
    assert not res["ok"] and res["parts"] == 0
    assert len(calls) == 2, "один кусок × (HTML + plain), дальше стоп"


def test_post_chunks_long_text(monkeypatch):
    calls = []
    monkeypatch.setattr("httpx.AsyncClient", _client_factory(calls, []))
    long_text = "\n".join("пункт %d" % i for i in range(1200))
    res = asyncio.run(post_telegram_text("tok", "42", long_text))
    assert res["ok"] and res["parts"] == len(calls) and len(calls) > 1


def test_post_refuses_without_token_or_chat(monkeypatch):
    calls = []
    monkeypatch.setattr("httpx.AsyncClient", _client_factory(calls, []))
    assert asyncio.run(post_telegram_text("", "42", "x"))["ok"] is False
    assert asyncio.run(post_telegram_text("tok", "", "x"))["ok"] is False
    assert asyncio.run(post_telegram_text("tok", "42", " "))["ok"] is False
    assert calls == [], "ни одного сетевого вызова"
