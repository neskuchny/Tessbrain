# -*- coding: utf-8 -*-
"""Сохранение сгенерированных документов переживает дрейф схемы Supabase.

Реальный кейс из прода: PostgREST ответил 400 PGRST204 «Could not find the
'folder' column of 'generated_documents'», httpx поднял HTTPStatusError, у
которого в str(e) только «Client error '400 Bad Request'» — подробности
лежат в e.response.text. Проверки doc_store искали «folder» в str(e),
поэтому защитный ретрай без folder не срабатывал НИКОГДА, и готовый
регламент превращался для пользователя в «не создал».
"""
import asyncio
import pathlib
import sys
from datetime import datetime, timezone

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import httpx  # noqa: E402

from backend.core.documents import doc_store  # noqa: E402


def _http_error(status: int, body: str) -> httpx.HTTPStatusError:
    """Точная копия того, что поднимает SupabaseClient: статус в str(e),
    подробности PostgREST — только в e.response.text."""
    req = httpx.Request("POST", "https://x.supabase.co/rest/v1/generated_documents")
    resp = httpx.Response(status, request=req, text=body)
    return httpx.HTTPStatusError(
        f"Client error '{status}' for url '…'", request=req, response=resp)


class _Doc:
    document_id = "d1"
    user_id = "u1"
    title = "Регламент из встреч"
    document_type = "regulation"
    version = "1.0"
    created_at = datetime.now(timezone.utc)
    updated_at = datetime.now(timezone.utc)
    summary = ""
    topic = "регламент"
    content_markdown = "# Регламент"
    content_html = ""
    keywords = []
    source_meetings = []
    authors = []
    sections = []
    table_of_contents = []
    status = "draft"
    confidence = 0.9
    word_count = 2388
    folder = ""


def test_exc_text_includes_response_body():
    e = _http_error(400, '{"code":"PGRST204","message":"Could not find the '
                         "'folder' column of 'generated_documents'\"}")
    assert "folder" in doc_store._exc_text(e)
    assert "pgrst204" in doc_store._exc_text(e)
    assert "folder" not in str(e), \
        "прекондиция кейса: в str(e) подробностей нет — только в теле"


def test_missing_table_detected_from_body():
    e = _http_error(404, '{"code":"PGRST205","message":"Could not find the '
                         'table \'public.generated_documents\'"}')
    assert doc_store._is_missing_table(e)


def test_save_retries_without_folder_on_pgrst204(monkeypatch):
    """Первый POST падает PGRST204 по folder → ретрай без folder проходит."""
    calls = []

    class _Client:
        async def _request(self, method, table, json_data=None, headers=None,
                           params=None):
            calls.append(dict(json_data or {}))
            if "folder" in (json_data or {}):
                raise _http_error(
                    400, '{"code":"PGRST204","message":"Could not find the '
                         "'folder' column of 'generated_documents' in the "
                         'schema cache"}')
            return None

    monkeypatch.setattr(doc_store, "_client", lambda: _Client())
    asyncio.run(doc_store.save_document(_Doc(), "u1"))
    assert len(calls) == 2, "был ретрай"
    assert "folder" in calls[0] and "folder" not in calls[1]
    assert calls[1]["title"] == "Регламент из встреч", \
        "документ сохранён, а не потерян"


def test_missing_table_still_raises_store_unavailable(monkeypatch):
    class _Client:
        async def _request(self, *a, **kw):
            raise _http_error(
                404, '{"code":"PGRST205","message":"Could not find the table"}')

    monkeypatch.setattr(doc_store, "_client", lambda: _Client())
    try:
        asyncio.run(doc_store.save_document(_Doc(), "u1"))
        raise AssertionError("ожидали StoreUnavailable")
    except doc_store.StoreUnavailable:
        pass


def test_unrelated_error_still_raises(monkeypatch):
    """Чужая ошибка (например RLS) не маскируется ни ретраем, ни фолбэком."""
    class _Client:
        async def _request(self, *a, **kw):
            raise _http_error(
                403, '{"code":"42501","message":"permission denied"}')

    monkeypatch.setattr(doc_store, "_client", lambda: _Client())
    try:
        asyncio.run(doc_store.save_document(_Doc(), "u1"))
        raise AssertionError("ожидали проброс ошибки")
    except httpx.HTTPStatusError:
        pass
