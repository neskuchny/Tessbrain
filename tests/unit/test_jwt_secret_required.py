# -*- coding: utf-8 -*-
"""В production нельзя стартовать без ключа для проверки подписи токенов.

ПОЧЕМУ ЭТО ВАЖНО. Строгий режим аутентификации выключает сам себя, если
проверять подпись нечем (иначе он отверг бы все токены и уронил UI).
Решение разумное, но тихое: деплой без секрета молча уезжает в
compat-режим, где `sub` из НЕПОДПИСАННОГО токена принимается за личность.
Безопасность отключается сама, а снаружи всё работает — только warning в
логах, которого никто не читает.

Проверка переносит это решение к старту: в production процесс не
поднимается. Значит «работаем в проде без проверки подписи» становится
невозможным, а не маловероятным.

Валидатор берётся из живого config.py вырезанием — поднимать весь Settings
здесь нечем (нет pydantic и половины зависимостей). Проверяется ровно та
логика, что стоит в проде.

Запуск:  python tests/unit/test_jwt_secret_required.py
"""
from __future__ import annotations

import io
import os
import re
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG = os.path.join(ROOT, "backend", "config.py")
SERVICE_TOKEN = os.path.join(ROOT, "backend", "core", "auth", "service_token.py")

PLACEHOLDER = "your-jwt-secret-key"

# Кандидаты, которые ДОЛЖНЫ совпадать в обоих местах. Разъедутся — проверка
# в config станет враньём (пропустит старт, а верификация всё равно не
# заработает, либо наоборот).
_ENV_RE = re.compile(r'"JWT_SECRET",\s*"SUPABASE_JWT_SECRET",\s*"SECRET_KEY",\s*\n?\s*"JWT_SECRET_KEY",\s*"LEGACY_JWT_SECRET"')


def load_validator():
    """Вырезать тело валидатора и собрать из него вызываемую функцию."""
    src = io.open(CONFIG, encoding="utf-8").read()
    start = src.index("    def _require_verifiable_jwt_secret_in_production")
    body = src[start:]
    # обрезаем по следующему декоратору/методу того же уровня
    nxt = body.index("\n    @model_validator", 10)
    body = body[:nxt]
    # снимаем один уровень отступа и делаем свободной функцией
    lines = [ln[4:] if ln.startswith("    ") else ln for ln in body.split("\n")]
    fn_src = "\n".join(lines)
    # валидатор ссылается на _INSECURE_DEFAULTS из модуля — берём НАСТОЯЩИЙ
    # набор из config.py, чтобы проверять то же, что и прод.
    m = re.search(r"_INSECURE_DEFAULTS[^=]*=\s*frozenset\((\{.*?\})\)", src, re.S)
    ns: dict = {"_INSECURE_DEFAULTS": frozenset(eval(m.group(1)))}
    exec(fn_src, ns)
    return ns["_require_verifiable_jwt_secret_in_production"]


class FakeSettings:
    """Достаточно того, к чему обращается валидатор."""

    def __init__(self, app_env: str, jwt_secret_key: str = PLACEHOLDER):
        self.app_env = app_env
        self.jwt_secret_key = jwt_secret_key


ENV_NAMES = ("JWT_SECRET", "SUPABASE_JWT_SECRET", "SECRET_KEY",
             "JWT_SECRET_KEY", "LEGACY_JWT_SECRET")


def clear_env():
    for n in ENV_NAMES:
        os.environ.pop(n, None)


def main() -> int:
    validate = load_validator()
    failures: list[str] = []

    def check(name, cond, detail=""):
        print(("  ok   " if cond else "  ПЛОХО ") + name + ("" if cond else f" — {detail}"))
        if not cond:
            failures.append(name)

    def boots(settings) -> tuple[bool, str]:
        try:
            validate(settings)
            return True, ""
        except ValueError as e:
            return False, str(e)

    print("production без секрета — старт запрещён:")
    clear_env()
    ok, msg = boots(FakeSettings("production"))
    check("не стартует", not ok, "стартовал!")
    check("в ошибке сказано, что задать",
          "SUPABASE_JWT_SECRET" in msg and "JWT_SECRET" in msg, msg[:90])
    ok, _ = boots(FakeSettings("production", jwt_secret_key=""))
    check("пустая строка не считается секретом", not ok)
    ok, _ = boots(FakeSettings("production", jwt_secret_key="   "))
    check("пробелы не считаются секретом", not ok)
    ok, _ = boots(FakeSettings("production", jwt_secret_key=PLACEHOLDER))
    check("плейсхолдер из дефолтов не считается секретом", not ok)

    for name in ENV_NAMES:
        clear_env()
        os.environ[name] = PLACEHOLDER   # как если скопировали env.example
        ok, _ = boots(FakeSettings("production"))
        check(f"плейсхолдер в {name} не считается секретом", not ok)
    clear_env()

    print("production с секретом — стартует:")
    ok, _ = boots(FakeSettings("production", jwt_secret_key="настоящий-секрет"))
    check("секрет в поле настроек", ok)
    for env_name in ENV_NAMES:
        clear_env()
        os.environ[env_name] = "секрет-из-окружения"
        ok, _ = boots(FakeSettings("production"))
        check(f"секрет в {env_name}", ok)
    clear_env()

    print("dev и staging не трогаем:")
    for env in ("development", "staging"):
        ok, _ = boots(FakeSettings(env))
        check(f"{env} стартует без секрета (прежнее поведение)", ok)

    print("списки кандидатов в config и service_token не разъехались:")
    cfg = io.open(CONFIG, encoding="utf-8").read()
    st = io.open(SERVICE_TOKEN, encoding="utf-8").read()
    check("одинаковый набор env-имён в обоих файлах",
          bool(_ENV_RE.search(cfg)) and bool(_ENV_RE.search(st)),
          f"config={bool(_ENV_RE.search(cfg))} service_token={bool(_ENV_RE.search(st))}")
    check("одинаковый плейсхолдер",
          PLACEHOLDER in cfg and PLACEHOLDER in st)

    print()
    if failures:
        print(f"ПРОВАЛЕНО: {len(failures)} — {', '.join(failures)}")
        return 1
    print("всё сошлось")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
