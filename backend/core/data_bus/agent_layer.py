# -*- coding: utf-8 -*-
"""Слой внешних агентов: реестр и задачи с приёмкой результата.

Закрывает два разрыва из бизнес-карты:
  - «самостоятельной регистрации чужого агента нет» — протокол был жёстко
    привязан к двум конкретным агентам;
  - «нет автоматической приёмки их результата нашими агентами» — внешняя
    система могла забрать контекст, но что она сделала, никто не проверял.

Устройство:

  РЕЕСТР. Внешний агент — это именованный исполнитель, привязанный к уже
  существующему каналу доступа (ключ шины или федеративная связь). Агент
  не получает собственных прав: он ходит через свой канал со всеми его
  ситами. Регистрирует агента администратор организации; агент без
  привязки к каналу не регистрируется — анонимных исполнителей нет.

  ЗАДАЧИ. Жизненный цикл: предложена → взята → сдана → (принята |
  возвращена). Приёмка — детерминированная и трёхисходная, как в Kanon:
  pass / fail / inconclusive, и «нет доказательств ≠ готово». Машинная
  приёмка отбраковывает явный брак и возвращает задачу с конкретными
  замечаниями; человек проверяет уже отобранное — финальное закрытие
  всегда за человеком.

Чистый модуль: правила и проверки, без файлов и сети.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ── Реестр ──────────────────────────────────────────────────────────────

AGENT_ACTIVE = "active"
AGENT_SUSPENDED = "suspended"
AGENT_RETIRED = "retired"

# Каналы, через которые агент может действовать.
CHANNEL_CONSUMER = "consumer"      # ключ шины данных
CHANNEL_FEDERATION = "federation"  # федеративная связь организаций

ADMIN_ROLES = frozenset({"founder", "admin"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ExternalAgent:
    """Внешний агент: кто, чей, через какой канал ходит, что умеет."""
    id: str
    org_id: str                    # организация, у которой он работает
    name: str
    channel_kind: str              # consumer | federation
    channel_id: str                # consumer_id или link_id
    capabilities: List[str] = field(default_factory=list)
    operator: str = ""             # чья это система («Acme Landing Bot»)
    status: str = AGENT_ACTIVE
    registered_by: str = ""
    created_at: str = ""
    retired_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ExternalAgent":
        return cls(**{k: v for k, v in (d or {}).items()
                      if k in cls.__dataclass_fields__})


def register_agent(*, org_id: str, name: str, channel_kind: str,
                   channel_id: str, role: str, registered_by: str,
                   capabilities: Optional[List[str]] = None,
                   operator: str = "") -> Dict[str, Any]:
    """Зарегистрировать внешнего агента. Правила:

    - только администратор организации: агент — это доверенный исполнитель;
    - обязательная привязка к существующему каналу: агент без канала был бы
      именем без границ доступа — анонимных исполнителей нет;
    - собственных прав агент не получает никогда: границы задаёт канал.
    """
    if role not in ADMIN_ROLES:
        return {"ok": False,
                "error": "регистрировать внешних агентов может только "
                         "основатель или администратор организации"}
    if not str(name or "").strip():
        return {"ok": False, "error": "у агента должно быть имя"}
    if channel_kind not in (CHANNEL_CONSUMER, CHANNEL_FEDERATION):
        return {"ok": False,
                "error": "канал агента — ключ шины или федеративная связь"}
    if not str(channel_id or "").strip():
        return {"ok": False,
                "error": "агент без привязки к каналу не регистрируется — "
                         "анонимных исполнителей нет"}
    agent = ExternalAgent(
        id=f"agent_{uuid.uuid4().hex[:12]}",
        org_id=str(org_id), name=str(name).strip()[:200],
        channel_kind=channel_kind, channel_id=str(channel_id).strip(),
        capabilities=[str(c).strip()[:60] for c in (capabilities or [])
                      if str(c).strip()][:20],
        operator=str(operator or "")[:200],
        registered_by=str(registered_by or ""),
        created_at=_now(),
    )
    return {"ok": True, "agent": agent}


def set_agent_status(agent: ExternalAgent, *, role: str,
                     status: str) -> Dict[str, Any]:
    """Приостановить/вернуть/списать агента. Списанный не возвращается:
    возобновление доверия — новая регистрация, а не смена флага."""
    if role not in ADMIN_ROLES:
        return {"ok": False, "error": "менять статус агента может только "
                                      "основатель или администратор"}
    if status not in (AGENT_ACTIVE, AGENT_SUSPENDED, AGENT_RETIRED):
        return {"ok": False, "error": f"неизвестный статус {status!r}"}
    if agent.status == AGENT_RETIRED:
        return {"ok": False,
                "error": "списанный агент не возвращается — зарегистрируйте "
                         "заново, это новое решение о доверии"}
    agent.status = status
    if status == AGENT_RETIRED:
        agent.retired_at = _now()
    return {"ok": True, "agent": agent}


# ── Задачи ──────────────────────────────────────────────────────────────

TASK_OFFERED = "offered"        # предложена, агент ещё не взял
TASK_IN_PROGRESS = "in_progress"
TASK_SUBMITTED = "submitted"    # сдана, ждёт машинной приёмки
TASK_ACCEPTED = "accepted"      # машина приняла — ждёт человека
TASK_RETURNED = "returned"      # возвращена с замечаниями
TASK_CLOSED = "closed"          # человек закрыл (финал)
TASK_CANCELLED = "cancelled"

MAX_RETURNS = 3   # после трёх возвратов задача не пересдаётся автоматически

PASS, FAIL, INCONCLUSIVE = "pass", "fail", "inconclusive"


@dataclass
class AgentTask:
    id: str
    org_id: str
    agent_id: str
    title: str
    spec_text: str
    # Проверки приёмки: [{"kind": "contains"|"regex"|"min_len", ...}]
    acceptance: List[Dict[str, Any]] = field(default_factory=list)
    status: str = TASK_OFFERED
    created_by: str = ""
    created_at: str = ""
    result_text: str = ""
    submitted_at: str = ""
    verdict: Dict[str, Any] = field(default_factory=dict)
    returns_count: int = 0
    closed_by: str = ""
    closed_at: str = ""
    history: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AgentTask":
        return cls(**{k: v for k, v in (d or {}).items()
                      if k in cls.__dataclass_fields__})

    def _log(self, event: str, detail: str = "") -> None:
        self.history.append({"at": _now(), "event": event,
                             "detail": str(detail or "")[:300]})
        self.history = self.history[-50:]


def offer_task(*, org_id: str, agent: ExternalAgent, title: str,
               spec_text: str, created_by: str,
               acceptance: Optional[List[Dict[str, Any]]] = None,
               ) -> Dict[str, Any]:
    """Предложить задачу агенту. Только активному и только своему."""
    if agent.org_id != str(org_id):
        return {"ok": False, "error": "агент принадлежит другой организации"}
    if agent.status != AGENT_ACTIVE:
        return {"ok": False,
                "error": f"агент в статусе «{agent.status}» задач не получает"}
    if not str(title or "").strip() or len(str(spec_text or "").strip()) < 20:
        return {"ok": False,
                "error": "нужны название и содержательное задание (≥20 симв.)"}
    checks = _normalize_acceptance(acceptance)
    task = AgentTask(
        id=f"atask_{uuid.uuid4().hex[:12]}",
        org_id=str(org_id), agent_id=agent.id,
        title=str(title).strip()[:300],
        spec_text=str(spec_text).strip()[:20000],
        acceptance=checks,
        created_by=str(created_by or ""),
        created_at=_now(),
    )
    task._log("offered", f"проверок приёмки: {len(checks)}")
    return {"ok": True, "task": task}


def _normalize_acceptance(raw: Optional[List[Dict[str, Any]]]
                          ) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for c in raw or []:
        if not isinstance(c, dict):
            continue
        kind = str(c.get("kind") or "").strip().lower()
        if kind == "contains" and str(c.get("target") or "").strip():
            out.append({"kind": "contains",
                        "target": str(c["target"]).strip()[:300]})
        elif kind == "regex" and str(c.get("pattern") or "").strip():
            try:
                re.compile(str(c["pattern"]))
            except re.error:
                continue
            out.append({"kind": "regex",
                        "pattern": str(c["pattern"])[:300]})
        elif kind == "min_len":
            try:
                out.append({"kind": "min_len",
                            "n": max(1, int(c.get("n") or 0))})
            except (TypeError, ValueError):
                continue
    return out[:20]


def take_task(task: AgentTask, *, agent_id: str) -> Dict[str, Any]:
    """Агент берёт задачу. Только адресат."""
    if task.agent_id != agent_id:
        return {"ok": False, "error": "задача адресована другому агенту"}
    if task.status not in (TASK_OFFERED, TASK_RETURNED):
        return {"ok": False,
                "error": f"задачу в статусе «{task.status}» взять нельзя"}
    task.status = TASK_IN_PROGRESS
    task._log("taken")
    return {"ok": True, "task": task}


def verify_result(task: AgentTask) -> Dict[str, Any]:
    """Трёхисходная приёмка. «Нет доказательств ≠ готово»:
    без единой проверки результат не принимается автоматически."""
    text = task.result_text or ""
    if not task.acceptance:
        return {"verdict": INCONCLUSIVE,
                "checks": [],
                "note": "проверок приёмки нет — принять может только человек"}
    checks = []
    any_fail = False
    for c in task.acceptance:
        ok = False
        if c["kind"] == "contains":
            ok = c["target"].lower() in text.lower()
            detail = c["target"]
        elif c["kind"] == "regex":
            ok = bool(re.search(c["pattern"], text))
            detail = c["pattern"]
        else:  # min_len
            ok = len(text.strip()) >= c["n"]
            detail = f"длина ≥ {c['n']}"
        checks.append({"kind": c["kind"], "detail": detail,
                       "verdict": PASS if ok else FAIL})
        any_fail = any_fail or not ok
    return {"verdict": FAIL if any_fail else PASS, "checks": checks}


def submit_result(task: AgentTask, *, agent_id: str,
                  result_text: str) -> Dict[str, Any]:
    """Агент сдаёт результат → машинная приёмка сразу.

    pass → accepted (ждёт человека); fail → returned с конкретными
    замечаниями (какие проверки не прошли); inconclusive → тоже к
    человеку, но с пометкой «не доказано». После MAX_RETURNS возвратов
    автоматическая пересдача останавливается — дальше решает человек.
    """
    if task.agent_id != agent_id:
        return {"ok": False, "error": "задача адресована другому агенту"}
    if task.status != TASK_IN_PROGRESS:
        return {"ok": False,
                "error": f"сдать можно только взятую задачу "
                         f"(сейчас «{task.status}»)"}
    if len(str(result_text or "").strip()) < 1:
        return {"ok": False, "error": "пустой результат не принимается"}
    task.result_text = str(result_text)[:100000]
    task.submitted_at = _now()
    verdict = verify_result(task)
    task.verdict = verdict
    if verdict["verdict"] == FAIL:
        task.returns_count += 1
        if task.returns_count >= MAX_RETURNS:
            task.status = TASK_SUBMITTED
            task._log("submitted",
                      f"провал приёмки №{task.returns_count} — предел "
                      f"возвратов, дальше решает человек")
        else:
            task.status = TASK_RETURNED
            failed = [c["detail"] for c in verdict["checks"]
                      if c["verdict"] == FAIL]
            task._log("returned", "; ".join(failed)[:300])
    else:
        task.status = TASK_ACCEPTED if verdict["verdict"] == PASS \
            else TASK_SUBMITTED
        task._log("submitted", f"машинная приёмка: {verdict['verdict']}")
    return {"ok": True, "task": task, "verdict": verdict}


def close_task(task: AgentTask, *, closed_by: str,
               approve: bool) -> Dict[str, Any]:
    """Финальное решение человека. Машина отбирает — человек закрывает."""
    if task.status not in (TASK_ACCEPTED, TASK_SUBMITTED, TASK_RETURNED):
        return {"ok": False,
                "error": f"задачу в статусе «{task.status}» закрыть нельзя"}
    task.status = TASK_CLOSED if approve else TASK_CANCELLED
    task.closed_by = str(closed_by or "")
    task.closed_at = _now()
    task._log("closed" if approve else "cancelled")
    return {"ok": True, "task": task}
