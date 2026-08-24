# -*- coding: utf-8 -*-
"""
Ingest webhooks от Daily Pulse — INTEGRATION_TESSENT.md Фаза 1.

Принимаем 5 типов событий (standup.processed, chat_summary.daily,
blocker.raised, weekly_report.generated, sprint.analyzed) с HMAC-
подписью. Каждое событие:
1) проверяется HMAC-подписью; невалидная → 401 (но сырое всё равно
   пишем в integration_events с signature_ok=false для отладки);
2) external_id → tessent_user_id через identity-маппинг (по email
   при первом обращении);
3) проецируется в наши примитивы:
   - event_log (актор + kind, payload с пометкой provenance);
   - факты сильных сигналов (blocker.raised) → лента инсайтов;
   - КАЖДЫЙ артефакт с пометкой _source=measured|llm_narrative|human
     (защита от LLM-on-LLM — их §4.2).

Безопасно для прежней системы по построению: всё за флагом
enable_daily_pulse_integration, любая ошибка обработки логируется и не
ломает API; agent_log/insights/integration_events работают независимо.
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import logging
import os
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

EVENT_KINDS = ("standup.processed", "chat_summary.daily", "blocker.raised",
               "weekly_report.generated", "sprint.analyzed")


# ──────────────────────────────────────────────────────────────────────
# HMAC подпись (чистая функция, тестируется без HTTP)
# ──────────────────────────────────────────────────────────────────────

def compute_signature(secret: str, raw_body: bytes) -> str:
    """sha256-HMAC от сырого тела; формат `sha256=<hex>`."""
    mac = hmac.new(str(secret).encode("utf-8"), raw_body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def verify_signature(secret: Optional[str], raw_body: bytes,
                     header: Optional[str]) -> bool:
    if not secret or not header:
        return False
    try:
        return hmac.compare_digest(compute_signature(secret, raw_body),
                                   str(header).strip())
    except Exception:
        return False


def secret_from_env() -> Optional[str]:
    return os.environ.get("DAILY_PULSE_HMAC_SECRET") or None


# ──────────────────────────────────────────────────────────────────────
# Чистый проектор: событие → нормализованный артефакт
# (тестируется без БД — врезка в сторы отдельным шагом)
# ──────────────────────────────────────────────────────────────────────

def _provenance(event: str, payload: dict) -> dict:
    """_source по событию: measured / llm_narrative / human."""
    base = {"source": "daily_pulse", "event": event,
            "received_at": datetime.datetime.now(
                datetime.timezone.utc).isoformat()}
    if event == "weekly_report.generated":
        # их методология: числа measured, нарратив llm_narrative
        # передаём оба сегмента, получатель не должен использовать
        # llm_narrative как ground truth
        base["_source"] = payload.get("_source") or "mixed"
    elif event in ("standup.processed", "chat_summary.daily"):
        base["_source"] = "llm_narrative"
    else:
        base["_source"] = "measured"
    return base


def project_event(event: str, payload: dict) -> Dict[str, Any]:
    """Спроецировать событие на наши примитивы. Возвращает
    {events: [для event_log], insights: [для ленты], errors: [...]}.

    Никаких сайд-эффектов — только структурные артефакты."""
    out: Dict[str, Any] = {"events": [], "insights": [], "errors": []}
    if event not in EVENT_KINDS:
        out["errors"].append(f"event {event!r} вне известных {EVENT_KINDS}")
        return out
    prov = _provenance(event, payload)
    user_ref = str(payload.get("user_ref") or payload.get("user") or "")
    team_ref = str(payload.get("team_ref") or "")
    at = str(payload.get("ts") or payload.get("date") or "")

    if event == "standup.processed":
        out["events"].append({
            "actor": user_ref,
            "kind": "standup",
            "at": at,
            "payload": {**payload.get("summary", {}),
                        "team": team_ref,
                        "provenance": prov},
        })
    elif event == "chat_summary.daily":
        s = payload.get("summary") or {}
        out["events"].append({
            "actor": team_ref or "team",
            "kind": "chat_digest",
            "at": at,
            "payload": {"tldr": s.get("tldr"),
                        "topics": s.get("topics") or [],
                        "decisions": s.get("decisions") or [],
                        "sentiment": s.get("sentiment"),
                        "provenance": prov},
        })
        # решения встреч/чата сами по себе — отдельные события
        for d in s.get("decisions") or []:
            out["events"].append({
                "actor": team_ref or "team", "kind": "decision_from_chat",
                "at": at, "payload": {"text": str(d.get("text") or d),
                                      "provenance": prov}})
        # task_mentions: сигналы по задачам с evidence
        for tm in s.get("task_mentions") or []:
            out["events"].append({
                "actor": str(tm.get("user") or user_ref or ""),
                "kind": f"task_signal:{tm.get('signal', 'discussed')}",
                "at": at,
                "payload": {"task": tm.get("task"),
                            "evidence": tm.get("evidence"),
                            "provenance": prov},
            })
    elif event == "blocker.raised":
        sev = str(payload.get("severity") or "medium").lower()
        out["events"].append({
            "actor": user_ref, "kind": "blocker", "at": at,
            "payload": {"task": payload.get("task"),
                        "reason": payload.get("reason"),
                        "days_open": payload.get("days_open"),
                        "severity": sev, "provenance": prov},
        })
        # severity ≥ medium → в инсайты (их предложение)
        if sev in ("medium", "high", "critical"):
            out["insights"].append({
                "insight_id": _stable_id("blocker", user_ref,
                                         payload.get("task"), at),
                "title": f"Блокер у {user_ref}: {payload.get('reason', '')[:80]}",
                "description": (f"Задача «{payload.get('task', '')}», "
                                f"висит дней: {payload.get('days_open', '?')}"),
                "priority": "high" if sev in ("high", "critical") else "medium",
                "source": "daily_pulse",
                "stored_at": prov["received_at"],
                "provenance": prov,
            })
    elif event == "weekly_report.generated":
        out["events"].append({
            "actor": user_ref, "kind": "weekly_report", "at": at,
            "payload": {"metrics": payload.get("performance_metrics") or {},
                        "narrative": payload.get("narrative"),
                        "provenance": prov},
        })
    elif event == "sprint.analyzed":
        out["events"].append({
            "actor": team_ref or "team", "kind": "sprint_analysis", "at": at,
            "payload": {**payload, "provenance": prov},
        })
    return out


def _stable_id(*parts) -> str:
    canon = "|".join(str(p) for p in parts)
    return hashlib.sha1(canon.encode("utf-8", errors="ignore")).hexdigest()[:16]


# ──────────────────────────────────────────────────────────────────────
# БД-слой: лог сырого + врезки в наши сторы (best-effort)
# ──────────────────────────────────────────────────────────────────────

async def store_raw_event(source: str, event: str, payload: dict, *,
                          signature_ok: bool) -> None:
    """Сырой лог в integration_events (для отладки/реплея)."""
    try:
        import json as _json

        from sqlalchemy import text

        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session() as session:
            await session.execute(
                text("INSERT INTO integration_events "
                     "(source, event, org_ref, team_ref, user_ref, "
                     "occurred_at, payload, signature_ok) VALUES "
                     "(:s, :e, :org, :tm, :u, CAST(:t AS timestamptz), "
                     "CAST(:p AS jsonb), :sig)"),
                {"s": source, "e": event,
                 "org": str(payload.get("org_ref") or ""),
                 "tm": str(payload.get("team_ref") or ""),
                 "u": str(payload.get("user_ref") or ""),
                 "t": payload.get("ts") or
                      datetime.datetime.now(datetime.timezone.utc).isoformat(),
                 "p": _json.dumps(payload, ensure_ascii=False),
                 "sig": bool(signature_ok)})
    except Exception as e:
        logger.debug(f"store_raw_event skipped: {e}")


async def apply_projection(tessent_user_id: Optional[str],
                           projection: Dict[str, Any]) -> Tuple[int, int]:
    """Раскидать спроецированные артефакты по наших сторам.
    Возвращает (events_pushed, insights_pushed)."""
    pushed_events = pushed_insights = 0
    # event_log (per-user — если есть привязанный user, иначе пропускаем)
    if tessent_user_id:
        try:
            from backend.core.memory.event_log import EventLog
            from backend.core.store.tenant_paths import (
                event_log_path_for_user,
            )
            log = EventLog(persist_path=event_log_path_for_user(tessent_user_id))
            for ev in projection.get("events") or []:
                # перезаписываем актора как tessent_user_id (унификация)
                ev = {**ev, "actor": tessent_user_id}
                log.record(actor=ev["actor"], kind=ev["kind"],
                           at=ev.get("at") or None,
                           payload=ev.get("payload") or {})
                pushed_events += 1
        except Exception as e:
            logger.debug(f"event_log push skipped: {e}")
        # insights в общий per-user стор
        try:
            from backend.core.sleep.insight_store import InsightStore
            from backend.core.store.tenant_paths import insights_path_for_user
            ins = InsightStore(persist_path=insights_path_for_user(tessent_user_id))
            if projection.get("insights"):
                r = ins.add(projection["insights"])
                pushed_insights = r.get("added", 0)
        except Exception as e:
            logger.debug(f"insights push skipped: {e}")
    return pushed_events, pushed_insights


def detect_stalled_work(events: list, *, min_blocked_signals: int = 2
                        ) -> list:
    """Outcome-сигнал «работа не движется»: задача, по которой накопились
    ≥ min_blocked_signals сигналов blocked/help_needed БЕЗ последующего
    done/progress. Детерминированно, без LLM.

    events — список dict-событий из event_log (kind, at, payload).
    Возвращает инсайты для ленты (provenance _source=measured: это
    подсчёт сигналов, не LLM-вывод)."""
    by_task: Dict[str, list] = {}
    for d in sorted(events or [], key=lambda e: str(e.get("at") or "")):
        kind = str(d.get("kind") or "")
        pl = d.get("payload") or {}
        if kind == "blocker":
            task = str(pl.get("task") or "")
            if task:
                by_task.setdefault(task, []).append(("blocked", d))
        elif kind.startswith("task_signal:"):
            sig = kind.split(":", 1)[1]
            task = str(pl.get("task") or "")
            if task:
                by_task.setdefault(task, []).append((sig, d))

    out = []
    for task, seq in by_task.items():
        # ищем хвост после последнего done/descoped — он «закрывает» задачу
        last_close = max((i for i, (s, _) in enumerate(seq)
                          if s in ("done", "descoped")), default=-1)
        tail = seq[last_close + 1:]
        bad = [d for s, d in tail if s in ("blocked", "help_needed")]
        if len(bad) >= min_blocked_signals:
            first_at = str(bad[0].get("at") or "")
            actor = str((bad[-1].get("payload") or {}).get("user")
                        or bad[-1].get("actor") or "")
            out.append({
                "insight_id": _stable_id("stalled", task, first_at),
                "title": f"Задача «{task}» застряла: {len(bad)} сигналов "
                         f"блокировки без прогресса",
                "description": (f"С {first_at[:10]} по задаче только блокеры/"
                                f"help_needed, ни done, ни progress."),
                "priority": "high" if len(bad) >= 3 else "medium",
                "source": "daily_pulse",
                "stored_at": datetime.datetime.now(
                    datetime.timezone.utc).isoformat(),
                "provenance": {"source": "daily_pulse",
                               "event": "stalled_work_detector",
                               "_source": "measured"},
                "actor": actor,
            })
    return out


def integration_enabled() -> bool:
    try:
        from backend.core.config.feature_flags import get_feature_flags
        return bool(get_feature_flags().enable_daily_pulse_integration)
    except Exception:
        return False
