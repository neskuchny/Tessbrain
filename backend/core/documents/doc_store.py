# -*- coding: utf-8 -*-
"""
Supabase-хранилище сгенерированных документов (таблица generated_documents).

Заменяет один JSON-файл на процесс (multi-worker невидимость + потеря данных
при конкурентной записи). Единый источник правды — БД.

Все функции — graceful: если таблицы нет (миграция 091 не прогнана) или
Supabase не настроен, поднимают StoreUnavailable, и вызывающий откатывается
на in-memory/JSON-стор DocumentGenerationSystem. Так работает и до, и после
миграции.
"""
import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_TABLE = "/rest/v1/generated_documents"

# Поля-JSON, которые PostgREST хранит как jsonb (сериализуем/десериализуем).
_JSON_FIELDS = ("keywords", "source_meetings", "authors", "sections", "table_of_contents")


class StoreUnavailable(Exception):
    """Таблица недоступна (нет миграции / не настроен Supabase) — фолбэк на JSON."""


def _client():
    from backend.db.supabase_client import SupabaseClient
    return SupabaseClient()


def _exc_text(exc: Exception) -> str:
    """Текст ошибки ВМЕСТЕ с телом HTTP-ответа.

    httpx.HTTPStatusError в str(e) несёт только статус («Client error '400
    Bad Request' for url …»), а подробности PostgREST (PGRST204 «Could not
    find the 'folder' column», PGRST205 «Could not find the table») лежат в
    e.response.text. Все проверки ниже, смотревшие в str(e), из-за этого не
    срабатывали никогда: ретрай без folder не запускался, и падение
    сохранения убивало весь запрос генерации документа (реальный кейс:
    «Не создал регламент по встрече», хотя регламент был сгенерирован)."""
    parts = [str(exc)]
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            parts.append(str(resp.text or "")[:500])
        except Exception:
            pass
    cause = getattr(exc, "__cause__", None)
    if cause is not None and cause is not exc:
        parts.append(str(cause))
    return " | ".join(p for p in parts if p).lower()


def _is_missing_table(exc: Exception) -> bool:
    """PostgREST при отсутствии таблицы отвечает 404/PGRST205 'Could not find the table'."""
    s = _exc_text(exc)
    return ("could not find the table" in s or "pgrst205" in s
            or "does not exist" in s or "42p01" in s)


def doc_to_row(doc, user_id: str) -> Dict[str, Any]:
    """GeneratedDocument → строка для Supabase."""
    return {
        "document_id": doc.document_id,
        "user_id": user_id or doc.user_id or "",
        "title": doc.title or "",
        "document_type": doc.document_type or "regulation",
        "version": doc.version or "1.0",
        "created_at": doc.created_at.isoformat(),
        "updated_at": doc.updated_at.isoformat(),
        "summary": doc.summary or "",
        "topic": doc.topic or "",
        "content_markdown": doc.content_markdown or "",
        "content_html": doc.content_html or "",
        "keywords": doc.keywords or [],
        "source_meetings": doc.source_meetings or [],
        "authors": doc.authors or [],
        "sections": doc.sections or [],
        "table_of_contents": doc.table_of_contents or [],
        "status": doc.status or "draft",
        "confidence": float(doc.confidence or 0.0),
        "word_count": int(doc.word_count or 0),
        "folder": getattr(doc, "folder", "") or "",
    }


def _normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Строка Supabase → dict API-формата (jsonb-поля уже list/dict у PostgREST)."""
    out = dict(row)
    for f in _JSON_FIELDS:
        v = out.get(f)
        if isinstance(v, str):  # на случай, если пришло строкой
            try:
                out[f] = json.loads(v)
            except Exception:
                out[f] = []
        elif v is None:
            out[f] = []
    return out


async def save_document(doc, user_id: str) -> None:
    """Upsert документа (по document_id)."""
    row = doc_to_row(doc, user_id)
    try:
        await _client()._request(
            "POST", _TABLE,
            json_data=row,
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
        )
    except Exception as e:
        # Колонка folder появляется миграцией 280 — до её применения
        # сохранение НЕ должно ломаться: ретрай без folder. Ищем «folder» в
        # ПОЛНОМ тексте ошибки (_exc_text): подробность PGRST204 лежит в теле
        # HTTP-ответа, а не в str(e).
        if "folder" in _exc_text(e) and "folder" in row:
            row.pop("folder", None)
            try:
                await _client()._request(
                    "POST", _TABLE, json_data=row,
                    headers={"Prefer":
                             "resolution=merge-duplicates,return=minimal"})
                logger.warning("generated_documents.folder не сохранён: "
                               "примени миграцию "
                               "280_generated_documents_folder.sql")
                return
            except Exception as e2:
                e = e2
        if _is_missing_table(e) or "not configured" in _exc_text(e):
            raise StoreUnavailable(str(e)) from e
        raise


async def list_documents(user_id: str, document_type: Optional[str] = None) -> List[Dict[str, Any]]:
    """Документы владельца (опц. по типу), новые сверху."""
    params = {
        "user_id": f"eq.{user_id}",
        "select": "*",
        "order": "created_at.desc",
    }
    if document_type:
        params["document_type"] = f"eq.{document_type}"
    try:
        rows = await _client()._request("GET", _TABLE, params=params)
        return [_normalize_row(r) for r in (rows or [])]
    except Exception as e:
        if _is_missing_table(e) or "not configured" in _exc_text(e):
            raise StoreUnavailable(str(e)) from e
        raise


async def get_document(document_id: str, user_id: str) -> Optional[Dict[str, Any]]:
    """Один документ владельца (или None)."""
    params = {
        "document_id": f"eq.{document_id}",
        "user_id": f"eq.{user_id}",
        "select": "*",
        "limit": "1",
    }
    try:
        rows = await _client()._request("GET", _TABLE, params=params)
        return _normalize_row(rows[0]) if rows else None
    except Exception as e:
        if _is_missing_table(e) or "not configured" in _exc_text(e):
            raise StoreUnavailable(str(e)) from e
        raise


async def set_folder(document_id: str, user_id: str, folder: str) -> bool:
    """Переложить документ владельца в папку ('' — убрать из папки)."""
    try:
        await _client()._request(
            "PATCH", _TABLE,
            params={"document_id": f"eq.{document_id}",
                    "user_id": f"eq.{user_id}"},
            json_data={"folder": (folder or "").strip()[:60]},
            headers={"Prefer": "return=minimal"},
        )
        return True
    except Exception as e:
        if "folder" in _exc_text(e):
            raise StoreUnavailable(
                "колонка generated_documents.folder отсутствует — примени "
                "миграцию 280_generated_documents_folder.sql") from e
        if _is_missing_table(e) or "not configured" in _exc_text(e):
            raise StoreUnavailable(str(e)) from e
        raise


async def delete_document(document_id: str, user_id: str) -> bool:
    """Удалить документ владельца."""
    try:
        await _client()._request(
            "DELETE", _TABLE,
            params={"document_id": f"eq.{document_id}", "user_id": f"eq.{user_id}"},
        )
        return True
    except Exception as e:
        if _is_missing_table(e) or "not configured" in _exc_text(e):
            raise StoreUnavailable(str(e)) from e
        raise
