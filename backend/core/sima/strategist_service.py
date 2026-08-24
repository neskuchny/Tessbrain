"""
SIMA Strategist Service — стратегический анализ продукта.
Портировано из sima-app/src/lib/strategist-service.ts
3 линзы (Product, Component, Market) + оркестратор.
"""
import asyncio
import json
import time

import structlog

from backend.core.llm.router import ModelTier, TaskType, get_llm_router
from backend.core.sima.prompts import (
    BLOCK_ANALYSIS_PROMPT,
    COMPONENT_LENS_PROMPT,
    MARKET_LENS_PROMPT,
    ORCHESTRATOR_PROMPT,
    PRODUCT_LENS_PROMPT,
    STRATEGIST_CHAT_PROMPT,
)
from backend.db.sima_client import get_sima_db

logger = structlog.get_logger(__name__)


async def _build_context(project_id: str, user_id: str) -> dict:
    """Собирает контекст проекта для стратега."""
    db = await get_sima_db()
    project = await db.get_project(project_id, user_id)
    if not project:
        raise ValueError("Project not found")
    return project


def _g(d: dict, *keys, default=""):
    """Helper: get first non-None value from dict trying multiple keys (camelCase + snake_case)."""
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return default


def _id_eq(a, b) -> bool:
    """Compare two IDs that may be UUID objects or strings."""
    return str(a) == str(b) if a and b else False


def _serialize_context(project: dict) -> str:
    """Сериализует контекст проекта в текст для промпта."""
    blocks = project.get("blocks", [])
    connections = project.get("connections", [])
    decisions = project.get("decisions", [])

    blocks_text = ""
    for b in blocks:
        blocks_text += f"""
[{b.get('label')}] {b.get('name')} (тип: {b.get('type')}, слой: {b.get('layer')}, сложность: {_g(b, 'estimatedComplexity', 'estimated_complexity', default='medium')})
  Описание: {b.get('description') or '—'}
  Цель: {b.get('goal') or '—'}
  Бизнес-ценность: {_g(b, 'businessValue', 'business_value') or '—'}
  Польза юзеру: {_g(b, 'userBenefit', 'user_benefit') or '—'}
  Решает проблему: {_g(b, 'problemSolved', 'problem_solved') or '—'}
  Если убрать: {_g(b, 'impactIfRemoved', 'impact_if_removed') or '—'}
  Метрики: {b.get('metrics') or '—'}
  Входные данные: {_g(b, 'inputData', 'input_data') or '—'}
  Выходные данные: {_g(b, 'outputData', 'output_data') or '—'}
  Реализация: {b.get('implementation') or '—'}
  Риски: {b.get('risks') or '—'}
  Альтернативы: {b.get('alternatives') or '—'}
  Аналоги: {b.get('analogues') or '—'}
"""

    conn_text = ""
    for c in connections:
        from_id = _g(c, 'fromBlockId', 'from_block_id')
        to_id = _g(c, 'toBlockId', 'to_block_id')
        from_b = next((b for b in blocks if _id_eq(b.get("id"), from_id)), {})
        to_b = next((b for b in blocks if _id_eq(b.get("id"), to_id)), {})
        biz = _g(c, 'businessMeaning', 'business_meaning')
        conn_text += f"  {from_b.get('label', '?')} → {to_b.get('label', '?')} ({c.get('type')}): {c.get('label') or '—'} | Бизнес-смысл: {biz or '—'}\n"

    dec_text = "\n".join([f"  - {d.get('title','')}: {d.get('description','')} ({d.get('reasoning') or 'без обоснования'})" for d in decisions]) if decisions else "  нет"

    return f"""ПРОЕКТ: {project.get('name')}
Описание: {project.get('description') or 'нет'}
Цель: {project.get('goal') or 'нет'}
Аудитория: {_g(project, 'targetAudience', 'target_audience') or 'не указана'}
MVP: {_g(project, 'mvpDescription', 'mvp_description') or 'не описан'}

Нарратив:
- Проблема: {_g(project, 'narrativeProblem', 'narrative_problem') or 'не описана'}
- Решение: {_g(project, 'narrativeSolution', 'narrative_solution') or 'не описано'}
- Как работает: {_g(project, 'narrativeHowItWorks', 'narrative_how_it_works') or 'не описано'}

БЛОКИ ({len(blocks)}):
{blocks_text}

СВЯЗИ ({len(connections)}):
{conn_text}

РЕШЕНИЯ ({len(decisions)}):
{dec_text}"""


