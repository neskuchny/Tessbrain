# -*- coding: utf-8 -*-
"""
Natural Language Knowledge Editor - Редактор знаний на естественном языке.

Позволяет пользователю редактировать данные в графе через текстовые команды:
- "Измени должность Ивана Петрова на Senior Developer"
- "Удали проект Alpha"
- "Свяжи Марию с проектом Beta"
- "Объедини сотрудников Иван Иванов и Ivan Ivanov в одного"

Workflow:
1. Пользователь вводит текст
2. IntentParser анализирует намерение
3. NLKnowledgeEditor ищет сущности в графе
4. Показывает пользователю что будет изменено
5. После подтверждения применяет изменения
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from .intent_parser import EditAction, EditIntent, IntentParser

logger = logging.getLogger(__name__)


class EditStatus(Enum):
    """Статус операции редактирования."""
    PENDING_SEARCH = "pending_search"       # Ищем сущности
    PENDING_CLARIFICATION = "pending_clarification"  # Нужно уточнение
    PENDING_CONFIRMATION = "pending_confirmation"    # Ждем подтверждения
    CONFIRMED = "confirmed"                 # Подтверждено, готово к применению
    APPLIED = "applied"                     # Применено
    CANCELLED = "cancelled"                 # Отменено
    FAILED = "failed"                       # Ошибка


@dataclass
class EditOperation:
    """Операция редактирования."""
    id: str
    user_id: str
    intent: EditIntent
    status: EditStatus

    # Найденные сущности
    found_entities: List[Dict[str, Any]] = field(default_factory=list)
    selected_entity: Optional[Dict[str, Any]] = None

    # Предпросмотр изменений
    preview: Optional[Dict[str, Any]] = None

    # Результат
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    # Временные метки
    created_at: str = ""
    confirmed_at: Optional[str] = None
    applied_at: Optional[str] = None

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "intent": self.intent.to_dict(),
            "status": self.status.value,
            "found_entities": self.found_entities,
            "selected_entity": self.selected_entity,
            "preview": self.preview,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "confirmed_at": self.confirmed_at,
            "applied_at": self.applied_at,
        }


class NLKnowledgeEditor:
    """
    Редактор знаний на естественном языке.

    Пример использования:
    ```python
    editor = NLKnowledgeEditor(graph_builder, vector_indexer)

    # Шаг 1: Анализ запроса
    operation = await editor.process_request(
        user_id="user_123",
        text="Измени должность Ивана на Senior Developer"
    )

    # Шаг 2: Если нужно выбрать из нескольких
    if operation.status == EditStatus.PENDING_CLARIFICATION:
        logger.info(operation.intent.clarification_needed)
        # Пользователь уточняет...

    # Шаг 3: Показываем предпросмотр
    if operation.status == EditStatus.PENDING_CONFIRMATION:
        logger.info(operation.preview)
        # "Изменить: Иван Петров.role: Developer -> Senior Developer"

    # Шаг 4: Подтверждение и применение
    result = await editor.confirm_and_apply(operation.id)
    ```
    """

    def __init__(
        self,
        graph_builder=None,
        vector_indexer=None,
        corrections_manager=None,
        llm_router=None,
    ):
        self.graph = graph_builder
        self.vector = vector_indexer
        self.corrections = corrections_manager

        # Intent parser
        self.intent_parser = IntentParser(llm_router)

        # Активные операции (in-memory, можно вынести в Redis/DB)
        self._operations: Dict[str, EditOperation] = {}

        # Счетчик для ID операций
        self._op_counter = 0

    def _generate_operation_id(self) -> str:
        """Генерирует уникальный ID операции."""
        self._op_counter += 1
        return f"edit_{datetime.now().strftime('%Y%m%d%H%M%S')}_{self._op_counter}"

    async def process_request(
        self,
        user_id: str,
        text: str,
        context: Optional[Dict[str, Any]] = None
    ) -> EditOperation:
        """
        Обрабатывает запрос пользователя на редактирование.

        Args:
            user_id: ID пользователя
            text: Текст запроса
            context: Дополнительный контекст

        Returns:
            EditOperation с результатом анализа
        """
        logger.info(f"Processing edit request from {user_id}: {text[:100]}...")

        # 1. Парсим намерение
        intent = await self.intent_parser.parse(text, context)

        # 2. Создаем операцию
        operation = EditOperation(
            id=self._generate_operation_id(),
            user_id=user_id,
            intent=intent,
            status=EditStatus.PENDING_SEARCH,
        )

        # 3. Если нужно уточнение - сразу возвращаем
        if intent.clarification_needed:
            operation.status = EditStatus.PENDING_CLARIFICATION
            self._operations[operation.id] = operation
            return operation

        # 4. Ищем сущности в графе
        if intent.entity_name:
            found = await self._search_entities(
                name=intent.entity_name,
                entity_type=intent.entity_type,
                user_id=user_id,
            )
            operation.found_entities = found
            intent.found_entities = found

            if not found:
                operation.status = EditStatus.PENDING_CLARIFICATION
                operation.intent.clarification_needed = (
                    f"Не нашёл сущность '{intent.entity_name}' в базе знаний. "
                    f"Уточните имя или проверьте написание."
                )
            elif len(found) == 1:
                # Одна сущность - выбираем её
                operation.selected_entity = found[0]
                operation.status = EditStatus.PENDING_CONFIRMATION
                operation.preview = await self._generate_preview(operation)
            else:
                # Несколько - нужен выбор
                operation.status = EditStatus.PENDING_CLARIFICATION
                entities_list = "\n".join([
                    f"  {i+1}. {e.get('name', 'Без имени')} ({e.get('type', '?')}) - {e.get('description', '')[:50]}"
                    for i, e in enumerate(found[:5])
                ])
                operation.intent.clarification_needed = (
                    f"Найдено несколько сущностей с похожим именем:\n{entities_list}\n\n"
                    f"Укажите номер или уточните имя."
                )
        else:
            # Нет имени сущности
            operation.status = EditStatus.PENDING_CLARIFICATION
            operation.intent.clarification_needed = (
                "Укажите, какую именно сущность вы хотите изменить."
            )

        # Сохраняем операцию
        self._operations[operation.id] = operation
        return operation

    async def select_entity(
        self,
        operation_id: str,
        selection: int
    ) -> EditOperation:
        """
        Выбрать сущность из списка найденных.

        Args:
            operation_id: ID операции
            selection: Номер сущности (1-based)
        """
        operation = self._operations.get(operation_id)
        if not operation:
            raise ValueError(f"Operation {operation_id} not found")

        if selection < 1 or selection > len(operation.found_entities):
            raise ValueError(f"Invalid selection: {selection}")

        operation.selected_entity = operation.found_entities[selection - 1]
        operation.status = EditStatus.PENDING_CONFIRMATION
        operation.preview = await self._generate_preview(operation)

        return operation

    async def clarify(
        self,
        operation_id: str,
        clarification_text: str
    ) -> EditOperation:
        """
        Уточнить запрос.

        Args:
            operation_id: ID операции
            clarification_text: Текст уточнения
        """
        operation = self._operations.get(operation_id)
        if not operation:
            raise ValueError(f"Operation {operation_id} not found")

        # Проверяем, это номер выбора или новый текст
        try:
            selection = int(clarification_text.strip())
            return await self.select_entity(operation_id, selection)
        except ValueError:
            logger.debug("suppressed exception", exc_info=True)

        # Это новый текст - парсим заново с контекстом предыдущего запроса
        context = {
            "previous_intent": operation.intent.to_dict(),
            "found_entities": operation.found_entities,
        }

        new_intent = await self.intent_parser.parse(clarification_text, context)

        # Объединяем с предыдущим намерением
        if not new_intent.entity_name and operation.intent.entity_name:
            new_intent.entity_name = operation.intent.entity_name
        if not new_intent.entity_type and operation.intent.entity_type:
            new_intent.entity_type = operation.intent.entity_type
        if not new_intent.target_field and operation.intent.target_field:
            new_intent.target_field = operation.intent.target_field
        if new_intent.action == EditAction.UNKNOWN:
            new_intent.action = operation.intent.action

        operation.intent = new_intent

        # Повторяем поиск если нужно
        if new_intent.entity_name:
            found = await self._search_entities(
                name=new_intent.entity_name,
                entity_type=new_intent.entity_type,
                user_id=operation.user_id,
            )
            operation.found_entities = found

            if len(found) == 1:
                operation.selected_entity = found[0]
                operation.status = EditStatus.PENDING_CONFIRMATION
                operation.preview = await self._generate_preview(operation)
            elif len(found) > 1:
                entities_list = "\n".join([
                    f"  {i+1}. {e.get('name', 'Без имени')} ({e.get('type', '?')})"
                    for i, e in enumerate(found[:5])
                ])
                operation.intent.clarification_needed = (
                    f"Найдено несколько:\n{entities_list}\n\nУкажите номер."
                )
            else:
                operation.intent.clarification_needed = (
                    f"Не нашёл '{new_intent.entity_name}'. Попробуйте другое имя."
                )

        return operation

    async def confirm_and_apply(
        self,
        operation_id: str,
        confirmed: bool = True
    ) -> EditOperation:
        """
        Подтвердить и применить изменения.

        Args:
            operation_id: ID операции
            confirmed: True - применить, False - отменить
        """
        operation = self._operations.get(operation_id)
        if not operation:
            raise ValueError(f"Operation {operation_id} not found")

        if not confirmed:
            operation.status = EditStatus.CANCELLED
            return operation

        if operation.status != EditStatus.PENDING_CONFIRMATION:
            raise ValueError(f"Operation not ready for confirmation: {operation.status}")

        operation.confirmed_at = datetime.now(timezone.utc).isoformat()
        operation.status = EditStatus.CONFIRMED

        try:
            # Применяем изменения
            result = await self._apply_changes(operation)
            operation.result = result
            operation.status = EditStatus.APPLIED
            operation.applied_at = datetime.now(timezone.utc).isoformat()

            logger.info(f"Edit operation {operation_id} applied successfully")

        except Exception as e:
            operation.status = EditStatus.FAILED
            operation.error = str(e)
            logger.error(f"Edit operation {operation_id} failed: {e}")

        return operation

    async def _search_entities(
        self,
        name: str,
        entity_type: Optional[str],
        user_id: str,
    ) -> List[Dict[str, Any]]:
        """Поиск сущностей в графе и векторном хранилище."""
        results = []

        # 1. Поиск в графе по имени. tenant_id ОБЯЗАТЕЛЕН: узлы проштампованы
        # tenant_id=user_id, а search_nodes без него на Neo4j видит только
        # legacy(NULL) → «Не нашёл сущность 'Ринат'», хотя Ринат в графе есть
        # (та же бага, что была в Объект 360).
        if self.graph:
            try:
                graph_results = await self.graph.search_nodes(
                    query=name,
                    node_type=entity_type,
                    limit=10,
                    tenant_id=user_id or None,
                )
                results.extend(graph_results)
            except TypeError:
                # старая сигнатура search_nodes без tenant_id (NetworkX-путь)
                try:
                    graph_results = await self.graph.search_nodes(
                        query=name, node_type=entity_type, limit=10)
                    results.extend(graph_results)
                except Exception as e:
                    logger.warning(f"Graph search error: {e}")
            except Exception as e:
                logger.warning(f"Graph search error: {e}")

        # 2. Семантический поиск
        if self.vector and len(results) < 5:
            try:
                # VectorIndexer уже namespaced на юзера; сигнатура —
                # (query, collections, limit, score_threshold, filter_conditions).
                # Раньше передавались filter_type/namespace → TypeError, и
                # семантический фолбэк не работал вообще.
                vector_results = await self.vector.search(
                    query=name,
                    limit=10,
                    score_threshold=0.4,
                )
                # Добавляем уникальные результаты
                existing_ids = {r.get("id") for r in results}
                for vr in vector_results:
                    if vr.get("id") not in existing_ids:
                        results.append(vr)
            except Exception as e:
                logger.warning(f"Vector search error: {e}")

        # Сортируем по релевантности
        results.sort(key=lambda x: x.get("score", 0), reverse=True)

        return results[:10]

    async def _generate_preview(self, operation: EditOperation) -> Dict[str, Any]:
        """Генерирует предпросмотр изменений."""
        intent = operation.intent
        entity = operation.selected_entity

        if not entity:
            return {"error": "No entity selected"}

        preview = {
            "action": intent.action.value,
            "entity": {
                "id": entity.get("id"),
                "name": entity.get("name"),
                "type": entity.get("type"),
            },
            "changes": [],
            "human_readable": "",
        }

        if intent.action == EditAction.UPDATE:
            old_value = entity.get(intent.target_field, "не задано") if intent.target_field else None
            preview["changes"].append({
                "field": intent.target_field,
                "old_value": old_value,
                "new_value": intent.new_value,
            })
            preview["human_readable"] = (
                f"Изменить {entity.get('name', 'сущность')}.{intent.target_field}: "
                f"'{old_value}' → '{intent.new_value}'"
            )

        elif intent.action == EditAction.DELETE:
            preview["human_readable"] = (
                f"Удалить {entity.get('type', 'сущность')}: {entity.get('name', 'без имени')}"
            )

        elif intent.action == EditAction.RENAME:
            preview["changes"].append({
                "field": "name",
                "old_value": entity.get("name"),
                "new_value": intent.new_value,
            })
            preview["human_readable"] = (
                f"Переименовать: '{entity.get('name')}' → '{intent.new_value}'"
            )

        elif intent.action == EditAction.LINK:
            preview["target"] = {
                "name": intent.target_entity_name,
                "type": intent.target_entity_type,
            }
            preview["relationship"] = intent.relationship_type or "связан_с"
            preview["human_readable"] = (
                f"Создать связь: {entity.get('name')} → "
                f"[{intent.relationship_type or 'связан_с'}] → {intent.target_entity_name}"
            )

        elif intent.action == EditAction.UNLINK:
            preview["target"] = {
                "name": intent.target_entity_name,
                "type": intent.target_entity_type,
            }
            preview["human_readable"] = (
                f"Удалить связь: {entity.get('name')} ✂ {intent.target_entity_name}"
            )

        elif intent.action == EditAction.MERGE:
            preview["target"] = {
                "name": intent.target_entity_name,
                "type": intent.target_entity_type,
            }
            preview["human_readable"] = (
                f"Объединить: {entity.get('name')} + {intent.target_entity_name}"
            )

        elif intent.action == EditAction.RECLASSIFY:
            preview["changes"].append({
                "field": "type",
                "old_value": entity.get("type"),
                "new_value": intent.new_value,
            })
            preview["human_readable"] = (
                f"Изменить тип: {entity.get('name')} ({entity.get('type')} → {intent.new_value})"
            )

        return preview

    async def _apply_changes(self, operation: EditOperation) -> Dict[str, Any]:
        """Применяет изменения к графу."""
        intent = operation.intent
        entity = operation.selected_entity

        if not entity:
            raise ValueError("No entity selected")

        entity_id = entity.get("id")
        result = {"success": False, "changes": []}

        # Используем систему коррекций для отслеживания изменений
        if self.corrections:
            correction_id = await self.corrections.record_correction(
                entity_id=entity_id,
                entity_type=entity.get("type", "unknown"),
                field=intent.target_field or "entity",
                old_value=entity.get(intent.target_field) if intent.target_field else entity,
                new_value=intent.new_value,
                reason=intent.reason or f"NL Edit: {intent.original_text[:100]}",
                user_id=operation.user_id,
                correction_type=intent.action.value,
            )
            result["correction_id"] = correction_id

        # Применяем изменения напрямую к графу
        if self.graph:
            if intent.action == EditAction.UPDATE:
                await self.graph.update_node(entity_id, {intent.target_field: intent.new_value})
                result["changes"].append({
                    "type": "update",
                    "entity_id": entity_id,
                    "field": intent.target_field,
                    "new_value": intent.new_value,
                })

            elif intent.action == EditAction.DELETE:
                await self.graph.remove_node(entity_id)
                result["changes"].append({
                    "type": "delete",
                    "entity_id": entity_id,
                })

            elif intent.action == EditAction.RENAME:
                await self.graph.update_node(entity_id, {"name": intent.new_value})
                result["changes"].append({
                    "type": "rename",
                    "entity_id": entity_id,
                    "new_name": intent.new_value,
                })

            elif intent.action == EditAction.LINK:
                # Найти целевую сущность
                target_entities = await self._search_entities(
                    name=intent.target_entity_name,
                    entity_type=intent.target_entity_type,
                    user_id=operation.user_id,
                )
                if target_entities:
                    target_id = target_entities[0].get("id")
                    await self.graph.add_edge(
                        source_id=entity_id,
                        target_id=target_id,
                        relationship_type=intent.relationship_type or "связан_с",
                    )
                    result["changes"].append({
                        "type": "link",
                        "source_id": entity_id,
                        "target_id": target_id,
                        "relationship": intent.relationship_type,
                    })
                else:
                    raise ValueError(f"Target entity not found: {intent.target_entity_name}")

            elif intent.action == EditAction.UNLINK:
                target_entities = await self._search_entities(
                    name=intent.target_entity_name,
                    entity_type=intent.target_entity_type,
                    user_id=operation.user_id,
                )
                if target_entities:
                    target_id = target_entities[0].get("id")
                    await self.graph.remove_edge(entity_id, target_id)
                    result["changes"].append({
                        "type": "unlink",
                        "source_id": entity_id,
                        "target_id": target_id,
                    })

            elif intent.action == EditAction.MERGE:
                # Объединение сущностей - сложная операция
                target_entities = await self._search_entities(
                    name=intent.target_entity_name,
                    entity_type=intent.target_entity_type,
                    user_id=operation.user_id,
                )
                if target_entities:
                    target = target_entities[0]
                    target_id = target.get("id")

                    # Переносим связи с target на entity
                    await self.graph.merge_nodes(entity_id, target_id)
                    result["changes"].append({
                        "type": "merge",
                        "kept_id": entity_id,
                        "merged_id": target_id,
                    })

            elif intent.action == EditAction.RECLASSIFY:
                await self.graph.update_node(entity_id, {"type": intent.new_value})
                result["changes"].append({
                    "type": "reclassify",
                    "entity_id": entity_id,
                    "new_type": intent.new_value,
                })

            # Сохраняем граф
            await self.graph.save()
            # Аудит: без этого поиск возвращал СТАРОЕ значение — граф
            # обновлялся, а supersede-история и векторный индекс нет.
            await self._sync_memory_after_edit(operation, result)

        result["success"] = True
        return result

    async def _sync_memory_after_edit(self, operation: "EditOperation",
                                      result: Dict[str, Any]) -> None:
        """Синхронизировать supersede-историю и векторный индекс с правкой
        (best-effort: ошибки логируются, правка графа уже применена)."""
        intent = operation.intent
        entity = operation.selected_entity or {}
        uid = operation.user_id or ""
        ent_name = entity.get("name") or entity.get("id") or "?"
        field = intent.target_field or intent.action.value
        old_v = entity.get(intent.target_field) if intent.target_field else None
        new_v = intent.new_value
        fact_key = f"{entity.get('id')}::{field}"
        try:
            if uid and new_v is not None:
                from backend.core.memory.supersede_store import SupersedeStore
                from backend.core.store.tenant_paths import (
                    supersede_store_path_for_user,
                )
                store = SupersedeStore(
                    persist_path=supersede_store_path_for_user(uid))
                store.record(f"{uid}::{fact_key}", str(new_v),
                             source=f"nl_edit:{operation.id}")
        except Exception as e:
            logger.warning(f"nl_editor: supersede sync failed for {fact_key}: {e}")
        try:
            if self.vector and new_v is not None:
                text = (f"ИСПРАВЛЕНИЕ ФАКТА: у «{ent_name}» поле «{field}» "
                        f"теперь «{new_v}»"
                        + (f" (ранее «{old_v}»)" if old_v is not None else "")
                        + f". Причина: {intent.reason or 'правка пользователя'}.")
                await self.vector.upsert_vector_public(
                    collection="entities",
                    vector_id=f"correction::{uid}::{fact_key}",
                    text=text,
                    metadata={"type": "memory_correction", "user_id": uid,
                              "entity_id": entity.get("id"), "field": field,
                              "new_value": str(new_v)})
        except Exception as e:
            logger.warning(f"nl_editor: vector sync failed for {fact_key}: {e}")

    def get_operation(self, operation_id: str) -> Optional[EditOperation]:
        """Получить операцию по ID."""
        return self._operations.get(operation_id)

    def get_user_operations(self, user_id: str, limit: int = 10) -> List[EditOperation]:
        """Получить операции пользователя."""
        user_ops = [
            op for op in self._operations.values()
            if op.user_id == user_id
        ]
        user_ops.sort(key=lambda x: x.created_at, reverse=True)
        return user_ops[:limit]

    def cancel_operation(self, operation_id: str) -> bool:
        """Отменить операцию."""
        operation = self._operations.get(operation_id)
        if operation and operation.status not in [EditStatus.APPLIED, EditStatus.CANCELLED]:
            operation.status = EditStatus.CANCELLED
            return True
        return False


# Instance cache per user
_nl_editor_instances: Dict[str, NLKnowledgeEditor] = {}


def get_nl_editor(
    graph_builder=None,
    vector_indexer=None,
    corrections_manager=None,
    user_id: str = "default",
) -> NLKnowledgeEditor:
    """Получить экземпляр редактора для пользователя."""
    global _nl_editor_instances

    # Создаём новый экземпляр если переданы компоненты
    if graph_builder is not None or vector_indexer is not None:
        editor = NLKnowledgeEditor(
            graph_builder=graph_builder,
            vector_indexer=vector_indexer,
            corrections_manager=corrections_manager,
        )
        _nl_editor_instances[user_id] = editor
        return editor

    # Возвращаем существующий или создаём пустой
    if user_id not in _nl_editor_instances:
        _nl_editor_instances[user_id] = NLKnowledgeEditor()

    return _nl_editor_instances[user_id]

