# -*- coding: utf-8 -*-
"""
Two-Phase Corrections Manager - Двухфазная система исправлений.

Реализует паттерн "День/Ночь":
- ФАЗА 1 (ДЕНЬ): Сбор исправлений в буфер
- ФАЗА 2 (НОЧЬ): Консолидация и применение

Преимущества:
- Не ломает граф при неправильных исправлениях
- Позволяет отменить исправления до применения
- Группирует связанные исправления
- Разрешает противоречия

Использование:
```python
from backend.core.corrections.two_phase_manager import TwoPhaseManager

manager = TwoPhaseManager(graph_builder)

# ФАЗА 1: Записать исправление (днём)
await manager.record_correction(
    entity_id="entity_123",
    field="name",
    old_value="Старое имя",
    new_value="Новое имя",
    reason="Опечатка",
    user_id="user_1"
)

# ФАЗА 2: Применить все исправления (ночью)
result = await manager.apply_pending_corrections()
```
"""
import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class CorrectionStatus(Enum):
    """Статус исправления."""
    PENDING = "pending"           # Ожидает применения
    APPROVED = "approved"         # Одобрено (готово к применению)
    APPLIED = "applied"           # Применено
    REJECTED = "rejected"         # Отклонено
    CONFLICTED = "conflicted"     # Конфликт с другим исправлением


class CorrectionType(Enum):
    """Тип исправления."""
    UPDATE = "update"             # Обновление значения
    DELETE = "delete"             # Удаление сущности
    MERGE = "merge"               # Объединение сущностей
    SPLIT = "split"               # Разделение сущности
    RECLASSIFY = "reclassify"     # Изменение типа


@dataclass
class Correction:
    """Исправление данных."""
    id: str
    entity_id: str
    entity_type: str
    correction_type: str  # CorrectionType value

    # Что меняем
    field: str
    old_value: Any
    new_value: Any

    # Метаданные
    reason: str
    user_id: str
    created_at: str
    status: str = "pending"  # CorrectionStatus value

    # Дополнительно
    priority: int = 1  # 1-5, где 5 - критичное
    source: str = "user"  # user, system, auto
    related_corrections: List[str] = field(default_factory=list)

    # Результат применения
    applied_at: Optional[str] = None
    applied_by: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Correction":
        return cls(**data)


@dataclass
class ConflictResolution:
    """Стратегия разрешения конфликта."""
    correction_ids: List[str]
    resolution: str  # "keep_first", "keep_last", "merge", "manual"
    resolved_value: Any
    resolved_by: str
    resolved_at: str


