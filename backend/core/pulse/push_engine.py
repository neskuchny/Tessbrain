# -*- coding: utf-8 -*-
"""Пуш-движок «Пульса»: напоминания исполнителям как у хорошего менеджера.

Правила (docs/EXECUTION_PULSE.md §6):
- вопрос, а не «смени статус»: «как движется? ✅ готово · 🏃 в работе ·
  🧱 упёрся · 📅 нужен перенос»;
- гигиена: дедуп по отпечатку (задача+сигнал), каденция по типу сигнала
  (просрочка — раз в 3 дня, «скоро дедлайн» — один раз, «висит» — раз в
  2 недели), бюджет пушей человеку за прогон, БАТЧИНГ — все задачи человека
  одним сообщением;
- тихие часы и mute уважаются; человек без канала не теряется — попадает в
  список «не знаю, кому напоминать» для владельца;
- эскалация: просрочка, по которой напоминали ≥2 раз без смены статуса, —
  мягкое сообщение руководителю (reports_to) с историей, один раз в неделю.

Планирование — чистая функция (тестируется без сети); отправка — за флагом
ENABLE_EXECUTION_PULSE_PUSH (default OFF, fail-closed): рассылка сотрудникам —
внешне видимое действие, включается осознанно. dry_run отдаёт план без
единой отправки — для предпросмотра.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.core.pulse.people_registry import load_registry, resolve

logger = logging.getLogger(__name__)

# Каденция повторов по типу сигнала (дней между пушами по одной задаче)
CADENCE_DAYS = {"overdue": 3, "blocked": 4, "deadline_soon": 9999,
                "stale": 14}
BUDGET_PER_PERSON = 3          # задач в одном сообщении за прогон
ESCALATE_AFTER_PUSHES = 2      # напоминаний без реакции → руководителю
ESCALATE_REPEAT_DAYS = 7

_QUESTION_FOOTER = ("\nОтветь одним словом или парой фраз: ✅ готово · "
                    "🏃 в работе · 🧱 упёрся (во что?) · 📅 нужен перенос "
                    "(какой срок?)")


def push_enabled() -> bool:
    """Персональные напоминания исполнителям. Включены по умолчанию по
    решению владельца продукта (2026-08-13): антиспам-механика (каденция по
    типу сигнала, бюджет 3 задачи, тихие часы, mute) построена именно для
    того, чтобы пуши можно было держать включёнными. Выключается
    ENABLE_EXECUTION_PULSE_PUSH=off."""
    return os.environ.get("ENABLE_EXECUTION_PULSE_PUSH", "on").strip().lower() \
        in ("1", "on", "true", "yes")


def _state_path(user_id: str) -> Path:
    base = Path(os.environ.get("PULSE_PUSH_DIR", "").strip()
                or "data/pulse_push")
    return base / f"{user_id}.json"


def _load_push_state(user_id: str) -> Dict[str, Any]:
    try:
        p = _state_path(user_id)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.warning("push state unreadable", exc_info=True)
    return {}


def _save_push_state(user_id: str, state: Dict[str, Any]) -> None:
    p = _state_path(user_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def _fp(task: Dict[str, Any], signal: str) -> str:
    key = (task.get("_pulse") or {}).get("key") or task.get("title") or ""
    extra = (task.get("_pulse") or {}).get("deadline") or "" \
        if signal == "deadline_soon" else ""
    return f"{signal}:{key}:{extra}"


def _days_since_iso(iso: str, now: datetime) -> float:
    try:
        return (now - datetime.fromisoformat(iso)).total_seconds() / 86400
    except Exception:
        return 1e9


def _in_quiet_hours(rec: Dict[str, Any], now: datetime) -> bool:
    qh = rec.get("quiet_hours")
    if not qh or len(qh) != 2:
        return False
    start, end = int(qh[0]), int(qh[1])
    h = now.hour
    return (start <= h < end) if start <= end else (h >= start or h < end)


def _task_line(task: Dict[str, Any], signal: str) -> str:
    title = str(task.get("title") or "задача").strip()[:90]
    if signal == "overdue":
        return f"• «{title}» — срок прошёл {task.get('_overdue_days', '?')} дн. назад. Как движется?"
    if signal == "deadline_soon":
        dl = (task.get("_pulse") or {}).get("deadline") or ""
        return f"• «{title}» — дедлайн {dl}. Успеваешь?"
    if signal == "blocked":
        return f"• «{title}» — стоит как заблокированная. Что мешает, чем помочь?"
    if signal == "stale":
        return f"• «{title}» — {task.get('_idle_days', '?')} дн. без движения. Она ещё актуальна?"
    return f"• «{title}»"


def plan_pushes(analysis: Dict[str, Any],
                registry: Dict[str, Dict[str, Any]],
                push_state: Dict[str, Any], *,
                now: Optional[datetime] = None,
                budget_per_person: int = BUDGET_PER_PERSON) -> Dict[str, Any]:
    """Чистое планирование: сигналы + реестр + история → кому что отправить.

    Возврат: {"pushes": [...], "escalations": [...], "unmapped": [...],
              "skipped": {...}} — без побочных эффектов; фиксация отправки
    происходит только после успешной доставки."""
    now = now or datetime.now(timezone.utc)
    sent: Dict[str, Any] = push_state.get("sent", {})
    escalated: Dict[str, Any] = push_state.get("escalated", {})

    per_person: Dict[str, Dict[str, Any]] = {}
    unmapped: Dict[str, List[str]] = {}
    skipped = {"muted": 0, "quiet": 0, "cadence": 0, "budget": 0}

    for signal in ("overdue", "blocked", "deadline_soon", "stale"):
        for task in analysis["signals"].get(signal, []):
            assignee = str(task.get("assignee")
                           or task.get("assignee_name") or "").strip()
            rec = resolve(registry, assignee) if assignee else None
            if rec is None or not (rec.get("telegram_chat_id")
                                   or rec.get("email")):
                if assignee:
                    unmapped.setdefault(assignee, []).append(
                        str(task.get("title") or ""))
                continue
            if rec.get("muted"):
                skipped["muted"] += 1
                continue
            if _in_quiet_hours(rec, now):
                skipped["quiet"] += 1
                continue
            fp = _fp(task, signal)
            prev = sent.get(fp)
            if prev and _days_since_iso(prev.get("last_sent", ""), now) < \
                    CADENCE_DAYS.get(signal, 9999):
                skipped["cadence"] += 1
                continue

            bucket = per_person.setdefault(rec["person_key"], {
                "person": rec, "lines": [], "fingerprints": [], "signals": []})
            if len(bucket["lines"]) >= budget_per_person:
                skipped["budget"] += 1
                continue
            bucket["lines"].append(
                f"{len(bucket['lines']) + 1}. " + _task_line(task, signal).lstrip("• "))
            bucket["fingerprints"].append(fp)
            bucket["signals"].append(signal)
            bucket.setdefault("tasks", []).append(task)

            # эскалация: по этой задаче уже напоминали, статус не сдвинулся
            if signal == "overdue" and prev and \
                    int(prev.get("count", 0)) >= ESCALATE_AFTER_PUSHES:
                efp = f"esc:{fp}"
                eprev = escalated.get(efp)
                if not eprev or _days_since_iso(
                        eprev.get("last_sent", ""), now) >= ESCALATE_REPEAT_DAYS:
                    manager = registry.get(rec.get("reports_to") or "")
                    per_person.setdefault("__escalations__", {
                        "items": []})["items"] = \
                        per_person.get("__escalations__", {}).get("items", [])
                    per_person["__escalations__"]["items"].append({
                        "fingerprint": efp,
                        "task_title": task.get("title"),
                        "assignee": assignee,
                        "pushes": int(prev.get("count", 0)),
                        "manager": manager,
                    })

    escalations = (per_person.pop("__escalations__", {}) or {}).get("items", [])

    pushes = []
    for pkey, b in per_person.items():
        rec = b["person"]
        name = (rec.get("names") or [pkey])[0]
        text = (f"Привет, {name}! Короткая сверка по задачам:\n"
                + "\n".join(b["lines"]) + _QUESTION_FOOTER)
        channel = "telegram" if rec.get("telegram_chat_id") else "email"
        pushes.append({"person_key": pkey, "name": name, "channel": channel,
                       "to": rec.get("telegram_chat_id") or rec.get("email"),
                       "text": text, "fingerprints": b["fingerprints"],
                       "tasks": b.get("tasks", [])})

    return {"pushes": pushes, "escalations": escalations,
            "unmapped": [{"assignee": a, "tasks": ts[:5]}
                         for a, ts in unmapped.items()],
            "skipped": skipped}


async def _send_telegram(user_id: str, chat_id: str, text: str) -> bool:
    """Прямая отправка ботом тенанта (resolve_telegram_bot_token)."""
    try:
        from backend.core.messengers.links import resolve_telegram_bot_token
        from backend.core.notifications.telegram_format import (
            post_telegram_text,
        )
        token = await resolve_telegram_bot_token(user_id)
        if not token:
            return False
        # markdown → Telegram-HTML с фолбэком в чистый текст (общий хелпер)
        res = await post_telegram_text(token, chat_id, text)
        return bool(res["ok"])
    except Exception:
        logger.warning("pulse push: telegram send failed", exc_info=True)
        return False


async def _send_email(to: str, text: str) -> bool:
    try:
        from backend.core.notifications.email import EmailMessage, send_email
        res = await send_email(EmailMessage(
            to=to, subject="Сверка по задачам", text_body=text))
        return bool(getattr(res, "ok", False))
    except Exception:
        logger.warning("pulse push: email send failed", exc_info=True)
        return False


async def send_pushes(user_id: str, *, dry_run: bool = False
                      ) -> Dict[str, Any]:
    """Полный цикл: реестр задач → сигналы → план → доставка → фиксация.

    Флаг выключен → честный refused (кроме dry_run — предпросмотр разрешён
    всегда, он ничего не отправляет)."""
    from backend.core.pulse.detectors import detect_signals
    from backend.core.pulse.ledger import build_ledger

    if not dry_run and not push_enabled():
        return {"status": "disabled",
                "message": ("Пуши сотрудникам выключены "
                            "(ENABLE_EXECUTION_PULSE_PUSH). Предпросмотр — "
                            "dry_run=true.")}

    ledger = await build_ledger(user_id)
    analysis = detect_signals(ledger["tasks"])
    registry = load_registry(user_id)
    state = _load_push_state(user_id)
    now = datetime.now(timezone.utc)

    plan = plan_pushes(analysis, registry, state, now=now)
    if dry_run:
        return {"status": "dry_run", **plan}

    delivered, failed = [], []
    for p in plan["pushes"]:
        ok = (await _send_telegram(user_id, p["to"], p["text"])
              if p["channel"] == "telegram" else
              await _send_email(p["to"], p["text"]))
        (delivered if ok else failed).append(
            {"person": p["name"], "channel": p["channel"]})
        if ok:
            if p["channel"] == "telegram":
                try:
                    from backend.core.pulse.inbound import record_chat_context
                    record_chat_context(p["to"], user_id, p["person_key"],
                                        p.get("tasks") or [])
                except Exception:
                    logger.debug("chat context not recorded", exc_info=True)
            for fp in p["fingerprints"]:
                prev = state.setdefault("sent", {}).get(fp) or {}
                state["sent"][fp] = {
                    "last_sent": now.isoformat(),
                    "count": int(prev.get("count", 0)) + 1}

    # эскалации — руководителю, если у него есть канал; иначе владельцу
    escalated_sent = []
    for e in plan["escalations"]:
        mgr = e.get("manager") or {}
        text = (f"⚠️ По задаче «{e['task_title']}» ({e['assignee']}) "
                f"напоминали {e['pushes']} раз(а) — движения нет. Возможно, "
                "нужно вмешаться: блокер, перегруз или задача неактуальна.")
        ok = False
        if mgr.get("telegram_chat_id"):
            ok = await _send_telegram(user_id, mgr["telegram_chat_id"], text)
        elif mgr.get("email"):
            ok = await _send_email(mgr["email"], text)
        if not ok:
            try:
                from backend.core.help.advice_push import notify_user
                res = await notify_user(user_id,
                                        subject="Пульс: нужна эскалация",
                                        text=text)
                ok = bool(res.get("telegram") or res.get("email"))
            except Exception:
                ok = False
        if ok:
            state.setdefault("escalated", {})[e["fingerprint"]] = {
                "last_sent": now.isoformat()}
            escalated_sent.append(e["task_title"])

    _save_push_state(user_id, state)
    logger.info("🫀 pulse push: %d доставлено, %d сбоев, %d эскалаций, "
                "%d без канала", len(delivered), len(failed),
                len(escalated_sent), len(plan["unmapped"]))
    return {"status": "success", "delivered": delivered, "failed": failed,
            "escalated": escalated_sent, "unmapped": plan["unmapped"],
            "skipped": plan["skipped"]}
