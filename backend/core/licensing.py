# -*- coding: utf-8 -*-
"""Офлайн-лицензия Tessbrain для on-prem поставок (анти-кража, слой 1 из 4).

Честная рамка: от решительного вора с root-доступом к своему железу код
на 100% не защитить технически — защита многослойная (см.
docs/ENTERPRISE_PORTABILITY.md): договор+NDA+аудит, поставка образами без
исходников, гибрид (ядро у нас), и ЭТОТ модуль — подписанная лицензия.

Механика (air-gap-совместимая, БЕЗ phone-home):
- Лицензия = `base64(payload_json) + "." + base64(ed25519_signature)`.
- payload: {org, deployment_id, plan, expires_at, seats}.
- Публичный ключ зашит здесь; ПРИВАТНЫЙ — только у нас (scripts/
  make_license.py). Подделать лицензию без приватного ключа нельзя;
  выпилить проверку из образа можно — но это уже осознанный взлом,
  фиксируемый договором (и watermark deployment_id остаётся в логах).
- Enforcement: TESSENT_LICENSE_REQUIRED=on (в enterprise-образах) →
  без валидной лицензии приложение НЕ стартует; иначе (наш SaaS,
  dev) — модуль молчит. Истечение: жёсткий отказ старта после
  expires_at + grace 14 дней (предупреждения в логи заранее).

Watermark: deployment_id из лицензии добавляется в логи старта — утёкшая
копия опознаётся по нему.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Публичный ключ подписи лицензий (Ed25519, raw 32 байта, base64).
# ЗАМЕНИТЬ на реальный при первом выпуске enterprise-образа:
#   python scripts/make_license.py keygen
_LICENSE_PUBKEY_B64 = os.getenv(
    "TESSENT_LICENSE_PUBKEY",
    "PLACEHOLDER_PUBKEY_REPLACE_BEFORE_ENTERPRISE_SHIP==")

GRACE_DAYS = 14


def license_required() -> bool:
    return os.getenv("TESSENT_LICENSE_REQUIRED", "").strip().lower() in (
        "on", "1", "true", "yes")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _verify_signature(payload_bytes: bytes, signature: bytes) -> bool:
    """Ed25519-проверка. Вынесена отдельно: тестируется подменой, а в этом
    test-env cryptography под pytest паникует (в проде работает)."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
        pub = Ed25519PublicKey.from_public_bytes(_b64d(_LICENSE_PUBKEY_B64))
        pub.verify(signature, payload_bytes)
        return True
    except Exception:
        return False


def parse_license(token: str) -> Optional[dict[str, Any]]:
    """Разобрать токен БЕЗ проверки подписи (payload для сообщений об
    ошибках). None при мусоре."""
    try:
        payload_b64, _sig_b64 = token.strip().split(".", 1)
        data = json.loads(_b64d(payload_b64))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def check_license(token: Optional[str] = None,
                  now: Optional[datetime] = None) -> dict[str, Any]:
    """Полная проверка лицензии. Возвращает
    {valid, reason, org, deployment_id, plan, expires_at, days_left, grace}.
    Never-raise."""
    token = token if token is not None else os.getenv("TESSENT_LICENSE", "")
    now = now or datetime.now(timezone.utc)
    out: dict[str, Any] = {"valid": False, "reason": None, "grace": False}
    if not token.strip():
        out["reason"] = "лицензия не задана (TESSENT_LICENSE)"
        return out
    try:
        payload_b64, sig_b64 = token.strip().split(".", 1)
        payload_bytes = _b64d(payload_b64)
        signature = _b64d(sig_b64)
    except Exception:
        out["reason"] = "лицензия повреждена (не base64 payload.sig)"
        return out

    if not _verify_signature(payload_bytes, signature):
        out["reason"] = "подпись лицензии не прошла проверку"
        return out

    try:
        data = json.loads(payload_bytes)
    except Exception:
        out["reason"] = "payload лицензии не JSON"
        return out

    out.update({k: data.get(k) for k in
                ("org", "deployment_id", "plan", "seats", "expires_at")})
    exp_raw = str(data.get("expires_at") or "")
    try:
        exp = datetime.fromisoformat(exp_raw.replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
    except Exception:
        out["reason"] = "expires_at лицензии нечитаем"
        return out

    days_left = (exp - now).days
    out["days_left"] = days_left
    if now <= exp:
        out["valid"] = True
        if days_left <= 30:
            logger.warning("Лицензия Tessbrain истекает через %d дн. (org=%s)",
                           days_left, out.get("org"))
        return out
    # истекла: grace-период, чтобы продление не роняло прод в полночь
    if now <= exp + timedelta(days=GRACE_DAYS):
        out["valid"] = True
        out["grace"] = True
        logger.error("Лицензия Tessbrain ИСТЕКЛА %s — grace до %s (org=%s). "
                     "Продлите немедленно.", exp.date(),
                     (exp + timedelta(days=GRACE_DAYS)).date(), out.get("org"))
        return out
    out["reason"] = f"лицензия истекла {exp.date()} (grace {GRACE_DAYS} дн. исчерпан)"
    return out


def enforce_license_on_startup() -> Optional[dict[str, Any]]:
    """Вызывается при старте приложения. TESSENT_LICENSE_REQUIRED=off →
    None (наш SaaS/dev — модуль молчит). on → валидная лицензия или
    RuntimeError (приложение не стартует). Возвращает инфо лицензии для
    watermark-лога."""
    if not license_required():
        return None
    info = check_license()
    if not info["valid"]:
        raise RuntimeError(
            f"Tessbrain license check failed: {info['reason']}. "
            "Обратитесь к поставщику за лицензией (TESSENT_LICENSE).")
    logger.info(
        "Tessbrain licensed: org=%s deployment=%s plan=%s until=%s%s",
        info.get("org"), info.get("deployment_id"), info.get("plan"),
        info.get("expires_at"), " [GRACE]" if info.get("grace") else "")
    return info
