# -*- coding: utf-8 -*-
"""
TESSENT BRAIN - Documents Routes
API для работы с документами

Включает:
- Загрузку и хранение документов
- Синхронизацию в базу знаний (векторы + граф)
- Поиск по документам
- Чат с документами
"""
import base64
import logging
from typing import Any, Dict, List, Optional

import json as _json
from pathlib import Path

from litestar import Router, delete, get, post
from litestar.params import Body, Parameter
from pydantic import BaseModel

from backend.api.middleware.auth_middleware import get_user_id_from_token
from backend.core.documents.file_parser import FileParser
from backend.db.supabase_client import get_supabase_client

logger = logging.getLogger(__name__)


# === Models ===

class DocumentUploadRequest(BaseModel):
    """Запрос на загрузку документа"""
    title: str
    content: str
    doc_type: Optional[str] = "text"
    metadata: Optional[Dict[str, Any]] = None
    folder: Optional[str] = None      # плоский ярлык-папка (в metadata.folder)


class SetDocFolderRequest(BaseModel):
    """Назначить/снять папку документа."""
    folder: Optional[str] = None      # пусто → убрать из папки


class DocumentSyncRequest(BaseModel):
    """Запрос на синхронизацию документов"""
    document_ids: Optional[List[str]] = None  # None = все несинхронизированные


class UrlIngestRequest(BaseModel):
    """Поглотить внешнюю ссылку в память компании."""
    url: str
    title: Optional[str] = None       # переопределить заголовок
    sync: bool = True                 # сразу индексировать в граф/вектора
    metadata: Optional[Dict[str, Any]] = None


class DocumentChatRequest(BaseModel):
    """Запрос на чат с документом"""
    document_id: str
    message: str
    history: Optional[List[Dict[str, str]]] = None


class DocumentSearchRequest(BaseModel):
    """Запрос на поиск по документам"""
    query: str
    limit: int = 10
    doc_type: Optional[str] = None


# === Helpers ===

def get_user_id(authorization: Optional[str], user_id: Optional[str]) -> Optional[str]:
    """Верифицированный токен > query (анти-IDOR).

    Аудит: раньше ?user_id= имел приоритет над токеном — любой мог читать/
    удалять чужие документы. Теперь при валидном токене user_id берётся из
    него, попытка действовать за чужого → None (отказ); query — fallback
    без токена."""
    try:
        from backend.core.auth.service_token import trusted_user_id
        headers = {"authorization": authorization} if authorization else {}
        uid, src = trusted_user_id(headers, user_id or "")
        if src != "unverified" and uid:
            return uid
    except PermissionError:
        logger.warning("documents: токен валиден, но запрошен чужой user_id — отказ")
        return None
    except Exception:
        logger.debug("documents: trusted_user_id unavailable", exc_info=True)
    if user_id:
        return user_id
    return get_user_id_from_token(authorization)


# === Реестр папок документов ==============================================
# Плоские ярлыки-папки для загруженных документов (как у чатов): значение
# папки живёт в documents.metadata.folder (без зависимости от миграции 269),
# а сам список папок — в per-user JSON-реестре, чтобы пустые папки не исчезали.

_DOC_FOLDERS_ROOT = Path(__file__).resolve().parent.parent.parent / "data" / "doc_folders"


def _doc_folders_file(user_id: str) -> Path:
    safe = "".join(c for c in (user_id or "") if c.isalnum() or c in "-_")[:64] or "anon"
    d = _DOC_FOLDERS_ROOT / safe
    d.mkdir(parents=True, exist_ok=True)
    return d / "folders.json"


def _load_doc_folders(user_id: str) -> List[str]:
    try:
        data = _json.loads(_doc_folders_file(user_id).read_text(encoding="utf-8"))
        return [str(x) for x in (data.get("folders") or [])]
    except Exception:
        return []


