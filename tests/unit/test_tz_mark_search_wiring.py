# -*- coding: utf-8 -*-
"""Шаги 4-5 аудита поиска: ТЗ добирает BM25, MARK получил флаговый блок.

Проверка структурная (честно): стадия сбора ТЗ и сборка MARK-контекста —
методы с тяжёлыми зависимостями, в песочнице их не поднять. Закрепляем
инварианты по исходнику — те самые, отсутствие которых аудит и нашёл:

ТЗ (data_driven_orchestrator):
- BM25-добор существует и отдаёт результаты в ОБЩИЙ RRF (третий список),
  а не приклеен в конец;
- тенантность: user_id берётся из _current_user_id (реальное имя поля!),
  без него добор ПРОПУСКАЕТСЯ, а не лезет в общий singleton-индекс;
- выключатель TESSENT_TZ_BM25.

MARK (agents.py):
- блок упоминаний существует, по умолчанию ВЫКЛЮЧЕН (включение — явное
  TESSENT_MARK_HYBRID=on: аудит пометил это дизайн-вопросом, не багом);
- не платит за сборку движков (build_if_missing=False);
- результат подмешивается в _mark_prefix.

Запуск:  python tests/unit/test_tz_mark_search_wiring.py
"""
from __future__ import annotations

import io
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

failures: list[str] = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  ПЛОХО ") + name + ("" if cond else f" — {detail}"))
    if not cond:
        failures.append(name)


print("ТЗ — BM25-добор в стадии сбора:")
tz = io.open(os.path.join(
    ROOT, "backend/core/think/task_specification/data_driven_orchestrator.py"),
    encoding="utf-8").read()

check("блок добора существует", "bm25_items" in tz and "TESSENT_TZ_BM25" in tz)
check("тенантность: читается _current_user_id",
      '_uid = getattr(self, "_current_user_id", None)' in tz)
check("без user_id — пропуск, не общий индекс",
      re.search(r"if not _uid:\s*\n\s*raise LookupError", tz) is not None)
# порядок в файле: guard идёт РАНЬШЕ вызова фабрики
g = tz.index('_uid = getattr(self, "_current_user_id"')
f = tz.index("get_bm25_searcher(user_id=_uid)")
check("guard стоит до вызова фабрики", g < f)
check("bm25 участвует в общем RRF (b_ranked)",
      "b_ranked = sorted(bm25_items" in tz
      and "+ list(bm25_items)" in tz)
check("bm25 попадает в sources_used с меткой",
      'f"bm25:{d[\'search_query\'][:30]}"' in tz)
check("дедуп против уже собранных документов",
      '_seen_bm = {d["node_id"][5:] for d in doc_items}' in tz)

print("MARK — флаговый блок упоминаний:")
ag = io.open(os.path.join(ROOT, "backend/api/routes/agents.py"),
             encoding="utf-8").read()
i = ag.index("_mark_mentions")
blk = ag[i - 200:i + 1400]
check("блок существует", "_mark_mentions" in ag)
check("по умолчанию ВЫКЛЮЧЕН (включение только явным on)",
      'in (\n                        "on", "1", "true", "yes")' in blk)
check("не платит за сборку движков", "build_if_missing=False" in blk)
check("используется общий хелпер topic_fragments", "topic_fragments" in blk)
check("результат подмешивается в префикс",
      "[Упоминания по теме из памяти компании]" in ag)
check("never-raise (обёрнут в try/except)",
      "mark hybrid mentions skipped" in ag)

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} — {', '.join(failures)}")
    raise SystemExit(1)
print("всё сошлось")
