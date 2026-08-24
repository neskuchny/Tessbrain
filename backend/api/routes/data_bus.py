# -*- coding: utf-8 -*-
"""
Data Bus API — Эндпоинты шины данных Tessbrain.

Два набора эндпоинтов:
1. External API (/data-bus/*) — для потребителей шины (по API ключу)
2. Admin API (/data-bus/admin/*) — для управления шиной (JWT авторизация)
"""
import logging
from typing import Any, Dict, List, Optional

from litestar import Router, get, post
from backend.core.auth.user_guard import enforce_user_id_matches_token
from litestar.params import Parameter
from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════
# Request/Response models
# ══════════════════════════════════════════════

class DataBusQueryRequest(BaseModel):
    """Запрос к шине данных."""
    query: str
    project_id: Optional[str] = None
    data_type: Optional[str] = None  # "tasks", "milestones", "kpis", "people", "projects"
    since: Optional[str] = None      # ISO datetime
    until: Optional[str] = None      # ISO datetime
    search_depth: str = "standard"   # "quick" | "standard" | "deep"
    response_format: str = "natural" # "natural" | "structured" | "both"


class CreateConsumerRequest(BaseModel):
    """Запрос на создание потребителя."""
    name: str
    consumer_type: str               # investor, contractor, partner, ai_agent, internal_dept
    contact_email: str = ""
    policy_template: Optional[str] = None  # investor_readonly, contractor_project, etc.
    allowed_project_ids: Optional[List[str]] = None
    expires_at: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    # P3 cross-org federation: разрешить этому консьюмеру ПИСАТЬ
    # (POST /data-bus/ingest). По умолчанию False — read-only.
    allow_ingest: bool = False
    ingest_max_items_per_request: int = 100
    # СРЕЗ ДАННЫХ: открыть только выбранные встречи/папки (как фильтр чата,
    # но наружу) + промпт владельца «как отвечать этому потребителю».
    allowed_meeting_ids: Optional[List[str]] = None
    allowed_folders: Optional[List[str]] = None
    answer_prompt: str = ""
    # Расширенный поиск (разведка по сводке + дочитывание встреч среза) —
    # тратит токены владельца; лимит запросов в день перекрывает шаблон.
    allow_deep_search: bool = False
    max_queries_per_day: Optional[int] = None


class DataBusIngestRequest(BaseModel):
    """Входящие данные от внешнего партнёра (P3 cross-org federation).

    items: [{type, name, description?, properties?, id?}].
    Запись разрешена только если policy.allow_ingest=True.
    Provenance (`external:partner:<id>`) проставляется сервером.
    """
    items: List[Dict[str, Any]]


class SubscribeRequest(BaseModel):
    """Подписка на события."""
    event_types: List[str]           # ["milestone_reached", "kpi_changed", "task_completed"]
    webhook_url: str                 # URL для push-уведомлений
    project_id: Optional[str] = None


# ══════════════════════════════════════════════
# External API (для потребителей шины — по API ключу)
# ══════════════════════════════════════════════

