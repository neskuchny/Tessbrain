"""
TESSENT BRAIN - Chat Sessions API
Эндпоинты для управления историей чатов
"""
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import structlog
from litestar import Request, Router, delete, get, post
from litestar.params import Parameter
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)

# Storage root for sessions (per-user subdirectories).
# Абсолютный путь от _DATA_ROOT: относительный "data/chat_sessions" зависел от
# cwd процесса (API vs worker) — тот же класс бага, что был у broadcasts.
try:
    from backend.core.store.tenant_paths import _DATA_ROOT as _TB_DATA_ROOT
    SESSIONS_ROOT_DIR = Path(_TB_DATA_ROOT) / "chat_sessions"
except Exception:  # tenant_paths стабится в части тестов
    SESSIONS_ROOT_DIR = Path("data/chat_sessions")
SESSIONS_ROOT_DIR.mkdir(parents=True, exist_ok=True)


def _pg() -> bool:
    """Postgres-бэкенд чатов включён? (D1c; иначе — файлы, как раньше)."""
    try:
        from backend.core.store import chat_session_store
        return chat_session_store.enabled()
    except Exception:
        return False

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _require_uuid(value: str, field_name: str) -> str:
    if not value or not _UUID_RE.match(value):
        from litestar.exceptions import ValidationException
        raise ValidationException(f"Invalid {field_name}")
    return value


def _user_dir(user_id: str) -> Path:
    user_id = _require_uuid(user_id, "user_id")
    d = SESSIONS_ROOT_DIR / user_id
    d.mkdir(parents=True, exist_ok=True)
    return d


# === Models ===

class ChatMessage(BaseModel):
    role: str
    content: str
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class ChatSessionCreate(BaseModel):
    title: Optional[str] = None
    folder: Optional[str] = None
    messages: list[ChatMessage] = Field(default_factory=list)
    filters: Optional[dict[str, Any]] = None
    user_id: Optional[str] = None
    # Режим, в котором создан чат (brain|mark|transcripts|automation|…) —
    # для цветовой пометки в списке чатов. Необязателен (старые чаты → None).
    agent_mode: Optional[str] = None


class ChatSessionUpdate(BaseModel):
    title: Optional[str] = None
    folder: Optional[str] = None
    messages: Optional[list[ChatMessage]] = None
    filters: Optional[dict[str, Any]] = None
    agent_mode: Optional[str] = None


class ChatSessionResponse(BaseModel):
    id: str
    title: str
    folder: Optional[str] = None
    preview: str
    created_at: str
    updated_at: str
    message_count: int
    filters: Optional[dict[str, Any]] = None
    user_id: Optional[str] = None
    agent_mode: Optional[str] = None


class ChatSessionDetail(ChatSessionResponse):
    messages: list[ChatMessage]


# === Storage Functions ===

def _get_session_path(user_id: str, session_id: str) -> Path:
    user_id = _require_uuid(user_id, "user_id")
    session_id = _require_uuid(session_id, "session_id")
    return _user_dir(user_id) / f"{session_id}.json"


