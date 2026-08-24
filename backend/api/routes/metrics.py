# -*- coding: utf-8 -*-
"""
API routes for system metrics.
"""
from typing import Any, Optional

import structlog
from litestar import Router, get
from backend.core.auth.user_guard import enforce_user_id_matches_token
from litestar.params import Parameter

logger = structlog.get_logger(__name__)


@get("/")
async def get_metrics() -> dict[str, Any]:
    """
    Получить все метрики системы.

    Returns:
        Полная статистика метрик
    """
    try:
        from backend.core.monitoring import get_metrics_collector

        collector = get_metrics_collector()
        stats = collector.get_stats()

        return {
            "status": "ok",
            "metrics": stats,
        }

    except Exception as e:
        logger.error(f"Error getting metrics: {e}")
        return {
            "status": "error",
            "error": str(e),
        }


@get("/counters")
async def get_counters() -> dict[str, Any]:
    """Получить все счётчики."""
    try:
        from backend.core.monitoring import get_metrics_collector

        collector = get_metrics_collector()

        return {
            "counters": dict(collector._counters),
        }

    except Exception as e:
        logger.error(f"Error getting counters: {e}")
        return {"error": str(e)}


@get("/histograms/{name:str}")
async def get_histogram(name: str) -> dict[str, Any]:
    """
    Получить статистику гистограммы.

    Args:
        name: Название метрики
    """
    try:
        from backend.core.monitoring import get_metrics_collector

        collector = get_metrics_collector()
        stats = collector.get_histogram_stats(name)

        return {
            "name": name,
            "stats": stats,
        }

    except Exception as e:
        logger.error(f"Error getting histogram: {e}")
        return {"error": str(e)}


@get("/timeseries")
async def get_timeseries(
    name: Optional[str] = Parameter(query="name", default=None),
    limit: int = Parameter(query="limit", default=100),
) -> dict[str, Any]:
    """
    Получить временной ряд метрик.

    Args:
        name: Фильтр по названию (опционально)
        limit: Максимум записей
    """
    try:
        from backend.core.monitoring import get_metrics_collector

        collector = get_metrics_collector()
        series = collector.get_timeseries(name=name, limit=limit)

        return {
            "count": len(series),
            "timeseries": series,
        }

    except Exception as e:
        logger.error(f"Error getting timeseries: {e}")
        return {"error": str(e)}


@get("/system")
async def get_system_metrics(
    user_id: str = Parameter(query="user_id", required=True),
) -> dict[str, Any]:
    """
    Получить метрики системы для пользователя.

    Args:
        user_id: ID пользователя
    """
    try:
        from backend.core.memory import get_agent_memory_manager
        from backend.core.store.graph_view import merged_graph_view_for_user

        # Метрики графа
        graph_stats = {}
        try:
            # Wave 2.4: federated view — метрики включают promoted org-узлы.
            graph = await merged_graph_view_for_user(user_id, use_networkx=None)

            if graph.use_networkx and graph.nx_graph:
                graph_stats = {
                    "nodes_count": graph.nx_graph.number_of_nodes(),
                    "edges_count": graph.nx_graph.number_of_edges(),
                }
        except Exception as e:
            graph_stats = {"error": str(e)}

        # Метрики памяти агентов
        memory_stats = {}
        try:
            memory = get_agent_memory_manager()
            memory_stats = memory.get_stats()
        except Exception as e:
            memory_stats = {"error": str(e)}

        return {
            "user_id": user_id,
            "graph": graph_stats,
            "agent_memory": memory_stats,
        }

    except Exception as e:
        logger.error(f"Error getting system metrics: {e}")
        return {"error": str(e)}


# Router
metrics_router = Router(
    path="/metrics",
    guards=[enforce_user_id_matches_token],
    route_handlers=[
        get_metrics,
        get_counters,
        get_histogram,
        get_timeseries,
        get_system_metrics,
    ],
    tags=["Metrics"],
)


