# -*- coding: utf-8 -*-
"""Сохранение полного психоанализа: payload'ы узлов PsychInsight.

Четыре блока психоанализа (мотивы, групповая динамика, прогнозы поведения,
рекомендации) считались на каждой встрече и выбрасывались. По решению
владельца продукта сохраняются. Здесь проверяются контракты сборки:

  1. мотив без имени человека НЕ сохраняется — анонимное обвинение в графе
     хуже потерянного анализа;
  2. мотивы и прогнозы всегда sensitive с повышенным грифом;
  3. рекомендация «для всей команды» не привязывается к человеку;
  4. разбухший или битый LLM-ответ не порождает мусорных узлов.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load():
    name = "backend.core.capture.psych_persistence"
    if name in sys.modules:
        return sys.modules[name]
    for pkg in ("backend", "backend.core", "backend.core.capture"):
        if pkg not in sys.modules:
            mod = types.ModuleType(pkg)
            mod.__path__ = [os.path.join(ROOT, *pkg.split("."))]
            sys.modules[pkg] = mod
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, "backend", "core", "capture",
                           "psych_persistence.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_pp = _load()
build = _pp.build_psych_insight_payloads
KW = dict(meeting_id="m1", user_id="u1", created_at="2026-08-13T00:00:00+00:00")


def test_motive_without_person_is_dropped():
    payloads = build({
        "hidden_motives": [
            {"participant": "", "manipulation_signs": ["давит на жалость"]},
            {"participant": "Аня", "true_intentions": ["хочет вести проект"]},
        ],
    }, **KW)
    kinds = [(p["properties"]["kind"], p.get("person_name")) for p in payloads]
    assert kinds == [("motives", "Аня")], (
        "мотив без человека — анонимное обвинение, сохранять нельзя"
    )
    print("✅ мотив без имени человека не сохраняется")


def test_motives_and_forecasts_are_sensitive():
    payloads = build({
        "hidden_motives": [{"participant": "Аня", "power_seeking": "стремится"}],
        "behavioral_predictions": [{"participant": "Боря",
                                    "stress_reaction": "замыкается"}],
        "management_recommendations": [{"recommendation": "хвалить публично",
                                        "target": "Боря"}],
    }, **KW)
    by_kind = {p["properties"]["kind"]: p["properties"] for p in payloads}
    for kind in ("motives", "behavior_forecast"):
        assert by_kind[kind]["sensitive"] is True
        assert by_kind[kind]["access_level"] == 4, (
            f"{kind} обязан нести повышенный гриф"
        )
    assert "sensitive" not in by_kind["recommendation"], (
        "рекомендация — рабочий инструмент, не гриф-4"
    )
    print("✅ мотивы и прогнозы sensitive с грифом 4, рекомендации — нет")


def test_group_dynamics_single_node_no_person():
    payloads = build({
        "group_dynamics": {
            "trust_level": "medium",
            "alliances": [{"members": ["Аня", "Боря"], "basis": "старый проект"}],
            "power_dynamics": [{"influencer": "Аня", "influenced": ["Вера"]}],
        },
    }, **KW)
    assert len(payloads) == 1
    p = payloads[0]
    assert p["properties"]["kind"] == "group_dynamics"
    assert p["person_name"] is None, (
        "динамика — свойство встречи, а не одного человека"
    )
    assert p["properties"]["alliances"][0]["members"] == ["Аня", "Боря"]
    print("✅ групповая динамика — один узел на встречу, без привязки к человеку")


def test_team_recommendation_not_bound_to_person():
    payloads = build({
        "management_recommendations": [
            {"recommendation": "ввести ретро", "target": "вся команда"},
            {"recommendation": "давать голос первым", "target": "Вера"},
        ],
    }, **KW)
    targets = {p["properties"]["target"]: p["person_name"] for p in payloads}
    assert targets["вся команда"] is None
    assert targets["Вера"] == "Вера"
    print("✅ «вся команда» не привязывается к человеку, конкретное имя — да")


def test_empty_and_garbage_input():
    assert build({}, **KW) == []
    assert build(None, **KW) == []
    payloads = build({
        "hidden_motives": ["строка вместо dict", 42, None],
        "behavioral_predictions": [{"no_participant": True}],
        "management_recommendations": [{"target": "Аня"}],  # без текста
        "group_dynamics": {},
    }, **KW)
    assert payloads == [], "битый ответ LLM не должен порождать узлы"
    print("✅ пустой и битый вход → ноль узлов")


def test_overgrown_response_is_capped():
    many = [{"participant": f"Ч{i}", "fears": ["x"]} for i in range(100)]
    payloads = build({"hidden_motives": many}, **KW)
    assert len(payloads) == 20, "кап на разбухший LLM-ответ"
    print("✅ разбухший ответ обрезается до 20")


def test_node_ids_are_deterministic():
    """Повторная обработка той же встречи перезаписывает те же узлы,
    а не плодит дубли."""
    a = build({"hidden_motives": [{"participant": "Аня"}]}, **KW)
    b = build({"hidden_motives": [{"participant": "Аня"}]}, **KW)
    assert a[0]["node_id"] == b[0]["node_id"] == "psychinsight_motives_m1_0"
    print("✅ id детерминированы — пересинк не плодит дубли")


def test_schema_has_node_and_edge():
    src = open(os.path.join(ROOT, "backend/core/store/schema.py"),
               encoding="utf-8").read()
    assert '"PsychInsight"' in src
    assert '"HAS_PSYCH_INSIGHT"' in src
    print("✅ тип узла и ребро объявлены в схеме")


def test_sync_does_not_index_insights_into_search():
    """Sensitive-слой не должен попадать в общий поиск."""
    src = open(os.path.join(ROOT, "backend/core/knowledge_sync.py"),
               encoding="utf-8").read()
    start = src.index("build_psych_insight_payloads")
    block = src[start:start + 2500]
    assert "upsert_vector_public" not in block, (
        "PsychInsight нельзя писать в векторный индекс"
    )
    assert "bm25" not in block.lower(), "PsychInsight нельзя писать в BM25"
    print("✅ инсайты не индексируются в поиск")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе тесты сохранения психоанализа прошли.")
