# -*- coding: utf-8 -*-
"""
Цикл самоулучшения: дайджест обратной связи для разработчиков.

Периодически (еженедельная задача) агрегирует накопленную обратную связь,
группирует по темам/подсистемам и LLM-ом формулирует ТОП болей + КОНКРЕТНЫЕ
идеи улучшений (что внедрить, куда добавить) — «loop/Hermes-инжиниринг».
Замыкает цикл: пользователи → саппорт → анализ → доработки.
"""
from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Optional

logger = logging.getLogger(__name__)


async def build_digest(*, days: int = 7,
                       requested_by: Optional[str] = None) -> dict[str, Any]:
    """Собрать дайджест обратной связи за N дней.

    Возвращает {period_days, items_count, counts, top_areas, summary}.
    summary — текст для разработчиков (LLM); '' если фидбэка нет/LLM недоступен.
    """
    from backend.core.help.feedback_store import list_feedback
    items = await list_feedback(since_days=days, limit=1000)
    counts = Counter(r.get("kind", "problem") for r in items)
    areas = Counter()
    for r in items:
        for a in (r.get("area") or []):
            areas[a] += 1
    top_areas = [{"area": a, "count": n} for a, n in areas.most_common(8)]

    result: dict[str, Any] = {
        "period_days": days,
        "items_count": len(items),
        "counts": dict(counts),
        "top_areas": top_areas,
        "summary": "",
    }
    if not items:
        result["summary"] = f"За {days} дн. обратной связи нет."
        return result

    # Компактный корпус тикетов для LLM (без раздувания).
    lines = []
    for r in items[-120:]:
        kind = r.get("kind", "problem")
        area = ",".join((r.get("area") or [])[:2])
        lines.append(f"- [{kind}] ({area}) {str(r.get('message', ''))[:280]}")
    corpus = "\n".join(lines)

    system = (
        "Ты — продукт-инженер Tessbrain. По обратной связи пользователей делаешь "
        "дайджест для команды разработки: коротко, приоритизированно, по делу. "
        "Не выдумывай — опирайся на тикеты.")
    prompt = (
        f"Обратная связь пользователей за {days} дн. "
        f"(problem=проблема, wish=пожелание, confusion=непонятно):\n{corpus}\n\n"
        f"Сделай дайджест для разработчиков:\n"
        f"1) ТОП-3–5 болей/тем (что чаще всего мешает), с частотой;\n"
        f"2) какие ПОДСИСТЕМЫ затронуты (по разделам в скобках);\n"
        f"3) КОНКРЕТНЫЕ идеи улучшений: что внедрить, куда добавить, что "
        f"починить — приоритизированно;\n"
        f"4) 1–2 быстрые победы. Кратко, маркерами.")
    try:
        from backend.core.llm.router import get_llm_router, set_llm_context
        set_llm_context(user_id=requested_by, session_id="feedback-digest",
                        agent_mode="guide")
        summary = await get_llm_router().generate(
            prompt=prompt, system_prompt=system, temperature=0.3, max_tokens=1200)
        result["summary"] = summary or ""
    except Exception as e:
        logger.warning("feedback digest LLM failed: %s", e)
        # честный фолбэк — хотя бы структурная сводка
        top = ", ".join(f"{a['area']}×{a['count']}" for a in top_areas[:5]) or "—"
        result["summary"] = (
            f"LLM недоступен. Тикетов: {len(items)} "
            f"({dict(counts)}). Частые разделы: {top}.")
    return result


__all__ = ["build_digest"]
