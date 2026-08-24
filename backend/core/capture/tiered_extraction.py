# -*- coding: utf-8 -*-
"""Адаптивный тиринг извлечения: чем крупнее модель — тем меньше запросов.

Логика (docs/MODEL_TIERING_RUNBOOK.md): секции извлечения группируются по
смыслу; сильная модель тянет их в один комбо-запрос, слабая — дробит. Здесь:
  - SECTIONS/GROUPS — смысловые слои;
  - extraction_plan(tier) — на сколько запросов дробить (S0→S4);
  - build_combo_prompt — один промпт на несколько секций;
  - parse_json_robust — устойчивый разбор (защита от ОБРЫВА комбо-JSON);
  - run_tiered — прогон плана: вызовы + слияние + ретрай на битом JSON.

Флаг enable_tiered_extraction (по умолчанию OFF) — интеграция в
CaptureOrchestrator включается осознанно; сам модуль ничего не ломает."""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Секции извлечения (соответствуют агентам оркестратора).
# participants и stakeholders разведены осознанно (правило 1): участники —
# только присутствовавшие, упомянутые третьи лица — в stakeholders.
SECTIONS = [
    "decisions", "tasks", "participants", "stakeholders", "entities", "kpis",
    "opinions", "ideas", "project_statuses", "fact_checks",
]

# Смысловые группы (какие секции про одно)
GROUPS: Dict[str, List[str]] = {
    "facts": ["decisions", "tasks", "participants", "stakeholders", "entities", "kpis"],
    "interpretation": ["opinions", "ideas", "project_statuses"],
    "verification": ["fact_checks"],
}

# Человекочитаемое описание секции — для комбо-промпта
_SECTION_DESC = {
    "decisions": "уже принятые решения",
    "tasks": "задачи (что сделать, кто, срок)",
    "participants": "участники встречи — ТОЛЬКО присутствовавшие (имя, роль)",
    "stakeholders": "упомянутые, но отсутствовавшие лица (имя, связь)",
    "entities": "сущности (проекты, продукты, организации)",
    "kpis": "метрики/KPI (name, value, unit — единица обязательна)",
    "opinions": "мнения/позиции сторон",
    "ideas": "идеи/предложения на будущее (ещё не принятые)",
    "project_statuses": "статусы проектов",
    "fact_checks": "только спорное/неуверенное/противоречивое",
}

# 4 правила схемы (docs/MODEL_TIERING_RUNBOOK). Дают больший прирост качества,
# чем апгрейд модели тиром выше: закрывают ошибки РАЗМЕЩЕНИЯ данных, из-за
# которых даже frontier-модели путали секции. Применяются при strict_schema=True.
_STRICT_RULES = {
    # (1) participants = только присутствующие; третьи лица → stakeholders
    "participants": ("ТОЛЬКО те, кто присутствовал/говорил на встрече. "
                     "Упомянутых третьих лиц (получателей выплат, контакты, "
                     "клиентов) сюда НЕ клади — им место в stakeholders."),
    "stakeholders": ("Лица, УПОМЯНУТЫЕ в разговоре, но НЕ присутствовавшие на "
                     "встрече. Формат {name, relation}. Если таких нет — []."),
    # (2) KPI всегда с единицей измерения
    "kpis": ("Каждый KPI ОБЯЗАН нести единицу измерения. Формат "
             "{name, value, unit}, где unit ∈ {руб., тыс.руб., млн руб., %, "
             "шт., дн., ...}. Если единица неясна из текста — unit: "
             "\"не указано\"; НЕ выдумывай масштаб (тыс./млн)."),
    # (3) fact_checks — только спорное, без надёжных фактов
    "fact_checks": ("ТОЛЬКО спорное: противоречия между спикерами, неуверенные "
                    "или неподтверждённые цифры, расхождения. Надёжные факты "
                    "сюда НЕ включай. Если спорного нет — []."),
    # (4) decisions vs ideas: решено→decision, предложено→idea
    "decisions": ("ТОЛЬКО уже принятые решения (решили/договорились/"
                  "окончательно). Предложение на будущее — это ideas, не сюда."),
    "ideas": ("ТОЛЬКО предложения/гипотезы на будущее, ещё НЕ принятые. "
              "Если по пункту уже есть решение — это decisions, не сюда."),
}


def extraction_plan(tier: str) -> List[List[str]]:
    """Во сколько запросов дробить извлечение (список групп секций на вызов).

    lite   → каждая секция отдельно (S0, ~9 вызовов) — слабая модель;
    mid    → 3 группы (факты · интерпретация · верификация) (S1);
    strong → 2 (факты+интерпретация · верификация) (S2);
    frontier → 1 комбо-вызов на всё (S4)."""
    if tier == "frontier":
        return [list(SECTIONS)]
    if tier == "strong":
        return [GROUPS["facts"] + GROUPS["interpretation"], GROUPS["verification"]]
    if tier == "mid":
        return [GROUPS["facts"], GROUPS["interpretation"], GROUPS["verification"]]
    # lite (и дефолт): дробим по секциям
    return [[s] for s in SECTIONS]


