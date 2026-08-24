# -*- coding: utf-8 -*-
"""Сериальность визуальных отчётов — Thread Model (Visual Reports §17).

Значимые сюжеты («риск задержки клиента», «выгорание команды») получают стабильный
идентификатор и УЗНАВАЕМЫЙ визуальный символ, переходящий из отчёта в отчёт. Так
недельные/месячные отчёты складываются в «сериал развития компании»: новый сюжет
появляется, продолжающийся сохраняет символ, нарастающий — крупнее, решённый —
визуально завершается.

Хранилище — файловое, на (user_id + scope). scope склеивает серию (напр. недельный
пульс одной команды). Всё опционально/never-raise: нет сюжетов → поведение прежнее.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_STATUSES = {"new", "continuing", "escalating", "improving", "resolved", "stale"}
# Пул РИСУЕМЫХ символов — стабильно закрепляется за сюжетом по его id.
_SYMBOLS = [
    "треснувший мост", "растущая зелёная стрелка", "грозовая туча", "песочные часы",
    "щит с трещиной", "маяк в тумане", "разорванная цепь", "росток сквозь асфальт",
    "воронка", "компас", "перегруженный мост", "тлеющий механизм",
]


def _norm_title(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def _symbol_for(thread_id: str) -> str:
    h = int(hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:8], 16)
    return _SYMBOLS[h % len(_SYMBOLS)]


def _path(user_id: str, scope: str) -> Optional[str]:
    try:
        from backend.core.store.tenant_paths import _DATA_ROOT
        d = _DATA_ROOT / "report_threads"
        d.mkdir(parents=True, exist_ok=True)
        su = "".join(c for c in str(user_id) if c.isalnum() or c in "-_")[:64]
        sc = "".join(c for c in str(scope or "default") if c.isalnum() or c in "-_")[:48] or "default"
        if not su:
            return None
        return str(d / f"{su}__{sc}.json")
    except Exception:
        logger.debug("threads path failed", exc_info=True)
        return None


def _load(p: str) -> Dict[str, Any]:
    try:
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        logger.debug("threads load failed", exc_info=True)
    return {}


def reconcile(user_id: Optional[str], scope: str,
              incoming: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Сверить сюжеты текущего отчёта с сохранёнными: сматчить по названию,
    обновить статус, закрепить стабильный символ, новым — выдать id и символ.

    incoming: [{"title": str, "status": new|continuing|escalating|improving|resolved}].
    Возврат: обогащённые [{title, status, symbol, thread_id, is_new, seen_count}].
    Never-raise; при любой проблеме возвращает вход с локально выданными символами."""
    items = [t for t in (incoming or []) if isinstance(t, dict) and str(t.get("title") or "").strip()]
    if not items:
        return []
    from datetime import date as _date
    today = _date.today().isoformat()
    p = _path(user_id or "", scope) if user_id else None
    store = _load(p) if p else {}
    threads: Dict[str, Any] = store.get("threads") if isinstance(store.get("threads"), dict) else {}
    # индекс существующих по нормализованному названию
    by_title = {}
    for tid, t in threads.items():
        by_title[_norm_title(t.get("title"))] = tid

    out: List[Dict[str, Any]] = []
    touched: set = set()
    for it in items[:8]:
        title = str(it.get("title") or "").strip()
        status = str(it.get("status") or "").strip().lower()
        if status not in _STATUSES:
            status = "continuing"
        nt = _norm_title(title)
        # матч: точное нормализованное равенство ИЛИ вложенность (устойчиво к вариациям)
        tid = by_title.get(nt)
        if not tid:
            for other_nt, other_tid in by_title.items():
                if other_nt and (other_nt in nt or nt in other_nt) and abs(len(other_nt) - len(nt)) <= 12:
                    tid = other_tid
                    break
        is_new = tid is None
        if is_new:
            tid = "thr_" + hashlib.sha256((nt + "|" + str(scope)).encode("utf-8")).hexdigest()[:10]
        prev = threads.get(tid) or {}
        symbol = str(prev.get("symbol") or "").strip() or _symbol_for(tid)
        seen = int(prev.get("seen_count") or 0) + 1
        first_seen = str(prev.get("first_seen") or today)
        threads[tid] = {"title": title, "status": status, "symbol": symbol,
                        "seen_count": seen, "first_seen": first_seen,
                        "last_seen": today, "missed_count": 0}
        by_title[nt] = tid
        touched.add(tid)
        out.append({"title": title, "status": status, "symbol": symbol,
                    "thread_id": tid, "is_new": is_new, "seen_count": seen,
                    "first_seen": first_seen})

    # Жизненный цикл памяти (иначе стор растёт вечно и «хвосты» не умирают):
    # resolved показали финал ОДИН раз → выходят из серии; не упомянутые в
    # этом отчёте копят missed_count и после 3 пропусков забываются.
    for tid in list(threads.keys()):
        if tid in touched:
            if threads[tid].get("status") == "resolved":
                del threads[tid]          # финал показан — сюжет завершён
            continue
        missed = int(threads[tid].get("missed_count") or 0) + 1
        if missed >= 3:
            del threads[tid]
        else:
            threads[tid]["missed_count"] = missed

    # persist (best-effort)
    if p:
        try:
            store["threads"] = threads
            with open(p, "w", encoding="utf-8") as f:
                json.dump(store, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.debug("threads write failed", exc_info=True)
    return out


def threads_prompt_block(threads: List[Dict[str, Any]], lang_ru: bool = True) -> str:
    """Директива непрерывности для промпта: продолжающиеся сюжеты со СТАБИЛЬНЫМ
    символом; нарастающий — крупнее/опаснее; решённый — визуально завершён."""
    ts = [t for t in (threads or []) if isinstance(t, dict) and t.get("title")]
    if not ts:
        return ""
    rows = []
    for t in ts[:6]:
        status = str(t.get("status") or "continuing")
        sym = str(t.get("symbol") or "").strip()
        title = str(t.get("title") or "").strip()
        seen = int(t.get("seen_count") or 1)
        row = f"«{title}» — символ «{sym}», статус: {status}"
        # ПАМЯТЬ-НАКОПЛЕНИЕ: хвост, тянущийся не первый отчёт, обязан визуально
        # РАСТИ (ком больше, гиря тяжелее) — иначе затянувшееся не заметят.
        if seen >= 2 and status not in ("resolved", "improving"):
            since = str(t.get("first_seen") or "").strip()
            row += (f"; ТЯНЕТСЯ {seen}-й отчёт подряд"
                    + (f" (с {since})" if since else "")
                    + f" — рисуй ЗАМЕТНО крупнее/тяжелее прошлого, с меткой ×{seen}"
                    if lang_ru else
                    f"; DRAGGING for {seen} reports"
                    + (f" (since {since})" if since else "")
                    + f" — draw it NOTICEABLY bigger/heavier than last time, tag ×{seen}")
        rows.append(row)
    joined = "; ".join(rows)
    if lang_ru:
        return (" СЮЖЕТЫ-НИТИ (сериальность): у каждого — свой УЗНАВАЕМЫЙ символ, "
                "тот же из отчёта в отчёт; нарастающий сюжет крупнее/тревожнее, "
                "улучшающийся — восстанавливается, решённый — визуально завершён "
                "(покажи финал). " + joined + ".")
    return (" STORY THREADS (seriality): each keeps its RECOGNIZABLE symbol across "
            "reports; an escalating thread is larger/more alarming, improving one "
            "recovers, resolved one is visually completed (show the finale). "
            + joined + ".")


# ── Память сцены: чем закончился ПРОШЛЫЙ отчёт серии (анти-повтор) ────────

def remember_scene(user_id: Optional[str], scope: str, descriptor: str) -> None:
    """Запомнить, как выглядел этот отчёт серии (стиль/вариация/хвост промпта).
    Храним последние 3 — следующий отчёт получит директиву «сделай иначе».
    Never-raise."""
    d = str(descriptor or "").strip()[:300]
    if not d:
        return
    p = _path(user_id or "", scope) if user_id else None
    if not p:
        return
    try:
        from datetime import date as _date
        store = _load(p)
        scenes = store.get("scenes") if isinstance(store.get("scenes"), list) else []
        scenes.append({"d": d, "at": _date.today().isoformat()})
        store["scenes"] = scenes[-3:]
        with open(p, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.debug("scene remember failed", exc_info=True)


def previous_scene(user_id: Optional[str], scope: str) -> str:
    """Дескриптор ПОСЛЕДНЕЙ сцены серии ('' если серии ещё нет)."""
    p = _path(user_id or "", scope) if user_id else None
    if not p:
        return ""
    scenes = _load(p).get("scenes") or []
    if not isinstance(scenes, list) or not scenes:
        return ""
    return str((scenes[-1] or {}).get("d") or "")


def anti_repeat_block(prev_descriptor: str, lang_ru: bool = True) -> str:
    """Директива «не повторяй прошлую композицию» — вариативность против
    баннерной слепоты (§3 VISUAL_REPORTS): метафорический язык стабилен,
    исполнение каждый раз другое."""
    d = str(prev_descriptor or "").strip()
    if not d:
        return ""
    if lang_ru:
        return (" ПАМЯТЬ СЕРИИ: прошлый отчёт выглядел так — «" + d + "». "
                "Сохрани метафорический язык и символы сюжетов, но сделай "
                "ЗАМЕТНО другую композицию, палитру и ракурс — образ не должен "
                "повторяться буквально.")
    return (" SERIES MEMORY: the previous report looked like — \"" + d + "\". "
            "Keep the metaphor language and thread symbols, but use a "
            "NOTICEABLY different composition, palette and angle — the image "
            "must not repeat literally.")


__all__ = ["reconcile", "threads_prompt_block", "remember_scene",
           "previous_scene", "anti_repeat_block"]


__all__ = ["reconcile", "threads_prompt_block"]