def _save_doc_folders(user_id: str, folders: List[str]) -> None:
    try:
        _doc_folders_file(user_id).write_text(
            _json.dumps({"folders": sorted(set(folders))[:100]}, ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        logger.warning("doc folders save failed", exc_info=True)


def _register_doc_folder(user_id: str, name: str) -> None:
    name = (name or "").strip()[:60]
    if not name:
        return
    folders = _load_doc_folders(user_id)
    if name not in folders:
        folders.append(name)
        _save_doc_folders(user_id, folders)


# === Route Handlers ===

@get("/")
async def list_documents(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
    limit: int = 50,
    offset: int = 0,
    sync_status: Optional[str] = None
) -> Dict[str, Any]:
    """Получить список документов"""
    resolved_user_id = get_user_id(authorization, user_id)
    if not resolved_user_id:
        return {"status": "error", "message": "Authentication required"}

    try:
        supabase = get_supabase_client()

        params = {
            "user_id": f"eq.{resolved_user_id}",
            "select": "id,title,doc_type,file_name,file_size,content_preview,status,sync_status,synced_at,chunks_count,created_at,updated_at,metadata",
            "order": "created_at.desc",
            "limit": limit,
            "offset": offset
        }

        if sync_status:
            params["sync_status"] = f"eq.{sync_status}"

        documents = await supabase._request(
            "GET",
            "/rest/v1/documents",
            params=params
        )

        return {
            "status": "success",
            "documents": documents,
            "total": len(documents),
            "limit": limit,
            "offset": offset
        }
    except Exception as e:
        msg = str(e)
        if "404" in msg or "42P01" in msg or "relation \"public.documents\" does not exist" in msg:
            logger.info("Documents table not found in Supabase; returning empty documents list")
            return {
                "status": "success",
                "documents": [],
                "message": "Documents table not found. Please run migration.",
                "migration_required": True
            }
        logger.error(f"Error listing documents: {e}")
        return {"status": "error", "message": str(e)}


@get("/{document_id:str}")
async def get_document(
    document_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
    include_content: bool = True
) -> Dict[str, Any]:
    """Получить документ по ID"""
    resolved_user_id = get_user_id(authorization, user_id)
    if not resolved_user_id:
        return {"status": "error", "message": "Authentication required"}

    try:
        supabase = get_supabase_client()

        select = "*" if include_content else "id,title,doc_type,file_name,file_size,content_preview,status,sync_status,synced_at,chunks_count,created_at,updated_at,metadata"

        documents = await supabase._request(
            "GET",
            "/rest/v1/documents",
            params={
                "id": f"eq.{document_id}",
                "user_id": f"eq.{resolved_user_id}",
                "select": select
            }
        )

        if not documents:
            return {"status": "error", "message": "Document not found"}

        return {
            "status": "success",
            "document": documents[0]
        }
    except Exception as e:
        logger.error(f"Error getting document: {e}")
        return {"status": "error", "message": str(e)}


@post("/")
async def upload_document(
    data: DocumentUploadRequest,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False)
) -> Dict[str, Any]:
    """Загрузить новый документ"""
    resolved_user_id = get_user_id(authorization, user_id)
    if not resolved_user_id:
        # РАНЬШЕ здесь был fallback на data.metadata["user_id"] — он обходил
        # анти-IDOR отказ (get_user_id → None означает «токен валиден, но
        # запрошен чужой user_id»): любой с валидным токеном мог загрузить
        # документ в чужой аккаунт произвольным metadata.user_id. Убрано.
        return {"status": "error", "message": "Authentication required"}

    try:
        supabase = get_supabase_client()

        # 1. Подготовка контента (обработка Base64 и парсинг файлов)
        content_to_save = data.content
        content_bytes = None

        # Проверяем на Base64 (Data URL)
        if data.content.startswith('data:'):
            try:
                # format: data:application/pdf;base64,JVBERi0xLj...
                _header, encoded = data.content.split(',', 1)
                content_bytes = base64.b64decode(encoded)
                logger.info(f"Decoded Base64 content: {len(content_bytes)} bytes")
            except Exception as e:
                logger.warning(f"Failed to decode Base64 content: {e}")

        # Если есть байты (успешно декодировали Base64), пробуем распарсить
        if content_bytes:
            parsed_text, detected_type = FileParser.parse_file(content_bytes, data.title)
            if parsed_text:
                content_to_save = parsed_text
                logger.info(f"Parsed file content: {len(content_to_save)} chars (type: {detected_type})")
            else:
                # Если парсер вернул пустоту, возможно это картинка или ошибка
                # Сохраняем как есть, но декодируем в utf-8 если это текст
                try:
                    content_to_save = content_bytes.decode('utf-8')
                except Exception:
                    content_to_save = "[Binary Content]" # Не сохраняем бинарник в текстовое поле
                    logger.warning("Could not decode binary content to text")

        # 2. Очистка от null-символов (PostgreSQL fix)
        cleaned_content = content_to_save.replace('\x00', '').replace('\u0000', '')
        cleaned_title = data.title.replace('\x00', '').replace('\u0000', '')

        # 3. Создаём превью контента
        content_preview = cleaned_content[:500] + "..." if len(cleaned_content) > 500 else cleaned_content

        _md = dict(data.metadata or {})
        _folder = (data.folder or "").strip()[:60]
        if _folder:
            _md["folder"] = _folder
        new_doc = {
            "title": cleaned_title,
            "content": cleaned_content,
            "doc_type": data.doc_type or "text",
            "user_id": resolved_user_id,
            "content_preview": content_preview,
            "file_size": len(cleaned_content.encode('utf-8')),
            "metadata": _md,
            "status": "active",
            "sync_status": "not_synced"
        }
        if _folder:
            _register_doc_folder(resolved_user_id, _folder)

        # Извлекаем file_name из metadata если есть
        if data.metadata and data.metadata.get("file_name"):
            new_doc["file_name"] = data.metadata.get("file_name")

        result = await supabase._request(
            "POST",
            "/rest/v1/documents",
            json_data=new_doc,
            headers={"Prefer": "return=representation"}
        )

        logger.info(f"Document uploaded: {data.title} for user {resolved_user_id}")

        return {
            "status": "success",
            "document": result[0] if result else new_doc,
            "message": "Document uploaded. Use /sync to index it for search."
        }
    except Exception as e:
        logger.error(f"Error uploading document: {e}")
        return {"status": "error", "message": str(e)}


@delete("/{document_id:str}", status_code=200)
async def delete_document(
    document_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False)
) -> Dict[str, Any]:
    """Удалить документ"""
    resolved_user_id = get_user_id(authorization, user_id)
    if not resolved_user_id:
        return {"status": "error", "message": "Authentication required"}

    try:
        supabase = get_supabase_client()

        # Удаляем индекс документа
        try:
            from backend.core.documents import DocumentIndexer
            indexer = DocumentIndexer(user_id=resolved_user_id)
            await indexer.initialize()
            await indexer.delete_document_index(document_id)
        except Exception as e:
            logger.warning(f"Could not delete document index: {e}")

        # Удаляем из Supabase
        await supabase._request(
            "DELETE",
            "/rest/v1/documents",
            params={
                "id": f"eq.{document_id}",
                "user_id": f"eq.{resolved_user_id}"
            }
        )

        logger.info(f"Document {document_id} deleted for user {resolved_user_id}")

        return {
            "status": "success",
            "message": f"Document {document_id} deleted"
        }
    except Exception as e:
        logger.error(f"Error deleting document: {e}")
        return {"status": "error", "message": str(e)}


