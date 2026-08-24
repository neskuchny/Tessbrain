# -*- coding: utf-8 -*-
"""
Многослойный загрузчик контекста для чата Tessbrain.

Цель:
- не собирать memory context вручную в chat route
- централизованно загружать snapshots / documents / session context
- держать под контролем объём контекста для скорости и цены
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


_SEARCH_DEPTH_LIMITS: Dict[str, Dict[str, int]] = {
    "quick": {
        "session_chars": 700,
        "company_chars": 900,
        "entity_chars": 700,
        "company_snippet_chars": 220,
        "doc_chunks": 2,
        "max_documents": 2,
        "max_entities": 2,
    },
    "standard": {
        "session_chars": 1100,
        "company_chars": 1600,
        "entity_chars": 900,
        "company_snippet_chars": 260,
        "doc_chunks": 3,
        "max_documents": 3,
        "max_entities": 3,
    },
    "deep": {
        "session_chars": 1500,
        "company_chars": 2200,
        "entity_chars": 1200,
        "company_snippet_chars": 320,
        "doc_chunks": 4,
        "max_documents": 4,
        "max_entities": 4,
    },
    "comprehensive": {
        "session_chars": 1800,
        "company_chars": 2600,
        "entity_chars": 1400,
        "company_snippet_chars": 360,
        "doc_chunks": 5,
        "max_documents": 5,
        "max_entities": 5,
    },
}


@dataclass
class MemoryContextBlock:
    """Один блок memory context."""
    layer: str
    title: str
    text: str
    source_type: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LoadedMemoryContext:
    """Итог загруженного memory context."""
    blocks: List[MemoryContextBlock] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)

    def add_block(
        self,
        layer: str,
        title: str,
        text: str,
        source_type: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        cleaned_text = (text or "").strip()
        if not cleaned_text:
            return
        self.blocks.append(
            MemoryContextBlock(
                layer=layer,
                title=title,
                text=cleaned_text,
                source_type=source_type,
                metadata=metadata or {},
            )
        )

    def to_text(self) -> str:
        """Собрать весь контекст в один текст для передачи в search/synthesis."""
        sections = []
        for block in self.blocks:
            sections.append(f"=== {block.layer} | {block.title} ===\n{block.text}")
        return "\n\n".join(sections).strip()

    def summary_lines(self) -> List[str]:
        """Короткие диагностические строки для reasoning_steps."""
        if not self.blocks:
            return []

        source_counts: Dict[str, int] = {}
        for block in self.blocks:
            source_counts[block.source_type] = source_counts.get(block.source_type, 0) + 1

        lines = [f"🧠 Memory context: {len(self.blocks)} блоков"]
        if source_counts.get("session_history"):
            lines.append("   SESSION: загружена история разговора")
        if source_counts.get("company_snapshot_snippet"):
            lines.append("   L0: загружен паспорт компании")
        if source_counts.get("company_snapshot"):
            lines.append("   L1: загружен полный company snapshot")

        entity_count = sum(
            count for key, count in source_counts.items()
            if key.endswith("_snapshot") and key != "company_snapshot"
        )
        if entity_count:
            lines.append(f"   L2: загружено entity snapshots: {entity_count}")

        if source_counts.get("document_context"):
            lines.append(f"   L3: загружен document context: {source_counts['document_context']}")

        return lines


class ChatMemoryContextLoader:
    """
    Централизованная загрузка контекста для chat pipeline.

    Слои:
    - SESSION: последние реплики чата
    - L0: короткий паспорт компании
    - L1: полный company snapshot
    - L2: entity snapshots по явным target'ам
    - L3: document context по фильтру документов
    """

    def __init__(self, graph_builder: Any, user_id: str):
        self.graph_builder = graph_builder
        self.user_id = user_id

    async def build_context(
        self,
        query: str,
        search_depth: str = "standard",
        session_context_text: str = "",
        document_filter: Optional[Sequence[str] | str] = None,
        entity_targets: Optional[Sequence[Any] | Any] = None,
    ) -> LoadedMemoryContext:
        limits = self._get_limits(search_depth)
        context = LoadedMemoryContext(
            stats={
                "search_depth": search_depth,
                "session_loaded": False,
                "company_loaded": False,
                "entity_snapshots_loaded": 0,
                "documents_loaded": 0,
                "implicit_entity_targets": 0,
                "graph_type_enriched_targets": 0,
            }
        )

        self._load_session_context(context, session_context_text, limits)
        merged_entity_targets = await self._merge_entity_targets(query, entity_targets, context)
        await self._load_snapshot_context(context, limits, merged_entity_targets)
        await self._load_document_context(context, query, document_filter, limits)

        context.stats["blocks"] = len(context.blocks)
        context.stats["total_chars"] = len(context.to_text())
        return context

    def _get_limits(self, search_depth: str) -> Dict[str, int]:
        return _SEARCH_DEPTH_LIMITS.get(search_depth, _SEARCH_DEPTH_LIMITS["standard"])

    def _load_session_context(
        self,
        context: LoadedMemoryContext,
        session_context_text: str,
        limits: Dict[str, int],
    ) -> None:
        if not session_context_text or not session_context_text.strip():
            return

        trimmed = session_context_text.strip()[:limits["session_chars"]]
        context.add_block(
            layer="SESSION",
            title="История разговора",
            text=trimmed,
            source_type="session_history",
        )
        context.stats["session_loaded"] = True

    async def _load_snapshot_context(
        self,
        context: LoadedMemoryContext,
        limits: Dict[str, int],
        entity_targets: Optional[Sequence[Any] | Any],
    ) -> None:
        try:
            from backend.core.config import flags
            if not flags.enable_enhanced_snapshots:
                return
        except ImportError:
            return

        try:
            from backend.core.sleep.enhanced_snapshot import get_enhanced_snapshot_generator

            snapshot_gen = get_enhanced_snapshot_generator(self.graph_builder, user_id=self.user_id)
            company_snapshot = await snapshot_gen.get_company_snapshot()

            if company_snapshot:
                snippet = company_snapshot.snippet or company_snapshot.generate_snippet()
                if snippet:
                    context.add_block(
                        layer="L0",
                        title="Паспорт компании",
                        text=snippet[:limits["company_snippet_chars"]],
                        source_type="company_snapshot_snippet",
                        metadata={"snapshot_type": "company", "version": company_snapshot.version},
                    )

                context.add_block(
                    layer="L1",
                    title="Снапшот компании",
                    text=company_snapshot.to_text(max_length=limits["company_chars"]),
                    source_type="company_snapshot",
                    metadata={"snapshot_type": "company", "version": company_snapshot.version},
                )
                context.stats["company_loaded"] = True

            normalized_targets = self._normalize_targets(entity_targets)[:limits["max_entities"]]
            for target in normalized_targets:
                snapshot_type, snapshot = await self._resolve_entity_snapshot(snapshot_gen, target)
                if not snapshot:
                    continue

                entity_name = getattr(snapshot, "name", None) or target.get("name") or target["id"]
                context.add_block(
                    layer="L2",
                    title=f"{snapshot_type.capitalize()} snapshot: {entity_name}",
                    text=snapshot.to_text(max_length=limits["entity_chars"]),
                    source_type=f"{snapshot_type}_snapshot",
                    metadata={
                        "snapshot_type": snapshot_type,
                        "entity_id": target["id"],
                        "entity_name": entity_name,
                        "target_type": target.get("type", ""),
                    },
                )
                context.stats["entity_snapshots_loaded"] += 1

        except Exception as e:
            logger.debug(f"Could not load snapshot context: {e}")

    async def _load_document_context(
        self,
        context: LoadedMemoryContext,
        query: str,
        document_filter: Optional[Sequence[str] | str],
        limits: Dict[str, int],
    ) -> None:
        document_ids = self._normalize_document_ids(document_filter)
        if not document_ids:
            return

        try:
            from backend.core.documents import DocumentIndexer

            doc_indexer = DocumentIndexer(user_id=self.user_id)
            await doc_indexer.initialize()

            for document_id in document_ids[:limits["max_documents"]]:
                doc_context = await doc_indexer.get_document_context(
                    document_id=document_id,
                    query=query,
                    max_chunks=limits["doc_chunks"],
                )
                if not doc_context:
                    continue

                context.add_block(
                    layer="L3",
                    title=f"Документ {document_id}",
                    text=doc_context,
                    source_type="document_context",
                    metadata={"document_id": document_id},
                )
                context.stats["documents_loaded"] += 1

        except Exception as e:
            logger.warning(f"Could not load document context: {e}")

    async def _merge_entity_targets(
        self,
        query: str,
        entity_targets: Optional[Sequence[Any] | Any],
        context: LoadedMemoryContext,
    ) -> List[Dict[str, str]]:
        """
        Объединить явные entity targets с теми, что можно дёшево вытащить из запроса.
        """
        explicit_targets = self._normalize_targets(entity_targets)
        implicit_targets = await self._infer_targets_from_query(query)
        context.stats["implicit_entity_targets"] = len(implicit_targets)

        merged: List[Dict[str, str]] = []
        seen_by_id: Dict[str, int] = {}
        for target in explicit_targets + implicit_targets:
            target_id = str(target.get("id", "")).strip()
            if not target_id:
                continue
            key = target_id.lower()
            target_type = str(target.get("type", "")).strip().lower()

            if key in seen_by_id:
                existing = merged[seen_by_id[key]]
                existing_type = str(existing.get("type", "")).strip().lower()
                if existing_type in {"", "unknown"} and target_type not in {"", "unknown"}:
                    existing["type"] = target_type
                if not existing.get("name") and target.get("name"):
                    existing["name"] = target.get("name", "")
                continue

            seen_by_id[key] = len(merged)
            merged.append({
                "id": target_id,
                "type": target_type,
                "name": str(target.get("name", "")).strip(),
            })

        enriched_targets, enriched_count = await self._enrich_target_types_from_graph(merged)
        context.stats["graph_type_enriched_targets"] = enriched_count
        return enriched_targets

    async def _infer_targets_from_query(self, query: str) -> List[Dict[str, str]]:
        """Извлечь потенциальные snapshot targets из запроса без LLM."""
        if not query or not query.strip():
            return []

        try:
            from backend.core.think.query_analyzer import QueryAnalyzer

            analyzer = QueryAnalyzer(llm_router=None)
            intent = await analyzer.analyze(query)
            allowed_types = {"project", "person", "product", "department", "team", "unknown"}

            targets: List[Dict[str, str]] = []
            for entity in intent.entities:
                entity_name = str(entity.get("name", "")).strip()
                entity_type = str(entity.get("type", "")).strip().lower() or "unknown"
                if not entity_name or entity_type not in allowed_types:
                    continue

                targets.append({
                    "id": entity_name,
                    "type": entity_type,
                    "name": entity_name,
                })

            return targets
        except Exception as e:
            logger.debug(f"Could not infer entity targets from query: {e}")
            return []

    async def _enrich_target_types_from_graph(
        self,
        targets: List[Dict[str, str]],
    ) -> tuple[List[Dict[str, str]], int]:
        """
        Попытаться уточнить тип сущности по графу, если analyzer вернул unknown.
        Это дешевле, чем тащить отдельный LLM/entity resolver на каждый запрос.
        """
        if not targets or not self.graph_builder or not hasattr(self.graph_builder, "search_nodes"):
            return targets, 0

        enriched_count = 0
        enriched_targets: List[Dict[str, str]] = []
        for target in targets:
            current_type = str(target.get("type", "")).strip().lower()
            if current_type and current_type != "unknown":
                enriched_targets.append(target)
                continue

            lookup_text = str(target.get("name") or target.get("id") or "").strip()
            inferred_type = await self._infer_graph_target_type(lookup_text)
            if inferred_type:
                enriched_targets.append({
                    **target,
                    "type": inferred_type,
                })
                enriched_count += 1
            else:
                enriched_targets.append(target)

        return enriched_targets, enriched_count

    async def _infer_graph_target_type(self, query_text: str) -> str:
        """Определить тип target по локальному graph search."""
        query_text = (query_text or "").strip()
        if not query_text:
            return ""

        try:
            results = await self.graph_builder.search_nodes(query=query_text, limit=5)
        except Exception as e:
            logger.debug(f"Could not search graph for target type inference: {e}")
            return ""

        query_lower = query_text.lower()
        best_type = ""
        best_score = 0
        for node in results or []:
            inferred_type = self._map_graph_node_to_target_type(node)
            if not inferred_type:
                continue

            score = self._score_target_graph_match(query_lower, node)
            if score > best_score:
                best_score = score
                best_type = inferred_type

        return best_type if best_score >= 2 else ""

    def _map_graph_node_to_target_type(self, node: Dict[str, Any]) -> str:
        """Свести graph label/entity_type к snapshot target type."""
        candidates = [
            str(node.get("entity_type", "")).strip().lower(),
            str(node.get("_label", "")).strip().lower(),
            str(node.get("type", "")).strip().lower(),
        ]
        mapping = {
            "person": "person",
            "employee": "person",
            "participant": "person",
            "speaker": "person",
            "founder": "person",
            "ceo": "person",
            "project": "project",
            "product": "product",
            "department": "department",
            "dept": "department",
            "team": "team",
        }
        for candidate in candidates:
            if candidate in mapping:
                return mapping[candidate]
        return ""

    def _score_target_graph_match(self, query_lower: str, node: Dict[str, Any]) -> int:
        """Грубая эвристика: насколько найденный граф-узел похож на target."""
        names = [
            str(node.get("name", "")).strip().lower(),
            str(node.get("id", "")).strip().lower(),
            str(node.get("normalized_name", "")).strip().lower(),
        ]

        aliases_raw = node.get("aliases", [])
        aliases: List[str] = []
        if isinstance(aliases_raw, list):
            aliases = [str(alias).strip().lower() for alias in aliases_raw if str(alias).strip()]
        elif isinstance(aliases_raw, str) and aliases_raw.strip():
            aliases = [aliases_raw.strip().lower()]

        if query_lower in aliases:
            return 4
        if any(name == query_lower for name in names if name):
            return 3
        if any(query_lower in name or name in query_lower for name in names if name):
            return 2
        return 1

    def _normalize_targets(self, entity_targets: Optional[Sequence[Any] | Any]) -> List[Dict[str, str]]:
        if not entity_targets:
            return []

        raw_targets = list(entity_targets) if isinstance(entity_targets, (list, tuple, set)) else [entity_targets]
        normalized: List[Dict[str, str]] = []

        for target in raw_targets:
            if isinstance(target, str):
                target_id = target.strip()
                if target_id:
                    normalized.append({"id": target_id, "type": "", "name": ""})
                continue

            if isinstance(target, dict):
                target_id = (
                    target.get("id")
                    or target.get("entity_id")
                    or target.get("snapshot_id")
                    or target.get("value")
                    or ""
                )
                target_id = str(target_id).strip()
                if target_id:
                    normalized.append({
                        "id": target_id,
                        "type": str(target.get("type") or target.get("snapshot_type") or "").strip(),
                        "name": str(target.get("name") or "").strip(),
                    })

        return normalized

    def _normalize_document_ids(self, document_filter: Optional[Sequence[str] | str]) -> List[str]:
        if not document_filter:
            return []

        raw_ids = list(document_filter) if isinstance(document_filter, (list, tuple, set)) else [document_filter]
        result = []
        for item in raw_ids:
            text = str(item).strip()
            if text:
                result.append(text)
        return result

    async def _resolve_entity_snapshot(self, snapshot_gen: Any, target: Dict[str, str]) -> tuple[str, Any]:
        entity_id = target["id"]
        entity_type = target.get("type", "").lower()

        resolvers: List[tuple[str, Any]]
        if entity_type in {"person", "employee", "participant", "user"}:
            resolvers = [("person", snapshot_gen.get_person_snapshot)]
        elif entity_type in {"project"}:
            resolvers = [("project", snapshot_gen.get_project_snapshot)]
        elif entity_type in {"product"}:
            resolvers = [("product", snapshot_gen.get_product_snapshot)]
        elif entity_type in {"department", "dept"}:
            resolvers = [("department", snapshot_gen.get_department_snapshot)]
        elif entity_type in {"team"}:
            resolvers = [("team", snapshot_gen.get_team_snapshot)]
        else:
            resolvers = [
                ("person", snapshot_gen.get_person_snapshot),
                ("project", snapshot_gen.get_project_snapshot),
                ("product", snapshot_gen.get_product_snapshot),
                ("department", snapshot_gen.get_department_snapshot),
                ("team", snapshot_gen.get_team_snapshot),
            ]

        for snapshot_type, getter in resolvers:
            try:
                snapshot = await getter(entity_id)
                if snapshot:
                    return snapshot_type, snapshot
            except Exception:
                continue

        return "", None
