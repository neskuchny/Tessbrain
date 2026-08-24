# -*- coding: utf-8 -*-
"""Федеративная связь между двумя организациями.

Чего не хватало. Шина умеет отдать срез памяти внешнему потребителю по
ключу — это односторонняя выдача: организация A завела ключ, кто-то им
пользуется. Но «компании обмениваются контекстом» — это не ключ, а
ОТНОШЕНИЕ: обе стороны существуют в системе, каждая сама решает, что
открывает, и ни одна не может выдать себе доступ к другой.

Здесь — состояние и правила этого отношения. Чистый модуль: ни файлов,
ни сети, ни БД; хранение и HTTP живут снаружи и зовут эти функции.

Правила, ради которых модуль отдельный:

  1. **Рукопожатие обязательно.** Связь начинается предложением одной
     стороны и становится действующей только после согласия ДРУГОЙ.
     Предложивший не может принять собственное предложение — иначе связь
     была бы односторонним захватом.
  2. **Доступ по умолчанию закрыт.** Даже у действующей связи каждая
     сторона отдаёт ровно то, что объявила своей политикой. Не объявила —
     не отдаёт ничего. Отсутствие политики никогда не означает «всё».
  3. **Разрыв односторонний и мгновенный.** Прекратить связь может любая
     сторона, без согласия второй и без ожидания.
  4. **Разорванная связь не воскресает.** Возобновление — это новое
     предложение и новое согласие, а не смена флага: иначе отзыв доступа
     не был бы отзывом.
  5. **Связь только между разными организациями** и только одна активная
     на пару — чтобы «какая политика действует» имело единственный ответ.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

# Состояния связи. Переходы описаны в _ALLOWED_TRANSITIONS.
PROPOSED = "proposed"     # предложена, ждёт ответа второй стороны
ACTIVE = "active"         # обе стороны согласились
REJECTED = "rejected"     # вторая сторона отказала
REVOKED = "revoked"       # действовавшая связь прекращена

_ALLOWED_TRANSITIONS = {
    PROPOSED: {ACTIVE, REJECTED, REVOKED},
    ACTIVE: {REVOKED},
    REJECTED: set(),
    REVOKED: set(),
}

# Роли, которым позволено заводить и рвать связь от имени организации.
# Обмен контекстом между компаниями — не рядовая настройка.
AUTHORIZED_ROLES = frozenset({"founder", "admin"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class FederationLink:
    """Связь между двумя организациями.

    policy_by_org: {org_id: policy_id} — что КАЖДАЯ сторона открывает
    второй. Ключ появляется только когда сторона явно объявила политику.
    """
    id: str
    org_a: str
    org_b: str
    status: str
    proposed_by_org: str
    proposed_by_user: str
    policy_by_org: Dict[str, str] = field(default_factory=dict)
    purpose: str = ""
    created_at: str = ""
    decided_at: str = ""
    decided_by_user: str = ""
    revoked_at: str = ""
    revoked_by_org: str = ""
    revoked_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "FederationLink":
        return cls(**{k: v for k, v in (d or {}).items()
                      if k in cls.__dataclass_fields__})

    @property
    def orgs(self) -> Tuple[str, str]:
        return (self.org_a, self.org_b)

    def other_side(self, org_id: str) -> Optional[str]:
        if org_id == self.org_a:
            return self.org_b
        if org_id == self.org_b:
            return self.org_a
        return None

    def involves(self, org_id: str) -> bool:
        return org_id in (self.org_a, self.org_b)


def pair_key(org_a: str, org_b: str) -> str:
    """Ключ пары, независимый от порядка: связь A↔B и B↔A — одна и та же."""
    return "::".join(sorted([str(org_a or ""), str(org_b or "")]))


def propose_link(*, org_a: str, org_b: str, proposed_by_user: str,
                 role: str, purpose: str = "",
                 policy_id: str = "") -> Dict[str, Any]:
    """Предложить связь. Возвращает {"ok", "link"|"error"}.

    policy_id — что предлагающая сторона готова открыть. Пустой допустим:
    можно предложить связь и объявить срез позже, до этого сторона не
    отдаёт ничего.
    """
    a, b = str(org_a or "").strip(), str(org_b or "").strip()
    if not a or not b:
        return {"ok": False, "error": "нужны обе организации"}
    if a == b:
        return {"ok": False,
                "error": "связь с самой собой не имеет смысла"}
    if role not in AUTHORIZED_ROLES:
        return {"ok": False,
                "error": "предложить обмен данными между компаниями может "
                         "только основатель или администратор организации"}
    link = FederationLink(
        id=f"fed_{uuid.uuid4().hex[:12]}",
        org_a=a, org_b=b, status=PROPOSED,
        proposed_by_org=a, proposed_by_user=str(proposed_by_user or ""),
        policy_by_org=({a: policy_id} if policy_id else {}),
        purpose=str(purpose or "")[:500],
        created_at=_now(),
    )
    return {"ok": True, "link": link}


def decide_link(link: FederationLink, *, decider_org: str,
                decider_user: str, role: str, accept: bool) -> Dict[str, Any]:
    """Ответить на предложение. Принять может ТОЛЬКО другая сторона."""
    if link.status != PROPOSED:
        return {"ok": False,
                "error": f"на связь в состоянии «{link.status}» ответить нельзя"}
    org = str(decider_org or "").strip()
    if not link.involves(org):
        return {"ok": False, "error": "организация не участвует в этой связи"}
    if org == link.proposed_by_org:
        return {"ok": False,
                "error": "принять предложение может только вторая сторона — "
                         "иначе связь была бы односторонней"}
    if role not in AUTHORIZED_ROLES:
        return {"ok": False,
                "error": "ответить на предложение может только основатель "
                         "или администратор организации"}
    link.status = ACTIVE if accept else REJECTED
    link.decided_at = _now()
    link.decided_by_user = str(decider_user or "")
    return {"ok": True, "link": link}


def revoke_link(link: FederationLink, *, org_id: str, role: str,
                reason: str = "") -> Dict[str, Any]:
    """Прекратить связь. Любая сторона, без согласия второй."""
    org = str(org_id or "").strip()
    if not link.involves(org):
        return {"ok": False, "error": "организация не участвует в этой связи"}
    if role not in AUTHORIZED_ROLES:
        return {"ok": False,
                "error": "прекратить связь может только основатель или "
                         "администратор организации"}
    if REVOKED not in _ALLOWED_TRANSITIONS.get(link.status, set()):
        return {"ok": False,
                "error": f"связь в состоянии «{link.status}» уже неактивна"}
    link.status = REVOKED
    link.revoked_at = _now()
    link.revoked_by_org = org
    link.revoked_reason = str(reason or "")[:300]
    return {"ok": True, "link": link}


def set_side_policy(link: FederationLink, *, org_id: str, role: str,
                    policy_id: str) -> Dict[str, Any]:
    """Объявить (или сменить) свой срез. Сторона управляет ТОЛЬКО своим.

    Пустой policy_id снимает выдачу: сторона перестаёт отдавать что-либо,
    не разрывая связь.
    """
    org = str(org_id or "").strip()
    if not link.involves(org):
        return {"ok": False, "error": "организация не участвует в этой связи"}
    if role not in AUTHORIZED_ROLES:
        return {"ok": False,
                "error": "менять срез выдачи может только основатель или "
                         "администратор организации"}
    if link.status not in (PROPOSED, ACTIVE):
        return {"ok": False,
                "error": f"связь в состоянии «{link.status}» — срез не меняется"}
    pid = str(policy_id or "").strip()
    if pid:
        link.policy_by_org[org] = pid
    else:
        link.policy_by_org.pop(org, None)
    return {"ok": True, "link": link}


def resolve_access(link: Optional[FederationLink], *, requester_org: str,
                   target_org: str) -> Dict[str, Any]:
    """Что запрашивающая организация вправе получить у целевой.

    Единственная точка, отвечающая на вопрос «пускать ли». Возвращает
    {"allowed": bool, "policy_id": str|None, "reason": str}.
    Отказ по умолчанию: нет связи, связь не действует, целевая сторона не
    объявила срез — доступа нет.
    """
    req, tgt = str(requester_org or "").strip(), str(target_org or "").strip()
    if link is None:
        return {"allowed": False, "policy_id": None,
                "reason": "между организациями нет связи"}
    if not (link.involves(req) and link.involves(tgt)) or req == tgt:
        return {"allowed": False, "policy_id": None,
                "reason": "связь не соединяет эти организации"}
    if link.status != ACTIVE:
        return {"allowed": False, "policy_id": None,
                "reason": f"связь не действует (состояние «{link.status}»)"}
    policy_id = link.policy_by_org.get(tgt)
    if not policy_id:
        return {"allowed": False, "policy_id": None,
                "reason": "принимающая сторона не объявила, что открывает"}
    return {"allowed": True, "policy_id": policy_id, "reason": "связь активна"}
