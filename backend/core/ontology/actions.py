# -*- coding: utf-8 -*-
"""Ontology Actions — типизированный контракт изменений (P4).

До P4 «действия» (write-back в Jira/Notion/граф) были разрозненным
fire-and-forget: `action_data: Dict[str, Any]` без схемы, исполнение без
предвалидации и без понятного результата. Это вторая половина пробела
«Ontology SDK + Actions».

P4 вводит ЕДИНЫЙ контракт:

  Action{kind, target, payload}
        │
        ├─ A. валидируем ВЕСЬ набор по онтологии (pseudo-transaction:
        │     сначала проверка всего, потом запись — как P3 ingest)
        ├─ B. в STRICT — ни одного жёсткого нарушения, иначе abort до записи
        └─ C. выполняем через инъектируемый executor, собираем ActionReport
              (частичный успех допустим, как и везде в think/store-слое)

Дизайн (как P0–P3):
- чистый, всё инъектируемо (executor, audit) → юнит-тест без графа/БД
- никогда не raises
- bounded — нельзя прислать неограниченный батч
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Optional

from .sdk import EnforcementMode, validate_object, validate_relationship

logger = logging.getLogger(__name__)

# executor(kind, target, payload) -> bool
ActionExecutorFn = Callable[..., Awaitable[bool]]
# audit(event, detail) -> None
AuditFn = Callable[..., Awaitable[None]]

# поддерживаемые виды действий
ACTION_CREATE_NODE = "create_node"
ACTION_CREATE_RELATIONSHIP = "create_relationship"
ACTION_WRITEBACK = "writeback"  # внешняя система (Jira/Notion/…), payload свободный
_KNOWN_KINDS = {ACTION_CREATE_NODE, ACTION_CREATE_RELATIONSHIP, ACTION_WRITEBACK}


@dataclass
class Action:
    """Одно типизированное изменение.

    kind=create_node:        target=label,  payload=props
    kind=create_relationship target="From->To" labels via payload
                             payload={from_label,to_label,rel_type,...}
    kind=writeback:          target=integration name, payload свободный
    """
    kind: str
    target: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionOutcome:
    index: int
    kind: str
    target: str
    applied: bool
    reason: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class ActionReport:
    applied: int = 0
    rejected: int = 0
    outcomes: list[ActionOutcome] = field(default_factory=list)
    aborted: bool = False
    status_code: int = 200

    @property
    def ok(self) -> bool:
        return self.status_code == 200 and not self.aborted

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "rejected": self.rejected,
            "aborted": self.aborted,
            "status_code": self.status_code,
            "outcomes": [
                {
                    "index": o.index, "kind": o.kind, "target": o.target,
                    "applied": o.applied, "reason": o.reason,
                    "warnings": o.warnings,
                }
                for o in self.outcomes
            ],
        }


def _validate_one(a: Action, mode: EnforcementMode) -> tuple[bool, str, list[str], dict[str, Any]]:
    """(ok, reason, warnings, normalized_payload) — никогда не raises."""
    if a.kind not in _KNOWN_KINDS:
        return False, f"unknown action kind '{a.kind}'", [], dict(a.payload)

    if a.kind == ACTION_CREATE_NODE:
        r = validate_object(a.target, a.payload or {}, mode=mode)
        reason = "; ".join(r.violations) if r.violations else ""
        return r.ok, reason, r.warnings, r.normalized

    if a.kind == ACTION_CREATE_RELATIONSHIP:
        p = a.payload or {}
        r = validate_relationship(
            str(p.get("from_label", "")),
            str(p.get("to_label", "")),
            str(p.get("rel_type", "")),
            mode=mode,
        )
        reason = "; ".join(r.violations) if r.violations else ""
        return r.ok, reason, r.warnings, dict(p)

    # writeback — онтология не описывает внешние системы; проверяем
    # только базовую форму (есть target и payload-объект).
    if not a.target:
        return False, "writeback action without target", [], dict(a.payload)
    if not isinstance(a.payload, dict):
        return False, "writeback payload must be an object", [], {}
    return True, "", [], dict(a.payload)


async def apply_actions(
    actions: Any,
    *,
    executor: ActionExecutorFn,
    mode: EnforcementMode = EnforcementMode.STRICT,
    audit: Optional[AuditFn] = None,
    max_actions: int = 200,
) -> ActionReport:
    """Применить набор действий. Никогда не raises.

    Pseudo-transaction: в STRICT сначала валидируем ВСЁ; если есть хоть
    одно жёсткое нарушение — abort до любой записи. В WARN пишем всё,
    логируя предупреждения.

    Args:
        actions: список Action (или dict'ов с теми же полями).
        executor: async (kind, target, payload) -> bool.
        mode: строгость онтологии (STRICT по умолчанию — Actions это
            управляемая граница, в отличие от грязной extraction).
        audit: async (event, detail) -> None, best-effort.
        max_actions: верхний предел батча.
    """
    report = ActionReport()

    async def _safe_audit(event: str, detail: dict[str, Any]) -> None:
        if audit is None:
            return
        try:
            await audit(event=event, detail=detail)
        except Exception as exc:
            logger.debug("ontology.actions: audit failed: %s", exc)

    if not isinstance(actions, list) or not actions:
        report.status_code = 400
        await _safe_audit("ontology.actions.empty", {})
        return report

    # нормализуем вход (dict → Action), bounded
    norm: list[Action] = []
    for raw in actions[:max_actions]:
        if isinstance(raw, Action):
            norm.append(raw)
        elif isinstance(raw, dict):
            norm.append(Action(
                kind=str(raw.get("kind", "")),
                target=str(raw.get("target", "")),
                payload=raw.get("payload") if isinstance(raw.get("payload"), dict) else {},
            ))
        else:
            norm.append(Action(kind="", target="", payload={}))

    # A. валидация всего набора
    validated: list[tuple[Action, bool, str, list[str], dict[str, Any]]] = []
    hard_fail = False
    for a in norm:
        ok, reason, warns, payload = _validate_one(a, mode)
        validated.append((a, ok, reason, warns, payload))
        if not ok:
            hard_fail = True

    # B. STRICT + есть нарушения → abort до записи
    if mode == EnforcementMode.STRICT and hard_fail:
        report.aborted = True
        report.status_code = 422
        for i, (a, ok, reason, warns, _p) in enumerate(validated):
            report.outcomes.append(ActionOutcome(
                index=i, kind=a.kind, target=a.target,
                applied=False,
                reason=reason or ("ok, but batch aborted" if ok else reason),
                warnings=warns,
            ))
            if not ok:
                report.rejected += 1
        await _safe_audit("ontology.actions.aborted", {
            "total": len(norm),
            "violations": [r for *_x, r, _w, _p in
                           [(v[0], v[1], v[2], v[3], v[4]) for v in validated] if r],
        })
        return report

    # C. исполнение (частичный успех допустим)
    for i, (a, ok, reason, warns, payload) in enumerate(validated):
        if not ok:
            report.rejected += 1
            report.outcomes.append(ActionOutcome(
                index=i, kind=a.kind, target=a.target,
                applied=False, reason=reason, warnings=warns,
            ))
            continue
        try:
            done = await executor(kind=a.kind, target=a.target, payload=payload)
        except Exception as exc:
            logger.debug("ontology.actions: executor failed idx=%s: %s", i, exc)
            done = False
        if done:
            report.applied += 1
        else:
            report.rejected += 1
        report.outcomes.append(ActionOutcome(
            index=i, kind=a.kind, target=a.target,
            applied=bool(done),
            reason="" if done else "executor rejected",
            warnings=warns,
        ))

    await _safe_audit("ontology.actions.applied", {
        "applied": report.applied, "rejected": report.rejected,
        "mode": mode.value,
    })
    return report


__all__ = [
    "ACTION_CREATE_NODE",
    "ACTION_CREATE_RELATIONSHIP",
    "ACTION_WRITEBACK",
    "Action",
    "ActionOutcome",
    "ActionReport",
    "apply_actions",
]
