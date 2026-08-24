# -*- coding: utf-8 -*-
"""
Gate-0 для readiness-gates / Q0: добавляет ли скелет-гейт ПОВЕРХ рекомендаций
LLM ценность, которой у чистого списка нет — БЕЗ ложных срабатываний?

Метод (честный): для сценариев, специально провоцирующих Q0 / «лучше руками»,
живой LLM генерит список рекомендаций (baseline). Тот же список прогоняем через
readiness_gates (skeleton): Q0 наверх, «руками» помечается. Frozen-reading: гейт
сработал верно (поймал то, что список не приоритизировал) или дал шум (ложный
ярлык / неверный подъём)? Один ложный ярлык на нейтральном кейсе = регрессия.

Запуск: OPENAI_API_KEY в окружении.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _load_gates():
    p = os.path.join(_ROOT, "backend", "core", "think", "readiness_gates.py")
    s = importlib.util.spec_from_file_location("rg_ab", p)
    m = importlib.util.module_from_spec(s)
    sys.modules["rg_ab"] = m
    s.loader.exec_module(m)
    return m


def _llm_json(system: str, user: str, model="gpt-4o-mini") -> dict:
    key = os.environ["OPENAI_API_KEY"]
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.4, "max_tokens": 700,
        "response_format": {"type": "json_object"},
    }).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    r = json.load(urllib.request.urlopen(req, timeout=90))
    return json.loads(r["choices"][0]["message"]["content"])


_SYSTEM = (
    "Ты — консультант по автоматизации. Дай РОВНО 5 конкретных рекомендаций списком. "
    'Верни JSON {"recommendations": ["...", "...", ...]}. Без преамбулы.'
)

# Сценарии: e — что гейт ДОЛЖЕН поймать (ожидание для frozen-reading).
_SCENARIOS = [
    {
        "id": "S1_q0_already_paid",
        "known_tools": ["MeetFlow", "CRM Bitrix", "Notion"],
        "prompt": (
            "Компания уже платит за MeetFlow, CRM Bitrix и Notion. Но: задачи со встреч "
            "теряются, CRM-аналитика не включена, в Notion никто не ведёт базу знаний. "
            "Команда жалуется «не хватает инструментов». Что делать?"
        ),
        "expect": "Q0-подъём активаций уже оплаченного (MeetFlow→задачи, CRM-аналитика, Notion)",
    },
    {
        "id": "S2_manual_better",
        "known_tools": [],
        "prompt": (
            "Раз в квартал команда вручную готовит индивидуальное стратегическое КП под "
            "крупного клиента — это требует переговоров и живого суждения, процесс каждый "
            "раз разный. Стоит ли это автоматизировать и как?"
        ),
        "expect": "«лучше руками» флаг (редкость + живое суждение + меняется)",
    },
    {
        "id": "S3_clear_automate_no_flag",
        "known_tools": [],
        "prompt": (
            "150 типовых заявок в день, чёткие стабильные правила категоризации, высокий "
            "объём, ошибок 12%. Что порекомендуешь по автоматизации категоризации?"
        ),
        "expect": "НЕТ ложных ярлыков (это чистый кейс на автоматизацию)",
    },
]


def run() -> dict:
    rg = _load_gates()
    out = []
    for sc in _SCENARIOS:
        recs = _llm_json(_SYSTEM, sc["prompt"]).get("recommendations", [])
        annotated = rg.annotate_recommendations(recs, known_tools=sc["known_tools"])
        out.append({
            "id": sc["id"], "expect": sc["expect"], "known_tools": sc["known_tools"],
            "baseline_order": recs,
            "skeleton": [{"text": a["text"], "verdict": a["verdict"], "note": a["note"]}
                         for a in annotated],
            "q0_lifted": [a["text"] for a in annotated if a["verdict"] == "Q0"],
            "flagged": [{"text": a["text"], "verdict": a["verdict"]}
                        for a in annotated if a["verdict"] in ("manual", "check_manual")],
        })
    return {"scenarios": out}


if __name__ == "__main__":
    res = run()
    for s in res["scenarios"]:
        print("=" * 78)
        print(f"[{s['id']}] ожидание: {s['expect']}")
        print(f"  baseline (порядок LLM):")
        for i, r in enumerate(s["baseline_order"], 1):
            print(f"    {i}. {r}")
        print(f"  skeleton: Q0-подъём={len(s['q0_lifted'])}, флагов={len(s['flagged'])}")
        for a in s["skeleton"]:
            tag = f" [{a['verdict']}]" if a["verdict"] != "ok" else ""
            print(f"    -{tag} {a['text']}")
            if a["note"]:
                print(f"        {a['note']}")
    p = os.path.join(_HERE, "gate0_readiness_run.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(f"\nполный прогон → {p}")
