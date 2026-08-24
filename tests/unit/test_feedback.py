"""Тесты обратной связи: стор, дайджест самоулучшения, нотификатор саппорта."""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def fbdir(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSENT_FEEDBACK_DIR", str(tmp_path))
    return tmp_path


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _no_supabase(monkeypatch):
    # Стор стал Supabase-primary (миграция 268); в тестах гоняем
    # детерминированный JSONL-fallback: _sb() падает → ветка fallback.
    from backend.core.help import feedback_store as fs
    def _boom():
        raise RuntimeError("no supabase in tests")
    monkeypatch.setattr(fs, "_sb", _boom)


# === feedback_store =======================================================

def test_add_and_list(fbdir) -> None:
    from backend.core.help import feedback_store as fs
    r1 = _run(fs.add_feedback(user_id="u1", kind="problem",
                         message="не грузится таблица", area=["knowledge/datasets"]))
    _run(fs.add_feedback(user_id="u2", kind="wish", message="хочу дайджест"))
    assert r1["id"] and r1["kind"] == "problem"
    items = _run(fs.list_feedback(since_days=7))
    assert len(items) == 2
    assert items[0]["message"] == "не грузится таблица"


def test_kind_normalized(fbdir) -> None:
    from backend.core.help import feedback_store as fs
    r = _run(fs.add_feedback(user_id="u", kind="garbage", message="x" * 20))
    assert r["kind"] == "problem"        # невалидный вид → problem


def test_stats(fbdir) -> None:
    from backend.core.help import feedback_store as fs
    _run(fs.add_feedback(user_id="u", kind="problem", message="a"))
    _run(fs.add_feedback(user_id="u", kind="problem", message="b"))
    _run(fs.add_feedback(user_id="u", kind="wish", message="c"))
    st = fs.stats()
    assert st["total"] == 3 and st["problem"] == 2 and st["wish"] == 1


def test_list_empty(fbdir) -> None:
    from backend.core.help import feedback_store as fs
    assert _run(fs.list_feedback(since_days=7)) == []


def test_status_open_by_default_and_resolve(fbdir) -> None:
    from backend.core.help import feedback_store as fs
    r = _run(fs.add_feedback(user_id="u1", kind="problem", message="кнопка не работает"))
    assert r["status"] == "open"
    upd = _run(fs.set_status(r["id"], "resolved", resolution="починили в 1.2.3"))
    assert upd and upd["status"] == "resolved" and "починили" in upd["resolution"]
    assert upd.get("resolved_at")
    # get_feedback видит обновление, list сохраняет остальные поля
    got = _run(fs.get_feedback(r["id"]))
    assert got["status"] == "resolved" and got["message"] == "кнопка не работает"


def test_set_status_unknown_id(fbdir) -> None:
    from backend.core.help import feedback_store as fs
    _run(fs.add_feedback(user_id="u", kind="problem", message="x"))
    assert _run(fs.set_status("nope", "resolved")) is None


# === feedback_digest ======================================================

def test_digest_no_items(fbdir) -> None:
    from backend.core.help.feedback_digest import build_digest
    d = _run(build_digest(days=7))
    assert d["items_count"] == 0 and "нет" in d["summary"]


def test_digest_aggregates_and_fallback(fbdir, monkeypatch) -> None:
    import backend.core.llm.router as r
    from backend.core.help import feedback_store as fs
    from backend.core.help.feedback_digest import build_digest

    class FakeRouter:
        async def generate(self, **k):
            raise RuntimeError("no llm in test")

    monkeypatch.setattr(r, "get_llm_router", lambda: FakeRouter())
    monkeypatch.setattr(r, "set_llm_context", lambda **k: None)

    _run(fs.add_feedback(user_id="u1", kind="problem", message="датасеты не грузятся",
                    area=["knowledge/datasets"]))
    _run(fs.add_feedback(user_id="u2", kind="problem", message="ещё про датасеты",
                    area=["knowledge/datasets"]))
    _run(fs.add_feedback(user_id="u3", kind="wish", message="хочу дайджест",
                    area=["knowledge/customers"]))
    d = _run(build_digest(days=7))
    assert d["items_count"] == 3
    assert d["counts"] == {"problem": 2, "wish": 1}
    assert d["top_areas"][0]["area"] == "knowledge/datasets"
    assert d["top_areas"][0]["count"] == 2
    assert "LLM недоступен" in d["summary"]     # честный фолбэк без сети


# === support_notify =======================================================

def test_support_not_configured(monkeypatch) -> None:
    from backend.core.help import support_notify as sn
    monkeypatch.delenv("TESSENT_SUPPORT_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert sn.is_configured() is False
    # не настроено → send возвращает False, не raises
    assert _run(sn.send_to_support("тест")) is False
