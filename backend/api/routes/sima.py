"""
TESSENT BRAIN - SIMA API Routes
Маршруты для Системы Интерактивного Мышления и Анализа (SIMA)
Все endpoints под /api/v1/sima/
"""
from typing import Any, Optional

import structlog
from litestar import Router, delete, get, patch, post
from litestar.exceptions import HTTPException
from litestar.params import Body, Parameter

from backend.api.middleware.auth_middleware import get_user_id_from_token
from backend.core.llm.router import ModelTier, TaskType
from backend.db.sima_client import get_sima_db

logger = structlog.get_logger(__name__)


DEFAULT_DEV_USER_ID = "00000000-0000-0000-0000-000000000001"


def get_user_id_from_request(request: Any) -> str:
    """Извлечь user_id из JWT токена в заголовке Authorization объекта request.
    Если токена нет — используется dev user_id для разработки."""
    authorization = request.headers.get("authorization") or request.headers.get("Authorization")
    user_id = get_user_id_from_token(authorization)
    return user_id or DEFAULT_DEV_USER_ID


async def _require_project_access(request: Any, project_id: str,
                                  *, write: bool = False) -> Optional[str]:
    """Анти-IDOR SIMA: вернуть user_id, если он ВЛАДЕЕТ проектом (или есть
    подходящий грант); иначе None. Раньше enumeration-хендлеры отдавали данные
    по любому projectId без проверки владельца — «данные из другого аккаунта».

    write=False → достаточно read-гранта (чтение). write=True → мутация:
    только владелец или явный write/edit-грант; bare read НЕ пропускает
    (иначе любой с ссылкой правил бы чужой проект).

    db.get_project(pid, uid) уже возвращает None, если проект не принадлежит
    uid; при отсутствии — проверяем грант (как в get_project)."""
    if not project_id:
        return None
    try:
        from backend.db.sima_client import get_sima_db
        user_id = get_user_id_from_request(request)
        db = await get_sima_db()
        if await db.get_project(project_id, user_id):
            return user_id  # владелец
        # грант на чужой проект? для мутаций нужен write/edit, не read
        try:
            from backend.core.auth.resource_permissions import check
            owner = await db.get_project(project_id)
            if owner:
                action = "write" if write else "read"
                res = await check(user_id, action, resource_type="project",
                                  resource_id=project_id,
                                  owner_id=str(owner.get("userId") or ""))
                if getattr(res, "allowed", False) or res is True:
                    return user_id
        except Exception:
            pass
        logger.warning(f"[SIMA] anti-IDOR: user {user_id[:8]}… нет "
                       f"{'write-' if write else ''}доступа к project "
                       f"{str(project_id)[:8]}… — отказ")
        return None
    except Exception:
        logger.debug("[SIMA] _require_project_access failed", exc_info=True)
        return None


async def _project_id_of_block(db: Any, block_id: str) -> Optional[str]:
    """project_id блока по его id (для проверки доступа на update/delete,
    где приходит только blockId). None если блок не найден."""
    if not block_id:
        return None
    try:
        from sqlalchemy import text as _sql
        async with db.pg.session() as session:
            row = (await session.execute(
                _sql("SELECT project_id FROM sima_blocks WHERE id = :id"),
                {"id": block_id})).fetchone()
        return str(row._mapping["project_id"]) if row else None
    except Exception:
        logger.debug("[SIMA] _project_id_of_block failed", exc_info=True)
        return None


async def _project_id_of_connection(db: Any, conn_id: str) -> Optional[str]:
    """project_id связи по её id (для update/delete связи)."""
    if not conn_id:
        return None
    try:
        from sqlalchemy import text as _sql
        async with db.pg.session() as session:
            row = (await session.execute(
                _sql("SELECT project_id FROM sima_connections WHERE id = :id"),
                {"id": conn_id})).fetchone()
        return str(row._mapping["project_id"]) if row else None
    except Exception:
        logger.debug("[SIMA] _project_id_of_connection failed", exc_info=True)
        return None


async def _persist_synthesis_artifact(
    user_id: str, project_id: str, *, atype: str, title: str,
    markdown: str, source_ids: Optional[list] = None) -> Optional[str]:
    """Сохранить результат синтеза (ТЗ/нарратив/…) как артефакт библиотеки.

    «Кубик = атом»: синтезированное должно само становиться переиспользуемым
    кубиком, а не жить только в поле проекта. Best-effort, never-raise —
    провал персиста не должен ломать саму генерацию. Возвращает id артефакта
    или None."""
    if not (user_id and project_id and (markdown or "").strip()):
        return None
    try:
        from backend.db.sima_client import get_sima_db
        db = await get_sima_db()
        artifact = await db.create_artifact(user_id, {
            "artifactType": atype,
            "title": title[:200],
            "description": f"Синтез {atype} из проекта",
            "tags": [f"#{atype}", "#синтез"],
            "payload": {"markdown": markdown,
                        "sourceIds": list(source_ids or [])},
            "sourceProjectId": project_id,
        })
        return str((artifact or {}).get("id") or "") or None
    except Exception:
        logger.debug("[SIMA] persist synthesis artifact failed", exc_info=True)
        return None


# ═══════════════════════════════════════════
# Projects
# ═══════════════════════════════════════════

@get("/projects")
async def list_projects(request: Any) -> list[dict]:
    """Список проектов пользователя + проекты, которыми с ним ПОДЕЛИЛИСЬ
    коллеги (P1 мульти-аккаунта: грант read/write на project → проект виден
    в списке с пометкой sharedWithMe). Never-raise по shared-части."""
    from backend.db.sima_client import get_sima_db
    user_id = get_user_id_from_request(request)
    logger.info(f"[SIMA] list_projects called, user_id={user_id}", flush=True)
    db = await get_sima_db()
    result = await db.list_projects(user_id)

    # «Доступные мне»: обратный lookup грантов (без Postgres — просто пусто).
    try:
        from backend.core.auth.resource_permissions import fetch_grants_for_grantee
        own_ids = {str(p.get("id")) for p in result}
        grants = await fetch_grants_for_grantee(user_id, resource_type="project")
        for g in grants[:20]:
            pid = str(g.get("resource_id") or "")
            if not pid or pid in own_ids:
                continue
            own_ids.add(pid)  # дедуп при нескольких грантах на один проект
            proj = await db.get_project(pid)  # без user-фильтра: доступ уже дал грант
            if not proj:
                continue
            proj["sharedWithMe"] = True
            proj["accessLevel"] = g.get("permission_type") or "read"
            result.append(proj)
    except Exception:
        logger.debug("[SIMA] shared-with-me lookup skipped", exc_info=True)

    logger.info(f"[SIMA] list_projects returning {len(result)} projects", flush=True)
    return result


@post("/projects")
async def create_project(request: Any, data: dict = Body()) -> dict:
    """Создать проект."""
    from backend.db.sima_client import get_sima_db
    user_id = get_user_id_from_request(request)
    db = await get_sima_db()
    return await db.create_project(user_id, data)


@get("/projects/{project_id:str}")
async def get_project(request: Any, project_id: str) -> dict:
    """Получить проект со всеми данными.

    S2 (ACCESS_CONTROL_DESIGN): не владелец, но есть read-грант на
    project → проект читается. Аддитивно: без грантов поведение прежнее
    («не найден»); невидимое неотличимо от несуществующего."""
    from backend.db.sima_client import get_sima_db
    user_id = get_user_id_from_request(request)
    db = await get_sima_db()
    project = await db.get_project(project_id, user_id)
    if not project:
        try:
            from backend.core.auth.resource_permissions import check
            owner_project = await db.get_project(project_id)
            if owner_project:
                res = await check(
                    user_id, "read", resource_type="project",
                    resource_id=project_id,
                    owner_id=str(owner_project.get("userId") or ""))
                if res["allowed"]:
                    owner_project["access_via"] = res["via"]
                    owner_project["access_level"] = res["level"]
                    return owner_project
        except Exception:
            logger.debug("project grant check skipped", exc_info=True)
        return {"error": "Project not found"}
    return project


@patch("/projects/{project_id:str}")
async def update_project(request: Any, project_id: str, data: dict = Body()) -> dict:
    """Обновить проект. P1 мульти-аккаунта: не-владелец с write-грантом тоже
    может править (иначе грант «правка» из «Поделиться» был бы пустым)."""
    from backend.core.auth.shared_access import update_project_with_grant
    from backend.db.sima_client import get_sima_db
    user_id = get_user_id_from_request(request)
    db = await get_sima_db()
    return await update_project_with_grant(db, user_id, project_id, data)


@delete("/projects/{project_id:str}", status_code=200)
async def delete_project(request: Any, project_id: str) -> dict:
    """Удалить проект."""
    from backend.db.sima_client import get_sima_db
    user_id = get_user_id_from_request(request)
    db = await get_sima_db()
    deleted = await db.delete_project(project_id, user_id)
    return {"deleted": deleted}


# ═══════════════════════════════════════════
# Blocks
# ═══════════════════════════════════════════

@post("/blocks")
async def create_block(request: Any, data: dict = Body()) -> dict:
    """Создать блок."""
    from backend.db.sima_client import get_sima_db
    db = await get_sima_db()
    pid = str(data.get("projectId") or data.get("project_id") or "")
    if not await _require_project_access(request, pid, write=True):
        return {"error": "forbidden"}
    return await db.create_block(data)


@patch("/blocks")
async def update_block(request: Any, data: dict = Body()) -> dict:
    """Обновить блок."""
    from backend.db.sima_client import get_sima_db
    block_id = data.pop("id", None)
    if not block_id:
        return {"error": "Block ID required"}
    db = await get_sima_db()
    pid = await _project_id_of_block(db, str(block_id))
    if not await _require_project_access(request, pid or "", write=True):
        return {"error": "forbidden"}
    result = await db.update_block(block_id, data)
    return result or {"error": "Block not found"}


@delete("/blocks", status_code=200)
async def delete_block(request: Any, id: str = Parameter(query="id")) -> dict:
    """Удалить блок."""
    from backend.db.sima_client import get_sima_db
    db = await get_sima_db()
    pid = await _project_id_of_block(db, str(id))
    if not await _require_project_access(request, pid or "", write=True):
        return {"error": "forbidden"}
    deleted = await db.delete_block(id)
    return {"deleted": deleted}


# ═══════════════════════════════════════════
# Connections
# ═══════════════════════════════════════════

