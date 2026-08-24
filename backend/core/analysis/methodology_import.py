# -*- coding: utf-8 -*-
"""Импорт методики разбора из документа.

Сотрудник загружает документ с методологией (как надо анализировать —
на что смотреть, что считать, с чем сравнивать, как оформлять вывод), а
система сама превращает его в AnalysisPlaybook: LLM извлекает параметры
(dimensions), контекст и структуру отчёта, мы санитизируем и валидируем.

Дизайн:
- `_playbook_from_llm_dict` — ЧИСТОЕ построение playbook'а из dict (санитайз
  ключей/типов, дедуп) → юнит-тестируется без LLM.
- `playbook_from_methodology` — обёртка: зовёт LLM (generate_json), при любом
  сбое/мусоре откатывается на минимальный exploratory-playbook, засеянный
  текстом (никогда не падает — сотрудник всё равно получает рабочую методику).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from backend.core.analysis.models import AnalysisDimension, AnalysisPlaybook

logger = logging.getLogger(__name__)

# Методики бывают большими — не жжём токены на весь 100-стр. документ.
_MAX_METHODOLOGY_CHARS = 24_000
_VALID_KINDS = {"quantitative", "qualitative"}

_SYSTEM = (
    "Ты — конструктор методик анализа. На вход даётся документ с методологией "
    "(как компания разбирает присланные материалы). Верни СТРОГО JSON-объект "
    "методики (playbook) без пояснений."
)

_PROMPT_TMPL = """Преобразуй эту методологию в JSON-методику разбора.

Схема JSON:
{{
  "name": "краткое название методики",
  "analysis_type": "due_diligence" | "financial" | "custom",
  "description": "1-2 предложения о чём методика",
  "dimensions": [
    {{
      "key": "snake_case_id",
      "label": "Человеческое название параметра",
      "rationale": "ПОЧЕМУ этот параметр важен",
      "extraction_hint": "что искать в присланном документе",
      "kind": "quantitative" | "qualitative",
      "computation": "формула над inputs (только для quantitative, иначе null)",
      "inputs": ["имена нужных метрик"],
      "benchmark": {{"red_flag": число, "warning": число}},
      "weight": 1.0
    }}
  ],
  "company_context": {{"custom": "с чьей позиции смотрим (если сказано)"}},
  "output_template": {{"blocks": [{{"name":"snake","label":"Заголовок","description":"что тут"}}]}}
}}

Правила:
- Выдели 3-10 параметров (dimensions), покрывающих суть методики.
- kind=quantitative только если реально есть расчёт; иначе qualitative и computation=null.
- Не выдумывай цифры benchmark, если их нет в тексте — оставляй {{}}.
- key — латиница snake_case, уникальные.

МЕТОДОЛОГИЯ:
\"\"\"
{text}
\"\"\""""


def _slug(s: str, fallback: str) -> str:
    s = re.sub(r"[^a-z0-9_]+", "_", str(s or "").strip().lower()).strip("_")
    return s or fallback


def _playbook_from_llm_dict(
    d: dict[str, Any], *, name: Optional[str], created_by: str,
    org_id: Optional[str],
) -> AnalysisPlaybook:
    """Санитизировать LLM-dict → валидный AnalysisPlaybook (чистая функция)."""
    dims_out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, raw in enumerate(d.get("dimensions") or []):
        if not isinstance(raw, dict):
            continue
        label = str(raw.get("label") or raw.get("key") or "").strip()
        if not label:
            continue
        key = _slug(raw.get("key") or label, f"dim_{i}")
        while key in seen:  # дедуп ключей
            key = f"{key}_{i}"
        seen.add(key)
        kind = str(raw.get("kind") or "qualitative").strip().lower()
        if kind not in _VALID_KINDS:
            kind = "qualitative"
        computation = raw.get("computation") if kind == "quantitative" else None
        if isinstance(computation, str) and not computation.strip():
            computation = None
        try:
            weight = float(raw.get("weight", 1.0) or 1.0)
        except (TypeError, ValueError):
            weight = 1.0
        dims_out.append({
            "key": key,
            "label": label,
            "rationale": str(raw.get("rationale") or "").strip(),
            "extraction_hint": str(raw.get("extraction_hint") or "").strip(),
            "kind": kind,
            "computation": computation,
            "inputs": [str(x) for x in (raw.get("inputs") or []) if str(x).strip()],
            "benchmark": dict(raw.get("benchmark") or {}) if isinstance(
                raw.get("benchmark"), dict) else {},
            "weight": weight,
        })

    analysis_type = str(d.get("analysis_type") or "custom").strip().lower()
    if analysis_type not in {"due_diligence", "financial", "custom"}:
        analysis_type = "custom"

    company_context = d.get("company_context")
    if not isinstance(company_context, dict):
        company_context = {}
    output_template = d.get("output_template")
    if not isinstance(output_template, dict):
        output_template = {}

    pb = AnalysisPlaybook.from_dict({
        "name": (name or str(d.get("name") or "").strip() or "Методика из документа"),
        "analysis_type": analysis_type,
        "description": str(d.get("description") or "").strip(),
        "dimensions": dims_out,
        "company_context": company_context,
        "output_template": output_template,
        "org_id": org_id,
        "created_by": created_by,
    })
    pb.id = AnalysisPlaybook.new_id()
    return pb