@post("/sync")
async def sync_documents(
    data: DocumentSyncRequest,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False)
) -> Dict[str, Any]:
    """
    Синхронизировать документы в базу знаний.

    Создаёт векторные эмбеддинги и добавляет в граф знаний
    для поиска через Brain.
    """
    resolved_user_id = get_user_id(authorization, user_id)
    if not resolved_user_id:
        return {"status": "error", "message": "Authentication required"}

    try:
        from backend.core.documents import sync_documents_to_knowledge

        logger.info(f"🔄 Starting documents sync for user {resolved_user_id}")

        result = await sync_documents_to_knowledge(
            user_id=resolved_user_id,
            document_ids=data.document_ids
        )

        return {
            "status": "success",
            "message": f"Synced {result['synced']}/{result['total']} documents",
            "stats": result
        }
    except Exception as e:
        logger.error(f"Error syncing documents: {e}")
        return {"status": "error", "message": str(e)}


@post("/search")
async def search_documents(
    data: DocumentSearchRequest,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False)
) -> Dict[str, Any]:
    """
    Семантический поиск по документам.

    Использует векторный поиск для нахождения релевантных чанков.
    """
    resolved_user_id = get_user_id(authorization, user_id)
    if not resolved_user_id:
        return {"status": "error", "message": "Authentication required"}

    try:
        from backend.core.documents import DocumentIndexer

        indexer = DocumentIndexer(user_id=resolved_user_id)
        await indexer.initialize()

        results = await indexer.search_documents(
            query=data.query,
            limit=data.limit,
            doc_type=data.doc_type
        )

        return {
            "status": "success",
            "query": data.query,
            "results": results,
            "total": len(results)
        }
    except Exception as e:
        logger.error(f"Error searching documents: {e}")
        return {"status": "error", "message": str(e)}