@post("/connections")
async def create_connection(request: Any, data: dict = Body()) -> dict:
    """Создать связь (с автогенерацией описания)."""
    from backend.core.sima.ai_service import generate_connection_description
    from backend.db.sima_client import get_sima_db
    db = await get_sima_db()
    _pid = str(data.get("projectId") or data.get("project_id") or "")
    if not await _require_project_access(request, _pid, write=True):
        return {"error": "forbidden"}

    # Автогенерация описания если не указано
    if not data.get("description") and not data.get("label"):
        try:
            project_id = data.get("projectId") or data.get("project_id")
            project = None
            all_blocks = []
            if project_id:
                project = await db.get_project(str(project_id))
                if project:
                    all_blocks = project.get("blocks", [])

            from_id = str(data.get("fromBlockId") or "")
            to_id = str(data.get("toBlockId") or "")
            from_block = next((b for b in all_blocks if str(b.get("id", "")) == from_id), None)
            to_block = next((b for b in all_blocks if str(b.get("id", "")) == to_id), None)

            if not from_block or not to_block:
                # Fallback: прямой запрос из БД
                from sqlalchemy import text as sql_text
                async with db.pg.session() as session:
                    from_result = await session.execute(
                        sql_text("SELECT * FROM sima_blocks WHERE id = :id"),
                        {"id": data.get("fromBlockId")}
                    )
                    to_result = await session.execute(
                        sql_text("SELECT * FROM sima_blocks WHERE id = :id"),
                        {"id": data.get("toBlockId")}
                    )
                    fb = from_result.fetchone()
                    tb = to_result.fetchone()
                    if fb:
                        from_block = dict(fb._mapping)
                    if tb:
                        to_block = dict(tb._mapping)

            if from_block and to_block:
                desc = await generate_connection_description(
                    from_block=from_block,
                    to_block=to_block,
                    project=project,
                    all_blocks=all_blocks,
                )
                data.update(desc)
        except Exception as e:
            logger.warning("Auto-describe connection failed", error=str(e))

    return await db.create_connection(data)


@patch("/connections")
async def update_connection(request: Any, data: dict = Body()) -> dict:
    """Обновить связь."""
    from backend.db.sima_client import get_sima_db
    conn_id = data.pop("id", None)
    if not conn_id:
        return {"error": "Connection ID required"}
    db = await get_sima_db()
    pid = await _project_id_of_connection(db, str(conn_id))
    if not await _require_project_access(request, pid or "", write=True):
        return {"error": "forbidden"}
    result = await db.update_connection(conn_id, data)
    return result or {"error": "Connection not found"}


@delete("/connections", status_code=200)
async def delete_connection(request: Any, id: str = Parameter(query="id")) -> dict:
    """Удалить связь."""
    from backend.db.sima_client import get_sima_db
    db = await get_sima_db()
    pid = await _project_id_of_connection(db, str(id))
    if not await _require_project_access(request, pid or "", write=True):
        return {"error": "forbidden"}
    deleted = await db.delete_connection(id)
    return {"deleted": deleted}


# ═══════════════════════════════════════════
# AI Chat
# ═══════════════════════════════════════════

@get("/ai/chat")
async def get_chat_messages(
    request: Any,
    projectId: str = Parameter(query="projectId"),
    sessionId: Optional[str] = Parameter(query="sessionId", default=None),
    messagesOnly: Optional[str] = Parameter(query="messagesOnly", default=None),
) -> dict:
    """Получить историю сообщений."""
    # Анти-IDOR: история AI-чата проекта — только владельцу/грантополучателю
    # (раньше читалась по любому projectId).
    if not await _require_project_access(request, str(projectId)):
        return {"messages": [], "error": "forbidden"}
    from backend.db.sima_client import get_sima_db
    db = await get_sima_db()
    messages = await db.get_messages(projectId, sessionId)
    return {"messages": messages}


@post("/ai/chat")
async def ai_chat(request: Any, data: dict = Body()) -> dict:
    """Обработка сообщения в AI чате."""
    from backend.core.sima import ai_service
    from backend.db.sima_client import get_sima_db
    user_id = get_user_id_from_request(request)

    project_id = data.get("projectId")
    # Анти-IDOR: чат применяет actions (создаёт блоки/связи) — это мутация
    # проекта, нужен write-доступ.
    if not await _require_project_access(request, str(project_id or ""), write=True):
        return {"error": "forbidden"}
    message = data.get("message", "")
    mode = data.get("mode", "default")
    parent_block_id = data.get("parentBlockId")

    logger.info(f"[SIMA ROUTE] ai_chat called: project={project_id}, mode={mode}, user={user_id}, msg_len={len(message)}, parent={parent_block_id}", flush=True)

    # Сохраняем сообщение пользователя
    db = await get_sima_db()
    await db.create_message({
        "projectId": project_id,
        "sessionId": data.get("sessionId"),
        "role": "user",
        "content": message,
    })

    # Обрабатываем в зависимости от режима
    if mode == "parse-tz":
        result = await ai_service.parse_tz_to_schema(project_id, message, user_id)
    elif mode == "brainstorm":
        result = await ai_service.brainstorm_idea(project_id, message, user_id, should_build=False)
    elif mode == "brainstorm-build":
        result = await ai_service.brainstorm_idea(project_id, message, user_id, should_build=True)
    elif mode == "deep-interview":
        result = await ai_service.deep_interview(project_id, message, user_id)
    else:
        result = await ai_service.process_user_message(
            project_id, message, user_id,
            subschema_context=data.get("subschemaContext"),
        )

    # Сохраняем ответ ассистента
    import json
    await db.create_message({
        "projectId": project_id,
        "sessionId": data.get("sessionId"),
        "role": "assistant",
        "content": result.get("message", ""),
        "actions": json.dumps(result.get("actions", []), ensure_ascii=False) if result.get("actions") else None,
    })

    # Если есть actions — применяем их (создаём блоки/связи в БД) и возвращаем обновлённый проект
    actions = result.get("actions", [])
    logger.info(f"[SIMA ROUTE] ai_chat result: message_len={len(result.get('message', ''))}, actions_count={len(actions)}", flush=True)
    if actions:
        try:
            from backend.core.sima.ai_service import apply_schema_actions
            logger.info(f"[SIMA ROUTE] Calling apply_schema_actions with {len(actions)} actions...", flush=True)
            project_update = result.get("projectUpdate")
            updated_project = await apply_schema_actions(project_id, actions, project_update, user_id, parent_block_id=parent_block_id)
            result["project"] = updated_project
            logger.info(f"[SIMA ROUTE] apply_schema_actions SUCCESS: {len(updated_project.get('blocks', []))} blocks, {len(updated_project.get('connections', []))} connections", flush=True)
        except Exception as e:
            import traceback
            logger.info(f"[SIMA ROUTE] apply_schema_actions FAILED: {e}", flush=True)
            logger.info(f"[SIMA ROUTE] Traceback: {traceback.format_exc()}", flush=True)
            # Всё равно пытаемся вернуть обновлённый проект
            try:
                updated_project = await db.get_project(project_id, user_id)
                if updated_project:
                    result["project"] = updated_project
            except Exception:
                logger.debug("suppressed exception", exc_info=True)

    return result


@post("/ai/reapply-actions")
async def reapply_actions(request: Any, data: dict = Body()) -> dict:
    """Повторно применить actions из последнего сообщения AI к проекту.
    Полезно если actions были сохранены, но блоки не созданы."""
    import json as json_module

    from backend.core.sima.ai_service import apply_schema_actions
    from backend.db.sima_client import get_sima_db

    user_id = get_user_id_from_request(request)
    project_id = data.get("projectId")
    message_id = data.get("messageId")  # Опционально — конкретное сообщение
    if not await _require_project_access(request, str(project_id or ""), write=True):
        return {"error": "forbidden", "applied": False}

    logger.info(f"[SIMA ROUTE] reapply_actions: project={project_id}, message_id={message_id}, user={user_id}", flush=True)

    db = await get_sima_db()

    if message_id:
        # Ищем конкретное сообщение
        messages = await db.get_messages(project_id)
        msg = next((m for m in messages if m.get("id") == message_id), None)
    else:
        # Берём последнее сообщение assistant с actions
        messages = await db.get_messages(project_id)
        msg = None
        for m in reversed(messages):
            if m.get("role") == "assistant" and m.get("actions"):
                msg = m
                break

    if not msg:
        return {"error": "No message with actions found", "applied": False}

    actions_raw = msg.get("actions", [])
    if isinstance(actions_raw, str):
        try:
            actions_raw = json_module.loads(actions_raw)
        except (json_module.JSONDecodeError, TypeError):
            return {"error": "Invalid actions JSON", "applied": False}

    if not actions_raw:
        return {"error": "No actions in message", "applied": False}

    logger.info(f"[SIMA ROUTE] reapply_actions: found {len(actions_raw)} actions in message {str(msg.get('id', '?'))[:8]}", flush=True)

    try:
        updated_project = await apply_schema_actions(project_id, actions_raw, None, user_id)
        blocks_count = len(updated_project.get("blocks", []))
        connections_count = len(updated_project.get("connections", []))
        logger.info(f"[SIMA ROUTE] reapply_actions DONE: {blocks_count} blocks, {connections_count} connections", flush=True)
        return {
            "applied": True,
            "project": updated_project,
            "blocksCount": blocks_count,
            "connectionsCount": connections_count,
        }
    except Exception as e:
        import traceback
        logger.info(f"[SIMA ROUTE] reapply_actions FAILED: {e}", flush=True)
        logger.info(f"[SIMA ROUTE] Traceback: {traceback.format_exc()}", flush=True)
        return {"error": str(e), "applied": False}


# ═══════════════════════════════════════════
# AI Generate
# ═══════════════════════════════════════════

@post("/ai/generate-description")
async def generate_description(request: Any, data: dict = Body()) -> dict:
    """Генерация описания блока."""
    from backend.core.sima.ai_service import generate_block_description
    user_id = get_user_id_from_request(request)
    project_id = data.get("projectId")
    block_id = data.get("blockId")
    if not project_id or not block_id:
        return {"error": "projectId and blockId are required"}
    if not await _require_project_access(request, str(project_id), write=True):
        return {"error": "forbidden"}
    try:
        return await generate_block_description(
            project_id, block_id,
            data.get("sections", ["description", "goal"]), user_id,
        )
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.info(f"[SIMA] Generate description error: {e}", flush=True)
        return {"error": f"Description generation failed: {e!s}"}


@post("/ai/generate-connection-description")
async def generate_connection_description_route(request: Any, data: dict = Body()) -> dict:
    """AI-генерация описания связи между блоками."""
    from backend.core.sima.ai_service import generate_connection_description
    from backend.db.sima_client import get_sima_db

    user_id = get_user_id_from_request(request)
    project_id = data.get("projectId")
    connection_id = data.get("connectionId")
    sections = data.get("sections")  # Опционально: конкретные поля

    if not project_id or not connection_id:
        return {"error": "projectId and connectionId are required"}
    if not await _require_project_access(request, str(project_id), write=True):
        return {"error": "forbidden"}

    try:
        db = await get_sima_db()
        project = await db.get_project(project_id, user_id)
        if not project:
            # Доступ уже проверен (владелец или write-грант) — берём проект
            # напрямую, чтобы грантополучатель тоже мог работать.
            project = await db.get_project(project_id)
        if not project:
            return {"error": "Project not found"}

        blocks = project.get("blocks", [])
        connections = project.get("connections", [])

        # Найти связь
        conn = next((c for c in connections if str(c.get("id", "")) == str(connection_id)), None)
        if not conn:
            return {"error": "Connection not found"}

        # Найти блоки связи
        from_id = str(conn.get("fromBlockId") or conn.get("from_block_id") or "")
        to_id = str(conn.get("toBlockId") or conn.get("to_block_id") or "")
        from_block = next((b for b in blocks if str(b.get("id", "")) == from_id), None)
        to_block = next((b for b in blocks if str(b.get("id", "")) == to_id), None)

        if not from_block or not to_block:
            return {"error": f"Connected blocks not found: from={from_id[:8]}, to={to_id[:8]}"}

        result = await generate_connection_description(
            from_block=from_block,
            to_block=to_block,
            project=project,
            all_blocks=blocks,
            sections=sections,
        )

        if not result:
            return {"error": "AI failed to generate description"}

        # Сохраняем в БД
        update_data = {}
        for key in ["label", "description", "dataDescription", "dataFormat", "dataExample",
                     "dataVolume", "condition", "errorHandling", "businessMeaning"]:
            if result.get(key):
                update_data[key] = result[key]

        if update_data:
            await db.update_connection(connection_id, update_data)

        return {"description": result}

    except Exception as e:
        logger.info(f"[SIMA] Generate connection description error: {e}", flush=True)
        return {"error": f"Connection description generation failed: {e!s}"}


