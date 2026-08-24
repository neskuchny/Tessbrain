# -*- coding: utf-8 -*-
"""
Composer — раскладывает ПРОИЗВОЛЬНЫЙ запрос на универсальные операции
и исполняет план (docs/CAPABILITY_READINESS.md, мета-слой).

ЗАЧЕМ. Задач будет произвольно много («сделай резюме Пети», «подготовь КП для
X», «оцени сайт от агентства», «подготовь меня к переговорам», …) — это
композиции одних и тех же операций: ДОСЬЕ (№1) → АРТЕФАКТ ПО СХЕМЕ (№4) →
ОЦЕНКА ПО РУБРИКЕ (№5). Композитор делает запрос нового типа задачей
КОНФИГУРАЦИИ, а не нового кода.

РАЗДЕЛЕНИЕ (скелет+LLM):
- ПЛАН — ДАННЫЕ: CompositionPlan можно построить LLM'ом (plan_request) ИЛИ
  передать готовым (caller знает, что хочет). План ВАЛИДИРУЕТСЯ В КОДЕ против
  доступных операций/схем — LLM не может «спланировать» несуществующее.
- ИСПОЛНЕНИЕ — СКЕЛЕТ: execute_plan детерминированно гоняет операции; источники
  досье и LLM инъектируются; покрытие/пробелы данных честно прокидываются.

Интеграция в chat — отдельный шаг за флагом (паттерн answer_mode_router).
Чистый stdlib + инъекции (тестируется с моками).
"""
from __future__ import annotations

import datetime
import json
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from backend.core.artifacts.artifact_engine import (
    BUILTIN_SCHEMAS,
    Artifact,
    ArtifactSchema,
    generate_artifact,
)
from backend.core.artifacts.rubric_engine import (
    Evaluation,
    build_rubric,
    criteria_from_context,
    criteria_from_schema,
    evaluate,
)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class CompositionPlan:
    """План — данные. Инспектируем/правим ДО исполнения."""
    request: str
    entities: Tuple[dict, ...]          # ({"name": str, "type": str}, ...)
    schema_name: str                    # ключ BUILTIN_SCHEMAS или 'custom'
    custom_schema: Optional[dict] = None
    evaluate_result: bool = False       # прогнать ли артефакт через рубрику
    reason: str = ""

    def schema(self) -> ArtifactSchema:
        if self.schema_name == "custom":
            return ArtifactSchema.from_dict(self.custom_schema or {})
        return BUILTIN_SCHEMAS[self.schema_name]


@dataclass(frozen=True)
class CompositionResult:
    plan: CompositionPlan
    artifact: Artifact
    dossier_coverage: dict              # entity → coverage
    evaluation: Optional[Evaluation] = None
    generated_at: str = ""
    plan_source: str = "planner"        # 'caller' | 'memory' | 'memory+planner' | 'planner'
    memory_record_id: Optional[str] = None


class PlanValidationError(ValueError):
    """План ссылается на несуществующее — честная ошибка, не тихий фолбэк."""


def validate_plan(plan: CompositionPlan) -> None:
    """Валидация плана В КОДЕ: схема существует, сущности заданы."""
    if plan.schema_name != "custom" and plan.schema_name not in BUILTIN_SCHEMAS:
        raise PlanValidationError(
            f"неизвестная схема {plan.schema_name!r}; доступны: "
            f"{sorted(BUILTIN_SCHEMAS)} или 'custom'")
    if plan.schema_name == "custom":
        ArtifactSchema.from_dict(plan.custom_schema or {})  # бросит ValueError
    if not plan.entities or not all((e.get("name") or "").strip() for e in plan.entities):
        raise PlanValidationError("план должен называть хотя бы одну сущность с именем")


_PLAN_SYSTEM = (
    "Ты — планировщик задач корпоративного ассистента. По запросу пользователя верни "
    "ТОЛЬКО JSON-объект плана:\n"
    '{"entities": [{"name": str, "type": "person|team|client|project|other"}],\n'
    ' "schema_name": одна из {schemas} ИЛИ "custom",\n'
    ' "custom_schema": null ИЛИ {"name": str, "audience": str, "sections": '
    '[{"key","title","instruction"}]},\n'
    ' "evaluate_result": bool — нужна ли проверка результата по принципам компании,\n'
    ' "reason": str — почему такой план}.\n'
    "Если запрос не похож на встроенные схемы — сконструируй custom_schema под него. "
    "Сущности — ТОЛЬКО реально названные в запросе; не выдумывай."
)


def plan_request(request: str, llm: Callable[[str, str], str]) -> CompositionPlan:
    """Построить план LLM'ом. План валидируется в коде; невалидный → исключение
    (честная ошибка вместо тихого неверного исполнения)."""
    raw = llm(_PLAN_SYSTEM.replace("{schemas}", json.dumps(sorted(BUILTIN_SCHEMAS))),
              f"Запрос: {request}\n\nJSON-план:")
    m = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not m:
        raise PlanValidationError("планировщик не вернул JSON-план")
    try:
        d = json.loads(m.group(0))
    except Exception as e:
        raise PlanValidationError(f"план не парсится: {e}") from e
    plan = CompositionPlan(
        request=request,
        entities=tuple({"name": str(e.get("name", "")).strip(),
                        "type": str(e.get("type", "other"))}
                       for e in (d.get("entities") or [])),
        schema_name=str(d.get("schema_name", "")),
        custom_schema=d.get("custom_schema"),
        evaluate_result=bool(d.get("evaluate_result", False)),
        reason=str(d.get("reason", ""))[:300],
    )
    validate_plan(plan)
    return plan


