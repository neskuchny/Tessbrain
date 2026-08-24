# -*- coding: utf-8 -*-
"""Детектор возможностей автоматизации (SIMA P2, звено A).

«Tessbrain сам ПОНИМАЕТ, что процесс стоит автоматизировать». По сводке о
компании (снапшот) LLM находит конкретные рутины/процессы-кандидаты и
сохраняет их как структурные предложения (create_proposal → лента
«Предложения», кнопка «Принять» заводит проект в SIMA). Дедуп предложений —
на уровне store (частичный уникальный индекс по title в статусе pending).

Честно: ничего не выдумываем — источник только сводка компании; нет
кандидатов → пустой список. Стоимость под контролем: max_items кап, дешёвый
tier, а ночной прогон — за env-флагом (по умолчанию ВЫКЛ).
"""
from __future__ import annotations

import os

import structlog

from backend.core.automations.proposals import create_proposal
from backend.core.llm.router import ModelTier, TaskType, get_llm_router

logger = structlog.get_logger(__name__)

_SYS_PROMPT = (
    "Ты — операционный аналитик Tessbrain. По сводке о компании находишь "
    "процессы и рутинные задачи, которые стоит автоматизировать, чтобы "
    "улучшить работу (сократить ручной труд, ошибки, задержки). Опираешься "
    "ТОЛЬКО на приведённые факты — ничего не выдумываешь. Если явных "
    "кандидатов на автоматизацию нет — возвращаешь пустой список."
)


def nightly_detection_enabled() -> bool:
    """Ночной авто-детектор включается env-флагом (по умолчанию ВЫКЛ —
    чтобы не тратить токены и не спамить предложениями без явного согласия)."""
    return os.getenv("AUTOMATION_DETECT_NIGHTLY", "").strip().lower() in ("on", "1", "true", "yes")


async def _company_context(user_id: str) -> str:
    """Сводка компании (снапшот → текст) как единственный источник фактов."""
    try:
        from backend.core.sleep.enhanced_snapshot import get_enhanced_snapshot_generator
        from backend.core.store.graph_view import merged_graph_view_for_user

        gb = await merged_graph_view_for_user(user_id, use_networkx=None)
        gen = get_enhanced_snapshot_generator(gb, user_id=user_id)
        gen.user_id = user_id
        snap = await gen.get_company_snapshot()
        await gb.close(save=False)
        return (snap.to_text() or "")[:8000]
    except Exception as e:
        logger.debug("opportunity_detector: company context unavailable", error=str(e))
        return ""


async def detect_automation_opportunities(user_id: str, max_items: int = 3) -> list[dict]:
    """Вернуть до max_items структурных кандидатов на автоматизацию (без записи)."""
    ctx = await _company_context(user_id)
    if not ctx.strip():
        return []
    cap = max(1, min(max_items, 5))
    prompt = (
        f"Сводка о компании:\n\n{ctx}\n\n"
        f"Найди до {cap} КОНКРЕТНЫХ процессов/задач, которые стоит "
        "автоматизировать. Верни строгий JSON вида:\n"
        '{"items": [{"title": "коротко что автоматизируем", '
        '"problem": "почему это стоит автоматизировать", '
        '"proposed_action": "что предлагаем сделать", '
        '"expected_benefit": "ожидаемая польза", '
        '"evidence": "факт/цитата из сводки"}]}\n'
        "Только на основе сводки. Нет явных кандидатов → \"items\": []."
    )
    try:
        router = get_llm_router()
        res = await router.generate_json(
            prompt=prompt,
            system_prompt=_SYS_PROMPT,
            task_type=TaskType.EXTRACTION,
            model_tier=ModelTier.STANDARD,
            temperature=0.4,
        )
    except Exception as e:
        logger.debug("opportunity_detector: LLM failed", error=str(e))
        return []

    raw = res.get("items") if isinstance(res, dict) else (res if isinstance(res, list) else [])
    out: list[dict] = []
    for it in (raw or [])[:cap]:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()
        if not title:
            continue
        out.append({
            "title": title[:200],
            "problem": str(it.get("problem") or "").strip(),
            "proposed_action": str(it.get("proposed_action") or it.get("proposedAction") or "").strip(),
            "expected_benefit": str(it.get("expected_benefit") or it.get("expectedBenefit") or "").strip(),
            "evidence": str(it.get("evidence") or "").strip(),
        })
    return out


async def run_detection(user_id: str, max_items: int = 3, source: str = "auto_detector") -> dict:
    """Найти кандидатов и сохранить как предложения (дедуп в store). Never-raise.

    Returns {"created": [...], "count": N} — created содержит только НОВЫЕ
    (create_proposal вернул объект; дубль pending считаем не-новым).
    """
    try:
        items = await detect_automation_opportunities(user_id, max_items=max_items)
    except Exception as e:
        logger.debug("run_detection: detect failed", error=str(e))
        return {"created": [], "count": 0}

    created: list[dict] = []
    for it in items:
        try:
            p = await create_proposal(
                user_id, it["title"],
                problem=it.get("problem", ""),
                proposed_action=it.get("proposed_action", ""),
                expected_benefit=it.get("expected_benefit", ""),
                evidence=it.get("evidence", ""),
                source=source,
            )
            # Новым считаем только реально вставленное (create_proposal
            # ставит _created=True при INSERT, False — при дубле pending).
            if p and p.get("_created"):
                created.append({k: v for k, v in p.items() if k != "_created"})
        except Exception as e:
            logger.debug("run_detection: create_proposal failed", error=str(e))
    logger.info("automation opportunity detection", user_id=user_id[:8], found=len(items), created=len(created))
    return {"created": created, "count": len(created)}
