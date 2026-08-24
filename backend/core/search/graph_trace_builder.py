import itertools
import logging
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _pick_first(*values: Any) -> str:
    for value in values:
        text = _stringify(value)
        if text:
            return text
    return ""


def _dedupe_strings(values: List[str]) -> List[str]:
    seen = set()
    ordered: List[str] = []
    for value in values:
        key = value.lower()
        if not value or key in seen:
            continue
        seen.add(key)
        ordered.append(value)
    return ordered


def _normalize_stage_name(value: Any) -> str:
    text = _stringify(value).lower().replace(" ", "_")
    if not text:
        return ""

    aliases = {
        "semantic": "vector",
        "embedding": "vector",
        "embeddings": "vector",
        "vector_search": "vector",
        "lexical": "bm25",
        "keyword": "bm25",
        "keyword_search": "bm25",
        "graph_search": "graph",
        "graph_traversal": "graph",
        "neo4j": "graph",
        "networkx": "graph",
        "document_chunk": "document",
        "raw_chunk": "document",
        "chunk": "document",
        "doc": "document",
        "docs": "document",
        "snapshot_memory": "snapshot",
        "entity_snapshot": "snapshot",
        "company_snapshot": "snapshot",
    }
    return aliases.get(text, text)


def _append_stage_values(target: List[str], raw_value: Any) -> None:
    if isinstance(raw_value, (list, tuple, set)):
        for item in raw_value:
            normalized = _normalize_stage_name(item)
            if normalized:
                target.append(normalized)
        return

    raw_text = _stringify(raw_value)
    if not raw_text:
        return

    splitter = "," if "," in raw_text else "|"
    parts = [part.strip() for part in raw_text.split(splitter)] if splitter in raw_text else [raw_text]
    for part in parts:
        normalized = _normalize_stage_name(part)
        if normalized:
            target.append(normalized)


def _collect_retrieval_sources(source: Dict[str, Any], metadata: Dict[str, Any]) -> List[str]:
    collected: List[str] = []

    for raw_value in (
        source.get("retrieval_sources"),
        metadata.get("retrieval_sources"),
        source.get("sources"),
        metadata.get("sources"),
    ):
        _append_stage_values(collected, raw_value)

    for stage_name, score_key in (("graph", "graph_score"), ("vector", "vector_score"), ("bm25", "bm25_score")):
        score = source.get(score_key, metadata.get(score_key))
        if score is None:
            continue
        try:
            if float(score) > 0:
                collected.append(stage_name)
        except (TypeError, ValueError):
            collected.append(stage_name)

    return _dedupe_strings(collected)


def _classify_retrieval_stage(
    explicit_stage: str,
    normalized_type: str,
    snapshot_type: str,
    document_id: str,
    retrieval_sources: List[str],
) -> str:
    explicit = _normalize_stage_name(explicit_stage)
    if explicit:
        return explicit

    if snapshot_type or normalized_type == "snapshot":
        return "snapshot"

    document_like_types = {
        "document",
        "document_chunk",
        "chunk",
        "raw_chunk",
        "note",
        "transcript",
        "file",
    }
    if document_id or normalized_type in document_like_types:
        return "document"

    for preferred_stage in ("graph", "vector", "bm25"):
        if preferred_stage in retrieval_sources:
            return preferred_stage

    graph_like_types = {
        "proposition",
        "decision",
        "task",
        "kpi",
        "entity",
        "meeting",
        "topic",
        "project",
        "person",
        "product",
        "knowledge",
        "regulation",
        "template",
        "risk",
    }
    if normalized_type in graph_like_types:
        return "graph"

    return "unknown"


