"""Feature-flag rollout for the reactive engine (Phase O — production rollout).

Раньше движок управлялся единственным булевым settings.reactive_engine_enabled
(глобально вкл/выкл). Для прод-раската этого мало: нужен канареечный режим —
включить движок для 5% юзеров, посмотреть метрики, потом 25%, потом 100%, с
возможностью мгновенного отката master-флагом.

Здесь — детерминированный per-user bucketing поверх master-флага:
- reactive_engine_enabled        — master kill-switch (False → полностью legacy);
- reactive_engine_rollout_pct    — процент юзеров в раскате [0;100], default 100
  (при включённом master = раскат на всех, т.е. прежнее поведение);
- reactive_engine_canary_users   — явный allowlist (comma-sep user_id) для
  догфудинга конкретных аккаунтов независимо от процента.

Бакетинг СТАБИЛЕН между процессами/рестартами: sha1(user_id) % 100. Встроенный
hash() НЕ используем — он соль-рандомизирован по процессам (PYTHONHASHSEED), и
тогда один и тот же юзер попадал бы в раскат на одном воркере и нет — на другом.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _settings():
    from backend.config import get_settings
    return get_settings()


def _clamp_pct(value: object) -> int:
    try:
        return max(0, min(100, int(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 100


def _parse_canary(raw: object) -> set[str]:
    if not raw:
        return set()
    return {u.strip() for u in str(raw).split(",") if u.strip()}


def _stable_bucket(user_id: str) -> int:
    """Детерминированный bucket 0..99 по sha1(user_id)."""
    h = hashlib.sha1(user_id.encode("utf-8")).hexdigest()
    return int(h[:8], 16) % 100


def is_reactive_enabled_for_user(user_id: Optional[str]) -> bool:
    """Включён ли reactive-движок для конкретного юзера.

    Логика:
    - master off                → False (полностью legacy «всегда слать»);
    - user в canary allowlist    → True;
    - rollout_pct >= 100         → True;
    - rollout_pct <= 0           → False;
    - иначе                      → stable_bucket(user_id) < pct.

    Без user_id и при частичном раскате (0<pct<100) возвращаем False:
    стабильно забакетить некого, ведём себя консервативно (как до раската).
    """
    s = _settings()
    if not bool(getattr(s, "reactive_engine_enabled", False)):
        return False

    canary = _parse_canary(getattr(s, "reactive_engine_canary_users", ""))
    if user_id and user_id in canary:
        return True

    pct = _clamp_pct(getattr(s, "reactive_engine_rollout_pct", 100))
    if pct >= 100:
        return True
    if pct <= 0:
        return False
    if not user_id:
        return False
    return _stable_bucket(user_id) < pct


def rollout_status(user_id: Optional[str] = None) -> dict:
    """Снимок состояния раската для дашборда/диагностики.

    Если задан user_id — добавляет его bucket и итоговое enabled-решение.
    Never-raise: при сбое настроек возвращает безопасный дефолт.
    """
    try:
        s = _settings()
        master = bool(getattr(s, "reactive_engine_enabled", False))
        pct = _clamp_pct(getattr(s, "reactive_engine_rollout_pct", 100))
        canary = _parse_canary(getattr(s, "reactive_engine_canary_users", ""))
    except Exception as exc:
        logger.debug("rollout_status settings failed: %s", exc)
        master, pct, canary = False, 100, set()

    out: dict = {
        "master_enabled": master,
        "rollout_pct": pct,
        "canary_count": len(canary),
        "effective_audience": (
            "none" if not master
            else "all" if pct >= 100
            else "none" if pct <= 0
            else f"~{pct}% + {len(canary)} canary"
        ),
    }
    if user_id is not None:
        out["user_id"] = user_id
        out["bucket"] = _stable_bucket(user_id)
        out["in_canary"] = user_id in canary
        out["enabled"] = is_reactive_enabled_for_user(user_id)
    return out


__all__ = ["is_reactive_enabled_for_user", "rollout_status"]
