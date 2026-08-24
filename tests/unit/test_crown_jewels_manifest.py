# -*- coding: utf-8 -*-
"""Манифест crown-jewels не должен протухать: каждый путь существует,
иначе рецепт компиляции молча пропустит IP-модуль (и он уедет .py к
клиенту в полном on-prem)."""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST = _ROOT / "deploy" / "onprem" / "crown_jewels.txt"


def _entries() -> list[str]:
    out = []
    for line in _MANIFEST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def test_manifest_exists_and_nonempty():
    assert _MANIFEST.exists()
    assert len(_entries()) >= 15  # реальный набор соуса, не заглушка


def test_every_listed_module_exists():
    missing = [p for p in _entries() if not (_ROOT / p).is_file()]
    assert not missing, f"crown_jewels.txt ссылается на несуществующее: {missing}"


def test_no_plumbing_leaks_into_jewels():
    # Санити: в манифест не должны попадать роуты/CRUD (это плумбинг, не IP;
    # компилировать их бессмысленно и вредно для отладки).
    bad = [p for p in _entries() if "/api/routes/" in p or p.endswith("_client.py")]
    assert not bad, f"в crown-jewels затесался плумбинг: {bad}"
