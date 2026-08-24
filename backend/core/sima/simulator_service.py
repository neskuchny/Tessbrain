"""
SIMA Simulator Service — симуляция пользовательских путешествий.
Портировано из sima-app/src/lib/simulator-service.ts
"""
import asyncio
import json
import time

import structlog

from backend.core.llm.router import ModelTier, TaskType, get_llm_router
from backend.core.sima.prompts import (
    BLOCK_SIMULATION_PROMPT,
    JOURNEY_SIMULATION_PROMPT,
    PERSONA_EXTRACTION_PROMPT,
    PERSONA_GENERATION_PROMPT,
)
from backend.db.sima_client import get_sima_db

logger = structlog.get_logger(__name__)


async def _build_simulation_context(project_id: str, user_id: str, layer_filter: str | None = None) -> dict:
    """Собирает контекст для симуляции."""
    db = await get_sima_db()
    project = await db.get_project(project_id, user_id)
    if not project:
        raise ValueError("Project not found")

    blocks = project.get("blocks", [])
    connections = project.get("connections", [])

    if layer_filter and layer_filter != "full":
        layer_order = ["problem", "solution", "mvp", "medium", "ideal"]
        max_idx = layer_order.index(layer_filter) if layer_filter in layer_order else -1
        if max_idx >= 0:
            blocks = [b for b in blocks if layer_order.index(b.get("layer", "mvp")) <= max_idx]
            block_ids = {str(b["id"]) for b in blocks}
            connections = [c for c in connections
                          if str(c.get("fromBlockId") or c.get("from_block_id") or "") in block_ids
                          and str(c.get("toBlockId") or c.get("to_block_id") or "") in block_ids]

    return {"project": project, "blocks": blocks, "connections": connections}


def _g(d: dict, *keys, default=""):
    """Helper: get first non-None value from dict trying multiple keys."""
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return default


def _id_eq(a, b) -> bool:
    """Compare two IDs that may be UUID objects or strings."""
    return str(a) == str(b) if a and b else False


def _serialize_persona(persona: dict) -> str:
    """Сериализует персону в текст."""
    traits = _g(persona, "traits", default=[])
    if isinstance(traits, str):
        traits = json.loads(traits) if traits.startswith("[") else []
    fears = _g(persona, "fears", default=[])
    if isinstance(fears, str):
        fears = json.loads(fears) if fears.startswith("[") else []
    experience = _g(persona, "previousExperience", "previous_experience", default=[])
    if isinstance(experience, str):
        experience = json.loads(experience) if experience.startswith("[") else []
    constraints = _g(persona, "constraints", default=[])
    if isinstance(constraints, str):
        constraints = json.loads(constraints) if constraints.startswith("[") else []

    return f"""ПЕРСОНА: {persona.get('name')}
Роль: {persona.get('role')}
Технический уровень: {_g(persona, 'techLevel', 'tech_level')}
Знание предметной области: {_g(persona, 'domainKnowledge', 'domain_knowledge')}
Цель: {persona.get('goal')}
Бюджет времени: {_g(persona, 'timeBudget', 'time_budget')}
Мотивация: {persona.get('motivation')}
Терпеливость: {_g(persona, 'patienceLevel', 'patience_level')}
Черты: {', '.join(traits) if traits else 'не указаны'}
Страхи: {', '.join(fears) if fears else 'не указаны'}
Опыт: {', '.join(experience) if experience else 'не указан'}
Ограничения: {', '.join(constraints) if constraints else 'нет'}
Архетип: {persona.get('archetype')}"""


