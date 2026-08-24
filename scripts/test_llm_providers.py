# -*- coding: utf-8 -*-
"""Живая проверка LLM-провайдеров и маршрутизации: «работает ли deepseek?».

Что делает:
  1. Показывает, какие ключи провайдеров найдены в env/.env.
  2. Для каждого провайдера с ключом бьёт НАТИВНЫМ API (те же вызыватели,
     что использует продукт) крошечным промптом и печатает ok/fail + латентность.
  3. Печатает карту маршрутизации: какой ворклоад → какой уровень →
     какая модель платформы реально ответит с ТЕКУЩИМИ ключами.

Запуск (из tessent_brain, с активным .venv):
  python scripts/test_llm_providers.py            # все провайдеры с ключами
  python scripts/test_llm_providers.py deepseek   # только один провайдер
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from backend.core.llm.model_router import (  # noqa: E402
    NATIVE_ENDPOINT, ROUTING,
    native_model_id, native_provider_for, platform_chain,
)
from backend.core.llm.native_callers import make_caller  # noqa: E402

PROMPT = "Ответь ровно одним словом: работаю"


def _key_for(provider: str) -> str:
    env = (NATIVE_ENDPOINT.get(provider) or {}).get("env", "")
    return os.environ.get(env, "") if env else ""


async def _probe(provider: str) -> None:
    key = _key_for(provider)
    env_name = (NATIVE_ENDPOINT.get(provider) or {}).get("env", "?")
    if not key:
        print(f"  {provider:<10} — ключа нет ({env_name} пуст) — пропуск")
        return
    model_name = ROUTING.get(provider, {}).get("standard", "")
    mid = native_model_id(model_name)
    if not mid:
        print(f"  {provider:<10} — нет нативного id для «{model_name}» "
              "(для anthropic задайте ANTHROPIC_STANDARD_MODEL)")
        return
    caller = make_caller(provider, mid, key)
    if caller is None:
        print(f"  {provider:<10} — нативный вызыватель не поддержан")
        return
    t0 = time.monotonic()
    try:
        text = await caller.generate(PROMPT)
        dt = time.monotonic() - t0
        snippet = (text or "").strip().replace("\n", " ")[:60]
        print(f"  {provider:<10} ✅ {mid:<24} {dt:5.1f}с  → «{snippet}» "
              f"(in={caller.total_in}, out={caller.total_out})")
    except Exception as e:
        dt = time.monotonic() - t0
        print(f"  {provider:<10} ❌ {mid:<24} {dt:5.1f}с  {str(e)[:180]}")
    finally:
        try:
            await caller.aclose()
        except Exception:
            pass


def _platform_pick(level: str) -> str:
    """Какая модель платформы реально ответит на этом уровне с текущими ключами."""
    for model_name in platform_chain(level):
        prov = native_provider_for(model_name)
        if prov and _key_for(prov):
            return f"{model_name} ({prov})"
    return "— нет ни одного ключа —"


async def main() -> int:
    only = sys.argv[1].strip().lower() if len(sys.argv) > 1 else ""

    print("═" * 72)
    print("КЛЮЧИ ПРОВАЙДЕРОВ")
    for prov, cfg in NATIVE_ENDPOINT.items():
        mark = "есть" if _key_for(prov) else "НЕТ "
        print(f"  {prov:<10} {cfg['env']:<22} {mark}")

    print()
    print("ЖИВАЯ ПРОВЕРКА (нативные API, модель уровня standard провайдера)")
    providers = [only] if only else list(NATIVE_ENDPOINT.keys())
    for prov in providers:
        await _probe(prov)

    print()
    print("МАРШРУТИЗАЦИЯ ПЛАТФОРМЫ С ТЕКУЩИМИ КЛЮЧАМИ")
    for level in ("fast", "standard", "premium"):
        print(f"  уровень {level:<8} → {_platform_pick(level)}")

    try:
        from backend.core.llm.workload_policy import WORKLOAD_LEVEL, level_for
        print()
        print("ВОРКЛОАДЫ (что каким уровнем отвечает)")
        for wl in sorted(WORKLOAD_LEVEL):
            lv = level_for(wl)
            print(f"  {wl:<24} → {lv:<8} → {_platform_pick(lv)}")
    except Exception as e:
        print(f"  (карта ворклоадов недоступна: {e})")

    print("═" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
