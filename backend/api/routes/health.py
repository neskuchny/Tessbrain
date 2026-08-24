"""
TESSENT BRAIN - Health Check Routes
Эндпоинты для проверки здоровья системы
"""
import asyncio
import logging
from datetime import datetime
from typing import Any

from litestar import Response, Router, get, post
from litestar.params import Parameter

from backend.config import settings

logger = logging.getLogger(__name__)


@get("/ping")
async def ping() -> dict[str, str]:
    """
    Простая проверка доступности API

    Returns:
        {"status": "pong"}
    """
    return {"status": "pong"}


@get("/livez")
async def livez() -> dict[str, str]:
    """Liveness probe: процесс жив. Возвращает 200 всегда.

    Используется Kubernetes/orchestrator'ом, чтобы решать о перезапуске
    контейнера. НЕ должен зависеть от внешних сервисов — это «pid-1» проверка.
    """
    return {"status": "alive"}


async def _probe(get_client_coro, label: str) -> bool:
    """Универсальная обёртка: вызывает client.health_check() с таймаутом 2с."""
    try:
        client = await get_client_coro()
        return bool(await asyncio.wait_for(client.health_check(), timeout=2.0))
    except Exception as exc:
        logger.debug("readyz: %s probe failed: %s", label, exc)
        return False


async def _probe_postgres() -> bool:
    from backend.db.postgres import get_postgres
    return await _probe(get_postgres, "postgres")


async def _probe_redis() -> bool:
    from backend.db.redis_client import get_redis
    return await _probe(get_redis, "redis")


async def _probe_qdrant() -> bool:
    from backend.db.qdrant import get_qdrant
    return await _probe(get_qdrant, "qdrant")


async def _probe_neo4j() -> bool:
    from backend.db.neo4j_client import get_neo4j
    return await _probe(get_neo4j, "neo4j")


@get("/readyz")
async def readyz() -> Response:
    """Readiness probe: все критичные зависимости отвечают.

    Проверяет Postgres, Redis, Qdrant, Neo4j параллельно с таймаутом 2с.
    Возвращает 200 если все ОК, 503 если хотя бы одна не отвечает.
    Поды без ready-статуса вынимаются из балансировщика — это правильно,
    потому что обработать запрос они всё равно не смогут.
    """
    return await _build_readiness_response()


async def _build_readiness_response() -> Response:
    """Реальная логика проверок — вынесена, чтобы /healthz мог переиспользовать
    её без обхода Litestar-обёртки (вызов handler-объекта как функции в 2.x
    приводит к `HTTPRouteHandler.__call__() missing argument 'fn'`).
    """
    pg, rd, qd, n4 = await asyncio.gather(
        _probe_postgres(), _probe_redis(), _probe_qdrant(), _probe_neo4j(),
        return_exceptions=False,
    )
    services = {
        "postgres": pg,
        "redis": rd,
        "qdrant": qd,
        "neo4j": n4,
    }
    all_ready = all(services.values())
    body: dict[str, Any] = {
        "status": "ready" if all_ready else "not_ready",
        "services": services,
        "timestamp": datetime.utcnow().isoformat(),
    }
    return Response(content=body, status_code=200 if all_ready else 503)


@get("/healthz")
async def healthz() -> Response:
    """Alias для /readyz — k8s часто использует именно `healthz`."""
    return await _build_readiness_response()


@get("/health")
async def health_check() -> dict[str, Any]:
    """
    Детальная проверка здоровья всех компонентов

    Returns:
        Статус всех сервисов
    """
    services = {
        "api": True,
        "graph": False,
        "vectors": False,
    }

    # Check graph
    try:
        from backend.api.app import _graph_builder
        if _graph_builder and _graph_builder.nx_graph:
            services["graph"] = True
            services["graph_nodes"] = _graph_builder.nx_graph.number_of_nodes()
            services["graph_edges"] = _graph_builder.nx_graph.number_of_edges()
    except Exception:
        logger.debug("suppressed exception", exc_info=True)

    # Check vector indexer
    try:
        from backend.api.routes.chat import _vector_indexer
        if _vector_indexer:
            services["vectors"] = True
    except Exception:
        logger.debug("suppressed exception", exc_info=True)

    all_healthy = services.get("api", False)

    return {
        "status": "healthy" if all_healthy else "degraded",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "0.1.0",
        "environment": settings.app_env,
        "services": services,
    }


@get("/info")
async def system_info() -> dict[str, Any]:
    """
    Информация о системе

    Returns:
        Информация о версии, возможностях и конфигурации
    """
    return {
        "name": "Tessbrain",
        "version": "0.1.0",
        "description": "Цифровой мозг компании",
        "environment": settings.app_env,
        "capabilities": {
            "ingestion": {
                "meetings": True,
                "documents": True,
                "tasks": True,
            },
            "reasoning": {
                "kag_engine": True,
                "multi_hop": True,
                "anomaly_detection": True,
            },
            "storage": {
                "knowledge_graph": "networkx (in-memory)",
                "vectors": "in-memory",
            },
            "agents": {
                "brain": True,
                "mark001": True,
                "meetflow": True,
            },
        },
        "api": {
            "docs": "/docs",
            "openapi": "/schema",
        },
    }


