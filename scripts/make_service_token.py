# -*- coding: utf-8 -*-
"""Выпуск сервис-токена для ВНЕШНИХ систем (шина данных наружу).

Любая внешняя система (mini Tess, n8n, свой скрипт, партнёрский сервис)
подключается к Tessbrain через REST API с Bearer-токеном. Токен —
HS256 JWT, подписанный TESSENT_SERVICE_JWT_SECRET; он жёстко привязан к
одному user_id (анти-IDOR: чужой ?user_id= с этим токеном → 403).

Использование (из корня tessent_brain, секрет берётся из настроек/env):
    python scripts/make_service_token.py --user-id <uuid> [--days 90]

Затем внешняя система шлёт заголовок:
    Authorization: Bearer <токен>
на любые эндпоинты каталога docs/MINI_TESS_INTEGRATION.md
(/insights/focus/today, /research/start, /datasets/ask, ...).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time


def _b64(b: bytes) -> bytes:
    return base64.urlsafe_b64encode(b).rstrip(b"=")


def make_token(secret: str, user_id: str, audience: str, issuer: str,
               days: int) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"},
                             separators=(",", ":")).encode())
    now = int(time.time())
    claims = {
        "user_id": user_id,
        "aud": audience,
        "iss": issuer,
        "iat": now,
        "exp": now + days * 86400,
    }
    payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
    sig = _b64(hmac.new(secret.encode("utf-8"), header + b"." + payload,
                        hashlib.sha256).digest())
    return (header + b"." + payload + b"." + sig).decode()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user-id", required=True,
                    help="user_id, к данным которого даётся доступ")
    ap.add_argument("--days", type=int, default=90, help="срок жизни (дней)")
    ap.add_argument("--secret", default=None,
                    help="перекрыть секрет (иначе settings/env)")
    args = ap.parse_args()

    secret = args.secret or os.getenv("SERVICE_JWT_SECRET", "") or os.getenv("TESSENT_SERVICE_JWT_SECRET", "")
    audience, issuer = "tessent-brain", "meetflow"
    if not secret:
        # пробуем настройки бэкенда (запуск из корня tessent_brain)
        try:
            sys.path.insert(0, ".")
            from backend.config import settings
            secret = getattr(settings, "service_jwt_secret", "") or ""
            audience = getattr(settings, "service_jwt_audience", audience)
            issuer = getattr(settings, "service_jwt_issuer", issuer)
        except Exception:
            pass
    if not secret:
        print("Секрет не найден: задайте SERVICE_JWT_SECRET в .env "
              "(любая длинная случайная строка) и перезапустите бэкенд.")
        sys.exit(1)

    token = make_token(secret, args.user_id, audience, issuer, args.days)
    print(token)
    print(f"\n# проверка (PowerShell):\n"
          f'curl.exe -H "Authorization: Bearer {token[:24]}..." '
          f'"http://localhost:8000/api/v1/insights/focus/today'
          f'?user_id={args.user_id}"', file=sys.stderr)


if __name__ == "__main__":
    main()
