"""Unit-тесты для openhands_events (W20)."""
from __future__ import annotations

from backend.core.executors.backends.openhands_events import (
    _TERMINAL_ACTIONS,
    _extract_events_list,
)

# === _extract_events_list ==============================================

def test_extract_from_list() -> None:
    data = [{"id": 1, "action": "message"}, {"id": 2, "action": "finish"}]
    out = _extract_events_list(data)
    assert len(out) == 2
    assert out[0]["id"] == 1


def test_extract_from_dict_events_key() -> None:
    data = {"events": [{"id": 1}, {"id": 2}]}
    out = _extract_events_list(data)
    assert len(out) == 2


def test_extract_from_dict_items_key() -> None:
    data = {"items": [{"id": 1}]}
    out = _extract_events_list(data)
    assert len(out) == 1


def test_extract_from_dict_data_key() -> None:
    data = {"data": [{"id": 1, "action": "step"}]}
    out = _extract_events_list(data)
    assert len(out) == 1


def test_extract_filters_non_dicts() -> None:
    data = [{"id": 1}, "not a dict", None, {"id": 2}]
    out = _extract_events_list(data)
    assert len(out) == 2


def test_extract_empty_list() -> None:
    assert _extract_events_list([]) == []


def test_extract_unknown_schema() -> None:
    """Не узнали schema — возвращаем []."""
    assert _extract_events_list({"some_other_key": [1, 2]}) == []
    assert _extract_events_list("not even a dict") == []
    assert _extract_events_list(None) == []


# === _TERMINAL_ACTIONS =================================================

def test_terminal_actions_known() -> None:
    """Эти actions останавливают streamer."""
    assert "finish" in _TERMINAL_ACTIONS
    assert "stop" in _TERMINAL_ACTIONS
    assert "error" in _TERMINAL_ACTIONS
    assert "abort" in _TERMINAL_ACTIONS


def test_terminal_actions_excludes_normal() -> None:
    assert "message" not in _TERMINAL_ACTIONS
    assert "step" not in _TERMINAL_ACTIONS
    assert "tool_call" not in _TERMINAL_ACTIONS