def _normalize_source(source: Dict[str, Any]) -> Dict[str, Any]:
    metadata = source.get("metadata", {}) or {}
    normalized_type = _pick_first(
        source.get("_normalized_type"),
        source.get("normalized_type"),
        source.get("type"),
        metadata.get("type"),
        metadata.get("doc_type"),
    ).lower()

    source_id = _pick_first(source.get("id"), metadata.get("id"))
    entity_id = _pick_first(source.get("entity_id"), metadata.get("entity_id"))
    meeting_id = _pick_first(
        source.get("meeting_id"),
        metadata.get("meeting_id"),
        metadata.get("source_meeting_id"),
    )
    document_id = _pick_first(
        source.get("document_id"),
        metadata.get("document_id"),
    )
    title = _pick_first(
        source.get("title"),
        source.get("name"),
        source.get("meeting_title"),
        metadata.get("title"),
        metadata.get("name"),
        metadata.get("document_title"),
        metadata.get("meeting_title"),
    )

    score = source.get("adjusted_score", source.get("score"))
    role = _pick_first(source.get("evidence_role"), metadata.get("evidence_role")) or "supporting"
    retrieval_sources = _collect_retrieval_sources(source, metadata)
    snippet = _pick_first(
        source.get("snippet"),
        source.get("text"),
        source.get("summary"),
        metadata.get("snippet"),
        metadata.get("summary"),
        metadata.get("description"),
    )[:240]

    graph_first_types = {
        "snapshot",
        "proposition",
        "decision",
        "task",
        "kpi",
        "entity",
        "meeting",
        "topic",
        "report",
        "project",
        "person",
        "product",
        "knowledge",
        "regulation",
        "template",
        "risk",
    }

    candidate_ids: List[str]
    if normalized_type in graph_first_types:
        candidate_ids = [entity_id, source_id, meeting_id, document_id]
    else:
        candidate_ids = [document_id, meeting_id, entity_id, source_id]

    return {
        "source_id": source_id,
        "entity_id": entity_id or source_id,
        "meeting_id": meeting_id,
        "document_id": document_id,
        "title": title,
        "source_type": normalized_type or _pick_first(source.get("type"), "unknown").lower(),
        "role": role,
        "score": score,
        "snippet": snippet,
        "snapshot_type": _pick_first(source.get("snapshot_type"), metadata.get("snapshot_type")),
        "retrieval_sources": retrieval_sources,
        "retrieval_stage": _classify_retrieval_stage(
            explicit_stage=_pick_first(source.get("retrieval_stage"), metadata.get("retrieval_stage")),
            normalized_type=normalized_type,
            snapshot_type=_pick_first(source.get("snapshot_type"), metadata.get("snapshot_type")),
            document_id=document_id,
            retrieval_sources=retrieval_sources,
        ),
        "candidate_ids": _dedupe_strings([item for item in candidate_ids if item]),
    }