@post("/chat")
async def chat_with_document(
    data: DocumentChatRequest,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False)
) -> Dict[str, Any]:
    """
    Чат с документом.

    Использует контекст документа для ответа на вопросы.
    """
    resolved_user_id = get_user_id(authorization, user_id)
    if not resolved_user_id:
        return {"status": "error", "message": "Authentication required"}

    try:
        from backend.core.documents import DocumentIndexer
        from backend.core.llm.router import get_llm_router
        from backend.db.supabase_client import get_supabase_client

        supabase = get_supabase_client()

        # Получаем документ
        documents = await supabase._request(
            "GET",
            "/rest/v1/documents",
            params={
                "id": f"eq.{data.document_id}",
                "user_id": f"eq.{resolved_user_id}",
                "select": "id,title,content,doc_type"
            }
        )

        if not documents:
            return {"status": "error", "message": "Document not found"}

        doc = documents[0]

        # Получаем релевантный контекст из документа
        indexer = DocumentIndexer(user_id=resolved_user_id)
        await indexer.initialize()

        context = await indexer.get_document_context(
            document_id=data.document_id,
            query=data.message,
            max_chunks=5
        )

        # Если контекст пустой, используем начало документа
        if not context:
            context = doc.get("content", "")[:3000]

        # Формируем промпт
        history_text = ""
        if data.history:
            for msg in data.history[-5:]:  # Последние 5 сообщений
                role = "Пользователь" if msg.get("role") == "user" else "Ассистент"
                history_text += f"{role}: {msg.get('content', '')}\n"

        prompt = f"""Ты - ассистент для работы с документами. Отвечай на вопросы пользователя, основываясь ТОЛЬКО на содержимом документа.

Документ: {doc.get('title', 'Без названия')}
Тип: {doc.get('doc_type', 'text')}

=== СОДЕРЖИМОЕ ДОКУМЕНТА ===
{context}
=== КОНЕЦ ДОКУМЕНТА ===

{f"История диалога:{chr(10)}{history_text}" if history_text else ""}

Вопрос пользователя: {data.message}

Инструкции:
- Отвечай только на основе информации из документа
- Если информации нет в документе, честно скажи об этом
- Цитируй релевантные части документа при необходимости
- Отвечай на русском языке

Ответ:"""

        # Генерируем ответ. cost_scope — чтобы расход чата с документом
        # атрибутировался на пользователя и документ (раньше уходил в
        # 'unknown' без user_id/document_id).
        from backend.core.llm.usage_tracker import cost_scope
        llm = get_llm_router()
        async with cost_scope(
            user_id=resolved_user_id, document_id=data.document_id,
            agent_mode="documents", request_type="document_chat", surface="chat",
        ):
            response = await llm.generate(
                prompt=prompt,
                task_type="generation",
                max_tokens=2000
            )

        return {
            "status": "success",
            "document_id": data.document_id,
            "document_title": doc.get("title"),
            "message": data.message,
            "response": response,
            "context_used": len(context)
        }

    except Exception as e:
        logger.error(f"Error in document chat: {e}")
        return {"status": "error", "message": str(e)}