@post("/ai/generate-narrative")
async def generate_narrative_route(request: Any, data: dict = Body()) -> dict:
    """Генерация нарратива продукта. Результат сохраняется в проект."""
    from backend.core.sima.ai_service import generate_narrative
    from backend.db.sima_client import get_sima_db
    user_id = get_user_id_from_request(request)
    project_id = data.get("projectId")
    if not project_id:
        return {"error": "projectId is required"}
    if not await _require_project_access(request, str(project_id), write=True):
        return {"error": "forbidden"}
    try:
        result = await generate_narrative(project_id, user_id)
        if result and not result.get("error"):
            # Сохраняем нарратив в проект
            db = await get_sima_db()
            update_data = {}
            if result.get("problem"):
                update_data["narrativeProblem"] = result["problem"]
            if result.get("solution"):
                update_data["narrativeSolution"] = result["solution"]
            if result.get("howItWorks"):
                update_data["narrativeHowItWorks"] = result["howItWorks"]
            if result.get("productMap"):
                update_data["narrativeProductMap"] = result["productMap"]
            if result.get("decisions"):
                update_data["narrativeDecisions"] = result["decisions"]
            if update_data:
                # user_id уже прошёл write-guard (владелец/write-грант) —
                # прежний fallback «найти реального владельца и записать от
                # его имени» был обходом ownership, убран.
                await db.update_project(project_id, user_id, update_data)
                logger.info(f"[SIMA] Narrative saved for project {project_id}: {list(update_data.keys())}", flush=True)
        return result
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.info(f"[SIMA] Generate narrative error: {e}", flush=True)
        return {"error": f"Narrative generation failed: {e!s}"}


@post("/ai/fill-field")
async def fill_field_route(request: Any, data: dict = Body()) -> dict:
    """Заполнить ОДНО поле карты продукта по данным проекта (вид «Поля»).
    Body: {projectId, field}. Возвращает {field, value} или {error}."""
    from backend.core.sima.ai_service import fill_project_field
    user_id = get_user_id_from_request(request)
    project_id = data.get("projectId")
    field = str(data.get("field") or "")
    if not project_id or not field:
        return {"error": "projectId и field обязательны"}
    if not await _require_project_access(request, str(project_id), write=True):
        return {"error": "forbidden"}
    return await fill_project_field(str(project_id), field, user_id)


@post("/ai/generate-tz")
async def generate_tz_route(request: Any, data: dict = Body()) -> dict:
    """Генерация ТЗ. Можно передать meetingIds и/или documentIds для включения данных."""
    from backend.core.sima.ai_service import generate_tz
    user_id = get_user_id_from_request(request)
    project_id = data.get("projectId")
    if not project_id:
        return {"error": "projectId is required"}
    if not await _require_project_access(request, str(project_id), write=True):
        return {"error": "forbidden"}
    try:
        tz = await generate_tz(
            project_id,
            user_id,
            meeting_ids=data.get("meetingIds"),
            document_ids=data.get("documentIds"),
        )
        # Синтезированное ТЗ автоматически становится кубиком библиотеки
        # (обратная ссылка на проект + источники). Раньше результат жил только
        # в поле проекта и в библиотеку не попадал — «кубик = атом» не замыкался.
        artifact_id = await _persist_synthesis_artifact(
            user_id, str(project_id), atype="tz",
            title=f"ТЗ: {data.get('title') or project_id}",
            markdown=tz,
            source_ids=(data.get("meetingIds") or []) + (data.get("documentIds") or []))
        return {"tz": tz, "artifactId": artifact_id}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.info(f"[SIMA] Generate TZ error: {e}", flush=True)
        return {"error": f"TZ generation failed: {e!s}"}


@post("/ai/tz-to-schema")
async def tz_to_schema_route(request: Any, data: dict = Body()) -> dict:
    """Создать схему (блоки + связи) из готового ТЗ проекта.

    Принимает:
        projectId — обязательно
        meetingIds — опционально, доп. встречи для обогащения
        documentIds — опционально, доп. документы для обогащения
    """
    from backend.core.sima.ai_service import apply_schema_actions, generate_tz, parse_tz_to_schema
    from backend.db.sima_client import get_sima_db
    user_id = get_user_id_from_request(request)
    project_id = data.get("projectId")
    if not await _require_project_access(request, str(project_id or ""), write=True):
        return {"error": "forbidden"}

    db = await get_sima_db()
    project = await db.get_project(project_id, user_id)
    if not project:
        return {"error": "Project not found"}

    tz_text = project.get("generated_tz") or project.get("generatedTZ") or ""

    # Если ТЗ нет — генерируем на лету
    if not tz_text:
        tz_text = await generate_tz(
            project_id, user_id,
            meeting_ids=data.get("meetingIds"),
            document_ids=data.get("documentIds"),
        )
        # Сохраняем ТЗ в проект
        await db.update_project(project_id, user_id, {"generatedTZ": tz_text})

    # Парсим ТЗ в схему
    result = await parse_tz_to_schema(project_id, tz_text, user_id)
    actions = result.get("actions", [])
    project_update = result.get("projectUpdate")

    if not actions:
        return {"message": result.get("message", "Не удалось создать блоки из ТЗ"), "project": project}

    # Применяем actions — создаём блоки и связи в БД
    logger.info(f"[SIMA ROUTE] tz_to_schema: applying {len(actions)} actions for project {project_id}", flush=True)
    try:
        updated_project = await apply_schema_actions(project_id, actions, project_update, user_id)
        logger.info(f"[SIMA ROUTE] tz_to_schema: SUCCESS - {len(updated_project.get('blocks', []))} blocks", flush=True)
    except Exception as e:
        import traceback
        logger.info(f"[SIMA ROUTE] tz_to_schema: FAILED - {e}", flush=True)
        logger.info(f"[SIMA ROUTE] Traceback: {traceback.format_exc()}", flush=True)
        # Вернём проект как есть + ошибку
        updated_project = await db.get_project(project_id, user_id) or project

    return {
        "message": result.get("message", f"Создано {len(actions)} элементов"),
        "actions": actions,
        "project": updated_project,
    }


@post("/seed-from-spec")
async def seed_from_spec_route(request: Any, data: dict = Body()) -> dict:
    """Звено D: сырое ТЗ → НАПОЛНЕННЫЙ проект SIMA за один вызов.

    В отличие от /ai/tz-to-schema (нужен готовый projectId), здесь проект
    создаётся с нуля — это программный мост «Tessbrain сформировал ТЗ → завёл
    продукт в SIMA» без ручного «открой SIMA и вставь текст».

    Тело: { tzText | tz_text (обязательно), title?, description? }
    Ответ: { project, actions, message } или { error }.
    """
    from backend.core.sima.ai_service import seed_project_from_spec
    user_id = get_user_id_from_request(request)
    tz_text = data.get("tzText") or data.get("tz_text") or ""
    if not str(tz_text).strip():
        return {"error": "tzText is required"}
    try:
        return await seed_project_from_spec(
            user_id,
            tz_text=str(tz_text),
            title=data.get("title") or data.get("name"),
            description=data.get("description"),
        )
    except Exception as e:
        logger.info(f"[SIMA] seed-from-spec error: {e}", flush=True)
        return {"error": f"seed-from-spec failed: {e!s}"}


# ═══════════════════════════════════════════
# Strategist
# ═══════════════════════════════════════════

@post("/ai/strategist")
async def strategist_route(request: Any, data: dict = Body()) -> dict:
    """Стратегический анализ."""
    from backend.core.sima import strategist_service
    user_id = get_user_id_from_request(request)
    action = data.get("action", "analyze")
    project_id = data.get("projectId")

    if not project_id:
        return {"error": "projectId is required"}
    if not await _require_project_access(request, str(project_id)):
        return {"error": "forbidden"}

    try:
        if action == "analyze":
            return await strategist_service.analyze_project(project_id, user_id)
        elif action == "analyze-block":
            return await strategist_service.analyze_block(project_id, data.get("blockId"), user_id)
        elif action == "chat":
            answer = await strategist_service.chat_with_strategist(
                project_id, data.get("question", ""), data.get("history", []), user_id,
            )
            return {"answer": answer}
        elif action == "last-report":
            report = await strategist_service.get_last_report(project_id)
            return report or {"error": "No report found"}
        else:
            return {"error": f"Unknown action: {action}"}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.info(f"[SIMA] Strategist error ({action}): {e}", flush=True)
        return {"error": f"Strategist failed: {e!s}"}


# ═══════════════════════════════════════════
# Simulator
# ═══════════════════════════════════════════

@post("/ai/simulator")
async def simulator_route(request: Any, data: dict = Body()) -> Any:
    """Симуляция пользовательских путешествий."""
    from backend.core.sima import simulator_service
    user_id = get_user_id_from_request(request)
    action = data.get("action")
    project_id = data.get("projectId")

    if not project_id and action not in ("delete-persona",):
        return {"error": "projectId is required"}
    if project_id and not await _require_project_access(
            request, str(project_id),
            write=action in ("create-persona", "generate-personas",
                             "simulate", "simulate-all", "simulate-block")):
        return {"error": "forbidden"}

    try:
        if action == "create-persona":
            return await simulator_service.create_persona_from_text(project_id, data.get("text", ""))
        elif action == "generate-personas":
            return await simulator_service.generate_personas(project_id, user_id)
        elif action == "get-personas":
            return await simulator_service.get_personas(project_id)
        elif action == "delete-persona":
            deleted = await simulator_service.delete_persona(data.get("personaId"))
            return {"deleted": deleted}
        elif action == "simulate":
            return await simulator_service.simulate(project_id, data.get("personaId"), data.get("layerSlug", "mvp"), user_id)
        elif action == "simulate-all":
            return await simulator_service.simulate_all_personas(project_id, data.get("layerSlug", "mvp"), user_id)
        elif action == "simulate-block":
            return await simulator_service.simulate_block(project_id, data.get("personaId"), data.get("blockId"), user_id)
        elif action == "last-report":
            report = await simulator_service.get_last_simulation_report(project_id, data.get("personaId"))
            return report or {"error": "No report found"}
        else:
            return {"error": f"Unknown action: {action}"}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.info(f"[SIMA] Simulator error ({action}): {e}", flush=True)
        return {"error": f"Simulator failed: {e!s}"}


# ═══════════════════════════════════════════
# Research
# ═══════════════════════════════════════════

