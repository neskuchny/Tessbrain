# -*- coding: utf-8 -*-
"""Outcome-петля наблюдателя (Ф4): «я говорил — проверил, что вышло».

Сильнейший сигнал живости агента: cron не возвращается проверить свой
совет. Через несколько дней и ≥N новых встреч LLM читает СТАРУЮ зацепку
и снапшоты НОВЫХ встреч и выносит вердикт по смыслу текста (не по тегам):
  applied   — тема закрылась / видны действия по ней;
  worsened  — стало заметнее, чем в момент зацепки;
  unchanged — ровно то же самое;
  unclear   — по данным не видно ни того, ни другого.

applied/worsened рождают follow-up-наблюдение («Пять дней назад говорил
про X — проверил: …») — оно попадает в память, ленту и (Ф3) в Telegram.
unchanged/unclear просто запоминаются — скрин видит их в истории.
Не больше OBSERVER_OUTCOME_MAX_PER_CYCLE (1) проверок за цикл. Never-raise.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

from backend.core.observer.state import (
    add_observation,
    recent_observations,
    set_outcome,
)

logger = logging.getLogger(__name__)

OUTCOME_SYSTEM_PROMPT = """\
Ты — наблюдатель компании, который несколько дней назад высказал человеку
наблюдение и теперь проверяет по НОВЫМ встречам, что с этой темой стало.

Жёсткие правила:
- НИКАКИХ общих формулировок («в целом», «наблюдается», «рекомендуется»).
  Только конкретика из старой зацепки и новых снапшотов.
- Сравнивай СМЫСЛ старой зацепки с тем, что появилось в новых снапшотах.
- 1–2 предложения живым языком. Если уместно — одно конкретное
  следующее действие.
- Не видно ни закрытия, ни ухудшения — честно ставь "unclear".

Верни строго JSON без markdown:
{"outcome_note": "1-2 предложения",
 "status": "applied" | "worsened" | "unchanged" | "unclear"}
"""


def _due_observation(user_id: str,
                     records: Dict[str, Dict[str, Any]],
                     ) -> Optional[Dict[str, Any]]:
    """Самое старое наблюдение без outcome, созревшее для проверки:
    прошло ≥ OBSERVER_OUTCOME_DAYS и после него появилось ≥ 2 снапшотов."""
    days = float(os.getenv("OBSERVER_OUTCOME_DAYS", "3"))
    min_new = int(os.getenv("OBSERVER_OUTCOME_MIN_NEW", "2"))
    now = time.time()
    candidates = [o for o in recent_observations(user_id, limit=30)
                  if not o.get("outcome_status")
                  and o.get("format") != "follow_up"
                  and (now - float(o.get("ts") or 0)) >= days * 86400]
    for obs in sorted(candidates, key=lambda o: o.get("ts") or 0):
        newer = [r for r in records.values()
                 if (r.get("ts") or 0) > float(obs.get("ts") or 0)]
        if len(newer) >= min_new:
            obs["_newer_records"] = newer
            return obs
    return None


def _snapshots_block(newer: List[Dict[str, Any]]) -> str:
    parts: List[str] = []
    for rec in newer[:10]:
        snap = rec.get("snapshot") or {}
        title = rec.get("title") or "(без названия)"
        date = str(rec.get("created_at") or "")[:10] or "?"
        lines = [f"{date} — {title}"]
        if snap.get("summary"):
            lines.append(f"  summary: {snap['summary']}")
        for key in ("topics", "blockers", "tensions", "decisions",
                    "explicit_asks"):
            vals = snap.get(key) or []
            if vals:
                lines.append(f"  {key}: " + "; ".join(str(v) for v in vals))
        parts.append("\n".join(lines))
    return "\n\n".join(parts) or "(новых снапшотов нет)"


async def check_one_outcome(user_id: str, router: Any,
                            records: Dict[str, Dict[str, Any]],
                            ) -> Optional[Dict[str, Any]]:
    """Проверить один созревший outcome. Возвращает
    {observation_id, status, note, follow_up_hook?} или None. Never-raise."""
    obs = _due_observation(user_id, records)
    if obs is None:
        return None
    newer = obs.pop("_newer_records", [])
    days_ago = max(1, int((time.time() - float(obs.get("ts") or 0)) / 86400))
    prompt = (
        f"# Что я говорил {days_ago} дн. назад\n"
        f"Фронт: {obs.get('front_id')}\n"
        f"Зацепка: \"{obs.get('hook')}\"\n"
        f"Сигнал, на котором она стояла: {obs.get('signal') or '(нет)'}\n\n"
        f"# Новые встречи ПОСЛЕ зацепки (структурные снапшоты)\n"
        f"{_snapshots_block(newer)}\n\n"
        "Реши, что стало с темой. Верни JSON по контракту.")
    try:
        from backend.core.llm.router import ModelTier
        raw = await router.generate_json(
            prompt=prompt, system_prompt=OUTCOME_SYSTEM_PROMPT,
            model_tier=ModelTier.STANDARD, temperature=0.3)
    except Exception as e:
        logger.warning("observer outcome LLM failed: %s", e)
        return None
    if not isinstance(raw, dict):
        return None
    status = str(raw.get("status") or "").strip()
    note = str(raw.get("outcome_note") or "").strip()
    if not (note and set_outcome(user_id, str(obs.get("id")), note, status)):
        return None
    out: Dict[str, Any] = {"observation_id": obs.get("id"),
                           "status": status, "note": note}
    if status in ("applied", "worsened"):
        # follow-up-наблюдение: «говорил — проверил». Идёт в память как
        # отдельная запись формата follow_up (сам без outcome-проверки).
        hook = (f"{days_ago} дн. назад я говорил: «{str(obs.get('hook'))[:200]}» "
                f"— проверил: {note}")
        oid = add_observation(
            user_id, front_id=str(obs.get("front_id") or ""),
            fmt="follow_up", hook=hook,
            signal=note, meeting_ids=[], score=0.6)
        out["follow_up_hook"] = hook
        out["follow_up_id"] = oid
    logger.info("👁 outcome [%s] %s: %s", obs.get("front_id"), status,
                note[:80])
    return out