@get("/stats")
async def get_documents_stats(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False)
) -> Dict[str, Any]:
    """Получить статистику по документам"""
    resolved_user_id = get_user_id(authorization, user_id)
    if not resolved_user_id:
        return {"status": "error", "message": "Authentication required"}

    try:
        supabase = get_supabase_client()

        # Получаем все документы для статистики
        documents = await supabase._request(
            "GET",
            "/rest/v1/documents",
            params={
                "user_id": f"eq.{resolved_user_id}",
                "select": "id,doc_type,sync_status,file_size,chunks_count"
            }
        )

        # Считаем статистику
        stats = {
            "total": len(documents),
            "by_type": {},
            "by_sync_status": {
                "not_synced": 0,
                "syncing": 0,
                "synced": 0,
                "error": 0
            },
            "total_size": 0,
            "total_chunks": 0
        }

        for doc in documents:
            doc_type = doc.get("doc_type", "text")
            sync_status = doc.get("sync_status", "not_synced")

            stats["by_type"][doc_type] = stats["by_type"].get(doc_type, 0) + 1
            stats["by_sync_status"][sync_status] = stats["by_sync_status"].get(sync_status, 0) + 1
            stats["total_size"] += doc.get("file_size", 0) or 0
            stats["total_chunks"] += doc.get("chunks_count", 0) or 0

        return {
            "status": "success",
            "stats": stats
        }

    except Exception as e:
        msg = str(e)
        if "404" in msg or "42P01" in msg:
            return {
                "status": "success",
                "stats": {
                    "total": 0,
                    "message": "Documents table not found"
                }
            }
        logger.error(f"Error getting documents stats: {e}")
        return {"status": "error", "message": str(e)}


@post("/ingest-url")
async def ingest_url(
    data: UrlIngestRequest,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False)
) -> Dict[str, Any]:
    """Поглотить внешнюю ссылку: скачать → извлечь текст → документ →
    (опц.) сразу индексировать в граф знаний и вектора.

    После этого содержимое ссылки участвует в поиске/анализе/отчётах
    наравне со встречами. SSRF-защита в core/documents/url_ingest."""
    resolved_user_id = get_user_id(authorization, user_id)
    if not resolved_user_id:
        return {"status": "error", "message": "Authentication required"}

    from backend.core.documents.url_ingest import fetch_url_text
    fetched = await fetch_url_text(data.url)
    if not fetched.get("ok"):
        return {"status": "error", "message": fetched.get("error", "fetch failed")}

    title = (data.title or fetched.get("title")
             or data.url.split("//", 1)[-1][:120])
    meta = dict(data.metadata or {})
    meta.update({"source": "url_ingest", "source_url": data.url,
                 "final_url": fetched.get("final_url"),
                 "content_type": fetched.get("content_type")})

    # вызываем логику upload напрямую (litestar handler → исходная функция)
    _upload_fn = getattr(upload_document, "fn", upload_document)
    upload = await _upload_fn(
        data=DocumentUploadRequest(
            title=title,
            content=f"Источник: {data.url}\n\n{fetched['text']}",
            doc_type="web_page",
            metadata=meta,
        ),
        authorization=authorization,
        user_id=resolved_user_id,
    )
    if upload.get("status") != "success":
        return upload
    doc = upload.get("document") or {}
    doc_id = doc.get("id")

    sync_result = None
    if data.sync and doc_id:
        try:
            from backend.core.documents import sync_documents_to_knowledge
            sync_result = await sync_documents_to_knowledge(
                user_id=resolved_user_id, document_ids=[doc_id])
        except Exception as e:
            logger.warning(f"ingest-url: sync failed (документ сохранён): {e}")
            sync_result = {"error": str(e)}

    return {
        "status": "success",
        "document_id": doc_id,
        "title": title,
        "chars": len(fetched["text"]),
        "synced": bool(sync_result and not sync_result.get("error")),
        "sync": sync_result,
        "message": "Ссылка поглощена" + (" и проиндексирована"
                                          if data.sync else " (без индексации)"),
    }


# === Router ===

