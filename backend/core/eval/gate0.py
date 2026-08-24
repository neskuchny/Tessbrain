# -*- coding: utf-8 -*-
"""
Gate-0 харнесс — kill-тест ценности скелета ДО дорогой стройки.

Дисциплина (TESSENT_BRAIN_ARCHITECTURE §8, INVENTORY_HONEST §4): прежде чем
полагаться на скелет (столпы Company Self-Model / hypotheses), ИЗМЕРИТЬ на
5-10 реальных кейсах, даёт ли «скелет + LLM» вывод, которого НЕТ у чистого LLM.
Если не бьёт — скелет не нужен, берём чистый LLM (и экономим главную стройку).

Харнесс:
1. run_skeleton(pillar, payload) — детерминированный вывод скелета столпа.
2. render_report(cases) — side-by-side (скелет vs baseline) для ВЕРДИКТА человека
   (frozen reading: вердикт ставится по выводу, до агрегации).
3. aggregate_gate0(verdicts) — ЧИСТАЯ агрегация вердиктов в PASS/FAIL по
   дисциплине. Это тестируемая суть.

Несущее: PASS только если скелет добавляет ценность БЕЗ регрессий И на
достаточной выборке. Один «skeleton_worse» = скелет может вредить → не полагаться.
Малая выборка (N<min_n) = «статистически пусто» (урок SEC N=5) → не PASS.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# Вердикт человека по кейсу (frozen reading — до агрегации).
VERDICT_SKELETON_WINS = "skeleton_wins"      # скелет дал вывод, которого нет у чистого LLM
VERDICT_TIE = "tie"                          # одинаково
VERDICT_BASELINE_WINS = "baseline_wins"      # чистый LLM не хуже/лучше
VERDICT_SKELETON_WORSE = "skeleton_worse"    # скелет ХУЖЕ чистого LLM (регрессия — фатально)
_ALL_VERDICTS = {
    VERDICT_SKELETON_WINS, VERDICT_TIE, VERDICT_BASELINE_WINS, VERDICT_SKELETON_WORSE,
}


def _verdict_of(item: Any) -> Optional[str]:
    """Нормализовать элемент вердиктов: строка или dict{'verdict': ...}."""
    v = item.get("verdict") if isinstance(item, dict) else item
    return v if v in _ALL_VERDICTS else None


def aggregate_gate0(verdicts: list, min_win_rate: float = 0.4, min_n: int = 5) -> dict:
    """ЧИСТАЯ агрегация вердиктов Gate-0 → решение.

    PASS ⟺ выборка достаточна (n≥min_n) И нет регрессий (skeleton_worse=0) И
    доля побед скелета ≥ min_win_rate. Иначе FAIL с честной причиной.
    """
    valid = [v for v in (_verdict_of(x) for x in (verdicts or [])) if v]
    n = len(valid)
    wins = sum(1 for v in valid if v == VERDICT_SKELETON_WINS)
    worse = sum(1 for v in valid if v == VERDICT_SKELETON_WORSE)
    ties = sum(1 for v in valid if v == VERDICT_TIE)
    baseline = sum(1 for v in valid if v == VERDICT_BASELINE_WINS)
    win_rate = round(wins / n, 3) if n else 0.0
    underpowered = n < min_n

    if n == 0:
        passed, reason = False, "нет вердиктов — нечего измерять"
    elif worse > 0:
        passed, reason = False, (
            f"скелет в {worse} кейс(ах) ХУЖЕ чистого LLM — может вредить, НЕ полагаться"
        )
    elif underpowered:
        passed, reason = False, (
            f"выборка мала (n={n} < {min_n}) — статистически пусто (урок SEC N=5), "
            "не делать вывод"
        )
    elif win_rate < min_win_rate:
        passed, reason = False, (
            f"скелет бьёт чистый LLM лишь в {win_rate:.0%} (<{min_win_rate:.0%}) — "
            "брать чистый LLM, стройку скелета СЭКОНОМИТЬ"
        )
    else:
        passed, reason = True, (
            f"скелет добавляет ценность в {win_rate:.0%} БЕЗ регрессий — полагаться/строить"
        )

    return {
        "passed": passed,
        "reason": reason,
        "n": n,
        "skeleton_wins": wins,
        "ties": ties,
        "baseline_wins": baseline,
        "skeleton_worse": worse,
        "win_rate": win_rate,
        "underpowered": underpowered,
        "recommendation": "BUILD/RELY" if passed else "USE_PLAIN_LLM",
    }


@dataclass
class Gate0Case:
    id: str
    pillar: str                    # table | desync | blind_spots
    payload: dict                  # вход для скелета
    note: str = ""                 # frozen-замечание (что мерим)


def render_report(cases_with_outputs: list) -> str:
    """Markdown side-by-side для ВЕРДИКТА человека. Каждый элемент:
    {id, pillar, input, skeleton_output, baseline_output}. Вердикт ставится
    глазами (frozen reading) и записывается отдельно — потом aggregate_gate0."""
    lines = ["# Gate-0 — вердикт человека (frozen reading)", ""]
    lines.append(f"Вердикты: `{VERDICT_SKELETON_WINS}` | `{VERDICT_TIE}` | "
                 f"`{VERDICT_BASELINE_WINS}` | `{VERDICT_SKELETON_WORSE}`")
    lines.append("")
    for c in (cases_with_outputs or []):
        lines.append(f"## [{c.get('pillar', '?')}] {c.get('id', '?')}")
        lines.append(f"**Вход:** {c.get('input', '')}")
        lines.append(f"**Скелет:** {c.get('skeleton_output', '')}")
        lines.append(f"**Baseline (чистый LLM):** {c.get('baseline_output', '— не запускался')}")
        lines.append("**Вердикт:** ____")
        lines.append("")
    return "\n".join(lines)


def evaluate_control(pillar: str, control: dict, skeleton_out: dict) -> dict:
    """Объективная проверка НЕГАТИВНОГО КОНТРОЛЯ (без человека/LLM): правильно ли
    скелет МОЛЧИТ / честно говорит «не знаю». Это реальный частичный Gate-0
    сигнал — ловит ложные срабатывания и над-claim. Возвращает {ok, expected, got}."""
    check = (control or {}).get("check")
    if check == "is_known":
        got = bool(skeleton_out.get("is_known"))
        exp = bool(control.get("expect"))
        return {"ok": got == exp, "expected": f"is_known={exp}", "got": f"is_known={got}"}
    if check == "no_desync":
        got = len(skeleton_out.get("desyncs", []))
        return {"ok": got == 0, "expected": "0 рассинхронов", "got": f"{got}"}
    if check == "no_metric_desync":
        metric = sum(1 for d in skeleton_out.get("desyncs", []) if d.get("conflict_type") == "metric")
        return {"ok": metric == 0, "expected": "0 metric-конфликтов", "got": f"{metric}"}
    if check == "no_blind_spot":
        got = len(skeleton_out.get("blind_spots", []))
        return {"ok": got == 0, "expected": "0 слепых зон", "got": f"{got}"}
    return {"ok": None, "expected": "?", "got": "неизвестный контроль"}


def check_controls(golden: list, runner=None) -> dict:
    """Прогнать ОБЪЕКТИВНЫЕ негативные контроли gold-набора. runner(pillar,
    payload)→skeleton_out (default run_skeleton). Возвращает агрегат + пер-кейс."""
    runner = runner or run_skeleton
    results = []
    for case in (golden or []):
        ctrl = case.get("control")
        if not ctrl:
            continue
        out = runner(case["pillar"], case.get("payload", {}))
        ev = evaluate_control(case["pillar"], ctrl, out)
        results.append({"id": case.get("id"), "pillar": case.get("pillar"), **ev})
    n = len(results)
    passed = sum(1 for r in results if r["ok"] is True)
    return {
        "controls": n, "passed": passed, "failed": n - passed,
        "all_passed": n > 0 and passed == n,
        "results": results,
    }


def build_worksheet(golden: list, baseline_fn=None, runner=None) -> list:
    """Собрать кейсы для render_report: вход + вывод скелета (+ baseline, если
    baseline_fn задан — иначе слот для ручного/LLM прогона)."""
    runner = runner or run_skeleton
    out = []
    for case in (golden or []):
        skel = runner(case["pillar"], case.get("payload", {}))
        base = ""
        if baseline_fn is not None:
            try:
                base = baseline_fn(case)
            except Exception as e:  # pragma: no cover
                base = f"(baseline error: {e})"
        out.append({
            "id": case.get("id"), "pillar": case.get("pillar"),
            "input": case.get("note", "") + f"  [payload-keys: {list(case.get('payload', {}).keys())}]",
            "skeleton_output": _short(skel),
            "baseline_output": base or "— чистый LLM не запускался (нужен live env)",
        })
    return out


def _short(d: dict, limit: int = 400) -> str:
    import json as _json
    s = _json.dumps(d, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + "…"


def run_skeleton(pillar: str, payload: dict) -> dict:
    """Детерминированный вывод скелета столпа (БЕЗ LLM) для Gate-0. Lazy-импорт
    столпов. Любой сбой → {'error': ...} (не роняет харнесс)."""
    try:
        if pillar == "table":
            from backend.core.think.table_understanding_agent import (
                TableUnderstandingAgent, build_schema_prior, prior_from_graph_nodes,
            )
            agent = TableUnderstandingAgent()
            profiles = agent.profile_columns(payload.get("columns", []), payload.get("rows", []))
            prior = (
                prior_from_graph_nodes(payload["nodes"]) if payload.get("nodes")
                else build_schema_prior(payload.get("snapshot"))
            )
            conf, known = agent.ground(profiles, prior)
            return {"is_known": known, "confidence": conf,
                    "columns": [c.display() for c in profiles]}
        if pillar == "desync":
            from backend.core.think.cross_department_desync import find_desyncs
            return {"desyncs": [d.to_dict() for d in find_desyncs(payload.get("dept_goals", {}))]}
        if pillar == "blind_spots":
            from backend.core.think.blind_spot_scanner import BlindSpotScanner
            spots = BlindSpotScanner(min_recurrence=payload.get("min_recurrence", 3)).scan(
                episodes=payload.get("episodes", []),
                drift=payload.get("drift"),
                contradictions=payload.get("contradictions"),
            )
            return {"blind_spots": [s.to_dict() for s in spots]}
        return {"error": f"unknown pillar {pillar}"}
    except Exception as e:  # pragma: no cover - защита харнесса
        return {"error": str(e)}
