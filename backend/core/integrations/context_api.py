# -*- coding: utf-8 -*-
"""Context API — INTEGRATION_TESSENT.md Фаза 2.

Daily Pulse спрашивает: «решения/знания, релевантные команде X на дату Y».
Отдаём из существующего event_log/insights с пометками provenance/_source,
чтобы их вечерний анализатор НЕ принимал наш llm_narrative за ground truth
(их §4.2: цикл LLM-on-LLM).
"""
from __future__ import annotations

import datetime
import logging
import re
from typing import List, Optional

logger = logging.getLogger(__name__)

_DIGITS_ONLY = re.compile(r"\D+")


async def context_for_team(team_ref: str, *, since: Optional[str] = None,
                           until: Optional[str] = None,
                           topics: Optional[List[str]] = None) -> dict:
    """Решения и связанные факты для команды на период.

    Алгоритм: маппим team_ref → членов команды (Tessbrain teams);
    собираем из event_log decisions/решений + verified-факты участников
    + выдержки релевантных инсайтов. _source на каждом — measured /
    llm_narrative / human (защита от LLM-on-LLM)."""
    out = {"team_ref": team_ref, "since": since, "until": until,
           "topics": topics or [], "decisions": [], "facts": [],
           "insights": [], "_contract": "INTEGRATION_TESSENT_RESPONSE.md"}

    member_ids = await _team_members(team_ref)
    if not member_ids:
        return out

    # decisions из event_log участников
    try:
        from backend.core.memory.event_log import EventLog
        from backend.core.store.tenant_paths import event_log_path_for_user
        seen = set()
        for uid in member_ids:
            try:
                log = EventLog(persist_path=event_log_path_for_user(uid))
            except Exception:
                continue
            for ev in log.events(kind="decision", since=since, until=until):
                d = ev.to_dict() if hasattr(ev, "to_dict") else dict(ev)
                key = (d.get("at"), str(d.get("payload") or "")[:200])
                if key in seen:
                    continue
                seen.add(key)
                text = (d.get("payload") or {}).get("t") or \
                       (d.get("payload") or {}).get("text") or ""
                if topics and not _matches_topics(text, topics):
                    continue
                out["decisions"].append({
                    "at": d.get("at"), "text": text,
                    "actor": d.get("actor"),
                    "_source": "human",       # решения фиксирует человек
                })
    except Exception as e:
        logger.debug(f"decisions collection: {e}")

    # verified-факты участников (measured)
    try:
        from backend.core.memory.verified_answers import verified_for_user
        for uid in member_ids:
            try:
                vstore = verified_for_user(uid)
            except Exception:
                continue
            for fact_key, rec in vstore.list().items():
                if topics and not _matches_topics(fact_key, topics):
                    continue
                out["facts"].append({
                    "fact_key": fact_key,
                    "verified_by": rec.get("verified_by"),
                    "verified_at": rec.get("verified_at"),
                    "_source": "human",
                })
    except Exception as e:
        logger.debug(f"facts collection: {e}")

    # инсайты (llm_narrative — Daily Pulse НЕ должен их использовать
    # как ground truth, только как сигнал)
    try:
        from backend.core.sleep.insight_store import InsightStore
        from backend.core.store.tenant_paths import insights_path_for_user
        seen_ins = set()
        for uid in member_ids:
            try:
                ins = InsightStore(persist_path=insights_path_for_user(uid))
            except Exception:
                continue
            for d in ins.list(since=since, limit=20):
                iid = d.get("insight_id")
                if iid in seen_ins:
                    continue
                seen_ins.add(iid)
                if topics and not _matches_topics(
                        d.get("title", "") + " " + d.get("description", ""),
                        topics):
                    continue
                out["insights"].append({
                    "insight_id": iid,
                    "title": d.get("title"),
                    "priority": d.get("priority"),
                    "_source": "llm_narrative",
                })
    except Exception as e:
        logger.debug(f"insights collection: {e}")

    out["as_of"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return out


async def context_for_customer(user_id: str, phone: str,
                               name: Optional[str] = None) -> dict:
    """Контекст Brain по клиенту для CallInsight (их этап 4,
    docs/CALLINSIGHT_INTEGRATION.md §2.4): встречи/решения/факты, где
    упомянут телефон или имя клиента. В ответ идут ТОЛЬКО human/measured
    артефакты — наши llm_narrative-выводы не попадают в их Gemini-промпт
    (двойная защита от LLM-on-LLM).

    Скоуп — один пользователь (вызывающий service-JWT передаёт user_id
    владельца данных), кросс-тенантного обхода нет по построению."""
    from backend.core.integrations.callinsight import normalize_phone
    norm = normalize_phone(phone)
    out = {"phone_norm": norm, "name": name or None,
           "meetings": [], "facts": [], "tasks": [],
           "_note": ("только human/measured; llm_narrative-артефакты "
                     "исключены намеренно"),
           "_contract": "docs/CALLINSIGHT_INTEGRATION.md"}
    if not norm and not name:
        return out

    needles = [n for n in (norm, (name or "").strip().lower()) if n]

    def _mentions(blob: str) -> bool:
        low = blob.lower()
        if norm and norm in _DIGITS_ONLY.sub("", blob):
            return True
        return any(n in low for n in needles if not n.isdigit())

    # решения/события с упоминанием клиента (human: фиксирует человек)
    try:
        import json as _json

        from backend.core.memory.event_log import EventLog
        from backend.core.store.tenant_paths import event_log_path_for_user
        log = EventLog(persist_path=event_log_path_for_user(user_id))
        for d in log.events(kind="decision"):
            blob = _json.dumps(d.get("payload") or {}, ensure_ascii=False)
            if not _mentions(blob):
                continue
            out["meetings"].append({
                "at": d.get("at"),
                "text": (d.get("payload") or {}).get("t")
                        or (d.get("payload") or {}).get("text") or "",
                "actor": d.get("actor"),
                "_source": "human",
            })
    except Exception as e:
        logger.debug(f"customer decisions: {e}")

    # verified-факты с упоминанием клиента (human: подтвердил человек)
    try:
        from backend.core.memory.verified_answers import verified_for_user
        vstore = verified_for_user(user_id)
        for fact_key, rec in vstore.list().items():
            blob = fact_key + " " + str(rec.get("answer") or "")
            if not _mentions(blob):
                continue
            out["facts"].append({
                "fact_key": fact_key,
                "verified_by": rec.get("verified_by"),
                "verified_at": rec.get("verified_at"),
                "_source": "human",
            })
    except Exception as e:
        logger.debug(f"customer facts: {e}")

    out["as_of"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return out


def _matches_topics(text: str, topics: List[str]) -> bool:
    low = str(text or "").lower()
    return any(str(t).lower() in low for t in topics if t)


async def _team_members(team_ref: str) -> List[str]:
    """Tessbrain team_id → список tessent_user_id. Best-effort."""
    try:
        from sqlalchemy import text

        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session() as session:
            res = await session.execute(
                text("SELECT user_id FROM team_members WHERE team_id = :t"),
                {"t": str(team_ref)})
            return [str(r._mapping["user_id"]) for r in res.fetchall()]
    except Exception as e:
        logger.debug(f"team_members lookup: {e}")
        return []
