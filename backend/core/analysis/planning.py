# -*- coding: utf-8 -*-
"""PLAN-фаза свободного разбора — ИИ сам решает, ЧТО читать и ЧТО считать.

Большинство разборов приходит без методики: «вот документ, проанализируй».
Базовые измерения exploratory общие; эта фаза по скелету документа просит
LLM предложить ПОД ЭТОТ ДОКУМЕНТ:
  - дополнительные измерения (что ещё вычитывать: специфичные разделы,
    обязательства, допущения);
  - расчёты-гипотезы: формула + какие числа для неё извлечь
    («payback = investment / annual_profit»).

БЕЗ ХАРДКОДА: формулы придумывает модель под конкретный документ.
Безопасность и честность обеспечивает КОД: каждая формула прогоняется через
safe-calc (AST-песочница) на тестовом namespace — не парсится/чужие имена →
отбрасывается с записью в журнал; входы формул становятся quantitative-
измерениями с extraction_hint, их извлекает обычный EXTRACT, считает
обычный COMPUTE (inputs → агрегат → формула → метрика).

Обогащается КОПИЯ методики (рантайм этого прогона) — сохранённый playbook
не мутируется. Лимиты: ≤6 новых измерений, ≤4 расчёта (защита промптов).
Never-raise: сбой планирования → идём по базовым измерениям.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from backend.core.analysis.models import (
    AnalysisDimension,
    AnalysisPlaybook,
    AnalysisRun,
)
from backend.core.analysis.safe_calc import evaluate_program

if TYPE_CHECKING:
    from backend.core.analysis.run_engine import RunDeps

logger = logging.getLogger(__name__)

MAX_PLAN_DIMENSIONS = 6
MAX_PLAN_COMPUTATIONS = 4
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


def plan_enabled(playbook: AnalysisPlaybook) -> bool:
    cc = playbook.company_context or {}
    return bool(cc.get("auto_plan"))


def _plan_prompt(sample: str, base_dims: str, client_context: str) -> str:
    client = (f"\nЗАПРОС КЛИЕНТА (планируй под него): {client_context}\n"
              if client_context else "")
    return (
        "Ты — старший аналитик. Перед детальным разбором спланируй, ЧТО "
        "именно стоит вычитать и ЧТО посчитать в ЭТОМ документе, чтобы "
        "анализ был качественным.\n"
        f"Базовые параметры уже есть: {base_dims}. Предлагай только то, "
        "чего они не покрывают и что видно по скелету документа.\n"
        f"{client}\n"
        "Верни СТРОГО JSON:\n"
        "{\n"
        '  "dimensions": [ {"key": "<snake_case латиницей>", '
        '"label": "<по-русски>", '
        '"extraction_hint": "<что именно искать в тексте>"} ],\n'
        '  "computations": [ {"key": "<snake_case>", "label": "<что это>", '
        '"formula": "<арифметика над входами, напр. investment / '
        'annual_profit>", "inputs": [ {"key": "<имя переменной из формулы>", '
        '"hint": "<какое число искать в документе>"} ], '
        '"why": "<зачем этот расчёт>"} ]\n'
        "}\n\n"
        "ПРАВИЛА:\n"
        "1. Формулы — ТОЛЬКО арифметика (+ - * / ** и sum/avg/min/max/abs/"
        "round) над именами входов. Никаких выдуманных констант-«норм» внутри "
        "формулы.\n"
        "2. Каждый вход формулы обязан быть реально извлекаемым числом из "
        "ЭТОГО документа (по скелету видно, что оно там есть).\n"
        "3. Расчёт предлагай, только если он даст проверяемый вывод "
        "(окупаемость, доля, сходимость, покрытие). Нечего считать — пустой "
        "массив, это честный ответ.\n"
        "4. Максимум 6 измерений и 4 расчёта — только самое ценное.\n\n"
        f"СКЕЛЕТ ДОКУМЕНТА:\n{sample}"
    )


def _valid_formula(formula: str, input_keys: list[str]) -> bool:
    """Формула парсится safe-calc и считает на заявленных входах."""
    try:
        return evaluate_program(formula, {k: 1.0 for k in input_keys}) is not None
    except Exception:
        return False


async def phase_plan(
    run: AnalysisRun, playbook: AnalysisPlaybook,
    chunks: list[str], deps: "RunDeps",
) -> AnalysisPlaybook:
    """Спланировать разбор и вернуть ОБОГАЩЁННУЮ КОПИЮ методики.

    Невалидные предложения отбрасываются кодом с шагом в журнале.
    Сбой → исходная методика (базовые измерения)."""
    if not chunks:
        return playbook
    n = len(chunks)
    idx = sorted(set([0, 1, n // 2, n - 2, n - 1]))
    sample = "\n---\n".join(chunks[i] for i in idx if 0 <= i < n)[:12000]
    base_dims = ", ".join(d.label for d in playbook.dimensions) or "—"

    try:
        raw = await deps.llm.generate_json(
            _plan_prompt(sample, base_dims, run.client_context or ""),
            system_prompt="Ты планируешь анализ документа. Только JSON.",
            temperature=0.3,
        )
    except Exception as e:
        run.add_step("parsing", "plan pass failed (skip)", {"error": str(e)[:200]})
        return playbook
    if not isinstance(raw, dict):
        run.add_step("parsing", "plan unparsable (skip)", None)
        return playbook

    enriched = AnalysisPlaybook.from_dict(playbook.to_dict())
    enriched.id = playbook.id
    existing = {d.key for d in enriched.dimensions}
    added_dims = added_comps = rejected = 0
    plan_summary: list[str] = []

    # 1) читательские измерения («что ещё вычитать»)
    for it in (raw.get("dimensions") or [])[:MAX_PLAN_DIMENSIONS]:
        if not isinstance(it, dict):
            continue
        key = str(it.get("key") or "").strip().lower()
        if not _KEY_RE.match(key) or key in existing:
            rejected += 1
            continue
        enriched.dimensions.append(AnalysisDimension(
            key=key,
            label=str(it.get("label") or key)[:120],
            rationale="предложено планом разбора под этот документ",
            extraction_hint=str(it.get("extraction_hint") or "")[:300],
            kind="qualitative",
        ))
        existing.add(key)
        added_dims += 1
        plan_summary.append(f"читать: {it.get('label') or key}")

    # 2) расчёты-гипотезы («что посчитать»): формулу валидирует КОД
    for it in (raw.get("computations") or [])[:MAX_PLAN_COMPUTATIONS]:
        if not isinstance(it, dict):
            continue
        key = str(it.get("key") or "").strip().lower()
        formula = str(it.get("formula") or "").strip()
        inputs = [i for i in (it.get("inputs") or []) if isinstance(i, dict)]
        in_keys = [str(i.get("key") or "").strip().lower() for i in inputs]
        in_keys = [k for k in in_keys if _KEY_RE.match(k)]
        if (not _KEY_RE.match(key) or key in existing or not formula
                or not in_keys or not _valid_formula(formula, in_keys)):
            rejected += 1
            run.add_step("parsing", "plan computation rejected", {
                "key": key or "?", "formula": formula[:120]})
            continue
        for i in inputs:
            ik = str(i.get("key") or "").strip().lower()
            if ik in existing or not _KEY_RE.match(ik):
                continue
            enriched.dimensions.append(AnalysisDimension(
                key=ik, label=str(i.get("hint") or ik)[:120],
                rationale=f"вход расчёта «{key}»",
                extraction_hint=str(i.get("hint") or "")[:300],
                kind="quantitative",
            ))
            existing.add(ik)
        enriched.dimensions.append(AnalysisDimension(
            key=key, label=str(it.get("label") or key)[:120],
            rationale=str(it.get("why") or "расчёт по плану разбора")[:300],
            kind="quantitative",
            computation=formula,
            inputs=in_keys,
        ))
        existing.add(key)
        added_comps += 1
        plan_summary.append(
            f"считать: {it.get('label') or key} = {formula}")

    run.add_step("parsing", "plan built", {
        "dimensions_added": added_dims,
        "computations_added": added_comps,
        "rejected": rejected,
        "plan": plan_summary[:10],
    })
    return enriched