def _serialize_sim_context(ctx: dict) -> str:
    """Сериализует контекст симуляции."""
    project = ctx["project"]
    blocks = ctx["blocks"]
    connections = ctx["connections"]

    blocks_text = ""
    for b in blocks:
        blocks_text += f"""
[{b.get('label')}] {b.get('name')} (тип: {b.get('type')}, слой: {b.get('layer')})
  Описание: {b.get('description') or '---'}
  Цель: {b.get('goal') or '---'}
  Польза юзеру: {_g(b, 'userBenefit', 'user_benefit') or '---'}
  Пользовательский сценарий: {_g(b, 'userFlow', 'user_flow') or '---'}
  Входные данные: {_g(b, 'inputData', 'input_data') or '---'}
  Выходные данные: {_g(b, 'outputData', 'output_data') or '---'}
"""

    conn_text = "\n".join([
        f"  {next((b.get('label') for b in blocks if _id_eq(b.get('id'), _g(c, 'fromBlockId', 'from_block_id'))), '?')} -> "
        f"{next((b.get('label') for b in blocks if _id_eq(b.get('id'), _g(c, 'toBlockId', 'to_block_id'))), '?')} ({c.get('type')}): {c.get('label') or '---'}"
        for c in connections
    ])

    return f"""ПРОДУКТ: {project.get('name')}
Описание: {project.get('description') or 'нет'}
Цель: {project.get('goal') or 'нет'}
Аудитория: {_g(project, 'targetAudience', 'target_audience') or 'не указана'}

БЛОКИ ({len(blocks)}):
{blocks_text}

СВЯЗИ ({len(connections)}):
{conn_text}"""


# ═══════════════════════════════════════════
# Персоны
# ═══════════════════════════════════════════

async def create_persona_from_text(project_id: str, free_text: str) -> dict:
    """Создать персону из свободного текста."""
    router = get_llm_router()
    extracted = await router.generate_json(
        prompt=free_text,
        system_prompt=PERSONA_EXTRACTION_PROMPT,
        task_type=TaskType.EXTRACTION,
        model_tier=ModelTier.PREMIUM,
        temperature=0.5,
    )

    if not isinstance(extracted, dict):
        extracted = {}

    db = await get_sima_db()
    return await db.create_persona({
        "projectId": project_id,
        "name": extracted.get("name", "Пользователь"),
        "role": extracted.get("role", "не указана"),
        "techLevel": extracted.get("techLevel", "basic"),
        "domainKnowledge": extracted.get("domainKnowledge", "basic"),
        "goal": extracted.get("goal", "не указана"),
        "timeBudget": extracted.get("timeBudget", "неограничено"),
        "motivation": extracted.get("motivation", "medium"),
        "patienceLevel": extracted.get("patienceLevel", "medium"),
        "traits": json.dumps(extracted.get("traits", []), ensure_ascii=False),
        "fears": json.dumps(extracted.get("fears", []), ensure_ascii=False),
        "previousExperience": json.dumps(extracted.get("previousExperience", []), ensure_ascii=False),
        "constraints": json.dumps(extracted.get("constraints", []), ensure_ascii=False),
        "archetype": extracted.get("archetype", "custom"),
    })


async def generate_personas(project_id: str, user_id: str) -> list[dict]:
    """Автогенерация 3 персон (best/average/worst)."""
    ctx = await _build_simulation_context(project_id, user_id)
    if not ctx["blocks"]:
        raise ValueError("Нет блоков для анализа.")

    router = get_llm_router()
    raw = await router.generate_json(
        prompt=_serialize_sim_context(ctx),
        system_prompt=PERSONA_GENERATION_PROMPT,
        task_type=TaskType.GENERATION,
        model_tier=ModelTier.PREMIUM,
        temperature=0.7,
    )

    personas_data = raw.get("personas", []) if isinstance(raw, dict) else []
    db = await get_sima_db()
    personas = []
    for p in personas_data:
        persona = await db.create_persona({
            "projectId": project_id,
            "name": p.get("name", "Пользователь"),
            "role": p.get("role", "не указана"),
            "techLevel": p.get("techLevel", "basic"),
            "domainKnowledge": p.get("domainKnowledge", "basic"),
            "goal": p.get("goal", "использовать продукт"),
            "timeBudget": p.get("timeBudget", "неограничено"),
            "motivation": p.get("motivation", "medium"),
            "patienceLevel": p.get("patienceLevel", "medium"),
            "traits": json.dumps(p.get("traits", []), ensure_ascii=False),
            "fears": json.dumps(p.get("fears", []), ensure_ascii=False),
            "previousExperience": json.dumps(p.get("previousExperience", []), ensure_ascii=False),
            "constraints": json.dumps(p.get("constraints", []), ensure_ascii=False),
            "archetype": p.get("archetype", "custom"),
        })
        personas.append(persona)

    return personas


async def get_personas(project_id: str) -> list[dict]:
    db = await get_sima_db()
    return await db.get_personas(project_id)