# ВАЖНО: Порядок route_handlers имеет значение!
# Более специфичные пути (stats, sync, search, chat) должны идти ПЕРЕД
# динамическим путём /{document_id:str}, иначе "stats" будет интерпретирован как document_id
@get("/folders")
async def list_doc_folders(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    """Папки документов: реестр (в т.ч. пустые) + используемые в документах."""
    uid = get_user_id(authorization, user_id)
    if not uid:
        return {"folders": []}
    names = set(_load_doc_folders(uid))
    try:
        supabase = get_supabase_client()
        rows = await supabase._request("GET", "/rest/v1/documents",
                                       params={"user_id": f"eq.{uid}", "select": "metadata"})
        for r in rows or []:
            f = str(((r.get("metadata") or {}).get("folder") or "")).strip()
            if f:
                names.add(f)
    except Exception:
        logger.debug("folders-in-use lookup failed", exc_info=True)
    return {"folders": sorted(names)}


@post("/folders")
async def create_doc_folder(
    data: dict = Body(),
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    """Создать пустую папку (реестр — не исчезает без документов)."""
    uid = get_user_id(authorization, user_id)
    if not uid:
        return {"status": "error", "message": "Authentication required"}
    name = str((data or {}).get("name") or "").strip()[:60]
    if not name:
        return {"status": "error", "message": "name required"}
    _register_doc_folder(uid, name)
    return {"status": "success", "folders": sorted(set(_load_doc_folders(uid)))}


@post("/folders/delete")
async def delete_doc_folder(
    data: dict = Body(),
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    """Убрать папку из реестра (документы остаются; если в ней есть документы,
    ярлык ещё виден по факту использования, пока их не перенесут)."""
    uid = get_user_id(authorization, user_id)
    if not uid:
        return {"status": "error", "message": "Authentication required"}
    name = str((data or {}).get("name") or "").strip()
    remaining = [f for f in _load_doc_folders(uid) if f != name]
    _save_doc_folders(uid, remaining)
    return {"status": "success", "folders": sorted(set(remaining))}


@post("/{document_id:str}/folder")
async def set_document_folder(
    document_id: str,
    data: SetDocFolderRequest,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    """Назначить/снять папку документа (metadata.folder). Пусто → убрать."""
    uid = get_user_id(authorization, user_id)
    if not uid:
        return {"status": "error", "message": "Authentication required"}
    folder = (data.folder or "").strip()[:60]
    try:
        supabase = get_supabase_client()
        rows = await supabase._request("GET", "/rest/v1/documents", params={
            "id": f"eq.{document_id}", "user_id": f"eq.{uid}",
            "select": "id,metadata", "limit": "1"})
        if not rows:
            return {"status": "error", "message": "Document not found"}
        md = dict(rows[0].get("metadata") or {})
        if folder:
            md["folder"] = folder
        else:
            md.pop("folder", None)
        await supabase._request("PATCH", "/rest/v1/documents",
                                params={"id": f"eq.{document_id}", "user_id": f"eq.{uid}"},
                                json_data={"metadata": md})
        if folder:
            _register_doc_folder(uid, folder)
        return {"status": "success", "folder": folder or None}
    except Exception as e:
        logger.error(f"set_document_folder failed: {e}")
        return {"status": "error", "message": str(e)}


router = Router(
    path="/documents",
    route_handlers=[
        list_documents,          # GET /documents/
        get_documents_stats,     # GET /documents/stats - ПЕРЕД get_document!
        list_doc_folders,        # GET /documents/folders - ПЕРЕД get_document!
        create_doc_folder,       # POST /documents/folders
        delete_doc_folder,       # POST /documents/folders/delete
        sync_documents,          # POST /documents/sync
        ingest_url,              # POST /documents/ingest-url
        search_documents,        # POST /documents/search
        chat_with_document,      # POST /documents/chat
        upload_document,         # POST /documents/
        set_document_folder,     # POST /documents/{document_id}/folder
        get_document,            # GET /documents/{document_id} - ПОСЛЕ специфичных путей!
        delete_document,         # DELETE /documents/{document_id}
    ],
    tags=["Documents"],
)
