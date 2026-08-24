# -*- coding: utf-8 -*-
"""Проактивные уведомления о рассинхроне (CogniLayer Ф2, шаг 3 — «Фаза 4»
документа, первый инкремент).

При появлении НОВОГО значимого рассинхрона (hard, либо soft, подтверждённый
судьёй) — уведомить нужных людей: вовлечённые стороны конфликта + владелец
отчёта (генеральный). Формулировка адаптируется под фрейм получателя через
cogni-adapt (Часть II): меняется ФОРМА, факты конфликта неизменны; пустой
фрейм → базовый текст без выдуманной «персональности».

Дедуп — по отпечатку конфликта (kind|стороны|тексты целей), хранится
per-owner в data/sync_index/<uid>.notified.json: одно и то же расхождение не
долбит людей при каждом открытии дашборда. Доставка: сигнал в UI
(publish_signal) + Telegram best-effort. Never-raise: сбой уведомлений не
ломает отчёт.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Максимум получателей одного прогона — уведомление, а не рассылка.
_MAX_RECIPIENTS = 6
# Максимум конфликтов в одном уведомлении (свежих).
_MAX_CONFLICTS = 5


def _fingerprint(c: Dict[str, Any]) -> str:
    raw = "|".join(str(c.get(k) or "").strip().lower()
                   for k in ("kind", "a", "b", "goal_a", "goal_b"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _notified_path(owner_uid: str) -> str:
    from backend.core.store.tenant_paths import _DATA_ROOT, _require_uuid
    owner_uid = _require_uuid(owner_uid, "user_id")
    d = _DATA_ROOT / "sync_index"
    d.mkdir(parents=True, exist_ok=True)
    return str(d / f"{owner_uid}.notified.json")


def _load_notified(owner_uid: str) -> set:
    try:
        p = _notified_path(owner_uid)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return set((json.load(f) or {}).get("fingerprints") or [])
    except Exception:
        logger.debug("sync_notify: notified load skipped", exc_info=True)
    return set()


def _save_notified(owner_uid: str, fps: set) -> None:
    try:
        from backend.core.store.tenant_io import atomic_write_json
        atomic_write_json(_notified_path(owner_uid),
                          {"fingerprints": sorted(fps)[-400:],
                           "updated_at": datetime.now(timezone.utc).isoformat()})
    except Exception:
        logger.debug("sync_notify: notified save skipped", exc_info=True)


def significant_conflicts(levels: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Hard-конфликты + soft, подтверждённые судьёй. Отклонённые — никогда.
    Итерирует ВСЕ уровни dict'а (включая predictive из предиктивной сверки)."""
    out: List[Dict[str, Any]] = []
    for key in (levels or {}):
        if not isinstance(levels.get(key), dict):
            continue
        for c in ((levels.get(key) or {}).get("conflicts") or []):
            verdict = (c.get("judge") or {}).get("verdict")
            if verdict == "rejected":
                continue
            if c.get("severity") == "high" or verdict == "confirmed":
                out.append(c)
    return out


def _resolve_recipients(
    conflicts: List[Dict[str, Any]], owner_uid: str,
    acc_names: Dict[str, str], members: List[dict],
) -> Dict[str, List[Dict[str, Any]]]:
    """uid → конфликты, в которых человек — сторона. Владелец отчёта получает
    все. Сторона резолвится честно: точное имя аккаунта либо руководитель
    отдела-стороны; не резолвится — просто не уведомляем (без догадок)."""
    name_to_uid = {str(n).strip().lower(): uid for uid, n in acc_names.items()}
    dept_mgr: Dict[str, str] = {}
    try:
        from backend.core.think.org_sync import detect_managers
        for m in detect_managers(members):
            d = str(m.get("department") or "").strip().lower()
            if d and m.get("user_id") and d not in dept_mgr:
                dept_mgr[d] = m["user_id"]
    except Exception:
        logger.debug("sync_notify: managers resolve skipped", exc_info=True)

    per_uid: Dict[str, List[Dict[str, Any]]] = {owner_uid: list(conflicts)}
    for c in conflicts:
        for side in (c.get("a"), c.get("b")):
            s = str(side or "").strip().lower()
            if not s or s == "company":
                continue
            uid = name_to_uid.get(s) or dept_mgr.get(s)
            if uid and uid != owner_uid:
                per_uid.setdefault(uid, []).append(c)
    return per_uid


def _base_text(conflicts: List[Dict[str, Any]]) -> str:
    lines = ["Обнаружен рассинхрон целей:"]
    for c in conflicts[:_MAX_CONFLICTS]:
        sev = "жёсткий" if c.get("severity") == "high" else "подтверждённый"
        shared = ", ".join(c.get("shared") or [])
        lines.append(
            f"• [{sev}] {c.get('a')} ↔ {c.get('b')}: "
            f"«{str(c.get('goal_a') or '')[:120]}» / «{str(c.get('goal_b') or '')[:120]}»"
            + (f" (общее: {shared})" if shared else ""))
    lines.append("Подробности — в панели «Организация» → «Синхронизация компании».")
    return "\n".join(lines)


