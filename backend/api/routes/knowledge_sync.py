# -*- coding: utf-8 -*-
"""
TESSENT BRAIN - Knowledge Sync Routes
API для управления синхронизацией знаний из встреч
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

import httpx
from litestar import Router, delete, get, patch, post
from litestar.params import Parameter
from pydantic import BaseModel, Field

from backend.api.middleware.auth_middleware import get_user_id_from_token

logger = logging.getLogger(__name__)

# In-memory registry of sync runs (dev-friendly). For production we can persist this in DB.
_SYNC_RUNS: Dict[str, Dict[str, Any]] = {}
# Ссылки на фоновые sync-таски (защита от GC до завершения)
_SYNC_TASKS: set = set()


def get_active_sync_progress(user_id: str) -> Optional[Dict[str, Any]]:
    """Прогресс активного (running) синка для юзера, иначе None.

    Используется чатом, чтобы честно предупредить «идёт синхронизация — данные
    могут быть неполными» (Qdrant/граф в этот момент дописываются). Берём самый
    свежий running-run.
    """
    running = [
        r for r in _SYNC_RUNS.values()
        if r.get("user_id") == user_id and r.get("status") == "running"
    ]
    if not running:
        return None
    running.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    return running[0].get("progress") or {"phase": "running"}


# === Models ===

class CreateSubscriptionRequest(BaseModel):
    """Запрос на создание подписки"""
    subscription_type: str  # 'project' or 'folder'
    project_id: Optional[str] = None
    folder_id: Optional[str] = None
    auto_process_new_meetings: bool = True
    include_subfolders: bool = True


class UpdateSubscriptionRequest(BaseModel):
    """Запрос на обновление подписки"""
    auto_process_new_meetings: Optional[bool] = None
    include_subfolders: Optional[bool] = None
    status: Optional[str] = None  # active, paused


class EmitEventRequest(BaseModel):
    """Ручной/внешний эмит события в EventBus."""
    event_type: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    source: str = "api_manual"


# === Helper Functions ===

def get_user_id_from_request(
    authorization: Optional[str],
    user_id: Optional[str] = None
) -> Optional[str]:
    """Получить user_id: ВЕРИФИЦИРОВАННЫЙ токен > query-параметр.

    Аудит (IDOR): раньше ?user_id= имел приоритет над токеном — любой мог
    читать/запускать/удалять чужие подписки. Теперь при валидном токене
    user_id берётся из него, попытка действовать за чужого → отказ; query —
    только fallback без токена (обратная совместимость)."""
    logger.info(f"get_user_id_from_request: user_id={user_id}, auth_len={len(authorization) if authorization else 0}")
    try:
        from backend.core.auth.service_token import trusted_user_id
        headers = {"authorization": authorization} if authorization else {}
        uid, src = trusted_user_id(headers, user_id or "")
        if src != "unverified" and uid:
            return uid
    except PermissionError:
        logger.warning("knowledge_sync: токен валиден, но запрошен чужой user_id — отказ")
        return None
    except Exception:
        logger.debug("knowledge_sync: trusted_user_id unavailable", exc_info=True)
    if user_id:
        return user_id
    return get_user_id_from_token(authorization)


def get_supabase_client():
    """Получить Supabase клиент"""
    from backend.db.supabase_client import get_supabase_client as get_client
    return get_client()


# === Route Handlers ===

@get("/subscriptions")
async def list_subscriptions(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False)
) -> Dict[str, Any]:
    """Получить список подписок пользователя"""
    resolved_user_id = get_user_id_from_request(authorization, user_id)
    if not resolved_user_id:
        return {"status": "error", "message": "Authentication required"}

    try:
        supabase = get_supabase_client()
        subscriptions = await supabase._request(
            "GET",
            "/rest/v1/knowledge_sync_subscriptions",
            params={
                "user_id": f"eq.{resolved_user_id}",
                "status": "neq.deleted",
                "select": "*",
                "order": "created_at.desc"
            }
        )

        return {
            "status": "success",
            "subscriptions": subscriptions or []
        }
    except Exception as e:
        logger.error(f"Error listing subscriptions: {e}")
        return {"status": "error", "message": str(e)}


@post("/subscriptions")
async def create_subscription(
    data: CreateSubscriptionRequest,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False)
) -> Dict[str, Any]:
    """Создать новую подписку на синхронизацию"""
    logger.info(f"DEBUG: create_subscription called with {data}")
    resolved_user_id = get_user_id_from_request(authorization, user_id)
    if not resolved_user_id:
        return {"status": "error", "message": "Authentication required"}

    # Validate subscription type and required fields
    if data.subscription_type not in ('project', 'folder'):
        return {"status": "error", "message": "subscription_type must be 'project' or 'folder'"}

    if data.subscription_type == 'project':
        if not data.project_id:
            return {"status": "error", "message": "project_id is required for project subscription"}
        if data.folder_id:
            return {"status": "error", "message": "folder_id must be null for project subscription"}
    elif data.subscription_type == 'folder':
        if not data.folder_id:
            return {"status": "error", "message": "folder_id is required for folder subscription"}

    try:
        supabase = get_supabase_client()

        # If subscription already exists, return it instead of throwing 409
        lookup_params: Dict[str, str] = {
            "user_id": f"eq.{resolved_user_id}",
            "subscription_type": f"eq.{data.subscription_type}",
            "select": "*",
            "limit": "1",
        }
        if data.subscription_type == "project":
            lookup_params["project_id"] = f"eq.{data.project_id}"
        else:
            lookup_params["folder_id"] = f"eq.{data.folder_id}"

        existing = await supabase._request(
            "GET",
            "/rest/v1/knowledge_sync_subscriptions",
            params=lookup_params,
        )
        if existing:
            existing_sub = existing[0]
            # revive soft-deleted subscription
            if existing_sub.get("status") == "deleted":
                revived = await supabase._request(
                    "PATCH",
                    "/rest/v1/knowledge_sync_subscriptions",
                    params={
                        "id": f"eq.{existing_sub.get('id')}",
                        "user_id": f"eq.{resolved_user_id}",
                    },
                    json_data={
                        "status": "active",
                        "auto_process_new_meetings": data.auto_process_new_meetings,
                        "include_subfolders": data.include_subfolders,
                    },
                    headers={"Prefer": "return=representation"},
                )
                return {
                    "status": "success",
                    "subscription": revived[0] if revived else existing_sub,
                    "already_exists": True,
                    "revived": True,
                }

            return {
                "status": "success",
                "subscription": existing_sub,
                "already_exists": True,
            }

        subscription_data = {
            "user_id": resolved_user_id,
            "subscription_type": data.subscription_type,
            "project_id": data.project_id,
            "folder_id": data.folder_id,
            "auto_process_new_meetings": data.auto_process_new_meetings,
            "include_subfolders": data.include_subfolders,
            "status": "active",
            "meetings_processed": 0
        }

        result = await supabase._request(
            "POST",
            "/rest/v1/knowledge_sync_subscriptions",
            json_data=subscription_data,
            headers={"Prefer": "return=representation"}
        )

        logger.info(f"Created subscription for user {resolved_user_id}: {data.subscription_type}")

        return {
            "status": "success",
            "subscription": result[0] if result else subscription_data
        }
    except httpx.HTTPStatusError as e:
        # race-condition: someone created the same subscription between our GET and POST
        if e.response is not None and e.response.status_code == 409:
            try:
                supabase = get_supabase_client()
                existing = await supabase._request(
                    "GET",
                    "/rest/v1/knowledge_sync_subscriptions",
                    params=lookup_params,
                )
                if existing:
                    return {
                        "status": "success",
                        "subscription": existing[0],
                        "already_exists": True,
                    }
            except Exception:
                logger.debug("suppressed exception", exc_info=True)
        logger.error(f"Error creating subscription (HTTP): {e}")
        return {"status": "error", "message": str(e)}
    except Exception as e:
        logger.error(f"Error creating subscription: {e}")
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": str(e)}


@patch("/subscriptions/{subscription_id:str}")
async def update_subscription(
    subscription_id: str,
    data: UpdateSubscriptionRequest,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False)
) -> Dict[str, Any]:
    """Обновить подписку"""
    resolved_user_id = get_user_id_from_request(authorization, user_id)
    if not resolved_user_id:
        return {"status": "error", "message": "Authentication required"}

    try:
        supabase = get_supabase_client()

        update_data = {}
        if data.auto_process_new_meetings is not None:
            update_data["auto_process_new_meetings"] = data.auto_process_new_meetings
        if data.include_subfolders is not None:
            update_data["include_subfolders"] = data.include_subfolders
        if data.status is not None:
            if data.status not in ('active', 'paused'):
                return {"status": "error", "message": "status must be 'active' or 'paused'"}
            update_data["status"] = data.status

        if not update_data:
            return {"status": "error", "message": "No data to update"}

        result = await supabase._request(
            "PATCH",
            "/rest/v1/knowledge_sync_subscriptions",
            params={
                "id": f"eq.{subscription_id}",
                "user_id": f"eq.{resolved_user_id}"
            },
            json_data=update_data,
            headers={"Prefer": "return=representation"}
        )

        return {
            "status": "success",
            "subscription": result[0] if result else None
        }
    except Exception as e:
        logger.error(f"Error updating subscription: {e}")
        return {"status": "error", "message": str(e)}


@delete("/subscriptions/{subscription_id:str}", status_code=200)
async def delete_subscription(
    subscription_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False)
) -> Dict[str, Any]:
    """Удалить подписку (soft delete - меняем status на deleted)"""
    resolved_user_id = get_user_id_from_request(authorization, user_id)
    if not resolved_user_id:
        return {"status": "error", "message": "Authentication required"}

    try:
        supabase = get_supabase_client()

        # Soft delete - set status to 'deleted'
        await supabase._request(
            "PATCH",
            "/rest/v1/knowledge_sync_subscriptions",
            params={
                "id": f"eq.{subscription_id}",
                "user_id": f"eq.{resolved_user_id}"
            },
            json_data={"status": "deleted"}
        )

        logger.info(f"Deleted subscription {subscription_id}")

        return {"status": "success", "message": "Subscription deleted"}
    except Exception as e:
        logger.error(f"Error deleting subscription: {e}")
        return {"status": "error", "message": str(e)}


@post("/sync/{subscription_id:str}")
async def trigger_sync(
    subscription_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
    wait: bool = Parameter(query="wait", default=False, required=False),
    force: bool = Parameter(query="force", default=False, required=False),
    model_tier: str = Parameter(query="model_tier", default="standard", required=False),  # standard или premium
    reprocess_older_than: Optional[str] = Parameter(query="reprocess_older_than", default=None, required=False),
) -> Dict[str, Any]:
    """Запустить синхронизацию для подписки

    Args:
        model_tier: Уровень модели LLM - "standard" (gemini-flash-lite-latest) или "premium" (gemini-flash-latest)
    """
    resolved_user_id = get_user_id_from_request(authorization, user_id)
    if not resolved_user_id:
        return {"status": "error", "message": "Authentication required"}

    try:
        supabase = get_supabase_client()

        # Получаем подписку
        subscriptions = await supabase._request(
            "GET",
            "/rest/v1/knowledge_sync_subscriptions",
            params={
                "id": f"eq.{subscription_id}",
                "user_id": f"eq.{resolved_user_id}",
                "select": "*"
            }
        )

        if not subscriptions:
            return {"status": "error", "message": "Subscription not found"}

        subscription = subscriptions[0]

        # Запускаем обработку (по умолчанию асинхронно, чтобы запрос не рвался на длинных LLM-задачах)
        from backend.core.knowledge_sync import process_meetings_for_subscription

        async def _runner(run_id: str) -> None:
            started_at = datetime.now(timezone.utc).isoformat()
            _SYNC_RUNS[run_id] = {
                "run_id": run_id,
                "subscription_id": subscription_id,
                "user_id": resolved_user_id,
                "status": "running",
                "started_at": started_at,
                "finished_at": None,
                "stats": None,
                "progress": None,
                "error": None,
                "model_tier": model_tier,
            }

            def _on_progress(p: Dict[str, Any]) -> None:
                # Live-прогресс: пишем снимок в run, чтобы /sync/run/{id} и
                # /sync/runs отдавали «N из total» во время обработки.
                run = _SYNC_RUNS.get(run_id)
                if run is not None:
                    run["progress"] = {**p, "updated_at": datetime.now(timezone.utc).isoformat()}

            try:
                stats = await process_meetings_for_subscription(
                    subscription_id=subscription_id,
                    user_id=resolved_user_id,
                    project_id=subscription.get("project_id"),
                    folder_id=subscription.get("folder_id"),
                    include_subfolders=subscription.get("include_subfolders", True),
                    force=force,
                    model_tier=model_tier,
                    progress_cb=_on_progress,
                    reprocess_older_than=reprocess_older_than,
                )
                _SYNC_RUNS[run_id].update(
                    {
                        "status": "completed",
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                        "stats": stats,
                    }
                )
                # После успешного sync перезагружаем граф в runtime-компонентах чата,
                # чтобы последующие вопросы точно шли по актуальным данным без рестарта сервера.
                try:
                    from backend.api.routes.chat import reload_graph_for_user
                    await reload_graph_for_user(resolved_user_id)
                    logger.info(f"Graph reloaded after sync for user {resolved_user_id}")
                except Exception as re:
                    logger.warning(f"Could not reload graph after sync: {re}")
            except Exception as e:
                logger.error(f"Background sync failed for run_id={run_id}: {e}", exc_info=True)
                _SYNC_RUNS[run_id].update(
                    {
                        "status": "error",
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                        "error": str(e),
                    }
                )

        # Debug mode: wait for completion (может занимать очень долго)
        if wait:
            run_id = str(uuid4())
            await _runner(run_id)
            return {"status": "success", "message": "Sync completed", "run": _SYNC_RUNS.get(run_id)}

        # Default: fire-and-forget (ссылку храним — иначе таску может
        # собрать GC посреди работы, аудит sync #4)
        run_id = str(uuid4())
        task = asyncio.create_task(_runner(run_id))
        _SYNC_TASKS.add(task)
        task.add_done_callback(_SYNC_TASKS.discard)
        return {"status": "success", "message": "Sync started", "run_id": run_id}
    except Exception as e:
        logger.error(f"Error triggering sync: {e}")
        return {"status": "error", "message": str(e)}


@get("/sync/run/{run_id:str}")
async def get_sync_run(
    run_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    """Получить статус конкретного запуска sync"""
    resolved_user_id = get_user_id_from_request(authorization, user_id)
    if not resolved_user_id:
        return {"status": "error", "message": "Authentication required"}

    run = _SYNC_RUNS.get(run_id)
    if not run:
        return {"status": "error", "message": "Run not found"}
    if run.get("user_id") != resolved_user_id:
        return {"status": "error", "message": "Forbidden"}
    return {"status": "success", "run": run}


@get("/sync/runs")
async def list_sync_runs(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    """Список последних sync-запусков юзера (для восстановления прогресса в UI).

    Прогресс синка раньше жил только в React-state и пропадал при уходе со
    страницы. Этот эндпоинт отдаёт текущие/последние run'ы из in-memory
    _SYNC_RUNS, чтобы фронт на маунте восстановил статус и продолжил поллинг
    активных. NB: in-memory → после рестарта backend список пуст (это ок,
    фронт просто покажет «нет активных»).
    """
    resolved_user_id = get_user_id_from_request(authorization, user_id)
    if not resolved_user_id:
        return {"status": "error", "message": "Authentication required"}

    runs = [r for r in _SYNC_RUNS.values() if r.get("user_id") == resolved_user_id]
    # Свежие сверху — по started_at (ISO-строки сравнимы лексикографически).
    runs.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    return {"status": "success", "runs": runs}

@get("/sync/status")
async def get_sync_status(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False)
) -> Dict[str, Any]:
    """Получить общий статус синхронизации"""
    resolved_user_id = get_user_id_from_request(authorization, user_id)
    if not resolved_user_id:
        return {"status": "error", "message": "Authentication required"}

    try:
        supabase = get_supabase_client()

        # ВСЕ подписки (аудит sync #5: раньше фильтр status=active стоял на
        # обоих счётчиках — total всегда равнялся active)
        subscriptions = await supabase._request(
            "GET",
            "/rest/v1/knowledge_sync_subscriptions",
            params={
                "user_id": f"eq.{resolved_user_id}",
                "select": "*"
            }
        )
        active = [s for s in (subscriptions or []) if s.get("status") == "active"]

        total_processed = sum(s.get("meetings_processed", 0) for s in (subscriptions or []))

        return {
            "status": "success",
            "total_subscriptions": len(subscriptions or []),
            "active_subscriptions": len(active),
            "total_meetings_processed": total_processed
        }
    except Exception as e:
        logger.error(f"Error getting sync status: {e}")
        return {"status": "error", "message": str(e)}


@post("/events/emit")
async def emit_event(
    data: EmitEventRequest,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    """Эмитить событие в EventBus для пользователя."""
    resolved_user_id = get_user_id_from_request(authorization, user_id)
    if not resolved_user_id:
        return {"status": "error", "message": "Authentication required"}

    try:
        from backend.core.automations.event_bus import get_event_bus

        bus = get_event_bus()
        event = await bus.emit(
            event_type=data.event_type,
            payload=data.payload or {},
            user_id=resolved_user_id,
            source=data.source or "api_manual",
        )
        return {
            "status": "success",
            "event": {
                "id": event.id,
                "event_type": event.event_type,
                "user_id": event.user_id,
                "source": event.source,
                "timestamp": event.timestamp,
            },
        }
    except Exception as e:
        logger.error(f"Error emitting event: {e}")
        return {"status": "error", "message": str(e)}


# === Router ===

router = Router(
    path="/knowledge-sync",
    route_handlers=[
        list_subscriptions,
        create_subscription,
        update_subscription,
        delete_subscription,
        trigger_sync,
        get_sync_run,
        list_sync_runs,
        get_sync_status,
        emit_event,
    ],
    tags=["Knowledge Sync"],
)
