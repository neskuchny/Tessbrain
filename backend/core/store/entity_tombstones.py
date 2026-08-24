# -*- coding: utf-8 -*-
"""Tombstone-память удалённых сущностей: «это НЕ сущность, не пересоздавать».

Кейс: транскрибация услышала «ИИшка» как «Ишка» → в снепшоте появился
«сотрудник Ишка». Пользователь удаляет его из снепшота — но извлечение из
следующей встречи создало бы узел заново. Tombstone решает: удаление — это
знание (имя+метка+комментарий), и create_node молча пропускает совпадения.

Паттерн хранения — как dedup_negative: per-user JSON, полное нормализованное
имя (нижний регистр, схлопнутые пробелы). Метка учитывается: удалённый
Person «ишка» не блокирует, например, Product с тем же именем — если метка
не указана, блокируются все метки.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

_MAX_ITEMS = 1000
# кеш: user_id → (mtime, {(name, label|'') ...})
_cache: Dict[str, Tuple[float, Set[Tuple[str, str]]]] = {}


def _dir() -> Path:
    p = Path("data") / "entity_tombstones"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _path(user_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", user_id or "")[:64] or "anon"
    return _dir() / f"{safe}.json"


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def load_tombstones(user_id: str) -> List[dict]:
    try:
        return json.loads(_path(user_id).read_text(encoding="utf-8"))
    except Exception:
        return []


def add_tombstone(user_id: str, name: str, label: Optional[str] = None,
                  comment: Optional[str] = None) -> bool:
    """Записать «сущность удалена, не пересоздавать». Never-raise."""
    if not user_id or not _norm(name):
        return False
    try:
        items = load_tombstones(user_id)
        key = (_norm(name), _norm(label or ""))
        for it in items:
            if (_norm(it.get("name", "")), _norm(it.get("label", ""))) == key:
                return True  # уже есть
        items.append({
            "name": _norm(name),
            "label": _norm(label or ""),
            "comment": (comment or "").strip()[:300],
            "deleted_at": datetime.now(timezone.utc).isoformat(),
        })
        _path(user_id).write_text(
            json.dumps(items[-_MAX_ITEMS:], ensure_ascii=False, indent=2),
            encoding="utf-8")
        _cache.pop(user_id, None)
        return True
    except Exception as e:
        logger.warning(f"add_tombstone failed: {e}")
        return False


def remove_tombstone(user_id: str, name: str, label: Optional[str] = None) -> bool:
    """Снять tombstone (передумали). Never-raise."""
    try:
        key = (_norm(name), _norm(label or ""))
        items = [it for it in load_tombstones(user_id)
                 if (_norm(it.get("name", "")), _norm(it.get("label", ""))) != key]
        _path(user_id).write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        _cache.pop(user_id, None)
        return True
    except Exception as e:
        logger.warning(f"remove_tombstone failed: {e}")
        return False


def is_tombstoned(user_id: str, name: str, label: Optional[str] = None) -> bool:
    """Проверка на горячем пути create_node: кеш по mtime, never-raise."""
    if not user_id or not name:
        return False
    try:
        p = _path(user_id)
        mtime = p.stat().st_mtime if p.exists() else 0.0
        cached = _cache.get(user_id)
        if cached is None or cached[0] != mtime:
            keys: Set[Tuple[str, str]] = set()
            for it in load_tombstones(user_id):
                keys.add((_norm(it.get("name", "")), _norm(it.get("label", ""))))
            _cache[user_id] = (mtime, keys)
            cached = _cache[user_id]
        keys = cached[1]
        if not keys:
            return False
        n, lb = _norm(name), _norm(label or "")
        return (n, lb) in keys or (n, "") in keys
    except Exception:
        return False