def build_combo_prompt(
    transcript: str, section_keys: List[str], *, strict: bool = True,
) -> str:
    """Один промпт на несколько секций → один структурный JSON.

    Транскрипт кладём ОДИН раз (в этом вся экономия). Просим строго JSON с
    ключами = section_keys, каждый — массив объектов.

    strict=True добавляет блок ПРАВИЛ (4 правила схемы) для тех секций из
    section_keys, для которых они заданы, — это исправляет ошибки размещения
    данных и подтягивает дешёвые mid-модели ближе к frontier."""
    fields = "\n".join(f'  "{k}": [...]   // {_SECTION_DESC.get(k, k)}' for k in section_keys)
    head = (
        "Ты извлекаешь структуру из транскрипта встречи. Верни СТРОГО валидный "
        "JSON без пояснений и без markdown-ограждений, с ключами:\n"
        f"{{\n{fields}\n}}\n"
        "Каждый ключ — массив объектов; если данных нет — пустой массив [].\n"
    )
    rules = ""
    if strict:
        applicable = [(k, _STRICT_RULES[k]) for k in section_keys if k in _STRICT_RULES]
        if applicable:
            lines = "\n".join(f"- {k}: {r}" for k, r in applicable)
            rules = (
                "\nПРАВИЛА (соблюдай строго):\n"
                f"{lines}\n"
                "- Ничего не выдумывай; бери только то, что есть в тексте.\n"
                "- Метки спикеров в транскрипте могут быть неточны — не "
                "приписывай реплику спикеру, если не уверен.\n"
            )
    return head + rules + f"\nТРАНСКРИПТ:\n{transcript}\n"


def parse_json_robust(text: str) -> Dict[str, Any]:
    """Устойчивый разбор JSON (защита от ОБРЫВА вывода крупной модели).

    Снимает ```-ограждения, берёт от первой { до последней }, чинит хвостовые
    запятые, дозакрывает незакрытые }/]. При полном провале — {}."""
    if not text:
        return {}
    s = text.strip()
    # снять ```json ... ```
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s).strip()
    # от первой { до последней }
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j != -1 and j > i:
        s = s[i:j + 1]
    try:
        return json.loads(s)
    except Exception:
        pass
    # чиним хвостовые запятые: ,} ,]
    s2 = re.sub(r",(\s*[}\]])", r"\1", s)
    try:
        return json.loads(s2)
    except Exception:
        pass
    # дозакрываем незакрытые скобки (обрыв)
    open_c = s2.count("{") - s2.count("}")
    open_b = s2.count("[") - s2.count("]")
    s3 = s2 + ("]" * max(0, open_b)) + ("}" * max(0, open_c))
    s3 = re.sub(r",(\s*[}\]])", r"\1", s3)
    try:
        return json.loads(s3)
    except Exception:
        logger.warning("parse_json_robust: не удалось распарсить (%d симв.)", len(text))
        return {}


def merge_extractions(parts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Слить результаты вызовов в один словарь по секциям (массивы конкатенируются)."""
    out: Dict[str, Any] = {k: [] for k in SECTIONS}
    for p in parts:
        if not isinstance(p, dict):
            continue
        for k, v in p.items():
            if k in out and isinstance(v, list):
                out[k].extend(v)
            elif k in out:
                out[k].append(v)
    return out


async def run_tiered(
    transcript: str,
    llm: Any,
    tier: str,
    *,
    generate: Optional[Callable] = None,
    strict_schema: bool = True,
) -> Dict[str, Any]:
    """Прогнать извлечение по плану tier'а. Возвращает {sections, stats}.

    llm — объект с async .generate_json(prompt) или .generate(prompt); можно
    передать свой `generate(prompt)->str`. Ретрай один раз на битом JSON.

    strict_schema=True (дефолт) — включает 4 правила схемы в промптах."""
    plan = extraction_plan(tier)
    parts: List[Dict[str, Any]] = []
    calls = 0

    async def _gen(prompt: str) -> str:
        if generate is not None:
            return await generate(prompt)
        if hasattr(llm, "generate_json"):
            r = await llm.generate_json(prompt)
            return r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
        return await llm.generate(prompt)

    for group in plan:
        prompt = build_combo_prompt(transcript, group, strict=strict_schema)
        calls += 1
        try:
            raw = await _gen(prompt)
        except Exception as e:  # noqa: BLE001
            logger.error("run_tiered: вызов упал (%s): %s", group, e)
            continue
        parsed = parse_json_robust(raw)
        # защита вывода: если ключи не пришли — один ретрай со «строго JSON»
        if not any(parsed.get(k) for k in group):
            calls += 1
            try:
                raw2 = await _gen(prompt + "\nВЕРНИ ТОЛЬКО ВАЛИДНЫЙ JSON.")
                parsed = parse_json_robust(raw2) or parsed
            except Exception:
                pass
        parts.append(parsed)

    return {"sections": merge_extractions(parts),
            "stats": {"tier": tier, "planned_calls": len(plan),
                      "actual_calls": calls, "strict_schema": strict_schema}}
