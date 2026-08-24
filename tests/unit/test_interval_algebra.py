# -*- coding: utf-8 -*-
"""Интервальная алгебра и диапазонный режим временного буста.

Три слоя проверки:
1. Сама алгебра: все 13 отношений Аллена на конкретных парах + свойство
   converse-симметрии на сетке пар + слияние/разрывы/покрытие.
2. Детектор диапазона: русские и английские периоды, относительные
   («в прошлом месяце») с якорем, и НЕ-срабатывание на «недавно»/голом
   «когда» — консервативность важна не меньше срабатываний.
3. Rerank: вопрос «в марте» поднимает мартовский документ над более
   свежим. КОНТРОЛЬ — тот же вход без вопроса (режим свежести) ставит
   свежий документ выше: значит механизм меняет реальный исход, а не
   украшает список. Плюс выключатель TESSENT_TEMPORAL_RANGE=off.

Запуск:  python tests/unit/test_interval_algebra.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _pkgs(*names: str) -> None:
    for n in names:
        if n not in sys.modules:
            m = types.ModuleType(n)
            m.__path__ = []
            sys.modules[n] = m


_pkgs("backend", "backend.core", "backend.core.temporal", "backend.core.search")
ia = _load("backend.core.temporal.interval_algebra",
           "backend/core/temporal/interval_algebra.py")
tr = _load("backend.core.search.temporal_range",
           "backend/core/search/temporal_range.py")
rr = _load("backend.core.search.temporal_rerank",
           "backend/core/search/temporal_rerank.py")

UTC = timezone.utc
D = lambda *a: datetime(*a, tzinfo=UTC)
I, R = ia.Interval, ia.IntervalRelation

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  ПЛОХО ") + name + ("" if cond else f" — {detail}"))
    if not cond:
        failures.append(name)


# ── 1. алгебра ────────────────────────────────────────────────────────
print("13 отношений Аллена на конкретных парах:")
cases = [
    (I(D(2025, 1, 1), D(2025, 2, 1)), I(D(2025, 3, 1), D(2025, 4, 1)), R.BEFORE),
    (I(D(2025, 3, 1), D(2025, 4, 1)), I(D(2025, 1, 1), D(2025, 2, 1)), R.AFTER),
    (I(D(2025, 1, 1), D(2025, 2, 1)), I(D(2025, 2, 1), D(2025, 3, 1)), R.MEETS),
    (I(D(2025, 2, 1), D(2025, 3, 1)), I(D(2025, 1, 1), D(2025, 2, 1)), R.MET_BY),
    (I(D(2025, 1, 1), D(2025, 3, 1)), I(D(2025, 2, 1), D(2025, 4, 1)), R.OVERLAPS),
    (I(D(2025, 2, 1), D(2025, 4, 1)), I(D(2025, 1, 1), D(2025, 3, 1)), R.OVERLAPPED_BY),
    (I(D(2025, 1, 1), D(2025, 2, 1)), I(D(2025, 1, 1), D(2025, 6, 1)), R.STARTS),
    (I(D(2025, 1, 1), D(2025, 6, 1)), I(D(2025, 1, 1), D(2025, 2, 1)), R.STARTED_BY),
    (I(D(2025, 2, 1), D(2025, 3, 1)), I(D(2025, 1, 1), D(2025, 6, 1)), R.DURING),
    (I(D(2025, 1, 1), D(2025, 6, 1)), I(D(2025, 2, 1), D(2025, 3, 1)), R.CONTAINS),
    (I(D(2025, 5, 1), D(2025, 6, 1)), I(D(2025, 1, 1), D(2025, 6, 1)), R.FINISHES),
    (I(D(2025, 1, 1), D(2025, 6, 1)), I(D(2025, 5, 1), D(2025, 6, 1)), R.FINISHED_BY),
    (I(D(2025, 1, 1), D(2025, 2, 1)), I(D(2025, 1, 1), D(2025, 2, 1)), R.EQUALS),
]
for a, b, want in cases:
    got = ia.relation(a, b)
    check(want.value, got is want, f"получили {got.value}")

print("свойства на сетке пар:")
# сетка: все пары интервалов по точкам 0..4 дней (включая открытые концы)
base = D(2025, 1, 1)
pts = [base + timedelta(days=k) for k in range(5)]
grid = [I(s, e) for s in pts for e in pts if s < e] + [I(s, None) for s in pts]
bad_conv = bad_ov = 0
for a in grid:
    for b in grid:
        rel = ia.relation(a, b)
        if ia.relation(b, a) is not ia.converse(rel):
            bad_conv += 1
        # overlaps должен совпадать с наивным пересечением полуоткрытых [s,e)
        naive = a.start < b.end_value and b.start < a.end_value
        if ia.overlaps(a, b) != naive:
            bad_ov += 1
check(f"converse-симметрия на {len(grid)}² парах", bad_conv == 0, f"{bad_conv} нарушений")
check("overlaps == наивное пересечение [s,e)", bad_ov == 0, f"{bad_ov} расхождений")

print("вырожденные и открытые интервалы:")
p = I(D(2025, 3, 15), D(2025, 3, 15))  # точка
check("точка == точка → EQUALS (не MEETS — фикс против оригинала)",
      ia.relation(p, I(D(2025, 3, 15), D(2025, 3, 15))) is R.EQUALS)
open_iv = I(D(2025, 1, 1), None)
check("открытый CONTAINS всё правее",
      ia.relation(open_iv, I(D(2026, 1, 1), D(2027, 1, 1))) is R.CONTAINS)
check("active_at: полуоткрытая семантика — конец НЕ активен",
      ia.active_at(I(D(2025, 1, 1), D(2025, 2, 1)), D(2025, 2, 1)) is False)
check("active_at: начало активно",
      ia.active_at(I(D(2025, 1, 1), D(2025, 2, 1)), D(2025, 1, 1)) is True)
check("active_at: открытый интервал активен всегда после старта",
      ia.active_at(open_iv, D(2099, 1, 1)) is True)
try:
    I(D(2025, 2, 1), D(2025, 1, 1))
    check("start > end отклоняется", False, "не бросил")
except ValueError:
    check("start > end отклоняется", True)

print("слияние, разрывы, покрытие:")
merged = ia.merge_intervals([
    I(D(2025, 1, 1), D(2025, 2, 1)), I(D(2025, 2, 1), D(2025, 3, 1)),  # стык
    I(D(2025, 2, 15), D(2025, 2, 20)),                                  # внутри
    I(D(2025, 6, 1), D(2025, 7, 1)),                                    # отдельно
])
check("стык и вложение слились в один, отдельный остался",
      len(merged) == 2 and merged[0].end == D(2025, 3, 1), str(merged))
check("merge идемпотентен", ia.merge_intervals(merged) == merged)
dom = I(D(2025, 1, 1), D(2025, 12, 31))
g = ia.gaps(merged, dom)
check("разрывов два (март→июнь, июль→конец)", len(g) == 2,
      f"{len(g)}: {[(x.start.date(), x.end.date()) for x in g]}")
cov = ia.coverage(merged, dom)
check("coverage в (0,1) и согласован с gaps",
      0 < cov < 1 and abs(cov + sum((x.end - x.start).total_seconds() for x in g)
                          / (dom.end - dom.start).total_seconds() - 1) < 1e-9,
      f"cov={cov:.4f}")
check("месяц/квартал/год согласованы: 12 месяцев == год",
      ia.merge_intervals([ia.month_interval(2025, m) for m in range(1, 13)])
      == [ia.year_interval(2025)])

# ── 2. детектор диапазона ─────────────────────────────────────────────
print("детектор диапазона (срабатывания):")
as_of = D(2026, 8, 14)
det = lambda q: tr.detect_range(q, as_of=as_of)
for q, want in [
    ("что решили в марте 2025?", ia.month_interval(2025, 3)),
    ("бюджет за март 2025 года", ia.month_interval(2025, 3)),
    ("what did we decide in March 2025", ia.month_interval(2025, 3)),
    ("итоги Q1 2025", ia.quarter_interval(2025, 1)),
    ("что было во втором квартале 2025", ia.quarter_interval(2025, 2)),
    ("расходы в 2024 году", ia.year_interval(2024)),
    ("решения в прошлом месяце", ia.month_interval(2026, 7)),
    ("встречи в этом году", ia.year_interval(2026)),
    ("в марте что обсуждали", ia.month_interval(2026, 3)),   # год от as_of
    ("что было в ноябре", ia.month_interval(2025, 11)),      # ещё не наступил → прошлый год
]:
    got = det(q)
    check(q[:44], got is not None and got.start == want.start and got.end == want.end,
          f"получили {got and (got.start.date(), got.end and got.end.date())}")

print("детектор диапазона (НЕ-срабатывания — консервативность):")
for q in ["что решили недавно?", "когда мы это обсуждали?", "покажи все задачи",
          "what happened recently", "решение по клиенту X", ""]:
    check(f"нет диапазона: «{q[:34]}»", det(q) is None, str(det(q)))

# ── 3. rerank: диапазонный режим против свежести ─────────────────────
print("rerank — вопрос про март против буста свежести:")


class FR:  # минимальный FusedResult
    def __init__(self, doc_id, score, date):
        self.doc_id, self.rrf_score = doc_id, score
        self.data = {"metadata": {"date": date}}


def run(query=None):
    fused = [FR("свежий_август", 0.100, "2026-08-10"),
             FR("мартовский", 0.098, "2026-03-12")]
    out = rr.apply_temporal_rerank(fused, query=query, as_of=as_of)
    return [x.doc_id for x in out]


os.environ.pop("TESSENT_TEMPORAL_RANGE", None)
check("КОНТРОЛЬ: без вопроса (свежесть) свежий август выше",
      run(None) == ["свежий_август", "мартовский"], str(run(None)))
check("с вопросом «что решили в марте» мартовский выше",
      run("что решили в марте?") == ["мартовский", "свежий_август"],
      str(run("что решили в марте?")))
check("вопрос БЕЗ периода → порядок как у свежести (байт-в-байт)",
      run("покажи все задачи") == run(None))
os.environ["TESSENT_TEMPORAL_RANGE"] = "off"
check("выключатель TESSENT_TEMPORAL_RANGE=off возвращает свежесть",
      run("что решили в марте?") == ["свежий_август", "мартовский"])
os.environ.pop("TESSENT_TEMPORAL_RANGE", None)

# гардрейл: вне периода — не наказываем
f = rr.range_factor(D(2026, 8, 10), ia.month_interval(2026, 3))
check("вне периода множитель ровно 1.0 (не штраф)", f == 1.0, str(f))
check("без даты множитель 1.0", rr.range_factor(None, ia.month_interval(2026, 3)) == 1.0)

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} — {', '.join(failures[:6])}")
    raise SystemExit(1)
print("всё сошлось")
