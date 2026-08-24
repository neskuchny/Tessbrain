# -*- coding: utf-8 -*-
"""Fallback-анализ стиля коммуникации в сборке персоны.

Найдено при сверке раздела HR с кодом: `_aggregate_communication` звал
несуществующие `CommunicationStyleAgent(llm_client=…)` и `.extract()` —
у агента конструктор `llm_router=…` и метод `analyze()`. TypeError вылетал
ВНЕ try/except и ронял сборку персоны целиком, так что fallback-ветка не
работала ни разу с момента написания.

Тест сверяет вызов с реальной сигнатурой агента, чтобы расхождение
не вернулось молча: оба файла читаются, и всё, что aggregator вызывает,
обязано существовать у агента.
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)


def _read(rel: str) -> str:
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def test_constructor_matches_agent_signature():
    agent_src = _read("backend/core/capture/agents/communication_style_agent.py")
    aggr_src = _read("backend/core/persona/aggregator.py")

    m = re.search(r"class CommunicationStyleAgent.*?def __init__\(self, (.*?)\)",
                  agent_src, re.S)
    assert m, "не нашли конструктор агента"
    ctor_params = m.group(1)
    assert "llm_router" in ctor_params

    assert "CommunicationStyleAgent(llm_client=" not in aggr_src, (
        "aggregator снова зовёт конструктор с несуществующим llm_client="
    )
    assert "CommunicationStyleAgent(llm_router=" in aggr_src
    print("✅ конструктор вызывается с реальным параметром llm_router")


def test_method_exists_on_agent():
    agent_src = _read("backend/core/capture/agents/communication_style_agent.py")
    aggr_src = _read("backend/core/persona/aggregator.py")

    assert "async def analyze(" in agent_src
    assert "async def extract(" not in agent_src, (
        "если у агента появился extract — обнови этот тест осознанно"
    )
    assert "agent.extract(" not in aggr_src, (
        "aggregator снова зовёт несуществующий extract()"
    )
    assert "agent.analyze(" in aggr_src
    print("✅ вызывается существующий метод analyze()")


def test_result_key_matches_agent_output():
    """analyze() возвращает dict; профили лежат в communication_profiles."""
    agent_src = _read("backend/core/capture/agents/communication_style_agent.py")
    aggr_src = _read("backend/core/persona/aggregator.py")
    assert '"communication_profiles"' in agent_src
    assert 'get("communication_profiles")' in aggr_src, (
        "aggregator обязан разбирать dict-результат analyze(), а не ждать список"
    )
    print("✅ результат разбирается по реальному ключу communication_profiles")


def test_fields_used_exist_in_profile_model():
    """Поля, которые aggregator читает из профиля, есть в модели агента."""
    agent_src = _read("backend/core/capture/agents/communication_style_agent.py")
    for field in ("speech_pace", "use_of_jargon"):
        assert field in agent_src, f"aggregator читает {field}, а в агенте его нет"
    print("✅ читаемые поля существуют в модели профиля")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе тесты fallback-коммуникации прошли.")
