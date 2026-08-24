# -*- coding: utf-8 -*-
"""
Единый веб-поиск для бэкенда: Tavily (если есть ключ) → DDGS (без ключа) →
пусто. Best-effort, никаких жёстких зависимостей — используется классическими
автоматизациями («сходи посмотри новости/сайты по теме»).

Раньше эта лесенка была продублирована в analyze._web_enrich и
nightly_consolidation; здесь — переиспользуемая версия. Возвращает и
структуру (для сборки корпуса), и готовую markdown-строку.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


async def web_search(query: str, *, max_results: int = 5) -> list[dict[str, Any]]:
    """Найти в вебе. Возвращает [{title, snippet, url}]. Никогда не raises.

    Порядок: Tavily (TAVILY_API_KEY) → DDGS-библиотека (ddgs/duckduckgo_search)
    → []. Snippet обрезается до ~300 символов."""
    q = (query or "").strip()
    if len(q) < 3:
        return []

    # Закрытый контур: веб-поиск не выполняется. Наружу уходит не только
    # запрос к поисковику — сам ТЕКСТ запроса собран из данных компании,
    # и это утечка сама по себе. DDGS работает без ключа, поэтому «просто
    # не задавать ключ» контур не закрывало.
    try:
        from backend.core.security.perimeter import enterprise_mode_enabled
        if enterprise_mode_enabled():
            logger.info("web_search: закрытый контур — поиск наружу отключён")
            return []
    except Exception:
        pass

    results: list[dict[str, Any]] = []

    # 1) Tavily — если настроен ключ (лучшее качество).
    tav = os.environ.get("TAVILY_API_KEY")
    if tav:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15.0) as cl:
                r = await cl.post("https://api.tavily.com/search", json={
                    "api_key": tav, "query": q, "max_results": max_results,
                    "search_depth": "basic"})
                if r.status_code == 200:
                    for it in (r.json().get("results") or [])[:max_results]:
                        results.append({
                            "title": str(it.get("title", "")),
                            "snippet": str(it.get("content", ""))[:300],
                            "url": str(it.get("url", "")),
                        })
        except Exception as e:
            logger.debug("web_search: Tavily failed: %s", e)

    # 2) DDGS — без ключа. Пакет мог переехать ddgs→duckduckgo_search.
    if not results:
        DDGS = None
        try:
            from ddgs import DDGS as _D
            DDGS = _D
        except Exception:
            try:
                from duckduckgo_search import DDGS as _D2
                DDGS = _D2
            except Exception:
                DDGS = None
        if DDGS is not None:
            try:
                with DDGS() as ddgs:
                    for it in list(ddgs.text(q, max_results=max_results)):
                        results.append({
                            "title": str(it.get("title", "")),
                            "snippet": str(it.get("body", ""))[:300],
                            "url": str(it.get("href", "") or it.get("url", "")),
                        })
            except Exception as e:
                logger.debug("web_search: DDGS failed: %s", e)

    return results[:max_results]


def format_results_md(query: str, results: list[dict[str, Any]]) -> str:
    """Собрать результаты поиска в markdown-блок для корпуса."""
    if not results:
        return f"### Поиск: {query}\n(ничего не найдено)\n"
    lines = [f"### Поиск: {query}"]
    for r in results:
        title = r.get("title") or r.get("url") or "—"
        lines.append(f"- **{title}** ({r.get('url', '')})\n  {r.get('snippet', '')}")
    return "\n".join(lines) + "\n"


__all__ = ["web_search", "format_results_md"]