@get("/query")
async def data_bus_query(
    search_query: str = Parameter(query="query"),
    project_id: Optional[str] = Parameter(query="project_id", default=None),
    data_type: Optional[str] = Parameter(query="data_type", default=None),
    since: Optional[str] = Parameter(query="since", default=None),
    until: Optional[str] = Parameter(query="until", default=None),
    search_depth: str = Parameter(query="search_depth", default="standard"),
    response_format: str = Parameter(query="format", default="natural"),
    x_api_key: Optional[str] = Parameter(header="X-API-Key", default=None),
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None),
    x_forwarded_for: Optional[str] = Parameter(header="X-Forwarded-For", default=None),
) -> Dict[str, Any]:
    """
    Текстовый запрос к шине данных.

    Аутентификация: заголовок X-API-Key или Authorization: Bearer <api_key>

    Поиск: Hybrid Search (Vector + BM25 + Graph → RRF Fusion).
    Ответ: LLM синтезирует человеческий ответ на основе найденных данных.

    format (response_format):
    - natural: ответ на человеческом языке (по умолчанию)
    - structured: JSON массив узлов (для API-интеграций и AI-агентов)
    - both: и человеческий ответ, и данные

    search_depth:
    - quick: top_k=15, быстрый ответ
    - standard: top_k=25, сбалансированный (по умолчанию)
    - deep: top_k=50, максимально полный

    Примеры:
    - GET /api/v1/data-bus/query?query=статус+проекта+Alpha
    - GET /api/v1/data-bus/query?query=мои+задачи&format=structured
    - GET /api/v1/data-bus/query?query=KPI+за+февраль&format=both&search_depth=deep
    """
    # Определяем API ключ
    api_key = x_api_key
    if not api_key and authorization and authorization.startswith("Bearer "):
        api_key = authorization[7:]

    if not api_key:
        return {"error": "API key required. Use X-API-Key header or Authorization: Bearer <key>", "code": 401}

    from backend.core.data_bus.bus_service import get_data_bus_service
    service = get_data_bus_service()

    result = await service.query(
        api_key=api_key,
        query_text=search_query,
        user_id=user_id,
        project_id=project_id,
        data_type=data_type,
        since=since,
        until=until,
        ip_address=x_forwarded_for,
        search_depth=search_depth,
        response_format=response_format,
    )

    return result


@post("/query")
async def data_bus_query_post(
    data: DataBusQueryRequest,
    x_api_key: Optional[str] = Parameter(header="X-API-Key", default=None),
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None),
    x_forwarded_for: Optional[str] = Parameter(header="X-Forwarded-For", default=None),
) -> Dict[str, Any]:
    """POST-версия запроса к шине (для сложных запросов)."""
    api_key = x_api_key
    if not api_key and authorization and authorization.startswith("Bearer "):
        api_key = authorization[7:]

    if not api_key:
        return {"error": "API key required", "code": 401}

    from backend.core.data_bus.bus_service import get_data_bus_service
    service = get_data_bus_service()

    return await service.query(
        api_key=api_key,
        query_text=data.query,
        user_id=user_id,
        project_id=data.project_id,
        data_type=data.data_type,
        since=data.since,
        until=data.until,
        ip_address=x_forwarded_for,
        search_depth=data.search_depth,
        response_format=data.response_format,
    )


@post("/dialog")
async def data_bus_dialog(
    data: Dict[str, Any],
    x_api_key: Optional[str] = Parameter(header="X-API-Key", default=None),
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None),
    x_forwarded_for: Optional[str] = Parameter(header="X-Forwarded-For", default=None),
) -> Dict[str, Any]:
    """Диалог агент↔агент поверх шины: внешний агент задаёт ВОПРОС словами,
    наш агент отвечает СВЯЗНЫМ текстом строго по данным, которые пропустила
    SharingPolicy (+redaction+rate limit+аудит — весь существующий контур
    /query переиспользуется, наружу не уходит ничего сверх политики)."""
    api_key = x_api_key
    if not api_key and authorization and authorization.startswith("Bearer "):
        api_key = authorization[7:]
    if not api_key:
        return {"error": "API key required", "code": 401}
    question = str((data or {}).get("question") or "").strip()
    if not question:
        return {"error": "question required", "code": 400}

    from backend.core.data_bus.bus_service import get_data_bus_service
    service = get_data_bus_service()
    # format="both": нужен и синтез (answer), и данные для контекста ниже.
    res = await service.query(
        api_key=api_key, query_text=question, user_id=user_id,
        project_id=(data or {}).get("project_id"),
        data_type=(data or {}).get("data_type"),
        since=(data or {}).get("since"), until=(data or {}).get("until"),
        ip_address=x_forwarded_for,
        response_format="both")
    if res.get("error"):
        return res
    # Сервис кладёт узлы в "data" (structured/both) — ключа "results" в его
    # ответе никогда не было. Прежний код читал res["results"], всегда
    # получал пустоту и перетирал готовый ответ на «данных не найдено»:
    # заявленный сценарий агент↔агент не работал ни разу.
    results = res.get("data") or []
    if not results:
        return {**res, "answer": "По вашему вопросу разрешённых к передаче "
                                 "данных не найдено."}
    import json as _json
    ctx = _json.dumps(results[:20], ensure_ascii=False)[:12000]
    prompt = ("Ты агент компании, отвечаешь агенту ДРУГОЙ компании. Ответь на "
              "вопрос СТРОГО по данным ниже (они уже отфильтрованы политикой "
              "доступа). Ничего не выдумывай и не добавляй сверх данных; "
              "нет ответа в данных — так и скажи. Кратко, по делу, по-русски."
              f"\n\nВОПРОС: {question}\n\nДАННЫЕ:\n{ctx}")
    try:
        from backend.core.llm.router import get_llm_router
        answer = await get_llm_router().generate(prompt, personalize=False)
    except Exception as e:
        return {**res, "answer": None, "error": f"answer synth failed: {e}"}
    return {**res, "answer": (answer or "").strip()}


