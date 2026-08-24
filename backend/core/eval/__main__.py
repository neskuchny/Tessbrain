# -*- coding: utf-8 -*-
"""CLI eval-гейта (P10): `python -m backend.core.eval`.

Режимы (env):
  EVAL_GOLDEN           путь к golden JSON (default: shipped
                        golden_extraction.json)
  EVAL_MIN_PRECISION    порог precision (default 0.8)
  EVAL_MIN_RECALL       порог recall (default 0.8)
  EVAL_MAX_VIOLATION    макс onto-violation-rate (default 0.05)
  EVAL_LLM_ENABLED      "1" → прогон через РЕАЛЬНЫЙ LLM (гейт перед
                        ONTOLOGY_EXTRACTION_MODE=strict). Иначе —
                        selfcheck-режим (smoke проводки, НЕ оценка
                        качества).

Exit: 0 pass · 1 gate failed · 2 ошибка/инфраструктура.
Никогда не бросает наружу — любая ошибка → exit 2.
"""
from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import sys

logger = logging.getLogger(__name__)


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "on", "yes")


def _thresholds() -> dict:
    def _f(name: str, default: float) -> float:
        try:
            return float(os.getenv(name, "") or default)
        except ValueError:
            return default

    return {
        "min_precision": _f("EVAL_MIN_PRECISION", 0.8),
        "min_recall": _f("EVAL_MIN_RECALL", 0.8),
        "max_violation_rate": _f("EVAL_MAX_VIOLATION", 0.05),
    }


def _selfcheck_extractor(cases):
    """Эхо ожидаемого из golden → проводка harness/runner/exit
    зелёная без ключей. Это НЕ оценка качества модели."""
    by_key = {}
    from .extractors import _input_key

    for c in cases:
        objs = [
            {"label": o.get("label"), "props": dict(o.get("key", {}))}
            for o in c.expected_objects
        ]
        by_key[_input_key(c.input)] = {
            "objects": objs,
            "actions": list(c.expected_actions),
        }
    from .extractors import make_input_extractor

    return make_input_extractor(by_key)


async def _resolve_llm_call():
    """Best-effort: реальный LLM-клиент → async (prompt)->json.
    None если не сконфигурирован."""
    try:
        from backend.core.llm.active_profile import ActiveProfileResolver

        client = await ActiveProfileResolver().get_active_client()
        if client is None:
            return None

        async def _call(prompt: str):
            return await client.generate_json(prompt, temperature=0.0)

        return _call
    except Exception as exc:
        logger.warning("eval.cli: LLM resolve failed: %s", exc)
        return None


def main() -> int:
    try:
        from .harness import load_golden
        from .runner import (
            EXIT_ERROR,
            format_summary,
            run_gate,
            run_llm_gate,
        )

        default_golden = (
            pathlib.Path(__file__).parent / "golden_extraction.json"
        )
        golden_path = os.getenv("EVAL_GOLDEN", str(default_golden))
        try:
            raw = pathlib.Path(golden_path).read_text(encoding="utf-8")
        except Exception as exc:
            print(f"=== Eval gate: ERROR — cannot read golden: {exc} ===")
            return EXIT_ERROR
        cases = load_golden(raw)
        if not cases:
            print("=== Eval gate: ERROR — empty/invalid golden set ===")
            return EXIT_ERROR

        th = _thresholds()

        if _truthy(os.getenv("EVAL_LLM_ENABLED")):
            llm_call = asyncio.run(_resolve_llm_call())
            if llm_call is None:
                print(
                    "=== Eval gate: ERROR — EVAL_LLM_ENABLED set but no "
                    "active LLM profile/key configured ==="
                )
                return EXIT_ERROR
            report, code = asyncio.run(
                run_llm_gate(cases, llm_json_call=llm_call, thresholds=th)
            )
        else:
            print(
                "[selfcheck] EVAL_LLM_ENABLED не задан — smoke проводки, "
                "НЕ оценка качества модели."
            )
            report, code = run_gate(
                cases,
                extractor=_selfcheck_extractor(cases),
                thresholds=th,
                check_ontology=False,
            )

        print(format_summary(report, code))
        return code
    except Exception as exc:
        print(f"=== Eval gate: ERROR — {exc} ===")
        return 2


if __name__ == "__main__":
    sys.exit(main())