def _fallback_playbook(
    text: str, *, name: Optional[str], created_by: str, org_id: Optional[str],
) -> AnalysisPlaybook:
    """LLM недоступен/дал мусор → минимальная рабочая методика по тексту."""
    snippet = (text or "").strip()[:600]
    pb = AnalysisPlaybook(
        id=AnalysisPlaybook.new_id(),
        name=(name or "Методика из документа (черновик)"),
        analysis_type="custom",
        description="Автоимпорт методологии не распознал структуру — черновик, отредактируйте параметры.",
        dimensions=[AnalysisDimension(
            key="general_review",
            label="Общий разбор по методологии",
            rationale="Методология загружена как текст, но не разложена на параметры автоматически.",
            extraction_hint=snippet,
            kind="qualitative",
        )],
        company_context={"custom": snippet} if snippet else {},
        created_by=created_by,
        org_id=org_id,
    )
    return pb


async def playbook_from_methodology(
    text: str, *, name: Optional[str] = None, created_by: str = "",
    org_id: Optional[str] = None, user_id: Optional[str] = None,
) -> tuple[AnalysisPlaybook, list[str]]:
    """Документ-методология → (AnalysisPlaybook, warnings). Никогда не raises."""
    text = (text or "").strip()
    if not text:
        return (_fallback_playbook("", name=name, created_by=created_by,
                                   org_id=org_id), ["пустой документ методологии"])

    warnings: list[str] = []
    clipped = text[:_MAX_METHODOLOGY_CHARS]
    if len(text) > _MAX_METHODOLOGY_CHARS:
        warnings.append(f"методология обрезана до {_MAX_METHODOLOGY_CHARS} симв.")

    try:
        from backend.core.llm.router import get_llm_router, set_llm_context
        set_llm_context(user_id=user_id, session_id="methodology-import",
                        agent_mode="analysis")
        result = await get_llm_router().generate_json(
            _PROMPT_TMPL.format(text=clipped),
            system_prompt=_SYSTEM,
            temperature=0.2,
            max_tokens=4096,
        )
    except Exception as e:
        logger.warning("methodology LLM import failed: %s", e)
        pb = _fallback_playbook(text, name=name, created_by=created_by, org_id=org_id)
        return pb, warnings + ["LLM недоступен — создан черновик, отредактируйте методику"]

    if not isinstance(result, dict) or not (result.get("dimensions")):
        pb = _fallback_playbook(text, name=name, created_by=created_by, org_id=org_id)
        return pb, warnings + ["не удалось распознать параметры — создан черновик"]

    pb = _playbook_from_llm_dict(result, name=name, created_by=created_by, org_id=org_id)
    if not pb.dimensions:
        pb = _fallback_playbook(text, name=name, created_by=created_by, org_id=org_id)
        warnings.append("параметры не извлечены — создан черновик")

    # Валидация: битая формула / дубль key → фиксим мягко (в warnings).
    try:
        from backend.core.analysis.validation import validate_playbook
        errors, warns = validate_playbook(pb)
        warnings.extend(warns or [])
        if errors:
            warnings.extend(f"валидация: {e}" for e in errors)
    except Exception:
        pass
    return pb, warnings


__all__ = ["playbook_from_methodology", "_playbook_from_llm_dict", "_fallback_playbook"]