@post("/ingest")
async def data_bus_ingest(
    data: DataBusIngestRequest,
    x_api_key: Optional[str] = Parameter(header="X-API-Key", default=None),
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    x_forwarded_for: Optional[str] = Parameter(header="X-Forwarded-For", default=None),
) -> Dict[str, Any]:
    """
    Принять данные от внешнего партнёра (двунаправленная федерация, P3).

    Аутентификация: X-API-Key или Authorization: Bearer <api_key>.

    Запись в граф разрешена ТОЛЬКО если SharingPolicy.allow_ingest=True
    (по умолчанию False → 403, все существующие консьюмеры read-only).
    Каждый принятый узел метится provenance `external:partner:<consumer_id>`
    и изолируется в namespace (tenant) консьюмера. Частичный успех — норма.

    Пример: POST /api/v1/data-bus/ingest
      {"items": [{"type": "Project", "name": "Joint Venture Alpha",
                  "description": "...", "properties": {"status": "active"}}]}
    """
    api_key = x_api_key
    if not api_key and authorization and authorization.startswith("Bearer "):
        api_key = authorization[7:]

    if not api_key:
        return {"error": "API key required", "code": 401}

    from backend.core.data_bus.bus_service import get_data_bus_service
    service = get_data_bus_service()

    return await service.ingest(
        api_key=api_key,
        items=data.items,
        ip_address=x_forwarded_for,
    )


@get("/snapshot/{entity_type:str}/{entity_id:str}")
async def data_bus_snapshot(
    entity_type: str,
    entity_id: str,
    level: str = Parameter(query="level", default="summary"),
    x_api_key: Optional[str] = Parameter(header="X-API-Key", default=None),
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None),
    x_forwarded_for: Optional[str] = Parameter(header="X-Forwarded-For", default=None),
) -> Dict[str, Any]:
    """
    Снэпшот сущности через шину.

    Примеры:
    - GET /api/v1/data-bus/snapshot/project/alpha?level=summary
    - GET /api/v1/data-bus/snapshot/product/tessent-brain?level=snippet
    - GET /api/v1/data-bus/snapshot/company/main?level=full
    """
    api_key = x_api_key
    if not api_key and authorization and authorization.startswith("Bearer "):
        api_key = authorization[7:]

    if not api_key:
        return {"error": "API key required", "code": 401}

    from backend.core.data_bus.bus_service import get_data_bus_service
    service = get_data_bus_service()

    return await service.get_snapshot(
        api_key=api_key,
        entity_type=entity_type,
        entity_id=entity_id,
        level=level,
        user_id=user_id,
        ip_address=x_forwarded_for,
    )


