# -*- coding: utf-8 -*-
"""
Маркетинговые материалы из ТЗ и описаний SIMA-проекта.

Задумка: SIMA не только проектирует продукт и пишет ТЗ — она же объясняет
ПРЕИМУЩЕСТВА и готовит промо-материалы (лендинг, питч, рекламные тексты)
из той же правды: полей блоков и ТЗ. Никакого отдельного «введи описание
продукта ещё раз» — источник один.

Скелет+LLM:
- build_benefits_brief() — ДЕТЕРМИНИРОВАННЫЙ сбор сырья из реальных полей
  (userBenefit/businessValue/problemSolved/metrics/analogues + нарратив +
  выдержка ТЗ). Пустое поле пропускается — не выдумываем.
- generate_marketing() — LLM только ФОРМУЛИРУЕТ материал из брифа
  (дешёвая модель достаточна: факты уже собраны). LLM упал/недоступна →
  честный fallback: структурированный бриф как markdown, llm_used=false.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_TZ_EXCERPT_CHARS = 4000

# Виды материалов и инструкция формулировки для каждого
MATERIAL_KINDS = {
    "benefits": (
        "Объяснение преимуществ продукта. Структура: 1) одна фраза — что "
        "это и для кого; 2) 5-8 преимуществ, каждое = выгода пользователя "
        "(не фича!) + одно предложение почему это правда (из брифа); "
        "3) кому особенно полезно. Без воды и превосходных степеней."),
    "landing": (
        "Текст лендинга. Структура: hero (заголовок ≤8 слов + подзаголовок "
        "+ CTA), боль → решение, 3-4 секции преимуществ с заголовками, "
        "блок «как это работает» (шаги), FAQ (3-5 вопросов), финальный CTA."),
    "pitch": (
        "Питч для инвестора/заказчика на 1 минуту: проблема → решение → "
        "почему сейчас → что уникального → что просим. 150-220 слов."),
    "ads": (
        "Рекламные тексты: 3 варианта короткого объявления (заголовок "
        "≤40 знаков + текст ≤120 знаков) с разными углами (боль / выгода / "
        "социальное доказательство) + 5 вариантов заголовков."),
    "social": (
        "Пост для соцсетей (анонс продукта): живой тон, история боли → "
        "решение, 3 ключевые выгоды, призыв попробовать. 100-180 слов + "
        "5 хэштегов."),
}


def _g(d: dict, *keys: str, default: Any = "") -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def build_benefits_brief(project: dict) -> dict:
    """Детерминированный бриф преимуществ из полей проекта и блоков.

    Только реальные данные: пустые поля пропускаются. Это сырьё и для
    LLM-формулировки, и для честного fallback-вывода."""
    blocks = project.get("blocks") or []

    benefits, values, problems, metrics, analogues = [], [], [], [], []
    for b in blocks:
        name = str(_g(b, "name", default="?"))
        for store, keys in ((benefits, ("userBenefit", "user_benefit")),
                            (values, ("businessValue", "business_value")),
                            (problems, ("problemSolved", "problem_solved")),
                            (metrics, ("metrics",)),
                            (analogues, ("analogues",))):
            v = _g(b, *keys)
            if v:
                store.append({"block": name, "text": str(v)})

    tz = str(_g(project, "generatedTZ", "generated_tz"))
    brief = {
        "product": {
            "name": _g(project, "name", default="Продукт"),
            "description": _g(project, "description"),
            "goal": _g(project, "goal"),
            "audience": _g(project, "targetAudience", "target_audience"),
            "mvp": _g(project, "mvpDescription", "mvp_description"),
            "problem": _g(project, "narrativeProblem", "narrative_problem"),
            "solution": _g(project, "narrativeSolution", "narrative_solution"),
        },
        "user_benefits": benefits,        # выгоды пользователя (по блокам)
        "business_values": values,        # бизнес-ценность
        "problems_solved": problems,      # какие боли закрывает
        "metrics": metrics,               # чем измеряется успех
        "analogues": analogues,           # конкуренты/аналоги (для отстройки)
        "tz_excerpt": tz[:_TZ_EXCERPT_CHARS],
        "blocks_total": len(blocks),
    }
    # чистим пустое в product — честный бриф без заглушек
    brief["product"] = {k: v for k, v in brief["product"].items() if v}
    return brief


def brief_to_md(brief: dict) -> str:
    """Бриф → markdown. Честный fallback без LLM: структурированные факты,
    никакой выдумки."""
    p = brief["product"]
    out = [f"# Преимущества: {p.get('name', 'Продукт')}", ""]
    for label, key in (("Что это", "description"), ("Цель", "goal"),
                       ("Для кого", "audience"), ("Проблема", "problem"),
                       ("Решение", "solution")):
        if p.get(key):
            out.append(f"**{label}:** {p[key]}\n")
    for title, key in (("Выгоды пользователя", "user_benefits"),
                       ("Бизнес-ценность", "business_values"),
                       ("Какие боли закрывает", "problems_solved"),
                       ("Чем измеряем успех", "metrics")):
        items = brief.get(key) or []
        if items:
            out.append(f"## {title}")
            out += [f"- **{i['block']}**: {i['text']}" for i in items]
            out.append("")
    if not any(brief.get(k) for k in ("user_benefits", "business_values",
                                      "problems_solved")):
        out.append("_Поля «польза для пользователя» / «бизнес-ценность» / "
                   "«какую проблему решает» в блоках не заполнены — "
                   "заполните их (или прогоните AI-автозаполнение), и "
                   "материалы станут предметными._")
    return "\n".join(out)


def build_marketing_prompt(brief: dict, kind: str, *,
                           tone: str = "", audience: str = "") -> str:
    """Промпт формулировки материала из брифа (детерминированный)."""
    if kind not in MATERIAL_KINDS:
        raise ValueError(f"kind must be one of {sorted(MATERIAL_KINDS)}")
    import json
    extra = ""
    if audience:
        extra += f"\nЦелевая аудитория этого материала: {audience}."
    if tone:
        extra += f"\nТон: {tone}."
    return (
        "Ты — продуктовый маркетолог. Сформулируй материал СТРОГО из "
        "фактов брифа ниже (бриф собран из спроектированной схемы продукта "
        "и ТЗ). Ничего не выдумывай: нет факта — не пиши. Пиши по-русски."
        f"\n\nЗАДАЧА: {MATERIAL_KINDS[kind]}{extra}\n\n"
        f"БРИФ:\n{json.dumps(brief, ensure_ascii=False, indent=1)}\n\n"
        'Верни JSON: {"title": "заголовок материала", '
        '"markdown": "сам материал в markdown"}. ТОЛЬКО JSON.')


async def generate_marketing(project_id: str, user_id: str, kind: str, *,
                             tone: str = "", audience: str = "") -> dict:
    """Материал вида kind из ТЗ/описаний проекта. LLM формулирует,
    при недоступности — честный бриф-fallback (llm_used=false)."""
    from backend.db.sima_client import get_sima_db
    db = await get_sima_db()
    project = await db.get_project(project_id, user_id)
    if not project:
        raise ValueError("Project not found")

    brief = build_benefits_brief(project)
    prompt = build_marketing_prompt(brief, kind, tone=tone, audience=audience)

    try:
        from backend.core.llm.router import (
            ModelTier,
            TaskType,
            get_llm_router,
        )
        result = await get_llm_router().generate_json(
            prompt=prompt,
            task_type=TaskType.GENERATION,
            model_tier=ModelTier.STANDARD,   # дешёвой модели достаточно
            temperature=0.7,
        )
        if isinstance(result, dict) and result.get("markdown"):
            return {"kind": kind, "title": result.get("title", ""),
                    "markdown": str(result["markdown"]),
                    "llm_used": True, "brief": brief}
    except Exception as e:
        logger.warning(f"marketing LLM failed: {e}")
    return {"kind": kind, "title": f"Бриф (без LLM): "
            f"{brief['product'].get('name', '')}",
            "markdown": brief_to_md(brief),
            "llm_used": False, "brief": brief}


def marketing_kinds() -> list[dict]:
    """Список видов материалов для UI."""
    titles = {"benefits": "Объяснение преимуществ", "landing": "Лендинг",
              "pitch": "Питч (1 минута)", "ads": "Рекламные тексты",
              "social": "Пост для соцсетей"}
    return [{"kind": k, "title": titles[k]} for k in MATERIAL_KINDS]
