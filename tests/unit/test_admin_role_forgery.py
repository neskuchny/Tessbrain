# -*- coding: utf-8 -*-
"""Подделка роли в JWT не должна открывать админские ручки.

Проверяем ровно тот сценарий, который был реально возможен: атакующий
собирает токен руками — три base64-строки через точку, подпись любая — и
кладёт туда `"role": "owner"`. До правки `admin_audit._require_admin`
декодировал такой токен с verify_signature=False и признавал роль. OWNER
открывает `?tenant_id=` — журнал аудита чужого тенанта.

Тест не поднимает сервер и не ходит в БД: проверяется решающая функция —
та, что говорит «роль такая-то». Запуск без pytest, чтобы работало в этом
окружении:  python tests/unit/test_admin_role_forgery.py
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SECRET = "test-secret-для-проверки-подписи"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def make_token(payload: dict, secret: str | None) -> str:
    """JWT HS256. secret=None → подпись мусорная (как у атакующего)."""
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps(payload).encode())
    signing_input = f"{header}.{body}".encode("ascii")
    if secret is None:
        sig = b"\x00" * 32  # атакующий подписать не может — кладёт что попало
    else:
        sig = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return f"{header}.{body}.{_b64(sig)}"


def _load():
    """Импорт admin_guard с минимальными заглушками вместо тяжёлых зависимостей."""
    os.environ["JWT_SECRET"] = SECRET

    # litestar.exceptions.HTTPException — единственное, что нужно от litestar
    if "litestar" not in sys.modules:
        litestar = types.ModuleType("litestar")
        exc_mod = types.ModuleType("litestar.exceptions")

        class HTTPException(Exception):
            def __init__(self, status_code: int = 500, detail: str = ""):
                self.status_code = status_code
                self.detail = detail
                super().__init__(f"{status_code}: {detail}")

        exc_mod.HTTPException = HTTPException
        litestar.exceptions = exc_mod
        sys.modules["litestar"] = litestar
        sys.modules["litestar.exceptions"] = exc_mod

    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)

    def load(name: str, relpath: str):
        spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    for pkg in ("backend", "backend.core", "backend.core.auth"):
        if pkg not in sys.modules:
            m = types.ModuleType(pkg)
            m.__path__ = []
            sys.modules[pkg] = m

    # config / feature_flags: strict-режим выключен — самый опасный случай,
    # именно в нём compat-путь возвращал непроверенные claims.
    cfg = types.ModuleType("backend.config")

    class _S:
        jwt_secret_key = SECRET
        service_jwt_secret = ""
        service_jwt_audience = "tessent-brain"
        service_jwt_issuer = "meetflow"

    cfg.settings = _S()
    sys.modules["backend.config"] = cfg

    ff = types.ModuleType("backend.core.config.feature_flags")

    class _F:
        enable_strict_chat_auth = False
        enable_service_auth = False

    ff.get_feature_flags = lambda: _F()
    cfgpkg = types.ModuleType("backend.core.config")
    cfgpkg.__path__ = []
    sys.modules["backend.core.config"] = cfgpkg
    sys.modules["backend.core.config.feature_flags"] = ff

    load("backend.core.auth.rbac", "backend/core/auth/rbac.py")
    load("backend.core.auth.service_token", "backend/core/auth/service_token.py")
    guard = load("backend.core.auth.admin_guard", "backend/core/auth/admin_guard.py")
    rbac = sys.modules["backend.core.auth.rbac"]
    return guard, rbac


def main() -> int:
    guard, rbac = _load()
    Role = rbac.Role
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(("  ok   " if cond else "  ПЛОХО ") + name + ("" if cond else f" — {detail}"))
        if not cond:
            failures.append(name)

    print("подделанная роль (подпись неверна):")

    forged = make_token({"sub": "attacker", "role": "owner"}, secret=None)
    uid, role = guard.caller_identity(f"Bearer {forged}")
    check("owner из неподписанного токена не признаётся",
          role == Role.NONE, f"получили {role!r}")
    check("личность при этом всё же извлекается (compat)", uid == "attacker", uid)

    try:
        guard.require_admin(f"Bearer {forged}")
        check("require_admin отказывает подделке", False, "пропустил!")
    except rbac.PermissionDenied:
        check("require_admin отказывает подделке", True)

    # тот же трюк с role: admin и с вложенным app_metadata (Supabase-стиль)
    for payload in (
        {"sub": "a", "role": "admin"},
        {"sub": "a", "app_metadata": {"role": "owner"}},
        {"sub": "a", "roles": ["admin"]},
        {"sub": "a", "realm_access": {"roles": ["owner"]}},
    ):
        t = make_token(payload, secret=None)
        _, r = guard.caller_identity(f"Bearer {t}")
        check(f"неподписанный {list(payload)[1]} → NONE", r == Role.NONE, repr(r))

    print("настоящая подпись:")

    signed = make_token({"sub": "real-admin", "role": "admin"}, secret=SECRET)
    uid, role = guard.caller_identity(f"Bearer {signed}")
    check("подписанный admin признаётся", role == Role.ADMIN, repr(role))
    check("user_id из подписанного токена", uid == "real-admin", uid)
    got_uid, got_role = guard.require_admin(f"Bearer {signed}")
    check("require_admin пропускает настоящего админа",
          got_uid == "real-admin" and got_role == Role.ADMIN)

    signed_viewer = make_token({"sub": "u", "role": "viewer"}, secret=SECRET)
    try:
        guard.require_admin(f"Bearer {signed_viewer}")
        check("подписанный viewer не проходит как admin", False, "пропустил!")
    except rbac.PermissionDenied:
        check("подписанный viewer не проходит как admin", True)

    print("прочее:")

    for bad, why in ((None, "нет заголовка"), ("", "пусто"), ("Basic xxx", "не Bearer")):
        try:
            guard.caller_identity(bad)
            check(f"401 при «{why}»", False, "не отказал")
        except Exception as e:
            check(f"401 при «{why}»", getattr(e, "status_code", None) == 401, repr(e))

    no_sub = make_token({"role": "owner"}, secret=SECRET)
    try:
        guard.caller_identity(f"Bearer {no_sub}")
        check("401 при токене без sub", False, "не отказал")
    except Exception as e:
        check("401 при токене без sub", getattr(e, "status_code", None) == 401, repr(e))

    print()
    if failures:
        print(f"ПРОВАЛЕНО: {len(failures)} — {', '.join(failures)}")
        return 1
    print("всё сошлось")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
