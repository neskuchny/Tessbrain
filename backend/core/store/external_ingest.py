# -*- coding: utf-8 -*-
"""External ingest — двунаправленная федерация Data Bus (P3).

До P3 Data Bus был ОДНОНАПРАВЛЕННЫМ: внешний консьюмер мог только
читать (query/snapshot) через API-key + SharingPolicy. Видение требует
передачи данных МЕЖДУ компаниями — значит партнёр должен уметь не только
читать, но и присылать данные в граф принимающей стороны, безопасно:

  payload(items) + consumer + policy
        │
        ├─ A. policy-gate: allow_ingest? тип узла разрешён? лимит?
        ├─ B. namespace-изоляция: всё пишется в tenant консьюмера
        ├─ C. provenance: каждый узел метится source=external:partner:<id>
        └─ D. write через инъектируемый graph_writer + audit
        ▼
  IngestResult{accepted, rejected, errors, provenance, namespace}

Дизайн (как P0–P2):
- чистая функция, ВСЁ инъектируемо (graph_writer, audit) → юнит-тест
  без графа/БД
- никогда не raises (контракт — degrade gracefully, частичный успех ок)
- bounded (max_items) — партнёр не зальёт нам весь свой граф
- provenance НЕ подделать: ставится сервером из consumer.id, поле
  во входных данных игнорируется
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# graph_writer(node_id, label, properties, access_group) -> bool
GraphWriterFn = Callable[..., Awaitable[bool]]
# audit(action, consumer_id, detail) -> None
AuditFn = Callable[..., Awaitable[None]]


# === Структуры ===========================================================

@dataclass
class IngestRejection:
    index: int
    reason: str
    item_type: str = ""


@dataclass
class IngestResult:
    """Итог ingest-операции. Частичный успех — норма."""
    accepted: int = 0
    rejected: int = 0
    node_ids: list[str] = field(default_factory=list)
    rejections: list[IngestRejection] = field(default_factory=list)
    provenance: str = ""
    namespace: str = ""
    status_code: int = 200

    @property
    def ok(self) -> bool:
        return self.status_code == 200

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "rejected": self.rejected,
            "node_ids": self.node_ids,
            "rejections": [
                {"index": r.index, "reason": r.reason, "type": r.item_type}
                for r in self.rejections
            ],
            "provenance": self.provenance,
            "namespace": self.namespace,
            "status_code": self.status_code,
        }


# === Хелперы =============================================================

def provenance_label(consumer_id: str) -> str:
    """Метка происхождения — единый формат `external:partner:<id>`."""
    return f"external:partner:{consumer_id or 'unknown'}"


def _norm_type(raw: Any) -> str:
    return str(raw or "").strip()


def _type_allowed(node_type: str, policy: Any) -> bool:
    """Тип узла разрешён к ЗАПИСИ той же policy, что и к чтению."""
    allowed = getattr(policy, "allowed_node_types", None) or []
    blocked = getattr(policy, "blocked_node_types", None) or []
    cap = node_type.capitalize()
    if node_type in blocked or cap in blocked:
        return False
    if "*" in allowed:
        return True
    return node_type in allowed or cap in allowed


def _clamp_access_level(props: dict[str, Any], policy: Any) -> int:
    """Внешние данные не могут быть «секретнее» потолка policy."""
    max_lvl = int(getattr(policy, "max_access_level", 1) or 1)
    try:
        requested = int(props.get("access_level", 1))
    except (TypeError, ValueError):
        requested = 1
    return max(1, min(requested, max_lvl))


# === Главная функция =====================================================

async def ingest_external_payload(
    *,
    consumer: Any,
    policy: Any,
    items: Any,
    graph_writer: GraphWriterFn,
    audit: Optional[AuditFn] = None,
    max_items: int = 100,
) -> IngestResult:
    """Принять данные от внешнего партнёра в граф. Никогда не raises.

    Args:
        consumer: DataBusConsumer (нужны .id, .tenant_id, .name).
        policy: SharingPolicy (нужны .allow_ingest, .allowed_node_types,
            .blocked_node_types, .max_access_level,
            .ingest_max_items_per_request).
        items: список dict'ов {type, name, description?, properties?, id?}.
        graph_writer: async (node_id, label, properties, access_group)->bool.
        audit: async (action, consumer_id, detail) -> None (best-effort).
        max_items: жёсткий потолок (защита принимающей стороны).

    Returns:
        IngestResult (частичный успех допустим; коды 403/400/200).
    """
    consumer_id = str(getattr(consumer, "id", "") or "")
    namespace = str(getattr(consumer, "tenant_id", "") or "")
    prov = provenance_label(consumer_id)
    result = IngestResult(provenance=prov, namespace=namespace)

    async def _safe_audit(action: str, detail: dict[str, Any]) -> None:
        if audit is None:
            return
        try:
            await audit(action=action, consumer_id=consumer_id, detail=detail)
        except Exception as exc:  # аудит не должен валить ingest
            logger.debug("external_ingest: audit failed: %s", exc)

    # A. Policy-gate: запись разрешена явно?
    if not bool(getattr(policy, "allow_ingest", False)):
        result.status_code = 403
        await _safe_audit("data_bus.ingest.denied",
                          {"reason": "ingest not allowed by policy"})
        return result

    if not namespace:
        result.status_code = 403
        await _safe_audit("data_bus.ingest.denied",
                          {"reason": "consumer has no tenant namespace"})
        return result

    if not isinstance(items, list) or not items:
        result.status_code = 400
        await _safe_audit("data_bus.ingest.denied",
                          {"reason": "empty or non-list payload"})
        return result

    # Bounded: min(жёсткий потолок, лимит политики)
    policy_limit = int(
        getattr(policy, "ingest_max_items_per_request", max_items) or max_items
    )
    hard_cap = max(1, min(max_items, policy_limit))
    batch = items[:hard_cap]

    now = datetime.now(timezone.utc).isoformat()

    for idx, raw in enumerate(batch):
        if not isinstance(raw, dict):
            result.rejected += 1
            result.rejections.append(IngestRejection(idx, "item is not an object"))
            continue

        node_type = _norm_type(raw.get("type"))
        if not node_type:
            result.rejected += 1
            result.rejections.append(IngestRejection(idx, "missing type"))
            continue

        if not _type_allowed(node_type, policy):
            result.rejected += 1
            result.rejections.append(
                IngestRejection(idx, f"type '{node_type}' not allowed by policy",
                                node_type))
            continue

        name = str(raw.get("name") or "").strip()
        if not name:
            result.rejected += 1
            result.rejections.append(
                IngestRejection(idx, "missing name", node_type))
            continue

        # B+C. Собираем свойства: namespace-изоляция + provenance.
        # Любые системные/обманные поля во входе перетираются сервером.
        in_props = raw.get("properties")
        props: dict[str, Any] = dict(in_props) if isinstance(in_props, dict) else {}
        props["name"] = name
        desc = raw.get("description")
        if isinstance(desc, str) and desc.strip():
            props["description"] = desc.strip()
        props["access_level"] = _clamp_access_level(props, policy)
        # namespace-изоляция: tenant_id выводится из user_id внутри
        # graph_builder.create_node → жёстко проставляем user_id консьюмера.
        props["user_id"] = namespace
        props["tenant_id"] = namespace
        # provenance — НЕ из входных данных, только сервер:
        props["source"] = prov
        props["external_consumer_id"] = consumer_id
        props["external_consumer_name"] = str(getattr(consumer, "name", "") or "")
        props["ingested_at"] = now
        props.setdefault("confidence", 0.5)  # внешним данным доверяем меньше

        node_id = str(raw.get("id") or "").strip() or (
            f"ext_{consumer_id[:8]}_{uuid.uuid4().hex[:12]}"
        )

        try:
            written = await graph_writer(
                node_id=node_id,
                label=node_type.capitalize(),
                properties=props,
                access_group="external",
            )
        except Exception as exc:
            logger.debug("external_ingest: writer failed idx=%s: %s", idx, exc)
            written = False

        if written:
            result.accepted += 1
            result.node_ids.append(node_id)
        else:
            result.rejected += 1
            result.rejections.append(
                IngestRejection(idx, "graph writer rejected node", node_type))

    if len(items) > hard_cap:
        result.rejections.append(
            IngestRejection(hard_cap,
                            f"payload truncated: {len(items)} > cap {hard_cap}"))

    await _safe_audit("data_bus.ingest", {
        "accepted": result.accepted,
        "rejected": result.rejected,
        "provenance": prov,
        "namespace": namespace,
        "truncated": len(items) > hard_cap,
    })
    return result


__all__ = [
    "IngestResult",
    "IngestRejection",
    "ingest_external_payload",
    "provenance_label",
]