@get("/health")
async def data_bus_health() -> Dict[str, Any]:
    """Health check шины данных (без аутентификации)."""
    return {
        "status": "ok",
        "service": "tessent-data-bus",
        "version": "1.0.0",
    }


# ══════════════════════════════════════════════
# Webhook-каналы: шина сама сообщает «появилось новое»
# ══════════════════════════════════════════════

class CreateWebhookRequest(BaseModel):
    consumer_id: str
    endpoint_url: str
    events: List[str] = []


@post("/admin/webhooks")
async def admin_create_webhook(
    data: CreateWebhookRequest,
    user_id: str = Parameter(query="user_id", required=True),
) -> Dict[str, Any]:
    """Зарегистрировать webhook-канал потребителя.

    Пуш не несёт данных — только «появилось новое, приходите за срезом».
    Секрет подписи возвращается ОДИН раз. В закрытом контуре внешние
    адреса отвергаются: уведомление — тоже трафик наружу.
    """
    from backend.core.data_bus.bus_service import get_data_bus_service
    from backend.core.data_bus.webhook_push import WebhookStore, new_channel

    consumer = await get_data_bus_service().storage.get_consumer(data.consumer_id)
    if not consumer or str(consumer.tenant_id) != str(user_id):
        return {"status": "not_found",
                "error": "потребитель не найден в вашей организации"}
    res = new_channel(tenant_id=user_id, consumer_id=data.consumer_id,
                      endpoint_url=data.endpoint_url, events=data.events)
    if not res.get("ok"):
        return {"status": "rejected", "error": res["error"]}
    WebhookStore(user_id).save(res["channel"])
    return {"status": "created", "channel": res["channel"].to_dict(),
            "secret": res["secret"],
            "note": "секрет показан один раз — дальше хранится только хэш"}


@get("/admin/webhooks")
async def admin_list_webhooks(
    user_id: str = Parameter(query="user_id", required=True),
) -> Dict[str, Any]:
    from backend.core.data_bus.webhook_push import WebhookStore
    channels = WebhookStore(user_id).list()
    return {"channels": channels, "total": len(channels)}


@post("/admin/webhooks/{channel_id:str}/delete")
async def admin_delete_webhook(
    channel_id: str,
    user_id: str = Parameter(query="user_id", required=True),
) -> Dict[str, Any]:
    from backend.core.data_bus.webhook_push import WebhookStore
    ok = WebhookStore(user_id).delete(channel_id)
    return {"status": "deleted" if ok else "not_found"}


# ══════════════════════════════════════════════
# Admin API (для управления шиной — JWT)
# ══════════════════════════════════════════════

@post("/admin/consumers")
async def admin_create_consumer(
    data: CreateConsumerRequest,
    user_id: str = Parameter(query="user_id", required=True),
) -> Dict[str, Any]:
    """
    Создать нового потребителя шины данных.

    Возвращает API ключ — СОХРАНИТЕ! Повторно не выдаётся.

    Шаблоны политик:
    - investor_readonly: Инвестор (проекты, KPI, вехи — без персональных данных)
    - contractor_project: Подрядчик (задачи своего проекта)
    - ai_agent_analysis: AI-агент (структура, отделы, продукты)
    - internal_department: Внутренний отдел (всё, ограничено access_level)
    - partner_public: Партнёр (только public данные)
    """
    from backend.core.data_bus.bus_service import get_data_bus_service
    service = get_data_bus_service()

    result = await service.create_consumer(
        tenant_id=user_id,
        name=data.name,
        consumer_type=data.consumer_type,
        contact_email=data.contact_email,
        policy_template=data.policy_template,
        allowed_project_ids=data.allowed_project_ids,
        expires_at=data.expires_at,
        metadata=data.metadata,
        allow_ingest=data.allow_ingest,
        ingest_max_items_per_request=data.ingest_max_items_per_request,
        allowed_meeting_ids=data.allowed_meeting_ids,
        allowed_folders=data.allowed_folders,
        answer_prompt=data.answer_prompt,
        allow_deep_search=data.allow_deep_search,
        max_queries_per_day=data.max_queries_per_day,
    )

    return result


