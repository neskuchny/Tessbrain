# -*- coding: utf-8 -*-
"""Предиктивная сверка: решения встречи против целей (CogniLayer Ф4, финал).

Обычная сверка ловит рассинхрон, когда цели УЖЕ разъехались. Предиктивная —
раньше: в момент ингеста встречи каждое свежее решение сверяется с активными
целями компании/отделов тем же детерминированным скелетом (detect_pair):
метрика в противоположных направлениях = ранний сигнал «решение тянет против
цели», общий дефицитный ресурс = мягкая пометка.

Дисциплина:
- скелет, без LLM — на каждом ингесте это должно быть бесплатно и предсказуемо;
- уведомляется ТОЛЬКО high (противоположные направления одной метрики);
  soft-сигналы копятся для дашборда, но людей не дёргают (шум приучает
  игнорировать);
- дедуп теми же отпечатками, что у sync_notify: одно расхождение — одно
  уведомление;
- цели берутся только из goal_tracker владельца ингеста (его тенант).

Never-raise: сбой предиктивной сверки не трогает обработку встречи.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backend.core.think.cross_department_desync import detect_pair

logger = logging.getLogger(__name__)

_MAX_WARNINGS_STORED = 100
_MAX_DECISIONS_PER_MEETING = 20


def _decision_text(d: Dict[str, Any]) -> str:
    for k in ("decision_summary", "summary", "title", "text", "content"):
        v = str(d.get(k) or "").strip()
        if v:
            details = str(d.get("details") or "").strip()
            return (v + (" " + details if details else ""))[:400]
    return ""


def check_decisions_against_goals(
    decisions: List[Dict[str, Any]], goals: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Чистый скелет: каждое решение × каждая цель → ранние сигналы.

    goals: цели goal_tracker (level company/department). Возвращает список
    warning-конфликтов в общем формате sync_notify (kind=decision_goal)."""
    out: List[Dict[str, Any]] = []
    for d in (decisions or [])[:_MAX_DECISIONS_PER_MEETING]:
        if not isinstance(d, dict):
            continue
        dtext = _decision_text(d)
        if len(dtext) < 12:
            continue
        for g in (goals or []):
            gtext = str(g.get("title") or "").strip()
            if not gtext:
                continue
            c = detect_pair(dtext, gtext)
            if not c:
                continue
            level = str(g.get("level") or "")
            dept = str(g.get("department") or "")
            out.append({
                "kind": "decision_goal",
                "a": "решение встречи",
                "b": dept or ("компания" if level == "company" else level),
                "goal_a": dtext,
                "goal_b": gtext,
                "goal_level": level,
                "goal_id": str(g.get("id") or ""),
                **c,
            })
    return out


# ── хранение ранних сигналов (для дашборда) ──

def _warnings_path(user_id: str) -> str:
    from backend.core.store.tenant_paths import _DATA_ROOT, _require_uuid
    user_id = _require_uuid(user_id, "user_id")
    d = _DATA_ROOT / "sync_index"
    d.mkdir(parents=True, exist_ok=True)
    return str(d / f"{user_id}.predictive.json")


def _store_warnings(user_id: str, meeting_id: str,
                    warnings: List[Dict[str, Any]]) -> None:
    try:
        path = _warnings_path(user_id)
        items: List[dict] = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                items = (json.load(f) or {}).get("warnings") or []
        now = datetime.now(timezone.utc).isoformat()
        items.extend({**w, "meeting_id": meeting_id, "detected_at": now}
                     for w in warnings)
        from backend.core.store.tenant_io import atomic_write_json
        atomic_write_json(path, {"warnings": items[-_MAX_WARNINGS_STORED:]})
    except Exception:
        logger.debug("predictive: warnings store skipped", exc_info=True)


def recent_warnings(user_id: str, limit: int = 30) -> List[Dict[str, Any]]:
    try:
        path = _warnings_path(user_id)
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            items = (json.load(f) or {}).get("warnings") or []
        return list(reversed(items[-max(1, limit):]))
    except Exception:
        return []


# ── оркестратор (вызывается из ингеста встречи) ──

async def run_predictive_check(
    user_id: str, meeting_id: str, agent_results: Dict[str, Any],
) -> Dict[str, Any]:
    """Сверить решения встречи с активными целями. Never-raise.

    High-сигналы уведомляются через sync_notify (дедуп отпечатками там же);
    все сигналы копятся для дашборда (/sync/predictive)."""
    out = {"warnings": 0, "notified": 0}
    try:
        from backend.config import settings as _settings
        if not getattr(_settings, "cross_dept_desync_enabled", False):
            return out
        decisions = (agent_results or {}).get("decisions") or []
        if not decisions:
            return out
        goals: List[Dict[str, Any]] = []
        try:
            from backend.core.goals.goal_tracker import goal_store_for_user
            store = goal_store_for_user(user_id)
            goals = [g for g in store.list_goals(status="active")
                     if g.get("level") in ("company", "department")]
        except Exception:
            logger.debug("predictive: goals read skipped", exc_info=True)
        if not goals:
            return out

        warnings = check_decisions_against_goals(decisions, goals)
        if not warnings:
            return out
        out["warnings"] = len(warnings)
        _store_warnings(user_id, meeting_id, warnings)

        # уведомляем только high — «решение тянет против цели» по метрике
        high = [w for w in warnings if w.get("severity") == "high"]
        if high:
            from backend.core.think.sync_notify import notify_new_desyncs
            r = await notify_new_desyncs(
                user_id, {"predictive": {"conflicts": high}})
            out["notified"] = int(r.get("recipients") or 0)
    except Exception:
        logger.warning("predictive sync check failed", exc_info=True)
    return out


if __name__ == "__main__":  # смоук чистого скелета
    goals = [
        {"id": "d1", "title": "Сократить время отклика поддержки",
         "level": "department", "department": "Поддержка"},
        {"id": "c1", "title": "Увеличить выручку до 100 млн",
         "level": "company", "department": ""},
    ]
    decisions = [
        {"decision_summary": "Увеличить время отклика ботов ради экономии",
         "details": "переводим поддержку на дешёвый тариф"},
        {"decision_summary": "Запустить новую рекламную кампанию"},
        {"decision_summary": "ок"},  # слишком коротко — пропуск
    ]
    ws = check_decisions_against_goals(decisions, goals)
    assert any(w["severity"] == "high" and "отклик" in w["goal_b"].lower()
               for w in ws), ws  # решение тянет против цели поддержки
    assert all(w["kind"] == "decision_goal" for w in ws)
    assert not any("ок" == w["goal_a"] for w in ws)
    # решение без пересечения метрик с целью выручки — конфликта нет
    assert not any(w["goal_id"] == "c1" and w["severity"] == "high"
                   for w in ws), ws
    print("predictive_sync smoke OK")
