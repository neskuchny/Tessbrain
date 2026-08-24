# -*- coding: utf-8 -*-
"""
API Routes для Natural Language Knowledge Editor.

Позволяет редактировать знания через текстовые команды в чате.
"""
import logging
from typing import Optional

from litestar import Router, get, post
from backend.core.auth.user_guard import enforce_user_id_matches_token
from litestar.params import Parameter
from pydantic import BaseModel

logger = logging.getLogger(__name__)


# === Request/Response Models ===

class EditRequest(BaseModel):
    """Запрос на редактирование."""
    text: str  # Текст команды от пользователя
    context: Optional[dict] = None  # Дополнительный контекст


class ClarifyRequest(BaseModel):
    """Запрос на уточнение."""
    text: str  # Текст уточнения или номер выбора


class ConfirmRequest(BaseModel):
    """Запрос на подтверждение."""
    confirmed: bool = True


class EditResponse(BaseModel):
    """Ответ операции редактирования."""
    operation_id: str
    status: str

    # Если нужно уточнение
    clarification_needed: Optional[str] = None
    found_entities: Optional[list] = None

    # Предпросмотр изменений
    preview: Optional[dict] = None

    # Результат применения
    result: Optional[dict] = None
    error: Optional[str] = None

    # Человекочитаемое сообщение
    message: str = ""


# === API Endpoints ===

@post("/edit")
async def process_edit_request(
    data: EditRequest,
    user_id: str = Parameter(query="user_id"),
) -> EditResponse:
    """
    Обработать запрос на редактирование знаний.

    Примеры запросов:
    - "Измени должность Ивана Петрова на Senior Developer"
    - "Удали проект Alpha"
    - "Свяжи Марию с проектом Beta"
    - "Переименуй отдел Разработка в Engineering"

    Возвращает:
    - operation_id: ID операции для последующих действий
    - status: текущий статус операции
    - clarification_needed: вопрос если нужно уточнение
    - preview: предпросмотр изменений если готово к подтверждению
    """
    logger.info(f"Edit request from {user_id}: {data.text[:100]}")

    try:
        from backend.core.corrections import TwoPhaseManager
        from backend.core.knowledge_editor import get_nl_editor
        from backend.core.store.graph_builder import GraphBuilder
        from backend.core.store.tenant_paths import graph_path_for_user, vector_index_path_for_user
        from backend.core.store.vector_indexer import VectorIndexer

        logger.info("Initializing graph_builder...")
        # Инициализируем компоненты
        graph_path = graph_path_for_user(user_id)
        logger.info(f"Graph path: {graph_path}")

        graph_builder = GraphBuilder(
            use_networkx=None,  # Auto-detect backend
            graph_storage_path=graph_path
        )
        await graph_builder.connect()
        logger.info("Graph builder connected")

        # VectorIndexer — ОПЦИОНАЛЕН и только из уже прогретого кэша модели.
        # Раньше route грузил sentence-transformers синхронно внутри запроса;
        # на Windows холодная загрузка модели в живом процессе роняла ВЕСЬ
        # воркер granian («Unexpected exit from worker-1» → фронт получал
        # «Internal Server Error»). Правка графа в вектора не упирается —
        # эмбеддинги обновит следующая синхронизация/консолидация.
        vector_indexer = None
        try:
            from backend.core.store.vector_indexer import (
                _EMBEDDING_MODEL_CACHE as _vi_cache,  # прогрет ли модельный кэш
            )
        except Exception:
            _vi_cache = None
        if _vi_cache:
            try:
                vector_indexer = VectorIndexer(
                    storage_path=vector_index_path_for_user(user_id),
                    namespace=user_id,
                )
                await vector_indexer.connect()
                logger.info("Vector indexer connected (cached model)")
            except Exception as _ve:
                logger.warning(f"Vector indexer skipped: {_ve}")
                vector_indexer = None
        else:
            logger.info("Vector indexer skipped (model not warmed)")

        logger.info("Initializing corrections_manager...")
        corrections_manager = TwoPhaseManager(
            graph_builder=graph_builder,
            storage_path=f"data/corrections/{user_id}"
        )
        logger.info("Corrections manager initialized")

        logger.info("Getting NL editor...")
        editor = get_nl_editor(
            graph_builder=graph_builder,
            vector_indexer=vector_indexer,
            corrections_manager=corrections_manager,
            user_id=user_id,
        )
        logger.info("NL editor obtained")

        # Обрабатываем запрос
        logger.info("Processing request...")
        operation = await editor.process_request(
            user_id=user_id,
            text=data.text,
            context=data.context,
        )
        logger.info(f"Request processed, status: {operation.status}")

        # Формируем ответ
        response = EditResponse(
            operation_id=operation.id,
            status=operation.status.value,
            clarification_needed=operation.intent.clarification_needed,
            found_entities=operation.found_entities if operation.found_entities else None,
            preview=operation.preview,
            error=operation.error,
        )

        # Добавляем человекочитаемое сообщение
        if operation.status.value == "pending_clarification":
            response.message = operation.intent.clarification_needed or "Нужно уточнение"
        elif operation.status.value == "pending_confirmation":
            response.message = (
                f"Подтвердите изменение:\n\n"
                f"{operation.preview.get('human_readable', 'Изменение готово')}\n\n"
                f"Отправьте 'да' для подтверждения или 'нет' для отмены."
            )

        return response

    except Exception as e:
        logger.error(f"Edit request error: {e}")
        return EditResponse(
            operation_id="",
            status="failed",
            error=str(e),
            message=f"Ошибка: {e!s}"
        )


