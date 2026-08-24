# -*- coding: utf-8 -*-
"""Eval-гейт раннер (P10).

Оркестрация: golden-набор → extractor → harness → решение
pass/fail с exit-кодом для CI. Это формальный **гейт перед
включением `ONTOLOGY_EXTRACTION_MODE=strict`**: пока качество
extraction и onto-violation-rate не проходят пороги — strict не
включаем.

Чистый, инъектируемый, never-raises. Async-обёртка `run_llm_gate`
прогоняет реальный/фейковый LLM по кейсам и сводит в exit-код.
"""
from __future__ import annotations

import logging
from typing import Any

from .extractors import LLMJsonCall, llm_extract_all, make_input_extractor
from .harness import EvalReport, GoldenCase, run_eval

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_GATE_FAILED = 1
EXIT_ERROR = 2

DEFAULT_THRESHOLDS = {
    "min_precision": 0.8,
    "min_recall": 0.8,
    "max_violation_rate": 0.05,
}


def decide_exit(report: EvalReport, thresholds: dict) -> int:
    """report + пороги → exit-код. Не raises."""
    try:
        ok = report.passed(
            min_precision=thresholds.get("min_precision", 0.8),
            min_recall=thresholds.get("min_recall", 0.8),
            max_violation_rate=thresholds.get("max_violation_rate", 0.05),
        )
        return EXIT_OK if ok else EXIT_GATE_FAILED
    except Exception as exc:
        logger.warning("eval.runner: decide_exit failed: %s", exc)
        return EXIT_ERROR


def run_gate(
    cases: list[GoldenCase],
    *,
    extractor,
    thresholds: dict | None = None,
    check_ontology: bool = True,
) -> tuple[EvalReport, int]:
    """Прогнать harness синхронным extractor'ом → (report, exit_code).

    Никогда не raises — внутренняя ошибка → EXIT_ERROR.
    """
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    try:
        report = run_eval(cases, extractor=extractor,
                          check_ontology=check_ontology)
    except Exception as exc:
        logger.warning("eval.runner: run_eval failed: %s", exc)
        return EvalReport(), EXIT_ERROR
    return report, decide_exit(report, th)


async def run_llm_gate(
    cases: list[GoldenCase],
    *,
    llm_json_call: LLMJsonCall,
    thresholds: dict | None = None,
    check_ontology: bool = True,
) -> tuple[EvalReport, int]:
    """Прогнать кейсы через (реальный/фейковый) LLM и свести в гейт.

    Pre-extract async по кейсам → sync-extractor для harness.
    Никогда не raises.
    """
    try:
        results = await llm_extract_all(cases, llm_json_call)
    except Exception as exc:
        logger.warning("eval.runner: llm_extract_all failed: %s", exc)
        return EvalReport(), EXIT_ERROR
    extractor = make_input_extractor(results)
    return run_gate(
        cases, extractor=extractor,
        thresholds=thresholds, check_ontology=check_ontology,
    )


def format_summary(report: EvalReport, exit_code: int) -> str:
    """Человекочитаемая сводка для CI-лога. Не raises."""
    try:
        d = report.to_dict()
        o, a = d["objects"], d["actions"]
        status = {
            EXIT_OK: "PASS",
            EXIT_GATE_FAILED: "FAIL (gate)",
            EXIT_ERROR: "ERROR",
        }.get(exit_code, "?")
        lines = [
            f"=== Eval gate: {status} (exit={exit_code}) ===",
            f"cases={d['cases']} errored={d['errored_cases']}",
            f"objects  P={o['precision']} R={o['recall']} F1={o['f1']}",
            f"actions  P={a['precision']} R={a['recall']} F1={a['f1']}",
            f"ontology violation_rate={d['violation_rate']}",
        ]
        for c in d["per_case"]:
            mark = "·"
            if c["error"]:
                mark = "ERR"
            lines.append(
                f"  [{mark}] {c['case_id']}: obj{c['object_prf']} "
                f"act{c['action_prf']} viol={c['ontology_violations']}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"=== Eval gate: summary failed: {exc} ==="


__all__ = [
    "EXIT_OK",
    "EXIT_GATE_FAILED",
    "EXIT_ERROR",
    "DEFAULT_THRESHOLDS",
    "decide_exit",
    "run_gate",
    "run_llm_gate",
    "format_summary",
]
