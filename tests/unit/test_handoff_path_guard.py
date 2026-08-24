# -*- coding: utf-8 -*-
"""Зона исполнения кодинг-агента: что можно, что нельзя.

Инцидент: TESSENT_HANDOFF_REPO_ROOT по умолчанию пуст, и это означало «агент
может работать в ЛЮБОЙ папке сервера» — включая /etc, ~/.ssh и собственный
data/ Tessbrain (токены, ключи, mcp_tokens.json). Заслон стоит в единой точке
запуска (_exec_handoff), поэтому его не обойти ни через /dispatch, ни через
узел доски, ни через kanon-луп.

Требование, которое тесты защищают: закрыть опасное, НЕ сломав рабочее
(обычный git-репозиторий и dogfooding по самому Tessbrain должны исполняться).
"""
import os
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _guard():
    """Грузим только функцию-заслон, без тяжёлых зависимостей модуля."""
    import logging
    src = (_ROOT / "backend" / "core" / "tasks" / "task_analysis.py").read_text(
        encoding="utf-8")
    start = src.index("_DENY_POSIX_ROOTS")
    end = src.index("async def _exec_handoff")
    ns = {"os": os, "logger": logging.getLogger("t"),
          "__file__": str(_ROOT / "backend" / "core" / "tasks" / "task_analysis.py")}
    exec(src[start:end], ns)  # noqa: S102 — целевой код проекта, не ввод извне
    return ns["handoff_path_violation"]


@pytest.fixture()
def guard(monkeypatch):
    monkeypatch.delenv("TESSENT_HANDOFF_REPO_ROOT", raising=False)
    return _guard()


# ── НЕ ломаем рабочие сценарии ──────────────────────────────────────────────

def test_normal_repo_allowed(guard, tmp_path):
    assert guard(str(tmp_path)) == ""


def test_own_repo_allowed_for_dogfooding(guard):
    """Tessbrain сам себя дорабатывает кодинг-агентом — это должно работать."""
    assert guard(str(_ROOT)) == ""
    assert guard(str(_ROOT / "backend")) == ""


def test_empty_path_is_not_blocked_here(guard):
    """Пустой путь отсекается отдельной проверкой isdir, не этой."""
    assert guard("") == ""


# ── Закрываем опасное ───────────────────────────────────────────────────────

def test_own_secrets_dir_blocked(guard):
    """data/ и config/ Tessbrain — там токены, ключи, mcp_tokens.json."""
    assert guard(str(_ROOT / "data")) != ""
    assert guard(str(_ROOT / "config")) != ""
    assert guard(str(_ROOT / "data" / "sub")) != ""


@pytest.mark.skipif(os.name == "nt", reason="POSIX-пути")
def test_system_dirs_blocked(guard):
    for p in ("/etc", "/usr/bin", "/root", "/boot"):
        assert guard(p) != "", p


@pytest.mark.skipif(os.name == "nt", reason="POSIX-пути")
def test_filesystem_root_blocked(guard):
    assert guard("/") != ""


# ── Строгий режим: задан корень ─────────────────────────────────────────────

def test_explicit_root_confines(monkeypatch, tmp_path):
    root = tmp_path / "repos"
    (root / "app").mkdir(parents=True)
    monkeypatch.setenv("TESSENT_HANDOFF_REPO_ROOT", str(root))
    g = _guard()
    assert g(str(root / "app")) == ""
    assert g(str(root)) == ""
    assert g(str(tmp_path / "elsewhere")) != ""


def test_explicit_root_blocks_traversal(monkeypatch, tmp_path):
    """../ не должен выводить за корень (realpath нормализует)."""
    root = tmp_path / "repos"
    root.mkdir(parents=True)
    monkeypatch.setenv("TESSENT_HANDOFF_REPO_ROOT", str(root))
    g = _guard()
    assert g(str(root / ".." / "secret")) != ""
