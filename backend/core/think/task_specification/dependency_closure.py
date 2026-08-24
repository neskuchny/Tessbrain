# -*- coding: utf-8 -*-
"""Dependency closure — «что ещё надо сделать, чтобы закрыть задачу» (P2).

Третье недостающее звено видения. До P2 `TaskAnalyzerAgent` выдавал
generic-категории зависимостей (technical/business/resource) от LLM, но
НЕ отвечал на конкретный вопрос: «что ещё нужно сделать / что блокирует /
что должно предшествовать», заземлённый на граф знаний.

P2 добавляет замыкание зависимостей:

  task + entities + llm_analysis
        │
        ├─ A. seed из llm_analysis.dependencies (technical/business/resource)
        ├─ B. graph-grounding: поиск блокирующих/предшествующих работ
        │     (storage.search_graph — best-effort, инъектируемо)
        └─ C. синтез упорядоченного actionable-списка + summary
              (LLM если есть, иначе детерминированный merge)
        ▼
  DependencyClosure{blocked_by, must_precede, missing, follow_ups, summary}
        │
        ▼
  .to_extra_context() → markdown-блок в TZRequest (ТЗ узнаёт о предусловиях)

Дизайн:
- чистая функция, ВСЁ инъектируемо (graph_search, llm_generate) →
  юнит-тест без графа/LLM
- никогда не raises (контракт think-агентов — degrade gracefully)
- bounded (max_graph_hits) — не тащим весь граф
- работает в 3 режимах: full (граф+LLM) / graph-only / analysis-only
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# === Структуры ===========================================================

@dataclass
class DependencyClosure:
    """Замыкание зависимостей задачи — что ещё надо сделать, чтобы закрыть."""
    blocked_by: list[str] = field(default_factory=list)      # что блокирует сейчас
    must_precede: list[str] = field(default_factory=list)    # что должно быть сделано до
    missing: list[str] = field(default_factory=list)         # недостающие сущности/ресурсы
    follow_ups: list[str] = field(default_factory=list)      # что доделать после
    summary: str = ""                                        # «чтобы закрыть, нужно: …»
    graph_grounded: bool = False                             # были ли graph-находки
    mode: str = "analysis_only"                              # full|graph_only|analysis_only

    @property
    def is_empty(self) -> bool:
        return not (
            self.blocked_by or self.must_precede
            or self.missing or self.follow_ups
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocked_by": self.blocked_by,
            "must_precede": self.must_precede,
            "missing": self.missing,
            "follow_ups": self.follow_ups,
            "summary": self.summary,
            "graph_grounded": self.graph_grounded,
            "mode": self.mode,
            "is_empty": self.is_empty,
        }

    def to_extra_context(self) -> str:
        """Markdown-блок для TZRequest.extra_context.

        ТЗ-генератор получит явный раздел про предусловия — это и есть
        «опиши что должно быть внутри и почему» из видения.
        """
        if self.is_empty and not self.summary:
            return ""
        lines = ["## Предусловия и зависимости (что ещё нужно сделать)"]
        if self.summary:
            lines.append(self.summary)
        if self.blocked_by:
            lines.append("Блокируется: " + "; ".join(self.blocked_by[:8]))
        if self.must_precede:
            lines.append("Должно быть сделано до: " + "; ".join(self.must_precede[:8]))
        if self.missing:
            lines.append("Не хватает: " + "; ".join(self.missing[:8]))
        if self.follow_ups:
            lines.append("После — доделать: " + "; ".join(self.follow_ups[:8]))
        return "\n".join(lines)


# Инъектируемые зависимости (default = из агента; test = фейки)
GraphSearchFn = Callable[..., Awaitable[list[dict[str, Any]]]]
LLMGenerateFn = Callable[..., Awaitable[dict[str, Any]]]


# === Хелперы =============================================================

def _seed_from_analysis(llm_analysis: dict[str, Any]) -> dict[str, list[str]]:
    """Достать категории зависимостей из результата TaskAnalyzer.

    Контракт analyzer: analysis.dependencies = {technical, business, resource}.
    """
    out = {"missing": [], "must_precede": [], "blocked_by": []}
    if not isinstance(llm_analysis, dict):
        return out
    analysis = llm_analysis.get("analysis", llm_analysis)
    if not isinstance(analysis, dict):
        return out
    deps = analysis.get("dependencies", {})
    if not isinstance(deps, dict):
        return out
    for key in ("technical", "business", "resource"):
        items = deps.get(key)
        if isinstance(items, list):
            out["missing"].extend(str(x).strip() for x in items if str(x).strip())
    # critical_path из recommended_approach → must_precede
    rec = analysis.get("recommended_approach", {})
    if isinstance(rec, dict):
        cp = rec.get("critical_path")
        if isinstance(cp, list):
            out["must_precede"].extend(str(x).strip() for x in cp if str(x).strip())
    return out


def _hit_to_text(hit: Any) -> str:
    """Свести graph-hit (dict разной формы) в строку."""
    if isinstance(hit, str):
        return hit.strip()
    if not isinstance(hit, dict):
        return ""
    for k in ("title", "name", "label", "text", "summary", "content"):
        v = hit.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()[:160]
    return ""


def _dedup(seq: list[str], limit: int = 10) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for s in seq:
        s = s.strip()
        key = s.lower()
        if s and key not in seen:
            seen.add(key)
            out.append(s)
        if len(out) >= limit:
            break
    return out


def _build_summary(closure: DependencyClosure, task_description: str) -> str:
    """Детерминированный summary (когда LLM недоступен)."""
    parts: list[str] = []
    if closure.blocked_by:
        parts.append(f"снять блокеры ({len(closure.blocked_by)})")
    if closure.must_precede:
        parts.append(f"сделать предшествующее ({len(closure.must_precede)})")
    if closure.missing:
        parts.append(f"закрыть нехватку ({len(closure.missing)})")
    if closure.follow_ups:
        parts.append(f"доделать после ({len(closure.follow_ups)})")
    if not parts:
        return "Явных зависимостей не обнаружено — задачу можно брать в работу."
    return "Чтобы закрыть задачу, нужно: " + ", ".join(parts) + "."


_SYNTH_SYSTEM = (
    "Ты — ведущий проектный аналитик. На основе задачи, категорий "
    "зависимостей и находок из графа знаний определи, ЧТО ЕЩЁ НУЖНО "
    "СДЕЛАТЬ, чтобы задачу можно было считать закрытой. "
    "Возвращай ТОЛЬКО валидный JSON без markdown."
)


def _synth_prompt(
    task_description: str,
    seeded: dict[str, list[str]],
    graph_hits: list[str],
) -> str:
    return (
        f"ЗАДАЧА: {task_description[:1500]}\n\n"
        f"КАТЕГОРИИ ЗАВИСИМОСТЕЙ (из анализа):\n"
        f"{seeded}\n\n"
        f"НАХОДКИ ИЗ ГРАФА ЗНАНИЙ (связанные работы/сущности):\n"
        f"{chr(10).join('- ' + h for h in graph_hits[:12]) or 'нет'}\n\n"
        "Верни JSON:\n"
        "{\n"
        '  "blocked_by": ["конкретные задачи/факторы, блокирующие СЕЙЧАС"],\n'
        '  "must_precede": ["что должно быть сделано ДО этой задачи"],\n'
        '  "missing": ["недостающие данные/ресурсы/решения"],\n'
        '  "follow_ups": ["что доделать ПОСЛЕ для полного закрытия"],\n'
        '  "summary": "1-2 предложения: чтобы закрыть задачу, нужно ..."\n'
        "}"
    )


# === Главная функция =====================================================

async def compute_dependency_closure(
    *,
    task_description: str,
    entities: Optional[dict[str, list[str]]] = None,
    llm_analysis: Optional[dict[str, Any]] = None,
    graph_search: Optional[GraphSearchFn] = None,
    llm_generate: Optional[LLMGenerateFn] = None,
    max_graph_hits: int = 8,
) -> DependencyClosure:
    """Посчитать замыкание зависимостей задачи. Никогда не raises.

    Args:
        task_description: текст задачи.
        entities: извлечённые сущности {projects, technologies, people, ...}.
        llm_analysis: результат TaskAnalyzerAgent.analyze() (с .dependencies).
        graph_search: async (query, depth) -> list[dict] (инъекция storage).
        llm_generate: async (prompt, system) -> dict (инъекция LLM).
        max_graph_hits: верхний предел graph-находок.

    Returns:
        DependencyClosure (mode: full|graph_only|analysis_only).
    """
    entities = entities or {}
    llm_analysis = llm_analysis or {}

    # A. Seed из категорий анализа
    seeded = _seed_from_analysis(llm_analysis)

    # B. Graph-grounding (best-effort)
    graph_hits: list[str] = []
    if graph_search is not None:
        try:
            queries = [f"что блокирует: {task_description[:120]}"]
            for proj in (entities.get("projects") or [])[:2]:
                queries.append(f"зависимости проекта {proj}")
            for q in queries:
                hits = await graph_search(q, depth=2)
                if isinstance(hits, list):
                    for h in hits[:max_graph_hits]:
                        t = _hit_to_text(h)
                        if t:
                            graph_hits.append(t)
        except Exception as exc:
            logger.debug("dependency_closure: graph_search failed: %s", exc)
    graph_hits = _dedup(graph_hits, limit=max_graph_hits)

    closure = DependencyClosure(graph_grounded=bool(graph_hits))

    # C. Синтез
    if llm_generate is not None:
        try:
            resp = await llm_generate(
                _synth_prompt(task_description, seeded, graph_hits),
                _SYNTH_SYSTEM,
            )
            if isinstance(resp, dict) and not resp.get("error"):
                closure.blocked_by = _dedup(
                    [str(x) for x in (resp.get("blocked_by") or []) if str(x).strip()]
                )
                closure.must_precede = _dedup(
                    [str(x) for x in (resp.get("must_precede") or []) if str(x).strip()]
                )
                closure.missing = _dedup(
                    [str(x) for x in (resp.get("missing") or []) if str(x).strip()]
                )
                closure.follow_ups = _dedup(
                    [str(x) for x in (resp.get("follow_ups") or []) if str(x).strip()]
                )
                summ = resp.get("summary")
                if isinstance(summ, str) and summ.strip():
                    closure.summary = summ.strip()
                closure.mode = "full" if graph_hits else "llm_only"
        except Exception as exc:
            logger.debug("dependency_closure: llm synth failed: %s", exc)

    # Fallback / дополнение: детерминированный merge если LLM пусто
    if closure.is_empty:
        closure.missing = _dedup(seeded["missing"] + graph_hits)
        closure.must_precede = _dedup(seeded["must_precede"])
        closure.mode = "graph_only" if graph_hits else "analysis_only"

    if not closure.summary:
        closure.summary = _build_summary(closure, task_description)

    return closure


__all__ = ["DependencyClosure", "compute_dependency_closure"]
