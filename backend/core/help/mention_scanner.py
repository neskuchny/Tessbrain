# -*- coding: utf-8 -*-
"""
Скан НЕЯВНЫХ упоминаний Tessbrain/MeetFlow с проблемой/непониманием в памяти
компании (когда жалуются не через форму, а в рабочих чатах/на встречах).

ТОКЕНО-ЭКОНОМНО: фильтрация чисто по ключевым словам (БЕЗ LLM) — упоминание
продукта РЯДОМ с негативом/непониманием. LLM-сводка — опционально и отдельно
(gated), по умолчанию отчёт сырой. Скан по компактным текст-полям узлов графа
(title/summary/description), НЕ по полным транскриптам. Best-effort, no-raise.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Упоминание продукта.
_PRODUCT = ("tessent", "тесент", "тессент", "meetflow", "мит флоу", "мидфлоу",
            "call insight", "callinsight", "колл инсайт")
# Маркеры проблемы/непонимания.
_NEGATIVE = (
    "не работа", "не пойм", "не понима", "непонятн", "не получ", "неудоб",
    "сломал", "не грузит", "не загруж", "баг", "ошибк", "тормоз", "глюч",
    "зависа", "не открыва", "не сохран", "странно работает", "плохо работает",
    "не нашёл", "не нашел", "как это", "куда нажать", "не отправля",
)
# Поля узлов, где может быть человеческий текст (компактные, не транскрипт).
_TEXT_FIELDS = ("title", "name", "summary", "description", "text", "content",
                "note", "notes")


def _node_text(d: dict[str, Any]) -> str:
    parts = []
    for f in _TEXT_FIELDS:
        v = d.get(f)
        if isinstance(v, str) and v.strip():
            parts.append(v)
    return " \n".join(parts)


async def scan_mentions(user_id: str, *, limit: int = 40) -> list[dict[str, Any]]:
    """Найти неявные упоминания продукта с негативом. Возвращает список
    {snippet, label, marker}. Без LLM. {} нет графа."""
    if not user_id:
        return []
    gb = None
    out: list[dict[str, Any]] = []
    try:
        from backend.core.store.graph_builder import GraphBuilder
        from backend.core.store.tenant_paths import graph_path_for_user
        gb = GraphBuilder(use_networkx=None,
                          graph_storage_path=graph_path_for_user(user_id))
        await gb.connect()
        if not (gb.connected and gb.nx_graph):
            return []
        for _nid, d in gb.nx_graph.nodes(data=True):
            text = _node_text(d)
            if not text:
                continue
            low = text.lower()
            if not any(p in low for p in _PRODUCT):
                continue
            marker = next((m for m in _NEGATIVE if m in low), "")
            if not marker:
                continue
            out.append({
                "snippet": text.strip()[:280],
                "label": str(d.get("_label") or d.get("label") or ""),
                "marker": marker,
            })
            if len(out) >= limit:
                break
    except Exception as e:
        logger.debug("mention scan failed: %s", e)
    finally:
        if gb is not None:
            try:
                await gb.close(save=False)
            except Exception:
                pass
    return out


def mention_scan_enabled() -> bool:
    import os
    return os.getenv("GUIDE_MENTION_SCAN", "").strip().lower() in ("1", "on", "true", "yes")


async def build_mention_report(*, user_ids: list[str] | None = None,
                               limit_users: int = 500,
                               use_llm: bool | None = None) -> dict[str, Any]:
    """Агрегат неявных упоминаний для РАЗРАБОТЧИКОВ — без сырого текста клиента
    (только счётчики/маркеры/разделы), приватно. LLM — только если явно
    включён (env GUIDE_MENTION_LLM=on), по умолчанию 0 токенов.

    Возвращает {count, users_affected, by_marker, by_label, summary}."""
    import os
    from collections import Counter

    if user_ids is None:
        try:
            from backend.memory.user_profiles import get_user_profile_service
            user_ids = await get_user_profile_service().list_user_ids(limit=limit_users)
        except Exception:
            user_ids = []

    by_marker: Counter = Counter()
    by_label: Counter = Counter()
    affected = 0
    total = 0
    for uid in (user_ids or []):
        try:
            hits = await scan_mentions(uid, limit=40)
        except Exception:
            hits = []
        if hits:
            affected += 1
            total += len(hits)
            for h in hits:
                by_marker[h.get("marker", "")] += 1
                by_label[h.get("label", "") or "—"] += 1

    top_m = ", ".join(f"«{m}»×{n}" for m, n in by_marker.most_common(6)) or "—"
    top_l = ", ".join(f"{l}×{n}" for l, n in by_label.most_common(6)) or "—"
    summary = (
        f"Неявные упоминания Tessbrain/MeetFlow с проблемой: {total} "
        f"(у {affected} пользователей). Частые сигналы: {top_m}. "
        f"Разделы: {top_l}.")

    # Опц. LLM-сводка — ТОЛЬКО по агрегату (не по тексту клиента), крошечная.
    do_llm = use_llm if use_llm is not None else (
        os.getenv("GUIDE_MENTION_LLM", "").strip().lower() in ("1", "on", "true", "yes"))
    if do_llm and total > 0:
        try:
            from backend.core.llm.router import get_llm_router, set_llm_context
            set_llm_context(user_id=None, session_id="mention-scan", agent_mode="guide")
            add = await get_llm_router().generate(
                prompt=(f"Агрегат неявных жалоб на Tessbrain/MeetFlow: сигналы "
                        f"{top_m}; разделы {top_l}. Что это значит для команды "
                        f"разработки и что улучшить? 3-4 строки, приоритет."),
                system_prompt="Ты — продукт-инженер. Кратко, по делу, без воды.",
                temperature=0.3, max_tokens=250) or ""
            if add:
                summary += "\n\nИдеи: " + add
        except Exception as e:
            logger.debug("mention report LLM skipped: %s", e)

    return {"count": total, "users_affected": affected,
            "by_marker": dict(by_marker), "by_label": dict(by_label),
            "summary": summary}


__all__ = ["scan_mentions", "build_mention_report", "mention_scan_enabled"]
