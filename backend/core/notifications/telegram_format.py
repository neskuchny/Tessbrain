# -*- coding: utf-8 -*-
"""Markdown → Telegram: конвертация разметки и единая отправка.

Telegram без parse_mode показывает `**жирный**`, `## заголовок` и
markdown-таблицы сырыми символами («в тг нет форматирования»). Legacy
parse_mode="Markdown" ещё хуже: непарная * или _ в тексте LLM — и
Telegram отвечает 400, сообщение теряется молча.

Здесь конвертируем markdown в Telegram-HTML (<b>/<code>), где парность
тегов гарантирована конструкцией (теги появляются только заменой ПАРНЫХ
markdown-маркеров), а весь остальной текст экранирован. Если Telegram
всё же отверг HTML — тот же кусок уходит повторно чистым текстом:
доставка важнее вида.

Таблицы Telegram не рендерит ни в одном parse_mode: строки складываем
через « · », строку-шапку выделяем жирным, разделители |---| выбрасываем.
"""
from __future__ import annotations

import html as _html
import logging
import re
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_CODE_RE = re.compile(r"`([^`]+)`")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_BULLET_RE = re.compile(r"^[-*]\s+")
_TABLE_SEP_RE = re.compile(r"\|?[\s:|-]+\|?")
_TAG_RE = re.compile(r"</?(?:b|i|code|pre)>")


def _inline(ln: str, html_mode: bool) -> str:
    """**жирный**/__жирный__ и `код` → теги (html) или чистый текст (plain)."""
    if html_mode:
        ln = _BOLD_RE.sub(
            lambda m: "<b>%s</b>" % (m.group(1) or m.group(2) or ""), ln)
        ln = _CODE_RE.sub(r"<code>\1</code>", ln)
    else:
        ln = _BOLD_RE.sub(lambda m: m.group(1) or m.group(2) or "", ln)
        ln = _CODE_RE.sub(r"\1", ln)
    return ln


def markdown_to_telegram(text: str, mode: str = "plain") -> str:
    """Markdown → текст для Telegram. mode="html" даёт <b>/<code>-разметку
    (слать с parse_mode=HTML), mode="plain" — чистый текст без parse_mode.
    Never-raise: на входе-мусоре вернёт исходный текст."""
    try:
        html_mode = mode == "html"
        lines = (text or "").split("\n")
        # шапка таблицы = строка |…|, за которой идёт разделитель |---|
        header_rows = set()
        for i in range(len(lines) - 1):
            s, nxt = lines[i].strip(), lines[i + 1].strip()
            if (s.startswith("|") and nxt.startswith("|")
                    and _TABLE_SEP_RE.fullmatch(nxt)):
                header_rows.add(i)

        out: List[str] = []
        for i, raw in enumerate(lines):
            s = raw.strip()
            # горизонтальная линия --- / *** / === → пустая строка
            if len(s) >= 3 and set(s) <= {"-", "*", "_", "="}:
                out.append("")
                continue
            # разделитель markdown-таблицы |---|:---| — мусор, пропускаем
            if s.startswith("|") and _TABLE_SEP_RE.fullmatch(s):
                continue
            esc = _html.escape(s, quote=False) if html_mode else s
            h = _HEADING_RE.match(esc)
            if h:
                if out and out[-1] != "":
                    out.append("")
                # внутри заголовка инлайн-маркеры разворачиваем в текст:
                # вложенный <b> внутри <b> Telegram не переваривает
                body = _inline(h.group(2).strip(), html_mode=False)
                out.append(f"<b>{body}</b>" if html_mode else body)
                continue
            if _BULLET_RE.match(s):
                out.append("• " + _inline(_BULLET_RE.sub("", esc), html_mode))
                continue
            if s.startswith("|"):
                cells = [c.strip() for c in esc.strip("|").split("|")]
                if i in header_rows:
                    row = " · ".join(_inline(c, False) for c in cells if c)
                    out.append(f"<b>{row}</b>" if html_mode else row)
                else:
                    row = " · ".join(_inline(c, html_mode)
                                     for c in cells if c)
                    out.append(row)
                continue
            out.append(_inline(esc, html_mode))
        res = "\n".join(out)
        return re.sub(r"\n{3,}", "\n\n", res).strip()
    except Exception:
        logger.debug("markdown_to_telegram failed", exc_info=True)
        return text or ""