async def delete_persona(persona_id: str) -> bool:
    db = await get_sima_db()
    return await db.delete_persona(persona_id)


# ═══════════════════════════════════════════
# Симуляция
# ═══════════════════════════════════════════

async def simulate(project_id: str, persona_id: str, layer_slug: str, user_id: str) -> dict:
    """Симуляция: одна персона -> один слой."""
    start_time = time.time()
    db = await get_sima_db()
    personas = await db.get_personas(project_id)
    persona = next((p for p in personas if str(p.get("id", "")) == str(persona_id)), None)
    if not persona:
        raise ValueError(f"Persona not found: {persona_id}, available: {[str(p.get('id',''))[:8] for p in personas]}")

    ctx = await _build_simulation_context(project_id, user_id, layer_slug)
    if not ctx["blocks"]:
        raise ValueError("Нет блоков в указанном слое.")

    router = get_llm_router()
    persona_text = _serialize_persona(persona)
    context_text = _serialize_sim_context(ctx)

    raw = await router.generate_json(
        prompt=f"{persona_text}\n\n{context_text}\n\nПроведи {persona.get('name')} через продукт. Ответь JSON.",
        system_prompt=JOURNEY_SIMULATION_PROMPT,
        task_type=TaskType.REASONING,
        model_tier=ModelTier.PREMIUM,
        temperature=0.7,
    )

    analysis_time = round(time.time() - start_time)

    if not isinstance(raw, dict):
        raw = {}

    # Обогащаем шаги: вычисляем cumulativeDropoff, гарантируем числовые типы
    steps = raw.get("steps", [])
    cumulative = 0.0
    for step in steps:
        try:
            sd = float(step.get("stepDropoff", 0) or 0)
        except (ValueError, TypeError):
            sd = 0.05
        step["stepDropoff"] = sd
        cumulative = 1.0 - (1.0 - cumulative) * (1.0 - sd)
        step["cumulativeDropoff"] = round(cumulative, 4)

    # Обогащаем friction points: гарантируем estimatedDropoff
    friction_points = raw.get("topFrictionPoints", [])
    for fp in friction_points:
        if "estimatedDropoff" not in fp:
            fp["estimatedDropoff"] = fp.get("dropoff", fp.get("impact", 0.15))
        try:
            fp["estimatedDropoff"] = float(fp["estimatedDropoff"] or 0)
        except (ValueError, TypeError):
            fp["estimatedDropoff"] = 0.1

    # Гарантируем числовые типы в summary
    summary = raw.get("summary", {})
    if not isinstance(summary, dict):
        summary = {}
    try:
        summary["completionRate"] = float(summary.get("completionRate", 0) or 0)
    except (ValueError, TypeError):
        summary["completionRate"] = 0.5
    summary.setdefault("totalSteps", len(steps))
    summary.setdefault("timeToValue", "---")
    summary.setdefault("overallVerdict", "---")
    summary.setdefault("oneLiner", "")

    report_data = {
        "projectId": project_id,
        "personaId": persona_id,
        "personaName": persona.get("name", "Персона"),
        "scope": layer_slug,
        "summary": summary,
        "steps": steps,
        "frictionPoints": friction_points,
        "recommendations": raw.get("recommendations", []),
        "analysisTime": analysis_time,
    }

    result = await db.create_simulation_report(report_data)
    result["personaName"] = persona.get("name", "Персона")
    return result


async def simulate_all_personas(project_id: str, layer_slug: str, user_id: str) -> list[dict]:
    """Симуляция: все персоны -> один слой (параллельно)."""
    db = await get_sima_db()
    personas = await db.get_personas(project_id)
    if not personas:
        raise ValueError("Нет персон. Сначала создайте персоны.")

    tasks = [simulate(project_id, str(p["id"]), layer_slug, user_id) for p in personas]
    return await asyncio.gather(*tasks)