def _resolve_block_ids(labels: list | None, blocks: list[dict]) -> list[str]:
    """Резолвим label-ы из ответа LLM в реальные blockId."""
    if not labels:
        return []
    result = []
    for lbl in labels:
        block = next((b for b in blocks if b.get("label") == lbl or b.get("name") == lbl), None)
        if block:
            result.append(str(block["id"]))
    return result


async def _run_lens(prompt: str, context: str, extra_instruction: str = "") -> dict:
    """Запускает одну линзу анализа."""
    router = get_llm_router()
    result = await router.generate_json(
        prompt=context + extra_instruction,
        system_prompt=prompt,
        task_type=TaskType.REASONING,
        model_tier=ModelTier.PREMIUM,
        temperature=0.5,
    )
    return result if isinstance(result, dict) else {}


async def analyze_project(project_id: str, user_id: str) -> dict:
    """Полный анализ проекта — 3 линзы параллельно + оркестрация."""
    start_time = time.time()
    project = await _build_context(project_id, user_id)
    blocks = project.get("blocks", [])

    if not blocks:
        raise ValueError("Нет блоков для анализа. Сначала опишите продукт в чате.")

    context_text = _serialize_context(project)

    # Параллельный запуск 3 линз
    product_task = _run_lens(PRODUCT_LENS_PROMPT, context_text)
    component_task = _run_lens(COMPONENT_LENS_PROMPT, context_text, "\n\nПроанализируй ВСЕ блоки выше.")
    market_task = _run_lens(MARKET_LENS_PROMPT, context_text)

    product, components, market = await asyncio.gather(product_task, component_task, market_task)

    # Оркестрация
    router = get_llm_router()
    orchestrator_input = json.dumps({
        "projectName": project.get("name"),
        "projectGoal": project.get("goal"),
        "productAnalysis": product,
        "componentAnalysis": components,
        "marketAnalysis": market,
    }, ensure_ascii=False)

    raw = await router.generate_json(
        prompt=orchestrator_input,
        system_prompt=ORCHESTRATOR_PROMPT,
        task_type=TaskType.REASONING,
        model_tier=ModelTier.PREMIUM,
        temperature=0.5,
    )

    analysis_time = round(time.time() - start_time)

    # Маппим label-ы в blockId
    if isinstance(raw, dict):
        raw_top_actions = raw.get("topActions", [])
        if not isinstance(raw_top_actions, list):
            raw_top_actions = []
        top_actions = []
        for a in raw_top_actions:
            if not isinstance(a, dict):
                continue
            top_actions.append({
                **a,
                "relatedBlockIds": _resolve_block_ids(a.get("relatedBlockLabels"), blocks),
            })

        raw_sections = raw.get("sections", [])
        if not isinstance(raw_sections, list):
            raw_sections = []
        sections = []
        for s in raw_sections:
            if not isinstance(s, dict):
                continue
            raw_insights = s.get("insights", [])
            if not isinstance(raw_insights, list):
                raw_insights = []
            insights = []
            for ins in raw_insights:
                if not isinstance(ins, dict):
                    continue
                ins.setdefault("relatedBlockIds", [])
                ins.setdefault("actionable", bool(ins.get("action")))
                insights.append({
                    **ins,
                    "relatedBlockIds": _resolve_block_ids(ins.get("relatedBlockLabels"), blocks),
                })
            sections.append({"title": s.get("title", "Без названия"), "insights": insights})

        # Гарантируем verdict формат
        verdict = raw.get("verdict", {})
        if not isinstance(verdict, dict):
            verdict = {"emoji": "---", "oneLiner": str(verdict), "confidence": 50}
        verdict.setdefault("emoji", "---")
        verdict.setdefault("oneLiner", "")
        verdict.setdefault("confidence", 50)

        report_data = {
            "projectId": project_id,
            "scope": "full",
            "verdict": verdict,
            "topActions": top_actions,
            "sections": sections,
            "rawAnalysis": json.dumps({"product": product, "components": components, "market": market}, ensure_ascii=False),
            "analysisTime": analysis_time,
        }

        # Сохраняем
        db = await get_sima_db()
        report = await db.create_strategist_report(report_data)
        return report

    return {"error": "Не удалось выполнить анализ"}


