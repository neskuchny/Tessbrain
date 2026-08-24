"""Federated vector search: personal + org (P1 #6).

Тот же паттерн что graph_view, но для vector index:
  - Personal VectorIndexer всегда подключается (свои данные)
  - Если user в org — параллельно подключается org-vector (`org_vector_path`)
  - Search в обоих → merge → dedupe → RBAC-filter по access_level → top_k

Принципы:
  - **Read-only**: caller не должен писать в merged результат (как в graph_view)
  - **Personal wins при совпадении document_id**: если один документ в обоих —
    берём personal версию (writer's truth)
  - **RBAC-фильтр на результаты**: payload может содержать access_level —
    скрываем results которые caller не должен видеть (та же шкала, что
    в access.levels)
  - **Best-effort**: если org-vector недоступен/пуст — отдаём только
    personal, не валим запрос
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _filter_by_rbac(
    results: list[dict[str, Any]],
    *,
    user_id: str,
    max_level: int,
) -> list[dict[str, Any]]:
    """Фильтр результатов по access_level в payload (та же логика что graph_view).

    Owner-check: если payload.user_id == caller — видим всегда.
    Promoted-from-self: тоже видим.
    Иначе access_level (default INTERNAL=2) ≤ max_level.
    """
    from backend.core.access.levels import AccessLevel, node_visible_to

    filtered: list[dict[str, Any]] = []
    for r in results:
        payload = r.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        # node_visible_to принимает любой dict с user_id/access_level — нам подходит.
        if node_visible_to(payload, user_id=user_id, max_level=max_level):
            filtered.append(r)
    return filtered


def _dedupe_by_doc_id(
    personal_results: list[dict[str, Any]],
    org_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Personal-wins по document id. Возвращает merged список без дубликатов.

    document_id ищется в payload.document_id или payload.id (legacy).
    Если document_id отсутствует — используем text-hash как fallback,
    чтобы хоть что-то сматчить (но это imperfect — будут false-different).
    """
    def _key(r: dict[str, Any]) -> str:
        p = r.get("payload") or {}
        return p.get("document_id") or p.get("id") or r.get("id") or repr(p)[:64]

    out: dict[str, dict[str, Any]] = {}
    # Personal сначала — это «победители» при конфликте.
    for r in personal_results:
        out[_key(r)] = r
    # Org только если такого нет в personal.
    for r in org_results:
        k = _key(r)
        if k not in out:
            out[k] = r
    return list(out.values())


async def federated_vector_search(
    user_id: str,
    query: str,
    *,
    limit: int = 10,
    score_threshold: float = 0.5,
    collections: Optional[list[str]] = None,
    filter_conditions: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Search в personal + org-vector с RBAC-фильтром.

    Args:
        user_id: caller
        query: текст запроса
        limit: сколько результатов вернуть после merge+dedupe (top_k)
        score_threshold: порог schemantic similarity
        collections / filter_conditions: проброс в VectorIndexer.search

    Returns:
        Список результатов (score + payload), отсортированный по score desc.
        Empty list если ни в personal, ни в org ничего не нашлось.
    """
    # Lazy imports — модуль импортируется в hot search paths.
    from backend.core.access.levels import max_access_level_for_user
    from backend.core.ingest.membership import get_org_for_user
    from backend.core.store.tenant_paths import (
        org_vector_path,
        vector_index_path_for_user,
    )
    from backend.core.store.vector_indexer import VectorIndexer

    if not (user_id and query):
        return []

    # 1. Personal vector — всегда.
    personal_results: list[dict[str, Any]] = []
    personal = VectorIndexer(
        storage_path=vector_index_path_for_user(user_id),
        namespace=user_id,
    )
    try:
        await personal.connect()
        # Запрашиваем 2*limit, чтобы после dedupe/RBAC ещё хватило top_k.
        personal_results = await personal.search(
            query=query,
            collections=collections,
            limit=max(limit * 2, 20),
            score_threshold=score_threshold,
            filter_conditions=filter_conditions,
        ) or []
    except Exception as e:
        logger.warning("federated_vector_search: personal search failed for %s: %s", user_id, e)
    finally:
        # VectorIndexer не имеет async close(), namespace-isolation на уровне
        # объекта; ок отпустить через garbage collection.
        pass

    # 2. Org vector — только если user в org.
    org_results: list[dict[str, Any]] = []
    org_id = get_org_for_user(user_id)
    if org_id:
        import os

        org_path = org_vector_path(org_id)
        if os.path.exists(org_path):
            org_vec = VectorIndexer(
                storage_path=org_path,
                namespace=f"org_{org_id}",
            )
            try:
                await org_vec.connect()
                org_results = await org_vec.search(
                    query=query,
                    collections=collections,
                    limit=max(limit * 2, 20),
                    score_threshold=score_threshold,
                    filter_conditions=filter_conditions,
                ) or []
            except Exception as e:
                logger.warning(
                    "federated_vector_search: org search failed for %s/%s: %s",
                    user_id, org_id, e,
                )

    # 3. Merge + dedupe + RBAC-filter.
    merged = _dedupe_by_doc_id(personal_results, org_results)
    # RBAC применяется только к org-results — personal по определению
    # видны owner'у. Но дешевле прогнать общий фильтр (owner-check внутри
    # node_visible_to сам пропустит personal).
    max_level = max_access_level_for_user(user_id)
    filtered = _filter_by_rbac(merged, user_id=user_id, max_level=max_level)

    # 4. Сортировка по score desc, top_k.
    filtered.sort(key=lambda r: r.get("score", 0), reverse=True)
    return filtered[:limit]
