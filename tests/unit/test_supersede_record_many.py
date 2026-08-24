# -*- coding: utf-8 -*-
"""Пакетная запись истории фактов.

Supersede-стор до сих пор наполнялся только ручными правками: цепочка
датированных версий с пометкой «заменено» существовала лишь для того, что
человек исправил руками. Разбор встречи писал версии в temporal-трекер, но
мимо supersede — то есть мимо самой пометки замещения и счётчика повторов.

Чтобы писать историю прямо с ингеста, нужна пакетная запись: на встрече за
раз фиксируются десятки фактов, и по одному record() на каждый означал бы
столько же циклов «взять лок, перечитать файл, записать файл».

Проверяется, что пакет ведёт себя ровно как последовательность record():
недеструктивно, идемпотентно по значению, с датировкой по встрече.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from backend.core.memory.supersede_store import SupersedeStore  # noqa: E402


def _store() -> SupersedeStore:
    """In-memory стор.

    Ветка с записью на диск здесь не гоняется намеренно: она уходит в
    file_lock из backend.core.store, а этот пакет тянет numpy, которого нет в
    песочнице. Проверяемая логика — семантика применения версий
    (_apply_record) — от способа хранения не зависит и общая с record();
    ветки различаются только обрамлением «взять лок → перечитать → сохранить»,
    которое дословно скопировано с record().
    """
    return SupersedeStore(persist_path=None)


def test_batch_writes_all_facts():
    s = _store()
    res = s.record_many([
        {"key": "u::person_1::role", "value": "аналитик",
         "source": "meeting:m1", "as_of": "2026-03-01T10:00:00+00:00"},
        {"key": "u::project_x::status", "value": "planning",
         "source": "meeting:m1", "as_of": "2026-03-01T10:00:00+00:00"},
    ])
    assert len(res) == 2
    assert all(r["action"] == "added" for r in res)
    assert s.current("u::person_1::role") == "аналитик"
    assert s.current("u::project_x::status") == "planning"
    print("✅ пакет записывает все факты")


def test_new_value_supersedes_without_deleting():
    """Ключевой инвариант: старое помечается заменённым, а не стирается."""
    s = _store()
    s.record_many([{"key": "u::person_1::role", "value": "аналитик",
                    "as_of": "2026-03-01T10:00:00+00:00"}])
    res = s.record_many([{"key": "u::person_1::role", "value": "CTO",
                          "as_of": "2026-06-01T10:00:00+00:00"}])
    assert res[0]["action"] == "superseded"
    assert s.current("u::person_1::role") == "CTO"

    hist = s.history("u::person_1::role")
    assert len(hist) == 2, "прошлая версия обязана остаться в истории"
    assert hist[0]["value"] == "аналитик"
    assert hist[0]["superseded_at"] == "2026-06-01T10:00:00+00:00"
    assert hist[0]["superseded_by"] == 1
    assert hist[1]["superseded_at"] is None
    print("✅ новое значение замещает, а не стирает прошлое")


def test_repeat_of_same_value_is_a_mention_not_a_duplicate():
    """Ту же роль назвали на трёх встречах — это одна версия и три упоминания."""
    s = _store()
    for i, day in enumerate(("2026-03-01", "2026-04-01", "2026-05-01")):
        r = s.record_many([{"key": "u::person_1::role", "value": "CTO",
                            "source": f"meeting:m{i}",
                            "as_of": f"{day}T10:00:00+00:00"}])
        assert r[0]["action"] == ("added" if i == 0 else "mention")
    assert len(s.history("u::person_1::role")) == 1
    assert s.total_mentions("u::person_1::role") == 3
    print("✅ повтор значения → упоминание, а не дубль версии")


def test_versions_are_dated_by_meeting_not_by_write_time():
    """as_of берётся из встречи — иначе загрузка архива схлопнет историю."""
    s = _store()
    s.record_many([
        {"key": "u::task_1::deadline", "value": "15 марта",
         "as_of": "2026-01-10T10:00:00+00:00"},
        {"key": "u::task_1::deadline", "value": "1 мая",
         "as_of": "2026-02-20T10:00:00+00:00"},
    ])
    hist = s.history("u::task_1::deadline")
    assert hist[0]["as_of"].startswith("2026-01-10")
    assert hist[1]["as_of"].startswith("2026-02-20")
    assert s.count_distinct_values("u::task_1::deadline") == 2
    print("✅ версии датируются встречей, а не моментом записи")


def test_empty_and_broken_items_are_skipped_silently():
    """Пустое поле на встрече — норма, а не повод уронить разбор."""
    s = _store()
    res = s.record_many([
        {"key": "u::person_1::role", "value": "CTO"},
        {"key": "", "value": "мусор"},
        {"key": "u::person_2::role", "value": ""},
        {"key": "u::person_3::role", "value": None},
        {},
        None,
    ])
    assert len(res) == 1
    assert s.current("u::person_1::role") == "CTO"
    assert s.current("u::person_2::role") is None
    print("✅ пустые и битые записи пропускаются молча")


def test_empty_batch_is_noop():
    s = _store()
    assert s.record_many([]) == []
    assert s.record_many(None) == []
    print("✅ пустой пакет — no-op")


def test_batch_equals_sequence_of_records():
    """Пакет обязан давать тот же результат, что и record() по одному."""
    seq, batch = _store(), _store()
    items = [
        {"key": "k::a", "value": "1", "as_of": "2026-01-01T00:00:00+00:00"},
        {"key": "k::b", "value": "x", "as_of": "2026-01-01T00:00:00+00:00"},
        {"key": "k::a", "value": "2", "as_of": "2026-02-01T00:00:00+00:00"},
        {"key": "k::a", "value": "2", "as_of": "2026-03-01T00:00:00+00:00"},
    ]
    for it in items:
        seq.record(it["key"], it["value"], as_of=it["as_of"])
    batch.record_many(items)

    for key in ("k::a", "k::b"):
        assert seq.history(key) == batch.history(key), f"расхождение по {key}"
        assert seq.current(key) == batch.current(key)
    print("✅ пакет эквивалентен последовательности record()")


def test_batch_does_not_multiply_keys():
    """Много наблюдений одного факта — один ключ, а не ключ на встречу."""
    s = _store()
    for day in ("2026-01-01", "2026-02-01", "2026-03-01"):
        s.record_many([{"key": "u::p::role", "value": f"роль-{day}",
                        "as_of": f"{day}T00:00:00+00:00"}])
    keys = s.keys(contains="u::p::")
    assert keys == ["u::p::role"], f"ожидали один ключ, получили {keys}"
    assert len(s.history("u::p::role")) == 3
    assert s.current("u::p::role") == "роль-2026-03-01"
    print("✅ пакет не плодит ключи: одна история на факт")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе тесты пакетной истории фактов прошли.")
