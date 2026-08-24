"""Unit-тесты для core.memory.profile_curator (W7).

Импорт через importlib.util чтобы обойти backend/core/memory/__init__.py,
который тянет mem0_client (требует пакет `mem0` с heavy deps).
"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib

_MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "backend" / "core" / "memory" / "profile_curator.py"
)
_spec = importlib.util.spec_from_file_location("_profile_curator_under_test", _MODULE_PATH)
_module = importlib.util.module_from_spec(_spec)
# Регистрируем в sys.modules ДО exec — иначе dataclass с Any-аннотацией
# не сможет resolve'ить тип через cls.__module__.
import sys

sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)
ProfileCurator = _module.ProfileCurator
ProfileUpdate = _module.ProfileUpdate
apply_proposals = _module.apply_proposals


# === apply_proposals (pure logic) ===

def test_apply_set_action() -> None:
    profile = {"preferred_language": "ru"}
    update = ProfileUpdate(
        action="set", field="preferred_language", value="en",
        confidence=0.9, reason="user wrote in English",
    )
    result = apply_proposals(profile, [update])
    assert result["preferred_language"] == "en"
    # Originalный dict не мутирован.
    assert profile["preferred_language"] == "ru"


def test_apply_append_to_list() -> None:
    profile = {"expertise": ["python"]}
    update = ProfileUpdate(action="append", field="expertise", value="ml",
                           confidence=0.8, reason="")
    result = apply_proposals(profile, [update])
    assert result["expertise"] == ["python", "ml"]


def test_apply_append_skips_duplicate() -> None:
    profile = {"expertise": ["python"]}
    update = ProfileUpdate(action="append", field="expertise", value="python",
                           confidence=0.9, reason="")
    result = apply_proposals(profile, [update])
    assert result["expertise"] == ["python"]  # no duplicate


def test_apply_append_creates_list_if_missing() -> None:
    profile: dict = {}
    update = ProfileUpdate(action="append", field="known_topics", value="k8s",
                           confidence=0.9, reason="")
    result = apply_proposals(profile, [update])
    assert result["known_topics"] == ["k8s"]


def test_apply_append_to_non_list_skipped() -> None:
    """Если поле не list — append игнорируется (защита от type-confusion)."""
    profile = {"current_focus": "Q3"}
    update = ProfileUpdate(action="append", field="current_focus", value="Q4",
                           confidence=0.9, reason="")
    result = apply_proposals(profile, [update])
    assert result["current_focus"] == "Q3"  # не изменилось


def test_apply_remove_filters_value() -> None:
    profile = {"blind_spots": ["accounting", "legal", "hr"]}
    update = ProfileUpdate(action="remove", field="blind_spots", value="legal",
                           confidence=0.9, reason="user demonstrated knowledge")
    result = apply_proposals(profile, [update])
    assert result["blind_spots"] == ["accounting", "hr"]


def test_apply_remove_missing_value_noop() -> None:
    profile = {"blind_spots": ["a"]}
    update = ProfileUpdate(action="remove", field="blind_spots", value="z",
                           confidence=0.9, reason="")
    result = apply_proposals(profile, [update])
    assert result["blind_spots"] == ["a"]


def test_apply_multiple_proposals_order() -> None:
    profile = {"known_topics": []}
    updates = [
        ProfileUpdate(action="append", field="known_topics", value="x", confidence=0.7, reason=""),
        ProfileUpdate(action="append", field="known_topics", value="y", confidence=0.7, reason=""),
        ProfileUpdate(action="remove", field="known_topics", value="x", confidence=0.9, reason=""),
    ]
    result = apply_proposals(profile, updates)
    assert result["known_topics"] == ["y"]


# === ProfileCurator.curate (parsing + filtering) ===

class _FakeRouter:
    """Стаб LLMRouter, отдаёт заданный JSON-ответ."""

    def __init__(self, response):
        self.response = response
        self.last_call = None

    async def generate_json(self, *, prompt, system_prompt, temperature, max_tokens):
        self.last_call = {
            "prompt": prompt,
            "system_prompt": system_prompt,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        return self.response


def test_curate_no_messages_returns_empty() -> None:
    curator = ProfileCurator(llm_router=_FakeRouter([]))

    async def go():
        return await curator.curate(messages=[], current_profile={})

    assert asyncio.run(go()) == []


def test_curate_no_router_returns_empty() -> None:
    curator = ProfileCurator(llm_router=None)

    async def go():
        return await curator.curate(
            messages=[{"role": "user", "content": "hi"}],
            current_profile={},
        )

    assert asyncio.run(go()) == []


def test_curate_filters_low_confidence() -> None:
    router = _FakeRouter([
        {"action": "set", "field": "preferred_language", "value": "en",
         "confidence": 0.4, "reason": "guess"},
        {"action": "set", "field": "preferred_language", "value": "en",
         "confidence": 0.9, "reason": "consistent"},
    ])
    curator = ProfileCurator(llm_router=router, min_confidence=0.6)

    async def go():
        return await curator.curate(
            messages=[{"role": "user", "content": "hello"}],
            current_profile={"preferred_language": "ru"},
        )

    proposals = asyncio.run(go())
    assert len(proposals) == 1
    assert proposals[0].confidence == 0.9


def test_curate_rejects_disallowed_field() -> None:
    """LLM не должен иметь права писать в identity-поля."""
    router = _FakeRouter([
        {"action": "set", "field": "email", "value": "evil@x", "confidence": 0.99},
        {"action": "set", "field": "user_id", "value": "hacker", "confidence": 0.99},
        {"action": "set", "field": "current_focus", "value": "Q4", "confidence": 0.8},
    ])
    curator = ProfileCurator(llm_router=router)

    async def go():
        return await curator.curate(
            messages=[{"role": "user", "content": "..."}],
            current_profile={},
        )

    proposals = asyncio.run(go())
    assert len(proposals) == 1
    assert proposals[0].field == "current_focus"


def test_curate_rejects_unknown_action() -> None:
    router = _FakeRouter([
        {"action": "delete_account", "field": "current_focus", "value": None, "confidence": 0.99},
        {"action": "set", "field": "current_focus", "value": "ok", "confidence": 0.7},
    ])
    curator = ProfileCurator(llm_router=router)

    async def go():
        return await curator.curate(
            messages=[{"role": "user", "content": "x"}],
            current_profile={},
        )

    proposals = asyncio.run(go())
    assert len(proposals) == 1
    assert proposals[0].action == "set"


def test_curate_handles_dict_response_with_updates_key() -> None:
    """Иногда модель оборачивает list в {"updates": [...]} — parser должен это понять."""
    router = _FakeRouter({"updates": [
        {"action": "set", "field": "preferred_language", "value": "de", "confidence": 0.95},
    ]})
    curator = ProfileCurator(llm_router=router)

    async def go():
        return await curator.curate(
            messages=[{"role": "user", "content": "guten tag"}],
            current_profile={},
        )

    proposals = asyncio.run(go())
    assert len(proposals) == 1
    assert proposals[0].value == "de"


def test_curate_passes_temperature_zero_for_determinism() -> None:
    """curate() должен дёргать LLM с temperature=0 (для prompt-cache + детерминированности)."""
    router = _FakeRouter([])
    curator = ProfileCurator(llm_router=router)

    async def go():
        await curator.curate(
            messages=[{"role": "user", "content": "x"}],
            current_profile={},
        )

    asyncio.run(go())
    assert router.last_call["temperature"] == 0
