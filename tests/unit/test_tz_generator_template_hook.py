"""Test что EnhancedTZGenerator (W18) использует template_resolver
для injection per-tenant template prompt в Generate stage (W25).

Без поднятого LLM/storage — мокаем только то что Generate использует.
"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import types

import pytest

# heavy module — load via importlib.util чтобы обойти каскадные deps
# (см. test_profile_curator pattern).
_MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "backend" / "core" / "think" / "task_specification"
    / "enhanced_tz_generator.py"
)


def _load_module():
    # Регистрируем lightweight stub-пакеты для импортов внутри модуля.
    for pkg in ("backend", "backend.core", "backend.core.think",
                "backend.core.think.task_specification"):
        if pkg not in sys.modules:
            m = types.ModuleType(pkg)
            m.__path__ = []  # type: ignore[attr-defined]
            sys.modules[pkg] = m

    # Stub `base_agent` (он импортируется enhanced_tz_generator'ом, но мы
    # не используем его в тесте).
    base_path = _MODULE_PATH.parent / "base_agent.py"
    if base_path.exists():
        spec = importlib.util.spec_from_file_location(
            "backend.core.think.task_specification.base_agent",
            base_path,
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)

    spec = importlib.util.spec_from_file_location(
        "backend.core.think.task_specification.enhanced_tz_generator",
        _MODULE_PATH,
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_mod = _load_module()
EnhancedTZGenerator = _mod.EnhancedTZGenerator


class _CapturingLLM:
    """Стаб LLM который записывает последний prompt и возвращает marker."""

    def __init__(self) -> None:
        self.last_prompt = None
        self.last_kwargs = None

    async def generate(self, *, prompt, system_prompt=None, **kwargs):
        self.last_prompt = prompt
        self.last_kwargs = {"system_prompt": system_prompt, **kwargs}
        return "## fake tz markdown"

    async def generate_json(self, *, prompt, system_prompt=None, **kwargs):
        return {}


def _run(coro):
    return asyncio.run(coro)


def test_init_accepts_template_resolver() -> None:
    """Backward-compat: existing constructor сигнатура работает + новый kwarg."""
    g1 = EnhancedTZGenerator(llm=None)
    assert g1.template_resolver is None

    async def _resolver(task_type):
        return None

    g2 = EnhancedTZGenerator(llm=None, template_resolver=_resolver)
    assert g2.template_resolver is not None


def test_no_resolver_no_template_block_in_prompt() -> None:
    """Default behavior preserved: без resolver template-block отсутствует."""
    llm = _CapturingLLM()
    gen = EnhancedTZGenerator(llm=llm)
    _run(gen._stage_generate(
        description="test task",
        understanding={"task_type": "landing"},
        structured={
            "facts": ["fact1"], "opinions": [], "numbers": [],
            "assumptions": [], "key_decisions": [], "risks": [],
        },
        analysis={"completeness_score": 7, "gaps": []},
    ))
    assert llm.last_prompt is not None
    # Маркер из render_template_prompt отсутствует.
    assert "ШАБЛОН ДЛЯ ЭТОГО ТИПА ЗАДАЧИ" not in llm.last_prompt


def test_resolver_injects_template_into_prompt() -> None:
    """Если resolver возвращает template — render_template_prompt в prompt."""
    template = {
        "name": "custom_landing",
        "blocks": [{"name": "hero", "label": "Hero"}],
    }

    async def _resolver(task_type):
        return template if task_type == "landing" else None

    llm = _CapturingLLM()
    gen = EnhancedTZGenerator(llm=llm, template_resolver=_resolver)
    _run(gen._stage_generate(
        description="x",
        understanding={"task_type": "landing"},
        structured={
            "facts": [], "opinions": [], "numbers": [],
            "assumptions": [], "key_decisions": [], "risks": [],
        },
        analysis={"completeness_score": 5, "gaps": []},
    ))
    assert llm.last_prompt is not None
    assert "custom_landing" in llm.last_prompt
    assert "ШАБЛОН ДЛЯ ЭТОГО ТИПА ЗАДАЧИ" in llm.last_prompt
    assert "hero" in llm.last_prompt


def test_resolver_returns_none_no_block_added() -> None:
    """Resolver возвращает None — prompt без template-block."""
    async def _resolver(task_type):
        return None

    llm = _CapturingLLM()
    gen = EnhancedTZGenerator(llm=llm, template_resolver=_resolver)
    _run(gen._stage_generate(
        description="x",
        understanding={"task_type": "landing"},
        structured={
            "facts": [], "opinions": [], "numbers": [],
            "assumptions": [], "key_decisions": [], "risks": [],
        },
        analysis={"completeness_score": 5, "gaps": []},
    ))
    assert "ШАБЛОН ДЛЯ ЭТОГО ТИПА ЗАДАЧИ" not in llm.last_prompt


def test_resolver_failure_does_not_break_generate() -> None:
    """Resolver raises → generate всё равно работает (best-effort)."""
    async def _resolver(task_type):
        raise RuntimeError("simulated")

    llm = _CapturingLLM()
    gen = EnhancedTZGenerator(llm=llm, template_resolver=_resolver)
    out = _run(gen._stage_generate(
        description="x",
        understanding={"task_type": "landing"},
        structured={
            "facts": [], "opinions": [], "numbers": [],
            "assumptions": [], "key_decisions": [], "risks": [],
        },
        analysis={"completeness_score": 5, "gaps": []},
    ))
    # generate всё равно вернул результат.
    assert out is not None
    assert llm.last_prompt is not None


def test_resolver_passes_correct_task_type() -> None:
    """Resolver получает task_type который понял understand-стадия."""
    received_task_types = []

    async def _resolver(task_type):
        received_task_types.append(task_type)
        return None

    llm = _CapturingLLM()
    gen = EnhancedTZGenerator(llm=llm, template_resolver=_resolver)
    _run(gen._stage_generate(
        description="x",
        understanding={"task_type": "api"},
        structured={
            "facts": [], "opinions": [], "numbers": [],
            "assumptions": [], "key_decisions": [], "risks": [],
        },
        analysis={"completeness_score": 5, "gaps": []},
    ))
    assert received_task_types == ["api"]
