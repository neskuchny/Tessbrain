# -*- coding: utf-8 -*-
"""Фрагменты знаний по теме — общий вход для «бедных» потребителей поиска.

ЗАЧЕМ. Аудит SEARCH_CONSUMERS_AUDIT показал: узел досок «Вопрос мозгу» и
Mini Tess собирали контекст ГОЛЫМ BM25 — один канал из трёх, без усиления
графа на перечислениях, без временного диапазона, без расширений. Вопрос
«покажи всех, кто…» в brain-чате находил всех, а тот же вопрос боту терял
участников — ровно измеренный нами провал.

Этот модуль даёт им тот же гибридный поиск, которым пользуется brain-чат,
с двумя гарантиями:

1. BM25-фолбэк ВСЕГДА: гибрид не собрался / упал / вернул пусто → прежнее
   поведение. Хуже, чем было, стать не может.
2. Бюджет времени уважается. У мессенджера 3 секунды на контекст — первая
   сборка движков в них не влезает. Поэтому build_if_missing=False:
   гибрид только если уже собран (peek), иначе быстрый BM25 сейчас и
   фоновая сборка на следующий раз. Доски (автоматизации, латентность не
   критична) собирают сразу.

Движки регистрируются в том же per-user singleton, что у brain-чата
(get_hybrid_search_orchestrator) — если пользователь уже ходил в чат,
доски и бот берут ГОТОВЫЙ комплект, ничего не строя.

Выключатель: TESSENT_CTX_HYBRID=off — все потребители возвращаются к
голому BM25.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# кто уже строится фоном — защита от лавины сборок на серии сообщений
_building: set = set()


def _enabled() -> bool:
    return (os.getenv("TESSENT_CTX_HYBRID", "") or "").strip().lower() not in (
        "off", "0", "false", "no")


def _bm25_fragments(user_id: Optional[str], question: str,
                    top_k: int, frag_len: int) -> List[str]:
    """Прежний путь — байт-в-байт то, что делали доски и Mini Tess."""
    from backend.core.search.bm25_searcher import get_bm25_searcher
    hits = get_bm25_searcher(user_id=user_id).search(question, top_k=top_k)
    frags = [str(getattr(h, "text", "") or "")[:frag_len] for h in hits]
    return [f for f in frags if f]


async def _build_and_register(user_id: Optional[str]) -> None:
    """Собрать per-user комплект движков и положить в общий singleton.

    Тот же состав, что строит brain-чат: federated graph view + персональный
    векторный индекс. Граф НЕ закрываем — он живёт в singleton'е, как и у
    чата. Never-raise."""
    key = user_id or "default"
    if key in _building:
        return
    _building.add(key)
    try:
        from backend.core.search.hybrid_search_orchestrator import (
            get_hybrid_search_orchestrator,
            peek_hybrid_search_orchestrator,
        )
        if peek_hybrid_search_orchestrator(user_id) is not None:
            return  # кто-то успел раньше (чат, параллельный вызов)
        from backend.core.store.graph_view import merged_graph_view_for_user
        from backend.core.store.tenant_paths import vector_index_path_for_user
        from backend.core.store.vector_indexer import VectorIndexer

        gb = await merged_graph_view_for_user(user_id, use_networkx=None)
        vi = VectorIndexer(storage_path=vector_index_path_for_user(user_id),
                           namespace=user_id)
        await vi.connect()
        get_hybrid_search_orchestrator(
            vector_indexer=vi, graph_builder=gb, user_id=user_id)
        logger.info("context_fragments: гибрид собран для %s…", str(user_id)[:8])
    except Exception:
        logger.debug("context_fragments: сборка гибрида не удалась",
                     exc_info=True)
    finally:
        _building.discard(key)


async def topic_fragments(user_id: Optional[str], question: str, *,
                          top_k: int = 6, frag_len: int = 400,
                          build_if_missing: bool = True,
                          ) -> Tuple[List[str], str]:
    """Top-k текстовых фрагментов по вопросу. Возврат (фрагменты, движок).

    Движок в ответе — «hybrid» или «bm25»: потребитель может честно
    показать в диагностике, каким путём собран контекст.
    """
    question = (question or "").strip()
    if not question:
        return [], ""

    if _enabled():
        try:
            from backend.core.search.hybrid_search_orchestrator import (
                peek_hybrid_search_orchestrator,
            )
            orch = peek_hybrid_search_orchestrator(user_id)
            if orch is None and build_if_missing:
                await _build_and_register(user_id)
                orch = peek_hybrid_search_orchestrator(user_id)
            elif orch is None:
                # бюджет времени дорог: BM25 сейчас, гибрид — со следующего раза
                asyncio.get_running_loop().create_task(
                    _build_and_register(user_id))
            if orch is not None:
                results = await orch.search(query=question, top_k=top_k)
                frags = [str(getattr(r, "text", "") or "")[:frag_len]
                         for r in results]
                frags = [f for f in frags if f]
                if frags:
                    return frags, "hybrid"
                # гибрид отработал, но пусто — BM25 может знать больше
                # (индексы наполняются разными путями), падаем в фолбэк
        except Exception:
            logger.debug("topic_fragments: гибрид упал → bm25", exc_info=True)

    try:
        return _bm25_fragments(user_id, question, top_k, frag_len), "bm25"
    except Exception:
        logger.debug("topic_fragments: и bm25 упал", exc_info=True)
        return [], ""


__all__ = ["topic_fragments"]