@post("/ai/research")
async def research_route(request: Any, data: dict = Body()) -> dict:
    """Исследование рынка."""
    from backend.core.sima.research_service import conduct_research
    user_id = get_user_id_from_request(request)
    project_id = data.get("projectId")
    if not project_id:
        return {"error": "projectId is required"}
    if not await _require_project_access(request, str(project_id), write=True):
        return {"error": "forbidden"}
    try:
        return await conduct_research(project_id, user_id)
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.info(f"[SIMA] Research error: {e}", flush=True)
        return {"error": f"Research failed: {e!s}"}


# ═══════════════════════════════════════════
# Data Sources (включая TessentProvider)
# ═══════════════════════════════════════════

@get("/data-sources")
async def get_data_sources(
    request: Any,
    projectId: str = Parameter(query="projectId"),
    insightsContext: Optional[str] = Parameter(query="insightsContext", default=None),
) -> Any:
    """Получить источники данных или контекст инсайтов."""
    if await _require_project_access(request, projectId) is None:
        return {"error": "access denied", "dataSources": []}
    from backend.core.sima.data_ingestion_service import (
        get_project_data_sources,
        get_project_insights_context,
    )
    from backend.db.sima_client import get_sima_db
    if insightsContext == "true":
        return {"context": await get_project_insights_context(projectId)}
    sources = await get_project_data_sources(projectId)

    # Подгружаем инсайты для каждого источника
    db = await get_sima_db()
    all_insights = await db.get_data_insights(projectId)
    insights_by_source: dict[str, list] = {}
    for insight in all_insights:
        # get_data_insights отдаёт camelCase (_to_camel_dict): ключ dataSourceId.
        # Раньше читали snake data_source_id → None → ВСЕ инсайты падали под
        # None и каждый источник получал insights=[] («не грузит контекст из
        # встреч»). Читаем оба на всякий случай.
        ds_id = insight.get("dataSourceId") or insight.get("data_source_id")
        insights_by_source.setdefault(str(ds_id), []).append(insight)

    for source in sources:
        source["insights"] = insights_by_source.get(str(source.get("id")), [])

    return {"sources": sources}


@post("/data-sources")
async def data_sources_action(request: Any, data: dict = Body()) -> Any:
    """Операции с источниками данных."""
    from backend.core.sima import data_ingestion_service
    user_id = get_user_id_from_request(request)
    action = data.get("action", "upload")
    project_id = data.get("projectId")

    try:
        if action == "upload":
            if not project_id:
                return {"error": "projectId is required"}
            sources = data.get("sources", [])
            results = []
            for source in sources:
                saved = await data_ingestion_service.save_data_source(project_id, source)
                results.append(saved)
                if data.get("autoAnalyze"):
                    await data_ingestion_service.extract_insights(project_id, saved["id"], user_id)
            return results

        elif action == "analyze":
            return await data_ingestion_service.extract_insights(project_id, data.get("dataSourceId"), user_id)

        elif action == "link":
            await data_ingestion_service.link_insight_to_block(data.get("insightId"), data.get("blockId"))
            return {"linked": True}

        elif action == "unlink":
            await data_ingestion_service.unlink_insight(data.get("insightId"))
            return {"unlinked": True}

        elif action == "apply-insights":
            # Применить все привязанные инсайты к блокам — обогатить описания
            from backend.core.llm.router import get_llm_router
            db = await get_sima_db()
            router = get_llm_router()
            insights = await db.get_data_insights(project_id)
            project_data = await db.get_project(project_id)
            if not project_data:
                return {"error": "Project not found"}
            blocks = project_data.get("blocks", [])

            linked_insights = [i for i in insights if i.get("linkedBlockId") or i.get("linked_block_id")]
            if not linked_insights:
                return {"error": "Нет привязанных инсайтов. Сначала привяжите инсайты к блокам."}

            # Группируем инсайты по blockId
            insights_by_block: dict[str, list] = {}
            for ins in linked_insights:
                bid = str(ins.get("linkedBlockId") or ins.get("linked_block_id") or "")
                insights_by_block.setdefault(bid, []).append(ins)

            updated_blocks = []
            for bid, block_insights in insights_by_block.items():
                block = next((b for b in blocks if str(b.get("id", "")) == bid), None)
                if not block:
                    continue

                insights_text = "\n".join([
                    f"- [{ins.get('category','?')}] {ins.get('title','')}: {ins.get('content','')}"
                    for ins in block_insights
                ])

                prompt = f"""Обогати описание модуля на основе данных из инсайтов.

Модуль: {block.get('label','?')} "{block.get('name','?')}"
Текущее описание: {block.get('description','нет')}
Текущая цель: {block.get('goal','нет')}
Текущая бизнес-ценность: {block.get('businessValue','нет')}
Текущая решаемая проблема: {block.get('problemSolved','нет')}

Инсайты привязанные к этому модулю:
{insights_text}

Верни JSON с обновлёнными полями блока. Только те поля, которые нужно обновить/обогатить:
{{"description": "...", "goal": "...", "businessValue": "...", "problemSolved": "...", "userBenefit": "..."}}
ТОЛЬКО JSON."""

                try:
                    result = await router.generate_json(
                        prompt=prompt,
                        task_type=TaskType.GENERATION,
                        model_tier=ModelTier.STANDARD,
                        temperature=0.7,
                    )
                    if isinstance(result, dict):
                        await db.update_block(bid, result)
                        updated_blocks.append({"id": bid, "label": block.get("label"), "fields": list(result.keys())})
                except Exception as e:
                    logger.info(f"[SIMA] Apply insight error for block {bid}: {e}", flush=True)

            return {"applied": True, "updatedBlocks": updated_blocks, "insightsCount": len(linked_insights)}

        return {"error": f"Unknown action: {action}"}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.info(f"[SIMA] Data sources error ({action}): {e}", flush=True)
        return {"error": f"Data sources operation failed: {e!s}"}


@delete("/data-sources", status_code=200)
async def delete_data_source_route(
    request: Any,
    id: str = Parameter(query="id"),
    project_id: str = Parameter(query="projectId", default=""),
) -> dict:
    """Удалить источник данных.

    Анти-IDOR: удаление — мутация, а проверки владельца здесь не было вовсе:
    любой аутентифицированный пользователь мог удалить чужой источник,
    зная только его id. Требуем projectId и право записи на него; для
    надёжности сверяем, что источник действительно принадлежит проекту.
    """
    from backend.core.sima.data_ingestion_service import (
        delete_data_source,
        get_project_data_sources,
    )
    if not project_id:
        raise HTTPException(status_code=400, detail="projectId обязателен")
    if not await _require_project_access(request, project_id, write=True):
        raise HTTPException(status_code=403, detail="Нет доступа к проекту")
    try:
        own = {str(d.get("id")) for d in (await get_project_data_sources(project_id) or [])}
    except Exception:
        own = set()
    if own and str(id) not in own:
        raise HTTPException(status_code=404,
                            detail="Источник не принадлежит этому проекту")
    deleted = await delete_data_source(id)
    return {"deleted": deleted}


# ═══════════════════════════════════════════
# Tessbrain Data Integration
# ═══════════════════════════════════════════

@get("/data-sources/tessent-meetings")
async def list_tessent_meetings(
    request: Any,
    project: Optional[str] = Parameter(query="project", default=None),
) -> dict:
    """Список доступных встреч из Tessbrain с папками и проектами."""
    from backend.core.sima.tessent_provider import fetch_meetings_list
    user_id = get_user_id_from_request(request)
    return await fetch_meetings_list(user_id, project)


@get("/data-sources/tessent-meetings/suggest")
async def suggest_tessent_meetings(
    request: Any,
    projectId: str = Parameter(query="projectId"),
) -> dict:
    """Предложить встречи под тему проекта — автоподбор вместо ручного
    перебора списка. Скоринг детерминированный (пересечение словарей),
    совпавшие слова возвращаются: видно, ПОЧЕМУ встреча предложена.
    Нет пересечения — честно пустой список, а не натянутые совпадения."""
    from backend.core.sima.meeting_suggest import score_meetings
    from backend.core.sima.tessent_provider import fetch_meetings_list
    user_id = get_user_id_from_request(request)
    if not await _require_project_access(request, projectId):
        raise HTTPException(status_code=403, detail="Нет доступа к проекту")
    db = await get_sima_db()
    project = await db.get_project(projectId)
    if not project:
        return {"suggestions": [], "error": "Project not found"}
    listed = await fetch_meetings_list(user_id, None)
    suggestions = score_meetings(project, listed.get("meetings") or [])
    return {
        "suggestions": [
            {"meeting": {
                "id": x["meeting"].get("id"),
                "title": x["meeting"].get("title") or x["meeting"].get("name"),
                "created_at": x["meeting"].get("created_at"),
             },
             "score": x["score"],
             "matched_words": x["matched_words"]}
            for x in suggestions
        ],
        "total_meetings": len(listed.get("meetings") or []),
    }


@post("/data-sources/tessent-meetings")
async def import_tessent_meeting(request: Any, data: dict = Body()) -> dict:
    """Импорт транскрипта встречи из Tessbrain в SIMA."""
    from backend.core.sima.data_ingestion_service import extract_insights, save_data_source
    from backend.core.sima.tessent_provider import fetch_meeting_transcript
    user_id = get_user_id_from_request(request)

    meeting_id = data.get("meetingId")
    project_id = data.get("projectId")

    transcript = await fetch_meeting_transcript(user_id, meeting_id)
    if not transcript:
        return {"error": "Meeting not found or no transcript"}

    # Сохраняем как DataSource
    saved = await save_data_source(project_id, transcript)

    # Автоматически извлекаем инсайты
    if data.get("autoAnalyze", True):
        await extract_insights(project_id, saved["id"], user_id)

    return saved


@post("/data-sources/tessent-knowledge")
async def import_tessent_knowledge(request: Any, data: dict = Body()) -> dict:
    """Импорт данных из Knowledge Graph Tessbrain в SIMA."""
    from backend.core.sima.data_ingestion_service import extract_insights, save_data_source
    from backend.core.sima.tessent_provider import fetch_knowledge_graph_data
    user_id = get_user_id_from_request(request)

    project_id = data.get("projectId")
    kg_data = await fetch_knowledge_graph_data(
        user_id,
        keywords=data.get("keywords"),
        entity_types=data.get("entityTypes"),
        limit=data.get("limit", 50),
    )

    if not kg_data:
        return {"error": "No knowledge graph data found"}

    saved = await save_data_source(project_id, kg_data)

    if data.get("autoAnalyze", True):
        await extract_insights(project_id, saved["id"], user_id)

    return saved


@get("/data-sources/tessent-documents")
async def list_tessent_documents(request: Any) -> list[dict]:
    """Список доступных документов из Tessbrain."""
    from backend.core.sima.tessent_provider import fetch_documents_list
    user_id = get_user_id_from_request(request)
    return await fetch_documents_list(user_id)


@post("/data-sources/tessent-documents")
async def import_tessent_document(request: Any, data: dict = Body()) -> dict:
    """Импорт документа из Tessbrain в SIMA."""
    from backend.core.sima.data_ingestion_service import extract_insights, save_data_source
    from backend.core.sima.tessent_provider import fetch_document_content
    user_id = get_user_id_from_request(request)

    document_id = data.get("documentId")
    project_id = data.get("projectId")

    doc_data = await fetch_document_content(user_id, document_id)
    if not doc_data:
        return {"error": "Document not found or empty"}

    saved = await save_data_source(project_id, doc_data)

    if data.get("autoAnalyze", True):
        await extract_insights(project_id, saved["id"], user_id)

    return saved


