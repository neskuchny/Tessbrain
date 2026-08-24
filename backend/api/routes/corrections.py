# -*- coding: utf-8 -*-
"""
TESSENT BRAIN - Corrections Routes
API для системы исправлений данных (Two-Phase Corrections)
"""
from typing import Any, List, Optional

import structlog
from litestar import Router, get, post, put
from backend.core.auth.user_guard import enforce_user_id_matches_token
from litestar.params import Parameter
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)


# ============================================================================
# REQUEST/RESPONSE MODELS
# ============================================================================

class CorrectionRequest(BaseModel):
    """Запрос на создание исправления."""
    entity_id: str = Field(..., description="ID сущности")
    entity_type: str = Field(default="unknown", description="Тип сущности")
    field: str = Field(..., description="Поле для исправления")
    old_value: Any = Field(..., description="Старое значение")
    new_value: Any = Field(..., description="Новое значение")
    reason: str = Field(..., description="Причина исправления")
    priority: int = Field(default=1, ge=1, le=5, description="Приоритет (1-5)")


class ConflictResolutionRequest(BaseModel):
    """Запрос на разрешение конфликта."""
    correction_ids: List[str] = Field(..., description="ID конфликтующих исправлений")
    resolution: str = Field(..., description="Стратегия: keep_first, keep_last, manual")
    resolved_value: Any = Field(default=None, description="Итоговое значение (для manual)")


class CorrectionResponse(BaseModel):
    """Ответ с информацией об исправлении."""
    id: str
    entity_id: str
    entity_type: str
    field: str
    old_value: Any
    new_value: Any
    reason: str
    status: str
    priority: int
    user_id: str
    created_at: str
    applied_at: Optional[str] = None
    error: Optional[str] = None


# ============================================================================
# HELPERS
# ============================================================================

async def _get_manager(user_id: str):
    """Получить менеджер исправлений для пользователя."""
    from backend.core.corrections import get_two_phase_manager
    from backend.core.store.graph_builder import GraphBuilder
    from backend.core.store.tenant_paths import graph_path_for_user

    graph_builder = GraphBuilder(
        use_networkx=None,  # Auto-detect backend
        graph_storage_path=graph_path_for_user(user_id)
    )
    await graph_builder.connect()

    # аудит: обязательно per-user — глобальный синглтон применял правки
    # одного пользователя к графу другого
    return get_two_phase_manager(graph_builder, user_id=user_id)


# ============================================================================
# ROUTE HANDLERS
# ============================================================================

@post("/")
async def create_correction(
    data: CorrectionRequest,
    user_id: str = Parameter(query="user_id", required=True),
) -> dict[str, Any]:
    """
    Создать исправление (ФАЗА 1 - ДЕНЬ).

    Исправление сохраняется в буфер и будет применено
    во время ночной консолидации (ФАЗА 2).

    Args:
        data: Данные исправления
        user_id: ID пользователя

    Returns:
        Результат с ID исправления
    """
    logger.info(
        "📝 Creating correction",
        entity_id=data.entity_id,
        field=data.field,
        user_id=user_id
    )

    try:
        manager = await _get_manager(user_id)

        result = await manager.record_correction(
            entity_id=data.entity_id,
            field=data.field,
            old_value=data.old_value,
            new_value=data.new_value,
            reason=data.reason,
            user_id=user_id,
            entity_type=data.entity_type,
            priority=data.priority,
        )

        return {
            "success": result["success"],
            "correction_id": result.get("correction_id"),
            "conflicts": result.get("conflicts", []),
            "message": "Correction recorded" if result["success"] else "Failed to record correction"
        }

    except Exception as e:
        logger.error(f"Error creating correction: {e}")
        return {
            "success": False,
            "error": str(e),
        }


@get("/pending")
async def get_pending_corrections(
    user_id: str = Parameter(query="user_id", required=True),
    entity_id: Optional[str] = Parameter(query="entity_id", default=None),
    status: Optional[str] = Parameter(query="status", default=None),
) -> dict[str, Any]:
    """
    Получить список pending исправлений.

    Args:
        user_id: ID пользователя
        entity_id: Фильтр по сущности
        status: Фильтр по статусу (pending, approved, conflicted)

    Returns:
        Список исправлений
    """
    logger.info("📋 Getting pending corrections", user_id=user_id)

    try:
        manager = await _get_manager(user_id)

        corrections = await manager.get_pending_corrections(
            entity_id=entity_id,
            user_id=user_id,
            status=status,
        )

        return {
            "total": len(corrections),
            "corrections": corrections,
        }

    except Exception as e:
        logger.error(f"Error getting pending corrections: {e}")
        return {
            "total": 0,
            "corrections": [],
            "error": str(e),
        }


