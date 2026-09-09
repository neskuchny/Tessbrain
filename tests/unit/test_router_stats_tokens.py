# -*- coding: utf-8 -*-
"""Агрегат LLMRouter.get_stats()["total"] несёт токены и числовую стоимость.

Что здесь закрепляется (docs/ru/REVIEW_TZ_FIX_SEARCH_DEFECTS.md, задача 4).
В ТЗ было сказано, что «LLMRouter не заполняет счётчики токенов». Это
неверно: клиенты их заполняют (openai_client.py:132-137 и аналоги).
Настоящий дефект был уже: в агрегат `total` токены не поднимались — там
лежали только requests/errors/cost, — и читавший `total` делал вывод, что
учёта нет вовсе. Плюс `estimated_cost` подменялся форматированной строкой
"$0.000000", по которой нельзя ни сложить, ни сравнить.

Контракты:
  1. токены попадают в `total` и суммируются по провайдерам;
  2. `estimated_cost` — ЧИСЛО, а показ живёт отдельным ключом;
  3. считаем с СЫРЫХ счётчиков клиента, а не разбором строки — точность
     не теряется на форматировании;
  4. клиент без `.stats` (duck-typed) не роняет сводку, читается запасным
     путём из get_stats();
  5. битая строка стоимости у одного провайдера не роняет остальных.

Запуск: python tests/unit/test_router_stats_tokens.py
"""
from __future__ import annotations

import os
import sys

try:                                     # pragma: no cover - зависит от среды
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from backend.core.llm.router import LLMRouter, LLMProvider  # noqa: E402


class ClientWithRawStats:
    """Как настоящие клиенты: числа в `.stats`, формат — в get_stats()."""

    def __init__(self, inp=1000, out=200, cost=0.00123):
        self.stats = {
            "total_requests": 3, "cached_requests": 0,
            "total_input_tokens": inp, "cached_input_tokens": 0,
            "output_tokens": out, "estimated_cost": cost,
            "estimated_cost_without_cache": cost, "errors": 1,
        }

    def get_stats(self):
        s = self.stats
        return {
            "requests": {"total": s["total_requests"], "errors": s["errors"]},
            "tokens": {"input": s["total_input_tokens"],
                       "cached": s["cached_input_tokens"],
                       "output": s["output_tokens"],
                       "total": s["total_input_tokens"] + s["output_tokens"]},
            "cost": {"actual": f"${s['estimated_cost']:.6f}",
                     "without_cache": f"${s['estimated_cost']:.6f}"},
        }


class DuckTypedClient:
    """Без `.stats` — только форматированный get_stats()."""

    def get_stats(self):
        return {
            "requests": {"total": 2, "errors": 0},
            "tokens": {"input": 50, "cached": 0, "output": 10, "total": 60},
            "cost": {"actual": "$0.000500"},
        }


class BrokenCostClient(DuckTypedClient):
    def get_stats(self):
        d = super().get_stats()
        d["cost"]["actual"] = "не число"
        return d


def _router(clients: dict) -> LLMRouter:
    r = LLMRouter(default_provider=LLMProvider.OPENAI, enable_fallback=False)
    r._clients = clients
    return r


# ── 1-3. Сырые счётчики, токены в total, числовая стоимость ────────────

def test_tokens_and_numeric_cost_in_total():
    r = _router({LLMProvider.OPENAI: ClientWithRawStats(inp=1000, out=200,
                                                        cost=0.00123)})
    t = r.get_stats()["total"]
    assert t["input_tokens"] == 1000, t
    assert t["output_tokens"] == 200, t
    assert t["total_tokens"] == 1200, t
    assert isinstance(t["estimated_cost"], float), (
        f"стоимость обязана быть числом, получено {type(t['estimated_cost'])}")
    assert abs(t["estimated_cost"] - 0.00123) < 1e-9, t["estimated_cost"]
    assert t["estimated_cost_display"] == "$0.001230", t
    print("✅ токены в агрегате, стоимость числом, показ отдельным ключом")


def test_precision_not_lost_to_formatting():
    """Сырое значение мельче шести знаков формата не должно обнуляться."""
    r = _router({LLMProvider.OPENAI: ClientWithRawStats(cost=1.2e-7)})
    t = r.get_stats()["total"]
    assert t["estimated_cost"] > 0, (
        "стоимость обнулилась — значит её всё ещё берут из строки, "
        f"а не из сырого счётчика: {t['estimated_cost']}")
    print("✅ точность не теряется на форматировании")


# ── 4. Суммирование по провайдерам ────────────────────────────────────

def test_sums_across_providers():
    r = _router({
        LLMProvider.OPENAI: ClientWithRawStats(inp=100, out=10, cost=0.001),
        LLMProvider.GEMINI: ClientWithRawStats(inp=300, out=30, cost=0.002),
    })
    t = r.get_stats()["total"]
    assert t["input_tokens"] == 400, t
    assert t["output_tokens"] == 40, t
    assert abs(t["estimated_cost"] - 0.003) < 1e-9, t
    assert t["requests"] == 6 and t["errors"] == 2, t
    print("✅ токены, стоимость и запросы суммируются по провайдерам")


# ── 5. Duck-typed клиент читается запасным путём ──────────────────────

def test_duck_typed_client_without_raw_stats():
    r = _router({LLMProvider.OPENAI: DuckTypedClient()})
    t = r.get_stats()["total"]
    assert t["input_tokens"] == 50 and t["output_tokens"] == 10, t
    assert abs(t["estimated_cost"] - 0.0005) < 1e-9, t
    print("✅ клиент без .stats читается запасным путём")


# ── 6. Битая стоимость не роняет сводку ───────────────────────────────

def test_broken_cost_does_not_break_summary():
    r = _router({LLMProvider.OPENAI: BrokenCostClient(),
                 LLMProvider.GEMINI: ClientWithRawStats(cost=0.005)})
    t = r.get_stats()["total"]
    assert abs(t["estimated_cost"] - 0.005) < 1e-9, (
        f"сбойный провайдер испортил сводку: {t['estimated_cost']}")
    print("✅ битая стоимость одного провайдера не роняет остальных")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе контракты агрегата статистики роутера прошли.")
