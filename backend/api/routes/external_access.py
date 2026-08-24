# -*- coding: utf-8 -*-
"""
External Access API - API для внешнего доступа подрядчиков.

Позволяет инвесторам, маркетинговым агентствам, аудиторам и другим
подрядчикам получать информацию о компании с автоматической фильтрацией.
"""
from typing import Any, Dict, List, Optional

import structlog
from litestar import Request, Router, get, post
from litestar.params import Parameter


def _verify_owner(request: Request, user_id: str) -> Optional[str]:
    """Анти-IDOR: ?user_id= должен совпадать с user_id из верифицированного
    токена. Возвращает строку-причину отказа либо None если ok."""
    try:
        from backend.core.auth.service_token import trusted_user_id
        uid, src = trusted_user_id(request.headers, user_id or "")
        if src != "unverified" and uid and uid != user_id:
            return "token not authorized for requested user_id"
    except PermissionError:
        return "token not authorized for requested user_id"
    except Exception:
        pass
    return None
from pydantic import BaseModel

logger = structlog.get_logger(__name__)


# Request/Response models
class ExternalQueryRequest(BaseModel):
    """Запрос на получение данных."""
    query: str
    categories: Optional[List[str]] = None
    format: str = "json"  # json | text | markdown


class ExternalAskRequest(BaseModel):
    """Вопрос своими словами (в границах разрешённых категорий)."""
    question: str
    categories: Optional[List[str]] = None


class ContractorRegistrationRequest(BaseModel):
    """Запрос на регистрацию подрядчика."""
    id: str
    name: str
    contractor_type: str  # investor | marketing | auditor | hr_partner | legal
    api_key: str
    allowed_categories: List[str]
    allowed_meeting_ids: Optional[List[str]] = None
    max_requests_per_hour: int = 100
    expires_at: Optional[str] = None


# Endpoints
@post("/query")
async def external_query(
    data: ExternalQueryRequest,
    authorization: str = Parameter(header="Authorization"),
    user_id: str = Parameter(query="user_id", required=True),
    x_forwarded_for: Optional[str] = Parameter(header="X-Forwarded-For", default=None),
    user_agent: Optional[str] = Parameter(header="User-Agent", default=None),
) -> Dict[str, Any]:
    """
    Выполнить поисковый запрос от подрядчика.

    Требует заголовок Authorization: Bearer <api_key>

    Args:
        data: Параметры запроса
        authorization: API ключ в формате "Bearer <key>"
        user_id: ID пользователя-владельца данных

    Returns:
        Отфильтрованные данные
    """
    logger.info("External query", query=data.query[:50], user_id=user_id)

    # Извлекаем API ключ
    if not authorization.startswith("Bearer "):
        return {
            "error": "Invalid authorization format. Use: Bearer <api_key>",
            "code": 401
        }

    api_key = authorization[7:]

    try:
        from backend.core.external_access import get_access_gateway
        from backend.core.sleep.enhanced_snapshot import get_enhanced_snapshot_generator
        from backend.core.store.graph_builder import GraphBuilder
        from backend.core.store.tenant_paths import graph_path_for_user

        # Инициализируем компоненты
        graph_builder = GraphBuilder(
            use_networkx=None,  # Auto-detect backend
            graph_storage_path=graph_path_for_user(user_id)
        )
        await graph_builder.connect()

        snapshot_gen = get_enhanced_snapshot_generator(graph_builder, user_id=user_id)

        gateway = get_access_gateway(
            graph_builder=graph_builder,
            snapshot_generator=snapshot_gen
        )

        # S3-EXTERNAL: API-ключ должен быть привязан к запрошенному
        # user_id-владельцу. Без этой проверки любой ключ работал бы по
        # любому user_id из query — даже когда подрядчик зарегистрирован
        # другим клиентом мозга.
        contractor = await gateway.authenticate(api_key)
        if contractor and not contractor.is_authorized_for(user_id):
            logger.warning(
                "external_query: api_key %s not authorized for user_id %s",
                contractor.id, user_id,
            )
            return {"error": "api_key not authorized for requested user_id",
                    "code": 403}

        # Обрабатываем запрос
        response = await gateway.process_request(
            api_key=api_key,
            query=data.query,
            categories=data.categories,
            user_id=user_id,
            ip_address=x_forwarded_for,
            user_agent=user_agent
        )

        if response.error:
            return {
                "error": response.error,
                "code": 401 if "Authentication" in response.error else 429
            }

        # Форматируем ответ
        if data.format == "text":
            return {
                "request_id": response.request_id,
                "format": "text",
                "content": _format_as_text(response.data),
                "categories_accessed": response.categories_accessed,
                "items_returned": response.items_returned,
                "items_filtered": response.items_filtered,
                "audit_log_id": response.audit_log_id
            }

        return response.to_dict()

    except Exception as e:
        logger.error(f"External query error: {e}")
        return {
            "error": str(e),
            "code": 500
        }


