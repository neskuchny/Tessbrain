"""Unit-тесты web-only трека: build_launch / resolve_target_key / list_targets."""
from __future__ import annotations

from urllib.parse import unquote

from backend.core.executors.web_targets import (
    WEB_TARGETS,
    build_launch,
    list_targets,
    resolve_target_key,
)


def test_resolve_explicit_target_wins() -> None:
    assert resolve_target_key("v0", "landing") == "v0"
    assert resolve_target_key("V0", None) == "v0"


def test_resolve_default_by_task_type() -> None:
    assert resolve_target_key(None, "landing") == "lovable"
    assert resolve_target_key(None, "code") == "bolt"
    assert resolve_target_key("", "unknown_type") == "lovable"  # fallback


def test_resolve_unknown_target_falls_back() -> None:
    assert resolve_target_key("bogus", "landing") == "lovable"


def test_build_launch_prefills_when_param_supported() -> None:
    r = build_launch("claude", task_title="Сделать КП",
                     tz_markdown="# ТЗ\nдетали задачи")
    assert r["tool"] == "claude"
    assert r["prefilled"] is True
    assert r["url"].startswith("https://claude.ai/new?q=")
    assert "Сделать" in unquote(r["url"])
    assert r["brief"] == "# ТЗ\nдетали задачи"   # полный бриф всегда есть


def test_build_launch_no_prefill_for_lovable() -> None:
    r = build_launch("lovable", task_title="Лендинг", tz_markdown="# ТЗ")
    assert r["tool"] == "lovable"
    assert r["prefilled"] is False
    assert r["url"] == "https://lovable.dev/"
    assert r["brief"] == "# ТЗ"


def test_build_launch_caps_url_length() -> None:
    huge = "x" * 50000
    r = build_launch("v0", task_title="Большое ТЗ", tz_markdown=huge)
    # даже с огромным ТЗ URL остаётся в пределах лимита (усечён до заголовка)
    assert len(r["url"]) <= 2000
    assert r["brief"] == huge                     # полный бриф не теряется


def test_list_targets_shape() -> None:
    targets = list_targets()
    keys = {t["key"] for t in targets}
    assert {"lovable", "v0", "bolt", "replit", "claude", "chatgpt"} <= keys
    for t in targets:
        assert set(t.keys()) == {"key", "label", "kind", "prefill", "note"}
    assert len(targets) == len(WEB_TARGETS)