async def _adapted_text(uid: str, base: str) -> str:
    """Адаптировать формулировку под фрейм получателя (cogni-adapt).
    Пустой фрейм или сбой LLM → базовый текст (без выдуманной подстройки)."""
    try:
        from backend.core.cogni.adapt import frame_directive, load_frame
        frame = await load_frame(uid)
        if frame.is_empty():
            return base
        prompt = (
            f"{frame_directive(frame)}\n\n"
            "Перескажи служебное уведомление ниже этому человеку. Требования:\n"
            "1. ВСЕ факты (стороны, формулировки целей, тип конфликта) — "
            "дословно из уведомления, ничего не добавлять и не смягчать.\n"
            "2. 40-90 слов, деловой тон в его стиле, без воды.\n"
            "3. Последняя строка про панель «Организация» сохраняется.\n\n"
            f"=== УВЕДОМЛЕНИЕ ===\n{base}")
        from backend.core.llm.router import LLMRouter, ModelTier
        text = await LLMRouter().generate(prompt=prompt,
                                          model_tier=ModelTier.STANDARD,
                                          max_tokens=500)
        return text.strip() if text and text.strip() else base
    except Exception:
        logger.debug("sync_notify: adapt skipped for %s", uid, exc_info=True)
        return base


async def _deliver(uid: str, text: str) -> Dict[str, bool]:
    delivered = {"signal": False, "telegram": False}
    try:
        from backend.api.websocket.signals_ws import publish_signal
        publish_signal("sync_desync", {
            "title": "Рассинхрон целей",
            "text": text[:2000],
        }, tenant_id=uid)
        delivered["signal"] = True
    except Exception:
        logger.debug("sync_notify: signal skipped", exc_info=True)
    try:
        from backend.config import get_settings
        token = (getattr(get_settings(), "telegram_bot_token", "") or "").strip()
        if token:
            from backend.core.messengers.links import get_messenger_links_service
            svc = await get_messenger_links_service()
            link = await svc.find_link_for_user(user_id=uid, platform="telegram")
            if link and getattr(link, "external_id", ""):
                # markdown → Telegram-HTML с фолбэком в чистый текст
                from backend.core.notifications.telegram_format import (
                    post_telegram_text,
                )
                res = await post_telegram_text(
                    token, str(link.external_id),
                    f"**⚠️ Синхронизация**\n\n{text[:3800]}", timeout=30.0)
                delivered["telegram"] = bool(res["ok"])
    except Exception:
        logger.debug("sync_notify: telegram skipped", exc_info=True)
    return delivered


async def notify_new_desyncs(
    owner_uid: str, levels: Dict[str, Any], *,
    acc_names: Optional[Dict[str, str]] = None,
    members: Optional[List[dict]] = None,
) -> Dict[str, Any]:
    """Уведомить о НОВЫХ значимых рассинхронах. Возвращает статистику.
    Never-raise; повторные прогоны те же конфликты не шлют (дедуп)."""
    out = {"new_conflicts": 0, "recipients": 0, "delivered": 0}
    try:
        sig = significant_conflicts(levels)
        if not sig:
            return out
        seen = _load_notified(owner_uid)
        fresh = [c for c in sig if _fingerprint(c) not in seen]
        if not fresh:
            return out
        out["new_conflicts"] = len(fresh)

        per_uid = _resolve_recipients(fresh, owner_uid,
                                      acc_names or {}, members or [])
        for uid, cs in list(per_uid.items())[:_MAX_RECIPIENTS]:
            base = _base_text(cs)
            text = await _adapted_text(uid, base)
            d = await _deliver(uid, text)
            out["recipients"] += 1
            if d["signal"] or d["telegram"]:
                out["delivered"] += 1

        seen.update(_fingerprint(c) for c in fresh)
        _save_notified(owner_uid, seen)
    except Exception:
        logger.warning("sync_notify failed", exc_info=True)
    return out


# держим ссылки на фоновые задачи (иначе GC до завершения)
_bg: set = set()


def spawn_notify(owner_uid: str, levels: Dict[str, Any], *,
                 acc_names: Optional[Dict[str, str]] = None,
                 members: Optional[List[dict]] = None) -> None:
    """Фоновый запуск уведомлений — не задерживает ответ отчёта."""
    try:
        task = asyncio.get_running_loop().create_task(
            notify_new_desyncs(owner_uid, levels,
                               acc_names=acc_names, members=members))
        _bg.add(task)
        task.add_done_callback(_bg.discard)
    except RuntimeError:
        logger.debug("sync_notify: no running loop, skipped")
