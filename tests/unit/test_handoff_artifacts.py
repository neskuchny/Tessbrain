# -*- coding: utf-8 -*-
"""Тесты сбора артефактов из scratch-папки (artifact_mode handoff).

_collect_artifacts — чистая функция: собирает файлы-результаты из рабочей папки
кодинг-агента, отсекая служебное/мусор, с сортировкой «презентабельные раньше».
"""
import os

from backend.core.tasks.task_analysis import _collect_artifacts


def test_collects_files_skips_spec_and_junk(tmp_path) -> None:
    (tmp_path / "task_spec.md").write_text("ТЗ", encoding="utf-8")
    (tmp_path / "presentation.html").write_text("<html>ok</html>", encoding="utf-8")
    (tmp_path / "calc.xlsx").write_bytes(b"PK\x03\x04data")
    (tmp_path / ".hidden").write_text("x", encoding="utf-8")
    junk = tmp_path / "node_modules"
    junk.mkdir()
    (junk / "lib.js").write_text("noise", encoding="utf-8")

    arts = _collect_artifacts(str(tmp_path))
    names = {a["name"] for a in arts}
    assert names == {"presentation.html", "calc.xlsx"}  # spec/hidden/junk отсеяны
    # презентабельные (html/xlsx) — с pref, порядок стабильный, поля на месте
    for a in arts:
        assert a["size"] > 0 and a["rel"] and os.path.isabs(a["path"])


def test_empty_and_missing_dir_safe() -> None:
    assert _collect_artifacts("") == []
    assert _collect_artifacts("/nonexistent/scratch/xyz") == []


def test_skips_oversize_files(tmp_path) -> None:
    (tmp_path / "big.bin").write_bytes(b"x" * 2048)
    (tmp_path / "small.html").write_text("<p>ok</p>", encoding="utf-8")
    arts = _collect_artifacts(str(tmp_path), max_bytes=1024)
    assert {a["name"] for a in arts} == {"small.html"}  # big.bin отсечён по размеру


def test_max_files_cap(tmp_path) -> None:
    for i in range(30):
        (tmp_path / f"f{i}.csv").write_text("a,b", encoding="utf-8")
    arts = _collect_artifacts(str(tmp_path), max_files=5)
    assert len(arts) == 5
