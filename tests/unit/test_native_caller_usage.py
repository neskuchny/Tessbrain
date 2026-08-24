# -*- coding: utf-8 -*-
"""Учёт расхода нативных вызывателей (native_callers → usage_tracker).

Инцидент: total_in/total_out честно копились из ответов API, но их НИКТО не
читал — ни тировый путь (extraction_route/workload_policy), ни доска
(/boards/llm-generate). Расход не попадал в usage_tracker, значит квоты
недосчитывали трафик и срезы по деньгам врали.

Флаш стоит в _Base.aclose() — единой точке, которую зовут ВСЕ потребители
(в т.ч. в finally), поэтому учёт включается везде без правок в местах вызова.
"""
import asyncio
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.core.llm import native_callers as NC  # noqa: E402
from backend.core.llm import usage_tracker as UT  # noqa: E402


@pytest.fixture()
def captured(monkeypatch):
    """Перехватываем track_usage вместе с окружающим usage_context —
    так проверяем и цифры, и атрибуцию (user_id из cost_scope)."""
    calls = []

    def _fake(provider, model, input_tokens, output_tokens, **kw):
        ctx = UT.get_usage_context() or {}
        calls.append({"provider": provider, "model": model,
                      "in": input_tokens, "out": output_tokens,
                      "user_id": kw.get("user_id") or ctx.get("user_id"),
                      "surface": kw.get("surface") or ctx.get("surface")})
        return None

    monkeypatch.setattr(UT, "track_usage", _fake)
    return calls


def _caller(provider="deepseek", model="deepseek-v4-pro"):
    c = NC.make_caller(provider, model, "k")
    assert c is not None
    return c


def test_provider_is_stamped_by_factory():
    """Без имени провайдера расход не отнести — фабрика обязана его проставить."""
    for p in ("anthropic", "google", "openai", "xai", "deepseek", "qwen", "moonshot"):
        c = NC.make_caller(p, "m", "k")
        assert c is not None and c.provider == p, p


def test_aclose_flushes_tokens(captured):
    c = _caller()
    c.total_in, c.total_out = 1200, 340
    asyncio.run(c.aclose())
    assert len(captured) == 1
    rec = captured[0]
    assert rec["provider"] == "deepseek" and rec["model"] == "deepseek-v4-pro"
    assert rec["in"] == 1200 and rec["out"] == 340


def test_no_double_count_on_repeated_close(captured):
    """aclose() зовут и явно, и в finally — расход не должен удваиваться."""
    c = _caller()
    c.total_in, c.total_out = 10, 20
    asyncio.run(c.aclose())
    asyncio.run(c.aclose())
    c.flush_usage()
    assert len(captured) == 1


def test_empty_run_writes_nothing(captured):
    """Ошибка/пустой прогон → нулевые токены → в учёт не пишем."""
    c = _caller()
    asyncio.run(c.aclose())
    assert captured == []


def test_attribution_from_cost_scope(captured):
    """user_id/surface подхватываются из окружающего cost_scope — именно так
    расход доски и тирового пути привязывается к пользователю (квоты)."""
    c = _caller()
    c.total_in, c.total_out = 7, 9

    async def _run():
        async with UT.cost_scope(user_id="u-42", surface="board_creative",
                                 agent_mode="board"):
            await c.aclose()

    asyncio.run(_run())
    assert len(captured) == 1
    assert captured[0]["user_id"] == "u-42"
    assert captured[0]["surface"] == "board_creative"


def test_workload_path_attributes_spend_to_user(captured, monkeypatch):
    """generate_for_workload знает user_id — расход тирового пути (чат/поиск/
    ночь) должен привязываться к нему, иначе квоты этот трафик не видят."""
    from backend.core.llm import workload_policy as WP

    caller = _caller("xai", "grok-4.5")

    async def _gen(prompt):
        caller.total_in, caller.total_out = 100, 50
        return "ответ"

    caller.generate = _gen

    async def _resolve(uid, workload):
        return {"caller": caller, "native": "xai", "level": "premium"}

    monkeypatch.setattr(WP, "resolve_for_workload", _resolve)

    out = asyncio.run(WP.generate_for_workload("u-77", "chat", "вопрос"))
    assert out == "ответ"
    assert len(captured) == 1, captured
    rec = captured[0]
    assert rec["user_id"] == "u-77"           # привязка есть
    assert rec["provider"] == "xai" and rec["in"] == 100 and rec["out"] == 50


def test_usage_failure_never_breaks_generation(monkeypatch):
    """Учёт не критичен: падение трекера не должно ронять вызыватель."""
    def _boom(*a, **k):
        raise RuntimeError("tracker down")

    monkeypatch.setattr(UT, "track_usage", _boom)
    c = _caller()
    c.total_in, c.total_out = 5, 5
    asyncio.run(c.aclose())      # не должно бросить
