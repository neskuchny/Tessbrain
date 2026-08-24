"""GDPR-каскад с учётом org-graph и filesystem (P1 #9).

Расширение существующего `erasure.erase_user` (Postgres-каскад) на:
  - **Filesystem**: personal graph JSON / vector index / bm25 / temporal DB
  - **Membership** (Wave 2.2): запись в user_org_mapping.json
  - **Invites** (Wave 3.1): pending invites созданные/принятые user'ом
  - **Org-graphs tombstone**: узлы с user_id или promoted_from_user_id
    НЕ удаляются (бизнес-IP компании) — личные identifiers заменяются
    на детерминированный tombstone-hash, PII-поля стираются

Принципы GDPR vs business preservation:
  - **Compliance**: personal identifiers + behavioral data → удалены
  - **Legitimate interest**: decisions/tasks/meetings, сделанные за компанию,
    остаются как IP компании (документировано в Art. 6(1)(f) consideration)
  - **Audit-log** не трогаем (Art. 17(3)(b) legal-hold, как в erasure.py)
  - **Promoted-from-self link** обнуляется → автор больше не «owner» узла,
    что отражает фактическое расставание

Каждое действие best-effort: сбой filesystem cleanup не блокирует
membership cleanup и наоборот. Возвращаем по-action счётчики + errors.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# PII-поля, которые удаляются из props узла при tombstoning.
# (имя/email/телефон/identifier'ы личных каналов)
PII_FIELDS_TO_STRIP = frozenset({
    "email",
    "personal_email",
    "phone",
    "display_name",
    "full_name",
    "first_name",
    "last_name",
    "username",
    "telegram_username",
    "slack_user_id",
    "address",
    "ip_address",
})


def _tombstone_id(user_id: str) -> str:
    """Детерминированный tombstone-id — sha256(user_id), укороченный.

    Детерминированность нужна чтобы все ссылки на user'а в графе
    указывали на один и тот же tombstone (можно соединить «author of
    decisions A,B,C — same former employee»), но не reverse'ить
    оригинальный user_id.
    """
    return "tombstone_" + hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]


def _strip_pii(props: dict[str, Any]) -> dict[str, Any]:
    """Удалить PII-поля. Возвращает новый dict (caller mutates с update)."""
    return {k: v for k, v in props.items() if k not in PII_FIELDS_TO_STRIP}


# === Filesystem cleanup =====================================================


def _try_unlink(path: str, label: str, errors: dict[str, str]) -> bool:
    if not path or not os.path.exists(path):
        return False
    try:
        os.unlink(path)
        return True
    except Exception as e:
        errors[label] = str(e)[:200]
        return False


def erase_filesystem_user_data(user_id: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Удалить персональные файлы user'а с диска.

    Cleans:
      - personal graph (data/graphs/{user_id}.json)
      - vector index (data/vector_indexes/{user_id}.json)
      - bm25 (data/bm25_indexes/{user_id}.pkl)
      - temporal DB (data/temporal/{user_id}.db)
    """
    from backend.core.store.tenant_paths import (
        bm25_index_path_for_user,
        graph_path_for_user,
        temporal_db_path_for_user,
        vector_index_path_for_user,
    )

    result: dict[str, Any] = {"deleted": {}, "errors": {}, "skipped": dry_run}
    if not user_id:
        result["errors"]["_"] = "user_id required"
        return result

    targets = {
        "graph": graph_path_for_user(user_id),
        "vector_index": vector_index_path_for_user(user_id),
        "bm25": bm25_index_path_for_user(user_id),
        "temporal_db": temporal_db_path_for_user(user_id),
    }

    for label, path in targets.items():
        if dry_run:
            result["deleted"][label] = os.path.exists(path)
        else:
            result["deleted"][label] = _try_unlink(path, label, result["errors"])

    return result


# === Membership + invites cleanup ===========================================


