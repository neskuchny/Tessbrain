"""П.7 — 360-review consumer.

Mini Tess отдаёт архив персоны (`/persona/me/export` + local_profile), а Tessbrain
строит 360-review. Здесь — приёмник peer-фидбека о субъекте + сборка ревью.

360 = self-view (персона субъекта) + others-view (анонимный peer-фидбек).
k-anonymity на ДВУХ уровнях:
  - весь others-блок скрыт, пока < k рейтеров (защита честности фидбека);
  - КАЖДАЯ компетенция скрыта, если её оценили < k рейтеров (иначе оценка от
    2 из 6 может деанонимизировать рейтера).

Идентичности рейтеров наружу не идут никогда (core гарантирует).
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Optional

from backend.core.aggregation.core import (
    DEFAULT_K,
    DOMAIN_REVIEW_360,
    aggregate_view,
    contribute,
)

logger = logging.getLogger(__name__)


async def submit_360_feedback(
    *,
    tenant_id: str,
    subject_user_id: str,
    rater_user_id: str,
    ratings: Optional[dict[str, Any]] = None,
    comment: Optional[str] = None,
) -> dict[str, Any]:
    """Принять peer-фидбек о субъекте. Один рейтер = один вклад (upsert).
    Never-raise.

    ratings: {competency_key: score(number)}; comment: свободный текст.

    Returns: {"ok", "distinct_raters", "reason"}.
    """
    out: dict[str, Any] = {"ok": False, "distinct_raters": 0, "reason": ""}
    if not (tenant_id and subject_user_id and rater_user_id):
        out["reason"] = "tenant_id, subject_user_id, rater_user_id required"
        return out
    if rater_user_id == subject_user_id:
        # self-оценка не считается peer-фидбеком (иначе портит k-счёт).
        out["reason"] = "rater cannot be the subject"
        return out

    clean_ratings: dict[str, float] = {}
    for kk, vv in (ratings or {}).items():
        try:
            clean_ratings[str(kk)] = float(vv)
        except (TypeError, ValueError):
            continue

    dcount = await contribute(
        tenant_id=tenant_id,
        domain=DOMAIN_REVIEW_360,
        aggregate_key=subject_user_id,
        contributor_user_id=rater_user_id,
        payload={"ratings": clean_ratings, "comment": (comment or "")[:5000]},
    )
    if dcount is None:
        out["reason"] = "contribute failed"
        return out
    out.update(ok=True, distinct_raters=dcount, reason="recorded")
    return out


async def _self_view(subject_user_id: str) -> Optional[dict[str, Any]]:
    """Self-блок 360 из персоны субъекта. Best-effort → None."""
    try:
        from backend.core.persona.store import get_persona_store
        store = get_persona_store()
        persona = await store.get(subject_user_id)
        if persona is None:
            return None
        pub = persona.to_public_dict()
        return {
            "schema_version": pub.get("schema_version"),
            "strengths_weaknesses": pub.get("strengths_weaknesses"),
            "development": pub.get("development"),
            "source_meetings": pub.get("source_meetings"),
            "confidence": pub.get("confidence"),
        }
    except Exception as exc:
        logger.debug("360 self_view skipped: %s", exc)
        return None


def _rollup_competencies(
    contributions: list[dict[str, Any]], k: int,
) -> dict[str, Any]:
    """Свести ratings по компетенциям. Компетенция включается ТОЛЬКО если её
    оценили >= k рейтеров (per-item k-anonymity). Возвращает
    {competency: {avg, count}} + список подавленных."""
    buckets: dict[str, list[float]] = {}
    for c in contributions:
        ratings = (c or {}).get("ratings") or {}
        if not isinstance(ratings, dict):
            continue
        for comp, score in ratings.items():
            try:
                buckets.setdefault(str(comp), []).append(float(score))
            except (TypeError, ValueError):
                continue
    visible: dict[str, Any] = {}
    suppressed: list[str] = []
    for comp, scores in buckets.items():
        if len(scores) >= k:
            visible[comp] = {
                "avg": round(sum(scores) / len(scores), 3),
                "count": len(scores),
            }
        else:
            suppressed.append(comp)
    return {"competencies": visible, "suppressed_competencies": sorted(suppressed)}


async def build_360_review(
    *,
    tenant_id: str,
    subject_user_id: str,
    k: int = DEFAULT_K,
    include_self: bool = True,
) -> dict[str, Any]:
    """Собрать 360-review субъекта. Never-raise.

    Если рейтеров < k — others-блок подавлён целиком (suppressed=True), но
    self-view (из персоны) всё равно отдаётся — он не нарушает анонимность.
    """
    view = await aggregate_view(
        tenant_id=tenant_id,
        domain=DOMAIN_REVIEW_360,
        aggregate_key=subject_user_id,
        k=k,
    )

    others: dict[str, Any]
    if view.suppressed:
        others = {
            "suppressed": True,
            "distinct_raters": view.distinct_count,
            "k": view.k,
            "reason": f"need >= {view.k} raters, have {view.distinct_count}",
        }
    else:
        rollup = _rollup_competencies(view.contributions, view.k)
        # Комментарии перемешиваем детерминированно по содержимому.
        # Хранилище отдаёт вклады в порядке подачи (created_at ASC), и этот
        # порядок сам по себе побочный канал: кто знает, что отвечал первым,
        # опознаёт автора первого комментария. Сортировка по хэшу текста
        # разрывает связь с порядком подачи и при этом устойчива между
        # запросами (иначе один и тот же отзыв «прыгал» бы по списку).
        comments = sorted(
            (
                (c or {}).get("comment", "")
                for c in view.contributions
                if (c or {}).get("comment")
            ),
            key=lambda text: hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
        others = {
            "suppressed": False,
            "distinct_raters": view.distinct_count,
            "k": view.k,
            **rollup,
            # комментарии анонимны (без rater-id), показываются только при >= k
            "comments": comments,
        }

    result: dict[str, Any] = {
        "subject_user_id": subject_user_id,
        "k": view.k,
        "others_view": others,
    }
    if include_self:
        result["self_view"] = await _self_view(subject_user_id)
    return result
