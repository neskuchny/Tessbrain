"""Unit-тесты для core.llm.system_prompt_builder (W7 user-adaptive)."""
from __future__ import annotations

from backend.core.llm.system_prompt_builder import (
    _PREAMBLE_HEADER,
    build_personalization_preamble,
    merge_with_base,
)


def test_empty_profile_returns_empty_preamble() -> None:
    assert build_personalization_preamble(None) == ""
    assert build_personalization_preamble({}) == ""


def test_profile_with_only_unrecognized_fields_returns_empty() -> None:
    """Поля, которые builder не знает, не должны порождать preamble."""
    assert build_personalization_preamble({"random_field": "value"}) == ""


def test_role_and_department_in_preamble() -> None:
    profile = {"role": "manager", "department": "Engineering"}
    out = build_personalization_preamble(profile)
    assert _PREAMBLE_HEADER in out
    assert "manager" in out
    assert "Engineering" in out


def test_communication_preferences() -> None:
    profile = {
        "communication_style": "executive",
        "detail_level": "brief",
        "preferred_language": "en",
    }
    out = build_personalization_preamble(profile)
    assert "style=executive" in out
    assert "detail=brief" in out
    assert "lang=en" in out


def test_focus_and_projects() -> None:
    profile = {
        "current_focus": "Q4 launch",
        "current_projects": ["Alpha", "Beta", "Gamma", "Delta"],
    }
    out = build_personalization_preamble(profile)
    assert "Q4 launch" in out
    # Берём только первые 3 проекта.
    assert "Alpha" in out and "Beta" in out and "Gamma" in out
    assert "Delta" not in out


def test_expertise_and_blind_spots() -> None:
    profile = {
        "expertise": ["python", "ml"],
        "known_topics": ["k8s"],
        "blind_spots": ["accounting", "legal"],
    }
    out = build_personalization_preamble(profile)
    assert "expert in: python, ml" in out
    assert "knows: k8s" in out
    assert "explain-when-mentioned: accounting, legal" in out


def test_decision_style() -> None:
    out = build_personalization_preamble({"decision_style": "data-driven"})
    assert "decision-style: data-driven" in out


def test_preamble_truncated_when_oversized() -> None:
    """Длинные строковые значения не должны раздуть preamble.

    Builder усекает списки до 5 элементов; гарантируем срабатывание
    cap'а на body=800 одной длинной строкой в current_focus.
    """
    profile = {"current_focus": "x" * 2000}
    out = build_personalization_preamble(profile)
    assert len(out) <= 1000  # header + body cap
    assert out.endswith("...")


def test_short_profile_not_truncated() -> None:
    """Короткие профили не должны получать суффикс '...'."""
    profile = {"current_focus": "Q4 launch"}
    out = build_personalization_preamble(profile)
    assert not out.endswith("...")


def test_enum_value_unwrapped() -> None:
    """Enum-like (.value) должны корректно сериализоваться."""

    class FakeEnum:
        def __init__(self, v: str) -> None:
            self.value = v

    profile = {"communication_style": FakeEnum("technical")}
    out = build_personalization_preamble(profile)
    assert "style=technical" in out


# === merge_with_base ===

def test_merge_empty_preamble_returns_base() -> None:
    assert merge_with_base("base", "") == "base"
    assert merge_with_base(None, "") is None


def test_merge_empty_base_returns_preamble() -> None:
    assert merge_with_base(None, "preamble") == "preamble"
    assert merge_with_base("", "preamble") == "preamble"


def test_merge_preamble_before_base() -> None:
    out = merge_with_base("you are helpful", "User: short answers")
    # Preamble идёт ПЕРЕД base — модель прочитает про юзера до бизнес-инструкций.
    assert out.startswith("User: short answers")
    assert "you are helpful" in out
