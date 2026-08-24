# -*- coding: utf-8 -*-
"""Арифметический аудит документа — сходятся ли ЕГО СОБСТВЕННЫЕ расчёты.

Ключевой сценарий: партнёр принёс документацию, где «статьи: 40 + 35 + 23,
итого 120». LLM такие ошибки замечает нестабильно; здесь разделение труда:

  LLM  — только НАХОДИТ заявленные вычисления (слагаемые/множители,
         заявленный итог, дословная цитата);
  КОД  — пересчитывает и сравнивает с заявленным (допуск 1% относительный
         на округления).

Расхождение = red_flag с цитатой и дельтой. Ничего не найдено — честный
пустой результат («заявленных вычислений в документе нет»), не подгонка.

Чанки для поиска отбирает код по плотности чисел (top-N) — LLM-вызовов
максимум ~3 независимо от размера документа. Результат пишется в
run.computed_metrics["__numeric_audit__"] и уходит в analyze/отчёт.
Never-raise.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from backend.core.analysis.models import AnalysisRun

if TYPE_CHECKING:
    from backend.core.analysis.run_engine import RunDeps

logger = logging.getLogger(__name__)

TOP_NUMERIC_CHUNKS = 9      # сколько самых «числовых» чанков смотрим
AUDIT_BATCH_CHARS = 11000   # размер батча одного LLM-вызова
REL_TOLERANCE = 0.01        # 1% на округления

_NUM_TOKEN_RE = re.compile(r"\d[\d\s.,]*")

_OPS = {
    "sum": lambda xs: sum(xs),
    "mul": lambda xs: _prod(xs),
    "sub": lambda xs: xs[0] - sum(xs[1:]) if xs else None,
    "div": lambda xs: (xs[0] / xs[1]) if len(xs) == 2 and xs[1] else None,
    "pct_of": lambda xs: (xs[0] * xs[1] / 100.0) if len(xs) == 2 else None,
}


def _prod(xs: list[float]) -> float:
    out = 1.0
    for x in xs:
        out *= x
    return out


def numeric_density(text: str) -> int:
    """Сколько чисел в тексте — метрика «числовитости» чанка."""
    return len(_NUM_TOKEN_RE.findall(text or ""))


def select_numeric_chunks(chunks: list[str],
                          top_n: int = TOP_NUMERIC_CHUNKS) -> list[str]:
    """Top-N чанков по плотности чисел (порядок документа сохраняется)."""
    scored = [(numeric_density(c), i) for i, c in enumerate(chunks)]
    top = sorted(scored, reverse=True)[:top_n]
    keep = sorted(i for score, i in top if score >= 3)
    return [chunks[i] for i in keep]


def _audit_prompt(batch: str) -> str:
    return (
        "Найди во фрагменте документа ЗАЯВЛЕННЫЕ ВЫЧИСЛЕНИЯ — места, где "
        "автор приводит слагаемые/множители И заявляет итог: суммы статей, "
        "«итого/всего», произведения (цена × количество), разности, доли и "
        "проценты от базы.\n\n"
        "Верни СТРОГО JSON-массив (пустой, если заявленных вычислений нет — "
        "это нормальный ответ):\n"
        '[{"parts": [<числа-операнды в одинаковых единицах>], '
        '"op": "sum|mul|sub|div|pct_of", '
        '"claimed": <заявленный автором итог>, '
        '"what": "<что это, 3-6 слов>", '
        '"quote": "<дословная строка документа с этим вычислением>"}]\n\n'
        "ПРАВИЛА:\n"
        "1. НИЧЕГО не считай сам — только выпиши операнды и заявленный итог "
        "как они написаны. Считать буду я.\n"
        "2. Приводи операнды к одной единице (если «2 млн + 500 тыс», пиши "
        "[2000000, 500000]); итог — в той же единице.\n"
        "3. Бери только случаи, где И операнды, И итог явно написаны в "
        "тексте. Не восстанавливай недостающее.\n"
        "4. pct_of: parts=[база, процент] для «X% от Y».\n\n"
        f"ФРАГМЕНТ:\n{batch}"
    )


def _recompute(claim: dict[str, Any]) -> dict[str, Any] | None:
    """Пересчитать одно заявленное вычисление. None = не проверяемо."""
    try:
        parts = [float(p) for p in (claim.get("parts") or [])]
        claimed = float(claim.get("claimed"))
    except (TypeError, ValueError):
        return None
    op = str(claim.get("op") or "sum").lower()
    fn = _OPS.get(op)
    if not fn or len(parts) < (1 if op == "sum" else 2):
        return None
    try:
        expected = fn(parts)
    except Exception:
        return None
    if expected is None:
        return None
    denom = max(abs(expected), abs(claimed), 1e-9)
    rel_delta = abs(expected - claimed) / denom
    return {
        "what": str(claim.get("what") or "")[:120],
        "op": op,
        "parts": parts[:12],
        "claimed": claimed,
        "expected": round(expected, 4),
        "rel_delta": round(rel_delta, 4),
        "ok": rel_delta <= REL_TOLERANCE,
        "quote": str(claim.get("quote") or "")[:300],
    }


def _batches(chunks: list[str]) -> list[str]:
    out: list[str] = []
    cur = ""
    for c in chunks:
        if cur and len(cur) + len(c) > AUDIT_BATCH_CHARS:
            out.append(cur)
            cur = c
        else:
            cur = f"{cur}\n---\n{c}" if cur else c
    if cur:
        out.append(cur)
    return out


async def audit_chunks(chunks: list[str], generate_json, *,
                       on_error=None) -> dict[str, Any]:
    """Ядро аудита, пригодное для любого текста (не только AnalysisRun).

    generate_json(prompt, system_prompt=..., temperature=...) — любой awaitable,
    возвращающий разобранный JSON (LLMRouter.generate_json подходит как есть).
    Возврат — та же структура, что кладётся в run.computed_metrics."""
    numeric = select_numeric_chunks(chunks)
    if not numeric:
        return {"checked": 0, "mismatches": [], "severity": "info",
                "note": "числовых фрагментов нет"}

    checks: list[dict[str, Any]] = []
    for idx, batch in enumerate(_batches(numeric)[:3]):
        try:
            raw = await generate_json(
                _audit_prompt(batch),
                system_prompt=("Ты выписываешь заявленные вычисления из "
                               "текста. Только JSON-массив."),
                temperature=0.0,
            )
        except Exception as e:
            if on_error:
                on_error(idx, e)
            else:
                logger.warning("numeric audit batch %s failed: %s", idx, e)
            continue
        items = raw if isinstance(raw, list) else (
            raw.get("claims") if isinstance(raw, dict) else None)
        for it in (items or []):
            if not isinstance(it, dict):
                continue
            res = _recompute(it)
            if res:
                checks.append(res)

    # дедуп по цитате
    seen: set = set()
    unique = []
    for c in checks:
        k = c["quote"].lower()[:80]
        if k in seen:
            continue
        seen.add(k)
        unique.append(c)

    mismatches = [c for c in unique if not c["ok"]]
    return {
        "checked": len(unique),
        "mismatches": mismatches[:15],
        "severity": "red_flag" if mismatches else ("ok" if unique else "info"),
        "note": ("расчёты документа проверены пересчётом"
                 if unique else "заявленных вычислений в документе не найдено"),
    }


async def audit_text(text: str, generate_json) -> dict[str, Any]:
    """Аудит сплошного текста (отчёт, документ): режем на абзацы-чанки сами."""
    paras = [p for p in (text or "").split("\n\n") if p.strip()]
    return await audit_chunks(paras, generate_json)


async def phase_numeric_audit(
    run: AnalysisRun, chunks: list[str], deps: "RunDeps",
) -> None:
    """Прогнать аудит и записать результат в run.computed_metrics."""
    if not select_numeric_chunks(chunks):
        run.add_step("computing", "numeric audit: числовых фрагментов нет", None)
        return

    def _err(idx, e):
        run.add_step("computing", "numeric audit batch failed (skip)", {
            "batch": idx, "error": str(e)[:160]})

    result = await audit_chunks(chunks, deps.llm.generate_json, on_error=_err)
    run.computed_metrics["__numeric_audit__"] = result
    run.add_step("computing", "numeric audit complete", {
        "checked": result["checked"], "mismatches": len(result["mismatches"]),
    })


def audit_context_block(run: AnalysisRun) -> str:
    """Блок для analyze-промпта/отчёта: результат пересчёта расчётов документа."""
    return format_audit((run.computed_metrics or {}).get("__numeric_audit__"))


def format_audit(audit: dict[str, Any] | None) -> str:
    """Человекочитаемый блок по результату аудита (без привязки к AnalysisRun)."""
    if not audit:
        return ""
    if not audit.get("checked"):
        return ("АРИФМЕТИЧЕСКИЙ АУДИТ: заявленных вычислений в документе не "
                "найдено (проверять нечего).")
    lines = [f"АРИФМЕТИЧЕСКИЙ АУДИТ: проверено {audit['checked']} "
             f"заявленных вычислений документа пересчётом."]
    for m in audit.get("mismatches") or []:
        lines.append(
            f"- НЕ СХОДИТСЯ: {m['what'] or m['op']} — заявлено {m['claimed']}, "
            f"пересчёт даёт {m['expected']} (расхождение "
            f"{round(m['rel_delta'] * 100, 1)}%). Цитата: «{m['quote']}»")
    if not audit.get("mismatches"):
        lines.append("- все проверенные вычисления сходятся.")
    return "\n".join(lines)