@put("/{correction_id:str}/approve")
async def approve_correction(
    correction_id: str,
    user_id: str = Parameter(query="user_id", required=True),
) -> dict[str, Any]:
    """
    Одобрить исправление.

    После одобрения исправление будет применено
    во время следующей ночной консолидации.

    Args:
        correction_id: ID исправления
        user_id: ID пользователя
    """
    logger.info("✅ Approving correction", correction_id=correction_id, user_id=user_id)

    try:
        manager = await _get_manager(user_id)
        success = await manager.approve_correction(correction_id, user_id)

        return {
            "success": success,
            "correction_id": correction_id,
            "message": "Correction approved" if success else "Correction not found"
        }

    except Exception as e:
        logger.error(f"Error approving correction: {e}")
        return {
            "success": False,
            "error": str(e),
        }


@put("/{correction_id:str}/reject")
async def reject_correction(
    correction_id: str,
    user_id: str = Parameter(query="user_id", required=True),
    reason: str = Parameter(query="reason", default=""),
) -> dict[str, Any]:
    """
    Отклонить исправление.

    Args:
        correction_id: ID исправления
        user_id: ID пользователя
        reason: Причина отклонения
    """
    logger.info("❌ Rejecting correction", correction_id=correction_id, user_id=user_id)

    try:
        manager = await _get_manager(user_id)
        success = await manager.reject_correction(correction_id, user_id, reason)

        return {
            "success": success,
            "correction_id": correction_id,
            "message": "Correction rejected" if success else "Correction not found"
        }

    except Exception as e:
        logger.error(f"Error rejecting correction: {e}")
        return {
            "success": False,
            "error": str(e),
        }


@post("/resolve-conflict")
async def resolve_conflict(
    data: ConflictResolutionRequest,
    user_id: str = Parameter(query="user_id", required=True),
) -> dict[str, Any]:
    """
    Разрешить конфликт между исправлениями.

    Стратегии:
    - keep_first: Оставить первое исправление
    - keep_last: Оставить последнее исправление
    - manual: Использовать указанное значение

    Args:
        data: Данные для разрешения конфликта
        user_id: ID пользователя
    """
    logger.info(
        "🔧 Resolving conflict",
        correction_ids=data.correction_ids,
        resolution=data.resolution,
        user_id=user_id
    )

    try:
        manager = await _get_manager(user_id)

        success = await manager.resolve_conflict(
            correction_ids=data.correction_ids,
            resolution=data.resolution,
            resolved_value=data.resolved_value,
            resolved_by=user_id,
        )

        return {
            "success": success,
            "message": "Conflict resolved" if success else "Failed to resolve conflict"
        }

    except Exception as e:
        logger.error(f"Error resolving conflict: {e}")
        return {
            "success": False,
            "error": str(e),
        }


@post("/apply")
async def apply_pending_corrections(
    user_id: str = Parameter(query="user_id", required=True),
    approved_only: bool = Parameter(query="approved_only", default=True),
) -> dict[str, Any]:
    """
    Применить pending исправления (ФАЗА 2 - НОЧЬ).

    Обычно вызывается автоматически cron-задачей,
    но можно вызвать вручную.

    Args:
        user_id: ID пользователя
        approved_only: Применять только одобренные

    Returns:
        Статистика применения
    """
    logger.info("🌙 Applying pending corrections", user_id=user_id)

    try:
        manager = await _get_manager(user_id)

        stats = await manager.apply_pending_corrections(
            apply_approved_only=approved_only
        )

        return {
            "success": True,
            "stats": stats,
        }

    except Exception as e:
        logger.error(f"Error applying corrections: {e}")
        return {
            "success": False,
            "error": str(e),
        }


@get("/stats")
async def get_corrections_stats(
    user_id: str = Parameter(query="user_id", required=True),
) -> dict[str, Any]:
    """
    Получить статистику системы исправлений.

    Args:
        user_id: ID пользователя

    Returns:
        Статистика
    """
    try:
        manager = await _get_manager(user_id)
        stats = manager.get_stats()

        return {
            "success": True,
            "stats": stats,
        }

    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        return {
            "success": False,
            "error": str(e),
        }


# Router
router = Router(
    path="/corrections",
    guards=[enforce_user_id_matches_token],
    route_handlers=[
        create_correction,
        get_pending_corrections,
        approve_correction,
        reject_correction,
        resolve_conflict,
        apply_pending_corrections,
        get_corrections_stats,
    ],
    tags=["Corrections"],
)


