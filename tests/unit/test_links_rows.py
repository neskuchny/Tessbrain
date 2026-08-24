# -*- coding: utf-8 -*-
"""Регрессия «This result object is closed»: пустой результат lookup — это
«привязки нет» (None), а не повторное чтение закрытого result."""
import asyncio
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.core.messengers.links import _first_row  # noqa: E402


class _ClosedAfterFirst:
    """Повторяет поведение SQLAlchemy: .first() закрывает result."""
    def __init__(self, row):
        self._row = row
        self._closed = False

    def mappings(self):
        return self

    def first(self):
        if self._closed:
            raise RuntimeError("This result object is closed.")
        self._closed = True
        return self._row


def test_empty_result_returns_none_without_touching_closed_result():
    out = asyncio.run(_first_row(_ClosedAfterFirst(None)))
    assert out is None, "пустой результат = None, без исключений"


def test_found_row_is_returned():
    out = asyncio.run(_first_row(_ClosedAfterFirst({"id": "l1",
                                                    "user_id": "u1"})))
    assert out == {"id": "l1", "user_id": "u1"}
