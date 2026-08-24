# -*- coding: utf-8 -*-
"""Auto/Tess: предпочтительный Enhanced-путь search_knowledge больше не мёртв.

Что закрепляем:
1. Хелпер build_enhanced_search: выключатель, never-raise на недостающих
   аргументах и на сбое импорта, и успешная сборка (на подставных модулях —
   тяжёлых зависимостей в песочнице нет, но проверяется, что хелпер
   передаёт именно наши объекты в правильные фабрики).
2. Структурно: все ТРИ места создания TessentBrainTools в agents.py
   передают enhanced_search=build_enhanced_search(...). Именно отсутствие
   этого аргумента и было багом (SEARCH_CONSUMERS_AUDIT) — тест не даст
   рефакторингу молча уронить его снова.

Запуск:  python tests/unit/test_auto_enhanced_search.py
"""
from __future__ import annotations

import io
import os
import re
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  ПЛОХО ") + name + ("" if cond else f" — {detail}"))
    if not cond:
        failures.append(name)


def _pkgs(*names: str) -> None:
    for n in names:
        if n not in sys.modules:
            m = types.ModuleType(n)
            m.__path__ = []
            sys.modules[n] = m


# ── 1. хелпер: вырезаем функцию из живого файла ──────────────────────
src = io.open(os.path.join(ROOT, "backend/integrations/tessent_brain_tools.py"),
              encoding="utf-8").read()
start = src.index("def build_enhanced_search")
end = src.index("\nclass TessentBrainTools", start)
ns: dict = {"logger": __import__("logging").getLogger("t")}
exec(src[start:end], ns)
build = ns["build_enhanced_search"]

print("хелпер build_enhanced_search:")

os.environ["TESSENT_AUTO_ENHANCED"] = "off"
check("выключатель off → None (прежнее поведение)",
      build(graph_builder=object(), vector_indexer=object()) is None)
os.environ.pop("TESSENT_AUTO_ENHANCED", None)

check("нет graph_builder → None", build(vector_indexer=object()) is None)
check("нет vector_indexer → None", build(graph_builder=object()) is None)

# сбой импорта (в песочнице backend.core.search не соберётся без стабов) —
# должен вернуть None, а не бросить: это и есть never-raise деградация
for m in list(sys.modules):
    if m.startswith("backend"):
        del sys.modules[m]
try:
    got = build(graph_builder=object(), vector_indexer=object())
    check("сбой сборки → None, не исключение", got is None, repr(got))
except Exception as e:
    check("сбой сборки → None, не исключение", False, repr(e))

# успешная сборка на подставных модулях: проверяем, что хелпер передал
# именно НАШИ объекты в фабрику hybrid и обёртку enhanced
_pkgs("backend", "backend.core", "backend.core.search", "backend.core.llm")
calls = {}

hso = types.ModuleType("backend.core.search.hybrid_search_orchestrator")


def _get_hybrid(vector_indexer=None, graph_builder=None, user_id=None):
    calls["hybrid"] = (vector_indexer, graph_builder, user_id)
    return "HYBRID"


hso.get_hybrid_search_orchestrator = _get_hybrid
sys.modules["backend.core.search.hybrid_search_orchestrator"] = hso

eso = types.ModuleType("backend.core.search.enhanced_search_orchestrator")


class _Enhanced:
    def __init__(self, hybrid_search=None, llm_router=None):
        calls["enhanced"] = (hybrid_search, llm_router)


eso.EnhancedSearchOrchestrator = _Enhanced
sys.modules["backend.core.search.enhanced_search_orchestrator"] = eso

gb, vi, lr = object(), object(), object()
got = build(graph_builder=gb, vector_indexer=vi, llm_router=lr, user_id="u1")
check("успешная сборка возвращает Enhanced", isinstance(got, _Enhanced), repr(got))
check("в hybrid ушли наши graph/vector/user",
      calls.get("hybrid") == (vi, gb, "u1"), str(calls.get("hybrid")))
check("в Enhanced ушли hybrid и наш llm_router",
      calls.get("enhanced") == ("HYBRID", lr), str(calls.get("enhanced")))

# ── 2. структурно: три сайта в agents.py передают аргумент ───────────
print("места создания TessentBrainTools в agents.py:")
ag = io.open(os.path.join(ROOT, "backend/api/routes/agents.py"), encoding="utf-8").read()
# Не regex до первой «)» — скобка в комментарии обрезала бы тело вызова.
# Берём окно в 30 строк от каждого вхождения: вызов заведомо короче.
sites = [ag[m.end():m.end() + 1600].split("\n\n")[0]
         for m in re.finditer(r"tools = TessentBrainTools\(", ag)]
check("мест создания по-прежнему три", len(sites) == 3, f"нашлось {len(sites)}")
without = [i for i, body in enumerate(sites, 1) if "enhanced_search=" not in body]
check("во ВСЕХ трёх передан enhanced_search",
      not without, f"без аргумента: сайты {without}")
check("сборка идёт через build_enhanced_search (не копипаста конструкции)",
      all("build_enhanced_search(" in b for b in sites))
check("vector_indexer передан во всех трёх (сайты 2-3 раньше его теряли)",
      all("vector_indexer=" in b for b in sites))

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} — {', '.join(failures)}")
    raise SystemExit(1)
print("всё сошлось")
