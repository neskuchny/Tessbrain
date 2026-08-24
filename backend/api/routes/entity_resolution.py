# -*- coding: utf-8 -*-
"""
Entity Resolution API Routes - эндпоинты для дедупликации и резолвинга сущностей.
"""

import logging
from typing import Any, Dict, Optional

from litestar import Controller, get, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter
from pydantic import BaseModel

from ...core.store.entity_resolver import EntityResolver

logger = logging.getLogger(__name__)


class ResolveEntityRequest(BaseModel):
    """Запрос на резолвинг сущности"""
    name: str
    entity_type: str  # person, project, task, etc.
    organization_id: str = "default"
    external_ids: Optional[Dict[str, str]] = None
    properties: Optional[Dict[str, Any]] = None


class CheckSameEntityRequest(BaseModel):
    """Запрос на проверку идентичности сущностей"""
    name1: str
    name2: str
    entity_type: str
    context1: Optional[Dict[str, Any]] = None
    context2: Optional[Dict[str, Any]] = None


class MergeEntitiesRequest(BaseModel):
    """Запрос на слияние сущностей"""
    source_id: str
    target_id: str


class EntityResolutionController(Controller):
    """Контроллер для Entity Resolution"""

    path = "/entity-resolution"
    tags = ["Entity Resolution"]

    def _get_resolver(self, graph=None) -> EntityResolver:
        """Получить EntityResolver"""
        return EntityResolver(graph_builder=graph)

    @post("/resolve")
    async def resolve_entity(self, data: ResolveEntityRequest) -> Dict[str, Any]:
        """
        Резолвить сущность - найти существующую или создать новую.

        Проверяет:
        1. External IDs (email, hr_id, etc.)
        2. Точное совпадение имени
        3. Aliases
        4. Fuzzy matching
        """
        from ...core.store.graph_builder import GraphBuilder

        graph = GraphBuilder(
            use_networkx=None,  # Auto-detect backend
            graph_storage_path="data/tessent_brain_graph.json"
        )
        await graph.connect()

        resolver = self._get_resolver(graph)

        result = await resolver.resolve(
            name=data.name,
            entity_type=data.entity_type,
            organization_id=data.organization_id,
            external_ids=data.external_ids,
            properties=data.properties
        )

        return {
            "status": "success",
            "resolved": {
                "canonical_id": result.canonical_id,
                "name": result.name,
                "entity_type": result.entity_type,
                "is_new": result.is_new,
                "confidence": result.confidence,
                "matched_by": result.matched_by,
                "aliases": result.aliases
            }
        }

    @post("/check-same")
    async def check_same_entity(self, data: CheckSameEntityRequest) -> Dict[str, Any]:
        """
        Проверить, являются ли два имени одной сущностью.

        Использует LLM для интеллектуального сравнения.
        """
        from ...core.store.graph_builder import GraphBuilder

        graph = GraphBuilder(
            use_networkx=None,  # Auto-detect backend
            graph_storage_path="data/tessent_brain_graph.json"
        )
        await graph.connect()

        resolver = self._get_resolver(graph)

        is_same, confidence, reasoning = await resolver.check_same_entity_llm(
            name1=data.name1,
            name2=data.name2,
            entity_type=data.entity_type,
            context1=data.context1,
            context2=data.context2
        )

        return {
            "status": "success",
            "name1": data.name1,
            "name2": data.name2,
            "is_same": is_same,
            "confidence": confidence,
            "reasoning": reasoning
        }

    @get("/duplicates")
    async def find_duplicates(
        self,
        entity_type: Optional[str] = None,
        threshold: float = Parameter(default=0.80, ge=0.5, le=1.0),
        use_llm: bool = Parameter(default=False)
    ) -> Dict[str, Any]:
        """
        Найти потенциальные дубликаты в графе.

        Args:
            entity_type: Тип сущности (person, project, etc.)
            threshold: Минимальный порог схожести
            use_llm: Использовать LLM для проверки
        """
        from ...core.store.graph_builder import GraphBuilder

        graph = GraphBuilder(
            use_networkx=None,  # Auto-detect backend
            graph_storage_path="data/tessent_brain_graph.json"
        )
        await graph.connect()

        resolver = self._get_resolver(graph)

        if use_llm:
            duplicates = await resolver.find_duplicates_llm(
                entity_type=entity_type,
                batch_size=20
            )
            return {
                "status": "success",
                "method": "llm",
                "count": len(duplicates),
                "duplicates": duplicates
            }
        else:
            candidates = await resolver.find_merge_candidates(
                entity_type=entity_type,
                threshold=threshold
            )
            return {
                "status": "success",
                "method": "fuzzy",
                "count": len(candidates),
                "candidates": [
                    {
                        "entity1_id": c.entity1_id,
                        "entity1_name": c.entity1_name,
                        "entity2_id": c.entity2_id,
                        "entity2_name": c.entity2_name,
                        "entity_type": c.entity_type,
                        "similarity_score": c.similarity_score,
                        "match_reason": c.match_reason,
                        "auto_merge": c.auto_merge
                    }
                    for c in candidates
                ]
            }

    @post("/merge")
    async def merge_entities(self, data: MergeEntitiesRequest) -> Dict[str, Any]:
        """
        Слить две сущности.

        Source будет удалён, все связи перенесены на target.
        """
        from ...core.store.graph_builder import GraphBuilder

        graph = GraphBuilder(
            use_networkx=None,  # Auto-detect backend
            graph_storage_path="data/tessent_brain_graph.json"
        )
        await graph.connect()

        resolver = self._get_resolver(graph)

        success = await resolver.merge_entities_networkx(
            source_id=data.source_id,
            target_id=data.target_id
        )

        if not success:
            raise HTTPException(status_code=400, detail="Failed to merge entities")

        # Сохраняем граф
        graph.save_graph()

        return {
            "status": "success",
            "merged": {
                "source_id": data.source_id,
                "target_id": data.target_id
            }
        }

    @post("/auto-merge")
    async def auto_merge_duplicates(
        self,
        entity_type: Optional[str] = None,
        threshold: float = Parameter(default=0.95, ge=0.9, le=1.0)
    ) -> Dict[str, Any]:
        """
        Автоматически слить очевидные дубликаты.

        ⚠️ Используйте с осторожностью! Сливает только с высоким порогом.
        """
        from ...core.store.graph_builder import GraphBuilder

        graph = GraphBuilder(
            use_networkx=None,  # Auto-detect backend
            graph_storage_path="data/tessent_brain_graph.json"
        )
        await graph.connect()

        resolver = self._get_resolver(graph)

        stats = await resolver.auto_merge_duplicates(
            entity_type=entity_type,
            threshold=threshold
        )

        return {
            "status": "success",
            "stats": stats
        }

    @get("/stats")
    async def get_stats(self) -> Dict[str, Any]:
        """
        Получить статистику Entity Resolution.
        """
        from ...core.store.graph_builder import GraphBuilder

        graph = GraphBuilder(
            use_networkx=None,  # Auto-detect backend
            graph_storage_path="data/tessent_brain_graph.json"
        )
        await graph.connect()

        resolver = self._get_resolver(graph)

        stats = resolver.get_stats()

        return {
            "status": "success",
            "stats": stats
        }


# Router для подключения к app
router = EntityResolutionController



