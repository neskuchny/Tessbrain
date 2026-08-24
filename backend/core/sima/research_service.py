"""
SIMA Research Service — исследование рынка и конкурентов.
Портировано из sima-app/src/lib/research-service.ts
"""
import time

import structlog

from backend.core.llm.router import ModelTier, TaskType, get_llm_router
from backend.core.sima.prompts import RESEARCH_PROMPT
from backend.db.sima_client import get_sima_db

logger = structlog.get_logger(__name__)


async def conduct_research(project_id: str, user_id: str) -> dict:
    """Проводит полное исследование рынка для продукта."""
    start_time = time.time()

    db = await get_sima_db()
    project = await db.get_project(project_id, user_id)
    if not project:
        raise ValueError("Project not found")

    blocks = project.get("blocks", [])
    blocks_info = "\n".join([
        f'{b.get("label")} "{b.get("name")}" ({b.get("type")}): {b.get("description") or "нет описания"}'
        for b in blocks
    ])

    prompt = f"""Проведи исследование рынка для продукта:

ПРОДУКТ: {project.get('name')}
Описание: {project.get('description') or 'нет'}
Цель: {project.get('goal') or 'нет'}
Аудитория: {project.get('targetAudience') or project.get('target_audience') or 'не указана'}

МОДУЛИ ПРОДУКТА:
{blocks_info}

{RESEARCH_PROMPT}"""

    router = get_llm_router()
    result = await router.generate_json(
        prompt=prompt,
        task_type=TaskType.REASONING,
        model_tier=ModelTier.PREMIUM,
        temperature=0.6,
    )

    analysis_time = round(time.time() - start_time)

    if isinstance(result, dict):
        result["analysisTime"] = analysis_time
        return result

    return {"error": "Не удалось провести исследование", "analysisTime": analysis_time}