async def simulate_block(project_id: str, persona_id: str, block_id: str, user_id: str) -> dict:
    """Симуляция: одна персона -> один блок."""
    start_time = time.time()
    db = await get_sima_db()
    personas = await db.get_personas(project_id)
    persona = next((p for p in personas if str(p.get("id", "")) == str(persona_id)), None)
    if not persona:
        raise ValueError(f"Persona not found: {persona_id}, available: {[str(p.get('id',''))[:8] for p in personas]}")

    ctx = await _build_simulation_context(project_id, user_id)
    block = next((b for b in ctx["blocks"] if str(b.get("id", "")) == str(block_id)), None)
    if not block:
        raise ValueError(f"Block not found: {block_id}")

    block_id_str = str(block.get("id", ""))
    related_conns = [c for c in ctx["connections"]
                     if _id_eq(_g(c, 'fromBlockId', 'from_block_id'), block_id_str) or _id_eq(_g(c, 'toBlockId', 'to_block_id'), block_id_str)]

    conn_text = "\n".join([
        f"  {next((b.get('label') for b in ctx['blocks'] if _id_eq(b.get('id'), _g(c, 'fromBlockId', 'from_block_id'))), '?')} -> "
        f"{next((b.get('label') for b in ctx['blocks'] if _id_eq(b.get('id'), _g(c, 'toBlockId', 'to_block_id'))), '?')}: {c.get('label') or '---'}"
        for c in related_conns
    ]) or "  нет"

    persona_text = _serialize_persona(persona)
    prompt = f"""{persona_text}

БЛОК ДЛЯ АНАЛИЗА:
[{block.get('label')}] {block.get('name')} (тип: {block.get('type')})
  Описание: {block.get('description') or '---'}
  Польза юзеру: {_g(block, 'userBenefit', 'user_benefit') or '---'}
  Пользовательский сценарий: {_g(block, 'userFlow', 'user_flow') or '---'}
  Входные данные: {_g(block, 'inputData', 'input_data') or '---'}
  Выходные данные: {_g(block, 'outputData', 'output_data') or '---'}

СВЯЗИ:
{conn_text}

КОНТЕКСТ: {ctx['project'].get('name')} --- {ctx['project'].get('goal') or 'цель не указана'}

Как {persona.get('name')} будет взаимодействовать с этим блоком?"""

    router = get_llm_router()
    raw = await router.generate_json(
        prompt=prompt,
        system_prompt=BLOCK_SIMULATION_PROMPT,
        task_type=TaskType.REASONING,
        model_tier=ModelTier.PREMIUM,
        temperature=0.7,
    )

    analysis_time = round(time.time() - start_time)

    if not isinstance(raw, dict):
        raw = {}

    # Обогащаем данные аналогично simulate()
    steps = raw.get("steps", [])
    cumul = 0.0
    for step in steps:
        try:
            sd = float(step.get("stepDropoff", 0) or 0)
        except (ValueError, TypeError):
            sd = 0.05
        step["stepDropoff"] = sd
        cumul = 1.0 - (1.0 - cumul) * (1.0 - sd)
        step["cumulativeDropoff"] = round(cumul, 4)

    fps = raw.get("topFrictionPoints", [])
    for fp in fps:
        if "estimatedDropoff" not in fp:
            fp["estimatedDropoff"] = fp.get("dropoff", fp.get("impact", 0.15))
        try:
            fp["estimatedDropoff"] = float(fp["estimatedDropoff"] or 0)
        except (ValueError, TypeError):
            fp["estimatedDropoff"] = 0.1

    summary = raw.get("summary", {})
    if not isinstance(summary, dict):
        summary = {}
    try:
        summary["completionRate"] = float(summary.get("completionRate", 0) or 0)
    except (ValueError, TypeError):
        summary["completionRate"] = 0.5
    summary.setdefault("totalSteps", len(steps))
    summary.setdefault("timeToValue", "---")
    summary.setdefault("overallVerdict", "---")
    summary.setdefault("oneLiner", "")

    report_data = {
        "projectId": project_id,
        "personaId": persona_id,
        "personaName": persona.get("name", "Персона"),
        "scope": "block",
        "summary": summary,
        "steps": steps,
        "frictionPoints": fps,
        "recommendations": raw.get("recommendations", []),
        "analysisTime": analysis_time,
    }

    result = await db.create_simulation_report(report_data)
    result["personaName"] = persona.get("name", "Персона")
    return result


async def get_last_simulation_report(project_id: str, persona_id: str | None = None) -> dict | None:
    db = await get_sima_db()
    return await db.get_last_simulation_report(project_id)
