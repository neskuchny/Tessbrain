# -*- coding: utf-8 -*-
"""Перебор пароля: адрес нельзя подделать заголовком, аккаунт придерживается.

Два разных механизма, оба нужны:

1. `_client_ip` — бакет общего rate-limit'а. Раньше брал ПЕРВУЮ запись
   X-Forwarded-For, а её пишет клиент. Caddy заголовок дописывает, не
   перезаписывает, поэтому подделанная строка оставалась первой. Меняешь
   её на каждом запросе — каждый запрос в новом бакете, лимит не работает.

2. `login_guard` — счётчик неудач на учётную запись. Нужен потому, что от
   смены адреса он не зависит: перебор пароля к одной почте упрётся в
   него, сколько бы адресов у атакующего ни было.

Redis тут не поднимается — вместо него крошечная подделка с тем же
поведением (incr/expire/ttl/get/delete). Проверяется наша логика, а не
Redis.

Запуск:  python tests/unit/test_login_bruteforce.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _pkgs(*names: str) -> None:
    for n in names:
        if n not in sys.modules:
            m = types.ModuleType(n)
            m.__path__ = []
            sys.modules[n] = m


class FakeRedis:
    """Мини-Redis: только то, что использует login_guard."""

    def __init__(self) -> None:
        self.store: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, k):
        return self.store.get(k)

    async def incr(self, k):
        self.store[k] = self.store.get(k, 0) + 1
        return self.store[k]

    async def expire(self, k, sec):
        self.ttls[k] = sec

    async def ttl(self, k):
        return self.ttls.get(k, -1)

    async def delete(self, k):
        self.store.pop(k, None)
        self.ttls.pop(k, None)


def scope(peer: str, xff: str | None = None):
    headers = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode("latin-1")))
    return {"type": "http", "headers": headers, "client": (peer, 51000)}


def main() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(("  ok   " if cond else "  ПЛОХО ") + name + ("" if cond else f" — {detail}"))
        if not cond:
            failures.append(name)

    # --- 1. адрес клиента ---------------------------------------------
    _pkgs("backend", "backend.api", "backend.api.middleware")
    rl = _load("backend.api.middleware.rate_limit",
               "backend/api/middleware/rate_limit.py")
    os.environ.pop("TRUSTED_PROXY_HOPS", None)

    print("за своим прокси (соединение из локальной сети):")
    check("честный запрос — адрес клиента",
          rl._client_ip(scope("172.18.0.2", "93.184.216.34")) == "93.184.216.34",
          rl._client_ip(scope("172.18.0.2", "93.184.216.34")))
    got = rl._client_ip(scope("172.18.0.2", "1.2.3.4, 93.184.216.34"))
    check("подделка слева не проходит — берём запись прокси",
          got == "93.184.216.34", got)
    got = rl._client_ip(scope("172.18.0.2", "a, b, c, d, 93.184.216.34"))
    check("сколько бы записей ни дописали слева", got == "93.184.216.34", got)

    print("напрямую из интернета (прокси нет):")
    got = rl._client_ip(scope("93.184.216.34", "1.2.3.4"))
    check("заголовку не верим вовсе — адрес соединения",
          got == "93.184.216.34", got)

    print("число прокси задано явно:")
    os.environ["TRUSTED_PROXY_HOPS"] = "2"
    got = rl._client_ip(scope("172.18.0.2", "1.1.1.1, 93.184.216.34, 10.0.0.5"))
    check("hops=2 → отступаем на две записи", got == "93.184.216.34", got)
    os.environ["TRUSTED_PROXY_HOPS"] = "0"
    got = rl._client_ip(scope("172.18.0.2", "1.2.3.4"))
    check("hops=0 → заголовок игнорируется", got == "172.18.0.2", got)
    os.environ.pop("TRUSTED_PROXY_HOPS")

    print("контроль — старая логика на тех же данных:")
    old = "1.2.3.4, 93.184.216.34".split(",")[0].strip()
    check("прежний код брал подделку", old == "1.2.3.4", old)

    # --- 2. счётчик неудач на аккаунт ---------------------------------
    _pkgs("backend.core", "backend.core.auth", "backend.db")
    os.environ["LOGIN_MAX_FAILURES"] = "5"
    os.environ["LOGIN_FAILURE_WINDOW_SEC"] = "900"
    guard = _load("backend.core.auth.login_guard", "backend/core/auth/login_guard.py")

    fake = FakeRedis()

    async def _fake_redis():
        return fake

    guard._redis = _fake_redis

    async def run() -> None:
        email = "жертва@example.com"
        print("счётчик неудач на учётную запись:")

        allowed, _ = await guard.check(email)
        check("сначала вход открыт", allowed)

        for i in range(5):
            await guard.record_failure(email)
        allowed, retry = await guard.check(email)
        check("после 5 неудач вход придержан", not allowed)
        check("сказано, через сколько повторить", retry > 0, str(retry))

        other = "другой@example.com"
        allowed, _ = await guard.check(other)
        check("другая учётка не задета", allowed)

        await guard.record_success(email)
        allowed, _ = await guard.check(email)
        check("успешный вход обнуляет счётчик", allowed)

        print("почта не хранится в открытом виде:")
        key = guard.account_key(email)
        check("ключ — отпечаток, не почта",
              "@" not in key and "жертва" not in key and len(key) == 32, key)
        check("одна и та же почта → один ключ",
              guard.account_key("ЖЕРТВА@Example.COM ") == key)

        print("Redis недоступен — вход не ломается:")

        async def _no_redis():
            return None

        guard._redis = _no_redis
        allowed, _ = await guard.check(email)
        check("пропускаем, а не отказываем", allowed)
        guard._redis = _fake_redis

        print("выключатель:")
        os.environ["LOGIN_MAX_FAILURES"] = "0"
        for _ in range(50):
            await guard.record_failure("кто-то@example.com")
        allowed, _ = await guard.check("кто-то@example.com")
        check("LOGIN_MAX_FAILURES=0 отключает проверку", allowed)
        os.environ["LOGIN_MAX_FAILURES"] = "5"

    asyncio.run(run())

    print()
    if failures:
        print(f"ПРОВАЛЕНО: {len(failures)} — {', '.join(failures)}")
        return 1
    print("всё сошлось")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