@post("/ask")
async def external_ask(
    data: ExternalAskRequest,
    authorization: str = Parameter(header="Authorization"),
    user_id: str = Parameter(query="user_id", required=True),
    x_forwarded_for: Optional[str] = Parameter(header="X-Forwarded-For", default=None),
    user_agent: Optional[str] = Parameter(header="User-Agent", default=None),
) -> Dict[str, Any]:
    """Спросить мозг своими словами — в границах, заданных владельцем.

    Тот же путь к данным, что у /query: аутентификация, лимит частоты,
    разрешённые подрядчику категории, маскирование, аудит. Разница только в
    том, что результат ещё и пересказывается связным текстом — и только по
    тому, что уже прошло фильтр. Выключено по умолчанию (ENABLE_EXTERNAL_ASK):
    связный ответ дополнительно отправляет содержимое в модель, и это решение
    владельца, а не наше.
    """
    logger.info("External ask", question=data.question[:50], user_id=user_id)

    if not authorization.startswith("Bearer "):
        return {"error": "Invalid authorization format. Use: Bearer <api_key>",
                "code": 401}
    api_key = authorization[7:]

    try:
        from backend.core.external_access import get_access_gateway
        from backend.core.sleep.enhanced_snapshot import get_enhanced_snapshot_generator
        from backend.core.store.graph_builder import GraphBuilder
        from backend.core.store.tenant_paths import graph_path_for_user

        graph_builder = GraphBuilder(
            use_networkx=None,
            graph_storage_path=graph_path_for_user(user_id),
        )
        await graph_builder.connect()
        snapshot_gen = get_enhanced_snapshot_generator(graph_builder, user_id=user_id)
        gateway = get_access_gateway(graph_builder=graph_builder,
                                     snapshot_generator=snapshot_gen)

        # Та же привязка ключа к владельцу данных, что и в /query.
        contractor = await gateway.authenticate(api_key)
        if contractor and not contractor.is_authorized_for(user_id):
            logger.warning("external_ask: api_key %s not authorized for user_id %s",
                           contractor.id, user_id)
            return {"error": "api_key not authorized for requested user_id",
                    "code": 403}

        response = await gateway.process_question(
            api_key=api_key,
            question=data.question,
            categories=data.categories,
            user_id=user_id,
            ip_address=x_forwarded_for,
            user_agent=user_agent,
        )
        if response.error:
            return {"error": response.error,
                    "code": 401 if "Authentication" in response.error else 429}
        return response.to_dict()

    except Exception as e:
        logger.error(f"External ask error: {e}")
        return {"error": str(e), "code": 500}


