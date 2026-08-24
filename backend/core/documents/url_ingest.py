# -*- coding: utf-8 -*-
"""
URL-ingestion — поглощение внешних ссылок в память компании.

Сценарий: пользователь кидает ссылку (статья, доки конкурента, лендинг,
отчёт) → система скачивает, извлекает читабельный текст и проводит через
СУЩЕСТВУЮЩИЙ конвейер документов (Supabase documents → sync → граф знаний +
векторный индекс) — после этого ссылка участвует в поиске/анализе/отчётах
наравне с встречами и загруженными файлами. Никакого отдельного хранилища.

Безопасность (SSRF): мы скачиваем URL С СЕРВЕРА, поэтому:
- только http/https;
- хост резолвится и блокируются приватные/loopback/link-local адреса
  (иначе ссылкой можно читать внутренние сервисы: metadata-endpoint
  облака, Redis, соседние контейнеры);
- лимит размера (2 МБ) и таймаут;
- редиректы перепроверяются той же проверкой.

Извлечение текста — stdlib html.parser (без зависимости от bs4): script/
style/nav выбрасываются, текст собирается с переводами строк по блочным
тегам. Для не-HTML (text/plain, markdown) — как есть.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
from html.parser import HTMLParser
from typing import Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

MAX_CONTENT_BYTES = 2 * 1024 * 1024  # 2 MB
FETCH_TIMEOUT = 20.0
MAX_REDIRECTS = 5

_SKIP_TAGS = {"script", "style", "noscript", "svg", "head", "iframe",
              "nav", "footer", "form", "button"}
_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5",
               "h6", "section", "article", "blockquote", "pre", "td", "th"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._chunks: list = []
        self._skip_depth = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data):
        if self._in_title and not self.title:
            self.title = data.strip()[:200]
        if self._skip_depth == 0 and data.strip():
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        # схлопываем пробелы внутри строк и пустые строки
        lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in raw.splitlines()]
        out, blank = [], 0
        for ln in lines:
            if not ln:
                blank += 1
                if blank > 1:
                    continue
            else:
                blank = 0
            out.append(ln)
        return "\n".join(out).strip()


def html_to_text(html: str) -> Tuple[str, str]:
    """HTML → (текст, title)."""
    p = _TextExtractor()
    try:
        p.feed(html)
    except Exception:
        logger.debug("html parse error", exc_info=True)
    return p.text(), p.title


def _check_url_safe(url: str) -> Optional[str]:
    """None если URL безопасен, иначе строка-причина отказа (SSRF-guard)."""
    try:
        parsed = urlparse(url)
    except Exception:
        return "невалидный URL"
    if parsed.scheme not in ("http", "https"):
        return f"схема {parsed.scheme!r} не разрешена (только http/https)"
    host = parsed.hostname
    if not host:
        return "URL без хоста"
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return f"хост не резолвится: {host}"
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return f"адрес {ip} — внутренний/приватный (запрещено)"
    return None


async def fetch_url_text(url: str) -> dict:
    """Скачать URL и извлечь текст. Возвращает
    {ok, url, final_url, title, text, content_type} или {ok: False, error}."""
    import httpx

    reason = _check_url_safe(url)
    if reason:
        return {"ok": False, "error": f"URL отклонён: {reason}"}

    try:
        async with httpx.AsyncClient(
                timeout=FETCH_TIMEOUT, follow_redirects=False,
                headers={"User-Agent": "TessentBrain/1.0 (+knowledge-ingest)"}) as client:
            current = url
            for _ in range(MAX_REDIRECTS + 1):
                resp = await client.get(current)
                if resp.status_code in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("location")
                    if not loc:
                        return {"ok": False, "error": "редирект без location"}
                    current = str(httpx.URL(current).join(loc))
                    reason = _check_url_safe(current)  # перепроверяем цель редиректа
                    if reason:
                        return {"ok": False, "error": f"редирект отклонён: {reason}"}
                    continue
                break
            else:
                return {"ok": False, "error": "слишком много редиректов"}

            if resp.status_code != 200:
                return {"ok": False, "error": f"HTTP {resp.status_code}"}
            body = resp.content[:MAX_CONTENT_BYTES]
            ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    except Exception as e:
        return {"ok": False, "error": f"не удалось скачать: {e}"}

    charset = "utf-8"
    try:
        raw_text = body.decode(charset, errors="replace")
    except Exception:
        raw_text = body.decode("utf-8", errors="replace")

    if "html" in ctype or raw_text.lstrip()[:200].lower().startswith(("<!doctype", "<html")):
        text, title = html_to_text(raw_text)
    elif ctype.startswith("text/") or ctype in ("application/json", "application/xml"):
        text, title = raw_text.strip(), ""
    else:
        return {"ok": False,
                "error": f"тип контента {ctype or 'unknown'} не поддерживается "
                         "(html/текст; PDF загружайте через документы)"}

    if len(text) < 200:
        return {"ok": False,
                "error": "извлечено слишком мало текста (<200 символов) — "
                         "страница пустая или контент рендерится JS"}

    return {"ok": True, "url": url, "final_url": current, "title": title,
            "text": text, "content_type": ctype}