async def analyze_block(project_id: str, block_id: str, user_id: str) -> dict:
    """Анализ одного блока."""
    start_time = time.time()
    project = await _build_context(project_id, user_id)
    blocks = project.get("blocks", [])
    connections = project.get("connections", [])

    block = next((b for b in blocks if _id_eq(b.get("id"), block_id)), None)
    if not block:
        raise ValueError(f"Block not found: {block_id}")

    block_id_str = str(block.get("id", ""))
    related_conns = [c for c in connections
                     if _id_eq(_g(c, 'fromBlockId', 'from_block_id'), block_id_str) or _id_eq(_g(c, 'toBlockId', 'to_block_id'), block_id_str)]

    conn_text = "\n".join([
        f"  {next((b.get('label') for b in blocks if _id_eq(b.get('id'), _g(c, 'fromBlockId', 'from_block_id'))), '?')} -> "
        f"{next((b.get('label') for b in blocks if _id_eq(b.get('id'), _g(c, 'toBlockId', 'to_block_id'))), '?')}: {c.get('label') or '---'}"
        for c in related_conns
    ]) or "  нет связей"

    all_blocks_text = "\n".join([f"  [{b.get('label')}] {b.get('name')} ({b.get('type')})" for b in blocks])

    prompt = f"""ПРОЕКТ: {project.get('name')}
Цель: {project.get('goal') or 'нет'}

АНАЛИЗИРУЕМЫЙ БЛОК:
[{block.get('label')}] {block.get('name')} (тип: {block.get('type')}, слой: {block.get('layer')})
  Описание: {block.get('description') or '---'}
  Цель: {block.get('goal') or '---'}
  Бизнес-ценность: {_g(block, 'businessValue', 'business_value') or '---'}
  Входные данные: {_g(block, 'inputData', 'input_data') or '---'}
  Выходные данные: {_g(block, 'outputData', 'output_data') or '---'}
  Реализация: {block.get('implementation') or '---'}
  Риски: {block.get('risks') or '---'}

СВЯЗИ:
{conn_text}

ВСЕ БЛОКИ:
{all_blocks_text}"""

    router = get_llm_router()
    raw = await router.generate_json(
        prompt=prompt,
        system_prompt=BLOCK_ANALYSIS_PROMPT,
        task_type=TaskType.REASONING,
        model_tier=ModelTier.PREMIUM,
        temperature=0.5,
    )

    analysis_time = round(time.time() - start_time)

    if isinstance(raw, dict):
        report_data = {
            "projectId": project_id,
            "scope": "block",
            "scopeId": block_id,
            "verdict": raw.get("verdict", {}),
            "topActions": raw.get("topActions", []),
            "sections": raw.get("sections", []),
            "analysisTime": analysis_time,
        }
        db = await get_sima_db()
        return await db.create_strategist_report(report_data)

    return {"error": "Не удалось проанализировать блок"}


async def chat_with_strategist(
    project_id: str,
    question: str,
    history: list[dict],
    user_id: str,
) -> str:
    """Диалог со стратегом в контексте проекта."""
    project = await _build_context(project_id, user_id)
    context_text = _serialize_context(project)

    # Получаем последний отчёт
    db = await get_sima_db()
    last_report = await db.get_last_strategist_report(project_id)
    report_context = ""
    if last_report:
        verdict = last_report.get("verdict", {})
        if isinstance(verdict, str):
            verdict = json.loads(verdict)
        top_actions = last_report.get("top_actions", [])
        if isinstance(top_actions, str):
            top_actions = json.loads(top_actions)
        report_context = f"\n\nПОСЛЕДНИЙ АНАЛИЗ:\nВердикт: {verdict.get('emoji', '')} {verdict.get('oneLiner', '')}"
        for a in top_actions[:3]:
            action_text = a.get("action", "") if isinstance(a, dict) else str(a)
            report_context += f"\n  - {action_text}"

    router = get_llm_router()
    result = await router.generate(
        prompt=question,
        system_prompt=STRATEGIST_CHAT_PROMPT + "\n\n" + context_text + report_context,
        task_type=TaskType.REASONING,
        model_tier=ModelTier.PREMIUM,
        temperature=0.7,
    )

    return result or "Не удалось получить ответ от стратега."


async def get_last_report(project_id: str) -> dict | None:
    """Получить последний отчёт стратега."""
    db = await get_sima_db()
    return await db.get_last_strategist_report(project_id)
