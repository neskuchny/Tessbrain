#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Цена за правильный ответ — второе число рядом с точностью.

Правило из инженерии памяти (Stanford): у системы памяти всегда два
числа — качество И цена за правильный ответ, и первое не называется без
второго. Две системы с одинаковой точностью расходятся по цене в разы,
и никакой бенчмарк точности этого не покажет.

Что считаем и в чём честность. Сохранённые прогоны НЕ записывали токены —
поэтому долларов здесь нет и не будет, пока прогоны не начнут их писать.
Что в них ЕСТЬ достоверно: сколько вопросов, какая рука, сколько ответов
верны. Из устройства харнесса известно число вызовов модели на вопрос:
1 генерация + 1 судья. Отсюда честная метрика:

    вызовов модели на один ПРАВИЛЬНЫЙ ответ = вызовы / правильные

Она сравнима между руками одного прогона (модель и судья одинаковы) и
именно это отношение показывает то самое «одинаковая точность — разная
цена». Сравнивать её МЕЖДУ моделями нельзя — вызовы разных моделей стоят
по-разному; это написано и в выводе.

Запуск:  python scripts/cost_per_correct.py
"""
from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
EVAL = os.path.join(os.path.dirname(HERE), "backend", "core", "eval")

# вызовов модели на вопрос в руке: генерация + судья
CALLS_PER_QUESTION = 2


def _fmt(x: float) -> str:
    return "∞ (ни одного правильного)" if x == float("inf") else f"{x:.1f}"


def analyze(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    if not isinstance(d, dict):
        return []
    summary = d.get("summary")
    n = int(d.get("n") or 0)
    if not (isinstance(summary, dict) and n):
        return []
    lines = [f"\n=== {os.path.basename(path)} "
             f"(модель {d.get('model', '?')}, n={n})"]
    if d.get("INVALID"):
        lines.append("  ⚠ ПРОГОН НЕДЕЙСТВИТЕЛЕН — числа не считаем: "
                     + str(d.get("invalid_reason", ""))[:160])
        return lines
    rows = []
    for arm, s in summary.items():
        acc = s.get("accuracy") if isinstance(s, dict) else None
        if acc is None:
            continue
        # рука с вырожденными ответами или фолбэками не участвует в цене:
        # завышенная точность дала бы завышенно дешёвый «правильный ответ»
        if isinstance(s, dict) and s.get("valid") is False:
            lines.append(f"  {arm:12s} ⚠ рука невалидна "
                         f"(вырожденных {s.get('degenerate','?')}) — пропущена")
            continue
        correct = acc * n
        calls = n * CALLS_PER_QUESTION
        cpc = calls / correct if correct else float("inf")
        rows.append((arm, acc, cpc))
    for arm, acc, cpc in sorted(rows, key=lambda r: (r[2], -r[1])):
        lines.append(f"  {arm:12s} точность {acc:.3f}   "
                     f"вызовов на правильный ответ {_fmt(cpc)}")
    if len(rows) >= 2:
        best = min(r[2] for r in rows if r[2] != float("inf"))
        worst = max((r[2] for r in rows if r[2] != float("inf")),
                    default=best)
        if best and worst and worst / best >= 1.5:
            lines.append(f"  → разрыв цены между руками: ×{worst / best:.1f} "
                         f"при том же числе вызовов — решает точность")
    return lines


def main() -> int:
    out = ["Цена за правильный ответ (вызовы модели / правильные ответы)",
           "Правило: точность не называется без цены.",
           "Сравнимо между руками одного прогона; между моделями — нет:",
           "вызовы разных моделей стоят по-разному. Долларов нет, потому",
           "что прогоны не писали токены, — это честнее, чем оценка."]
    found = False
    for fname in sorted(os.listdir(EVAL)):
        if not fname.endswith(".json"):
            continue
        try:
            lines = analyze(os.path.join(EVAL, fname))
        except Exception as exc:
            lines = [f"\n=== {fname}: не разобран ({exc})"]
        if lines:
            found = True
            out.extend(lines)
        try:
            out.extend(money_report(os.path.join(EVAL, fname)))
        except Exception:
            pass
    if not found:
        out.append("\nПрогонов с summary/accuracy не найдено.")
    print("\n".join(out))
    return 0




# ── Цена в деньгах (когда прогон записал токены) ────────────────────────
# Тарифы за 1M токенов, USD. Источник — прайс-листы провайдеров на дату
# прогона; вынесено таблицей, чтобы цифру можно было пересчитать, а не
# принимать на веру. Неизвестная модель → денег не считаем, честно молчим.
PRICES_USD_PER_1M = {
    "gemini-3.1-flash-lite": {"in": 0.10, "out": 0.40},
    "gpt-4o-mini":           {"in": 0.15, "out": 0.60},
    "gpt-4o":                {"in": 2.50, "out": 10.00},
}


def money_report(path: str) -> list[str]:
    """Цена прогона в долларах, если он записал usage. Иначе — молчим."""
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    usage = d.get("usage") or {}
    if not usage.get("calls"):
        return []
    model = str(d.get("model") or "")
    price = PRICES_USD_PER_1M.get(model)
    n = int(d.get("n") or 0)
    summary = d.get("summary") or {}
    lines = [f"\n=== ДЕНЬГИ: {os.path.basename(path)} (модель {model})",
             f"  вызовов {usage['calls']}, вход {usage['prompt_tokens']}, "
             f"выход {usage['completion_tokens']} токенов"]
    if not price:
        lines.append("  тариф для этой модели не задан — доллары не считаем")
        return lines
    total = (usage["prompt_tokens"] / 1e6 * price["in"]
             + usage["completion_tokens"] / 1e6 * price["out"])
    lines.append(f"  стоимость прогона ≈ ${total:.4f}")
    best = max(((a, s.get("accuracy") or 0) for a, s in summary.items()
                if isinstance(s, dict)), key=lambda x: x[1], default=None)
    if best and n:
        correct = best[1] * n
        if correct:
            # Прогон общий на все руки — делим поровну, это ОЦЕНКА сверху
            # для лучшей руки; точную раскладку по рукам API не даёт.
            per_arm = total / max(1, len(summary))
            lines.append(
                f"  лучшая рука «{best[0]}» ({best[1]:.3f}): "
                f"≈ ${per_arm / correct:.4f} за правильный ответ "
                f"(доля прогона на руку — оценка, API раскладки не даёт)")
    return lines


if __name__ == "__main__":
    raise SystemExit(main())
