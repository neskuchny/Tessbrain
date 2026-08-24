# -*- coding: utf-8 -*-
"""
Hebbian Learning - Усиление связей при совместном упоминании.

Принцип: "Neurons that fire together, wire together"
(Нейроны, которые активируются вместе, связываются вместе)

Применение в Tessbrain:
- Сущности, упоминаемые вместе на встречах, получают усиленные связи
- Часто совместно упоминаемые сущности формируют кластеры
- Связи, которые не используются, постепенно ослабевают (temporal decay)

Использование:
```python
from backend.core.memory.hebbian_learning import HebbianLearning

hebbian = HebbianLearning(graph_builder)

# После обработки встречи
await hebbian.process_meeting_cooccurrences(
    meeting_id="meeting_123",
    entities=["Проект А", "Кирилл", "Дедлайн"]
)

# Ночное затухание
await hebbian.apply_temporal_decay(decay_rate=0.01)
```
"""
import logging
from datetime import datetime, timedelta, timezone
from itertools import combinations
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class HebbianLearning:
    """
    Hebbian Learning для усиления связей в графе знаний.

    Основные функции:
    1. Усиление связей между совместно упоминаемыми сущностями
    2. Временное затухание неиспользуемых связей
    3. Отслеживание паттернов совместного упоминания
    """

    # Константы для Hebbian Learning
    LEARNING_RATE = 0.1  # Скорость обучения
    MAX_WEIGHT = 10.0    # Максимальный вес связи
    MIN_WEIGHT = 0.1     # Минимальный вес связи
    DECAY_RATE = 0.01    # Скорость затухания (по умолчанию)

    # --- Порог совместного упоминания (guardrail против O(n²) ложных связей) ---
    # Первое со-упоминание создаёт лишь "слабое" (provisional) ребро. Только если
    # пара встретилась ПОВТОРНО (в другой встрече) связь промотируется в реальную.
    # Слабое ребро само истлевает через temporal_decay, если повтора не было.
    PROVISIONAL_WEIGHT = 0.15   # Начальный вес для одиночного со-упоминания
    PROMOTION_WEIGHT = 0.5      # Порог: weight < этого ⇒ связь ещё provisional
    PROMOTED_WEIGHT = 1.0       # Вес реальной связи после 2-го со-упоминания
    # Предохранитель от взрыва пар на огромных встречах (n*(n-1)/2 операций).
    MAX_ENTITIES_FOR_PAIRING = 25

    # --- Типо-зависимое затухание (guardrail против исчезновения связей) ---
    # Затуханию подвергаются ТОЛЬКО контекстные co-mention рёбра (созданные
    # hebbian). Структурные/смысловые связи (DECIDED, ASSIGNED_TO, PROPOSED,
    # HAS_PROFILE, SUPERSEDES, ...) НЕ затухают никогда.
    DECAYABLE_CREATED_BY = "hebbian_learning"
    # Слабое provisional-ребро, не подтверждённое повтором за столько дней,
    # удаляется (подчистка для guardrail A).
    PROVISIONAL_TTL_DAYS = 30
    # Реальная co-mention связь затухает только если её не трогали так долго —
    # длинное окно бережёт «редкие, но стратегические» связи (CEO↔CFO ежеквартально).
    REAL_DECAY_DAYS = 90

    def __init__(self, graph_builder):
        """
        Args:
            graph_builder: Экземпляр GraphBuilder
        """
        self.graph = graph_builder

    async def strengthen_connection(
        self,
        entity1_id: str,
        entity2_id: str,
        rel_type: str = "RELATED_TO",
        strength: float | None = None,
        context: Optional[str] = None
    ) -> bool:
        """
        Усилить связь между двумя сущностями.

        Формула: Δw = η × (1 - w/w_max)
        где η - learning rate, w - текущий вес, w_max - максимальный вес

        Args:
            entity1_id: ID первой сущности
            entity2_id: ID второй сущности
            rel_type: Тип связи
            strength: Сила усиления (опционально, иначе используется LEARNING_RATE)
            context: Контекст усиления (для логирования)

        Returns:
            True если связь усилена
        """
        try:
            from backend.core.config import flags
            if not flags.enable_hebbian_learning:
                return True

            # Получаем текущий вес связи
            current_weight = await self._get_edge_weight(entity1_id, entity2_id, rel_type)

            if current_weight is None:
                # Создаём новую связь
                success = await self.graph.create_relationship(
                    from_id=entity1_id,
                    to_id=entity2_id,
                    rel_type=rel_type,
                    properties={
                        "weight": 1.0,
                        "co_occurrences": 1,
                        "created_by": "hebbian_learning",
                        "context": context or "",
                    }
                )
                if success:
                    logger.debug(f"Created new Hebbian connection: {entity1_id} -> {entity2_id}")
                return success

            # Вычисляем усиление по формуле Hebbian
            eta = strength if strength is not None else self.LEARNING_RATE
            delta_w = eta * (1 - current_weight / self.MAX_WEIGHT)
            new_weight = min(self.MAX_WEIGHT, current_weight + delta_w)

            # Обновляем вес
            success = await self.graph.update_edge_weight(
                from_id=entity1_id,
                to_id=entity2_id,
                rel_type=rel_type,
                set_weight=new_weight,
                increment_co_occurrences=True
            )

            if success:
                logger.debug(
                    f"Hebbian strengthened: {entity1_id} -> {entity2_id} "
                    f"({current_weight:.2f} -> {new_weight:.2f})"
                )

            return success

        except Exception as e:
            logger.error(f"❌ Hebbian strengthen failed: {e}")
            return False

    async def process_meeting_cooccurrences(
        self,
        meeting_id: str,
        entities: List[Dict[str, Any]],
        participants: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """
        Обработать совместные упоминания из встречи.

        Для каждой пары сущностей, упомянутых на одной встрече,
        усиливаем связь между ними.

        Args:
            meeting_id: ID встречи
            entities: Список сущностей (с id и name)
            participants: Список участников (опционально)

        Returns:
            Статистика обработки
        """
        stats = {
            "meeting_id": meeting_id,
            "entities_processed": 0,
            "connections_strengthened": 0,
            "new_connections": 0,
            "provisional_created": 0,   # одиночное со-упоминание (слабое ребро)
            "promoted": 0,              # повторное со-упоминание ⇒ реальная связь
            "errors": 0,
        }

        try:
            from backend.core.config import flags
            if not flags.enable_hebbian_learning:
                return stats

            # Собираем все ID сущностей (без дублей, сохраняя порядок)
            entity_ids: List[str] = []
            _seen: set = set()
            for e in entities:
                eid = e.get("entity_id") or e.get("id")
                if eid and eid not in _seen:
                    _seen.add(eid)
                    entity_ids.append(eid)

            # Добавляем участников если есть
            if participants:
                for p in participants:
                    pid = p.get("person_id") or p.get("id")
                    if pid and pid not in _seen:
                        _seen.add(pid)
                        entity_ids.append(pid)

            stats["entities_processed"] = len(entity_ids)

            # Предохранитель: на очень многолюдных встречах пар слишком много и
            # сигнал низкий (все «со всеми»). Ограничиваем и явно логируем срез,
            # чтобы это не выглядело как «обработали всё».
            if len(entity_ids) > self.MAX_ENTITIES_FOR_PAIRING:
                logger.warning(
                    f"⚠️ Hebbian: meeting {meeting_id} имеет {len(entity_ids)} сущностей "
                    f"(> {self.MAX_ENTITIES_FOR_PAIRING}); связываю только первые "
                    f"{self.MAX_ENTITIES_FOR_PAIRING}, остальные пары пропущены"
                )
                stats["entities_skipped"] = len(entity_ids) - self.MAX_ENTITIES_FOR_PAIRING
                entity_ids = entity_ids[: self.MAX_ENTITIES_FOR_PAIRING]

            now_iso = datetime.now(timezone.utc).isoformat()

            # Обрабатываем все пары с порогом совместного упоминания
            for id1, id2 in combinations(entity_ids, 2):
                try:
                    props = await self._get_edge_props(id1, id2, "RELATED_TO")

                    if props is None:
                        # Первое со-упоминание → слабое (provisional) ребро.
                        # Оно НЕ считается реальной связью и истлеет, если повтора
                        # в других встречах не будет (temporal_decay).
                        ok = await self.graph.create_relationship(
                            from_id=id1,
                            to_id=id2,
                            rel_type="RELATED_TO",
                            properties={
                                "weight": self.PROVISIONAL_WEIGHT,
                                "co_occurrences": 1,
                                "provisional": True,
                                "created_by": "hebbian_learning",
                                "context": f"meeting:{meeting_id}",
                                "last_strengthened": now_iso,
                            },
                        )
                        if ok:
                            stats["provisional_created"] += 1
                        else:
                            stats["errors"] += 1
                        continue

                    weight = props.get("weight", 1.0) or 0.0
                    is_provisional = bool(props.get("provisional")) or weight < self.PROMOTION_WEIGHT

                    if is_provisional:
                        # Повторное со-упоминание (в другой встрече) → промотируем
                        # связь до реальной. Только теперь это «настоящее» ребро.
                        ok = await self.graph.update_edge_weight(
                            from_id=id1,
                            to_id=id2,
                            rel_type="RELATED_TO",
                            set_weight=self.PROMOTED_WEIGHT,
                            increment_co_occurrences=True,
                        )
                        if ok:
                            await self._clear_provisional_flag(id1, id2, "RELATED_TO")
                            stats["promoted"] += 1
                            stats["new_connections"] += 1
                            stats["connections_strengthened"] += 1
                        else:
                            stats["errors"] += 1
                    else:
                        # Уже реальная связь → обычное усиление по Hebbian.
                        success = await self.strengthen_connection(
                            entity1_id=id1,
                            entity2_id=id2,
                            rel_type="RELATED_TO",
                            context=f"meeting:{meeting_id}",
                        )
                        if success:
                            stats["connections_strengthened"] += 1
                        else:
                            stats["errors"] += 1

                except Exception as e:
                    logger.warning(f"Failed to strengthen {id1}-{id2}: {e}")
                    stats["errors"] += 1

            logger.info(
                f"✅ Hebbian processed meeting {meeting_id}: "
                f"{stats['connections_strengthened']} strengthened "
                f"({stats['promoted']} promoted to real), "
                f"{stats['provisional_created']} provisional (single co-mention)"
            )

            return stats

        except Exception as e:
            logger.error(f"❌ Failed to process meeting cooccurrences: {e}")
            stats["errors"] += 1
            return stats

    async def apply_temporal_decay(
        self,
        decay_rate: float | None = None,
        min_weight: float | None = None,
        older_than_days: int = 7
    ) -> Dict[str, Any]:
        """
        Применить типо-зависимое временное затухание к связям.

        ВАЖНО (guardrail C): затуханию подвергаются ТОЛЬКО контекстные
        co-mention рёбра (created_by=hebbian_learning). Структурные/смысловые
        связи (DECIDED, ASSIGNED_TO, PROPOSED, HAS_PROFILE, SUPERSEDES, ...)
        НЕ затухают — иначе оргструктура и история решений деградируют.

        Разные окна для разных стадий co-mention ребра:
        - provisional (weight < PROMOTION_WEIGHT): удаляется, если не подтверждено
          повтором за PROVISIONAL_TTL_DAYS дней (подчистка для guardrail A);
        - реальное: затухает только если не трогали REAL_DECAY_DAYS дней —
          длинное окно бережёт редкие, но стратегические связи.

        Args:
            decay_rate: Скорость затухания (0.0 - 1.0)
            min_weight: Минимальный вес (связи ниже удаляются)
            older_than_days: (deprecated) сохранён для обратной совместимости;
                реальные окна берутся из PROVISIONAL_TTL_DAYS / REAL_DECAY_DAYS

        Returns:
            Статистика затухания
        """
        stats = {
            "edges_processed": 0,
            "edges_decayed": 0,
            "edges_removed": 0,
            "edges_skipped_structural": 0,
            "errors": 0,
        }

        try:
            from backend.core.config import flags
            if not flags.enable_temporal_decay:
                return stats

            rate = decay_rate if decay_rate is not None else self.DECAY_RATE
            min_w = min_weight if min_weight is not None else self.MIN_WEIGHT
            now = datetime.now(timezone.utc)
            prov_cutoff = now - timedelta(days=self.PROVISIONAL_TTL_DAYS)
            real_cutoff = now - timedelta(days=self.REAL_DECAY_DAYS)

            if not self.graph.use_networkx or not self.graph.nx_graph:
                # Neo4j (живой бэкенд): безопасная ветка — удаляем только
                # устаревшие provisional co-mention рёбра, реальные не трогаем.
                return await self._apply_temporal_decay_neo4j(stats, prov_cutoff)

            edges_to_remove = []

            for source, target, key, attrs in self.graph.nx_graph.edges(keys=True, data=True):
                # Guardrail: трогаем только co-mention рёбра, остальное пропускаем
                if attrs.get("created_by") != self.DECAYABLE_CREATED_BY:
                    stats["edges_skipped_structural"] += 1
                    continue

                stats["edges_processed"] += 1

                current_weight = attrs.get("weight", 1.0) or 0.0
                is_provisional = bool(attrs.get("provisional")) or current_weight < self.PROMOTION_WEIGHT
                cutoff_date = prov_cutoff if is_provisional else real_cutoff

                # Дата последнего усиления; если недавно — не трогаем
                ref_ts = attrs.get("last_strengthened") or attrs.get("_created_at") or attrs.get("known_from")
                if ref_ts:
                    try:
                        last_date = datetime.fromisoformat(str(ref_ts).replace("Z", "+00:00"))
                        if last_date.tzinfo is None:
                            last_date = last_date.replace(tzinfo=timezone.utc)
                        if last_date > cutoff_date:
                            continue  # ещё в пределах окна
                    except (ValueError, TypeError):
                        pass

                if is_provisional:
                    # Не подтверждённое повтором слабое ребро вышло за TTL → удаляем
                    edges_to_remove.append((source, target, key))
                    stats["edges_removed"] += 1
                    continue

                new_weight = current_weight * (1 - rate)
                if new_weight < min_w:
                    edges_to_remove.append((source, target, key))
                    stats["edges_removed"] += 1
                else:
                    attrs["weight"] = new_weight
                    stats["edges_decayed"] += 1

            # Удаляем слабые связи
            for source, target, key in edges_to_remove:
                self.graph.nx_graph.remove_edge(source, target, key)

            logger.info(
                f"✅ Temporal decay applied: "
                f"{stats['edges_decayed']} decayed, "
                f"{stats['edges_removed']} removed, "
                f"{stats['edges_skipped_structural']} structural skipped"
            )

            return stats

        except Exception as e:
            logger.error(f"❌ Failed to apply temporal decay: {e}")
            stats["errors"] += 1
            return stats

    async def _apply_temporal_decay_neo4j(
        self,
        stats: Dict[str, Any],
        prov_cutoff: datetime,
    ) -> Dict[str, Any]:
        """Безопасная подчистка provisional co-mention рёбер в Neo4j.

        Удаляет только слабые (weight < PROMOTION_WEIGHT) рёбра, созданные
        hebbian и не подтверждённые повтором за PROVISIONAL_TTL_DAYS дней.
        Реальные связи в Neo4j НЕ затухают — риск рецентности структурно исключён.
        Сравнение по created_at/known_from (ISO-строки) лексикографически
        корректно для ISO-8601.
        """
        try:
            cutoff_iso = prov_cutoff.isoformat()
            async with self.graph.driver.session() as session:
                query = f"""
                MATCH (a)-[r:RELATED_TO]->(b)
                WHERE r.created_by = $created_by
                  AND coalesce(r.weight, 1.0) < $promotion_weight
                  AND coalesce(toString(r.last_strengthened), r.created_at, r.known_from, '') < $cutoff
                DELETE r
                RETURN count(r) AS removed
                """
                result = await session.run(query, {
                    "created_by": self.DECAYABLE_CREATED_BY,
                    "promotion_weight": self.PROMOTION_WEIGHT,
                    "cutoff": cutoff_iso,
                })
                record = await result.single()
                removed = record.get("removed", 0) if record else 0
                stats["edges_removed"] = removed
                stats["edges_processed"] = removed
                logger.info(
                    f"✅ Temporal decay (Neo4j): {removed} stale provisional "
                    f"co-mention edges removed (real edges untouched)"
                )
            return stats
        except Exception as e:
            logger.error(f"❌ Neo4j temporal decay failed: {e}")
            stats["errors"] += 1
            return stats

    async def _get_edge_weight(
        self,
        from_id: str,
        to_id: str,
        rel_type: str
    ) -> Optional[float]:
        """Получить текущий вес связи."""
        try:
            if self.graph.use_networkx:
                if not self.graph.nx_graph.has_edge(from_id, to_id):
                    return None

                for _key, attrs in self.graph.nx_graph[from_id][to_id].items():
                    if attrs.get("_type") == rel_type:
                        return attrs.get("weight", 1.0)

                return None
            else:
                # Neo4j
                async with self.graph.driver.session() as session:
                    query = f"""
                    MATCH (a)-[r:{rel_type}]->(b)
                    WHERE a.id = $from_id AND b.id = $to_id
                    RETURN r.weight as weight
                    """
                    result = await session.run(query, {"from_id": from_id, "to_id": to_id})
                    record = await result.single()
                    if record:
                        return record.get("weight", 1.0)
                    return None

        except Exception as e:
            logger.error(f"Failed to get edge weight: {e}")
            return None

    async def _get_edge_props(
        self,
        from_id: str,
        to_id: str,
        rel_type: str
    ) -> Optional[Dict[str, Any]]:
        """Получить свойства ребра (weight, co_occurrences, provisional, ...).

        Возвращает None, если ребра нужного типа нет.
        """
        try:
            if self.graph.use_networkx:
                if not self.graph.nx_graph.has_edge(from_id, to_id):
                    return None
                for _key, attrs in self.graph.nx_graph[from_id][to_id].items():
                    if attrs.get("_type") == rel_type:
                        return dict(attrs)
                return None
            else:
                # Neo4j
                async with self.graph.driver.session() as session:
                    query = f"""
                    MATCH (a)-[r:{rel_type}]->(b)
                    WHERE a.id = $from_id AND b.id = $to_id
                    RETURN r.weight AS weight,
                           r.co_occurrences AS co_occurrences,
                           r.provisional AS provisional
                    """
                    result = await session.run(query, {"from_id": from_id, "to_id": to_id})
                    record = await result.single()
                    if record:
                        return {
                            "weight": record.get("weight", 1.0),
                            "co_occurrences": record.get("co_occurrences", 1),
                            "provisional": record.get("provisional", False),
                        }
                    return None
        except Exception as e:
            logger.error(f"Failed to get edge props: {e}")
            return None

    async def _clear_provisional_flag(
        self,
        from_id: str,
        to_id: str,
        rel_type: str
    ) -> bool:
        """Снять флаг provisional с ребра (связь стала реальной)."""
        try:
            if self.graph.use_networkx:
                if not self.graph.nx_graph.has_edge(from_id, to_id):
                    return False
                for _key, attrs in self.graph.nx_graph[from_id][to_id].items():
                    if attrs.get("_type") == rel_type:
                        attrs["provisional"] = False
                        return True
                return False
            else:
                async with self.graph.driver.session() as session:
                    query = f"""
                    MATCH (a)-[r:{rel_type}]->(b)
                    WHERE a.id = $from_id AND b.id = $to_id
                    SET r.provisional = false
                    RETURN r
                    """
                    result = await session.run(query, {"from_id": from_id, "to_id": to_id})
                    return await result.single() is not None
        except Exception as e:
            logger.error(f"Failed to clear provisional flag: {e}")
            return False

    def get_strongly_connected_entities(
        self,
        entity_id: str,
        min_weight: float = 2.0,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Получить сильно связанные сущности.

        Args:
            entity_id: ID сущности
            min_weight: Минимальный вес связи
            limit: Максимальное количество

        Returns:
            Список связанных сущностей с весами
        """
        if not self.graph.use_networkx or not self.graph.nx_graph:
            return []

        connected = []

        # Исходящие связи
        if self.graph.nx_graph.has_node(entity_id):
            for _, target, attrs in self.graph.nx_graph.out_edges(entity_id, data=True):
                weight = attrs.get("weight", 1.0)
                if weight >= min_weight:
                    target_attrs = self.graph.nx_graph.nodes.get(target, {})
                    connected.append({
                        "id": target,
                        "name": target_attrs.get("name", ""),
                        "weight": weight,
                        "co_occurrences": attrs.get("co_occurrences", 1),
                        "direction": "out",
                    })

            # Входящие связи
            for source, _, attrs in self.graph.nx_graph.in_edges(entity_id, data=True):
                weight = attrs.get("weight", 1.0)
                if weight >= min_weight:
                    source_attrs = self.graph.nx_graph.nodes.get(source, {})
                    connected.append({
                        "id": source,
                        "name": source_attrs.get("name", ""),
                        "weight": weight,
                        "co_occurrences": attrs.get("co_occurrences", 1),
                        "direction": "in",
                    })

        # Сортируем по весу и возвращаем top N
        connected.sort(key=lambda x: x["weight"], reverse=True)
        return connected[:limit]


# Singleton
_hebbian_instance: Optional[HebbianLearning] = None


def get_hebbian_learning(graph_builder) -> HebbianLearning:
    """Получить экземпляр HebbianLearning."""
    global _hebbian_instance
    if _hebbian_instance is None or _hebbian_instance.graph != graph_builder:
        _hebbian_instance = HebbianLearning(graph_builder)
    return _hebbian_instance

