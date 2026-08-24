# -*- coding: utf-8 -*-
"""Тесты анти-спама версий снапшота компании (баг «Сравнить показывает пусто»)."""
from __future__ import annotations

from backend.core.sleep.enhanced_snapshot import CompanySnapshot


def test_no_change_merge_does_not_bump_version() -> None:
    a = CompanySnapshot(name="КПД")
    a.version = 10
    a.merge(CompanySnapshot(name="КПД"))  # ничего материального
    assert a.version == 10                 # версия НЕ бампится на шуме
    assert a.delta_summary == "Минорные обновления"


def test_material_merge_bumps_and_records_real_delta() -> None:
    a = CompanySnapshot(name="КПД")
    a.version = 10
    b = CompanySnapshot(name="КПД")
    b.key_people = [{"name": "Саша"}]
    b.active_projects = [{"name": "Проект X"}]
    a.merge(b)
    assert a.version == 11                          # реальное изменение → +1
    assert "Проект X" in a.delta_summary            # delta осмысленный, не «нет изменений»
    assert "Саша" in a.delta_summary


def test_status_change_detected_after_overwrite_bug_fixed() -> None:
    # регресс: раньше delta считался ПОСЛЕ перезаписи self → всегда пусто.
    a = CompanySnapshot(name="КПД")
    a.version = 5
    a.current_status = "старый статус"
    b = CompanySnapshot(name="КПД")
    b.current_status = "новый статус"
    a.merge(b)
    assert a.version == 6
    assert "новый статус" in a.delta_summary