def _load_session(user_id: str, session_id: str) -> Optional[dict]:
    if _pg():
        from backend.core.store import chat_session_store
        return chat_session_store.get_session(
            _require_uuid(user_id, "user_id"),
            _require_uuid(session_id, "session_id"))
    path = _get_session_path(user_id, session_id)
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def _save_session(user_id: str, session_id: str, data: dict):
    if _pg():
        from backend.core.store import chat_session_store
        chat_session_store.put_session(
            _require_uuid(user_id, "user_id"),
            _require_uuid(session_id, "session_id"), data)
        return
    path = _get_session_path(user_id, session_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── Реестр папок чатов (per-user _folders.json рядом с сессиями) ──
# Папка — самостоятельная сущность: не исчезает, когда из неё убрали все
# чаты, и создаётся пустой. Автопополняется при назначении папки сессии.
# _list_sessions этот файл не подхватит: в нём нет user_id.

def _folders_path(user_id: str):
    return _user_dir(user_id) / "_folders.json"


def _load_folder_registry(user_id: str) -> list[str]:
    if _pg():
        try:
            from backend.core.store import chat_session_store
            return chat_session_store.get_folders(_require_uuid(user_id, "user_id"))
        except Exception:
            logger.warning("folders PG load failed", exc_info=True)
            return []
    try:
        with open(_folders_path(user_id), "r", encoding="utf-8") as f:
            data = json.load(f)
        return [str(x) for x in (data.get("folders") or [])]
    except Exception:
        return []


def _save_folder_registry(user_id: str, folders: list[str]) -> None:
    if _pg():
        from backend.core.store import chat_session_store
        chat_session_store.put_folders(_require_uuid(user_id, "user_id"), folders)
        return
    with open(_folders_path(user_id), "w", encoding="utf-8") as f:
        json.dump({"folders": sorted(set(folders))[:100]},
                  f, ensure_ascii=False, indent=2)


def _register_folder(user_id: str, name: str) -> None:
    name = (name or "").strip()[:60]
    if not name:
        return
    reg = _load_folder_registry(user_id)
    if name not in reg:
        reg.append(name)
        _save_folder_registry(user_id, reg)


def _delete_session(user_id: str, session_id: str) -> bool:
    if _pg():
        from backend.core.store import chat_session_store
        return chat_session_store.delete_session(
            _require_uuid(user_id, "user_id"),
            _require_uuid(session_id, "session_id"))
    path = _get_session_path(user_id, session_id)
    if path.exists():
        path.unlink()
        return True
    return False


def _list_sessions(user_id: str) -> list[dict]:
    user_id = _require_uuid(user_id, "user_id")
    if _pg():
        from backend.core.store import chat_session_store
        return chat_session_store.list_sessions(user_id)
    sessions = []
    user_path = _user_dir(user_id)
    for path in user_path.glob("*.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                # extra safety: enforce ownership in file too
                if data.get("user_id") == user_id:
                    sessions.append(data)
        except Exception as e:
            logger.error(f"Failed to load session {path}: {e}")

    # Sort by updated_at descending
    sessions.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return sessions


def _generate_title(messages: list[dict]) -> str:
    """Generate title from first user message"""
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            # Take first 50 chars
            title = content[:50]
            if len(content) > 50:
                title += "..."
            return title
    return "Новый чат"


def _get_preview(messages: list[dict]) -> str:
    """Get preview from last assistant message"""
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            content = msg.get("content", "")
            preview = content[:100]
            if len(content) > 100:
                preview += "..."
            return preview
    return "Нет сообщений"


# === Route Handlers ===

def _verify_user(request: Request, user_id: str) -> str:
    """Анти-IDOR: при валидном токене user_id берётся из него; попытка
    действовать за чужого → 403. Без токена — query как есть (совместимость,
    закрывается флагом enable_strict_chat_auth в trusted_user_id)."""
    from litestar.exceptions import PermissionDeniedException
    try:
        from backend.core.auth.service_token import trusted_user_id
        uid, src = trusted_user_id(request.headers, user_id or "")
        if src != "unverified" and uid:
            return uid
    except PermissionError:
        raise PermissionDeniedException("token not authorized for requested user_id")
    except Exception:
        pass
    return user_id


@get("/")
async def list_sessions(
    request: Request,
    user_id: str = Parameter(query="user_id", required=True),
) -> list[ChatSessionResponse]:
    """Get all chat sessions for a user"""
    user_id = _verify_user(request, user_id)
    sessions = _list_sessions(user_id=user_id)
    return [
        ChatSessionResponse(
            id=s["id"],
            title=s.get("title", "Новый чат"),
            folder=s.get("folder") or None,
            preview=_get_preview(s.get("messages", [])),
            created_at=s.get("created_at", ""),
            updated_at=s.get("updated_at", ""),
            message_count=len(s.get("messages", [])),
            filters=s.get("filters"),
            user_id=s.get("user_id"),
            agent_mode=s.get("agent_mode"),
        )
        for s in sessions
    ]


@post("/")
async def create_session(data: ChatSessionCreate) -> ChatSessionDetail:
    """Create a new chat session"""
    if not data.user_id:
        from litestar.exceptions import ValidationException
        raise ValidationException("user_id is required")
    _require_uuid(data.user_id, "user_id")
    session_id = str(uuid4())
    now = datetime.utcnow().isoformat()

    messages = [m.model_dump() for m in data.messages]
    title = data.title or _generate_title(messages)

    session_data = {
        "id": session_id,
        "title": title,
        "folder": (data.folder or "").strip()[:60],
        "messages": messages,
        "filters": data.filters,
        "user_id": data.user_id,
        "agent_mode": (data.agent_mode or "").strip() or None,
        "created_at": now,
        "updated_at": now,
    }

    _save_session(data.user_id, session_id, session_data)
    if session_data.get("folder"):
        _register_folder(data.user_id, session_data["folder"])
    logger.info(f"Created session {session_id} for user {data.user_id}")

    return ChatSessionDetail(
        id=session_id,
        title=title,
        preview=_get_preview(messages),
        created_at=now,
        updated_at=now,
        message_count=len(messages),
        filters=data.filters,
        user_id=data.user_id,
        agent_mode=session_data.get("agent_mode"),
        messages=[ChatMessage(**m) for m in messages],
    )


@get("/{session_id:str}")
async def get_session(
    session_id: str,
    request: Request,
    user_id: str = Parameter(query="user_id", required=True),
) -> ChatSessionDetail:
    """Get a specific chat session"""
    user_id = _verify_user(request, user_id)
    data = _load_session(user_id, session_id)
    if not data:
        from litestar.exceptions import NotFoundException
        raise NotFoundException(f"Session {session_id} not found")

    # Enforce ownership
    if data.get("user_id") != user_id:
        from litestar.exceptions import NotFoundException
        raise NotFoundException(f"Session {session_id} not found")

    messages = data.get("messages", [])
    return ChatSessionDetail(
        id=data["id"],
        title=data.get("title", "Новый чат"),
        preview=_get_preview(messages),
        created_at=data.get("created_at", ""),
        updated_at=data.get("updated_at", ""),
        message_count=len(messages),
        filters=data.get("filters"),
        user_id=data.get("user_id"),
        agent_mode=data.get("agent_mode"),
        messages=[ChatMessage(**m) for m in messages],
    )


@post("/{session_id:str}")
async def update_session(
    session_id: str,
    data: ChatSessionUpdate,
    request: Request,
    user_id: str = Parameter(query="user_id", required=True),
) -> ChatSessionDetail:
    """Update a chat session"""
    user_id = _verify_user(request, user_id)
    existing = _load_session(user_id, session_id)
    if not existing:
        from litestar.exceptions import NotFoundException
        raise NotFoundException(f"Session {session_id} not found")
    if existing.get("user_id") != user_id:
        from litestar.exceptions import NotFoundException
        raise NotFoundException(f"Session {session_id} not found")

    now = datetime.utcnow().isoformat()

    if data.title is not None:
        existing["title"] = data.title

    if data.folder is not None:
        existing["folder"] = data.folder.strip()[:60]
        if existing["folder"]:
            _register_folder(user_id, existing["folder"])

    if data.messages is not None:
        existing["messages"] = [m.model_dump() for m in data.messages]
        # Auto-update title if not set
        if not existing.get("title") or existing["title"] == "Новый чат":
            existing["title"] = _generate_title(existing["messages"])

    if data.filters is not None:
        existing["filters"] = data.filters

    if data.agent_mode is not None:
        existing["agent_mode"] = (data.agent_mode or "").strip() or None

    existing["updated_at"] = now

    _save_session(user_id, session_id, existing)
    logger.info(f"Updated session {session_id}")

    messages = existing.get("messages", [])
    return ChatSessionDetail(
        id=existing["id"],
        title=existing.get("title", "Новый чат"),
        preview=_get_preview(messages),
        created_at=existing.get("created_at", ""),
        updated_at=now,
        message_count=len(messages),
        filters=existing.get("filters"),
        user_id=existing.get("user_id"),
        agent_mode=existing.get("agent_mode"),
        messages=[ChatMessage(**m) for m in messages],
    )


@post("/{session_id:str}/messages")
async def add_message(
    session_id: str,
    message: ChatMessage,
    request: Request,
    user_id: str = Parameter(query="user_id", required=True),
) -> ChatSessionDetail:
    """Add a message to a session"""
    user_id = _verify_user(request, user_id)
    existing = _load_session(user_id, session_id)
    if not existing:
        from litestar.exceptions import NotFoundException
        raise NotFoundException(f"Session {session_id} not found")
    if existing.get("user_id") != user_id:
        from litestar.exceptions import NotFoundException
        raise NotFoundException(f"Session {session_id} not found")

    now = datetime.utcnow().isoformat()

    if "messages" not in existing:
        existing["messages"] = []

    existing["messages"].append(message.model_dump())
    existing["updated_at"] = now

    # Auto-update title from first user message
    if existing.get("title") == "Новый чат" or not existing.get("title"):
        existing["title"] = _generate_title(existing["messages"])

    _save_session(user_id, session_id, existing)

    messages = existing.get("messages", [])
    return ChatSessionDetail(
        id=existing["id"],
        title=existing.get("title", "Новый чат"),
        preview=_get_preview(messages),
        created_at=existing.get("created_at", ""),
        updated_at=now,
        message_count=len(messages),
        filters=existing.get("filters"),
        messages=[ChatMessage(**m) for m in messages],
    )


@delete("/{session_id:str}", status_code=200)
async def delete_session_endpoint(
    session_id: str,
    request: Request,
    user_id: str = Parameter(query="user_id", required=True),
) -> dict[str, Any]:
    """Delete a chat session"""
    user_id = _verify_user(request, user_id)
    if _delete_session(user_id, session_id):
        logger.info(f"Deleted session {session_id}")
        return {"status": "deleted", "id": session_id}

    from litestar.exceptions import NotFoundException
    raise NotFoundException(f"Session {session_id} not found")


# Router
@get("/folders")
async def list_chat_folders(
    request: Request,
    user_id: str = Parameter(query="user_id", required=True),
) -> dict:
    """Папки чатов: реестр ∪ фактически используемые в сессиях."""
    user_id = _verify_user(request, user_id)
    reg = set(_load_folder_registry(user_id))
    used = {(s.get("folder") or "").strip()
            for s in _list_sessions(user_id)}
    used.discard("")
    return {"folders": sorted(reg | used, key=lambda x: x.lower())}


@post("/folders")
async def create_chat_folder(
    request: Request,
    data: dict,
    user_id: str = Parameter(query="user_id", required=True),
) -> dict:
    """Создать пустую папку чатов (без привязки к чатам/встречам)."""
    user_id = _verify_user(request, user_id)
    name = str((data or {}).get("name") or "").strip()[:60]
    if not name:
        from litestar.exceptions import ValidationException
        raise ValidationException("name is required")
    _register_folder(user_id, name)
    return {"success": True, "name": name}


@post("/folders/delete")
async def delete_chat_folder(
    request: Request,
    data: dict,
    user_id: str = Parameter(query="user_id", required=True),
) -> dict:
    """Удалить папку: чаты НЕ удаляются — у них снимается метка папки."""
    user_id = _verify_user(request, user_id)
    name = str((data or {}).get("name") or "").strip()[:60]
    if not name:
        from litestar.exceptions import ValidationException
        raise ValidationException("name is required")
    reg = [f for f in _load_folder_registry(user_id) if f != name]
    _save_folder_registry(user_id, reg)
    cleared = 0
    for sess in _list_sessions(user_id):
        if (sess.get("folder") or "").strip() == name:
            sess["folder"] = ""
            _save_session(user_id, sess["id"], sess)
            cleared += 1
    return {"success": True, "name": name, "chats_unassigned": cleared}


router = Router(
    path="/sessions",
    route_handlers=[
        list_chat_folders, create_chat_folder, delete_chat_folder,
        list_sessions,
        create_session,
        get_session,
        update_session,
        add_message,
        delete_session_endpoint,
    ],
    tags=["Sessions"],
)