@get("/admin/consumers")
async def admin_list_consumers(
    user_id: str = Parameter(query="user_id", required=True),
) -> Dict[str, Any]:
    """Список всех потребителей шины данных."""
    from backend.core.data_bus.bus_service import get_data_bus_service
    service = get_data_bus_service()

    consumers = await service.list_consumers(user_id)

    return {
        "consumers": consumers,
        "total": len(consumers),
    }


@post("/admin/consumers/{consumer_id:str}/deactivate")
async def admin_deactivate_consumer(
    consumer_id: str,
    user_id: str = Parameter(query="user_id", required=True),
) -> Dict[str, Any]:
    """Деактивировать потребителя."""
    from backend.core.data_bus.bus_service import get_data_bus_service
    service = get_data_bus_service()

    success = await service.deactivate_consumer(consumer_id, tenant_id=user_id)

    return {
        "status": "success" if success else "not_found",
        "consumer_id": consumer_id,
    }


@post("/admin/consumers/{consumer_id:str}/rotate-key")
async def admin_rotate_key(
    consumer_id: str,
    user_id: str = Parameter(query="user_id", required=True),
) -> Dict[str, Any]:
    """
    Ротировать API ключ потребителя.

    Старый ключ перестаёт работать. Новый возвращается ОДИН раз.
    """
    from backend.core.data_bus.bus_service import get_data_bus_service
    service = get_data_bus_service()

    result = await service.rotate_api_key(consumer_id, tenant_id=user_id)

    if result:
        return {"status": "success", **result}
    return {"status": "not_found", "error": f"Consumer {consumer_id} not found"}


@get("/admin/consumers/{consumer_id:str}/stats")
async def admin_consumer_stats(
    consumer_id: str,
    user_id: str = Parameter(query="user_id", required=True),
) -> Dict[str, Any]:
    """Статистика потребителя (использование, лимиты)."""
    from backend.core.data_bus.bus_service import get_data_bus_service
    service = get_data_bus_service()

    return await service.get_consumer_stats(consumer_id)


@get("/admin/audit")
async def admin_audit_log(
    user_id: str = Parameter(query="user_id", required=True),
    consumer_id: Optional[str] = Parameter(query="consumer_id", default=None),
    limit: int = Parameter(query="limit", default=50),
    since: Optional[str] = Parameter(query="since", default=None),
) -> Dict[str, Any]:
    """Аудит-лог шины данных."""
    from backend.core.data_bus.bus_service import get_data_bus_service
    service = get_data_bus_service()

    entries = await service.get_audit_log(
        tenant_id=user_id,
        consumer_id=consumer_id,
        limit=limit,
        since=since,
    )

    return {
        "entries": entries,
        "total": len(entries),
    }


@get("/admin/policies")
async def admin_list_policy_templates() -> Dict[str, Any]:
    """Список доступных шаблонов политик."""
    from backend.core.data_bus.models import POLICY_TEMPLATES

    templates = {}
    for key, policy in POLICY_TEMPLATES.items():
        templates[key] = {
            "name": policy.name,
            "description": policy.description,
            "allowed_node_types": policy.allowed_node_types,
            "blocked_node_types": policy.blocked_node_types,
            "max_access_level": policy.max_access_level,
            "max_queries_per_day": policy.max_queries_per_day,
            "max_queries_per_hour": policy.max_queries_per_hour,
        }

    return {"templates": templates}