@get("/nightly-status")
async def nightly_status() -> dict[str, Any]:
    """
    Статус ночной консолидации и нагрузки системы.

    Returns:
        - enabled: включена ли ночная обработка
        - schedule: расписание (часы UTC)
        - is_running: выполняется ли сейчас
        - last_run: время последнего запуска
        - last_result: результат последнего запуска
        - load_monitor: текущая нагрузка (запросы за 15 мин / час)
    """
    try:
        from backend.core.automations.scheduler import get_scheduler
        scheduler = get_scheduler()
        return scheduler.get_nightly_status()
    except Exception as e:
        return {
            "enabled": False,
            "error": str(e),
        }


@post("/nightly-run")
async def trigger_nightly_run(
    user_id: str = Parameter(query="user_id", default=""),
) -> dict[str, Any]:
    """Принудительно запустить ночную консолидацию СЕЙЧАС (вне ночного окна).

    Плановый прогон 2:00–6:00 UTC на локальной машине почти никогда не
    срабатывает (комп ночью выключен). Эта кнопка гоняет тот же pipeline
    (коррекции → temporal decay → консолидация памяти → снапшоты → инсайты)
    по требованию. Работает в фоне: возвращает started=true сразу, прогресс
    виден через /nightly-status. Защита от двойного запуска — общий флаг
    scheduler._nightly_running (плановый и ручной не пересекутся).

    user_id (опц.) — обработать ТОЛЬКО этого юзера (кнопка для текущего
    аккаунта). Без него — все юзеры (как плановый прогон).
    """
    from datetime import timezone

    from backend.core.automations.scheduler import get_scheduler
    scheduler = get_scheduler()

    if getattr(scheduler, "_nightly_running", False):
        return {"started": False, "reason": "already_running",
                "detail": "Консолидация уже выполняется"}

    scheduler._nightly_running = True

    async def _run() -> None:
        start = datetime.now(timezone.utc)
        try:
            from backend.core.corrections.nightly_consolidation import (
                run_nightly_consolidation,
            )
            # force=True — мимо распределённого лока: кнопку нажали осознанно,
            # реплик нет, а stale-лок от упавшего прогона иначе блокирует на TTL.
            # only_user_id — гоняем только текущий аккаунт (если передан).
            result = await run_nightly_consolidation(
                force=True, only_user_id=(user_id or None))
            scheduler._nightly_last_result = result
            logger.info("✅ Manual consolidation completed: %s", result)
        except Exception as e:  # noqa: BLE001
            logger.exception("❌ Manual consolidation failed")
            scheduler._nightly_last_result = {"error": str(e)}
        finally:
            scheduler._nightly_last_run = datetime.now(timezone.utc).isoformat()
            scheduler._nightly_running = False
            elapsed = (datetime.now(timezone.utc) - start).total_seconds()
            logger.info("🌙 Manual consolidation finished in %.1fs", elapsed)

    asyncio.create_task(_run())
    return {"started": True, "detail": "Консолидация запущена в фоне"}


@get("/scheduler-status")
async def scheduler_status() -> dict[str, Any]:
    """Здоровье планировщика автоматизаций (отложенные/повторяющиеся задачи).

    Для uptime-мониторинга: status='stale' если планировщик не тикал дольше
    3× интервала проверки — отложенные задачи НЕ исполняются, даже если
    процесс жив. 'down' — не запущен вовсе.
    """
    try:
        from datetime import datetime, timezone

        from backend.core.automations.scheduler import CHECK_INTERVAL, get_scheduler
        s = get_scheduler()
        last_tick = getattr(s, "_last_tick_at", None)
        status = "down"
        stale_after_s = CHECK_INTERVAL * 3
        if s.is_running:
            status = "running"
            if last_tick:
                try:
                    dt = datetime.fromisoformat(last_tick)
                    age = (datetime.now(timezone.utc) - dt).total_seconds()
                    if age > stale_after_s:
                        status = "stale"
                except ValueError:
                    pass
        return {
            "status": status,
            "is_running": s.is_running,
            "last_tick_at": last_tick,
            "tick_count": getattr(s, "_tick_count", 0),
            "check_interval_seconds": CHECK_INTERVAL,
            "stale_after_seconds": stale_after_s,
        }
    except Exception as e:
        return {"status": "down", "error": str(e)}


@get("/feature-flags")
async def feature_flags_status() -> dict[str, Any]:
    """
    Статус всех feature flags.

    Показывает какие подсистемы включены/выключены.
    Для изменения: config/feature_flags.json или ENV TESSENT_ENABLE_<FLAG>=true

    Returns:
        Словарь {flag_name: bool} для всех флагов
    """
    try:
        from backend.core.config.feature_flags import get_feature_flags
        flags = get_feature_flags()
        return {
            "flags": flags.get_status(),
            "active_count": sum(1 for v in flags.get_status().values() if v),
            "total_count": len(flags.get_status()),
        }
    except Exception as e:
        return {"error": str(e)}


@get("/metrics", media_type="text/plain")
async def prometheus_metrics() -> Response:
    """Prometheus scrape endpoint (W13).

    Возвращает все registered metrics в Prometheus text format. Если
    `prometheus_client` не установлен — пустой body с пометкой, чтобы
    scraper не падал.

    ВАЖНО: в production защитить network-policy (только Prometheus CIDR)
    либо service-JWT. Метрики содержат tenant_id — наружу не светить.
    """
    from backend.core.observability.metrics import render_metrics
    body, content_type = render_metrics()
    return Response(content=body, media_type=content_type)


# Router для health endpoints
router = Router(
    path="/",
    route_handlers=[
        ping, livez, readyz, healthz,
        health_check, system_info, nightly_status, trigger_nightly_run,
        scheduler_status, feature_flags_status, prometheus_metrics,
    ],
    tags=["Health"],
)