class TwoPhaseManager:
    """
    Менеджер двухфазных исправлений.

    ФАЗА 1 (ДЕНЬ):
    - Пользователи вносят исправления
    - Исправления сохраняются в буфер
    - Проверяются на конфликты

    ФАЗА 2 (НОЧЬ):
    - Группировка связанных исправлений
    - Разрешение конфликтов
    - Применение к графу
    - Обновление снапшотов
    """

    def __init__(
        self,
        graph_builder=None,
        storage_path: str = "data/corrections",
        user_id: Optional[str] = None
    ):
        self.graph = graph_builder
        self.user_id = user_id  # для синка supersede/векторов после apply
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)

        # Буфер исправлений
        self._pending: Dict[str, Correction] = {}
        self._applied: List[Correction] = []
        self._conflicts: List[ConflictResolution] = []

        # Загружаем pending исправления
        self._load_pending()

    def _load_pending(self):
        """Загрузить pending исправления из файла."""
        pending_file = self.storage_path / "pending.json"
        if pending_file.exists():
            try:
                with open(pending_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for item in data:
                        correction = Correction.from_dict(item)
                        self._pending[correction.id] = correction
                logger.info(f"Loaded {len(self._pending)} pending corrections")
            except Exception as e:
                logger.error(f"Failed to load pending corrections: {e}")

    def _save_pending(self):
        """Сохранить pending исправления."""
        pending_file = self.storage_path / "pending.json"
        try:
            with open(pending_file, "w", encoding="utf-8") as f:
                json.dump(
                    [c.to_dict() for c in self._pending.values()],
                    f, ensure_ascii=False, indent=2
                )
        except Exception as e:
            logger.error(f"Failed to save pending corrections: {e}")

    def _save_applied(self):
        """Сохранить историю применённых исправлений."""
        history_file = self.storage_path / "history.json"
        try:
            # Загружаем существующую историю
            existing = []
            if history_file.exists():
                with open(history_file, "r", encoding="utf-8") as f:
                    existing = json.load(f)

            # Добавляем новые
            existing.extend([c.to_dict() for c in self._applied])

            # Ограничиваем размер (последние 1000)
            if len(existing) > 1000:
                existing = existing[-1000:]

            with open(history_file, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)

            self._applied = []

        except Exception as e:
            logger.error(f"Failed to save applied corrections: {e}")

    def _generate_correction_id(self, entity_id: str, field: str) -> str:
        """Генерация ID исправления."""
        raw = f"{entity_id}:{field}:{datetime.now().timestamp()}"
        return hashlib.md5(raw.encode()).hexdigest()[:12]

    def is_enabled(self) -> bool:
        """Проверить включена ли система."""
        try:
            from backend.core.config import flags
            return flags.enable_two_phase_corrections
        except ImportError:
            return False

    async def record_correction(
        self,
        entity_id: str,
        field: str,
        old_value: Any,
        new_value: Any,
        reason: str,
        user_id: str,
        entity_type: str = "unknown",
        correction_type: str = "update",
        priority: int = 1,
        source: str = "user"
    ) -> Dict[str, Any]:
        """
        Записать исправление в буфер (ФАЗА 1).

        Args:
            entity_id: ID сущности
            field: Поле для исправления
            old_value: Старое значение
            new_value: Новое значение
            reason: Причина исправления
            user_id: ID пользователя
            entity_type: Тип сущности
            correction_type: Тип исправления
            priority: Приоритет (1-5)
            source: Источник (user/system/auto)

        Returns:
            Результат с ID исправления
        """
        result = {
            "success": False,
            "correction_id": None,
            "conflicts": [],
        }

        if not self.is_enabled():
            # Если система отключена, применяем сразу
            return await self._apply_immediately(
                entity_id, field, old_value, new_value, reason, user_id
            )

        correction_id = self._generate_correction_id(entity_id, field)

        correction = Correction(
            id=correction_id,
            entity_id=entity_id,
            entity_type=entity_type,
            correction_type=correction_type,
            field=field,
            old_value=old_value,
            new_value=new_value,
            reason=reason,
            user_id=user_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            priority=priority,
            source=source,
        )

        # Проверяем конфликты
        conflicts = self._check_conflicts(correction)
        if conflicts:
            correction.status = CorrectionStatus.CONFLICTED.value
            result["conflicts"] = [c.id for c in conflicts]

        # Сохраняем
        self._pending[correction_id] = correction
        self._save_pending()

        result["success"] = True
        result["correction_id"] = correction_id

        logger.info(
            f"Recorded correction {correction_id}: "
            f"{entity_id}.{field} = {new_value}"
        )

        return result

    def _check_conflicts(self, correction: Correction) -> List[Correction]:
        """Проверить конфликты с существующими исправлениями."""
        conflicts = []

        for existing in self._pending.values():
            if existing.id == correction.id:
                continue

            # Конфликт: то же поле той же сущности
            if (existing.entity_id == correction.entity_id and
                existing.field == correction.field and
                existing.status == CorrectionStatus.PENDING.value):
                conflicts.append(existing)

        return conflicts

    async def _apply_immediately(
        self,
        entity_id: str,
        field: str,
        old_value: Any,
        new_value: Any,
        reason: str,
        user_id: str
    ) -> Dict[str, Any]:
        """Применить исправление немедленно (если система отключена).

        Поддерживает оба бэкенда: NetworkX и Neo4j.
        """
        result = {
            "success": False,
            "correction_id": None,
            "applied": True,
        }

        if not self.graph:
            result["error"] = "No graph available"
            return result

        try:
            # Применяем к графу (универсально для обоих бэкендов)
            updated = await self.graph.update_node(entity_id, {field: new_value})
            if updated:
                await self.graph.save()
                result["success"] = True
            else:
                result["error"] = f"Entity {entity_id} not found"

            return result

        except Exception as e:
            result["error"] = str(e)
            return result

    async def get_pending_corrections(
        self,
        entity_id: Optional[str] = None,
        user_id: Optional[str] = None,
        status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Получить список pending исправлений.

        Args:
            entity_id: Фильтр по сущности
            user_id: Фильтр по пользователю
            status: Фильтр по статусу

        Returns:
            Список исправлений
        """
        result = []

        for correction in self._pending.values():
            if entity_id and correction.entity_id != entity_id:
                continue
            if user_id and correction.user_id != user_id:
                continue
            if status and correction.status != status:
                continue

            result.append(correction.to_dict())

        # Сортируем по приоритету и дате
        result.sort(key=lambda x: (-x["priority"], x["created_at"]))

        return result

    async def approve_correction(
        self,
        correction_id: str,
        approved_by: str
    ) -> bool:
        """Одобрить исправление."""
        if correction_id not in self._pending:
            return False

        self._pending[correction_id].status = CorrectionStatus.APPROVED.value
        self._save_pending()

        logger.info(f"Correction {correction_id} approved by {approved_by}")
        return True

    async def reject_correction(
        self,
        correction_id: str,
        rejected_by: str,
        reason: str = ""
    ) -> bool:
        """Отклонить исправление."""
        if correction_id not in self._pending:
            return False

        correction = self._pending[correction_id]
        correction.status = CorrectionStatus.REJECTED.value
        correction.error = reason

        # Перемещаем в историю
        self._applied.append(correction)
        del self._pending[correction_id]

        self._save_pending()
        self._save_applied()

        logger.info(f"Correction {correction_id} rejected by {rejected_by}: {reason}")
        return True

    async def resolve_conflict(
        self,
        correction_ids: List[str],
        resolution: str,
        resolved_value: Any,
        resolved_by: str
    ) -> bool:
        """
        Разрешить конфликт между исправлениями.

        Args:
            correction_ids: ID конфликтующих исправлений
            resolution: Стратегия ("keep_first", "keep_last", "merge", "manual")
            resolved_value: Итоговое значение
            resolved_by: Кто разрешил
        """
        if not all(cid in self._pending for cid in correction_ids):
            return False

        # Создаём запись о разрешении
        resolution_record = ConflictResolution(
            correction_ids=correction_ids,
            resolution=resolution,
            resolved_value=resolved_value,
            resolved_by=resolved_by,
            resolved_at=datetime.now(timezone.utc).isoformat()
        )
        self._conflicts.append(resolution_record)

        # Применяем стратегию
        if resolution == "keep_first":
            # Оставляем первое, отклоняем остальные
            for i, cid in enumerate(correction_ids):
                if i == 0:
                    self._pending[cid].status = CorrectionStatus.APPROVED.value
                else:
                    self._pending[cid].status = CorrectionStatus.REJECTED.value

        elif resolution == "keep_last":
            # Оставляем последнее
            for i, cid in enumerate(correction_ids):
                if i == len(correction_ids) - 1:
                    self._pending[cid].status = CorrectionStatus.APPROVED.value
                else:
                    self._pending[cid].status = CorrectionStatus.REJECTED.value

        elif resolution == "manual":
            # Используем resolved_value для первого, отклоняем остальные
            for i, cid in enumerate(correction_ids):
                if i == 0:
                    self._pending[cid].new_value = resolved_value
                    self._pending[cid].status = CorrectionStatus.APPROVED.value
                else:
                    self._pending[cid].status = CorrectionStatus.REJECTED.value

        self._save_pending()

        logger.info(f"Conflict resolved: {correction_ids} -> {resolution}")
        return True

    async def apply_pending_corrections(
        self,
        apply_approved_only: bool = True
    ) -> Dict[str, Any]:
        """
        Применить все pending исправления (ФАЗА 2).

        Args:
            apply_approved_only: Применять только одобренные

        Returns:
            Статистика применения
        """
        stats = {
            "total": 0,
            "applied": 0,
            "skipped": 0,
            "failed": 0,
            "errors": [],
        }

        if not self.graph:
            stats["errors"].append("No graph available")
            return stats

        # Собираем исправления для применения
        to_apply = []
        for correction in self._pending.values():
            stats["total"] += 1

            if apply_approved_only:
                if correction.status != CorrectionStatus.APPROVED.value:
                    stats["skipped"] += 1
                    continue
            else:
                if correction.status in (
                    CorrectionStatus.REJECTED.value,
                    CorrectionStatus.APPLIED.value
                ):
                    stats["skipped"] += 1
                    continue

            to_apply.append(correction)

        # Сортируем по приоритету
        to_apply.sort(key=lambda x: -x.priority)

        # Применяем
        applied_now: List[Correction] = []
        for correction in to_apply:
            try:
                success = await self._apply_correction(correction)

                if success:
                    correction.status = CorrectionStatus.APPLIED.value
                    correction.applied_at = datetime.now(timezone.utc).isoformat()
                    correction.applied_by = "system"
                    stats["applied"] += 1

                    # Перемещаем в историю
                    self._applied.append(correction)
                    applied_now.append(correction)
                    del self._pending[correction.id]
                else:
                    stats["failed"] += 1
                    stats["errors"].append(f"{correction.id}: Apply failed")

            except Exception as e:
                stats["failed"] += 1
                correction.error = str(e)
                stats["errors"].append(f"{correction.id}: {e}")

        # Сохраняем
        self._save_pending()
        self._save_applied()

        # Сохраняем граф
        if stats["applied"] > 0:
            await self.graph.save()
            # Аудит: без этого ПОИСК продолжал возвращать старое значение —
            # граф обновлялся, а supersede-история и векторный индекс нет.
            await self._sync_memory_after_apply(applied_now)

        logger.info(
            f"Applied corrections: {stats['applied']}/{stats['total']} "
            f"(skipped: {stats['skipped']}, failed: {stats['failed']})"
        )

        return stats

    async def _sync_memory_after_apply(self, applied: List[Correction]) -> None:
        """Синхронизировать остальную память с применёнными правками (best-effort).

        1) SupersedeStore: новая версия факта `{entity}::{field}` — история
           «было → стало» сохраняется недеструктивно и видна в досье/синтезе.
        2) Векторный индекс: upsert документа-исправления, чтобы семантический
           поиск находил НОВОЕ значение (полный reindex узла не нужен —
           детерминированный id перезаписывает прошлую правку того же факта).
        Ошибки не валят apply: граф уже обновлён и сохранён.
        """
        uid = self.user_id or ""
        for c in applied:
            fact_key = f"{c.entity_id}::{c.field}"
            try:
                if uid:
                    from backend.core.memory.supersede_store import SupersedeStore
                    from backend.core.store.tenant_paths import (
                        supersede_store_path_for_user,
                    )
                    store = SupersedeStore(
                        persist_path=supersede_store_path_for_user(uid))
                    store.record(f"{uid}::{fact_key}", str(c.new_value),
                                 source=f"correction:{c.id}",
                                 as_of=c.applied_at)
            except Exception as e:
                logger.warning(f"corrections: supersede sync failed for {fact_key}: {e}")
            try:
                from backend.core.store.vector_indexer import get_vector_indexer
                indexer = await get_vector_indexer()
                text = (f"ИСПРАВЛЕНИЕ ФАКТА: у «{c.entity_id}» поле «{c.field}» "
                        f"теперь «{c.new_value}» (ранее «{c.old_value}»). "
                        f"Причина: {c.reason or 'правка пользователя'}.")
                await indexer.upsert_vector_public(
                    collection="entities",
                    vector_id=f"correction::{uid}::{fact_key}",
                    text=text,
                    metadata={"type": "memory_correction", "user_id": uid,
                              "entity_id": c.entity_id, "field": c.field,
                              "new_value": str(c.new_value),
                              "applied_at": c.applied_at})
            except Exception as e:
                logger.warning(f"corrections: vector sync failed for {fact_key}: {e}")

    async def _apply_correction(self, correction: Correction) -> bool:
        """Применить одно исправление к графу.

        Поддерживает оба бэкенда: NetworkX и Neo4j.
        """
        entity_id = correction.entity_id
        field = correction.field
        new_value = correction.new_value

        # Проверяем существование узла (универсально для обоих бэкендов)
        node = await self.graph.get_node_by_id(entity_id)

        if not node:
            # Пробуем найти по имени через search_nodes
            try:
                search_results = await self.graph.search_nodes(query=entity_id, limit=1)
                if search_results:
                    entity_id = search_results[0].get("id", entity_id)
                    node = search_results[0]
            except Exception:
                logger.debug("suppressed exception", exc_info=True)

        if not node:
            correction.error = f"Entity {entity_id} not found"
            return False

        # Применяем изменение (через универсальные методы GraphBuilder)
        if correction.correction_type == CorrectionType.UPDATE.value:
            return await self.graph.update_node(entity_id, {field: new_value})

        elif correction.correction_type == CorrectionType.DELETE.value:
            return await self.graph.remove_node(entity_id)

        elif correction.correction_type == CorrectionType.RENAME.value:
            return await self.graph.update_node(entity_id, {"name": new_value})

        # Другие типы пока не реализованы
        return False

    def get_stats(self) -> Dict[str, Any]:
        """Получить статистику."""
        pending_by_status = {}
        for c in self._pending.values():
            status = c.status
            pending_by_status[status] = pending_by_status.get(status, 0) + 1

        return {
            "pending_total": len(self._pending),
            "pending_by_status": pending_by_status,
            "conflicts_resolved": len(self._conflicts),
            "enabled": self.is_enabled(),
        }


# Singleton
_manager_instance: Optional[Dict[str, TwoPhaseManager]] = None


def get_two_phase_manager(graph_builder=None, *,
                          user_id: Optional[str] = None) -> TwoPhaseManager:
    """Получить экземпляр TwoPhaseManager.

    Аудит (мульти-тенант): раньше это был ОДИН глобальный синглтон — он
    запоминал граф ПЕРВОГО пользователя, и правки следующих пользователей
    применялись к чужому графу; буфер pending.json тоже был общим. Теперь
    кэш per-user, буфер — data/corrections/{user_id}/, граф перепривязывается
    на случай пересоздания.
    """
    global _manager_instance
    key = user_id or "__global__"
    if not isinstance(_manager_instance, dict):
        _manager_instance = {}
    mgr = _manager_instance.get(key)
    if mgr is None:
        storage = "data/corrections"
        if user_id:
            try:
                from backend.core.store.tenant_paths import _DATA_ROOT
                storage = str(_DATA_ROOT / "corrections" / user_id)
            except Exception:
                storage = f"data/corrections/{user_id}"
        mgr = TwoPhaseManager(graph_builder, storage_path=storage, user_id=user_id)
        _manager_instance[key] = mgr
    if graph_builder is not None:
        mgr.graph = graph_builder  # свежий граф запроса, не замороженный первый
    return mgr


