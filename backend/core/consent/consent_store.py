# -*- coding: utf-8 -*-
"""Согласие сотрудника на предоставление своего профиля/слепка.

Зачем: обмен слепками между компаниями («я даю свой аватар — через свой
кабинет») юридически невозможен без согласия самого человека. Этот модуль —
ВСЯ механика согласий, готовая заранее: когда юридическая модель будет
выбрана, обмен включается ОДНИМ флагом ENABLE_PROFILE_EXCHANGE, без дописывания
кода согласий.

Принципы:
  1. Согласие даёт ТОЛЬКО сам человек из своего кабинета (verified-токен),
     не работодатель и не админ. Отзыв — так же, в один клик, действует сразу.
  2. Согласие всегда конкретное: кому (grantee: id организации/потребителя
     или "*"), на какой срез слепка (scope: named-разделы или "*"),
     до какого срока (expires_at; бессрочных нет — максимум 365 дней).
  3. Каждая выдача/отзыв — журналируемое событие с версией текста согласия
     (consent_text_version): видно, под чем именно человек подписался.
  4. Fail-closed: нет активного согласия (или флаг обмена выключен) →
     require_person_consent() поднимает PermissionError. Гейт зовётся в
     точке выдачи слепка наружу (data_bus / внешний twin-путь), И ПОКА
     ОБМЕНА НЕТ — не зовётся никем, т.е. ничего не меняет в проде.

Хранилище — data/consents/<owner_uid>.json (паттерн остальных примитивов
памяти): atomic write, never-raise на чтении.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Версия текста согласия. Меняется при изменении юридического текста в UI —
# старые записи хранят версию, под которой были выданы.
CONSENT_TEXT_VERSION = "draft-1"

# Разделы слепка, на которые можно дать согласие по отдельности.
SCOPES = ("role", "skills", "projects", "decisions", "opinions",
          "communication_style", "cognitive_frame")

_MAX_DAYS = 365


def _dir() -> Path:
    return Path(os.environ.get("CONSENTS_DIR", "").strip() or "data/consents")


def _path(owner_uid: str) -> Path:
    return _dir() / f"{owner_uid}.json"


def _load(owner_uid: str) -> List[Dict[str, Any]]:
    try:
        p = _path(owner_uid)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
    except Exception:
        logger.warning("consents: файл не читается", exc_info=True)
    return []


def _save(owner_uid: str, rows: List[Dict[str, Any]]) -> None:
    p = _path(owner_uid)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, p)


def exchange_enabled() -> bool:
    """Обмен профилями между компаниями. По умолчанию ВЫКЛ."""
    return os.environ.get("ENABLE_PROFILE_EXCHANGE", "").strip().lower() in (
        "1", "on", "true", "yes")


def grant(owner_uid: str, *, grantee: str, scope: Optional[List[str]] = None,
          days: int = 90, note: str = "") -> Dict[str, Any]:
    """Выдать согласие. owner_uid — сам человек (проверенный токен на роуте).

    grantee — кому: id организации/потребителя, либо "*" (любой, кому владелец
    данных решит показать — самый широкий вариант, в UI помечается отдельно).
    scope — какие разделы слепка; None/["*"] = весь слепок."""
    owner_uid = str(owner_uid or "").strip()
    grantee = str(grantee or "").strip()
    if not owner_uid or not grantee:
        raise ValueError("owner_uid и grantee обязательны")
    days = max(1, min(int(days or 90), _MAX_DAYS))
    sc = [s for s in (scope or ["*"]) if s == "*" or s in SCOPES]
    if not sc:
        raise ValueError(f"scope должен быть из {SCOPES} или '*'")
    now = int(time.time())
    rec = {
        "id": uuid.uuid4().hex[:16],
        "grantee": grantee,
        "scope": sc,
        "granted_at": now,
        "expires_at": now + days * 86400,
        "revoked_at": None,
        "note": str(note or "")[:300],
        "consent_text_version": CONSENT_TEXT_VERSION,
    }
    rows = _load(owner_uid)
    rows.append(rec)
    _save(owner_uid, rows)
    logger.info("consent granted: owner=%s grantee=%s scope=%s days=%s",
                owner_uid, grantee, sc, days)
    return rec


def list_for(owner_uid: str, *, include_inactive: bool = True
             ) -> List[Dict[str, Any]]:
    now = int(time.time())
    out = []
    for r in _load(owner_uid):
        active = (not r.get("revoked_at")) and int(r.get("expires_at") or 0) > now
        if include_inactive or active:
            out.append({**r, "active": active})
    return out


def revoke(owner_uid: str, consent_id: str) -> bool:
    """Отозвать. Действует немедленно: has_active_consent сразу False."""
    rows = _load(owner_uid)
    hit = False
    for r in rows:
        if r.get("id") == consent_id and not r.get("revoked_at"):
            r["revoked_at"] = int(time.time())
            hit = True
    if hit:
        _save(owner_uid, rows)
        logger.info("consent revoked: owner=%s id=%s", owner_uid, consent_id)
    return hit


def has_active_consent(owner_uid: str, *, grantee: str,
                       scope: Optional[str] = None) -> bool:
    """Есть ли живое согласие owner'а на выдачу grantee (и раздел scope)."""
    now = int(time.time())
    for r in _load(owner_uid):
        if r.get("revoked_at") or int(r.get("expires_at") or 0) <= now:
            continue
        g = str(r.get("grantee") or "")
        if g != "*" and g != str(grantee or ""):
            continue
        sc = r.get("scope") or []
        if scope and "*" not in sc and scope not in sc:
            continue
        return True
    return False


def require_person_consent(owner_uid: str, *, grantee: str,
                           scope: Optional[str] = None) -> None:
    """Гейт выдачи слепка наружу. Fail-closed.

    Точка врезки, когда обмен будет включён: bus_service (резолв политики с
    Person-слепком) и любой внешний twin-путь. Порядок проверок важен: сначала
    флаг (обмена нет вообще), потом согласие конкретного человека."""
    if not exchange_enabled():
        raise PermissionError(
            "обмен профилями выключен (ENABLE_PROFILE_EXCHANGE)")
    if not has_active_consent(owner_uid, grantee=grantee, scope=scope):
        raise PermissionError(
            "нет активного согласия сотрудника на предоставление профиля")