@post("/edit/{operation_id:str}/clarify")
async def clarify_edit(
    operation_id: str,
    data: ClarifyRequest,
    user_id: str = Parameter(query="user_id"),
) -> EditResponse:
    """
    Уточнить запрос на редактирование.

    Используется когда:
    - Найдено несколько сущностей - отправить номер (1, 2, 3...)
    - Нужно уточнить имя или другие детали - отправить текст
    """
    logger.info(f"Clarify edit {operation_id} from {user_id}: {data.text}")

    try:
        from backend.core.knowledge_editor import get_nl_editor

        editor = get_nl_editor(user_id=user_id)
        operation = await editor.clarify(operation_id, data.text)

        response = EditResponse(
            operation_id=operation.id,
            status=operation.status.value,
            clarification_needed=operation.intent.clarification_needed,
            found_entities=operation.found_entities if operation.found_entities else None,
            preview=operation.preview,
        )

        if operation.status.value == "pending_clarification":
            response.message = operation.intent.clarification_needed or "Нужно уточнение"
        elif operation.status.value == "pending_confirmation":
            response.message = (
                f"Подтвердите изменение:\n\n"
                f"{operation.preview.get('human_readable', 'Изменение готово')}\n\n"
                f"Отправьте 'да' для подтверждения."
            )

        return response

    except Exception as e:
        logger.error(f"Clarify error: {e}")
        return EditResponse(
            operation_id=operation_id,
            status="failed",
            error=str(e),
            message=f"Ошибка: {e!s}"
        )


@post("/edit/{operation_id:str}/confirm")
async def confirm_edit(
    operation_id: str,
    data: ConfirmRequest,
    user_id: str = Parameter(query="user_id"),
) -> EditResponse:
    """
    Подтвердить и применить изменения.

    Args:
        confirmed: true - применить, false - отменить
    """
    logger.info(f"Confirm edit {operation_id} from {user_id}: confirmed={data.confirmed}")

    try:
        from backend.core.knowledge_editor import get_nl_editor

        editor = get_nl_editor(user_id=user_id)
        operation = await editor.confirm_and_apply(operation_id, data.confirmed)

        response = EditResponse(
            operation_id=operation.id,
            status=operation.status.value,
            result=operation.result,
            error=operation.error,
        )

        if operation.status.value == "applied":
            changes_desc = ""
            if operation.result and operation.result.get("changes"):
                changes_desc = ", ".join([
                    f"{c.get('type', 'изменение')}"
                    for c in operation.result["changes"]
                ])
            response.message = f"Изменения успешно применены: {changes_desc}"
        elif operation.status.value == "cancelled":
            response.message = "Операция отменена"
        elif operation.status.value == "failed":
            response.message = f"Ошибка: {operation.error}"

        return response

    except Exception as e:
        logger.error(f"Confirm error: {e}")
        return EditResponse(
            operation_id=operation_id,
            status="failed",
            error=str(e),
            message=f"Ошибка: {e!s}"
        )