# ═══════════════════════════════════════════
# Snapshots
# ═══════════════════════════════════════════

@get("/snapshots")
async def list_snapshots(request: Any, projectId: str = Parameter(query="projectId")) -> dict:
    if await _require_project_access(request, projectId) is None:
        return {"snapshots": []}
    from backend.db.sima_client import get_sima_db
    db = await get_sima_db()
    snapshots = await db.list_snapshots(projectId)
    return {"snapshots": snapshots}


@post("/snapshots")
async def create_snapshot(request: Any, data: dict = Body()) -> dict:
    # гейтим ТОЛЬКО при наличии projectId: снапшот без проекта (blocksData
    # напрямую) — легитимный сценарий, безусловный отказ его ломал бы
    _pid = str(data.get("projectId") or "")
    if _pid and await _require_project_access(request, _pid) is None:
        return {"error": "access denied"}
    from backend.db.sima_client import get_sima_db
    db = await get_sima_db()

    project_id = data.get("projectId")
    blocks_data = data.get("blocksData", [])
    connections_data = data.get("connectionsData", [])

    if project_id and (not blocks_data or len(blocks_data) == 0):
        project = await db.get_project(project_id)
        if project:
            blocks_data = project.get("blocks", [])
            connections_data = project.get("connections", [])
            project_meta = {
                "name": project.get("name"),
                "description": project.get("description"),
                "goal": project.get("goal"),
                "narrativeProblem": project.get("narrativeProblem"),
                "narrativeSolution": project.get("narrativeSolution"),
                "narrativeHowItWorks": project.get("narrativeHowItWorks"),
            }
            data["blocksData"] = blocks_data
            data["connectionsData"] = connections_data
            if not data.get("projectData"):
                data["projectData"] = project_meta

    return await db.create_snapshot(data)


# ═══════════════════════════════════════════
# Sessions
# ═══════════════════════════════════════════

@get("/sessions")
async def list_sessions(request: Any, projectId: str = Parameter(query="projectId")) -> dict:
    if await _require_project_access(request, projectId) is None:
        return {"sessions": []}
    from backend.db.sima_client import get_sima_db
    db = await get_sima_db()
    sessions = await db.get_sessions(projectId)
    return {"sessions": sessions}


@post("/sessions")
async def create_session(request: Any, data: dict = Body()) -> dict:
    _pid = str(data.get("projectId") or "")
    if _pid and await _require_project_access(request, _pid) is None:
        return {"error": "access denied"}
    from backend.db.sima_client import get_sima_db
    db = await get_sima_db()
    session = await db.create_session(data)
    return {"session": session}


# ═══════════════════════════════════════════
# Sessions - DELETE
# ═══════════════════════════════════════════

@delete("/sessions", status_code=200)
async def delete_session_route(id: str = Parameter(query="id")) -> dict:
    """Удалить сессию чата."""
    from backend.db.sima_client import get_sima_db
    db = await get_sima_db()
    return await db.delete_session(id)


# ═══════════════════════════════════════════
# Decisions
# ═══════════════════════════════════════════

@get("/decisions")
async def list_decisions(request: Any, projectId: str = Parameter(query="projectId")) -> list[dict]:
    """Список решений проекта."""
    if await _require_project_access(request, projectId) is None:
        return []
    from backend.db.sima_client import get_sima_db
    db = await get_sima_db()
    return await db.list_decisions(projectId)


@delete("/decisions", status_code=200)
async def delete_decision_route(id: str = Parameter(query="id")) -> dict:
    """Удалить решение."""
    from backend.db.sima_client import get_sima_db
    db = await get_sima_db()
    return await db.delete_decision(id)


# ═══════════════════════════════════════════
# Economics (AI-оценка ценности/сложности)
# ═══════════════════════════════════════════

@post("/ai/economics")
async def economics_route(request: Any, data: dict = Body()) -> dict:
    """Анализ экономики решений — оценка value/complexity каждого блока."""

    from backend.core.llm.router import get_llm_router
    from backend.db.sima_client import get_sima_db

    project_id = data.get("projectId")
    if not project_id:
        return {"blocks": [], "summary": {}, "overallAdvice": "projectId is required", "analysisTime": 0}

    # Анти-IDOR: остальные SIMA-хендлеры зовут _require_project_access, этот
    # не звал — экономику чужого проекта (все блоки с описаниями) можно было
    # получить, зная только его projectId.
    if not await _require_project_access(request, project_id):
        raise HTTPException(status_code=403, detail="Нет доступа к проекту")

    db = await get_sima_db()
    project = await db.get_project(project_id)
    if not project:
        return {"blocks": [], "summary": {}, "overallAdvice": "Project not found", "analysisTime": 0}

    blocks = project.get("blocks", [])

    if not blocks:
        return {"blocks": [], "summary": {}, "overallAdvice": "Нет блоков для анализа.", "analysisTime": 0}

    import time
    start = time.time()
    router = get_llm_router()

    blocks_info = "\n".join([
        f"- {b.get('label', '?')} \"{b.get('name', '?')}\" (type={b.get('type', '?')}, layer={b.get('layer', '?')}): {b.get('description', 'нет описания')}"
        for b in blocks
    ])

    prompt = f"""Ты — бизнес-аналитик. Оцени каждый блок продукта по двум осям:
- value (1-10): бизнес-ценность для продукта
- complexity (1-10): сложность реализации

Продукт: {project.get('name', '?')}
Описание: {project.get('description', 'нет')}

Блоки:
{blocks_info}

Верни JSON:
{{
  "blocks": [
    {{
      "blockId": "id",
      "blockLabel": "A1",
      "blockName": "name",
      "value": 8,
      "complexity": 3,
      "quadrant": "quick_win",
      "valueReasoning": "почему",
      "complexityReasoning": "почему",
      "priority": 1,
      "recommendation": "что делать"
    }}
  ],
  "summary": {{
    "mvpRecommendation": ["block1", "block2"],
    "totalComplexity": "средняя",
    "quickWins": ["block1"],
    "strategicInvestments": ["block2"],
    "avoidOrDefer": []
  }},
  "overallAdvice": "общий совет"
}}

quadrant: quick_win (value>=6, complexity<=5), strategic (value>=6, complexity>5), fill_in (value<6, complexity<=5), avoid (value<6, complexity>5)
ТОЛЬКО JSON, никакого текста вокруг."""

    try:
        parsed = await router.generate_json(
            prompt=prompt,
            task_type=TaskType.REASONING,
            model_tier=ModelTier.PREMIUM,
            temperature=0.5,
            max_tokens=8192,
        )
        elapsed = round(time.time() - start, 1)
        if isinstance(parsed, dict):
            parsed["analysisTime"] = elapsed
            return parsed
        return {"blocks": [], "summary": {}, "overallAdvice": "LLM returned invalid format", "analysisTime": elapsed}
    except Exception as e:
        logger.info(f"[SIMA] Economics analysis error: {e}", flush=True)
        return {"blocks": [], "summary": {}, "overallAdvice": f"Ошибка анализа: {e!s}", "analysisTime": 0}


# ═══════════════════════════════════════════
# Export / Import / Launch
# ═══════════════════════════════════════════

@post("/export")
async def export_project(request: Any, data: dict = Body()) -> dict:
    """Экспорт проекта для IDE / платформы.

    Поддерживаемые target: cursor, claude-code, lovable, github.
    Все платформы получают полную спецификацию + пошаговый план реализации
    с проверками, тестами, отладкой и циклом самокоррекции.
    LLM НЕ используется — 100% данных из СИМА напрямую.
    """
    from backend.core.sima.export_service import export_project_for_platform
    from backend.db.sima_client import get_sima_db

    project_id = data.get("projectId")
    if not project_id:
        return {"error": "projectId is required"}
    if not await _require_project_access(request, str(project_id)):
        return {"error": "forbidden"}

    target = data.get("target", "cursor")
    db = await get_sima_db()
    project = await db.get_project(project_id)
    if not project:
        return {"error": "Project not found"}

    blocks = project.get("blocks", [])
    connections = project.get("connections", [])
    decisions = project.get("decisions", [])

    return await export_project_for_platform(project, blocks, connections, decisions, target)


@post("/import")
async def import_project(request: Any, data: dict = Body()) -> dict:
    """Импорт блоков из текстового описания (через AI) или JSON."""
    import json as _json

    from backend.db.sima_client import get_sima_db

    project_id = data.get("projectId")
    content = data.get("content", "")
    parent_block_id = data.get("parentBlockId")

    if not project_id or not content:
        return {"error": "projectId and content are required"}
    if not await _require_project_access(request, str(project_id), write=True):
        return {"error": "forbidden"}

    db = await get_sima_db()

    # Попробуем распарсить как JSON
    imported_blocks = []
    imported_conns = []
    fmt = "text"
    try:
        parsed = _json.loads(content)
        if isinstance(parsed, dict) and "blocks" in parsed:
            # Это JSON экспорт — импортируем блоки напрямую
            fmt = "json"
            for b in parsed.get("blocks", []):
                b["projectId"] = project_id
                if parent_block_id:
                    b["parentId"] = parent_block_id
                created = await db.create_block(b)
                imported_blocks.append(created)
            for c in parsed.get("connections", []):
                c["projectId"] = project_id
                created = await db.create_connection(c)
                imported_conns.append(created)
        else:
            raise ValueError("Not a project export")
    except (ValueError, _json.JSONDecodeError):
        # Текстовый контент — парсим через LLM
        fmt = "text"
        from backend.core.llm.router import get_llm_router
        router = get_llm_router()
        prompt = f"""Преобразуй текстовое описание в блоки продукта SIMA.

Текст:
{content}

Создай блоки и связи на основе этого текста. Верни JSON:
{{
  "blocks": [
    {{
      "name": "...",
      "type": "feature|service|page|api|database|integration|analytics|payment|auth|notification|storage|other",
      "layer": "frontend|backend|data|infrastructure|external",
      "description": "описание"
    }}
  ],
  "connections": [
    {{
      "fromIndex": 0,
      "toIndex": 1,
      "label": "описание связи",
      "type": "data_flow|depends_on|triggers|contains|api_call|inherits"
    }}
  ]
}}
ТОЛЬКО JSON."""

        try:
            parsed = await router.generate_json(
                prompt=prompt,
                task_type=TaskType.GENERATION,
                model_tier=ModelTier.STANDARD,
                temperature=0.7,
                max_tokens=8192,
            )
            if not isinstance(parsed, dict):
                return {"error": "LLM returned invalid format"}

            # Создаём блоки
            block_ids = []
            for b in parsed.get("blocks", []):
                b["projectId"] = project_id
                if parent_block_id:
                    b["parentId"] = parent_block_id
                created = await db.create_block(b)
                imported_blocks.append(created)
                block_ids.append(created["id"])

            # Создаём связи по индексам
            for c in parsed.get("connections", []):
                fi = c.get("fromIndex", 0)
                ti = c.get("toIndex", 1)
                if fi < len(block_ids) and ti < len(block_ids):
                    conn_data = {
                        "projectId": project_id,
                        "fromBlockId": block_ids[fi],
                        "toBlockId": block_ids[ti],
                        "label": c.get("label", ""),
                        "type": c.get("type", "data_flow"),
                    }
                    created = await db.create_connection(conn_data)
                    imported_conns.append(created)
        except Exception as e:
            logger.error("Import parse error", error=str(e))
            return {"error": f"Failed to parse content: {e!s}"}

    # Получаем обновлённый проект
    project = await db.get_project(project_id)
    return {
        "project": project,
        "format": fmt,
        "importedBlocks": len(imported_blocks),
        "importedConnections": len(imported_conns),
    }


