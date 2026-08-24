# -*- coding: utf-8 -*-
"""Long-polling бота: апдейты доходят до общего процессора, offset растёт,
падение одного апдейта не роняет остальные, флаг fail-closed."""
import asyncio
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.core.messengers import tg_polling as tp  # noqa: E402


class _Resp:
    def __init__(self, payload):
        self.status_code = 200
        self._p = payload

    def json(self):
        return self._p


class _Client:
    def __init__(self, updates):
        self._updates = updates
        self.calls = 0

    async def get(self, url, params=None):
        self.calls += 1
        assert "getUpdates" in url
        assert params["offset"] == 0 or params["offset"] > 0
        return _Resp({"ok": True, "result": self._updates})


def test_poll_once_processes_and_advances_offset():
    updates = [
        {"update_id": 10, "message": {"chat": {"id": "1"}, "text": "a"}},
        {"update_id": 11, "message": {"chat": {"id": "2"}, "text": "b"}},
    ]
    seen = []

    async def _process(upd):
        seen.append(upd["update_id"])
        if upd["update_id"] == 10:
            raise RuntimeError("одно сообщение битое")

    offset = asyncio.run(tp.poll_once(_Client(updates), "tok", 0, _process))
    assert offset == 12, "offset = последний update_id + 1"
    assert seen == [10, 11], "падение одного апдейта не роняет остальные"


def test_poll_once_raises_on_api_error():
    class _Bad:
        async def get(self, url, params=None):
            return _Resp({"ok": False, "description": "Unauthorized"})

    try:
        asyncio.run(tp.poll_once(_Bad(), "tok", 0, lambda u: None))
        assert False, "должен был подняться RuntimeError"
    except RuntimeError as e:
        assert "Unauthorized" in str(e)


def test_polling_flag(monkeypatch):
    monkeypatch.delenv("TELEGRAM_POLLING", raising=False)
    assert tp.polling_enabled() is False
    monkeypatch.setenv("TELEGRAM_POLLING", "on")
    assert tp.polling_enabled() is True
