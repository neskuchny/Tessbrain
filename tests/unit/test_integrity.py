# -*- coding: utf-8 -*-
"""Проверка целостности ядра (анти-тампер). Подпись подменяется
(_verify_manifest_signature) — реальный Ed25519 в этом test-env паникует,
в проде работает; здесь тестируем логику хэш-сверки и enforce."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import backend.core.integrity as integ


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    """Корень репо → tmp; подпись → всегда валидна (кроме явных тестов)."""
    monkeypatch.setattr(integ, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(integ, "_MANIFEST_PATH",
                        tmp_path / "deploy" / "onprem" / "integrity_manifest.json")
    monkeypatch.setattr(integ, "_verify_manifest_signature", lambda mb, s: True)
    return tmp_path


def _write_core(root: Path, mapping: dict[str, str]):
    for rel, content in mapping.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _write_manifest(root: Path):
    files = integ.compute_hashes([
        "backend/core/licensing.py", "backend/core/sima/prompts.py"])
    mpath = root / "deploy" / "onprem" / "integrity_manifest.json"
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_bytes(json.dumps({"version": "1", "files": files},
                                 sort_keys=True).encode())


def test_intact_core_passes(wired):
    _write_core(wired, {"backend/core/licensing.py": "print('license')",
                        "backend/core/sima/prompts.py": "PROMPT='x'"})
    _write_manifest(wired)
    res = integ.verify_integrity(signature_b64="ZmFrZQ")
    assert res["ok"] is True and res["checked"] == 2 and not res["tampered"]


def test_tampered_module_detected(wired):
    _write_core(wired, {"backend/core/licensing.py": "print('license')",
                        "backend/core/sima/prompts.py": "PROMPT='x'"})
    _write_manifest(wired)
    # вор правит licensing.py (напр. вырезал проверку) → хэш не сходится
    (wired / "backend/core/licensing.py").write_text("print('BYPASS')")
    res = integ.verify_integrity(signature_b64="ZmFrZQ")
    assert res["ok"] is False
    assert "backend/core/licensing.py" in res["tampered"]


def test_missing_module_detected(wired):
    _write_core(wired, {"backend/core/licensing.py": "x",
                        "backend/core/sima/prompts.py": "y"})
    _write_manifest(wired)
    (wired / "backend/core/licensing.py").unlink()
    res = integ.verify_integrity(signature_b64="ZmFrZQ")
    assert res["ok"] is False
    assert "backend/core/licensing.py" in res["missing"]


def test_no_signature_fails(wired):
    _write_core(wired, {"backend/core/licensing.py": "x",
                        "backend/core/sima/prompts.py": "y"})
    _write_manifest(wired)
    res = integ.verify_integrity(signature_b64="")   # без подписи — фейл
    assert res["ok"] is False and "подпись" in res["reason"]


def test_bad_signature_fails(wired, monkeypatch):
    _write_core(wired, {"backend/core/licensing.py": "x",
                        "backend/core/sima/prompts.py": "y"})
    _write_manifest(wired)
    monkeypatch.setattr(integ, "_verify_manifest_signature", lambda mb, s: False)
    res = integ.verify_integrity(signature_b64="ZmFrZQ")
    assert res["ok"] is False and "подпис" in res["reason"]


def test_compiled_so_is_hashed(wired):
    # crown-jewel скомпилирован в .so — целостность считается по .so
    _write_core(wired, {"backend/core/licensing.cpython-311.so": "BINARY"})
    p = integ._module_path("backend/core/licensing.py")
    assert p is not None and p.name.endswith(".so")


def test_enforce_flag_off_is_noop(monkeypatch):
    monkeypatch.delenv("TESSENT_INTEGRITY_REQUIRED", raising=False)
    assert integ.enforce_integrity_on_startup() is None


def test_enforce_on_blocks_on_tamper(wired, monkeypatch):
    _write_core(wired, {"backend/core/licensing.py": "x",
                        "backend/core/sima/prompts.py": "y"})
    _write_manifest(wired)
    (wired / "backend/core/sima/prompts.py").write_text("STOLEN")
    monkeypatch.setenv("TESSENT_INTEGRITY_REQUIRED", "on")
    monkeypatch.setenv("TESSENT_INTEGRITY_SIG", "ZmFrZQ")
    with pytest.raises(RuntimeError):
        integ.enforce_integrity_on_startup()
