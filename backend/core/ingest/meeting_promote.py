"""Promote one episode из personal-graph в org-graph.

Идея: пользователь обработал meeting через `_process_single_meeting`,
данные лежат в `data/graphs/{user_id}.json`. Если этот user привязан к org
(`user_org_mapping.json`) И источник встречи — corp-канал (не Mini Tess /
не личный voice memo), то слепок «Meeting + всё что из него извлекли»
копируется в `data/orgs/{org_id}/graph.json`.

Что копируется:
  * сам узел `meeting_{meeting_id}`;
  * все узлы где `source_meeting_id == meeting_id` (Decision, Task, Risk,
    Opinion, Participant — то что extractors положили в personal-граф);
  * все рёбра между копируемыми узлами.

Что НЕ копируется:
  * cross-meeting связи в personal графе (личный wisdom-engine);
  * Persona-секции (это отдельный pipe `/persona/me/*`).

Идемпотентность: используем те же node_id, что и в personal — повторный
promote безопасен, GraphBuilder.create_node делает MERGE-by-id в Neo4j
mode и add_node-update в NetworkX. Метаданные `promoted_from` и
`promoted_at` позволяют отличить «копию из personal» от нативных
org-узлов (если такие появятся).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from backend.core.ingest.membership import get_org_for_user

logger = logging.getLogger(__name__)


def _graph_path_for_user(user_id: str) -> str:
    # Lazy: tenant_paths подменяется в части тестов (см. test_nightly_curator_step).
    from backend.core.store.tenant_paths import graph_path_for_user
    return graph_path_for_user(user_id)


def _org_graph_path(org_id: str) -> str:
    from backend.core.store.tenant_paths import org_graph_path
    return org_graph_path(org_id)

# Источники, которые НЕ promote'им автоматически — даже если user в org.
# Mini Tess и личные voice-memos — приватный канал; founder сам решает
# когда (и через какой share-bundle) расшарить их с командой.
PERSONAL_SOURCES = frozenset({
    "mini_tess",
    "mini_tess_input",
    "telegram_voice_personal",
    "manual_personal",
})


def _promoter_department(user_id: str) -> str:
    """Отдел автора для штампа на узлы эпизода (P2 мульти-аккаунта)."""
    try:
        from backend.core.ingest.membership import get_user_org_department
        return get_user_org_department(user_id) or ""
    except Exception:
        logger.debug("promote: department lookup failed", exc_info=True)
        return ""


def should_promote(user_id: str, source: Optional[str]) -> Optional[str]:
    """Решить — promote эту встречу в org-graph или нет.

    Returns:
        org_id (str): да, promote сюда.
        None:          нет (solo-user, personal source, или нет mapping).
    """
    if not user_id:
        return None
    if source and source.lower() in PERSONAL_SOURCES:
        return None
    org_id = get_org_for_user(user_id)
    return org_id  # None если solo, иначе строка


async def promote_meeting_to_org(
    *,
    user_id: str,
    meeting_id: str,
    org_id: str,
    source: Optional[str],
) -> dict[str, Any]:
    """Сделать узлы meeting'а видимыми в org-graph.

    Backend-зависимая семантика:
      - **NetworkX**: physical copy узлов из personal-файла в org-файл.
        Файлы независимые; copy неизбежен.
      - **Neo4j**: shared instance — все узлы в одной БД. «Promote» =
        in-place tag update: ставим org_id/tenant_id/promoted_* на узлы
        эпизода, чтобы они показались через org-фильтр в read'ах.
        Это **значительно быстрее** (одна транзакция вместо чтения двух
        файлов) и устраняет file-lock contention для 100+ users.

    Returns:
        {
          "promoted": bool,
          "nodes_copied": int,
          "edges_copied": int,
          "org_id": str,
          "backend": "networkx" | "neo4j",
          "reason": str | None,
        }
    """
    from backend.core.store.graph_builder import GraphBuilder

    result: dict[str, Any] = {
        "promoted": False,
        "nodes_copied": 0,
        "edges_copied": 0,
        "org_id": org_id,
        "backend": "unknown",
        "reason": None,
    }

    # Сначала определяем backend через короткое подключение к personal-graph
    # (auto-detect: use_networkx=None решает Neo4j vs NetworkX из env).
    probe = GraphBuilder(
        use_networkx=None,
        graph_storage_path=_graph_path_for_user(user_id),
    )
    try:
        await probe.connect()
    except Exception as e:
        result["reason"] = f"probe_connect_failed: {str(e)[:200]}"
        return result

    is_neo4j = not getattr(probe, "use_networkx", True)
    result["backend"] = "neo4j" if is_neo4j else "networkx"

    # Neo4j-mode: in-place tag update через Cypher.
    # P2 мульти-аккаунта: отдел автора встречи — на узлы эпизода. Уже
    # заданный department узла НЕ перетираем (coalesce/setdefault): явная
    # разметка сильнее автоматической.
    promoter_department = _promoter_department(user_id)

    if is_neo4j:
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            meeting_node_id = f"meeting_{meeting_id}"
            async with probe.driver.session() as session:
                # MATCH узлы эпизода: сам Meeting (по id) + всё с
                # source_meeting_id == meeting_id (Decision/Task/etc).
                cypher = """
                MATCH (n)
                WHERE n.id = $meeting_node_id
                   OR n.source_meeting_id = $meeting_id
                SET n.org_id = $org_id,
                    n.tenant_id = $org_id,
                    n.promoted_from_user_id = $user_id,
                    n.promoted_at = $now,
                    n.promoted_source = $source,
                    n.department = coalesce(n.department,
                        CASE WHEN $department = '' THEN null ELSE $department END)
                RETURN count(n) as touched
                """
                r = await session.run(cypher, {
                    "meeting_node_id": meeting_node_id,
                    "meeting_id": meeting_id,
                    "org_id": org_id,
                    "user_id": user_id,
                    "now": now_iso,
                    "source": source or "",
                    "department": promoter_department,
                })
                rec = await r.single()
                touched = int(rec["touched"]) if rec else 0
                result["nodes_copied"] = touched
                # Edges в Neo4j shared — отдельно не копируются (они уже
                # связывают те же узлы, что мы только что промоутили).
                result["edges_copied"] = 0
                result["promoted"] = touched > 0
                if touched == 0:
                    result["reason"] = "meeting_node_not_found"
            logger.info(
                "Promoted meeting %s to org %s (Neo4j): %d nodes tagged",
                meeting_id, org_id, result["nodes_copied"],
            )
            return result
        except Exception as e:
            logger.warning("Neo4j promote failed for %s: %s", meeting_id, e)
            result["reason"] = f"neo4j_error: {str(e)[:200]}"
            return result
        finally:
            try:
                await probe.close(save=False)
            except Exception:
                logger.debug("probe.close suppressed", exc_info=True)

    # NetworkX-mode: physical copy (existing path).
    # probe уже подключён как personal-graph — используем его как src.
    src = probe
    dst = GraphBuilder(
        use_networkx=True,
        graph_storage_path=_org_graph_path(org_id),
    )
    try:
        await dst.connect()

        meeting_node_id = f"meeting_{meeting_id}"
        src_g = src.nx_graph

        if not src_g.has_node(meeting_node_id):
            result["reason"] = "meeting_node_not_found_in_personal_graph"
            return result

        # 1. Собираем все узлы этого эпизода.
        episode_node_ids: set[str] = {meeting_node_id}
        for nid, attrs in src_g.nodes(data=True):
            if attrs.get("source_meeting_id") == meeting_id:
                episode_node_ids.add(nid)

        # 2. Копируем узлы. Metadata `promoted_*` помечает «копия».
        now_iso = datetime.now(timezone.utc).isoformat()
        skipped_private: set = set()
        for nid in episode_node_ids:
            attrs = dict(src_g.nodes[nid])  # копия dict'а — не мутируем personal
            label = attrs.pop("_label", "Unknown")

            # Перебиваем tenant-поля на org-владельца. user_id оставляем как
            # `originator` — provenance: «исходник был у такого-то».
            attrs["promoted_from_user_id"] = user_id
            attrs["promoted_at"] = now_iso
            attrs["org_id"] = org_id
            attrs["tenant_id"] = org_id
            if source:
                attrs["source"] = source
            # отдел автора (явная разметка узла сильнее автоматической)
            if promoter_department:
                attrs.setdefault("department", promoter_department)

            # private-узлы эпизода в общий org-граф не копируем: private
            # явно значит «не для организации» (chat-memory и пр.)
            if str(attrs.get("access_group") or "").lower() == "private":
                skipped_private.add(nid)
                continue

            ok = await dst.create_node(
                node_id=nid,
                label=label,
                properties=attrs,
                access_group=attrs.get("access_group", "org"),
            )
            if ok:
                result["nodes_copied"] += 1

        # 3. Копируем рёбра только между скопированными узлами (intra-episode).
        for u, v, edge_attrs in src_g.edges(data=True):
            if u not in episode_node_ids or v not in episode_node_ids:
                continue
            if u in skipped_private or v in skipped_private:
                continue  # рёбра к непромоченным private-узлам не тянем
            rel_type = edge_attrs.get("_type") or edge_attrs.get("type") or "RELATED_TO"
            props = {
                k: val for k, val in edge_attrs.items()
                if k not in ("_type", "type") and val is not None
            }
            ok = await dst.create_relationship(
                from_id=u, to_id=v, rel_type=rel_type, properties=props,
            )
            if ok:
                result["edges_copied"] += 1

        # Сохраняем dst на диск — GraphBuilder.close(save=False) дефолтно
        # НЕ персистит, чтобы случайно не перетереть конкурента; здесь же мы
        # явно владеем org-копией этого эпизода и должны сделать durable.
        await dst.save()

        result["promoted"] = True
        logger.info(
            "Promoted meeting %s from user %s to org %s: %d nodes, %d edges",
            meeting_id, user_id, org_id,
            result["nodes_copied"], result["edges_copied"],
        )
        return result

    except Exception as e:
        logger.warning("promote_meeting_to_org failed for %s: %s", meeting_id, e)
        result["reason"] = f"error: {str(e)[:200]}"
        return result
    finally:
        # src закрываем без сохранения — мы её только читали.
        try:
            await src.close(save=False)
        except Exception:
            logger.debug("src.close() suppressed", exc_info=True)
        try:
            await dst.close(save=False)  # уже сохранили через .save() выше
        except Exception:
            logger.debug("dst.close() suppressed", exc_info=True)


async def promote_document_to_org(
    *,
    user_id: str,
    document_id: str,
    org_id: str,
    source: Optional[str] = None,
) -> dict[str, Any]:
    """Promote ДОКУМЕНТА в org-graph (P2 мульти-аккаунта: «документы мы
    добавляем и синхронизируем уже сейчас» — теперь они, как и встречи,
    сливаются в общий мозг с уровнями/отделом).

    Эпизод документа: узел `document_{document_id}` (DocumentIndexer) +
    все узлы с attrs.document_id == document_id (чанки) + рёбра между ними.
    Семантика идентична promote_meeting_to_org: NetworkX — physical copy,
    Neo4j — in-place tag update; штампуем org_id/tenant_id/promoted_* и
    отдел автора (setdefault/coalesce — явная разметка сильнее).
    """
    from backend.core.store.graph_builder import GraphBuilder

    result: dict[str, Any] = {
        "promoted": False,
        "nodes_copied": 0,
        "edges_copied": 0,
        "org_id": org_id,
        "backend": "unknown",
        "reason": None,
    }

    probe = GraphBuilder(
        use_networkx=None,
        graph_storage_path=_graph_path_for_user(user_id),
    )
    try:
        await probe.connect()
    except Exception as e:
        result["reason"] = f"probe_connect_failed: {str(e)[:200]}"
        return result

    is_neo4j = not getattr(probe, "use_networkx", True)
    result["backend"] = "neo4j" if is_neo4j else "networkx"
    promoter_department = _promoter_department(user_id)
    doc_node_id = f"document_{document_id}"
    now_iso = datetime.now(timezone.utc).isoformat()

    if is_neo4j:
        try:
            async with probe.driver.session() as session:
                cypher = """
                MATCH (n)
                WHERE n.id = $doc_node_id
                   OR n.document_id = $document_id
                SET n.org_id = $org_id,
                    n.tenant_id = $org_id,
                    n.promoted_from_user_id = $user_id,
                    n.promoted_at = $now,
                    n.promoted_source = $source,
                    n.department = coalesce(n.department,
                        CASE WHEN $department = '' THEN null ELSE $department END)
                RETURN count(n) as touched
                """
                r = await session.run(cypher, {
                    "doc_node_id": doc_node_id,
                    "document_id": document_id,
                    "org_id": org_id,
                    "user_id": user_id,
                    "now": now_iso,
                    "source": source or "",
                    "department": promoter_department,
                })
                rec = await r.single()
                touched = int(rec["touched"]) if rec else 0
                result["nodes_copied"] = touched
                result["promoted"] = touched > 0
                if touched == 0:
                    result["reason"] = "document_node_not_found"
            logger.info(
                "Promoted document %s to org %s (Neo4j): %d nodes tagged",
                document_id, org_id, result["nodes_copied"],
            )
            return result
        except Exception as e:
            logger.warning("Neo4j document promote failed for %s: %s",
                           document_id, e)
            result["reason"] = f"neo4j_error: {str(e)[:200]}"
            return result
        finally:
            try:
                await probe.close(save=False)
            except Exception:
                logger.debug("probe.close suppressed", exc_info=True)

    # NetworkX-mode: physical copy.
    src = probe
    dst = GraphBuilder(
        use_networkx=True,
        graph_storage_path=_org_graph_path(org_id),
    )
    try:
        await dst.connect()
        src_g = src.nx_graph

        if not src_g.has_node(doc_node_id):
            result["reason"] = "document_node_not_found_in_personal_graph"
            return result

        episode_node_ids: set[str] = {doc_node_id}
        for nid, attrs in src_g.nodes(data=True):
            if attrs.get("document_id") == document_id:
                episode_node_ids.add(nid)

        skipped_private: set = set()
        for nid in episode_node_ids:
            attrs = dict(src_g.nodes[nid])
            label = attrs.pop("_label", "Unknown")
            attrs["promoted_from_user_id"] = user_id
            attrs["promoted_at"] = now_iso
            attrs["org_id"] = org_id
            attrs["tenant_id"] = org_id
            if source:
                attrs["source"] = source
            if promoter_department:
                attrs.setdefault("department", promoter_department)
            if str(attrs.get("access_group") or "").lower() == "private":
                skipped_private.add(nid)
                continue
            ok = await dst.create_node(
                node_id=nid, label=label, properties=attrs,
                access_group=attrs.get("access_group", "org"),
            )
            if ok:
                result["nodes_copied"] += 1

        for u, v, edge_attrs in src_g.edges(data=True):
            if u not in episode_node_ids or v not in episode_node_ids:
                continue
            if u in skipped_private or v in skipped_private:
                continue  # рёбра к непромоченным private-узлам не тянем
            rel_type = edge_attrs.get("_type") or edge_attrs.get("type") or "RELATED_TO"
            props = {k: val for k, val in edge_attrs.items()
                     if k not in ("_type", "type") and val is not None}
            ok = await dst.create_relationship(
                from_id=u, to_id=v, rel_type=rel_type, properties=props,
            )
            if ok:
                result["edges_copied"] += 1

        await dst.save()
        result["promoted"] = True
        logger.info(
            "Promoted document %s from user %s to org %s: %d nodes, %d edges",
            document_id, user_id, org_id,
            result["nodes_copied"], result["edges_copied"],
        )
        return result
    except Exception as e:
        logger.warning("promote_document_to_org failed for %s: %s",
                       document_id, e)
        result["reason"] = f"error: {str(e)[:200]}"
        return result
    finally:
        try:
            await src.close(save=False)
        except Exception:
            logger.debug("src.close() suppressed", exc_info=True)
        try:
            await dst.close(save=False)
        except Exception:
            logger.debug("dst.close() suppressed", exc_info=True)
