"""Federated read view: personal-graph ∪ org-graph для одного user'а.

Контекст. После Wave 1 (ingest dedup + promote) данные ингестятся в
personal-graph пользователя, а затем копируются в org-graph (если user
привязан к org и источник — corp). Но 18+ мест в кодовой базе всё ещё
читают только `graph_path_for_user(user_id)` через GraphBuilder, то есть
не видят данные коллег из той же org.

Этот модуль даёт **read-only merged view**: на вход user_id, на выход —
GraphBuilder с `nx_graph = personal ∪ org` в памяти.

⚠️  WRITERS — ВНИМАНИЕ ⚠️
=========================
merged_graph_view_for_user возвращает GraphBuilder с **заблокированным
_save_graph_to_file** (см. _readonly_save_blocker ниже). Это
**намеренная защита** для предотвращения утечки границы personal/org:
если бы save работал, org-данные из in-memory merged-графа перетекли
бы в personal-файл при следующем dump'е.

Если ваш код **пишет** в граф (create_node / update_node /
create_relationship / save) — НЕ используйте merged view.
Используйте либо:
  1. `writer_graph_for_user(user_id)` (helper ниже) — для writes в
     personal-граф пользователя
  2. `GraphBuilder(graph_storage_path=graph_path_for_user(user_id))`
     напрямую — старый паттерн до Wave 2

Конкретные writers в проекте (для контроля):
  - `knowledge_sync._process_single_meeting` — пишет в personal через
    свой собственный GraphBuilder (не merged); правильно
  - `CaptureOrchestrator._reinforce_memory` — пишет в graph_builder,
    но instantiated с personal-builder из knowledge_sync; правильно
  - `mem0_client._extract_entities` — пишет через self.graph.add_node;
    сейчас mem0 НЕ подключён к merged view, но если будет — нужен
    отдельный writer-builder
  - `corrections.py` route — намеренно НЕ использует merged view
    (это writer-flow, two-phase correction manager)

Принципы:
  - **Один in-memory граф**: вместо 2 GraphBuilder'ов даём один merged.
    Caller'у не нужно менять логику (search/reasoning работает как было).
  - **Personal wins при конфликте node_id**: если узел есть и в personal,
    и в org — берём personal (это writer's truth для этого user'а).
  - **Save заблокирован**: попытка save → no-op + ERROR-log
  - **NetworkX-only merge**: для Neo4j-mode (shared instance) merge не
    нужен, возвращаем GraphBuilder как есть (Cypher автоматически даёт
    видеть всех узлов в нужном tenant'е).
  - **Best-effort на ошибках**: если org-graph битый/недоступен — личный
    flow продолжает работать без него, с warning'ом в логи.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _readonly_save_blocker(*args, **kwargs):
    """Заглушка для _save_graph_to_file у merged view.

    Не raise (caller не должен ломаться), но громко логируем —
    если кто-то по ошибке попытается сохранить merged граф, это
    видно в проде.
    """
    logger.error(
        "REFUSED: attempted to save a merged graph view back to disk. "
        "Merged views are read-only by construction — use the per-tenant "
        "personal GraphBuilder for writes."
    )


def _merge_org_into_personal(
    gb,
    org_graph_file: Path,
    *,
    user_id: str = "",
    max_access_level: Optional[int] = None,
) -> tuple[int, int, int]:
    """In-place: добавить узлы и рёбра из org-graph в gb.nx_graph с RBAC-фильтром.

    Args:
        gb: GraphBuilder (personal-граф)
        org_graph_file: путь к org-graph JSON
        user_id: caller (для owner-check — свои узлы не фильтруем)
        max_access_level: предвычисленный max_level (если None — узнаём через
            membership внутри). Передавайте чтобы избежать N+1 lookup'ов.

    Returns:
        (added_nodes, added_edges, filtered_out_nodes) — для логов/наблюдаемости.
        filtered_out_nodes — сколько узлов скрыто RBAC-фильтром.
    """
    import json

    import networkx as nx

    # Lazy: RBAC модуль импортируется только если merge актуален.
    from backend.core.access.levels import (
        department_for_user,
        max_access_level_for_user,
        node_visible_to,
    )

    if max_access_level is None:
        max_access_level = max_access_level_for_user(user_id)
    # P2 мульти-аккаунта: отдел зрителя — один lookup на merge (не N+1 на
    # узел). Контент чужого отдела виден только с уровня руководителя.
    viewer_department = department_for_user(user_id)

    try:
        with open(org_graph_file, "r", encoding="utf-8") as f:
            org_data = json.load(f)
    except Exception as e:
        logger.warning("merged_view: failed to load org graph %s: %s", org_graph_file, e)
        return (0, 0, 0)

    org_g = nx.MultiDiGraph()
    for node in org_data.get("nodes", []):
        node_id = node.get("id")
        if not node_id:
            continue
        attrs = {k: v for k, v in node.items() if k != "id"}
        org_g.add_node(node_id, **attrs)
    for edge in org_data.get("edges", []):
        src = edge.get("source")
        tgt = edge.get("target")
        if not (src and tgt):
            continue
        org_g.add_edge(src, tgt, key=edge.get("key", 0), **edge.get("data", {}))

    # P1 #5: RBAC-фильтр. Сначала определяем какие nodes видны user'у,
    # затем добавляем только их. Edges фильтруются: оба конца должны быть
    # видны (иначе мы леакаем «есть связь с чем-то невидимым»).
    visible_ids: set[str] = set()
    filtered_out = 0
    for nid, attrs in org_g.nodes(data=True):
        if node_visible_to(attrs, user_id=user_id, max_level=max_access_level,
                           viewer_department=viewer_department):
            visible_ids.add(nid)
        else:
            filtered_out += 1

    added_nodes = 0
    added_edges = 0
    # Personal wins: добавляем только тех, кого нет в personal.
    for nid in visible_ids:
        attrs = org_g.nodes[nid]
        if not gb.nx_graph.has_node(nid):
            gb.nx_graph.add_node(nid, **attrs)
            added_nodes += 1
            # Обновляем индекс label/key для совместимости с GraphBuilder lookup.
            label = attrs.get("_label", "")
            key = attrs.get("_key", "")
            if label and key and hasattr(gb, "_node_index"):
                gb._node_index[(label, key)] = nid

    for u, v, k, attrs in org_g.edges(keys=True, data=True):
        # MultiDiGraph: edge identified by (u, v, key). Дубликат не добавляем.
        # RBAC: ребро видно только если оба конца видны caller'у. Это
        # предотвращает leak вида «у тебя есть связь с CONFIDENTIAL узлом
        # которого ты не видишь» (можно было бы догадаться о его id).
        if u not in visible_ids or v not in visible_ids:
            continue
        if not gb.nx_graph.has_edge(u, v, key=k):
            gb.nx_graph.add_edge(u, v, key=k, **attrs)
            added_edges += 1

    return (added_nodes, added_edges, filtered_out)


async def merged_graph_view_for_user(
    user_id: str,
    *,
    use_networkx: Optional[bool] = True,
):
    """Открыть read-only merged GraphBuilder: personal ∪ org (если user в org).

    Args:
        user_id: владелец personal-графа.
        use_networkx: True (по умолчанию) — собирает merged in-memory view.
            None/False — Neo4j-режим, возвращаем обычный GraphBuilder, без
            merge (там данные уже shared на уровне БД).

    Returns:
        GraphBuilder с `connected=True`, готовый к read-операциям.
        Save заблокирован: попытка `.save()` или `.close(save=True)` будет
        no-op с error-логом.
    """
    # Lazy imports — модуль импортируется на горячем chat-path, не хотим
    # тянуть тяжёлые depend'ы (numpy/networkx) если caller не дойдёт сюда.
    from backend.core.ingest.membership import get_org_for_user
    from backend.core.store.graph_builder import GraphBuilder
    from backend.core.store.tenant_paths import graph_path_for_user, org_graph_path

    # Tenant-scope для Neo4j-чтений: данные в shared Neo4j, RLS-фильтр
    # `tenant_id = $tenant` изолирует. У solo-юзера (без org) tenant_id ==
    # его user_id → проставляем scope в tenant_context, чтобы _get_all_nodes
    # фильтровал на ЕГО данные, а не на общий NULL-бакет (чужие аккаунты).
    # NB: эффективно ТОЛЬКО после миграции, которая проставит tenant_id на
    # существующих узлах (см. scripts/migrate_tenant_ownership.py).
    try:
        from backend.core.observability.tenant_context import (
            get_current_tenant, set_current_tenant,
        )
        if not get_current_tenant():  # не перетираем явный org-tenant из middleware
            scope = get_org_for_user(user_id) or user_id
            if scope:
                set_current_tenant(scope)
    except Exception as e:
        logger.debug("merged_view: could not bind tenant scope for %s: %s", user_id, e)

    personal_path = graph_path_for_user(user_id)
    gb = GraphBuilder(use_networkx=use_networkx, graph_storage_path=personal_path)
    await gb.connect()

    # Если backend не NetworkX (Neo4j) — merge не нужен (shared БД).
    # Возвращаем как есть, save НЕ блокируем (Neo4j-writes никуда не идут
    # через _save_graph_to_file).
    if not getattr(gb, "use_networkx", False):
        return gb

    # Federated merge для NetworkX-mode.
    org_id = get_org_for_user(user_id)
    if org_id:
        try:
            org_file = Path(org_graph_path(org_id))
            if org_file.exists():
                # P1 #5: RBAC-фильтр применяется внутри _merge.
                added_nodes, added_edges, filtered = _merge_org_into_personal(
                    gb, org_file, user_id=user_id,
                )
                logger.info(
                    "merged_view for user %s ∪ org %s: +%d nodes, +%d edges, %d filtered by RBAC",
                    user_id, org_id, added_nodes, added_edges, filtered,
                )
            else:
                logger.debug("merged_view: org graph file not found for org %s", org_id)
        except Exception as e:
            # Org-merge — не критично, personal-данные уже загружены.
            logger.warning("merged_view: org-merge failed for user %s: %s", user_id, e)

    # Read-only protection: на merged view запрещаем save.
    # bound method подменяем на функцию-блокер.
    gb._save_graph_to_file = _readonly_save_blocker  # type: ignore[assignment]

    return gb


# =============================================================================
# Writer-side helper
# =============================================================================


async def writer_graph_for_user(
    user_id: str,
    *,
    use_networkx: Optional[bool] = None,
):
    """Открыть WRITEABLE GraphBuilder для personal-graph пользователя.

    Используйте этот helper в коде, который **пишет** в граф —
    knowledge_sync write-path, mem0 entity extraction, custom
    correction-flow, etc. Это противоположность merged_graph_view_for_user:
      - НЕ загружает org-граф (изоляция: writer не должен случайно
        материализовать chained org-данные в personal-файл)
      - НЕ блокирует save (это writer, ему нужно сохранять)
      - Совместим с promote-flow Wave 1: данные попадают в personal,
        потом promote копирует в org (если user в org и source corp)

    Это явный «единственный правильный» writer-канал для personal-графа.
    Если вы добавляете новый flow и не уверены writer/reader — спрашивайте.

    Args:
        user_id: владелец personal-графа.
        use_networkx: None — auto-detect (Neo4j если NEO4J_URI задан).

    Returns:
        GraphBuilder с `connected=True`, готовый к read+write операциям.
        Caller обязан вызвать `await gb.save()` после writes (иначе
        дефолтный close(save=False) ничего не сохранит).
    """
    from backend.core.store.graph_builder import GraphBuilder
    from backend.core.store.tenant_paths import graph_path_for_user

    gb = GraphBuilder(
        use_networkx=use_networkx,
        graph_storage_path=graph_path_for_user(user_id),
    )
    await gb.connect()
    # save НЕ блокируем — caller сам решает когда персистить.
    return gb