@post("/admin/consumers/{consumer_id:str}/test")
async def admin_test_consumer(
    consumer_id: str,
    data: DataBusQueryRequest,
    user_id: str = Parameter(query="user_id", required=True),
) -> Dict[str, Any]:
    """
    Тестовый запрос от имени потребителя.

    Позволяет администратору проверить, что потребитель видит
    правильные данные (с учётом политики).
    """
    from backend.core.data_bus.bus_service import get_data_bus_service
    service = get_data_bus_service()

    consumer = await service.storage.get_consumer(consumer_id)
    if not consumer:
        return {"error": f"Consumer {consumer_id} not found", "code": 404}

    # Ищем ключ по хешу (для тестовых запросов ищем напрямую)
    # Для тестирования используем внутренний вызов
    policy = await service.storage.get_policy(consumer.sharing_policy_id)
    if not policy:
        return {"error": "Policy not found", "code": 404}

    consumer.sharing_policy = policy

    # Выполняем поиск напрямую (без rate limiting и аутентификации)
    try:
        raw_results = await service._search_brain(
            query=data.query,
            user_id=user_id,
            policy=policy,
            project_id=data.project_id,
            data_type=data.data_type,
        )

        filtered_results, redacted_count = service.redaction.redact_results(raw_results, policy)

        return {
            "test_mode": True,
            "consumer": consumer.name,
            "policy": policy.name,
            "data": filtered_results,
            "meta": {
                "raw_results": len(raw_results),
                "filtered_results": len(filtered_results),
                "redacted_count": redacted_count,
            }
        }
    except Exception as e:
        return {"error": str(e), "code": 500}


# ══════════════════════════════════════════════
# Router
# ══════════════════════════════════════════════

# ══════════════════════════════════════════════
# Ресурсы компании: сохранённые ссылки (сайт/прайс/шаблоны) — хранить,
# смотреть и отдавать системе в работу (контекст генерации ТЗ/документов).
# user_id сверяется с токеном router-guard'ом.
# ══════════════════════════════════════════════

class ResourceAddRequest(BaseModel):
    url: str
    title: Optional[str] = None
    note: Optional[str] = None
    kind: Optional[str] = None   # link | price | template | page | doc


@get("/resources")
async def resources_list(
    user_id: str = Parameter(query="user_id", required=True),
) -> Dict[str, Any]:
    """Список сохранённых ссылок пользователя."""
    from backend.core.store.user_resources import list_resources
    return {"resources": list_resources(user_id)}


@post("/resources")
async def resources_add(
    data: ResourceAddRequest,
    user_id: str = Parameter(query="user_id", required=True),
) -> Dict[str, Any]:
    """Сохранить ссылку (дубль по url — обновление)."""
    from backend.core.store.user_resources import add_resource, list_resources
    rec = add_resource(user_id, url=data.url, title=data.title or "",
                       note=data.note or "", kind=data.kind or "link")
    if rec is None:
        return {"status": "error", "message": "url обязателен"}
    return {"status": "ok", "resource": rec, "resources": list_resources(user_id)}


@post("/resources/{resource_id:str}/delete")
async def resources_delete(
    resource_id: str,
    user_id: str = Parameter(query="user_id", required=True),
) -> Dict[str, Any]:
    """Удалить ссылку."""
    from backend.core.store.user_resources import delete_resource, list_resources
    ok = delete_resource(user_id, resource_id)
    return {"status": "ok" if ok else "error", "resources": list_resources(user_id)}


data_bus_router = Router(
    path="/data-bus",
    guards=[enforce_user_id_matches_token],
    route_handlers=[
        data_bus_dialog,
        # External API
        data_bus_query,
        data_bus_query_post,
        data_bus_ingest,
        data_bus_snapshot,
        data_bus_health,
        # Ресурсы компании (ссылки)
        resources_list,
        resources_add,
        resources_delete,
        # Admin API
        admin_create_consumer,
        admin_list_consumers,
        admin_deactivate_consumer,
        admin_rotate_key,
        admin_consumer_stats,
        admin_audit_log,
        admin_list_policy_templates,
        admin_test_consumer,
        # Webhook push
        admin_create_webhook,
        admin_list_webhooks,
        admin_delete_webhook,
    ],
    tags=["Data Bus"],
)
