# -*- coding: utf-8 -*-
"""Уход сотрудника: что происходит с его слепком, командой и согласиями.

До этого модуля `remove_member` убирал членство — и всё. Оставались три
хвоста, каждый со своей ценой:

  1. **Слепок продолжал отвечать.** Человек ушёл, а его цифровая копия
     по-прежнему говорит от его лица — и он об этом уже не узнает, потому
     что доступа к журналу обращений у него больше нет. Это та самая яма,
     на которую юристы указывают Viven: использование слепка бывшего
     сотрудника — вопрос согласия, компенсации и коммерческой тайны.

  2. **Подчинённые повисали.** `reports_to` указывал на человека, которого
     в организации уже нет. Цепочка руководителей обрывалась: руководитель
     ушедшего переставал видеть его бывшую команду, хотя фактически она
     теперь его. Тихая потеря доступа, которую замечают не сразу.

  3. **Согласия оставались активными.** Разрешения, выданные человеком на
     обращение к его слепку, продолжали действовать после ухода.

Здесь всё это делается одним журналируемым действием, чтобы не зависело
от того, вспомнил ли администратор про каждый пункт.

Заморозка слепка — умолчание, а не жёсткое правило: сценарий «человек
ушёл, а знания остались» законен и востребован, но включать его должен
кто-то явно и осознанно, а не забывчивость.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _frozen_path() -> Path:
    base = Path(os.environ.get("TWIN_FROZEN_DIR", "").strip()
                or "data/twin_frozen")
    return base / "frozen.json"


def _load_frozen() -> Dict[str, Any]:
    try:
        p = _frozen_path()
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        logger.warning("offboarding: реестр заморозок не читается",
                       exc_info=True)
    return {}


def _save_frozen(data: Dict[str, Any]) -> None:
    p = _frozen_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, p)


def _key(org_id: str, person_id: str) -> str:
    return f"{str(org_id or '').strip()}:{str(person_id or '').strip().lower()}"


def freeze_twin(org_id: str, person_id: str, *, actor_id: str = "",
                reason: str = "offboarding") -> bool:
    """Запретить обращения к слепку. Идемпотентно."""
    if not person_id:
        return False
    data = _load_frozen()
    data[_key(org_id, person_id)] = {
        "person_id": person_id,
        "org_id": org_id,
        "frozen_at": int(time.time()),
        "actor_id": str(actor_id or "")[:100],
        "reason": str(reason or "")[:200],
    }
    _save_frozen(data)
    logger.info("twin frozen: org=%s person=%s reason=%s",
                org_id, person_id, reason)
    return True


def unfreeze_twin(org_id: str, person_id: str) -> bool:
    """Снять заморозку — например, человек вернулся или организация
    осознанно решила сохранить доступ к знаниям ушедшего."""
    data = _load_frozen()
    if data.pop(_key(org_id, person_id), None) is None:
        return False
    _save_frozen(data)
    logger.info("twin unfrozen: org=%s person=%s", org_id, person_id)
    return True


def is_twin_frozen(org_id: str, person_id: str) -> bool:
    if not person_id:
        return False
    return _key(org_id, person_id) in _load_frozen()


def frozen_twin_info(org_id: str, person_id: str) -> Optional[Dict[str, Any]]:
    return _load_frozen().get(_key(org_id, person_id))


def list_frozen(org_id: str) -> List[Dict[str, Any]]:
    prefix = f"{str(org_id or '').strip()}:"
    return [v for k, v in _load_frozen().items() if k.startswith(prefix)]


async def offboard(user_id: str, org_id: str, *, actor_id: str,
                   reassign_reports_to: Optional[str] = None,
                   freeze: bool = True,
                   remove_membership: bool = True) -> Dict[str, Any]:
    """Провести уход сотрудника одним действием.

    reassign_reports_to — кому переходят прямые подчинённые. Если не
    указан, берём руководителя уходящего: команда естественно поднимается
    на уровень выше, а не повисает. Если руководителя нет (уходит первое
    лицо) — подчинённые остаются без него, и это возвращается явно в
    `orphaned`, чтобы администратор увидел и решил сам, а не обнаружил
    случайно.

    freeze=False — организация осознанно сохраняет слепок доступным.
    """
    from backend.core.ingest import membership

    result: Dict[str, Any] = {
        "status": "success", "user_id": user_id, "org_id": org_id,
        "reassigned": [], "orphaned": [], "twin_frozen": False,
        "consents_revoked": 0, "membership_removed": False,
    }
    if not (user_id and org_id):
        result["status"] = "error"
        result["message"] = "user_id и org_id обязательны"
        return result

    person_id = ""
    try:
        person_id = str(membership.get_user_person_entity(user_id) or "")
    except Exception:
        logger.debug("offboarding: person_entity не резолвится", exc_info=True)

    # 1. Подчинённые. Делаем ДО удаления членства: после него уходящий уже
    # не найдётся и его руководителя не вычислить.
    new_manager = reassign_reports_to
    if new_manager is None:
        try:
            new_manager = membership.get_user_org_reports_to(user_id) or ""
        except Exception:
            new_manager = ""

    try:
        for m in membership.list_members(org_id):
            if str(m.get("reports_to") or "") != user_id:
                continue
            sub = str(m.get("user_id") or "")
            if not sub:
                continue
            if new_manager and new_manager != sub:
                if membership.set_member_reports_to(sub, org_id, new_manager):
                    result["reassigned"].append(
                        {"user_id": sub, "new_manager": new_manager})
                else:
                    result["orphaned"].append(sub)
            else:
                # Некому передать (ушло первое лицо) либо получилось бы
                # подчинение самому себе — снимаем ссылку и говорим прямо.
                membership.set_member_reports_to(sub, org_id, None)
                result["orphaned"].append(sub)
    except Exception:
        logger.exception("offboarding: переназначение подчинённых не удалось")
        result["status"] = "partial"

    # 2. Слепок
    if freeze and person_id:
        try:
            result["twin_frozen"] = freeze_twin(
                org_id, person_id, actor_id=actor_id, reason="offboarding")
        except Exception:
            logger.exception("offboarding: заморозка слепка не удалась")
            result["status"] = "partial"

    # 3. Согласия, выданные уходящим
    try:
        from backend.core.consent import consent_store
        for c in consent_store.list_for(user_id, include_inactive=False):
            if c.get("active") and consent_store.revoke(user_id, c["id"]):
                result["consents_revoked"] += 1
    except Exception:
        logger.exception("offboarding: отзыв согласий не удался")
        result["status"] = "partial"

    # 4. Членство — последним: всё выше опирается на него
    if remove_membership:
        try:
            result["membership_removed"] = bool(
                membership.remove_member(user_id, org_id))
        except Exception:
            logger.exception("offboarding: удаление членства не удалось")
            result["status"] = "partial"

    try:
        from backend.core.observability import audit_log
        await audit_log.emit(
            action="org.member.offboarded", user_id=actor_id,
            payload={"target": user_id, "org_id": org_id,
                     "person_id": person_id,
                     "reassigned": len(result["reassigned"]),
                     "orphaned": len(result["orphaned"]),
                     "twin_frozen": result["twin_frozen"],
                     "consents_revoked": result["consents_revoked"]})
    except Exception:
        logger.debug("offboarding: аудит не записан", exc_info=True)

    return result