@get("/resources")
async def list_resources(
    authorization: str = Parameter(header="Authorization"),
) -> Dict[str, Any]:
    """
    Получить список доступных ресурсов для подрядчика.

    Returns:
        Список ресурсов с описаниями
    """
    # Извлекаем API ключ
    if not authorization.startswith("Bearer "):
        return {"error": "Invalid authorization", "code": 401}

    api_key = authorization[7:]

    try:
        from backend.core.external_access import get_access_gateway

        gateway = get_access_gateway()
        contractor = await gateway.authenticate(api_key)

        if not contractor:
            return {"error": "Authentication failed", "code": 401}

        # Формируем список ресурсов
        resources = []

        resource_descriptions = {
            "company_overview": "Общая информация о компании",
            "product_description": "Описание продуктов и услуг",
            "team_overview": "Обзор команды и структуры",
            "project_progress": "Статус и прогресс проектов",
            "risks_overview": "Обзор рисков и проблем",
            "achievements": "Достижения компании",
            "decisions": "Принятые решения",
            "metrics": "Ключевые метрики",
            "processes": "Бизнес-процессы",
            "regulations": "Регламенты и политики",
        }

        for category in contractor.allowed_categories:
            resources.append({
                "uri": f"data://{category}",
                "name": category.replace("_", " ").title(),
                "description": resource_descriptions.get(category, ""),
                "access_level": "granted"
            })

        return {
            "contractor_id": contractor.id,
            "contractor_type": contractor.type.value,
            "resources": resources,
            "total": len(resources)
        }

    except Exception as e:
        logger.error(f"List resources error: {e}")
        return {"error": str(e), "code": 500}


@get("/audit")
async def get_audit_logs(
    request: Request,
    user_id: str = Parameter(query="user_id", required=True),
    contractor_id: Optional[str] = Parameter(query="contractor_id", default=None),
    limit: int = Parameter(query="limit", default=50),
) -> Dict[str, Any]:
    """Логи аудита для владельца данных (только свои)."""
    deny = _verify_owner(request, user_id)
    if deny:
        return {"error": deny, "code": 403}
    logger.info("Audit logs requested", user_id=user_id, contractor_id=contractor_id)

    try:
        from backend.core.external_access import get_audit_logger

        audit_logger = get_audit_logger()
        entries = audit_logger.get_entries(
            contractor_id=contractor_id,
            limit=limit
        )

        return {
            "entries": [e.to_dict() for e in entries],
            "total": len(entries),
            "stats": audit_logger.get_stats(contractor_id)
        }

    except Exception as e:
        logger.error(f"Audit logs error: {e}")
        return {"error": str(e), "code": 500}


@get("/audit/anomalies")
async def get_audit_anomalies(
    request: Request,
    user_id: str = Parameter(query="user_id", required=True),
) -> Dict[str, Any]:
    """Аномалии в запросах (только для владельца данных)."""
    deny = _verify_owner(request, user_id)
    if deny:
        return {"error": deny, "code": 403}
    try:
        from backend.core.external_access import get_audit_logger

        audit_logger = get_audit_logger()
        anomalies = audit_logger.detect_anomalies()

        return {
            "anomalies": anomalies,
            "total": len(anomalies)
        }

    except Exception as e:
        logger.error(f"Anomalies detection error: {e}")
        return {"error": str(e), "code": 500}


