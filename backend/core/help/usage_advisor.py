# -*- coding: utf-8 -*-
"""
Советник по использованию Tessbrain — проактивно подбирает под конкретного
пользователя, чем система полезна ИМЕННО ему и с чего начать.

Опирается на профиль (роль/фокус/проекты) + корпус справки (какие есть
инструменты) + опц. сигналы компании (напр. «много просроченных задач»).
LLM формулирует короткий персональный совет + список рекомендованных
инструментов (slugs — для deep-link). Никогда не raises.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


async def _profile_hint(uid: str) -> str:
    try:
        from backend.memory.user_profiles import get_user_profile_service
        p = await get_user_profile_service().get_profile(uid)
        bits = []
        if getattr(p, "role", None):
            bits.append(f"роль: {p.role}")
        if getattr(p, "current_focus", None):
            bits.append(f"чем занимается: {p.current_focus}")
        if getattr(p, "current_projects", None):
            bits.append("проекты: " + ", ".join(p.current_projects[:5]))
        return "; ".join(bits)
    except Exception as e:
        logger.debug("advisor profile hint failed: %s", e)
        return ""


async def build_advice(uid: Optional[str], *,
                       signals: Optional[dict[str, Any]] = None
                       ) -> dict[str, Any]:
    """Персональный совет по использованию Tessbrain.

    Возвращает {advice, tools:[{slug,title}], personalized:bool}. Если профиля
    нет — общий совет «с чего начать» (personalized=False)."""
    hint = await _profile_hint(uid) if uid else ""
    personalized = bool(hint)

    # Заземление на граф компании: если сигналы не переданы — соберём сами
    # (что собрано со встреч, что не так). Best-effort.
    if signals is None and uid:
        try:
            from backend.core.help.signals_collector import collect_company_signals
            signals = await collect_company_signals(uid)
        except Exception as e:
            logger.debug("advisor signals skipped: %s", e)
            signals = None
    if signals:
        personalized = True    # даже без профиля сигналы делают совет предметным

    # Подбираем релевантные инструменты из корпуса по фокусу пользователя.
    from backend.core.help.help_corpus import get_help_corpus
    corpus = get_help_corpus()
    query = hint or "с чего начать пользоваться Tessbrain, основные возможности"
    retrieved = corpus.retrieve(query, k=5)
    chunks = retrieved.get("chunks", [])
    tools: list[dict[str, str]] = []
    seen = set()
    for c in chunks:
        m = c.get("meta", {})
        slug = m.get("slug")
        if slug and slug not in seen:
            seen.add(slug)
            tools.append({"slug": slug, "title": m.get("title", slug)})

    # Контекст справки для LLM.
    snippets = "\n\n".join(
        f"[{i+1}] {c.get('meta', {}).get('title', '')} "
        f"(вкладка: {c.get('meta', {}).get('slug', '')})\n{c.get('text', '')[:600]}"
        for i, c in enumerate(chunks)) or "(инструменты справки)"

    sig_text = ""
    if signals:
        parts = [f"{k}: {v}" for k, v in signals.items() if v not in (None, "", 0)]
        if parts:
            sig_text = ("\nЧто сейчас в компании (реальные сигналы из памяти/встреч): "
                        + "; ".join(parts))

    who = hint or "пользователь только начал, о себе пока не рассказал"
    system = (
        "Ты — советник по продукту Tessbrain. Смотришь на реальную ситуацию "
        "пользователя (сигналы из его данных) и конкретно советуешь, какой "
        "инструмент Tessbrain это улучшит и КАК им пользоваться. Опирайся только "
        "на инструменты из справки, не выдумывай. По-дружески, на «вы».")
    prompt = (
        f"О пользователе: {who}.{sig_text}\n\n"
        f"Инструменты Tessbrain (из справки):\n{snippets}\n\n"
        f"Дай КОРОТКИЙ предметный совет (3-5 предложений). Если есть сигналы "
        f"(«что не так», просроченные задачи, много встреч и т.п.) — оттолкнись "
        f"от них: «вижу, что у вас X → используйте инструмент Y (вкладка), это "
        f"поможет так-то; сделайте вот что». Если сигналов нет — предложи, с "
        f"каких 2-3 инструментов начать под его роль. Заверши мотивацией.")

    advice = ""
    try:
        from backend.core.llm.router import get_llm_router, set_llm_context
        set_llm_context(user_id=uid, session_id="usage-advisor", agent_mode="guide")
        advice = await get_llm_router().generate(
            prompt=prompt, system_prompt=system, temperature=0.4,
            max_tokens=400) or ""
    except Exception as e:
        logger.debug("advisor LLM failed: %s", e)

    return {"advice": advice.strip(), "tools": tools[:4],
            "personalized": personalized}


__all__ = ["build_advice"]
