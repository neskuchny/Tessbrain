# -*- coding: utf-8 -*-
"""
API Routes для генерации документов.

Endpoints:
- GET /documents - список сгенерированных документов
- GET /documents/{id} - получить документ
- POST /documents/generate - сгенерировать документ по теме
- POST /documents/discover - запустить обнаружение и генерацию
- GET /documents/{id}/export - экспортировать документ
- GET /documents/candidates - кандидаты на генерацию
"""

import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from litestar import Controller, delete, get, post
from litestar.exceptions import NotFoundException
from litestar.params import Parameter
from litestar.response import Response
from pydantic import BaseModel, Field

from backend.api.middleware.auth_middleware import get_user_id_from_token
from backend.core.documents import DocumentGenerationSystem
from backend.core.documents import doc_store
from backend.core.llm.router import get_llm_router
from backend.core.llm.usage_tracker import cost_scope
from backend.core.store.tenant_paths import DEFAULT_USER_ID
from backend.db.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)


def _sanitize_filename_component(value: str) -> str:
    """
    Подготовить строку к использованию в имени файла (для Content-Disposition).
    - убираем управляющие символы (включая \\r/\\n/\\t)
    - убираем потенциально опасные разделители путей
    - нормализуем пробелы
    """
    if not value:
        return ""
    # Control chars + DEL
    value = re.sub(r"[\x00-\x1f\x7f]+", "", value)
    value = value.replace("\\", "_").replace("/", "_")
    value = value.strip()
    value = re.sub(r"\s+", " ", value)
    return value

# Singleton для системы генерации
_doc_gen_system: Optional[DocumentGenerationSystem] = None


def get_doc_gen_system() -> DocumentGenerationSystem:
    """Получить или создать систему генерации документов."""
    global _doc_gen_system
    if _doc_gen_system is None:
        llm = get_llm_router()
        _doc_gen_system = DocumentGenerationSystem(llm)
        logger.info("✅ DocumentGenerationSystem initialized")
    return _doc_gen_system


def _resolve_user_id(authorization: Optional[str], user_id: Optional[str]) -> str:
    """Получить user_id: ТОКЕН ПЕРВИЧЕН, query — только если совпадает.

    Раньше `if user_id: return user_id` доверял query-параметру ПОВЕРХ
    валидного токена — любой мог читать/генерировать чужие документы через
    ?user_id=<жертва> (IDOR, аудит подтвердил). Теперь как в documents.py:
    берём uid из подписанного токена, чужой query → отказ.
    """
    from backend.core.auth.service_token import trusted_user_id
    headers = {"authorization": authorization} if authorization else {}
    try:
        uid, src = trusted_user_id(headers, user_id or "")
        if src != "unverified" and uid:
            return uid  # доверенный токен победил (и сверил query, если был)
    except PermissionError:
        logger.warning("documents_gen: токен валиден, но запрошен чужой user_id — отказ")
        raise
    # Валидного токена нет — БЕЗ query-fallback (это и была дыра)
    logger.warning("documents_gen: нет валидного токена — отказ (anti-IDOR)")
    raise PermissionError("documents_gen: authentication required")


# ============================================
# Request/Response Models
# ============================================

class GenerateDocumentRequest(BaseModel):
    """Запрос на генерацию документа."""
    topic: str = Field(..., description="Тема документа")
    document_type: str = Field(default="regulation", description="Тип: regulation, sop, checklist, specification")
    meeting_ids: List[str] = Field(default_factory=list, description="ID встреч для использования")
    transcript: Optional[str] = Field(default=None, description="Вставленный текст вместо/вместе со встречами")
    model_tier: str = Field(default="standard", description="standard или premium")


class DiscoverDocumentsRequest(BaseModel):
    """Запрос на обнаружение и генерацию документов."""
    min_meetings: int = Field(default=1, description="Минимум встреч для создания документа")
    force_regenerate: bool = Field(default=False, description="Пересоздать все документы")


class DocumentResponse(BaseModel):
    """Ответ с документом."""
    document_id: str
    title: str
    document_type: str
    version: str
    created_at: str
    updated_at: str
    summary: str
    topic: str
    keywords: List[str]
    source_meetings: List[str]
    status: str
    word_count: int
    content_preview: str  # Первые 500 символов


class DocumentDetailResponse(DocumentResponse):
    """Полный документ."""
    content_markdown: str
    content_html: str
    sections: List[Dict[str, Any]]
    table_of_contents: List[Dict[str, str]]