class _GraphTraceResolver:
    def __init__(self, graph_builder: Any):
        self.graph_builder = graph_builder
        self._node_cache: Dict[str, Optional[Dict[str, Any]]] = {}
        self._neighbor_cache: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}

    async def get_node(self, node_key: str) -> Optional[Dict[str, Any]]:
        node_key = _stringify(node_key)
        if not node_key:
            return None
        if node_key not in self._node_cache:
            try:
                self._node_cache[node_key] = await self.graph_builder.get_node_by_id(node_key)
            except Exception as exc:
                logger.debug("Graph trace get_node failed for %s: %s", node_key, exc)
                self._node_cache[node_key] = None
        return self._node_cache[node_key]

    async def resolve_source(self, source: Dict[str, Any], source_order: int) -> Optional[Dict[str, Any]]:
        normalized = _normalize_source(source)
        node: Optional[Dict[str, Any]] = None
        resolved_key = ""

        for candidate_id in normalized["candidate_ids"]:
            node = await self.get_node(candidate_id)
            if node:
                resolved_key = _pick_first(node.get("id"), candidate_id)
                break

        if not node or not resolved_key:
            return None

        return self._build_node_payload(
            node=node,
            node_key=resolved_key,
            role=normalized["role"],
            source_type=normalized["source_type"],
            source_order=source_order,
            source_ref=normalized,
            is_anchor=False,
        )

    async def build_anchor_node(self, node_key: str) -> Optional[Dict[str, Any]]:
        node = await self.get_node(node_key)
        if not node:
            return None
        return self._build_node_payload(
            node=node,
            node_key=_pick_first(node.get("id"), node_key),
            role="anchor",
            source_type=_pick_first(node.get("_label"), "anchor").lower(),
            source_order=-1,
            source_ref={
                "retrieval_stage": "graph",
                "retrieval_sources": ["graph"],
            },
            is_anchor=True,
        )

    def _build_node_payload(
        self,
        node: Dict[str, Any],
        node_key: str,
        role: str,
        source_type: str,
        source_order: int,
        source_ref: Dict[str, Any],
        is_anchor: bool,
    ) -> Dict[str, Any]:
        label = _pick_first(
            node.get("name"),
            node.get("title"),
            source_ref.get("title"),
            source_ref.get("meeting_id"),
            node_key,
        )
        return {
            "node_key": node_key,
            "entity_id": _pick_first(node.get("entity_id"), node.get("id"), source_ref.get("entity_id"), node_key),
            "label": label,
            "graph_label": _pick_first(node.get("_label"), source_type, "unknown"),
            "source_type": source_type or _pick_first(node.get("_label"), "unknown").lower(),
            "role": role,
            "is_anchor": is_anchor,
            "source_order": source_order,
            "score": source_ref.get("score"),
            "meeting_id": _pick_first(source_ref.get("meeting_id"), node.get("meeting_id")),
            "document_id": _pick_first(source_ref.get("document_id"), node.get("document_id")),
            "snapshot_type": _pick_first(source_ref.get("snapshot_type"), node.get("snapshot_type")),
            "snippet": source_ref.get("snippet", ""),
            "retrieval_stage": _pick_first(source_ref.get("retrieval_stage"), "unknown"),
            "retrieval_sources": source_ref.get("retrieval_sources", []),
        }

    async def get_neighbors(self, node_key: str) -> List[Tuple[str, Dict[str, Any]]]:
        node_key = _stringify(node_key)
        if not node_key:
            return []
        if node_key in self._neighbor_cache:
            return self._neighbor_cache[node_key]

        neighbors: List[Tuple[str, Dict[str, Any]]] = []
        seen_edges = set()

        try:
            relationships = await self.graph_builder.get_node_relationships(node_key)
        except Exception as exc:
            logger.debug("Graph trace relationships failed for %s: %s", node_key, exc)
            relationships = {"incoming": [], "outgoing": []}

        for rel in relationships.get("outgoing", []):
            target_key = _stringify(rel.get("target"))
            label = _pick_first(rel.get("type"), rel.get("_type"), rel.get("relationship_type"), "RELATED")
            edge_key = (node_key, target_key, label)
            if target_key and edge_key not in seen_edges:
                seen_edges.add(edge_key)
                neighbors.append((target_key, {"from_key": node_key, "to_key": target_key, "label": label}))

        for rel in relationships.get("incoming", []):
            source_key = _stringify(rel.get("source"))
            label = _pick_first(rel.get("type"), rel.get("_type"), rel.get("relationship_type"), "RELATED")
            edge_key = (source_key, node_key, label)
            if source_key and edge_key not in seen_edges:
                seen_edges.add(edge_key)
                neighbors.append((source_key, {"from_key": source_key, "to_key": node_key, "label": label}))

        self._neighbor_cache[node_key] = neighbors
        return neighbors

    async def find_path(self, start_key: str, end_key: str, max_depth: int = 3) -> Optional[Dict[str, Any]]:
        start_key = _stringify(start_key)
        end_key = _stringify(end_key)
        if not start_key or not end_key:
            return None
        if start_key == end_key:
            return {"node_keys": [start_key], "edges": []}

        queue = deque([(start_key, [start_key], [])])
        visited_depth = {start_key: 0}

        while queue:
            current_key, node_path, edge_path = queue.popleft()
            if len(edge_path) >= max_depth:
                continue

            for next_key, edge in await self.get_neighbors(current_key):
                if not next_key or next_key in node_path:
                    continue

                next_node_path = [*node_path, next_key]
                next_edge_path = [*edge_path, edge]
                if next_key == end_key:
                    return {"node_keys": next_node_path, "edges": next_edge_path}

                depth = len(next_edge_path)
                if visited_depth.get(next_key, 999) <= depth:
                    continue
                visited_depth[next_key] = depth
                queue.append((next_key, next_node_path, next_edge_path))

        return None