@post("/contractors/register")
async def register_contractor(
    data: ContractorRegistrationRequest,
    request: Request,
    user_id: str = Parameter(query="user_id", required=True),
) -> Dict[str, Any]:
    """
    Зарегистрировать нового подрядчика.

    Только для владельца данных: создаваемый ключ автоматически привязан к
    user_id из верифицированного токена (или query, если токена нет —
    закрывается enable_strict_chat_auth). API-ключ дальше работает ТОЛЬКО
    с данными этого владельца.
    """
    # анти-IDOR при регистрации (как в sessions/documents/knowledge_sync)
    try:
        from backend.core.auth.service_token import trusted_user_id
        uid, src = trusted_user_id(request.headers, user_id)
        owner = uid if (src != "unverified" and uid) else user_id
    except PermissionError:
        return {"error": "token not authorized for requested user_id", "code": 403}
    except Exception:
        owner = user_id

    logger.info("Registering contractor", contractor_id=data.id,
                type=data.contractor_type, owner=owner)

    try:
        from backend.core.external_access import get_access_gateway

        gateway = get_access_gateway()
        contractor = gateway.register_contractor(
            id=data.id,
            name=data.name,
            contractor_type=data.contractor_type,
            api_key=data.api_key,
            allowed_categories=data.allowed_categories,
            allowed_meeting_ids=data.allowed_meeting_ids,
            allowed_user_ids=[owner],
            max_requests_per_hour=data.max_requests_per_hour,
            expires_at=data.expires_at
        )

        return {
            "status": "success",
            "contractor": contractor.to_dict(),
            "message": f"Contractor {contractor.id} registered successfully"
        }

    except Exception as e:
        logger.error(f"Registration error: {e}")
        return {"error": str(e), "code": 500}


@post("/contractors/{contractor_id:str}/deactivate")
async def deactivate_contractor(
    contractor_id: str,
    request: Request,
    user_id: str = Parameter(query="user_id", required=True),
) -> Dict[str, Any]:
    """Деактивировать подрядчика. Только владелец данных."""
    deny = _verify_owner(request, user_id)
    if deny:
        return {"error": deny, "code": 403}
    logger.info("🚫 Deactivating contractor", contractor_id=contractor_id)

    try:
        from backend.core.external_access import get_access_gateway

        gateway = get_access_gateway()
        # дополнительная проверка: ключ привязан к этому владельцу
        contractor = gateway.contractors.get(contractor_id)
        if contractor and contractor.allowed_user_ids and \
                user_id not in contractor.allowed_user_ids:
            return {"error": "not your contractor", "code": 403}
        success = gateway.deactivate_contractor(contractor_id)

        if success:
            return {
                "status": "success",
                "message": f"Contractor {contractor_id} deactivated"
            }
        else:
            return {
                "status": "error",
                "message": f"Contractor {contractor_id} not found"
            }

    except Exception as e:
        logger.error(f"Deactivation error: {e}")
        return {"error": str(e), "code": 500}


@get("/categories")
async def list_categories(
    contractor_type: str = Parameter(query="type", required=True),
) -> Dict[str, Any]:
    """
    Получить доступные категории для типа подрядчика.

    Args:
        contractor_type: Тип подрядчика (investor, marketing, etc.)

    Returns:
        Список категорий
    """
    try:
        from backend.core.external_access import get_access_gateway

        gateway = get_access_gateway()
        categories = gateway.get_available_categories(contractor_type)

        return {
            "contractor_type": contractor_type,
            "categories": categories,
            "total": len(categories)
        }

    except Exception as e:
        logger.error(f"Categories error: {e}")
        return {"error": str(e), "code": 500}


def _format_as_text(data: Dict[str, Any], indent: int = 0) -> str:
    """Форматировать данные как текст."""
    lines = []
    prefix = "  " * indent

    for key, value in data.items():
        title = key.replace("_", " ").title()

        if isinstance(value, dict):
            lines.append(f"{prefix}## {title}")
            lines.append(_format_as_text(value, indent + 1))
        elif isinstance(value, list):
            lines.append(f"{prefix}## {title}")
            for item in value:
                if isinstance(item, dict):
                    for k, v in item.items():
                        lines.append(f"{prefix}  - {k}: {v}")
                    lines.append("")
                else:
                    lines.append(f"{prefix}  - {item}")
        else:
            lines.append(f"{prefix}{title}: {value}")

    return "\n".join(lines)


# Router
external_access_router = Router(
    path="/external",
    route_handlers=[
        external_query,
        external_ask,
        list_resources,
        get_audit_logs,
        get_audit_anomalies,
        register_contractor,
        deactivate_contractor,
        list_categories,
    ],
    tags=["External Access"]
)

