# -*- coding: utf-8 -*-
"""Символическая память с drill-down (перенос TencentDB-Agent-Memory, G1+G2).

Чистая структура данных без I/O/сети — тесты быстрые и детерминированные."""
from __future__ import annotations

import pytest
from backend.core.memory.symbolic_memory import SymbolicMemory, one_line


def test_one_line_collapses_and_truncates():
    assert one_line("  первая\nстрока  ", 80) == "первая"
    assert one_line("\n\n  \nтело", 80) == "тело"
    assert one_line("", 80) == ""
    long = "x" * 200
    got = one_line(long, 20)
    assert len(got) == 20 and got.endswith("…")


def test_add_returns_ids_and_symbol_has_size_marker():
    sm = SymbolicMemory(symbol_chars=40)
    big = "деталь " * 100  # длинное сырьё
    n1 = sm.add(big, label="search склад")
    n2 = sm.add("коротко")
    assert n1 == "n1" and n2 == "n2"
    view = sm.render()
    assert "[n1] search склад" in view
    assert "⟨" in view and "c⟩" in view          # маркер объёма у большого атома
    assert "[n2] коротко" in view
    # у короткого атома маркера объёма нет
    assert "коротко ⟨" not in view


def test_expand_drills_down_to_raw():
    sm = SymbolicMemory()
    raw = "полное сырьё tool-run'а\nмного строк\n" + "z" * 500
    nid = sm.add(raw, label="compute остатки")
    assert sm.expand(nid) == raw           # символ в render, сырьё — по id
    assert raw not in sm.render()          # сырья в компактном виде нет


def test_group_builds_scene_with_downward_pointers():
    sm = SymbolicMemory(symbol_chars=60)
    a = sm.add("A" * 300, label="search")
    b = sm.add("B" * 300, label="compute")
    c = sm.add("C" * 300, label="draft")
    sid = sm.group([a, b, c], label="план закупок")
    assert sid == "s1"          # у сцен собственный счётчик id (n… отдельно от s…)
    view = sm.render()
    # сцена в верхнем потоке, атомы — под ней с отступом
    assert "[s1] план закупок ▸3" in view
    lines = view.splitlines()
    assert lines[0].startswith("[s1]")
    assert all(ln.startswith("  [n") for ln in lines[1:])  # дети с отступом
    # drill-down: trace до листьев, expand собирает всё сырьё
    assert sm.trace(sid) == [a, b, c]
    assert sm.expand(sid) == "A" * 300 + "\n" + "B" * 300 + "\n" + "C" * 300


def test_group_inserts_scene_at_first_child_position():
    sm = SymbolicMemory()
    x = sm.add("x", label="до")
    a = sm.add("a", label="ша1")
    b = sm.add("b", label="ша2")
    y = sm.add("y", label="после")
    sm.group([a, b], label="сцена")
    # верхний поток = строки без отступа; дети сцены — с отступом
    top = [ln.split("]")[0].lstrip("[")
           for ln in sm.render().splitlines() if ln.startswith("[")]
    # до → сцена (на месте первого ребёнка) → после; a,b ушли внутрь сцены
    assert top == [x, "s1", y]
    assert a not in top and b not in top


def test_nested_scenes_trace_through_levels():
    sm = SymbolicMemory()
    a = sm.add("a"); b = sm.add("b"); c = sm.add("c")
    inner = sm.group([a, b], label="внутр")
    outer = sm.group([inner, c], label="внешн")
    assert sm.trace(outer) == [a, b, c]        # сквозной provenance через уровни


def test_group_rejects_empty_and_expand_unknown_raises():
    sm = SymbolicMemory()
    with pytest.raises(ValueError):
        sm.group(["нет-такого"], label="пусто")
    with pytest.raises(KeyError):
        sm.expand("n999")
    with pytest.raises(KeyError):
        sm.trace("s999")


def test_token_savings_positive_on_verbose_input():
    sm = SymbolicMemory(symbol_chars=40)
    for i in range(5):
        sm.add(f"шаг {i}: " + "подробности " * 80, label=f"tool{i}")
    stats = sm.token_savings()
    assert stats["full_chars"] > stats["symbolic_chars"]
    assert stats["saved_pct"] >= 60           # заявленная TencentDB экономия ~60%


def test_non_string_raw_is_coerced():
    sm = SymbolicMemory()
    nid = sm.add({"a": 1}, label="dict")
    assert sm.expand(nid) == "{'a': 1}"


def test_empty_memory_renders_empty():
    sm = SymbolicMemory()
    assert sm.render() == ""
    assert sm.token_savings() == {"full_chars": 0, "symbolic_chars": 0,
                                  "saved_chars": 0, "saved_pct": 0}
