# -*- coding: utf-8 -*-
"""Факт-гейт стратегии (фаза 3): симуляция против реальной кампании.

Панель персон — генератор гипотез; здесь гипотезы встречают факт.
Менеджер приносит цифры кампании по вариантам посыла (показы/клики/лиды —
из Директа/Метрики руками; авто-pull — когда появятся ключи кампаний),
КОД считает CTR/CR/CPA и сверяет ПОРЯДОК: если панель сказала «вариант А
лучше Б», а факт говорит наоборот — это дрейф симуляции, он записывается
в документ исследования (пере-гейт паттерн МОРМ: не стираем — помечаем).

Ничего не «оценивается» LLM: и метрики, и сверка рангов — чистый код.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


def _ctr(impressions: float, clicks: float) -> Optional[float]:
    return round(clicks / impressions * 100, 2) if impressions else None


def compute_fact_rows(facts: List[dict]) -> List[dict]:
    """Метрики кампании кодом: CTR, CR, CPA. Вход: [{label, impressions,
    clicks, leads?, spend?}]."""
    rows = []
    for f in facts or []:
        if not isinstance(f, dict) or not str(f.get("label") or "").strip():
            continue

        def _num(key):
            try:
                v = float(f.get(key) or 0)
                return v if v >= 0 else 0.0
            except (TypeError, ValueError):
                return 0.0
        imp, clk, leads, spend = (_num("impressions"), _num("clicks"),
                                  _num("leads"), _num("spend"))
        rows.append({
            "label": str(f["label"]).strip()[:200],
            "impressions": int(imp), "clicks": int(clk),
            "leads": int(leads), "spend": spend,
            "ctr_pct": _ctr(imp, clk),
            "cr_pct": (round(leads / clk * 100, 2) if clk else None),
            "cpa": (round(spend / leads, 2) if leads and spend else None),
        })
    return rows


def _norm(s: str) -> str:
    return re.sub(r"[^а-яёa-z0-9]+", " ", (s or "").lower()).strip()


def match_simulations(rows: List[dict], sim_history: List[dict]) -> List[dict]:
    """Привязать варианты фактов к симуляциям панели (по вхождению
    нормализованного label в message или наоборот)."""
    out = []
    for r in rows:
        ln = _norm(r["label"])
        best = None
        for s in sim_history:
            mn = _norm(s.get("message") or "")
            if not mn:
                continue
            if ln in mn or mn in ln or (
                    len(ln) > 15 and len(mn) > 15
                    and (ln[:25] in mn or mn[:25] in ln)):
                best = s
        out.append({**r, "predicted_trust": (best or {}).get("avg_trust"),
                    "sim_matched": bool(best)})
    return out


def rank_agreement(rows: List[dict]) -> Tuple[List[dict], Optional[float]]:
    """Согласие порядков: панель (predicted_trust) против факта (CTR).
    Возвращает (вердикты по парам, доля согласных пар). Чистый код."""
    comparable = [r for r in rows
                  if r.get("predicted_trust") is not None
                  and r.get("ctr_pct") is not None]
    verdicts: List[dict] = []
    if len(comparable) < 2:
        return verdicts, None
    agree = 0
    total = 0
    for i in range(len(comparable)):
        for j in range(i + 1, len(comparable)):
            a, b = comparable[i], comparable[j]
            dp = a["predicted_trust"] - b["predicted_trust"]
            df = a["ctr_pct"] - b["ctr_pct"]
            if dp == 0 or df == 0:
                continue
            total += 1
            ok = (dp > 0) == (df > 0)
            agree += ok
            verdicts.append({
                "pair": f"«{a['label']}» vs «{b['label']}»",
                "verdict": "держится" if ok else "дрейф",
                "detail": (f"панель: {a['predicted_trust']} vs "
                           f"{b['predicted_trust']} · факт CTR: "
                           f"{a['ctr_pct']}% vs {b['ctr_pct']}%"),
            })
    return verdicts, (round(agree / total, 2) if total else None)


async def apply_campaign_facts(user_id: str, run_id: str,
                               facts: List[dict]) -> dict:
    """Принять факты кампании, посчитать метрики, сверить с панелью,
    записать «Факт-гейт» в ран и документ исследования."""
    from backend.core.marketing.research_engine import (
        _store_dir, _update_run, append_doc_section, get_run)
    run = get_run(user_id, run_id)
    if not run:
        return {"success": False, "error": "исследование не найдено"}
    rows = compute_fact_rows(facts)
    if not rows:
        return {"success": False,
                "error": "нет валидных строк фактов (label + цифры)"}

    # история симуляций панели этого пользователя
    sim_history: List[dict] = []
    try:
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", user_id)[:64] or "anon"
        p = _store_dir() / f"simulations_{safe}.json"
        sim_history = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    rows = match_simulations(rows, sim_history)
    verdicts, agreement = rank_agreement(rows)

    stamp = datetime.utcnow().strftime("%Y-%m-%d")
    md = [f"## Факт-гейт кампании ({stamp})", "",
          "_Метрики и сверка рангов посчитаны кодом. Это и есть мерило "
          "симуляций: панель — гипотезы, CTR — факт._", "",
          "| Вариант | Показы | Клики | CTR | Лиды | CR | CPA | Панель |",
          "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append(
            f"| {r['label'][:60]} | {r['impressions']} | {r['clicks']} | "
            f"{r['ctr_pct'] if r['ctr_pct'] is not None else '—'}% | "
            f"{r['leads'] or '—'} | "
            f"{r['cr_pct'] if r['cr_pct'] is not None else '—'} | "
            f"{r['cpa'] if r['cpa'] is not None else '—'} | "
            f"{r['predicted_trust'] if r['sim_matched'] else 'не сопоставлен'} |")
    md.append("")
    if verdicts:
        for v in verdicts:
            icon = "✅" if v["verdict"] == "держится" else "🔴"
            md.append(f"- {icon} {v['pair']} — {v['verdict']}: {v['detail']}")
        md.append("")
        if agreement is not None:
            md.append(f"**Панель угадала порядок в {agreement:.0%} пар.** "
                      + ("Симуляции этого корпуса можно осторожно "
                         "использовать для приоритизации."
                         if agreement >= 0.7 else
                         "Симуляциям этого корпуса доверять рано — "
                         "нужен пересбор корпуса или больше фактов."))
    else:
        md.append("_Сверка с панелью не построена: нужно ≥2 вариантов, "
                  "сопоставленных с симуляциями (label должен перекликаться "
                  "с посылом, который гоняли через панель)._")
    md_block = "\n".join(md)

    fact_entry = {"at": datetime.utcnow().isoformat(), "rows": rows,
                  "verdicts": verdicts, "agreement": agreement}
    _update_run(user_id, run_id,
                facts=(run.get("facts") or []) + [fact_entry],
                report_markdown=(run.get("report_markdown") or "")
                                + "\n\n" + md_block)
    if run.get("document_id"):
        await append_doc_section(user_id, run["document_id"], md_block)
    return {"success": True, "rows": rows, "verdicts": verdicts,
            "agreement": agreement, "markdown": md_block}
