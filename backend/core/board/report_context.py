# -*- coding: utf-8 -*-
"""Контекст последнего визуального отчёта — дно воронки «Спросить мозг».

Воронка визуального отчёта (VISUAL_REPORTS §2.2): взгляд → детали → текст →
ВОПРОС МОЗГУ. Последний шаг был оборван: картинка в Telegram — тупик,
спросить «что за динамит у отдела продаж?» некуда.

Замыкание: при доставке визуального отчёта его факты запоминаются per-user;
следующий вопрос пользователя боту (messenger chat_handler) получает этот
контекст — бот понимает, О КАКОМ отчёте спрашивают, без уточнений.

Свежесть 48 часов: старый отчёт не должен искажать несвязанные вопросы.
Never-raise: сбой стора не ломает ни доставку, ни чат.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_MAX_FACTS_CHARS = 2400   # хватает для контекста, не раздувает промпт бота
_FRESH_HOURS = 48


def _path(user_id: str) -> Optional[Path]:
    try:
        from backend.core.store.tenant_paths import _DATA_ROOT
        d = _DATA_ROOT / "report_context"
        d.mkdir(parents=True, exist_ok=True)
        su = "".join(c for c in str(user_id) if c.isalnum() or c in "-_")[:64]
        return (d / f"{su}.json") if su else None
    except Exception:
        logger.debug("report context path failed", exc_info=True)
        return None


def remember_report(user_id: Optional[str], title: str, facts: str) -> None:
    """Запомнить доставленный визуальный отчёт (заголовок + текст-дубль)."""
    if not user_id or not str(facts or "").strip():
        return
    p = _path(user_id)
    if not p:
        return
    try:
        p.write_text(json.dumps({
            "title": str(title or "").strip()[:160],
            "facts": str(facts).strip()[:_MAX_FACTS_CHARS],
            "at": datetime.now(timezone.utc).isoformat(),
        }, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    except Exception:
        logger.debug("report context write failed", exc_info=True)


def recent_report(user_id: Optional[str],
                  max_age_hours: int = _FRESH_HOURS) -> Optional[Dict[str, Any]]:
    """Последний отчёт, если он СВЕЖИЙ (иначе None — не искажаем чат)."""
    if not user_id:
        return None
    p = _path(user_id)
    if not p or not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8")) or {}
        at = datetime.fromisoformat(str(data.get("at") or ""))
        age_h = (datetime.now(timezone.utc) - at).total_seconds() / 3600.0
        if age_h > max_age_hours:
            return None
        if not str(data.get("facts") or "").strip():
            return None
        return data
    except Exception:
        logger.debug("report context read failed", exc_info=True)
        return None


def context_block(user_id: Optional[str]) -> str:
    """Блок контекста для промпта бота ('' если свежего отчёта нет)."""
    rep = recent_report(user_id)
    if not rep:
        return ""
    title = rep.get("title") or "визуальный отчёт"
    return (f"[Контекст: пользователь недавно получил визуальный отчёт "
            f"«{title}». Его содержание:\n{rep.get('facts')}\n"
            f"Если вопрос похож на вопрос об этом отчёте (что подсвечено, "
            f"что за символ/событие, почему) — отвечай по этому содержанию; "
            f"иначе игнорируй контекст.]")


__all__ = ["remember_report", "recent_report", "context_block"]
