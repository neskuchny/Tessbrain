"""Unit-тесты P12g: user-portrait (профиль клиента в системный промпт).

Чистый stdlib-модуль — importlib. Загрузчик инъектируем (без БД).
"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import types

_ROOT = pathlib.Path(__file__).resolve().parents[2]

for pkg in ("backend", "backend.core", "backend.core.hermes"):
    if pkg not in sys.modules:
        m = types.ModuleType(pkg)
        m.__path__ = []  # type: ignore[attr-defined]
        sys.modules[pkg] = m
_spec = importlib.util.spec_from_file_location(
    "backend.core.hermes.portrait",
    _ROOT / "backend/core/hermes/portrait.py",
)
_p = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _p
_spec.loader.exec_module(_p)

render_portrait = _p.render_portrait
maybe_portrait_block = _p.maybe_portrait_block
load_profile = _p.load_profile
maybe_portrait_for_user = _p.maybe_portrait_for_user


def _run(c):
    return asyncio.run(c)


_PROFILE = {
    "user_id": "u1",
    "name": "Иван",
    "role": "PM",
    "email": "secret@x.com",          # identity — НЕ должно протечь
    "communication_style": "кратко, по делу",
    "preferred_language": "ru",
    "expertise": ["backend", "графы"],
    "current_projects": ["Alpha"],
    "blind_spots": "",                 # пустое — пропустить
    "random_unknown_field": "LEAK",    # не в allow-list — не показывать
}


# === render_portrait ===================================================

def test_render_includes_allowed_nonempty_fields() -> None:
    out = render_portrait(_PROFILE)
    assert "Стиль общения: кратко, по делу" in out
    assert "Язык: ru" in out
    assert "Экспертиза: backend, графы" in out      # список → CSV
    assert "Текущие проекты: Alpha" in out
    assert "Иван" in out and "PM" in out            # identity в заголовке


def test_render_excludes_identity_and_unknown_and_empty() -> None:
    out = render_portrait(_PROFILE)
    assert "secret@x.com" not in out                # email не протёк
    assert "LEAK" not in out                        # неизвестное поле
    assert "Слепые зоны" not in out                 # пустое поле пропущено


def test_render_empty_or_garbage_returns_empty() -> None:
    assert render_portrait({}) == ""
    assert render_portrait(None) == ""
    assert render_portrait("nope") == ""
    assert render_portrait({"user_id": "x"}) == ""  # только identity → пусто


def test_render_no_identity_header_when_absent() -> None:
    out = render_portrait({"preferred_language": "en"})
    assert out.startswith("Портрет пользователя — учитывай")
    assert "(" not in out.split("\n")[0]


# === maybe_portrait_block (gate) =======================================

def test_block_disabled_is_empty() -> None:
    assert maybe_portrait_block(_PROFILE, enabled=False) == ""


def test_block_enabled_renders() -> None:
    assert "Стиль общения" in maybe_portrait_block(_PROFILE, enabled=True)


# === load_profile (injectable fetcher) =================================

def test_load_profile_with_fetcher() -> None:
    async def fetch(uid):
        return {"preferred_language": "ru"} if uid == "u1" else {}

    assert _run(load_profile("u1", fetcher=fetch)) == {"preferred_language": "ru"}
    assert _run(load_profile("", fetcher=fetch)) == {}            # no uid


def test_load_profile_fetcher_failure_safe() -> None:
    async def boom(uid):
        raise RuntimeError("db down")

    assert _run(load_profile("u1", fetcher=boom)) == {}            # never raises


def test_load_profile_fetcher_garbage_safe() -> None:
    async def bad(uid):
        return "not a dict"

    assert _run(load_profile("u1", fetcher=bad)) == {}


# === maybe_portrait_for_user (end-to-end, gated) =======================

def test_portrait_for_user_disabled() -> None:
    async def fetch(uid):
        raise AssertionError("must not fetch when disabled")

    assert _run(maybe_portrait_for_user("u1", fetcher=fetch,
                                        enabled=False)) == ""


def test_portrait_for_user_enabled() -> None:
    async def fetch(uid):
        return _PROFILE

    out = _run(maybe_portrait_for_user("u1", fetcher=fetch, enabled=True))
    assert "Портрет пользователя" in out and "PM" in out
    assert "secret@x.com" not in out


def test_portrait_for_user_empty_profile_safe() -> None:
    async def fetch(uid):
        return {}

    assert _run(maybe_portrait_for_user("u1", fetcher=fetch,
                                        enabled=True)) == ""
