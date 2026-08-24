# -*- coding: utf-8 -*-
"""ASSEMBLE фаза — финальный отчёт по output_template + reference few-shot (Фаза D).

Собирает итоговый отчёт из extracted_facts + computed_metrics (с analysis/
severity). Два режима:

  LLM-режим (если playbook.output_template ИЛИ reference_report заданы):
    LLM генерирует отчёт по структуре output_template, используя
    reference_report как few-shot пример «как должен выглядеть результат».
    Это закрывает запрос «сотрудник даёт пример итогового отчёта».

  Детерминированный fallback (нет template/reference или LLM-сбой):
    Структурный markdown из dimensions с severity-маркерами + сводка.

Оба режима добавляют severity_summary (red_flags/warnings count) +
overall verdict. never-raises.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from backend.core.analysis.models import AnalysisPlaybook, AnalysisRun

if TYPE_CHECKING:
    from backend.core.analysis.run_engine import RunDeps

logger = logging.getLogger(__name__)

_SEVERITY_ICON = {
    "red_flag": "🔴",
    "warning": "🟡",
    "info": "🔵",
    "ok": "🟢",
}
_SEVERITY_ORDER = ["red_flag", "warning", "info", "ok"]


def _severity_summary(run: AnalysisRun, playbook: AnalysisPlaybook) -> dict[str, Any]:
    counts = {s: 0 for s in _SEVERITY_ORDER}
    for d in playbook.dimensions:
        m = run.computed_metrics.get(d.key) or {}
        sev = m.get("severity")
        if sev in counts:
            counts[sev] += 1
    overall = "ok"
    for level in _SEVERITY_ORDER:
        if counts.get(level):
            overall = level
            break
    return {"counts": counts, "overall": overall}


def _verdict_text(overall: str, counts: dict[str, int]) -> str:
    if overall == "red_flag":
        return f"🔴 ТРЕБУЕТ ВНИМАНИЯ: {counts.get('red_flag', 0)} красных флага(ов)."
    if overall == "warning":
        return f"🟡 С ОГОВОРКАМИ: {counts.get('warning', 0)} предупреждение(й)."
    if overall == "info":
        return "🔵 НЕЙТРАЛЬНО: критичных факторов не выявлено."
    return "🟢 В НОРМЕ: существенных рисков не выявлено."


# === Deterministic fallback ================================================


def _assemble_deterministic(run: AnalysisRun, playbook: AnalysisPlaybook) -> dict[str, Any]:
    summary = _severity_summary(run, playbook)
    verdict = _verdict_text(summary["overall"], summary["counts"])

    lines: list[str] = [
        f"# Аналитический разбор: {playbook.name}",
        "",
        f"**Вердикт:** {verdict}",
        "",
    ]
    if run.client_context:
        lines += [f"**Контекст клиента:** {run.client_context}", ""]
    lines += [f"_Документов: {len(run.input_document_ids)}_", "", "---", ""]

    sections = []
    for d in playbook.dimensions:
        m = run.computed_metrics.get(d.key) or {}
        sev = m.get("severity", "")
        icon = _SEVERITY_ICON.get(sev, "")
        header = f"## {icon} {d.label}".strip()
        lines.append(header)
        if d.rationale:
            lines.append(f"_{d.rationale}_")
            lines.append("")
        if "value" in m:
            status = m.get("benchmark_status", "")
            lines.append(f"- **Показатель:** {m['value']} "
                         f"({status}) — формула `{m.get('formula', '')}`")
        if m.get("analysis"):
            lines.append(f"- **Вывод:** {m['analysis']}")
        facts = run.extracted_facts.get(d.key, [])
        if facts:
            quotes = [f"«{f.get('quote', '')}»" for f in facts[:3] if f.get("quote")]
            if quotes:
                lines.append(f"- **Из документа:** {'; '.join(quotes)}")
        if "value" not in m and not m.get("analysis") and not facts:
            lines.append("- _данные не найдены_")
        lines.append("")
        sections.append({"key": d.key, "label": d.label, "severity": sev})

    return {
        "markdown": "\n".join(lines),
        "sections": sections,
        "verdict": verdict,
        "severity_summary": summary,
        "generated_by": "deterministic",
    }


# === LLM mode ==============================================================


def _build_assemble_prompt(run: AnalysisRun, playbook: AnalysisPlaybook) -> str:
    # Данные по dimensions.
    blocks = []
    for d in playbook.dimensions:
        m = run.computed_metrics.get(d.key) or {}
        facts = run.extracted_facts.get(d.key, [])
        facts_txt = "; ".join(
            f"{f.get('value')} {f.get('unit') or ''}".strip() for f in facts[:6]
        ) or "(нет)"
        metric_txt = ""
        if "value" in m:
            metric_txt = f"; расчёт={m['value']} ({m.get('benchmark_status', '')})"
        analysis_txt = f"; вывод={m['analysis']}" if m.get("analysis") else ""
        blocks.append(f'- {d.label} [{d.key}]: факты={facts_txt}{metric_txt}{analysis_txt}')
    data = "\n".join(blocks)

    # Структура отчёта.
    template = playbook.output_template or {}
    tmpl_block = ""
    if template.get("blocks"):
        tb = "\n".join(
            f'  - {b.get("label", b.get("name", ""))}: {b.get("description", "")}'
            for b in template["blocks"]
        )
        tmpl_block = f"\nСТРУКТУРА ОТЧЁТА (используй эти разделы):\n{tb}\n"

    # Few-shot пример.
    ref_block = ""
    if playbook.reference_report:
        ref_block = (
            "\nПРИМЕР ЖЕЛАЕМОГО ФОРМАТА ОТЧЁТА (ориентируйся на стиль/структуру, "
            f"но используй НАШИ данные):\n```\n{playbook.reference_report[:3000]}\n```\n"
        )

    client = f"\nКонтекст сделки: {run.client_context}\n" if run.client_context else ""

    return (
        f"Ты — аналитик. Составь ИТОГОВЫЙ ОТЧЁТ по разбору «{playbook.name}» "
        "на основе собранных данных. Отчёт — в Markdown, структурный, с "
        "выводами и вердиктом. Опирайся ТОЛЬКО на данные ниже, не выдумывай.\n"
        f"{client}{tmpl_block}{ref_block}\n"
        f"СОБРАННЫЕ ДАННЫЕ:\n{data}\n\n"
        "Верни СТРОГО JSON: {\"markdown\": \"<полный отчёт в markdown>\", "
        "\"verdict\": \"<краткий итоговый вердикт>\"}"
    )


async def assemble_report(
    run: AnalysisRun, playbook: AnalysisPlaybook, deps: "RunDeps",
) -> dict[str, Any]:
    """Собрать финальный отчёт. LLM-режим если есть template/reference,
    иначе детерминированный. never-raises → fallback на детерминированный.
    """
    summary = _severity_summary(run, playbook)
    use_llm = bool(playbook.output_template or playbook.reference_report)

    if not use_llm:
        return _assemble_deterministic(run, playbook)

    prompt = _build_assemble_prompt(run, playbook)
    try:
        raw = await deps.llm.generate_json(
            prompt,
            system_prompt="Ты составляешь аналитический отчёт. Только JSON с markdown+verdict.",
        )
    except Exception as e:
        run.add_step("assembling", "llm failed → deterministic", {"error": str(e)[:200]})
        return _assemble_deterministic(run, playbook)

    if not isinstance(raw, dict) or not raw.get("markdown"):
        run.add_step("assembling", "llm bad output → deterministic", None)
        return _assemble_deterministic(run, playbook)

    verdict = str(raw.get("verdict") or _verdict_text(summary["overall"], summary["counts"]))
    sections = [{"key": d.key, "label": d.label,
                 "severity": (run.computed_metrics.get(d.key) or {}).get("severity", "")}
                for d in playbook.dimensions]
    return {
        "markdown": str(raw["markdown"]),
        "sections": sections,
        "verdict": verdict,
        "severity_summary": summary,
        "generated_by": "llm",
    }