@get("/edit/{operation_id:str}")
async def get_edit_operation(
    operation_id: str,
    user_id: str = Parameter(query="user_id"),
) -> EditResponse:
    """Получить статус операции редактирования."""
    try:
        from backend.core.knowledge_editor import get_nl_editor

        editor = get_nl_editor(user_id=user_id)
        operation = editor.get_operation(operation_id)

        if not operation:
            return EditResponse(
                operation_id=operation_id,
                status="not_found",
                error="Operation not found",
                message="Операция не найдена"
            )

        return EditResponse(
            operation_id=operation.id,
            status=operation.status.value,
            clarification_needed=operation.intent.clarification_needed,
            found_entities=operation.found_entities if operation.found_entities else None,
            preview=operation.preview,
            result=operation.result,
            error=operation.error,
        )

    except Exception as e:
        logger.error(f"Get operation error: {e}")
        return EditResponse(
            operation_id=operation_id,
            status="failed",
            error=str(e),
        )


@get("/edit/history")
async def get_edit_history(
    user_id: str = Parameter(query="user_id"),
    limit: int = Parameter(query="limit", default=10),
) -> dict:
    """Получить историю операций редактирования пользователя."""
    try:
        from backend.core.knowledge_editor import get_nl_editor

        editor = get_nl_editor(user_id=user_id)
        operations = editor.get_user_operations(user_id, limit)

        return {
            "operations": [op.to_dict() for op in operations],
            "total": len(operations),
        }

    except Exception as e:
        logger.error(f"Get history error: {e}")
        return {"error": str(e), "operations": []}


@post("/edit/{operation_id:str}/cancel")
async def cancel_edit(
    operation_id: str,
    user_id: str = Parameter(query="user_id"),
) -> EditResponse:
    """Отменить операцию редактирования."""
    try:
        from backend.core.knowledge_editor import get_nl_editor

        editor = get_nl_editor(user_id=user_id)
        success = editor.cancel_operation(operation_id)

        if success:
            return EditResponse(
                operation_id=operation_id,
                status="cancelled",
                message="Операция отменена"
            )
        else:
            return EditResponse(
                operation_id=operation_id,
                status="failed",
                error="Cannot cancel operation",
                message="Невозможно отменить операцию (уже применена или отменена)"
            )

    except Exception as e:
        logger.error(f"Cancel error: {e}")
        return EditResponse(
            operation_id=operation_id,
            status="failed",
            error=str(e),
        )


# === Chat Integration ===

@post("/chat/edit")
async def chat_edit_command(
    data: EditRequest,
    user_id: str = Parameter(query="user_id"),
) -> dict:
    """
    Интеграция с чатом - обработка команд редактирования.

    Автоматически определяет:
    - Это команда редактирования или обычный вопрос
    - Если редактирование - обрабатывает через NL Editor
    - Если вопрос - возвращает флаг для обработки через поиск

    Примеры команд редактирования:
    - "измени...", "обнови...", "удали...", "добавь..."
    - "Иван теперь Senior Developer"
    - "проект Alpha закрыт"
    """
    text = data.text.lower().strip()

    # Ключевые слова команд редактирования
    edit_keywords = [
        "измени", "обнови", "поменяй", "исправь", "установи", "замени",
        "удали", "убери", "убрать", "сотри",
        "добавь", "создай", "внеси",
        "объедини", "слей",
        "переименуй",
        "свяжи", "соедини", "отвяжи", "разъедини",
        "теперь", "стал", "стала", "сейчас", "больше не",
        "change", "update", "delete", "remove", "add", "create", "rename",
    ]

    is_edit_command = any(kw in text for kw in edit_keywords)

    if is_edit_command:
        # Это команда редактирования
        result = await process_edit_request(data, user_id)
        return {
            "type": "edit",
            "response": result.model_dump(),
        }
    else:
        # Это не команда редактирования
        return {
            "type": "question",
            "message": "Это не команда редактирования. Используйте поиск.",
        }


# === Router ===

knowledge_editor_router = Router(
    path="/knowledge",
    guards=[enforce_user_id_matches_token],
    route_handlers=[
        process_edit_request,
        clarify_edit,
        confirm_edit,
        get_edit_operation,
        get_edit_history,
        cancel_edit,
        chat_edit_command,
    ],
    tags=["Knowledge Editor"],
)

