# -*- coding: utf-8 -*-
"""Идентификаторы моделей DeepSeek: живые в списках, снятые — перенаправлены.

Сверено 14.08.2026 по api-docs.deepseek.com (страницы pricing и list-models):
в API остались ТОЛЬКО `deepseek-v4-pro` и `deepseek-v4-flash`. Модели
`deepseek-v3.2`, `deepseek-chat`, `deepseek-reasoner` из списка исчезли.

Два разных требования, и их легко перепутать:

· В СПИСКАХ ВЫБОРА (что показываем пользователю) снятых моделей быть не
  должно — иначе человек выбирает то, что вернёт 400.
· В МАППИНГЕ запроса снятые id обязаны ОСТАТЬСЯ — но вести на живую
  модель. На сохранённых досках эти id уже лежат; удаление ключа сломало
  бы работающие процессы, а не «почистило легаси».

Плюс цены: каталог помечает deepseek как `verified`, значит числа обязаны
совпадать с прайсом. Расхождение по flash (0.09/0.18 против 0.14/0.28) и
было главной находкой — метка «сверено» стояла при несверенных числах.

Запуск:  python tests/unit/test_deepseek_model_ids.py
"""
from __future__ import annotations

import io
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Живые модели и их цены (cache miss), сверено 14.08.2026
ALIVE = {"deepseek-v4-pro", "deepseek-v4-flash"}
RETIRED = {"deepseek-v3.2", "deepseek-chat", "deepseek-reasoner"}
PRICES = {"deepseek-4-pro": (0.435, 0.870), "deepseek-4-flash": (0.14, 0.28)}

failures: list[str] = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  ПЛОХО ") + name + ("" if cond else f" — {detail}"))
    if not cond:
        failures.append(name)


def read(rel):
    return io.open(os.path.join(ROOT, rel), encoding="utf-8").read()


print("списки выбора — снятых моделей быть не должно:")
node = read("frontend/banana/components/nodes/LLMGenerateNode.tsx")
ds_block = node[node.index("deepseek: ["):]
ds_block = ds_block[:ds_block.index("]")]
for r in RETIRED:
    check(f"узел досок не предлагает {r}", f'"{r}"' not in ds_block)
check("узел досок предлагает обе живые",
      all(f'"{a}"' in ds_block for a in ALIVE), ds_block[:120])

panel = read("frontend/components/ModelSettingsPanel.tsx")
line = [ln for ln in panel.split("\n") if "id: 'deepseek'" in ln][0]
for r in RETIRED:
    check(f"панель настроек не предлагает {r}", r not in line)
check("панель настроек предлагает живые",
      all(a in line for a in ALIVE), line.strip()[:110])

print("маппинг запроса — снятые id ведут на живую модель:")
route = read("frontend/app/api/banana/llm/route.ts")
ds = route[route.index("deepseek: {"):]
ds = ds[:ds.index("qwen: {")]
pairs = dict(re.findall(r'"([\w.-]+)":\s*"([\w.-]+)"', ds))
for r in RETIRED:
    check(f"{r} остался ключом (сохранённые доски не ломаются)", r in pairs,
          "ключ удалён — доски с ним получат 400")
    check(f"{r} ведёт на живую модель", pairs.get(r) in ALIVE, str(pairs.get(r)))
for a in ALIVE:
    check(f"{a} маппится сам на себя", pairs.get(a) == a, str(pairs.get(a)))

print("цены в каталоге совпадают с прайсом (метка verified обязывает):")
cat = read("scripts/model_catalog.py")
for name, (pin, pout) in PRICES.items():
    m = re.search(r'\{"name": "' + re.escape(name) +
                  r'".*?"in": ([\d.]+), "out": ([\d.]+)', cat, re.S)
    check(f"{name} — вход {pin}", m and float(m.group(1)) == pin,
          m and m.group(1))
    check(f"{name} — выход {pout}", m and float(m.group(2)) == pout,
          m and m.group(2))

print("backend отдаёт живые API-id:")
mr = read("backend/core/llm/model_router.py")
ids = dict(re.findall(r'"(deepseek-4-[\w]+)": "([\w.-]+)"', mr))
check("deepseek-4-pro → deepseek-v4-pro", ids.get("deepseek-4-pro") == "deepseek-v4-pro",
      str(ids.get("deepseek-4-pro")))
check("deepseek-4-flash → deepseek-v4-flash",
      ids.get("deepseek-4-flash") == "deepseek-v4-flash", str(ids.get("deepseek-4-flash")))
check("в backend нет снятых id",
      not any(r in mr for r in RETIRED),
      [r for r in RETIRED if r in mr])

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} — {', '.join(failures[:5])}")
    raise SystemExit(1)
print("всё сошлось")
