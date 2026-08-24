"""Generic action-recommendation bridge into the reactive learning loop (Phase N).

До Phase N через reactive-движок (load-gate + per-signal-type calibration +
feedback-обучение) ходили ТОЛЬКО Coffee-артефакты. Но ровно так же должны
себя вести любые проактивные рекомендации:
- Night Insights — ночные находки графа (RECOMMENDATION / RISK / OPPORTUNITY);
- Wisdom — рекомендации из паттернов прошлых решений;
- Automations — «дайджест встреч», «напоминание о дедлайне».

Их тоже нельзя слать в лоб (когда у юзера высокая нагрузка), и они тоже
должны учиться на реакции. Этот модуль даёт единую точку входа
`submit_recommendation(...)`, которая переиспользует ВЕСЬ существующий
Phase C–M pipeline, добавляя лишь namespace в signal_type:

  1. delivery_gate.should_deliver_now() — load- и calibration-aware решение;
  2. deliver_now=True  → пробуем доставить сразу (deliver_artifact по каналам);
     при провале ВСЕХ каналов — паркуем на retry (как coffee-orphan фикс), не
     теряем рекомендацию;
  3. deliver_now=False → паркуем сигнал; drain_runner доставит позже, когда
     нагрузка спадёт.

Обучение происходит САМО: пользовательский feedback (POST /reactive/feedback)
с тем же signal_type ложится в reactive_feedback, а recompute_calibration
строит per-type порог и веса. Нового обучающего кода тут нет — это и есть
смысл «применить learning-loop к другому сценарию»: достаточно нового
signal_type и вызова submit_recommendation.

Доставку parked-рекомендаций в drain'е обслуживает общий
_deliver_recommendation_signal (drain_runner.py), который умеет любой
non-coffee signal_type — поэтому новые сценарии подключаются без правок drain'а.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Дефолтный TTL parked-рекомендации: сутки. Рекомендация устаревает медленнее
# чем coffee-артефакт (12ч), но не должна висеть вечно.
_DEFAULT_VALID_UNTIL_MIN = 24 * 60

# Dedup-окно (F1-N): отметка «эта рекомендация уже доставлена» в Redis,
# чтобы wire-success-but-reported-failed + retry не слал дубль пользователю.
# Канал доставки (telegram/email) не атомарен с нашим status-update, поэтому
# до повторной отправки сверяемся с этой меткой. TTL покрывает retry-окно.
_DEDUP_TTL_SECONDS = 24 * 3600


def _dedup_key(user_id: str, signal_type: str, source_id: Optional[str],
               title: str, body_markdown: str) -> str:
    """Стабильный ключ дедупа: по source_id если есть, иначе хеш контента."""
    import hashlib
    if source_id:
        ident = f"id:{source_id}"
    else:
        h = hashlib.sha1(f"{title}\x00{body_markdown}".encode("utf-8")).hexdigest()[:16]
        ident = f"h:{h}"
    return f"recdedup:{user_id}:{signal_type}:{ident}"


async def _already_delivered(key: str) -> bool:
    try:
        from backend.core.reactive.dist_cache import cache_get_json
        return bool(await cache_get_json(key))
    except Exception:
        return False


async def _mark_delivered(key: str) -> None:
    try:
        from backend.core.reactive.dist_cache import cache_set_json
        await cache_set_json(key, {"delivered": True}, _DEDUP_TTL_SECONDS)
    except Exception:
        pass

# InsightPriority (night_processor) → priority 0..100 для гейта.
# critical/high доставляются почти всегда (>= high-threshold 70),
# medium — при нормальной нагрузке, low — только когда юзер свободен.
_INSIGHT_PRIORITY = {
    "critical": 85,
    "high": 70,
    "medium": 50,
    "low": 35,
}

# Минимальная уверенность инсайта, ниже которой не беспокоим юзера вообще.
_MIN_INSIGHT_CONFIDENCE = 0.5


def _build_wrap(
    *,
    title: str,
    body_markdown: str,
    channels: list[str],
    content_structured: Optional[dict[str, Any]],
    recommended_executor: Optional[str],
    signal_type: str,
) -> Any:
    """Duck-typed объект для coffee.delivery.deliver_artifact.

    deliver_artifact читает: title, content_markdown, content_structured,
    recommended_channels, recommended_executor, artifact_type, role.
    Рекомендация — не coffee-артефакт, но интерфейс тот же, поэтому
    переиспользуем весь channel-routing (telegram/email/notion/executor).
    """
    return type("RecommendationWrap", (), {
        "artifact_type": signal_type,
        "title": title,
        "content_markdown": body_markdown,
        "content_structured": content_structured or {},
        "recommended_channels": channels,
        "recommended_executor": recommended_executor,
        "role": "other",
    })()


async def _try_direct_delivery(
    *,
    user_id: str,
    title: str,
    body_markdown: str,
    channels: list[str],
    content_structured: Optional[dict[str, Any]],
    recommended_executor: Optional[str],
    signal_type: str,
    dedup_key: Optional[str] = None,
) -> tuple[bool, str, str]:
    """Попытаться доставить по каналам по очереди. (success, channel, last_err).

    F1-N: если dedup_key уже помечен delivered — не шлём повторно (idempotent),
    возвращаем success. После реальной доставки помечаем ключ.
    """
    if dedup_key and await _already_delivered(dedup_key):
        return True, "dedup", ""
    wrap = _build_wrap(
        title=title, body_markdown=body_markdown, channels=channels,
        content_structured=content_structured,
        recommended_executor=recommended_executor, signal_type=signal_type,
    )
    last_err = ""
    try:
        from backend.core.coffee.delivery import deliver_artifact
    except Exception as exc:
        return False, "", f"delivery import failed: {exc}"
    for ch in channels:
        if not ch or ch == "pending":
            continue
        try:
            dr = await deliver_artifact(
                pipeline_output=wrap, channel=ch, user_id=user_id,
            )
        except Exception as exc:
            last_err = str(exc)
            continue
        if dr.success:
            ch_val = dr.channel.value if hasattr(dr.channel, "value") else str(dr.channel)
            if dedup_key:
                await _mark_delivered(dedup_key)
            return True, ch_val, ""
        last_err = dr.error_message or last_err
    return False, "", last_err or "all channels failed"


async def _enqueue_recommendation(
    *,
    user_id: str,
    title: str,
    body_markdown: str,
    signal_type: str,
    priority: int,
    channels: list[str],
    recommended_executor: Optional[str],
    content_structured: Optional[dict[str, Any]],
    tenant_id: Optional[str],
    source_kind: Optional[str],
    source_id: Optional[str],
    valid_until_minutes: Optional[int],
    extra_payload: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    """Запарковать рекомендацию в reactive_signals для drain-доставки."""
    payload: dict[str, Any] = {
        "recommended_channels": channels,
        "recommended_executor": recommended_executor,
        "content_structured": content_structured or {},
    }
    if extra_payload:
        payload.update(extra_payload)
    try:
        from backend.core.reactive.signal_queue import enqueue_signal
        return await enqueue_signal(
            user_id=user_id,
            signal_type=signal_type,
            title=title,
            body_markdown=body_markdown,
            payload=payload,
            priority=priority,
            tenant_id=tenant_id,
            valid_until_minutes=valid_until_minutes,
            source_kind=source_kind or "recommendation",
            source_id=source_id,
        )
    except Exception as exc:
        logger.warning("enqueue recommendation failed: %s", exc)
        return None


async def submit_recommendation(
    *,
    user_id: str,
    title: str,
    body_markdown: str,
    signal_type: str,
    priority: int = 50,
    recommended_channels: Optional[list[str]] = None,
    recommended_executor: Optional[str] = None,
    content_structured: Optional[dict[str, Any]] = None,
    tenant_id: Optional[str] = None,
    persona: Optional[Any] = None,
    source_kind: Optional[str] = None,
    source_id: Optional[str] = None,
    valid_until_minutes: Optional[int] = _DEFAULT_VALID_UNTIL_MIN,
    force: bool = False,
) -> dict[str, Any]:
    """Провести рекомендацию через reactive learning-loop. Never-raise.

    Returns:
        {"status": "delivered"|"queued"|"failed", "signal_id": str|None,
         "channel": str|None, "signal_type": str, "reason": str}
    """
    channels = [c for c in (recommended_channels or ["telegram"]) if c]
    result: dict[str, Any] = {
        "status": "failed",
        "signal_id": None,
        "channel": None,
        "signal_type": signal_type,
        "reason": "",
    }
    # F1-N: dedup-ключ для идемпотентной доставки (direct + drain-retry).
    dedup_key = _dedup_key(user_id, signal_type, source_id, title, body_markdown)

    # 1. Гейт: load + per-type калибровка. При сбое гейта → None → ведём себя
    # как «можно слать» (legacy always-deliver, не теряем рекомендацию).
    gate = None
    try:
        from backend.core.reactive.delivery_gate import should_deliver_now
        gate = await should_deliver_now(
            user_id=user_id, priority=priority, signal_type=signal_type,
            persona=persona, tenant_id=tenant_id, force=force,
        )
    except Exception as exc:
        logger.warning("recommendation gate failed: %s", exc)

    # 2. Парковка если гейт сказал «не сейчас»
    if gate is not None and not gate.deliver_now:
        sid = await _enqueue_recommendation(
            user_id=user_id, title=title, body_markdown=body_markdown,
            signal_type=signal_type, priority=priority, channels=channels,
            recommended_executor=recommended_executor,
            content_structured=content_structured, tenant_id=tenant_id,
            source_kind=source_kind, source_id=source_id,
            valid_until_minutes=valid_until_minutes,
        )
        result.update(
            status="queued", signal_id=sid,
            reason=f"parked by gate: {gate.reason}",
        )
        return result

    # 3. Гейт разрешил (или недоступен) → пробуем доставить сразу
    delivered, channel, last_err = await _try_direct_delivery(
        user_id=user_id, title=title, body_markdown=body_markdown,
        channels=channels, content_structured=content_structured,
        recommended_executor=recommended_executor, signal_type=signal_type,
        dedup_key=dedup_key,
    )
    if delivered:
        result.update(status="delivered", channel=channel, reason="delivered directly")
        return result

    # 4. Все каналы упали → паркуем на retry (orphan-avoidance, как в coffee).
    # dedup_key кладём в payload, чтобы drain не слал дубль если direct-попытка
    # реально ушла по проводу, но отрапортовала об ошибке.
    sid = await _enqueue_recommendation(
        user_id=user_id, title=title, body_markdown=body_markdown,
        signal_type=signal_type, priority=priority, channels=channels,
        recommended_executor=recommended_executor,
        content_structured=content_structured, tenant_id=tenant_id,
        source_kind=source_kind, source_id=source_id,
        valid_until_minutes=valid_until_minutes,
        extra_payload={"retry_after_direct_failure": True, "last_error": last_err,
                       "dedup_key": dedup_key},
    )
    result.update(
        status="queued" if sid else "failed", signal_id=sid,
        reason=f"direct delivery failed ({last_err}); parked for retry",
    )
    return result


# ─────────────────────────────────────────────────────────────────
# Producer adapter: Night Insights → recommendations
# ─────────────────────────────────────────────────────────────────


def _enum_value(x: Any) -> str:
    """enum → .value, иначе str(). Толерантно к None."""
    if x is None:
        return ""
    return str(x.value) if hasattr(x, "value") else str(x)


def _coerce_insight(ins: Any) -> dict[str, Any]:
    """Привести NightInsight-объект ИЛИ dict к плоскому словарю.

    Намеренно не импортируем night_processor (decoupling): принимаем
    что угодно с нужными полями/ключами. Толерантно к пропускам.
    """
    def _get(key: str, default: Any = None) -> Any:
        if isinstance(ins, dict):
            return ins.get(key, default)
        return getattr(ins, key, default)

    return {
        "insight_id": str(_get("insight_id", "") or ""),
        "insight_type": (_enum_value(_get("insight_type")) or "recommendation").lower(),
        "priority": (_enum_value(_get("priority")) or "medium").lower(),
        "title": str(_get("title", "") or ""),
        "description": str(_get("description", "") or ""),
        "actions": list(_get("actions", []) or []),
        "confidence": float(_get("confidence", 0.5) or 0.0),
        "is_actionable": bool(_get("is_actionable", True)),
    }


def _insight_body(data: dict[str, Any]) -> str:
    """Собрать markdown-тело рекомендации из инсайта."""
    parts = []
    if data["description"]:
        parts.append(data["description"])
    if data["actions"]:
        parts.append("\n**Что можно сделать:**")
        for a in data["actions"][:5]:
            parts.append(f"- {a}")
    conf_pct = int(round(data["confidence"] * 100))
    parts.append(f"\n_Уверенность: {conf_pct}%_")
    return "\n".join(parts).strip()


async def submit_night_insights(
    *,
    user_id: str,
    insights: Any,
    tenant_id: Optional[str] = None,
    persona: Optional[Any] = None,
    channels: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Маршрутизировать список NightInsight через learning-loop. Never-raise.

    Только actionable инсайты с confidence >= _MIN_INSIGHT_CONFIDENCE
    реально идут в гейт; остальные пропускаются (не шумим). signal_type =
    f"night_insight_{type}" — калибровка учится отдельно для risk /
    opportunity / recommendation и т.д.

    Точка интеграции: вызывать из nightly-джоба после генерации инсайтов,
    например:
        from backend.core.reactive.recommendations import submit_night_insights
        await submit_night_insights(user_id=uid, insights=insights, tenant_id=tid)

    Returns {"submitted": N, "skipped": M, "results": [...]}.
    """
    submitted: list[dict[str, Any]] = []
    skipped = 0
    for ins in (insights or []):
        try:
            data = _coerce_insight(ins)
            if not data["is_actionable"] or data["confidence"] < _MIN_INSIGHT_CONFIDENCE:
                skipped += 1
                continue
            # Audit-фикс (F5-N): critical-инсайты обязаны дойти даже при
            # CRITICAL load (гейт авто-пропускает только priority>=90, а
            # critical маппится в 85 → парковался). force=True гарантирует
            # доставку для critical; остальные приоритеты проходят через гейт.
            is_critical = data["priority"] == "critical"
            res = await submit_recommendation(
                user_id=user_id,
                title=data["title"] or "Инсайт Tessbrain",
                body_markdown=_insight_body(data),
                signal_type=f"night_insight_{data['insight_type']}",
                priority=_INSIGHT_PRIORITY.get(data["priority"], 50),
                recommended_channels=channels or ["telegram"],
                tenant_id=tenant_id,
                persona=persona,
                source_kind="night_insight",
                source_id=data["insight_id"] or None,
                valid_until_minutes=_DEFAULT_VALID_UNTIL_MIN,
                force=is_critical,
            )
            submitted.append(res)
        except Exception as exc:
            logger.debug("night insight route failed: %s", exc)
            skipped += 1
    return {"submitted": len(submitted), "skipped": skipped, "results": submitted}


__all__ = [
    "submit_recommendation",
    "submit_night_insights",
]