async def build_graph_trace(
    graph_builder: Any,
    sources: List[Dict[str, Any]],
    reasoning_steps: Optional[List[str]] = None,
    title: str = "Reasoning trace",
    max_primary_nodes: int = 6,
    max_depth: int = 3,
) -> Optional[Dict[str, Any]]:
    if not graph_builder or not sources:
        return None

    resolver = _GraphTraceResolver(graph_builder)
    primary_nodes: List[Dict[str, Any]] = []
    unmatched_sources: List[Dict[str, Any]] = []
    seen_primary_keys = set()

    for source_index, source in enumerate(sources[:12]):
        resolved = await resolver.resolve_source(source, source_index)
        if not resolved:
            normalized = _normalize_source(source)
            unmatched_sources.append({
                "id": normalized["source_id"],
                "title": normalized["title"] or normalized["entity_id"] or normalized["source_id"],
                "source_type": normalized["source_type"],
                "role": normalized["role"],
                "retrieval_stage": normalized["retrieval_stage"],
                "retrieval_sources": normalized["retrieval_sources"],
            })
            continue

        node_key = resolved["node_key"]
        if node_key in seen_primary_keys:
            continue
        seen_primary_keys.add(node_key)
        primary_nodes.append(resolved)

        if len(primary_nodes) >= max_primary_nodes:
            break

    if len(primary_nodes) < 2:
        return None

    unique_nodes = {node["node_key"]: node for node in primary_nodes}
    ordered_node_keys: List[str] = [primary_nodes[0]["node_key"]]
    trace_edges: List[Dict[str, Any]] = []
    trace_segments: List[Dict[str, Any]] = []
    seen_edges = set()
    missing_links: List[Dict[str, Any]] = []

    for previous_node, current_node in itertools.pairwise(primary_nodes):
        path = await resolver.find_path(
            previous_node["node_key"],
            current_node["node_key"],
            max_depth=max_depth,
        )

        if not path:
            if current_node["node_key"] not in ordered_node_keys:
                ordered_node_keys.append(current_node["node_key"])
            missing_link = {
                "from_key": previous_node["node_key"],
                "to_key": current_node["node_key"],
                "from_label": previous_node["label"],
                "to_label": current_node["label"],
                "reason": f"no_graph_path_within_depth_{max_depth}",
                "segment_stage": current_node.get("retrieval_stage", "unknown"),
                "segment_sources": current_node.get("retrieval_sources", []),
            }
            missing_links.append(missing_link)
            trace_segments.append({
                "from_key": previous_node["node_key"],
                "to_key": current_node["node_key"],
                "from_label": previous_node["label"],
                "to_label": current_node["label"],
                "from_retrieval_stage": previous_node.get("retrieval_stage", "unknown"),
                "to_retrieval_stage": current_node.get("retrieval_stage", "unknown"),
                "segment_stage": current_node.get("retrieval_stage", "unknown"),
                "segment_sources": current_node.get("retrieval_sources", []),
                "bridge_kind": "gap",
                "path_found": False,
                "path_edge_count": 0,
                "intermediate_node_keys": [],
                "relation_labels": [],
                "reason": missing_link["reason"],
                "path_order": len(trace_segments),
            })
            continue

        trace_segments.append({
            "from_key": previous_node["node_key"],
            "to_key": current_node["node_key"],
            "from_label": previous_node["label"],
            "to_label": current_node["label"],
            "from_retrieval_stage": previous_node.get("retrieval_stage", "unknown"),
            "to_retrieval_stage": current_node.get("retrieval_stage", "unknown"),
            "segment_stage": current_node.get("retrieval_stage", "unknown"),
            "segment_sources": current_node.get("retrieval_sources", []),
            "bridge_kind": "graph_path",
            "path_found": True,
            "path_edge_count": len(path.get("edges", [])),
            "intermediate_node_keys": path.get("node_keys", [])[1:-1],
            "relation_labels": _dedupe_strings([_pick_first(edge.get("label"), "RELATED") for edge in path.get("edges", [])]),
            "reason": "",
            "path_order": len(trace_segments),
        })

        for node_key in path.get("node_keys", [])[1:]:
            if node_key not in unique_nodes:
                anchor_node = await resolver.build_anchor_node(node_key)
                if anchor_node:
                    unique_nodes[node_key] = anchor_node
            if node_key not in ordered_node_keys:
                ordered_node_keys.append(node_key)

        for edge in path.get("edges", []):
            edge_key = (edge.get("from_key"), edge.get("to_key"), edge.get("label"))
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            trace_edges.append({
                "from_key": edge.get("from_key"),
                "to_key": edge.get("to_key"),
                "label": edge.get("label", "RELATED"),
                "path_order": len(trace_edges),
                "kind": "graph_relation",
                "retrieval_stage": "graph",
                "segment_stage": current_node.get("retrieval_stage", "unknown"),
            })

    ordered_nodes = [unique_nodes[node_key] for node_key in ordered_node_keys if node_key in unique_nodes]
    for path_order, node in enumerate(ordered_nodes):
        node["path_order"] = path_order

    reasoning_steps = [step.strip() for step in (reasoning_steps or []) if _stringify(step)]

    return {
        "title": title,
        "mode": "backend_provenance_v2",
        "nodes": ordered_nodes,
        "edges": trace_edges,
        "segments": trace_segments,
        "ordered_node_keys": ordered_node_keys,
        "reasoning_steps": reasoning_steps[:6],
        "unmatched_sources": unmatched_sources[:6],
        "missing_links": missing_links[:6],
        "matched_primary_nodes": len(primary_nodes),
        "edge_count": len(trace_edges),
    }