def html_to_plain(html_text: str) -> str:
    """Наши <b>/<code>-теги → чистый текст (фолбэк при отказе parse_mode)."""
    return _html.unescape(_TAG_RE.sub("", html_text or ""))


def split_chunks(text: str, limit: int = 3500) -> List[str]:
    """Разбить текст на куски ≤limit по границам строк (лимит Telegram 4096).
    Сверхдлинная строка без переносов режется жёстко, но НЕ раньше уже
    накопленного буфера — порядок сообщений сохраняется."""
    chunks: List[str] = []
    buf = ""
    for ln in (text or "").split("\n"):
        if len(ln) > limit:
            if buf:
                chunks.append(buf)
                buf = ""
            for j in range(0, len(ln), limit):
                chunks.append(ln[j:j + limit])
            continue
        if buf and len(buf) + len(ln) + 1 > limit:
            chunks.append(buf)
            buf = ln
        else:
            buf = f"{buf}\n{ln}" if buf else ln
    if buf:
        chunks.append(buf)
    return chunks


async def post_telegram_text(token: str, chat_id: str, markdown_text: str,
                             *, timeout: float = 15.0, chunk_limit: int = 3500,
                             max_chunks: int = 8,
                             disable_preview: bool = True) -> Dict[str, Any]:
    """Единая отправка текста в Telegram: markdown → HTML, чанки, фолбэк.

    Возвращает {"ok", "parts", "message_id"}: ok=True, если доставлен хотя бы
    один кусок; message_id — последнего доставленного. Never-raise."""
    res: Dict[str, Any] = {"ok": False, "parts": 0, "message_id": None}
    if not (token and chat_id and (markdown_text or "").strip()):
        return res
    try:
        import httpx
    except Exception:
        return res

    html_text = markdown_to_telegram(markdown_text, mode="html")
    chunks = split_chunks(html_text, limit=chunk_limit)
    if len(chunks) > max_chunks:
        logger.warning("telegram post: текст обрезан до %d сообщений из %d",
                       max_chunks, len(chunks))
        chunks = chunks[:max_chunks]

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for ch in chunks:
                data: Dict[str, Any] = {}
                try:
                    r = await client.post(url, json={
                        "chat_id": chat_id, "text": ch, "parse_mode": "HTML",
                        "disable_web_page_preview": disable_preview})
                    try:
                        data = r.json()
                    except Exception:
                        data = {}
                    ok = r.status_code == 200 and bool(data.get("ok", True))
                except Exception:
                    ok = False
                if not ok:
                    # повтор без parse_mode — доставка важнее вида
                    try:
                        r = await client.post(url, json={
                            "chat_id": chat_id, "text": html_to_plain(ch),
                            "disable_web_page_preview": disable_preview})
                        try:
                            data = r.json()
                        except Exception:
                            data = {}
                        ok = (r.status_code == 200
                              and bool(data.get("ok", True)))
                    except Exception:
                        ok = False
                if ok:
                    res["ok"] = True
                    res["parts"] += 1
                    mid = (data.get("result") or {}).get("message_id")
                    if mid is not None:
                        res["message_id"] = mid
                else:
                    # чат недоступен (бот заблокирован/битый chat_id) —
                    # остальные куски не долбим
                    logger.warning("telegram post: кусок не доставлен, "
                                   "останавливаемся (%s)",
                                   str(data.get("description") or "")[:200])
                    break
    except Exception:
        logger.warning("telegram post failed", exc_info=True)
    return res


__all__ = ["markdown_to_telegram", "html_to_plain", "split_chunks",
           "post_telegram_text"]
