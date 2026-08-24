# -*- coding: utf-8 -*-
"""Сохранённые ресурсы компании: ссылки (сайт, прайс, документы, страницы),
которые пользователь хочет хранить и давать системе в работу.

Зачем: ТЗ/КП генерились «в вакууме» — системе негде было взять ссылку на
страницу компании, прайс-лист, шаблон КП. Теперь ссылки хранятся per-user и
автоматически подкладываются в контекст генерации ТЗ (data_driven, этап
сборки контекста) — и доступны для просмотра/редактирования в UI.

Паттерн — как entity_tombstones: per-user JSON, atomic, never-raise."""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_MAX_ITEMS = 200


def _dir() -> Path:
    p = Path("data") / "user_resources"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _path(user_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", user_id or "")[:64] or "anon"
    return _dir() / f"{safe}.json"


def list_resources(user_id: str) -> List[dict]:
    try:
        items = json.loads(_path(user_id).read_text(encoding="utf-8"))
        return items if isinstance(items, list) else []
    except Exception:
        return []


def add_resource(user_id: str, *, url: str, title: str = "",
                 note: str = "", kind: str = "link") -> Optional[dict]:
    """Добавить ссылку/ресурс. Дубликаты по url обновляются. Never-raise."""
    url = (url or "").strip()
    if not user_id or not url:
        return None
    try:
        items = list_resources(user_id)
        now = datetime.now(timezone.utc).isoformat()
        for it in items:
            if (it.get("url") or "").strip().lower() == url.lower():
                it.update({"title": (title or it.get("title") or "")[:200],
                           "note": (note or it.get("note") or "")[:500],
                           "kind": kind or it.get("kind") or "link",
                           "updated_at": now})
                _path(user_id).write_text(
                    json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
                return it
        rec = {"id": uuid.uuid4().hex[:8], "url": url[:1000],
               "title": (title or "")[:200], "note": (note or "")[:500],
               "kind": (kind or "link")[:40], "created_at": now}
        items.insert(0, rec)
        _path(user_id).write_text(
            json.dumps(items[:_MAX_ITEMS], ensure_ascii=False, indent=2),
            encoding="utf-8")
        return rec
    except Exception as e:
        logger.warning(f"add_resource failed: {e}")
        return None


def delete_resource(user_id: str, resource_id: str) -> bool:
    try:
        items = [it for it in list_resources(user_id)
                 if it.get("id") != resource_id]
        _path(user_id).write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception as e:
        logger.warning(f"delete_resource failed: {e}")
        return False


def resources_context_block(user_id: str, limit: int = 30) -> str:
    """Блок для LLM-контекста генерации ТЗ/документов. Пусто → ''."""
    items = list_resources(user_id)[:limit]
    if not items:
        return ""
    lines = ["=== РЕСУРСЫ КОМПАНИИ (сохранённые ссылки — используй в документе) ==="]
    for it in items:
        t = it.get("title") or it.get("kind") or "ссылка"
        note = f" — {it['note']}" if it.get("note") else ""
        lines.append(f"• {t}: {it.get('url')}{note}")
    return "\n".join(lines)