# ═══════════════════════════════════════════
# Artifacts — переиспользование между проектами (Sima Remix: галерея)
# ═══════════════════════════════════════════

_BLOCK_COPY_FIELDS = (
    "name", "label", "type", "layer", "color", "description", "goal",
    "businessValue", "userBenefit", "problemSolved", "impactIfRemoved",
    "metrics", "inputData", "outputData", "acceptanceCriteria", "userFlow",
    "edgeCases", "implementation", "blockTechStack", "apiEndpoints",
    "dataModel", "filesToCreate", "technicalDeps", "estimatedComplexity",
    "analogues", "alternatives", "risks", "futureEvolution", "problems",
    "blockNarrative")


@get("/artifacts")
async def list_artifacts_route(
        request: Any,
        type: Optional[str] = Parameter(query="type", default=None),
        q: Optional[str] = Parameter(query="q", default=None)) -> dict:
    """Галерея артефактов: блоки/подсхемы/ТЗ/вики, переиспользуемые между
    проектами. Фильтр по типу, поиск по названию/описанию/тегам."""
    user_id = get_user_id_from_request(request)
    db = await get_sima_db()
    items = await db.list_artifacts(user_id, artifact_type=type, query=q)
    for it in items:
        it["usedInCount"] = len(it.get("usedIn") or [])
        it.pop("payload", None)        # галерее содержимое не нужно
    return {"artifacts": items, "total": len(items)}


@post("/artifacts")
async def save_artifact_route(request: Any, data: dict = Body()) -> dict:
    """Сохранить артефакт в библиотеку. Два способа:
    1) {projectId, blockId, ...} — снять блок с канваса (подсхема целиком,
       если у блока есть вложенные блоки);
    2) {artifactType: tz|wiki|map, payload: {...}} — произвольный артефакт
       (например ТЗ проекта: payload.markdown)."""
    user_id = get_user_id_from_request(request)
    db = await get_sima_db()

    block_id = data.get("blockId")
    if block_id:        # снять блок/подсхему с канваса
        project_id = data.get("projectId")
        project = (await db.get_project(project_id, user_id)
                   if project_id else None)
        if not project:
            return {"success": False, "error": "Project not found"}
        blocks = project.get("blocks") or []
        block = next((b for b in blocks
                      if str(b.get("id")) == str(block_id)), None)
        if not block:
            return {"success": False, "error": "Block not found"}
        children = [b for b in blocks
                    if str(b.get("parentBlockId") or "") == str(block_id)]
        child_ids = {str(b.get("id")) for b in children} | {str(block_id)}
        conns = [c for c in (project.get("connections") or [])
                 if str(c.get("fromBlockId")) in child_ids
                 and str(c.get("toBlockId")) in child_ids]

        def _strip(b: dict) -> dict:
            out = {k: b.get(k) for k in _BLOCK_COPY_FIELDS
                   if b.get(k) not in (None, "")}
            out["_old_id"] = str(b.get("id"))
            out["_parent_old_id"] = (str(b.get("parentBlockId"))
                                     if b.get("parentBlockId") else None)
            return out

        payload = {"block": _strip(block),
                   "children": [_strip(b) for b in children],
                   "connections": [{
                       "from": str(c.get("fromBlockId")),
                       "to": str(c.get("toBlockId")),
                       "type": c.get("type"), "label": c.get("label"),
                       "dataDescription": c.get("dataDescription"),
                   } for c in conns]}
        artifact = await db.create_artifact(user_id, {
            "artifactType": "subschema" if children else "block",
            "title": data.get("title") or block.get("name", "Блок"),
            "description": data.get("description")
                or block.get("description"),
            "tags": data.get("tags") or [],
            "payload": payload,
            "sourceProjectId": project_id})
        return {"success": True, "artifact": artifact}

    # произвольный артефакт (tz/wiki/map)
    if not data.get("title") or not data.get("payload"):
        return {"success": False,
                "error": "нужны blockId+projectId ИЛИ title+payload"}
    if data.get("artifactType") not in ("block", "subschema", "map",
                                        "tz", "wiki"):
        return {"success": False,
                "error": "artifactType: block|subschema|map|tz|wiki"}
    artifact = await db.create_artifact(user_id, data)
    return {"success": True, "artifact": artifact}


@get("/artifacts/{artifact_id:str}")
async def get_artifact_route(request: Any, artifact_id: str) -> dict:
    """Полный артефакт с payload — превью перед вставкой.

    P1 мульти-аккаунта: не-владелец с read-грантом на artifact тоже читает
    (паттерн S2 как у get_project)."""
    from backend.core.auth.shared_access import artifact_with_grant
    user_id = get_user_id_from_request(request)
    db = await get_sima_db()
    artifact = await artifact_with_grant(db, user_id, artifact_id)
    if not artifact:
        return {"success": False, "error": "Artifact not found"}
    return {"success": True, "artifact": artifact}


@post("/artifacts/{artifact_id:str}/maturity")
async def artifact_maturity_route(request: Any, artifact_id: str,
                                  data: dict = Body()) -> dict:
    """Сменить маркер зрелости артефакта (draft/verified/canon). Только
    владелец (update_artifact_maturity фильтрует по user_id)."""
    user_id = get_user_id_from_request(request)
    maturity = str((data or {}).get("maturity") or "")
    db = await get_sima_db()
    updated = await db.update_artifact_maturity(user_id, artifact_id, maturity)
    if not updated:
        return {"success": False, "error": "not found or invalid maturity"}
    return {"success": True, "artifact": updated}


