# -*- coding: utf-8 -*-
"""Оценка размера сегментов через Яндекс.Вордстат (фаза 3 исследований).

«Насколько их много потенциально» — не мнение LLM, а частоты реальных
поисковых запросов сегмента (те самые queries «как ищут эти люди» из
карты сегментов). Wordstat доступен через Директ API v4 Live
(CreateNewWordstatReport → poll → Get → Delete), токен —
YANDEX_DIRECT_TOKEN. Без токена — честный отказ с подсказкой, никаких
выдуманных оценок.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_API = "https://api.direct.yandex.ru/live/v4/json/"
_POLL_TRIES = 12
_POLL_DELAY = 2.0

# Пороги «крупный/средний/узкий» — показов в месяц по фразам сегмента
TIER_BIG = 10_000
TIER_MID = 1_000


def direct_token() -> str:
    return os.getenv("YANDEX_DIRECT_TOKEN", "").strip()


def config_hint() -> str:
    return ("Wordstat не настроен: добавьте OAuth-токен Директа во вкладке "
            "«Интеграции» → «Яндекс.Директ / Wordstat» (или env "
            "YANDEX_DIRECT_TOKEN)")


async def _call(method: str, param, token: str, http=None) -> dict:
    body = {"method": method, "param": param, "token": token}
    if http is None:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(_API, json=body)
    else:
        r = await http.post(_API, json=body)
    data = r.json()
    if isinstance(data, dict) and data.get("error_code"):
        raise RuntimeError(
            f"Директ API: {data.get('error_str') or data.get('error_detail')}"
            f" (код {data['error_code']})")
    return data


async def wordstat_volumes(phrases: List[str], *, token: Optional[str] = None,
                           http=None) -> Dict[str, int]:
    """Показы в месяц по фразам. Жизненный цикл отчёта Wordstat v4."""
    token = token or direct_token()
    if not token:
        raise RuntimeError(config_hint())
    phrases = [p.strip() for p in phrases if p and p.strip()][:10]
    if not phrases:
        return {}
    created = await _call("CreateNewWordstatReport",
                          {"Phrases": phrases, "GeoID": [0]}, token, http=http)
    report_id = created.get("data")
    if not report_id:
        raise RuntimeError("Wordstat: отчёт не создан")
    try:
        for _ in range(_POLL_TRIES):
            lst = await _call("GetWordstatReportList", {}, token, http=http)
            st = next((r for r in (lst.get("data") or [])
                       if r.get("ReportID") == report_id), None)
            if st and st.get("StatusReport") == "Done":
                break
            if st and st.get("StatusReport") == "Failed":
                raise RuntimeError("Wordstat: отчёт упал")
            await asyncio.sleep(_POLL_DELAY)
        else:
            raise RuntimeError("Wordstat: отчёт не успел за отведённое время")
        rep = await _call("GetWordstatReport", report_id, token, http=http)
        out: Dict[str, int] = {}
        for block in rep.get("data") or []:
            phrase = (block.get("Phrase") or "").strip()
            rows = block.get("SearchedWith") or []
            # первая строка SearchedWith — сама фраза с суммарными показами
            shows = 0
            for r in rows:
                if (r.get("Phrase") or "").strip().lower() == phrase.lower():
                    shows = int(r.get("Shows") or 0)
                    break
            if not shows and rows:
                shows = int(rows[0].get("Shows") or 0)
            out[phrase] = shows
        return out
    finally:
        try:
            await _call("DeleteWordstatReport", report_id, token, http=http)
        except Exception:
            logger.debug("wordstat report cleanup failed", exc_info=True)


def _tier(total: int) -> str:
    if total >= TIER_BIG:
        return "крупный"
    if total >= TIER_MID:
        return "средний"
    return "узкий"


async def estimate_segments(user_id: str, run_id: str, *,
                            token: Optional[str] = None, http=None) -> dict:
    """Размер каждого сегмента = сумма показов Wordstat по его queries.
    Результат — в ран + секцией в документ исследования."""
    from backend.core.marketing.research_engine import (
        _update_run, append_doc_section, get_run)
    run = get_run(user_id, run_id)
    if not run:
        return {"success": False, "error": "исследование не найдено"}
    segments = run.get("segments") or []
    if not segments:
        return {"success": False, "error": "в ране нет сегментов"}
    tok = token or direct_token()
    if not tok:
        from backend.core.marketing.keys import yandex_direct_token
        tok = await yandex_direct_token(user_id)
    if not tok:
        return {"success": False, "error": config_hint()}

    all_phrases: List[str] = []
    for s in segments:
        all_phrases += (s.get("queries") or [])[:3]
    try:
        volumes = await wordstat_volumes(all_phrases, token=tok, http=http)
    except Exception as e:
        return {"success": False, "error": f"Wordstat: {e}"}

    sized = []
    for s in segments:
        qs = (s.get("queries") or [])[:3]
        per_q = {q: volumes.get(q.strip()) for q in qs}
        known = [v for v in per_q.values() if v is not None]
        total = sum(known)
        sized.append({
            "name": s.get("name"), "type": s.get("type"),
            "queries": per_q, "monthly_shows": total,
            "tier": _tier(total) if known else "нет данных",
        })
        s["sizing"] = {"monthly_shows": total,
                       "tier": _tier(total) if known else "нет данных"}

    stamp = datetime.utcnow().strftime("%Y-%m-%d")
    md = [f"## Размер сегментов (Wordstat, {stamp})", "",
          "_Показы в месяц по запросам сегмента — частоты реального "
          "спроса, не оценка LLM. Косвенные/донорские сегменты ищут "
          "другими словами — их размер этим не меряется полностью._", ""]
    for s in sized:
        md.append(f"### {s['name']} — {s['tier']} "
                  f"({s['monthly_shows']:,} показов/мес)".replace(",", " "))
        for q, v in s["queries"].items():
            md.append(f"- «{q}»: "
                      + (f"{v:,}".replace(",", " ") if v is not None
                         else "нет данных"))
        md.append("")
    md_block = "\n".join(md)

    _update_run(user_id, run_id, segments=segments, sizing=sized,
                report_markdown=(run.get("report_markdown") or "")
                                + "\n\n" + md_block)
    if run.get("document_id"):
        await append_doc_section(user_id, run["document_id"], md_block)
    return {"success": True, "sizing": sized, "markdown": md_block}
