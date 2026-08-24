# -*- coding: utf-8 -*-
"""Библиотека результатов.

Сохраняет произвольный сгенерированный результат (медиаплан, выполненное ТЗ,
собранный документ) в общий стор `generated_documents`, чтобы он был виден в
списке «Сгенерированные» и не терялся (жалоба «ничего не сохраняется»).

Пишем прямо в тот же JSON-файл, что и DocumentGenerationSystem (роут-синглтон
перечитывает его по mtime — см. `_maybe_reload`), в ЕГО формате — но без
инициализации тяжёлой системы с LLM-агентами. Плюс best-effort upsert в
Supabase (источник правды между воркерами). Никогда не бросает: сохранение
результата не должно ронять саму генерацию.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

# Тот же файл, что и DocumentGenerationSystem._storage_file (data/…).
_STORAGE_FILE = str(
    Path(__file__).parent.parent.parent.parent / "data" / "generated_documents.json"
)


def _doc_json(*, document_id: str, user_id: str, title: str, content_markdown: str,
              document_type: str, summary: str, topic: str,
              source_meetings: List[str], now: datetime) -> Dict[str, Any]:
    """Документ в формате save_to_file DocumentGenerationSystem."""
    return {
        "document_id": document_id,
        "title": title,
        "document_type": document_type,
        "version": "1.0",
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "summary": summary,
        "content_markdown": content_markdown,
        "content_html": "",
        "topic": topic,
        "keywords": [],
        "source_meetings": source_meetings,
        "authors": [],
        "sections": [],
        "table_of_contents": [],
        "status": "draft",
        "confidence": 0.0,
        "word_count": len((content_markdown or "").split()),
        "user_id": user_id,
    }


def _append_to_json_store(doc_json: Dict[str, Any], storage_file: str = _STORAGE_FILE) -> None:
    """Дописать документ в общий generated_documents.json. Атомарно (tmp+replace),
    дедуп по document_id, merge с тем, что уже на диске (не теряем чужое)."""
    data: Dict[str, Any] = {"documents": [], "candidates": []}
    try:
        if os.path.exists(storage_file):
            with open(storage_file, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
    except Exception as e:
        logger.warning("results_library: read store failed: %s", e)
        data = {"documents": [], "candidates": []}
    data.setdefault("documents", [])
    data.setdefault("candidates", [])
    known = {d.get("document_id") for d in data["documents"]}
    if doc_json["document_id"] not in known:
        data["documents"].append(doc_json)
    Path(storage_file).parent.mkdir(parents=True, exist_ok=True)
    tmp = f"{storage_file}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, storage_file)


async def save_result_document(
    user_id: str,
    *,
    title: str,
    content_markdown: str,
    document_type: str = "result",
    summary: str = "",
    topic: str = "",
    source_meetings: Optional[List[str]] = None,
    storage_file: str = _STORAGE_FILE,
) -> Dict[str, str]:
    """Сохранить результат в библиотеку. Возвращает {document_id, title}.

    document_type помечает вид результата (mediaplan | task_result | document…)
    — по нему список «Сгенерированные» можно фильтровать. Best-effort и
    never-raise."""
    document_id = str(uuid4())
    now = datetime.utcnow()
    title = (title or "Результат").strip()[:200]
    doc_json = _doc_json(
        document_id=document_id, user_id=user_id or "", title=title,
        content_markdown=content_markdown or "", document_type=document_type or "result",
        summary=(summary or "")[:1000], topic=(topic or "")[:200],
        source_meetings=list(source_meetings or []), now=now,
    )
    # 1. JSON-стор — виден в списке-фолбэке даже без Supabase.
    try:
        _append_to_json_store(doc_json, storage_file)
    except Exception as e:
        logger.warning("results_library: json save failed: %s", e)
    # 2. Supabase — единый источник между воркерами, best-effort.
    try:
        from backend.core.documents import doc_store
        from backend.core.documents.document_writer_agent import GeneratedDocument
        doc = GeneratedDocument(
            document_id=document_id, title=title,
            document_type=document_type or "result", version="1.0",
            created_at=now, updated_at=now, summary=(summary or "")[:1000],
            content_markdown=content_markdown or "", content_html="",
            topic=(topic or "")[:200], source_meetings=list(source_meetings or []),
            word_count=len((content_markdown or "").split()),
            status="draft", user_id=user_id or "",
        )
        await doc_store.save_document(doc, user_id)
    except Exception as e:
        logger.debug("results_library: supabase save skipped: %s", e)
    return {"document_id": document_id, "title": title}