@post("/artifacts/{artifact_id:str}/insert")
async def insert_artifact_route(request: Any, artifact_id: str,
                                data: dict = Body()) -> dict:
    """Вставить артефакт в проект: block/subschema — создаются блоки и
    связи (новые id, позиция со смещением); tz/wiki — возвращается
    markdown (ничего не перезаписывается). usage отмечается."""
    user_id = get_user_id_from_request(request)
    db = await get_sima_db()
    artifact = await db.get_artifact(artifact_id, user_id)
    if not artifact:
        return {"success": False, "error": "Artifact not found"}
    project_id = data.get("projectId")
    if not project_id:
        return {"success": False, "error": "projectId is required"}

    atype = artifact.get("artifactType", "block")
    payload = artifact.get("payload") or {}

    if atype in ("tz", "wiki", "map"):
        await db.mark_artifact_used(artifact_id, user_id, project_id)
        return {"success": True, "artifactType": atype,
                "markdown": payload.get("markdown", ""),
                "payload": payload}

    # block | subschema → создать блоки в проекте
    px = float(data.get("positionX", 100))
    py = float(data.get("positionY", 100))

    def _mk(b: dict, parent_id: Optional[str], dx: float, dy: float) -> dict:
        body = {k: v for k, v in b.items()
                if not k.startswith("_") and k in _BLOCK_COPY_FIELDS}
        body.update({"projectId": project_id, "positionX": px + dx,
                     "positionY": py + dy})
        if parent_id:
            body["parentBlockId"] = parent_id
        return body

    # create_block пишет только базовые колонки; богатые поля 4 перспектив
    # (businessValue/acceptanceCriteria/...) доезжают вторым шагом
    async def _create_full(b: dict, parent_id, dx, dy) -> dict:
        body = _mk(b, parent_id, dx, dy)
        created = await db.create_block(body)
        rich = {k: v for k, v in body.items()
                if k not in ("projectId", "parentBlockId",
                             "positionX", "positionY")}
        if rich:
            await db.update_block(str(created["id"]), rich)
        return created

    root = await _create_full(payload.get("block") or {}, None, 0, 0)
    id_map = {(payload.get("block") or {}).get("_old_id"): str(root["id"])}
    for i, ch in enumerate(payload.get("children") or []):
        nb = await _create_full(
            ch, str(root["id"]), 40 + (i % 3) * 220, 60 + (i // 3) * 140)
        id_map[ch.get("_old_id")] = str(nb["id"])
    created_conns = 0
    for c in payload.get("connections") or []:
        frm, to = id_map.get(c.get("from")), id_map.get(c.get("to"))
        if frm and to:
            await db.create_connection({
                "projectId": project_id, "fromBlockId": frm, "toBlockId": to,
                "type": c.get("type") or "data_flow",
                "label": c.get("label"),
                "dataDescription": c.get("dataDescription"),
                "parentBlockId": str(root["id"])
                if payload.get("children") else None})
            created_conns += 1

    await db.mark_artifact_used(artifact_id, user_id, project_id)
    return {"success": True, "artifactType": atype,
            "rootBlockId": str(root["id"]),
            "blocksCreated": 1 + len(payload.get("children") or []),
            "connectionsCreated": created_conns}


@delete("/artifacts", status_code=200)
async def delete_artifact_route(request: Any,
                                id: str = Parameter(query="id")) -> dict:
    user_id = get_user_id_from_request(request)
    db = await get_sima_db()
    return {"deleted": await db.delete_artifact(id, user_id)}


@get("/ai/marketing")
async def marketing_kinds_route() -> dict:
    """Виды промо-материалов, которые SIMA делает из ТЗ/описаний."""
    from backend.core.sima.marketing_service import marketing_kinds
    return {"kinds": marketing_kinds()}


@post("/ai/marketing")
async def marketing_route(request: Any, data: dict = Body()) -> dict:
    """Промо-материал из ТЗ и описаний проекта (скелет+LLM): объяснение
    преимуществ / лендинг / питч / рекламные тексты / пост. Факты собирает
    код из полей блоков (userBenefit/businessValue/problemSolved/metrics)
    и ТЗ; LLM только формулирует. LLM недоступна → честный бриф-fallback."""
    from backend.core.sima.marketing_service import generate_marketing
    user_id = get_user_id_from_request(request)
    project_id = data.get("projectId")
    if not project_id:
        return {"success": False, "error": "projectId is required"}
    try:
        result = await generate_marketing(
            project_id, user_id, data.get("kind", "benefits"),
            tone=data.get("tone", ""), audience=data.get("audience", ""))
        return {"success": True, **result}
    except ValueError as e:
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════════
# Kanon Protocol (docs/SIMA_KANON.md, флаг enable_sima_kanon)
# ═══════════════════════════════════════════

def _kanon_guard() -> Optional[dict]:
    from backend.core.sima.kanon_service import kanon_enabled
    if not kanon_enabled():
        return {"success": False,
                "error": "Kanon выключен (enable_sima_kanon=false)"}
    return None


def _operator_profile(user_id: str):
    from backend.core.sima.kanon_service import OperatorProfile
    from backend.core.store.tenant_paths import (
        sima_operator_profile_path_for_user,
    )
    return OperatorProfile(sima_operator_profile_path_for_user(user_id))


async def _write_kanon_workspace(project_id: str, folder: Optional[str],
                                 user_id: Optional[str] = None) -> dict:
    """Собрать и записать Kanon-workspace проекта. Общий код для
    /kanon/workspace и /kanon/handoff."""
    import os as _os
    import pathlib
    import re as _re

    from backend.core.sima.kanon_service import (
        build_kanon_workspace,
        write_workspace,
    )
    db = await get_sima_db()
    project = await db.get_project(project_id)
    if not project:
        return {"success": False, "error": "Project not found"}
    decisions = await db.list_decisions(project_id)
    op_profile = _operator_profile(user_id).get() if user_id else None
    files = build_kanon_workspace(project, decisions,
                                  operator_profile=op_profile)
    slug = _re.sub(r"[^a-z0-9]+", "-",
                   str(project.get("name", "sima-project")).lower()).strip("-")
    folder = folder or str(
        pathlib.Path(_os.path.expanduser("~/sima-projects"))
        / (slug or "sima-project"))
    written = write_workspace(files, folder)
    return {"success": True, "projectFolder": folder, "project": project,
            "files_written": written, "files_total": len(files)}


@get("/kanon/status")
async def kanon_status(request: Any,
                       projectId: str = Parameter(query="projectId")) -> dict:
    """Kanon-статус проекта: контракт каждого блока (mission/kpi/acceptance/
    tasks/files/narrative как ok/partial/missing), число детерминированных
    проверок, готовность к handoff + последние вердикты верификаций."""
    guard = _kanon_guard()
    if guard:
        return guard
    from backend.core.sima.kanon_service import KanonStore, project_kanon_status
    from backend.core.store.tenant_paths import sima_kanon_path_for_user
    user_id = get_user_id_from_request(request)
    if not await _require_project_access(request, str(projectId)):
        return {"success": False, "error": "forbidden"}
    db = await get_sima_db()
    project = await db.get_project(projectId)
    if not project:
        return {"success": False, "error": "Project not found"}
    status = project_kanon_status(project)
    verdicts = KanonStore(
        sima_kanon_path_for_user(user_id)).get_project(projectId)
    for b in status["blocks"]:
        v = verdicts.get(b["block_id"])
        if v:
            b["last_verdict"] = v.get("verdict")
            b["cascade_state"] = v.get("cascade_state")
            b["checked_at"] = v.get("checked_at")
    return {"success": True, **status}


@post("/kanon/verify")
async def kanon_verify(request: Any, data: dict = Body()) -> dict:
    """Tri-state верификация блока против workspace (папки реализации) +
    Kanon cascade по обратным зависимостям (desync — сразу). cmd-проверки
    исполняются только при runCommands=true."""
    guard = _kanon_guard()
    if guard:
        return guard
    from backend.core.sima import kanon_service as ks
    from backend.core.store.tenant_paths import sima_kanon_path_for_user
    user_id = get_user_id_from_request(request)
    project_id = data.get("projectId")
    block_id = data.get("blockId")
    workspace = data.get("workspace")
    if not project_id:
        return {"success": False, "error": "projectId обязателен"}
    # blockId и workspace необязательны: кнопка «Проверить» на экране
    # реализации присылает только projectId, и раньше получала ошибку —
    # проверить результат из интерфейса было нельзя вовсе. Без blockId
    # проверяем ВСЕ блоки с готовым контрактом (так же делает автоприёмка
    # после прогона агента), без workspace берём папку проекта по умолчанию.
    if not workspace:
        import os as _os
        import pathlib as _pl
        import re as _re2
        _db0 = await get_sima_db()
        _p0 = await _db0.get_project(project_id)
        _slug = _re2.sub(r"[^a-z0-9]+", "-",
                         str((_p0 or {}).get("name", "sima-project")).lower()
                         ).strip("-")
        workspace = str(_pl.Path(_os.path.expanduser("~/sima-projects"))
                        / (_slug or "sima-project"))
    if not await _require_project_access(request, str(project_id), write=True):
        return {"success": False, "error": "forbidden"}
    db = await get_sima_db()
    project = await db.get_project(project_id)
    if not project:
        return {"success": False, "error": "Project not found"}
    blocks = project.get("blocks") or []
    connections = project.get("connections") or []
    idx = {str(b.get("id")): b for b in blocks}

    run_commands = bool(data.get("runCommands"))

    # Проверка всего проекта: блоки с готовым контрактом. Каскад тут не
    # нужен — проверяются и так все.
    if not block_id:
        targets = [b for b in blocks
                   if ks.implementation_status(
                       ks.derive_contract(b, connections, idx), b
                   ).get("ready_for_handoff")]
        if not targets:
            return {"success": False,
                    "error": "Нет блоков с проверяемым контрактом — "
                             "сначала задайте критерии приёмки"}
        all_results = [ks.verify_block(b, connections, idx, workspace,
                                       run_commands=run_commands)
                       for b in targets]
        drift_all = ks.scan_for_drift(workspace, _operator_profile(user_id).get())
        if drift_all["verdict"] == "fail":
            for r in all_results:
                r["verdict"] = "fail"
                r["drift"] = drift_all
        ks.KanonStore(
            sima_kanon_path_for_user(user_id)).record(project_id, all_results)
        _counts: dict = {}
        for r in all_results:
            _counts[r["verdict"]] = _counts.get(r["verdict"], 0) + 1
        return {"success": True, "results": all_results, "counts": _counts,
                "workspace": workspace, "drift": drift_all}

    block = idx.get(str(block_id))
    if not block:
        return {"success": False, "error": "Block not found"}

    result = ks.verify_block(block, connections, idx, workspace,
                             run_commands=run_commands)
    # Опц. семантический судья (R-7.94): по флагу тела ИЛИ env. НЕ создаёт pass —
    # только обогащает вердикт dimensions/todo_to_pass (спека Kanon).
    if bool(data.get("judge")) or ks.semantic_judge_enabled():
        if result.get("verdict") != ks.FAIL:
            result["semantic"] = await ks.semantic_judge(block, connections, idx, workspace)
    # drift-сканер (R-7.82): hard-нарушение dont_use валит прогон
    drift = ks.scan_for_drift(workspace, _operator_profile(user_id).get())
    result["drift"] = drift
    if drift["verdict"] == "fail":
        result["verdict"] = "fail"
    results = [result]
    cascade = None
    if data.get("cascade", True) and result["verdict"] == "pass":
        cascade = ks.cascade_verify(str(block_id), project, workspace,
                                    run_commands=run_commands)
        results.extend(cascade["results"])
    ks.KanonStore(
        sima_kanon_path_for_user(user_id)).record(project_id, results)
    # append-only память прогона в narrative блока workspace
    ks.append_narrative(workspace, ks.block_slug(block),
                        f"verify: {result['verdict']}"
                        + (f"; desync: {cascade['desync']}"
                           if cascade and cascade["desync"] else ""))
    return {"success": True, "result": result, "cascade": cascade}


@post("/kanon/suggest-acceptance")
async def kanon_suggest_acceptance(request: Any, data: dict = Body()) -> dict:
    """Автогенерация проверок приёмки из описания блока («за ручку» для
    непрограммиста: file:/contains:/cmd: пишет SIMA, а не сотрудник).
    body: {projectId, blockId, apply?: bool}. apply=true → критерии сразу
    сохраняются в acceptanceCriteria блока (markdown-списком)."""
    guard = _kanon_guard()
    if guard:
        return guard
    from backend.core.sima.ai_service import suggest_acceptance_criteria
    user_id = get_user_id_from_request(request)
    project_id, block_id = data.get("projectId"), data.get("blockId")
    if not (project_id and block_id):
        return {"success": False, "error": "projectId и blockId обязательны"}
    res = await suggest_acceptance_criteria(project_id, block_id, user_id)
    if res.get("error"):
        return {"success": False, "error": res["error"]}
    criteria = res.get("criteria", [])
    applied = False
    if criteria and data.get("apply"):
        db = await get_sima_db()
        text_value = "\n".join(f"- {c}" for c in criteria)
        await db.update_block(str(block_id), {"acceptanceCriteria": text_value})
        applied = True
    return {"success": True, "criteria": criteria,
            "deterministic": res.get("deterministic", 0), "applied": applied}


@post("/kanon/workspace")
async def kanon_workspace(request: Any, data: dict = Body()) -> dict:
    """Сгенерировать Kanon-workspace проекта (контракты блоков + AGENTS.md +
    architecture_decisions.md + auto_tz.md) в папку на диске. narrative.md и
    решения append-only — повторный вызов их не перезаписывает."""
    guard = _kanon_guard()
    if guard:
        return guard
    project_id = data.get("projectId")
    if not project_id:
        return {"success": False, "error": "projectId is required"}
    if not await _require_project_access(request, str(project_id), write=True):
        return {"success": False, "error": "forbidden"}
    user_id = get_user_id_from_request(request)
    res = await _write_kanon_workspace(project_id, data.get("folder"),
                                       user_id)
    res.pop("project", None)
    return res


@get("/kanon/wiki")
async def kanon_wiki(request: Any,
                     projectId: str = Parameter(query="projectId")) -> dict:
    """Документация как проекция графа (Kanon §7): WIKI из контрактов
    блоков (+ вердикты верификаций в статусах) и пользовательские
    туториалы по блокам с user-facing полями. Детерминированно, без LLM —
    не пишется вручную, перегенерируется."""
    guard = _kanon_guard()
    if guard:
        return guard
    from backend.core.sima.kanon_service import (
        KanonStore,
        generate_user_docs,
        generate_wiki,
    )
    from backend.core.store.tenant_paths import sima_kanon_path_for_user
    user_id = get_user_id_from_request(request)
    if not await _require_project_access(request, str(projectId)):
        return {"success": False, "error": "forbidden"}
    db = await get_sima_db()
    project = await db.get_project(projectId)
    if not project:
        return {"success": False, "error": "Project not found"}
    decisions = await db.list_decisions(projectId)
    verdicts = KanonStore(
        sima_kanon_path_for_user(user_id)).get_project(projectId)
    return {"success": True,
            "wiki": generate_wiki(project, decisions, verdicts),
            "user_docs": generate_user_docs(project)}


@post("/kanon/handoff")
async def kanon_handoff(request: Any, data: dict = Body()) -> dict:
    """ФАЗА 1 автономной реализации: Kanon-workspace + ТЗ → PENDING-handoff
    для кодинг-агента (claude/cursor/codex; через подписку — claude CLI,
    не API). НИЧЕГО не запускает: исполнение только после явного
    подтверждения пользователя (POST /task-analysis/handoff/{id}/confirm)."""
    guard = _kanon_guard()
    if guard:
        return guard
    user_id = get_user_id_from_request(request)
    project_id = data.get("projectId")
    if not project_id:
        return {"success": False, "error": "projectId is required"}
    if not await _require_project_access(request, str(project_id), write=True):
        return {"success": False, "error": "forbidden"}
    agent = data.get("agent", "claude")

    # 1) workspace с контрактами (идемпотентно, append-only части целы)
    ws = await _write_kanon_workspace(project_id, data.get("folder"),
                                      user_id)
    if not ws.get("success"):
        return ws
    folder, project = ws["projectFolder"], ws["project"]

    # 2) ТЗ: generatedTZ проекта; без него — честный отказ (сначала
    #    сгенерируй ТЗ в SIMA — в этом смысл «создать само ТЗ»)
    tz = project.get("generatedTZ") or project.get("generated_tz")
    if not tz:
        return {"success": False,
                "error": "У проекта нет ТЗ (generatedTZ). Сгенерируй ТЗ "
                         "в SIMA (/ai/generate-tz) перед handoff."}
    spec = (f"{tz}\n\n---\n\nРаботай по Kanon Protocol: прочитай AGENTS.md "
            f"в корне, контракты блоков в atlas/blocks/*/contract/. Блок "
            f"готов только когда его acceptance-проверки проходят, "
            f"narrative.md блока дополняй (append-only).")

    from backend.core.tasks.task_analysis import coding_handoff
    rec = await coding_handoff(
        user_id,
        {"title": f"SIMA: {project.get('name', project_id)}",
         "description": project.get("description") or ""},
        agent=agent, repo_path=folder, spec_text=spec,
        kanon={"project_id": project_id,
               "run_commands": bool(data.get("runCommands")),
               "judge": bool(data.get("judge")),
               "iteration": 0})  # P3: авто-verify всех готовых блоков после прогона
    rec["projectFolder"] = folder
    rec["success"] = True
    return rec


@get("/kanon/operator-profile")
async def kanon_operator_profile_get(request: Any) -> dict:
    """Типизированная память оператора: lessons / dont_use / always_use.
    Переживает проекты (Kanon §5)."""
    guard = _kanon_guard()
    if guard:
        return guard
    user_id = get_user_id_from_request(request)
    return {"success": True, **_operator_profile(user_id).get()}


@post("/kanon/operator-profile")
async def kanon_operator_profile_set(request: Any, data: dict = Body()) -> dict:
    """Добавить/удалить правило профиля. body: {action: add|remove,
    kind: lessons|dont_use|always_use, rule, severity?: hard|soft, reason?}.
    dont_use[hard] — drift-сканер валит прогон агента с таким кодом."""
    guard = _kanon_guard()
    if guard:
        return guard
    user_id = get_user_id_from_request(request)
    prof = _operator_profile(user_id)
    action = data.get("action", "add")
    kind, rule = data.get("kind"), data.get("rule")
    if not (kind and rule):
        return {"success": False, "error": "kind и rule обязательны"}
    try:
        if action == "remove":
            removed = prof.remove(kind, rule)
            return {"success": True, "removed": removed, **prof.get()}
        out = prof.add(kind, rule, severity=data.get("severity", "soft"),
                       reason=data.get("reason", ""))
        return {"success": True, **out}
    except ValueError as e:
        return {"success": False, "error": str(e)}


@post("/kanon/context-pack")
async def kanon_context_pack(request: Any, data: dict = Body()) -> dict:
    """Детерминированный context-pack блока (граф вместо моря контекста).
    body: {projectId, blockId, profile?: design|backend-fix|ui-fix|
    acceptance-only}. Возвращает pack (JSON) + markdown-промпт."""
    guard = _kanon_guard()
    if guard:
        return guard
    from backend.core.sima.kanon_service import (
        build_context_pack,
        context_pack_md,
    )
    user_id = get_user_id_from_request(request)
    project_id, block_id = data.get("projectId"), data.get("blockId")
    if not (project_id and block_id):
        return {"success": False, "error": "projectId и blockId обязательны"}
    if not await _require_project_access(request, str(project_id)):
        return {"success": False, "error": "forbidden"}
    db = await get_sima_db()
    project = await db.get_project(project_id)
    if not project:
        return {"success": False, "error": "Project not found"}
    decisions = await db.list_decisions(project_id)
    try:
        pack = build_context_pack(
            project, block_id, profile=data.get("profile", "design"),
            decisions=decisions,
            operator_profile=_operator_profile(user_id).get())
    except ValueError as e:
        return {"success": False, "error": str(e)}
    return {"success": True, "pack": pack, "markdown": context_pack_md(pack)}


@post("/kanon/handoff-batch")
async def kanon_handoff_batch(request: Any, data: dict = Body()) -> dict:
    """Agent-loop поверх двухфазной модели: обойти граф, выбрать runnable-
    блоки (контракт готов, сам не pass, зависимости не fail) и создать
    PENDING-handoff НА КАЖДЫЙ — с узким context-pack вместо всего ТЗ.
    НИЧЕГО не запускает: каждый handoff ждёт явного confirm пользователя."""
    guard = _kanon_guard()
    if guard:
        return guard
    from backend.core.sima import kanon_service as ks
    from backend.core.store.tenant_paths import sima_kanon_path_for_user
    from backend.core.tasks.task_analysis import coding_handoff
    user_id = get_user_id_from_request(request)
    project_id = data.get("projectId")
    if not project_id:
        return {"success": False, "error": "projectId is required"}
    if not await _require_project_access(request, str(project_id), write=True):
        return {"success": False, "error": "forbidden"}
    agent = data.get("agent", "claude")
    limit = max(1, min(int(data.get("limit", 5)), 20))

    ws = await _write_kanon_workspace(project_id, data.get("folder"), user_id)
    if not ws.get("success"):
        return ws
    folder, project = ws["projectFolder"], ws["project"]
    db = await get_sima_db()
    decisions = await db.list_decisions(project_id)
    verdicts = ks.KanonStore(
        sima_kanon_path_for_user(user_id)).get_project(project_id)
    runnable = ks.pick_runnable_blocks(project, verdicts)[:limit]

    op_profile = _operator_profile(user_id).get()
    handoffs = []
    for rb in runnable:
        pack = ks.build_context_pack(
            project, rb["block_id"], profile=data.get("profile", "design"),
            decisions=decisions, operator_profile=op_profile)
        spec = (ks.context_pack_md(pack)
                + "\n---\n\nРаботай по Kanon Protocol: AGENTS.md в корне, "
                  f"контракт блока в atlas/blocks/{rb['slug']}/contract/. "
                  "Блок готов только когда acceptance-проверки проходят; "
                  "narrative.md дополняй (append-only).")
        rec = await coding_handoff(
            user_id,
            {"title": f"SIMA блок: {rb['name']}",
             "description": f"проект {project.get('name', project_id)}"},
            agent=agent, repo_path=folder, spec_text=spec,
            kanon={"project_id": project_id, "block_id": rb["block_id"],
                   "run_commands": bool(data.get("runCommands")),
                   "judge": bool(data.get("judge")),
                   "iteration": 0})  # P3: авто-verify этого блока после прогона
        handoffs.append({"handoff_id": rec.get("id"),
                         "block_id": rb["block_id"], "name": rb["name"],
                         "command": rec.get("command")})
    return {"success": True, "projectFolder": folder,
            "runnable_total": len(runnable), "handoffs": handoffs,
            "note": "все handoff в статусе pending_confirmation — запуск "
                    "только после явного подтверждения каждого"}


@post("/launch")
async def launch_project(request: Any, data: dict = Body()) -> dict:
    """Генерация кода проекта и запуск в IDE."""
    import os
    import pathlib

    from backend.core.llm.router import get_llm_router
    from backend.db.sima_client import get_sima_db

    project_id = data.get("projectId")
    if not project_id:
        return {"success": False, "error": "projectId is required"}
    if not await _require_project_access(request, str(project_id), write=True):
        return {"success": False, "error": "forbidden"}

    target = data.get("target", "cursor")
    folder = data.get("folder")
    db = await get_sima_db()

    # Open folder action
    if target == "open-folder" and folder:
        return {"success": True, "message": "Folder registered", "projectFolder": folder}

    project = await db.get_project(project_id)
    if not project:
        return {"success": False, "error": "Project not found"}

    router = get_llm_router()
    blocks = project.get("blocks", [])
    connections = project.get("connections", [])

    blocks_text = "\n".join([
        f"- {b.get('label','?')} \"{b.get('name','?')}\" (type={b.get('type','?')}, layer={b.get('layer','?')}): {b.get('description','')}"
        for b in blocks
    ])
    conns_text = "\n".join([
        f"- {str(c.get('fromBlockId','?'))[:8]} -> {str(c.get('toBlockId','?'))[:8]}: {c.get('label','')}" for c in connections
    ])

    tool_name = "Cursor" if target == "cursor" else "Claude Code"

    prompt = f"""Ты — Senior разработчик. Сгенерируй структуру проекта для запуска в {tool_name}.

Проект: {project.get('name', '?')}
Описание: {project.get('description', '')}
Цель: {project.get('goal', '')}

Модули:
{blocks_text}

Связи:
{conns_text}

Создай файлы проекта. Верни JSON:
{{
  "projectName": "slug-имя",
  "files": {{
    "README.md": "содержимое",
    "package.json": "содержимое",
    "src/index.ts": "содержимое"
  }},
  "commands": ["npm install", "npm run dev"],
  "description": "краткое описание того, что сгенерировано"
}}
ТОЛЬКО JSON."""

    try:
        gen = await router.generate_json(
            prompt=prompt,
            task_type=TaskType.GENERATION,
            model_tier=ModelTier.PREMIUM,
            temperature=0.7,
            max_tokens=16384,
        )
        if not isinstance(gen, dict):
            return {"success": False, "error": "LLM returned invalid format"}

        # Записываем файлы на диск
        project_name = gen.get("projectName", "sima-project")
        base_dir = pathlib.Path(os.path.expanduser("~/sima-projects")) / project_name
        base_dir.mkdir(parents=True, exist_ok=True)

        for fpath, content in gen.get("files", {}).items():
            fp = base_dir / fpath
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(str(content), encoding="utf-8")

        return {
            "success": True,
            "launched": True,
            "message": f"Проект сгенерирован в {base_dir}",
            "projectFolder": str(base_dir),
            "files": gen.get("files", {}),
            "commands": gen.get("commands", []),
        }
    except Exception as e:
        logger.info(f"[SIMA] Launch error: {e}", flush=True)
        return {"success": False, "launched": False, "error": str(e), "message": str(e)}


# ═══════════════════════════════════════════
# Router
# ═══════════════════════════════════════════

sima_router = Router(
    path="/sima",
    route_handlers=[
        list_projects, create_project, get_project, update_project, delete_project,
        create_block, update_block, delete_block,
        create_connection, update_connection, delete_connection,
        get_chat_messages, ai_chat, reapply_actions,
        generate_description, generate_connection_description_route,
        generate_narrative_route, generate_tz_route, tz_to_schema_route,
        fill_field_route,
        seed_from_spec_route,
        strategist_route, simulator_route, research_route, economics_route,
        marketing_kinds_route, marketing_route,
        list_artifacts_route, save_artifact_route, insert_artifact_route,
        artifact_maturity_route,
        get_artifact_route,
        delete_artifact_route,
        get_data_sources, data_sources_action, delete_data_source_route,
        list_tessent_meetings, suggest_tessent_meetings,
        import_tessent_meeting, import_tessent_knowledge,
        list_tessent_documents, import_tessent_document,
        list_snapshots, create_snapshot,
        list_sessions, create_session, delete_session_route,
        list_decisions, delete_decision_route,
        export_project, import_project, launch_project,
        kanon_status, kanon_verify, kanon_workspace, kanon_handoff,
        kanon_suggest_acceptance,
        kanon_operator_profile_get, kanon_operator_profile_set,
        kanon_context_pack, kanon_handoff_batch, kanon_wiki,
    ],
    tags=["SIMA"],
)