class CandidateResponse(BaseModel):
    """Кандидат на генерацию документа."""
    candidate_id: str
    meeting_id: str
    meeting_title: str
    content_type: str
    topic: str
    confidence: float
    created_at: str
    processed: bool


class DiscoveryResultResponse(BaseModel):
    """Результат обнаружения документов."""
    candidates_found: int
    clusters_created: int
    documents_generated: int
    documents_updated: int
    documents: List[DocumentResponse]
    errors: List[str]


# ============================================
# Controller
# ============================================

class GeneratedDocumentsController(Controller):
    """Контроллер для работы со сгенерированными документами."""

    path = "/generated-documents"
    tags = ["Generated Documents"]

    @get("/")
    async def list_documents(
        self,
        authorization: Optional[str] = Parameter(header="Authorization", default=None),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
        document_type: Optional[str] = Parameter(query="type", default=None),
    ) -> Dict[str, Any]:
        """Получить список сгенерированных документов для пользователя."""
        effective_user_id = _resolve_user_id(authorization, user_id)

        # Единый источник правды — Supabase (решает multi-worker). Фолбэк на
        # in-memory/JSON, если таблицы нет (миграция 091 не прогнана).
        try:
            rows = await doc_store.list_documents(effective_user_id, document_type)
            def _prev(md):
                return (md[:500] + "...") if md and len(md) > 500 else (md or "")
            return {
                "documents": [
                    {
                        "document_id": r["document_id"], "title": r.get("title", ""),
                        "document_type": r.get("document_type", ""), "version": r.get("version", ""),
                        "created_at": r.get("created_at"), "updated_at": r.get("updated_at"),
                        "summary": r.get("summary", ""), "topic": r.get("topic", ""),
                        "keywords": r.get("keywords", []), "source_meetings": r.get("source_meetings", []),
                        "status": r.get("status", "draft"), "word_count": r.get("word_count", 0),
                        "user_id": r.get("user_id", ""),
                        "folder": r.get("folder") or "",
                        "content_preview": _prev(r.get("content_markdown", "")),
                    }
                    for r in rows
                ],
                "total": len(rows),
            }
        except doc_store.StoreUnavailable:
            logger.warning("generated_documents table missing — fallback to JSON store")

        doc_gen = get_doc_gen_system()
        all_documents = (doc_gen.get_documents_by_type(document_type)
                         if document_type else doc_gen.get_all_documents())
        documents = [d for d in all_documents if not d.user_id or d.user_id == effective_user_id]
        return {
            "documents": [
                {
                    "document_id": d.document_id, "title": d.title,
                    "document_type": d.document_type, "version": d.version,
                    "created_at": d.created_at.isoformat(), "updated_at": d.updated_at.isoformat(),
                    "summary": d.summary, "topic": d.topic, "keywords": d.keywords,
                    "source_meetings": d.source_meetings, "status": d.status,
                    "word_count": d.word_count, "user_id": d.user_id,
                    "folder": getattr(d, "folder", "") or "",
                    "content_preview": d.content_markdown[:500] + "..." if len(d.content_markdown) > 500 else d.content_markdown,
                }
                for d in documents
            ],
            "total": len(documents),
        }

    @get("/{document_id:str}")
    async def get_document(
        self,
        document_id: str,
        authorization: Optional[str] = Parameter(header="Authorization", default=None),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
    ) -> Dict[str, Any]:
        """Получить документ по ID (только владелец; 404 если нет)."""
        effective_user_id = _resolve_user_id(authorization, user_id)

        # 404 вместо 200+{"error"} — раньше фронт клал объект-ошибку в стейт и
        # открывал битую модалку («регламенты не открываются»). Владельческая
        # проверка закрывает IDOR (чтение чужого документа по id).
        try:
            row = await doc_store.get_document(document_id, effective_user_id)
            if not row:
                raise NotFoundException(detail=f"Document not found: {document_id}")
            return {
                "document_id": row["document_id"], "title": row.get("title", ""),
                "document_type": row.get("document_type", ""), "version": row.get("version", ""),
                "created_at": row.get("created_at"), "updated_at": row.get("updated_at"),
                "summary": row.get("summary", ""), "topic": row.get("topic", ""),
                "keywords": row.get("keywords", []), "source_meetings": row.get("source_meetings", []),
                "authors": row.get("authors", []), "status": row.get("status", "draft"),
                "word_count": row.get("word_count", 0),
                "content_markdown": row.get("content_markdown", ""),
                "content_html": row.get("content_html", ""),
                "sections": row.get("sections", []),
                "table_of_contents": row.get("table_of_contents", []),
            }
        except doc_store.StoreUnavailable:
            pass  # фолбэк на in-memory ниже

        doc_gen = get_doc_gen_system()
        doc = doc_gen.get_document(document_id)
        if not doc or (doc.user_id and doc.user_id != effective_user_id):
            raise NotFoundException(detail=f"Document not found: {document_id}")

        return {
            "document_id": doc.document_id, "title": doc.title,
            "document_type": doc.document_type, "version": doc.version,
            "created_at": doc.created_at.isoformat(), "updated_at": doc.updated_at.isoformat(),
            "summary": doc.summary, "topic": doc.topic, "keywords": doc.keywords,
            "source_meetings": doc.source_meetings, "authors": doc.authors,
            "status": doc.status, "word_count": doc.word_count,
            "content_markdown": doc.content_markdown, "content_html": doc.content_html,
            "sections": doc.sections, "table_of_contents": doc.table_of_contents,
        }

    @get("/{document_id:str}/export")
    async def export_document(
        self,
        document_id: str,
        format: str = Parameter(query="format", default="markdown"),
        authorization: Optional[str] = Parameter(header="Authorization", default=None),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
    ) -> Response:
        """Экспортировать документ (только владелец)."""
        try:
            effective_user_id = _resolve_user_id(authorization, user_id)

            # Владельческая проверка ДО выдачи контента (закрывает IDOR-экспорт).
            # Единый источник — Supabase, фолбэк на in-memory.
            title = None
            content = None
            try:
                row = await doc_store.get_document(document_id, effective_user_id)
                if not row:
                    return Response(content={"error": "Document not found"}, status_code=404)
                title = row.get("title", "document")
                if format == "html":
                    content = row.get("content_html", "")
                elif format == "json":
                    content = json.dumps(row, ensure_ascii=False, indent=2)
                else:
                    content = row.get("content_markdown", "")
            except doc_store.StoreUnavailable:
                doc_gen = get_doc_gen_system()
                doc = doc_gen.get_document(document_id)
                if not doc or (doc.user_id and doc.user_id != effective_user_id):
                    return Response(content={"error": "Document not found"}, status_code=404)
                content = doc_gen.export_document(document_id, format)
                title = doc.title if doc else "document"

            if not content:
                return Response(
                    content={"error": "Document not found"},
                    status_code=404,
                )

            # Определяем content-type и расширение
            content_types = {
                "markdown": ("text/markdown", "md"),
                "html": ("text/html", "html"),
                "json": ("application/json", "json"),
            }

            content_type, ext = content_types.get(format, ("text/plain", "txt"))

            base_name = _sanitize_filename_component(title or "document") or "document"
            filename = f"{base_name.replace(' ', '_')}.{ext}"

            # RFC 5987: кодируем имя файла для поддержки кириллицы в HTTP заголовках
            filename_ascii = filename.encode("ascii", "ignore").decode("ascii")
            # ASCII fallback должен быть безопасным для заголовка: без кавычек/точек с запятой
            filename_ascii = filename_ascii.replace('"', "").replace(";", "")
            if not filename_ascii:
                filename_ascii = f"document.{ext}"

            filename_encoded = quote(filename, safe="")

            return Response(
                content=content,
                media_type=content_type,
                headers={
                    "Content-Disposition": f"attachment; filename=\"{filename_ascii}\"; filename*=UTF-8''{filename_encoded}"
                },
            )
        except Exception as e:
            logger.exception(f"❌ export_document failed: document_id={document_id}, format={format}, err={e}")
            return Response(content={"error": "Export failed", "details": str(e)}, status_code=500)

    @post("/generate")
    async def generate_document(
        self,
        data: GenerateDocumentRequest,
        authorization: Optional[str] = Parameter(header="Authorization", default=None),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
    ) -> Dict[str, Any]:
        """Сгенерировать документ по теме."""
        effective_user_id = _resolve_user_id(authorization, user_id)

        doc_gen = get_doc_gen_system()

        # Загружаем встречи С ТРАНСКРИПТАМИ
        meetings = []
        supabase = SupabaseClient()

        if data.meeting_ids:
            # Загружаем все встречи с транскриптами и фильтруем по ID
            all_user_meetings = await supabase.get_meetings_with_transcripts(
                user_id=effective_user_id,
                limit=200
            )
            meetings_by_id = {m["id"]: m for m in all_user_meetings}
            for meeting_id in data.meeting_ids:
                if meeting_id in meetings_by_id:
                    meeting = meetings_by_id[meeting_id]
                    # Проверяем что есть транскрипт
                    if meeting.get("transcription_text"):
                        meetings.append(meeting)
                        logger.info(f"📝 Meeting {meeting_id}: has transcript ({len(meeting.get('transcription_text', ''))} chars)")
                    else:
                        logger.warning(f"⚠️ Meeting {meeting_id}: no transcript")

        # Вставленный вручную текст → псевдо-встреча (раньше поле transcript
        # молча выбрасывалось: юзер вставлял текст, а документ собирался из
        # случайно совпавших по названию встреч). Пайплайн читает
        # transcription_text, так что псевдо-встреча проходит его целиком.
        if data.transcript and data.transcript.strip():
            from uuid import uuid4
            meetings.append({
                "id": f"manual_{uuid4().hex[:8]}",
                "title": data.topic,
                "transcription_text": data.transcript,
            })

        if not meetings:
            # Если встречи не указаны или без транскриптов, ищем по теме
            all_meetings = await supabase.get_meetings_with_transcripts(
                user_id=effective_user_id,
                limit=50
            )

            # Фильтруем по теме (простая эвристика по названию)
            topic_words = set(data.topic.lower().split())
            for m in all_meetings:
                if not m.get("transcription_text"):
                    continue
                title_words = set(m.get("title", "").lower().split())
                if topic_words & title_words:
                    meetings.append(m)

        if not meetings:
            return {
                "error": "No meetings found for this topic",
                "topic": data.topic
            }

        # Генерируем документ. cost_scope на корне — все LLM-вызовы пайплайна
        # атрибутируются на user_id/documents (раньше generate_for_topic был
        # вообще без UsageContext → токены летели в 'unknown' без user_id).
        try:
            async with cost_scope(
                user_id=effective_user_id, agent_mode="documents",
                request_type="document_generate_topic", surface="documents_gen",
            ):
                document = await doc_gen.generate_for_topic(
                    topic=data.topic,
                    meetings=meetings,
                    document_type=data.document_type,
                    user_id=effective_user_id,
                    model_tier=data.model_tier,
                )

            if not document:
                return {"error": "Failed to generate document"}

            # Персистим в Supabase (единый источник правды между воркерами).
            # Сбой персиста НЕ отменяет результат: документ уже сгенерирован
            # и сохранён в JSON внутри generate_for_topic — раньше 400 от
            # Supabase превращал готовый регламент в «Failed» для пользователя.
            persist_warning = ""
            try:
                await doc_store.save_document(document, effective_user_id)
            except doc_store.StoreUnavailable:
                pass  # таблицы нет — уже сохранено в JSON внутри generate_for_topic
            except Exception as pe:
                logger.error(f"doc persist failed (документ сохранён в JSON): {pe}")
                persist_warning = ("Документ создан, но не записался в базу "
                                   "(доступен локально). Детали в логе сервера.")

            return {
                "success": True,
                **({"persist_warning": persist_warning} if persist_warning else {}),
                "document": {
                    "document_id": document.document_id,
                    "title": document.title,
                    "document_type": document.document_type,
                    "word_count": document.word_count,
                    "content_preview": document.content_markdown[:1000]
                }
            }

        except Exception as e:
            logger.error(f"Document generation failed: {e}")
            return {"error": str(e)}

    @post("/discover")
    async def discover_and_generate(
        self,
        data: DiscoverDocumentsRequest,
        authorization: Optional[str] = Parameter(header="Authorization", default=None),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
    ) -> Dict[str, Any]:
        """
        Обнаружить темы и сгенерировать документы.

        ИНКРЕМЕНТАЛЬНАЯ ЛОГИКА:
        1. Берём только НЕПРОВЕРЕННЫЕ встречи (doc_generation_checked = false)
        2. Или встречи, помеченные как кандидаты (is_regulation_candidate = true)
        3. Генерируем документы только из кластеров кандидатов
        """
        effective_user_id = _resolve_user_id(authorization, user_id)

        doc_gen = get_doc_gen_system()
        supabase = SupabaseClient()

        # ШАГ 1: Получаем уже помеченные кандидаты из локального хранилища
        existing_candidates = list(doc_gen.candidates.values())
        unprocessed_candidates = [c for c in existing_candidates if not c.processed]

        logger.info(f"📋 Found {len(unprocessed_candidates)} unprocessed candidates in memory")

        # ШАГ 2: Если кандидатов мало — проверяем последние N встреч
        meetings_to_process = []

        if len(unprocessed_candidates) < 3 or data.force_regenerate:
            # Загружаем только последние 20 встреч для быстрой проверки
            meetings_raw = await supabase.get_meetings_with_transcripts(
                user_id=effective_user_id,
                limit=20  # Только последние 20, не все
            )

            logger.info(f"📥 Got {len(meetings_raw)} meetings from DB")

            # Логируем поля первой встречи для отладки
            if meetings_raw:
                sample = meetings_raw[0]
                logger.info(f"📋 Sample meeting fields: {list(sample.keys())}")
                # Ищем поле с контентом
                content_fields = [k for k, v in sample.items() if isinstance(v, str) and len(v) > 100]
                logger.info(f"📝 Fields with content >100 chars: {content_fields}")

            # Нормализуем и фильтруем — пробуем разные варианты названий полей
            for meeting in meetings_raw:
                content = (
                    meeting.get("transcription_text") or  # Правильное название поля!
                    meeting.get("transcription") or
                    meeting.get("transcript") or
                    meeting.get("transcript_text") or
                    meeting.get("text") or
                    meeting.get("content") or
                    meeting.get("summary") or
                    ""
                )
                if content and len(content) > 100:  # Снизил порог до 100
                    meetings_to_process.append({
                        "id": meeting.get("id"),
                        "meeting_id": meeting.get("meeting_id"),
                        "title": meeting.get("title", "Без названия"),
                        "transcript": content,
                        "summary": meeting.get("summary", ""),
                        "created_at": meeting.get("created_at"),
                    })

            logger.info(f"📊 Will process {len(meetings_to_process)} recent meetings (with content)")

        # ШАГ 3: Добавляем встречи из кандидатов
        {c.meeting_id for c in unprocessed_candidates}
        for candidate in unprocessed_candidates:
            # Проверяем, не добавлена ли уже эта встреча
            if not any(m.get("id") == candidate.meeting_id for m in meetings_to_process):
                # Загружаем данные встречи
                meeting_data = await supabase.get_meeting_details(
                    candidate.meeting_id, include_transcript=True,
                    user_id=effective_user_id)
                if meeting_data:
                    content = meeting_data.get("transcription_text") or meeting_data.get("transcription") or meeting_data.get("transcript") or meeting_data.get("summary")
                    if content:
                        meetings_to_process.append({
                            "id": meeting_data.get("id"),
                            "meeting_id": meeting_data.get("meeting_id"),
                            "title": meeting_data.get("title", candidate.meeting_title),
                            "transcript": content,
                            "summary": meeting_data.get("summary", ""),
                            "created_at": meeting_data.get("created_at"),
                        })

        if not meetings_to_process:
            return {
                "success": True,
                "message": "No new meetings to process",
                "candidates_found": len(unprocessed_candidates),
                "clusters_created": 0,
                "documents_generated": 0,
                "documents_updated": 0,
                "documents": [],
                "errors": []
            }

        logger.info(f"🚀 Starting document generation for {len(meetings_to_process)} meetings")

        # Запускаем обнаружение и генерацию. cost_scope с user_id — чтобы
        # весь discover-пайплайн (classifier→cluster→aggregator→writer)
        # атрибутировался на пользователя (раньше user_id=NULL → 'unknown').
        try:
            async with cost_scope(
                user_id=effective_user_id, agent_mode="documents",
                request_type="document_generation", surface="documents_gen",
            ):
                result = await doc_gen.discover_and_generate(
                    meetings=meetings_to_process,
                    existing_documents=doc_gen.get_all_documents(),
                    min_meetings_for_document=data.min_meetings,
                    force_regenerate=data.force_regenerate,
                    user_id=effective_user_id
                )

            # Персистим созданные/обновлённые документы в Supabase.
            # По-документно и best-effort: один сбой не отменяет ни остальные
            # записи, ни сам результат (JSON-стор уже содержит документы).
            for d in result.documents:
                try:
                    await doc_store.save_document(d, effective_user_id)
                except doc_store.StoreUnavailable:
                    break  # таблицы нет — уже сохранено в JSON
                except Exception as pe:
                    logger.error(f"doc persist failed ({d.document_id}): {pe}")

            # Помечаем обработанных кандидатов — иначе те же встречи
            # переклассифицируются при каждом «Найти» (лишние LLM + дубли)
            if unprocessed_candidates:
                doc_gen.mark_candidates_processed(
                    [c.candidate_id for c in unprocessed_candidates])

            return {
                "success": True,
                "candidates_found": result.candidates_found,
                "clusters_created": result.clusters_created,
                "documents_generated": result.documents_generated,
                "documents_updated": result.documents_updated,
                "documents": [
                    {
                        "document_id": d.document_id,
                        "title": d.title,
                        "document_type": d.document_type,
                        "word_count": d.word_count,
                        "topic": d.topic
                    }
                    for d in result.documents
                ],
                "errors": result.errors
            }

        except Exception as e:
            logger.error(f"Document discovery failed: {e}")
            return {"error": str(e)}

    @get("/candidates")
    async def list_candidates(
        self,
        authorization: Optional[str] = Parameter(header="Authorization", default=None),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
        processed: Optional[bool] = Parameter(query="processed", default=None),
    ) -> Dict[str, Any]:
        """Получить список кандидатов на генерацию документов (нужна аутентификация)."""
        _resolve_user_id(authorization, user_id)  # гейт: без валидного токена — 401
        doc_gen = get_doc_gen_system()
        candidates = doc_gen.get_candidates(processed=processed)

        return {
            "candidates": [
                {
                    "candidate_id": c.candidate_id,
                    "meeting_id": c.meeting_id,
                    "meeting_title": c.meeting_title,
                    "content_type": c.classification.content_type.value,
                    "topic": c.classification.topic,
                    "confidence": c.classification.confidence,
                    "created_at": c.created_at.isoformat(),
                    "processed": c.processed
                }
                for c in candidates
            ],
            "total": len(candidates)
        }

    @post("/{document_id:str}/folder")
    async def set_document_folder(
        self,
        document_id: str,
        data: Dict[str, Any],
        authorization: Optional[str] = Parameter(header="Authorization", default=None),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
    ) -> Dict[str, Any]:
        """Переложить документ в папку: body {"folder": "..."} ('' — убрать).

        Папки документов — плоские ярлыки (как у чат-сессий), не связаны
        с папками встреч MeetFlow.
        """
        effective_user_id = _resolve_user_id(authorization, user_id)
        folder = str((data or {}).get("folder") or "").strip()[:60]

        try:
            row = await doc_store.get_document(document_id, effective_user_id)
            if not row:
                raise NotFoundException(detail=f"Document not found: {document_id}")
            await doc_store.set_folder(document_id, effective_user_id, folder)
            return {"success": True, "document_id": document_id,
                    "folder": folder}
        except doc_store.StoreUnavailable as e:
            # без Supabase/миграции 269 — фолбэк на in-memory документ
            logger.warning(f"set_document_folder: store unavailable ({e})")

        doc_gen = get_doc_gen_system()
        doc = doc_gen.get_document(document_id)
        if not doc or (doc.user_id and doc.user_id != effective_user_id):
            raise NotFoundException(detail=f"Document not found: {document_id}")
        try:
            doc.folder = folder  # in-memory/JSON-стор: атрибут динамический
        except Exception:
            pass
        return {"success": True, "document_id": document_id, "folder": folder}

    @delete("/{document_id:str}", status_code=200)
    async def delete_document(
        self,
        document_id: str,
        authorization: Optional[str] = Parameter(header="Authorization", default=None),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
    ) -> Dict[str, Any]:
        """Удалить документ (только владелец)."""
        effective_user_id = _resolve_user_id(authorization, user_id)

        # Владельческая проверка — раньше любой мог удалить чужой документ по id
        try:
            row = await doc_store.get_document(document_id, effective_user_id)
            if not row:
                raise NotFoundException(detail=f"Document not found: {document_id}")
            await doc_store.delete_document(document_id, effective_user_id)
            # чистим и локальный кеш, если документ там осел
            get_doc_gen_system().delete_document(document_id)
            return {"success": True, "deleted": document_id}
        except doc_store.StoreUnavailable:
            pass  # фолбэк на in-memory

        doc_gen = get_doc_gen_system()
        doc = doc_gen.get_document(document_id)
        if not doc or (doc.user_id and doc.user_id != effective_user_id):
            raise NotFoundException(detail=f"Document not found: {document_id}")
        if doc_gen.delete_document(document_id):
            return {"success": True, "deleted": document_id}
        return {"error": "Document not found", "document_id": document_id}

