"""Drain runner (Phase D) — единая реализация доставки parked-сигналов.

До Phase D логика «как доставить отложенный сигнал» дублировалась в:
- taskiq reactive_drain_task
- POST /api/v1/reactive/drain
- post-meeting hook в process_meeting_task

Теперь единая точка: `run_drain(user_id, max_signals)`. Знает как
восстановить coffee-артефакт из БД и доставить через delivery.deliver_artifact.
Расширяется новыми signal_type'ами через _DELIVER_HANDLERS.

Best-effort: никогда не raise.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


async def _deliver_coffee_signal(sig: Any) -> tuple[bool, str, str]:
    """Восстановить coffee-артефакт из coffee_artifacts и доставить.

    Returns (success, channel, error_message).
    """
    from backend.core.coffee.delivery import deliver_artifact
    from backend.core.coffee.storage import get_artifact

    payload = sig.payload or {}
    aid = payload.get("artifact_id")
    if not aid:
        return False, "", "no artifact_id in payload"

    # Audit-фикс: точечный SELECT по id вместо сканирования capped списка.
    # Раньше list_artifacts(user_id, limit=200) пропускал артефакты у
    # активных юзеров с >200 строк, и parked-сигнал терял свой target.
    target = await get_artifact(aid)
    if not target:
        return False, "", f"artifact {aid} not found"
    # Если артефакт уже инвалидирован cascade'ом — не доставляем
    if target.get("status") == "superseded":
        return False, "", "artifact superseded (cascade)"

    wrap = type("CoffeeWrap", (), {
        "artifact_type": target["artifact_type"],
        "title": target["title"],
        "content_markdown": target["content_markdown"],
        "content_structured": target["content_structured"],
        "recommended_channels": target["recommended_channels"],
        "recommended_executor": target["recommended_executor"],
        "role": target["role"],
    })()

    channels = (
        payload.get("recommended_channels")
        or target.get("recommended_channels")
        or ["telegram"]
    )
    for ch in channels:
        if not ch or ch == "pending":
            continue
        dr = await deliver_artifact(
            pipeline_output=wrap, channel=ch, user_id=sig.user_id,
        )
        if dr.success:
            ch_val = dr.channel.value if hasattr(dr.channel, "value") else str(dr.channel)
            # Phase D: синхронизируем coffee_artifacts delivery state
            try:
                from backend.core.coffee.storage import update_delivery_status
                await update_delivery_status(
                    artifact_id=aid,
                    delivered_channel=ch_val,
                    delivered_external_ref=dr.external_ref,
                    success=True,
                )
            except Exception as exc:
                logger.debug("update_delivery_status after drain failed: %s", exc)
            return True, ch_val, ""
    return False, "", "all channels failed"


async def _deliver_recommendation_signal(sig: Any) -> tuple[bool, str, str]:
    """Phase N: общий деливерер для НЕ-coffee сигналов (рекомендаций).

    Night Insights, Wisdom, Automations и любые будущие сценарии паркуются
    как reactive_signals с title/body_markdown и payload.recommended_channels.
    Здесь мы просто доставляем их по каналам через тот же coffee.delivery
    (telegram/email/notion/executor) — без coffee_artifacts DB-sync, т.к.
    источник тут не coffee-артефакт. Returns (success, channel, error).
    """
    from backend.core.coffee.delivery import deliver_artifact

    payload = sig.payload or {}
    channels = payload.get("recommended_channels") or ["telegram"]

    # F1-N: idempotency. Если direct-попытка реально ушла по проводу, но
    # отрапортовала об ошибке (wire-success-but-reported-failed), dedup-метка
    # уже стоит → не шлём дубль. Ключ кладёт submit_recommendation в payload;
    # fallback на контент-хеш для сигналов, запаркованных без него.
    dedup_key = payload.get("dedup_key")
    if not dedup_key:
        import hashlib
        h = hashlib.sha1(
            f"{sig.title}\x00{sig.body_markdown or ''}".encode("utf-8")
        ).hexdigest()[:16]
        dedup_key = f"recdedup:{sig.user_id}:{sig.signal_type}:h:{h}"
    try:
        from backend.core.reactive.dist_cache import cache_get_json, cache_set_json
    except Exception:
        cache_get_json = cache_set_json = None  # type: ignore
    if cache_get_json is not None:
        try:
            if await cache_get_json(dedup_key):
                return True, "dedup", ""
        except Exception:
            pass

    wrap = type("RecWrap", (), {
        "artifact_type": sig.signal_type,
        "title": sig.title,
        "content_markdown": sig.body_markdown or "",
        "content_structured": payload.get("content_structured") or {},
        "recommended_channels": channels,
        "recommended_executor": payload.get("recommended_executor"),
        "role": "other",
    })()

    last_err = ""
    for ch in channels:
        if not ch or ch == "pending":
            continue
        try:
            dr = await deliver_artifact(
                pipeline_output=wrap, channel=ch, user_id=sig.user_id,
            )
        except Exception as exc:
            last_err = str(exc)
            continue
        if dr.success:
            ch_val = dr.channel.value if hasattr(dr.channel, "value") else str(dr.channel)
            if cache_set_json is not None:
                try:
                    await cache_set_json(dedup_key, {"delivered": True}, 24 * 3600)
                except Exception:
                    pass
            return True, ch_val, ""
        last_err = dr.error_message or last_err
    return False, "", last_err or "all channels failed"


# signal_type prefix → специализированный handler. Coffee нуждается в особом
# (восстановление артефакта + DB-sync); всё остальное обслуживает общий
# recommendation-деливерер (см. _dispatch fallback).
_DELIVER_HANDLERS = {
    "coffee_": _deliver_coffee_signal,
}


async def _dispatch(sig: Any) -> tuple[bool, str, str]:
    """Выбрать handler по signal_type prefix; fallback — общий recommendation.

    Phase N: раньше неизвестный signal_type возвращал «no handler» и сигнал
    вечно копил delivery_attempts. Теперь любой не-coffee сигнал доставляется
    общим деливерером — новые сценарии (night_insight_*, wisdom_*, automation_*)
    подключаются без правок drain'а. Never-raise.
    """
    try:
        for prefix, handler in _DELIVER_HANDLERS.items():
            if sig.signal_type.startswith(prefix):
                return await handler(sig)
        return await _deliver_recommendation_signal(sig)
    except Exception as exc:
        return False, "", str(exc)


async def run_drain(
    *,
    user_id: Optional[str] = None,
    max_signals: int = 100,
) -> dict[str, Any]:
    """Drain очереди через built-in coffee handler. Best-effort.

    Args:
        user_id: None → все юзеры с queued сигналами; иначе только этот
        max_signals: лимит на вызов

    Returns:
        {"checked", "delivered", "deferred", "expired"} (или {} при сбое)
    """
    try:
        from backend.core.reactive.signal_queue import drain_signals
        return await drain_signals(
            deliver_fn=_dispatch, user_id=user_id, max_signals=max_signals,
        )
    except Exception as exc:
        logger.warning("run_drain failed: %s", exc)
        return {"checked": 0, "delivered": 0, "deferred": 0, "expired": 0}


__all__ = ["run_drain"]