def execute_plan(
    plan: CompositionPlan,
    *,
    dossier_fn: Callable[[str, str], "object"],   # (name, type) -> EntityDossier
    llm: Callable[[str, str], str],
    company_context: str = "",
) -> CompositionResult:
    """Исполнить план: досье(№1) → артефакт(№4) → [оценка(№5)]. Скелет."""
    validate_plan(plan)
    schema = plan.schema()

    dossiers = {}
    for e in plan.entities:
        dossiers[e["name"]] = dossier_fn(e["name"], e.get("type", "other"))

    artifact = generate_artifact(schema, dossiers, llm,
                                 extra_context=company_context, request=plan.request)

    evaluation = None
    if plan.evaluate_result:
        criteria = criteria_from_schema(schema)
        if company_context:
            criteria = criteria + criteria_from_context(company_context, llm)
        rubric = build_rubric(f"Приёмка: {schema.name}", extra=criteria)
        evaluation = evaluate(artifact.text, rubric, llm, context=company_context)

    return CompositionResult(
        plan=plan, artifact=artifact,
        dossier_coverage={name: d.coverage() for name, d in dossiers.items()},
        evaluation=evaluation, generated_at=_now_iso(),
    )


def _plan_template(plan: CompositionPlan) -> dict:
    """Переиспользуемая часть плана — БЕЗ сущностей (они меняются от задачи
    к задаче; «как делаем» — это схема и режим)."""
    return {"schema_name": plan.schema_name,
            "custom_schema": plan.custom_schema,
            "evaluate_result": plan.evaluate_result}


def compose(request: str, *, dossier_fn, llm, company_context: str = "",
            plan: Optional[CompositionPlan] = None,
            plan_memory=None,
            entities: Optional[List[dict]] = None) -> CompositionResult:
    """Запрос → план → исполнение. Одна точка входа.

    plan_memory (PlanMemory, опц.) — память «как мы делаем такие задачи»:
    - ДО планирования: recall по детерминированной похожести запроса. Если
      способ вспомнен И сущности переданы вызывающим — LLM-планирование не
      нужно вовсе ('memory'). Если сущности не переданы — планировщик зовётся
      ТОЛЬКО за сущностями, схема берётся из памяти ('memory+planner') —
      повторные задачи делаются консистентно, тем же способом.
    - ПОСЛЕ исполнения: remember(шаблон) + исход оценки в stats — память
      учится, какие способы работают.
    """
    plan_source = "caller" if plan is not None else "planner"
    memory_record_id = None
    p = plan

    if p is None and plan_memory is not None:
        recalled = None
        try:
            recalled = plan_memory.recall(request)
        except Exception:
            recalled = None  # память best-effort, не роняет компоновку
        if recalled:
            tpl = recalled["template"]
            memory_record_id = recalled["record_id"]
            if entities:
                p = CompositionPlan(
                    request=request, entities=tuple(entities),
                    schema_name=tpl["schema_name"], custom_schema=tpl.get("custom_schema"),
                    evaluate_result=bool(tpl.get("evaluate_result")),
                    reason=f"из памяти планов ({recalled['record_id']}, "
                           f"sim={recalled['similarity']})")
                plan_source = "memory"
            else:
                planned = plan_request(request, llm)  # только ради сущностей
                p = CompositionPlan(
                    request=request, entities=planned.entities,
                    schema_name=tpl["schema_name"], custom_schema=tpl.get("custom_schema"),
                    evaluate_result=bool(tpl.get("evaluate_result")),
                    reason="схема из памяти планов, сущности от планировщика")
                plan_source = "memory+planner"

    if p is None:
        if entities:
            # сущности известны, способа в памяти нет → планировщик строит схему
            planned = plan_request(request, llm)
            p = CompositionPlan(request=request, entities=tuple(entities),
                                schema_name=planned.schema_name,
                                custom_schema=planned.custom_schema,
                                evaluate_result=planned.evaluate_result,
                                reason=planned.reason)
        else:
            p = plan_request(request, llm)
        plan_source = plan_source if plan is not None else "planner"

    result = execute_plan(p, dossier_fn=dossier_fn, llm=llm,
                          company_context=company_context)

    if plan_memory is not None:
        try:
            r = plan_memory.remember(request, _plan_template(p))
            memory_record_id = memory_record_id or r.get("record_id")
            if result.evaluation is not None and memory_record_id:
                plan_memory.record_outcome(memory_record_id, result.evaluation.decision)
        except Exception:
            pass  # память best-effort

    return CompositionResult(
        plan=result.plan, artifact=result.artifact,
        dossier_coverage=result.dossier_coverage, evaluation=result.evaluation,
        generated_at=result.generated_at, plan_source=plan_source,
        memory_record_id=memory_record_id)
