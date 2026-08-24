# -*- coding: utf-8 -*-
"""ANALYZE фаза — LLM-вывод по dimensions с company/client context (Фаза C).

Для каждого dimension даём LLM:
  - extracted_facts (что нашли в документе)
  - computed_metrics (что посчитали + benchmark_status)
  - rationale (почему параметр важен)
  - company_context (snapshot/rule_book/custom — «с точки зрения компании»)
  - client_context (что обсуждалось с клиентом)

LLM возвращает по каждому dimension: краткий вывод (1-3 предложения) +
severity (ok/info/warning/red_flag). Результат пишется в
computed_metrics[key]["analysis"] и ["severity"] — assemble-фаза их
рендерит. Один LLM-вызов по всем dimensions (эффективно).

Company context собирается из:
  - Company Snapshot (если playbook.company_context.use_snapshot)
  - custom-текста (playbook.company_context.custom)
Best-effort: если snapshot недоступен — продолжаем без него.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from backend.core.analysis.models import AnalysisPlaybook, AnalysisRun

if TYPE_CHECKING:
    from backend.core.analysis.run_engine import RunDeps

logger = logging.getLogger(__name__)


async def _load_company_context(playbook: AnalysisPlaybook) -> str:
    """Собрать company-context преамбулу для analyze-промпта."""
    parts: list[str] = []
    cc = playbook.company_context or {}

    if cc.get("custom"):
        parts.append(f"О нашей компании / методике оценки:\n{cc['custom']}")

    if cc.get("use_snapshot"):
        try:
            from backend.core.sleep.company_snapshot_agent import (
                get_company_snapshot_agent,
            )
            agent = get_company_snapshot_agent()
            snap = None
            # Разные версии API — пробуем безопасно.
            for meth in ("get_latest_snapshot", "get_snapshot", "load_snapshot"):
                fn = getattr(agent, meth, None)
                if fn:
                    snap = await fn() if _is_async(fn) else fn()
                    break
            if snap:
                txt = snap if isinstance(snap, str) else json.dumps(snap, ensure_ascii=False)[:2000]
                parts.append(f"Контекст нашей компании (snapshot):\n{txt}")
        except Exception as e:
            logger.debug("company snapshot unavailable: %s", e)

    return "\n\n".join(parts)


def _is_async(fn) -> bool:
    import inspect
    return inspect.iscoroutinefunction(fn)


async def _load_company_knowledge(run: "AnalysisRun", playbook: AnalysisPlaybook) -> str:
    """Что КОМПАНИЯ уже знает/обсуждала по теме документа (граф/вектора).

    Как в финмодель-ТЗ: до выводов система ищет в собственных данных контекст
    по теме — прошлые решения, мнения, обсуждения — чтобы анализ опирался не
    только на документ, но и на знания компании. Best-effort.
    """
    uid = getattr(run, "user_id", None)
    if not uid:
        return ""
    # запрос из контекста клиента + меток измерений
    terms = []
    if run.client_context:
        terms.append(str(run.client_context))
    for d in (playbook.dimensions or [])[:4]:
        if d.label:
            terms.append(d.label)
    query = " ".join(terms)[:200].strip()
    if len(query) < 3:
        return ""
    try:
        from backend.core.store.graph_view import merged_graph_view_for_user
        gb = await merged_graph_view_for_user(uid, use_networkx=None)
        hits = []
        try:
            hits = await gb.search_nodes(query=query, limit=10, tenant_id=uid)
        except Exception:
            hits = []
        try:
            await gb.close(save=False)
        except Exception:
            pass
        rel = []
        for h in (hits or []):
            lbl = str(h.get("_label") or h.get("label") or "")
            if lbl in ("Decision", "Opinion", "Knowledge", "Meeting", "Project", "Principle"):
                nm = h.get("name") or h.get("summary") or h.get("title")
                if nm:
                    rel.append(f"- [{lbl}] {str(nm)[:140]}")
        if rel:
            return ("Что наша компания уже знает/обсуждала по теме (опирайся, "
                    "различай ТЗ vs мнения):\n" + "\n".join(rel[:8]))
    except Exception as e:
        logger.debug("company knowledge enrich failed: %s", e)
    return ""


async def _web_enrich(run: "AnalysisRun", playbook: AnalysisPlaybook) -> str:
    """Опциональный веб-поиск под тему документа (gate: company_context.web_enrich).
    Tavily (TAVILY_API_KEY) → ddgs-библиотека → пропуск. Best-effort, без жёстких
    зависимостей."""
    cc = playbook.company_context or {}
    if not cc.get("web_enrich"):
        return ""
    import os
    query = (str(run.client_context or "") + " " +
             (playbook.name or ""))[:160].strip()
    if len(query) < 4:
        return ""
    results: list = []
    try:
        tav = os.environ.get("TAVILY_API_KEY")
        if tav:
            import httpx
            async with httpx.AsyncClient(timeout=12.0) as cl:
                r = await cl.post("https://api.tavily.com/search", json={
                    "api_key": tav, "query": query, "max_results": 4,
                    "search_depth": "basic"})
                if r.status_code == 200:
                    for it in (r.json().get("results") or [])[:4]:
                        results.append(f"- {it.get('title','')}: {str(it.get('content',''))[:160]}")
        if not results:
            try:
                from duckduckgo_search import DDGS
                with DDGS() as ddgs:
                    for it in list(ddgs.text(query, max_results=4)):
                        results.append(f"- {it.get('title','')}: {str(it.get('body',''))[:160]}")
            except Exception:
                pass
    except Exception as e:
        logger.debug("web enrich failed: %s", e)
    if results:
        return "Внешний контекст из интернета (для триангуляции):\n" + "\n".join(results[:4])
    return ""


def _build_analyze_prompt(
    playbook: AnalysisPlaybook, run: AnalysisRun, company_ctx: str,
    extra_ctx: str = "", multi_doc: bool = False,
) -> str:
    dim_blocks = []
    for d in playbook.dimensions:
        facts = run.extracted_facts.get(d.key, [])
        metric = run.computed_metrics.get(d.key)
        facts_txt = "; ".join(
            f"{f.get('value')} {f.get('unit') or ''}".strip()
            for f in facts[:8]
        ) or "(не найдено)"
        metric_txt = ""
        if metric and "value" in metric:
            metric_txt = f" | Расчёт: {metric['value']} (статус: {metric.get('benchmark_status', '')})"
        rationale = f" — важно потому что: {d.rationale}" if d.rationale else ""
        dim_blocks.append(
            f'  "{d.key}" ({d.label}){rationale}\n'
            f"    Факты: {facts_txt}{metric_txt}"
        )
    dims = "\n".join(dim_blocks)

    client = f"\nКонтекст сделки/клиента: {run.client_context}\n" if run.client_context else ""
    company = f"\n{company_ctx}\n" if company_ctx else ""
    extra = f"\n{extra_ctx}\n" if extra_ctx else ""
    # Реверсивный разбор: гипотезы, выдвинутые ДО деталей — проверяем их
    # фактами (top-down), а не только собираем выводы снизу вверх.
    hyp_block = ""
    if getattr(run, "hypotheses", None):
        hlines = "\n".join(
            f'  - Гипотеза: {h.get("claim","")} (проверить: {h.get("check","")})'
            for h in run.hypotheses[:6]
        )
        hyp_block = (
            "\nГИПОТЕЗЫ (выдвинуты по скелету документа ДО деталей — "
            "ПОДТВЕРДИ или ОПРОВЕРГНИ каждую фактами; если факт не найден — "
            "так и скажи):\n" + hlines + "\n"
        )
    multi = (
        "\nДОКУМЕНТОВ НЕСКОЛЬКО: по каждому параметру укажи СОВПАДЕНИЯ и "
        "ПРОТИВОРЕЧИЯ между документами; отметь, где данные расходятся и какому "
        "источнику доверять. Ищи скрытые связи между документами.\n"
        if multi_doc else ""
    )
    return (
        "Ты — аналитик-исследователь. Дай вывод по каждому параметру разбора "
        "С ТОЧКИ ЗРЕНИЯ НАШЕЙ КОМПАНИИ (контекст ниже). Будь конкретным, "
        "опирайся на факты и расчёты, различай НАЙДЕНО vs ПРЕДПОЛОЖЕНО "
        "(с указанием на чём основано). Не выдумывай.\n"
        f"{company}{extra}{client}{multi}{hyp_block}\n"
        f"ПАРАМЕТРЫ И ДАННЫЕ:\n{dims}\n\n"
        "Верни СТРОГО JSON:\n"
        '{ "<param_key>": {"verdict": "<1-3 предложения вывода>", '
        '"severity": "ok|info|warning|red_flag"} }\n'
        "severity: red_flag = серьёзный риск/стоп-фактор; warning = требует "
        "внимания; info = нейтральный факт; ok = в норме."
    )


def _coerce_analysis(raw: Any) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    if not isinstance(raw, dict):
        return out
    valid_sev = {"ok", "info", "warning", "red_flag"}
    for key, val in raw.items():
        if isinstance(val, dict):
            verdict = str(val.get("verdict", ""))[:1000]
            sev = str(val.get("severity", "info")).lower()
            if sev not in valid_sev:
                sev = "info"
            out[str(key)] = {"verdict": verdict, "severity": sev}
        elif isinstance(val, str):
            out[str(key)] = {"verdict": val[:1000], "severity": "info"}
    return out


async def run_analyze_phase(
    run: AnalysisRun, playbook: AnalysisPlaybook, deps: "RunDeps",
) -> dict[str, Any]:
    """LLM-вывод по dimensions. Дополняет run.computed_metrics полями
    analysis/severity. never-raises.
    """
    if not playbook.dimensions:
        return run.computed_metrics

    company_ctx = await _load_company_context(playbook)
    # Обогащение как в финмодель-ТЗ: знания компании по теме + (опц.) веб.
    knowledge = await _load_company_knowledge(run, playbook)
    web = await _web_enrich(run, playbook)
    # Арифметический аудит: пересчитанные КОДОМ расчёты документа — судья
    # обязан видеть найденные расхождения (это факты, не мнение LLM).
    audit = ""
    try:
        from backend.core.analysis.numeric_audit import audit_context_block
        audit = audit_context_block(run)
    except Exception:
        logger.debug("audit context skipped", exc_info=True)
    extra_ctx = "\n\n".join(p for p in (knowledge, web, audit) if p)
    multi_doc = len(getattr(run, "input_document_ids", []) or []) > 1
    if knowledge:
        run.add_step("analyzing", "company knowledge enriched", None)
    if web:
        run.add_step("analyzing", "web enriched", None)
    prompt = _build_analyze_prompt(playbook, run, company_ctx,
                                   extra_ctx=extra_ctx, multi_doc=multi_doc)

    try:
        raw = await deps.llm.generate_json(
            prompt,
            system_prompt="Ты аналитик. Возвращай только JSON с verdict+severity.",
        )
    except Exception as e:
        run.add_step("analyzing", "llm failed (skip analysis)", {"error": str(e)[:200]})
        return run.computed_metrics

    analysis = _coerce_analysis(raw)
    metrics = dict(run.computed_metrics)
    severities: list[str] = []
    for d in playbook.dimensions:
        a = analysis.get(d.key)
        if not a:
            continue
        entry = dict(metrics.get(d.key) or {})
        entry["analysis"] = a["verdict"]
        entry["severity"] = a["severity"]
        metrics[d.key] = entry
        severities.append(a["severity"])

    run.computed_metrics = metrics
    # Сводный verdict: худшая severity определяет общий тон.
    overall = "ok"
    for level in ("red_flag", "warning", "info", "ok"):
        if level in severities:
            overall = level
            break
    run.add_step("analyzing", "analysis complete", {
        "dimensions_analyzed": len(analysis), "overall_severity": overall,
    })
    return metrics
