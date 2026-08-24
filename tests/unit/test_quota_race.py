# -*- coding: utf-8 -*-
"""Гонка в квоте закрыта: параллельный залп не пробивает лимит.

Проверяем ровно сценарий из разбора («состояние гонки»): много вызовов,
пришедших одновременно, проверяются раньше, чем хоть один записал расход.
Без резервации все они читают одно и то же «ещё не потрачено» и проходят —
лимит пробивается на величину залпа. С резервацией — нет.

ЧЕСТНАЯ ОГОВОРКА про стенд. Настоящий Redis тут не поднять (в окружении нет
ни redis, ни fakeredis). Поэтому Lua-скрипт из quota_reservation портирован
один-в-один в eval() фейка. Это законно ровно по одной причине: гарантия
Redis — что Lua выполняется ЦЕЛИКОМ, не чередуясь с другими клиентами.
Однопоточный asyncio даёт то же самое: наш eval() не содержит await внутри,
значит между двумя параллельными корутинами он не прервётся на середине.
То есть фейк воспроизводит именно то свойство, на котором держится защита.
Проверяется алгоритм; продовый путь — тот же алгоритм на настоящем Lua.

Контроль: тот же залп со старой логикой (только записанный расход) — и
видно, что он проходит целиком.

Запуск:  python tests/unit/test_quota_race.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class FakeRedisAtomicZSet:
    """ZSET-подмножество + eval, повторяющий _LUA_RESERVE.

    eval() — обычная синхронная функция без await: на однопоточном asyncio
    она не прерывается, как и Lua на Redis. Именно это делает проверку
    осмысленной.
    """

    def __init__(self) -> None:
        self.z: dict[str, dict[str, float]] = {}

    async def eval(self, script, numkeys, key, now, expiry, est, budget, member):
        z = self.z.setdefault(key, {})
        # ZREMRANGEBYSCORE key -inf now  (выкинуть протухшее)
        for m in [m for m, sc in z.items() if sc <= now]:
            del z[m]
        # сложить живое
        inflight = 0
        for m in z:
            mo = re.search(r":(-?\d+)$", m)
            if mo:
                inflight += int(mo.group(1))
        if inflight + int(est) > int(budget):
            return [0, inflight]
        z[member] = float(expiry)
        return [1, inflight]

    async def zrem(self, key, member):
        self.z.get(key, {}).pop(member, None)


def _load_quota_reservation(fake):
    for pkg in ("backend", "backend.core", "backend.core.llm", "backend.db"):
        if pkg not in sys.modules:
            m = types.ModuleType(pkg)
            m.__path__ = []
            sys.modules[pkg] = m

    # redis_client.get_redis → обёртка вокруг фейка
    rc = types.ModuleType("backend.db.redis_client")

    class _R:
        client = fake

        async def health_check(self):
            return True

    async def get_redis():
        return _R()

    rc.get_redis = get_redis
    sys.modules["backend.db.redis_client"] = rc

    spec = importlib.util.spec_from_file_location(
        "backend.core.llm.quota_reservation",
        os.path.join(ROOT, "backend/core/llm/quota_reservation.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backend.core.llm.quota_reservation"] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    failures: list[str] = []

    def check(name, cond, detail=""):
        print(("  ok   " if cond else "  ПЛОХО ") + name + ("" if cond else f" — {detail}"))
        if not cond:
            failures.append(name)

    os.environ["LLM_QUOTA_ATOMIC"] = "on"
    os.environ["LLM_QUOTA_RESERVE_TOKENS"] = "1000"
    fake = FakeRedisAtomicZSet()
    qr = _load_quota_reservation(fake)

    async def run():
        # лимит 10000, уже записано 6000 → бюджет 4000, оценка 1000 →
        # поместиться должны ровно 4 из залпа.
        LIMIT, USED, EST = 10000.0, 6000.0, 1000

        print("параллельный залп у границы бюджета:")

        # asyncio.gather создаёт задачи, каждая копирует текущий контекст —
        # то есть у каждой свой список резервов, как у отдельного запроса.
        async def one():
            return await qr.reserve(
                scope="user", subject_id="u1",
                metric="total_tokens", window="day",
                limit=LIMIT, used=USED)

        results = await asyncio.gather(*[one() for _ in range(50)])
        admitted = sum(1 for a, _ in results if a)
        check("из 50 одновременных прошло ровно 4 (бюджет/оценка)",
              admitted == 4, f"прошло {admitted}")

        # ещё один залп — места уже нет (резервы держатся)
        more = await asyncio.gather(*[one() for _ in range(10)])
        check("повторный залп — 0 (резервы держат границу)",
              sum(1 for a, _ in more if a) == 0, str(more))

        print("после снятия резервов место возвращается:")
        # снимаем все резервы (эмулируем запись расхода всех прошедших)
        fake.z.clear()
        ok, _ = await qr.reserve(scope="user", subject_id="u1",
                                 metric="total_tokens", window="day",
                                 limit=LIMIT, used=USED)
        check("после очистки резерв снова проходит", ok)

        print("протухшие резервы выкидываются сами (самозалечивание):")
        # Оставляем один резерв, у которого срок истёк: следующий вызов
        # обязан его выкинуть и найти место. Так утечка резерва (процесс упал
        # между ответом и записью) рассасывается без ручного вмешательства.
        fake.z.clear()
        key = qr._key("user", "u1", "total_tokens", "day")
        fake.z[key] = {f"protuhshiy:{int(LIMIT - USED)}": 1.0}  # score в прошлом
        ok, inflight = await qr.reserve(scope="user", subject_id="u1",
                                        metric="total_tokens", window="day",
                                        limit=LIMIT, used=USED)
        check("протухший резерв не занимает место", ok and inflight == 0,
              f"ok={ok} inflight={inflight}")

        print("fail-open без Redis:")

        async def _no_redis():
            return None

        real_client = qr._redis_client
        qr._redis_client = _no_redis
        oks = await asyncio.gather(*[qr.reserve(
            scope="user", subject_id="u2", metric="total_tokens",
            window="day", limit=LIMIT, used=USED) for _ in range(50)])
        check("нет Redis → пропускаем все (как было раньше)",
              all(a for a, _ in oks), "должны пройти все")
        qr._redis_client = real_client

        # --- КОНТРОЛЬ: тот же залп при ВЫКЛЮЧЕННОЙ резервации ----------
        # Это и есть прежнее поведение: решение принимается только по
        # записанному расходу, который у всех одинаков. Если тут пройдут не
        # все 50 — значит проверка выше ничего не доказывает.
        print("контроль — тот же залп с выключенной резервацией:")
        os.environ["LLM_QUOTA_ATOMIC"] = "off"
        fake.z.clear()
        old = await asyncio.gather(*[one() for _ in range(50)])
        old_admitted = sum(1 for a, _ in old if a)
        check("без резервации проходит весь залп (дыра реальна)",
              old_admitted == 50, f"прошло {old_admitted}")
        check("выключатель действительно выключает", old_admitted == 50)
        os.environ["LLM_QUOTA_ATOMIC"] = "on"

    asyncio.run(run())

    print()
    if failures:
        print(f"ПРОВАЛЕНО: {len(failures)} — {', '.join(failures)}")
        return 1
    print("всё сошлось")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