def erase_org_membership(user_id: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Удалить user из user_org_mapping (Wave 2.2)."""
    from backend.core.ingest import membership

    result: dict[str, Any] = {"removed": False, "org_id": None}
    if not user_id:
        return result

    org_id = membership.get_org_for_user(user_id)
    result["org_id"] = org_id
    if not org_id:
        return result  # not a member

    if not dry_run:
        try:
            removed = membership.remove_member(user_id, org_id)
            result["removed"] = bool(removed)
        except Exception as e:
            result["error"] = str(e)[:200]
    else:
        result["removed"] = True  # would remove

    return result


def revoke_user_invites(user_id: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Revoke все pending invites созданные user'ом.

    Used invites (consumed) НЕ трогаем — это исторический артефакт
    (Алиса приняла invite Боба → членство Алисы в org).
    """
    from backend.core.ingest import invite_store

    result: dict[str, Any] = {"revoked": 0, "skipped_used": 0, "errors": []}
    if not user_id:
        return result

    try:
        all_invites = invite_store._get_all()
    except Exception as e:
        result["errors"].append(f"list_failed: {str(e)[:120]}")
        return result

    for invite_id, entry in list(all_invites.items()):
        if not isinstance(entry, dict):
            continue
        if entry.get("invited_by") != user_id:
            continue
        if entry.get("used_at"):
            result["skipped_used"] += 1
            continue
        if entry.get("revoked_at"):
            continue  # уже отозван
        if dry_run:
            result["revoked"] += 1
            continue
        try:
            org_id = entry.get("org_id")
            if org_id and invite_store.revoke_invite(invite_id, org_id, revoked_by="gdpr_cascade"):
                result["revoked"] += 1
        except Exception as e:
            result["errors"].append(f"{invite_id}: {str(e)[:120]}")

    return result


# === Org-graph tombstone ====================================================


def _tombstone_neo4j(
    user_id: str,
    tombstone: str,
    dry_run: bool,
    result: dict[str, Any],
) -> bool:
    """Атомарный Cypher-tombstone на всех узлах Neo4j. Возвращает True если
    операция была выполнена (success или explicit error), False — если Neo4j
    SDK недоступен / нет URI → caller должен сделать NetworkX-fallback.
    """
    try:
        from neo4j import GraphDatabase  # sync driver — мы в sync-функции
    except ImportError:
        return False  # SDK не установлен — fallback на NetworkX

    uri = os.environ.get("NEO4J_URI")
    if not uri:
        return False

    user = os.environ.get("NEO4J_USER", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD", "neo4j_secret")

    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()

    # Список PII-полей для REMOVE clause. Cypher не поддерживает REMOVE из
    # параметра, поэтому генерируем литеральный список (safe — наш whitelist).
    pii_remove = ", ".join(f"n.{f}" for f in PII_FIELDS_TO_STRIP)

    # Сначала COUNT (всегда), затем UPDATE (если не dry_run).
    count_cypher = """
    MATCH (n)
    WHERE n.user_id = $uid
       OR n.promoted_from_user_id = $uid
       OR n.created_by = $uid
    RETURN count(n) as touched
    """
    update_cypher = f"""
    MATCH (n)
    WHERE n.user_id = $uid
       OR n.promoted_from_user_id = $uid
       OR n.created_by = $uid
    SET n.tombstoned_at = $now,
        n.tombstoned_reason = 'gdpr_erasure'
    SET n.user_id = CASE WHEN n.user_id = $uid THEN $tombstone ELSE n.user_id END
    SET n.promoted_from_user_id = CASE WHEN n.promoted_from_user_id = $uid
                                       THEN $tombstone ELSE n.promoted_from_user_id END
    SET n.created_by = CASE WHEN n.created_by = $uid
                            THEN $tombstone ELSE n.created_by END
    REMOVE {pii_remove}
    RETURN count(n) as touched
    """

    try:
        # Используем sync driver чтобы вписаться в sync-функцию.
        # Для async-flow API caller'у нужно вызывать tombstone через
        # asyncio.to_thread (или мы выделим отдельный async-wrapper в P2 #10
        # full migration).
        driver = GraphDatabase.driver(uri, auth=(user, password))
        try:
            with driver.session() as session:
                if dry_run:
                    r = session.run(count_cypher, {"uid": user_id})
                    record = r.single()
                    result["nodes_tombstoned"] = int(record["touched"]) if record else 0
                else:
                    r = session.run(update_cypher, {
                        "uid": user_id,
                        "tombstone": tombstone,
                        "now": now_iso,
                    })
                    record = r.single()
                    result["nodes_tombstoned"] = int(record["touched"]) if record else 0
            result["backend"] = "neo4j"
            result["orgs_scanned"] = 1  # один shared instance = «один org-scope»
            return True
        finally:
            driver.close()
    except Exception as e:
        logger.warning("Neo4j tombstone failed: %s", e)
        result["errors"].append(f"neo4j: {str(e)[:200]}")
        result["backend"] = "neo4j_error"
        # True — мы попробовали, не падаем обратно в NetworkX. Иначе при
        # network issue к Neo4j мы бы тихо начали писать в JSON-файлы,
        # что в Neo4j-deployment просто отсутствуют.
        return True


def tombstone_in_org_graphs(user_id: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Обойти все org-graphs, заменить user_id → tombstone, удалить PII.

    Узлы НЕ удаляются — это бизнес-IP компании. Эффект:
      - props.user_id = tombstone_<hash>
      - props.promoted_from_user_id (если был = user_id) = tombstone_<hash>
      - props без PII полей (email, phone, и т.п.)
      - props.tombstoned_at + tombstoned_reason = "gdpr_erasure"

    Backend-aware:
      - **NetworkX**: обходим JSON-файлы в data/orgs/*/graph.json
      - **Neo4j**: один Cypher `MATCH … SET … REMOVE` на всех узлах БД
        (atomic transaction, scale-ready для 100+ users)

    Returns:
        {"orgs_scanned": N, "nodes_tombstoned": N, "backend": "...",
         "errors": [...]}
    """
    result: dict[str, Any] = {
        "orgs_scanned": 0,
        "nodes_tombstoned": 0,
        "errors": [],
        "dry_run": dry_run,
        "backend": "unknown",
    }
    if not user_id:
        return result

    tombstone = _tombstone_id(user_id)

    # --- Neo4j path: atomic Cypher на всех узлах -----------------------------
    # Sync-функция, поэтому проверяем env-флаг напрямую (не делаем async probe).
    use_networkx_env = os.environ.get("USE_NETWORKX_GRAPH", "").lower()
    force_networkx = use_networkx_env in ("true", "1", "yes")

    if not force_networkx:
        # Попробуем Neo4j, если SDK доступен и подключение есть.
        neo4j_done = _tombstone_neo4j(user_id, tombstone, dry_run, result)
        if neo4j_done:
            return result
        # Иначе fall through на NetworkX (либо Neo4j unavailable, либо
        # SDK не установлен — это нормально для dev/test).

    # --- NetworkX path: обход JSON-файлов ------------------------------------
    result["backend"] = "networkx"
    from backend.core.store.tenant_paths import orgs_dir

    orgs_root = orgs_dir()
    if not orgs_root.exists():
        return result

    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()

    for org_dir in orgs_root.iterdir():
        if not org_dir.is_dir():
            continue
        graph_file = org_dir / "graph.json"
        if not graph_file.exists():
            continue
        result["orgs_scanned"] += 1

        try:
            with open(graph_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            result["errors"].append(f"{org_dir.name}: read failed: {str(e)[:120]}")
            continue

        changed = 0
        for node in data.get("nodes", []):
            if not isinstance(node, dict):
                continue
            touched = False
            if node.get("user_id") == user_id:
                node["user_id"] = tombstone
                touched = True
            if node.get("promoted_from_user_id") == user_id:
                node["promoted_from_user_id"] = tombstone
                touched = True
            if node.get("created_by") == user_id:
                node["created_by"] = tombstone
                touched = True
            if touched:
                # Удаляем PII-поля.
                for k in list(node.keys()):
                    if k in PII_FIELDS_TO_STRIP:
                        del node[k]
                node["tombstoned_at"] = now_iso
                node["tombstoned_reason"] = "gdpr_erasure"
                changed += 1

        result["nodes_tombstoned"] += changed
        if changed and not dry_run:
            try:
                # Atomic write (как в membership/org_store/invite_store).
                import tempfile
                parent = str(graph_file.parent)
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=parent,
                    prefix=".tmp_gdpr_", suffix=".json", delete=False,
                ) as tmp:
                    tmp_name = tmp.name
                    json.dump(data, tmp, ensure_ascii=False, indent=2, sort_keys=True)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                os.replace(tmp_name, str(graph_file))
            except Exception as e:
                result["errors"].append(f"{org_dir.name}: write failed: {str(e)[:120]}")

    return result


# === Orchestrator ==========================================================


async def erase_user_full(user_id: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Полный GDPR-каскад: Postgres + filesystem + membership + invites + tombstone.

    Returns:
        {
          "user_id", "dry_run",
          "postgres": {...},          # erasure.erase_user output
          "filesystem": {...},        # erase_filesystem_user_data output
          "membership": {...},        # erase_org_membership output
          "invites": {...},           # revoke_user_invites output
          "org_graphs": {...},        # tombstone_in_org_graphs output
        }
    """
    result: dict[str, Any] = {"user_id": user_id, "dry_run": dry_run}

    # 1. Postgres-каскад (existing erasure.py).
    try:
        from backend.core.gdpr.erasure import erase_user
        result["postgres"] = await erase_user(user_id, dry_run=dry_run)
    except Exception as e:
        logger.warning("postgres cascade failed: %s", e)
        result["postgres"] = {"error": str(e)[:200]}

    # 2. Filesystem cleanup. Должен идти ПОСЛЕ membership query (иначе
    # erase_org_membership уже не сможет найти org_id, но это не критично:
    # мы используем filesystem cleanup ради дисковой гигиены, не для org).
    result["membership"] = erase_org_membership(user_id, dry_run=dry_run)

    # 3. Invites (pending — отзываем).
    result["invites"] = revoke_user_invites(user_id, dry_run=dry_run)

    # 4. Tombstone в org-graphs (узлы остаются, identifiers удалены).
    result["org_graphs"] = tombstone_in_org_graphs(user_id, dry_run=dry_run)

    # 5. Filesystem — last (после membership query).
    result["filesystem"] = erase_filesystem_user_data(user_id, dry_run=dry_run)

    return result
