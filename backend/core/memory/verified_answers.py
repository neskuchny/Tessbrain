# -*- coding: utf-8 -*-
"""
Verified answers (Glean-перенос №4, docs/GLEAN_ADOPTION.md):
пометка «проверено владельцем» на факте + честное протухание.

БЕЗОПАСНОСТЬ ПО ПОСТРОЕНИЮ: supersede-стор (ядро памяти) НЕ трогается —
ни формат, ни код, ни sqlite-миграции. Отметки живут в ОТДЕЛЬНОМ
per-user JSON; интеграция только аддитивная (бейдж в 360-карточке).
Сломать поиск или данные памяти этим модулем физически нечем.

Протухание (детерминированно):
1) факт ИЗМЕНИЛСЯ после проверки (as_of текущей версии > verified_at)
   → stale «факт изменился после проверки»;
2) истёк срок (ttl_days, default 90) → stale «срок проверки истёк».
"""
from __future__ import annotations

import datetime
import json
from typing import Dict, Optional

DEFAULT_TTL_DAYS = 90


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _parse(s: Optional[str]) -> Optional[datetime.datetime]:
    if not s:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(
            tzinfo=datetime.timezone.utc)
    except (ValueError, AttributeError):
        return None


class VerifiedAnswers:
    """{fact_key: {verified_by, verified_at, ttl_days}} — отдельный стор."""

    def __init__(self, persist_path: str):
        self._path = persist_path

    def _load(self) -> Dict[str, dict]:
        try:
            with open(self._path, encoding="utf-8") as f:
                return json.load(f).get("verified", {})
        except (OSError, ValueError):
            return {}

    def verify(self, fact_key: str, *, by: str,
               ttl_days: int = DEFAULT_TTL_DAYS) -> dict:
        fact_key = str(fact_key or "").strip()
        if not fact_key or not str(by or "").strip():
            raise ValueError("fact_key и by обязательны")
        from backend.core.store.tenant_io import atomic_write_json, file_lock
        with file_lock(self._path):
            data = self._load()
            data[fact_key] = {"verified_by": str(by).strip(),
                              "verified_at": _now_iso(),
                              "ttl_days": max(1, int(ttl_days))}
            atomic_write_json(self._path, {"verified": data})
            return {"fact_key": fact_key, **data[fact_key]}

    def unverify(self, fact_key: str) -> bool:
        from backend.core.store.tenant_io import atomic_write_json, file_lock
        with file_lock(self._path):
            data = self._load()
            if fact_key not in data:
                return False
            del data[fact_key]
            atomic_write_json(self._path, {"verified": data})
            return True

    def list(self) -> Dict[str, dict]:
        return self._load()

    def status(self, fact_key: str,
               fact_as_of: Optional[str] = None) -> Optional[dict]:
        """Статус проверки факта. None — факт не проверялся (честно).

        fact_as_of — as_of ТЕКУЩЕЙ версии факта из supersede (read-only
        вход; сам supersede не трогаем)."""
        rec = self._load().get(str(fact_key or "").strip())
        if not rec:
            return None
        out = dict(rec)
        out["stale"] = False
        out["stale_reason"] = None
        v_at = _parse(rec.get("verified_at"))
        f_at = _parse(fact_as_of)
        now = datetime.datetime.now(datetime.timezone.utc)
        # толерантность 2с: verified_at секундной точности, as_of — с
        # микросекундами; «проверил сразу после записи» не должно протухать
        if v_at and f_at and (f_at - v_at).total_seconds() > 2:
            out["stale"] = True
            out["stale_reason"] = "факт изменился после проверки"
        elif v_at and (now - v_at).days > int(rec.get("ttl_days",
                                                      DEFAULT_TTL_DAYS)):
            out["stale"] = True
            out["stale_reason"] = (f"срок проверки истёк "
                                   f"(> {rec.get('ttl_days')} дн.)")
        return out


def verified_for_user(user_id: str) -> VerifiedAnswers:
    from backend.core.store.tenant_paths import verified_path_for_user
    return VerifiedAnswers(verified_path_for_user(user_id))
