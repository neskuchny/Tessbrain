# -*- coding: utf-8 -*-
"""Коллекторы полевых данных для исследований аудитории (Mark).

Принцип: собираем РЕАЛЬНЫЕ тексты реальных людей (посты, отзывы, лендинги
конкурентов) — без этого исследование вырождается в «красивый отчёт из
головы LLM». Каждый фрагмент несёт source/url для цитат-происхождения.

Источники без ключей: веб-поиск (DDGS c фолбэком на html.duckduckgo.com),
любые страницы (лендинги, форумы, отзовики), Телеграм-каналы через
публичное превью t.me/s/<канал>. С ключом: VK API (VK_SERVICE_KEY).
Instagram закрыт честно — не парсим.
"""
from __future__ import annotations

import asyncio
import html as _html
import logging
import os
import re
from typing import List, Optional
from urllib.parse import quote, unquote, urlparse

logger = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
_TIMEOUT = 12.0
PAGE_CAP = 4000


def strip_html(raw: str) -> str:
    """HTML → текст без bs4: script/style вон, теги → пробел, entities."""
    s = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", raw or "")
    s = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</li>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = _html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


async def fetch_page(url: str, cap: int = PAGE_CAP) -> str:
    """Текст страницы (лендинг конкурента, форум, отзовик)."""
    try:
        import httpx
        async with httpx.AsyncClient(
                timeout=_TIMEOUT, follow_redirects=True,
                headers={"User-Agent": _UA}) as client:
            r = await client.get(url)
            ctype = r.headers.get("content-type", "")
            if r.status_code != 200 or ("html" not in ctype
                                        and "text" not in ctype):
                return ""
            return strip_html(r.text)[:cap]
    except Exception as e:
        logger.debug(f"fetch_page {url}: {e}")
        return ""


# ── Веб-поиск ────────────────────────────────────────────────────────────

def _ddgs_lib(query: str, max_results: int) -> List[dict]:
    DDGS = None
    try:
        from ddgs import DDGS  # type: ignore
    except ImportError:
        try:
            from duckduckgo_search import DDGS  # type: ignore
        except ImportError:
            return []
    out = []
    try:
        with DDGS() as d:
            for r in d.text(query, max_results=max_results):
                out.append({"title": r.get("title") or "",
                            "url": r.get("href") or r.get("url") or "",
                            "snippet": r.get("body") or ""})
    except Exception as e:
        logger.debug(f"ddgs lib failed: {e}")
    return out


async def _ddgs_html(query: str, max_results: int) -> List[dict]:
    """Фолбэк без библиотек: html.duckduckgo.com (ссылки уходят через
    /l/?uddg=<url>)."""
    try:
        import httpx
        async with httpx.AsyncClient(
                timeout=_TIMEOUT, headers={"User-Agent": _UA}) as client:
            r = await client.post("https://html.duckduckgo.com/html/",
                                  data={"q": query})
        out = []
        for m in re.finditer(
                r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                r.text, re.S):
            href, title = m.group(1), strip_html(m.group(2))
            uddg = re.search(r"[?&]uddg=([^&]+)", href)
            url = unquote(uddg.group(1)) if uddg else href
            if url.startswith("http"):
                out.append({"title": title, "url": url, "snippet": ""})
            if len(out) >= max_results:
                break
        return out
    except Exception as e:
        logger.debug(f"ddg html failed: {e}")
        return []


async def web_search(query: str, max_results: int = 5) -> List[dict]:
    res = await asyncio.to_thread(_ddgs_lib, query, max_results)
    if res:
        return res
    return await _ddgs_html(query, max_results)


# ── Телеграм: публичное превью канала (без ключей и MTProto) ────────────

def parse_tme_html(raw: str, limit: int = 30) -> List[str]:
    """Тексты постов из HTML t.me/s/<канал>."""
    posts = []
    for m in re.finditer(
            r'(?s)<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
            raw or ""):
        text = strip_html(m.group(1))
        if text and len(text) > 20:
            posts.append(text[:1500])
        if len(posts) >= limit:
            break
    return posts


async def telegram_channel_posts(channel: str, limit: int = 30) -> List[dict]:
    """Посты публичного канала: https://t.me/s/<channel>."""
    ch = channel.strip().lstrip("@").split("/")[-1]
    if not re.match(r"^[A-Za-z0-9_]{3,40}$", ch):
        return []
    try:
        import httpx
        async with httpx.AsyncClient(
                timeout=_TIMEOUT, follow_redirects=True,
                headers={"User-Agent": _UA}) as client:
            r = await client.get(f"https://t.me/s/{ch}")
        if r.status_code != 200:
            return []
        return [{"kind": "telegram", "url": f"https://t.me/{ch}",
                 "title": f"t.me/{ch}", "text": t}
                for t in parse_tme_html(r.text, limit)]
    except Exception as e:
        logger.debug(f"tg {channel}: {e}")
        return []


# ── VK: открытые группы (нужен сервисный ключ) ──────────────────────────

async def vk_group_posts(group: str, limit: int = 30, *,
                         user_id: Optional[str] = None) -> List[dict]:
    from backend.core.marketing.keys import vk_service_key
    key = await vk_service_key(user_id)
    if not key:
        return []
    dom = group.strip().lstrip("@").split("/")[-1]
    try:
        import httpx
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(
                "https://api.vk.com/method/wall.get",
                params={"domain": dom, "count": min(limit, 100),
                        "access_token": key, "v": "5.199"})
        items = (r.json().get("response") or {}).get("items") or []
        out = []
        for it in items:
            t = (it.get("text") or "").strip()
            if len(t) > 20:
                out.append({"kind": "vk", "url": f"https://vk.com/{dom}",
                            "title": f"vk.com/{dom}", "text": t[:1500]})
        return out
    except Exception as e:
        logger.debug(f"vk {group}: {e}")
        return []


def vk_configured() -> bool:
    return bool(os.getenv("VK_SERVICE_KEY", "").strip())


# ── Отзовики: через веб-поиск по site: ───────────────────────────────────

REVIEW_SITES = ("otzovik.com", "irecommend.ru", "otzyvru.com")


async def review_search(product_query: str, max_results: int = 4) -> List[dict]:
    """Отзывы: поиск по отзовикам + выкачивание страниц."""
    out: List[dict] = []
    for site in REVIEW_SITES[:2]:
        hits = await web_search(f"site:{site} {product_query}",
                                max_results=max_results)
        for h in hits[:max_results]:
            text = await fetch_page(h["url"])
            if text:
                out.append({"kind": "review", "url": h["url"],
                            "title": h["title"] or site, "text": text})
    return out


async def collect_web(query: str, max_results: int = 4) -> List[dict]:
    """Веб: поиск + текст страниц (лендинги конкурентов, форумы, статьи)."""
    out: List[dict] = []
    for h in await web_search(query, max_results=max_results):
        url = h.get("url") or ""
        host = urlparse(url).netloc
        text = await fetch_page(url)
        if text:
            out.append({"kind": "web", "url": url,
                        "title": h.get("title") or host, "text": text})
    return out
