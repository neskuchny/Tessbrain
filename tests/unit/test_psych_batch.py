# -*- coding: utf-8 -*-
"""Тесты батч-режима психопрофилей (Шаг C, флаг PSYCH_BATCH)."""
from __future__ import annotations

import asyncio
import json

from backend.core.capture.agents.psychological_analyzer import PsychologicalAnalyzer


def _run(coro):
    return asyncio.run(coro)


class _FakeLLM:
    def __init__(self, payload: str):
        self.payload = payload
        self.calls = 0

    async def generate(self, prompt, **kw):
        self.calls += 1
        return self.payload


_PARTS = [
    {"name": "Аня", "role": "PM"},
    {"name": "Боря", "role": "Dev"},
]


def test_batched_maps_names_roles_one_call() -> None:
    arr = json.dumps([
        {"personality_type": "C", "strengths": ["анализ"]},   # без name → берём из списка
        {"name": "Боря", "personality_type": "D"},
    ], ensure_ascii=False)
    a = PsychologicalAnalyzer(llm_router=_FakeLLM(arr))
    res = _run(a._create_profiles_batched("транскрипт Аня Боря", _PARTS, {}))
    assert len(res) == 2 and a.llm.calls == 1          # один вызов на всех
    assert res[0]["name"] == "Аня" and res[0]["role"] == "PM"
    assert res[1]["name"] == "Боря"


def test_batched_returns_empty_on_junk() -> None:
    a = PsychologicalAnalyzer(llm_router=_FakeLLM("не json"))
    assert _run(a._create_profiles_batched("t", _PARTS, {})) == []


def test_batched_empty_when_too_few_covered() -> None:
    # 1 профиль на 4 участников → покрытие < половины → [] (вызывающий откатится)
    arr = json.dumps([{"name": "Аня"}])
    parts4 = _PARTS + [{"name": "Вика"}, {"name": "Гена"}]
    a = PsychologicalAnalyzer(llm_router=_FakeLLM(arr))
    assert _run(a._create_profiles_batched("t", parts4, {})) == []


def test_default_off_uses_per_participant(monkeypatch) -> None:
    # без флага PSYCH_BATCH — идёт per-участника (N вызовов), батч не трогается
    monkeypatch.delenv("PSYCH_BATCH", raising=False)
    a = PsychologicalAnalyzer(llm_router=_FakeLLM(json.dumps({"personality_type": "C"})))
    res = _run(a._create_personality_profiles("t Аня Боря", _PARTS, {}))
    assert a.llm.calls == 2 and len(res) == 2          # по вызову на участника
