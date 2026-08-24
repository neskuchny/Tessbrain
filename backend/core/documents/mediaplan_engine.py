# -*- coding: utf-8 -*-
"""Медиаплан: расчёт КОДОМ, LLM только извлекает параметры и обосновывает.

Целевой флоу владельца (рекламное агентство): встреча с клиентом →
система понимает потребности, поднимает старые данные клиента и примеры
медиапланов компании (заготовки/датасеты) → LLM предлагает СПЛИТ бюджета
по каналам и переписывает СТАВКИ из материалов в JSON (не выдумывая) →
каскадную арифметику считает Decimal-код:

    показы = бюджет / CPM × 1000
    клики  = бюджет / CPC   ЛИБО   показы × CTR%
    лиды   = клики × CR%    ЛИБО   бюджет / CPA
    CPA    = бюджет / лиды

→ на выходе таблица (xlsx готовым рендером) + обоснование + честные
пометки: какие ставки из материалов, какие предположены, чего не хватило.
"""
from __future__ import annotations

import json
import logging
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional

from backend.core.documents.money import _dec

logger = logging.getLogger(__name__)

_CENT = Decimal("0.01")
_INT = Decimal("1")

COLUMNS = ["Канал", "Бюджет, ₽", "CPM, ₽", "Показы", "CPC, ₽", "CTR, %",
           "Клики", "CR, %", "Лиды", "CPA, ₽"]


def _q2(v: Decimal) -> Decimal:
    return v.quantize(_CENT, rounding=ROUND_HALF_UP)


def _qi(v: Decimal) -> Decimal:
    return v.quantize(_INT, rounding=ROUND_HALF_UP)


def compute_mediaplan(channels: List[dict],
                      total_budget: Optional[Any] = None) -> dict:
    """Каскадный расчёт по каналам. channel: {name, budget?|share_pct?,
    cpm?, cpc?, ctr_pct?, cr_pct?, cpa?}. Возвращает {columns, rows,
    totals, notes} — числа Decimal-строками, готово в xlsx/показ."""
    total = _dec(total_budget) if total_budget not in (None, "") else None
    notes: List[str] = []
    rows: List[dict] = []
    sum_budget = Decimal("0")
    sum_impr = Decimal("0")
    sum_clicks = Decimal("0")
    sum_leads = Decimal("0")

    for ch in channels or []:
        name = str(ch.get("name") or "Канал").strip()[:60]
        budget = _dec(ch.get("budget")) if ch.get("budget") not in (None, "") else None
        if budget is None and ch.get("share_pct") not in (None, ""):
            if total is None:
                notes.append(f"«{name}»: указана доля, но нет общего бюджета")
            else:
                budget = _q2(total * _dec(ch["share_pct"]) / 100)
        if budget is None or budget <= 0:
            notes.append(f"«{name}»: нет бюджета — канал пропущен в расчёте")
            continue

        cpm = _dec(ch.get("cpm")) if ch.get("cpm") not in (None, "") else None
        cpc = _dec(ch.get("cpc")) if ch.get("cpc") not in (None, "") else None
        ctr = _dec(ch.get("ctr_pct")) if ch.get("ctr_pct") not in (None, "") else None
        cr = _dec(ch.get("cr_pct")) if ch.get("cr_pct") not in (None, "") else None
        cpa_in = _dec(ch.get("cpa")) if ch.get("cpa") not in (None, "") else None

        impressions = _qi(budget / cpm * 1000) if cpm and cpm > 0 else None
        clicks: Optional[Decimal] = None
        if cpc and cpc > 0:
            clicks = _qi(budget / cpc)
        elif impressions is not None and ctr is not None:
            clicks = _qi(impressions * ctr / 100)
        leads: Optional[Decimal] = None
        if clicks is not None and cr is not None:
            leads = _qi(clicks * cr / 100)
        elif cpa_in and cpa_in > 0:
            leads = _qi(budget / cpa_in)
        cpa = _q2(budget / leads) if leads and leads > 0 else None

        missing = []
        if impressions is None and clicks is None:
            missing.append("CPM или CPC")
        if leads is None:
            missing.append("CR или CPA")
        if missing:
            notes.append(f"«{name}»: не хватает ставок ({', '.join(missing)}) "
                         "— строка посчитана частично")

        rows.append({
            "Канал": name,
            "Бюджет, ₽": str(_q2(budget)),
            "CPM, ₽": str(_q2(cpm)) if cpm else "",
            "Показы": str(impressions) if impressions is not None else "",
            "CPC, ₽": str(_q2(cpc)) if cpc else "",
            "CTR, %": str(ctr) if ctr is not None else "",
            "Клики": str(clicks) if clicks is not None else "",
            "CR, %": str(cr) if cr is not None else "",
            "Лиды": str(leads) if leads is not None else "",
            "CPA, ₽": str(cpa) if cpa is not None else "",
        })
        sum_budget += budget
        sum_impr += impressions or 0
        sum_clicks += clicks or 0
        sum_leads += leads or 0

    if total is not None and rows:
        diff = total - sum_budget
        if abs(diff) > Decimal("1"):
            notes.append(f"сплит каналов ({_q2(sum_budget)} ₽) не сходится с "
                         f"общим бюджетом ({_q2(total)} ₽): разница {_q2(diff)} ₽")

    totals = {
        "Канал": "ИТОГО",
        "Бюджет, ₽": str(_q2(sum_budget)),
        "CPM, ₽": "", "CPC, ₽": "", "CTR, %": "", "CR, %": "",
        "Показы": str(_qi(sum_impr)) if sum_impr else "",
        "Клики": str(_qi(sum_clicks)) if sum_clicks else "",
        "Лиды": str(_qi(sum_leads)) if sum_leads else "",
        "CPA, ₽": str(_q2(sum_budget / sum_leads)) if sum_leads else "",
    }
    return {"columns": COLUMNS, "rows": rows, "totals": totals,
            "notes": notes}


def formulas_table(table: dict) -> List[dict]:
    """Строки медиаплана для экспорта с ЖИВЫМИ формулами вместо чисел в
    производных колонках: менеджер меняет бюджет/ставку в ячейке — таблица
    пересчитывается сама (Excel и Google Sheets, USER_ENTERED).

    Каскад тот же, что в compute_mediaplan; формула ставится, только если
    в строке есть её входы (иначе остаётся посчитанное значение/пусто).
    Колонки: A Канал, B Бюджет, C CPM, D Показы, E CPC, F CTR, G Клики,
    H CR, I Лиды, J CPA."""
    cols = table.get("columns") or COLUMNS
    rows = list(table.get("rows") or [])
    out: List[dict] = []

    def _f(row: dict, col: str) -> bool:
        return str(row.get(col, "")).strip() not in ("", "None")

    def _typed(v: Any) -> Any:
        try:
            return float(v) if str(v).strip() not in ("", "None") else ""
        except (TypeError, ValueError):
            return v

    for i, r in enumerate(rows):
        n = i + 2  # 1 — заголовок
        t = {c: (_typed(r.get(c, "")) if c != "Канал" else r.get(c, ""))
             for c in cols}
        if _f(r, "CPM, ₽") and _f(r, "Бюджет, ₽"):
            t["Показы"] = f"=ROUND(B{n}/C{n}*1000,0)"
        if _f(r, "CPC, ₽") and _f(r, "Бюджет, ₽"):
            t["Клики"] = f"=ROUND(B{n}/E{n},0)"
        elif _f(r, "CPM, ₽") and _f(r, "CTR, %"):
            t["Клики"] = f"=ROUND(D{n}*F{n}/100,0)"
        if _f(r, "CR, %") and str(t.get("Клики", "")).strip() not in ("", "None"):
            t["Лиды"] = f"=ROUND(G{n}*H{n}/100,0)"
        if str(t.get("Лиды", "")).strip() not in ("", "None"):
            t["CPA, ₽"] = f"=ROUND(B{n}/I{n},2)"
        out.append(t)

    totals = table.get("totals")
    if totals and rows:
        last = len(rows) + 1
        tn = last + 1
        t = {c: _typed(totals.get(c, "")) if c != "Канал" else "ИТОГО"
             for c in cols}
        t["Бюджет, ₽"] = f"=SUM(B2:B{last})"
        for col, letter in (("Показы", "D"), ("Клики", "G"), ("Лиды", "I")):
            if any(str(o.get(col, "")).strip() not in ("", "None") for o in out):
                t[col] = f"=SUM({letter}2:{letter}{last})"
        if str(t.get("Лиды", "")).strip() not in ("", "None"):
            t["CPA, ₽"] = f"=ROUND(B{tn}/I{tn},2)"
        out.append(t)
    return out


# ── LLM-извлечение параметров (не считает — только переписывает) ────────

async def _methodology_text(user_id: str) -> str:
    """Свежая методология жанра «медиаплан» (МОРМ-lite, вкладка Анализ):
    правила с рамками и trust — контекст выше бенчмарков. Нет — пусто."""
    try:
        from backend.core.documents import doc_store
        rows = await doc_store.list_documents(user_id, "methodology")
        for r in rows:  # новые сверху
            blob = " ".join([str(r.get("title") or ""),
                             str(r.get("topic") or ""),
                             " ".join(r.get("keywords") or [])]).lower()
            if "медиаплан" in blob:
                return (r.get("content_markdown") or "")[:6000]
    except Exception:
        logger.debug("mediaplan methodology skipped", exc_info=True)
    return ""


async def build_mediaplan(user_id: str, *, client_query: str,
                          meeting_ids: List[str],
                          extra_context: str = "",
                          preset_ids: Optional[List[str]] = None,
                          total_budget: Optional[Any] = None,
                          llm=None) -> dict:
    """Собрать медиаплан из контекста мозга: встречи + заготовки со
    ставками + цифры компании. LLM отдаёт JSON каналов (ставки ТОЛЬКО из
    материалов, с source и confidence), код считает таблицу."""
    if llm is None:
        from backend.core.llm.router import get_llm_router
        llm = get_llm_router()
    from backend.core.documents.fill_engine import _meetings_text, presets_text

    parts: List[str] = []
    meetings = await _meetings_text(user_id, meeting_ids)
    if meetings:
        parts.append(meetings)
    pt = presets_text(user_id, list(preset_ids or [])[:10])
    if pt:
        parts.append("=== ЗАГОТОВКИ КОМПАНИИ (ставки/прайсы/примеры) ===\n" + pt)
    method_md = await _methodology_text(user_id)
    if method_md:
        parts.append("=== МЕТОДОЛОГИЯ КОМПАНИИ (правила выведены из ваших "
                     "медиапланов и прошли гейт на отложенных примерах) ===\n"
                     + method_md)
    if extra_context:
        parts.append("=== ДОП. КОНТЕКСТ ===\n" + extra_context)
    try:
        from backend.core.ontology.numbers_context import numbers_block
        nb = numbers_block(user_id, f"медиаплан бюджет {client_query}")
        if nb.get("text"):
            parts.append(nb["text"])
    except Exception:
        logger.debug("mediaplan numbers_block skipped", exc_info=True)
    context = "\n\n".join(parts)[:50000]
    budget_hint = (str(total_budget) if total_budget not in (None, "")
                   else "возьми из материалов, если озвучен; иначе null")

    prompt = (
        "Ты — медиапланер. По материалам ниже предложи СПЛИТ рекламного "
        f"бюджета по каналам для клиента: {client_query or 'клиент'}.\n"
        "ЖЁСТКИЕ ПРАВИЛА ПО СТАВКАМ (CPM/CPC/CTR/CR/CPA):\n"
        "1. ПРИОРИТЕТ №1 — ставки ЭТОГО клиента: озвучены на встрече, из "
        "его прошлых кампаний, из цифр компании по нему. Тогда "
        "rate_origin='client_data'.\n"
        "1.5. Если есть блок МЕТОДОЛОГИЯ КОМПАНИИ — её правила выше "
        "бенчмарков: применяй правило, ТОЛЬКО если клиент попадает в его "
        "рамку применимости; тогда rate_origin='methodology', в source — "
        "правило и его trust. Клиент вне рамки → правило НЕ применять "
        "(переходи к п.2).\n"
        "2. Заготовки/примеры компании — это БЕНЧМАРКИ-ОРИЕНТИРЫ, НЕ "
        "истина: от ниши, гео, конкуренции и сезона реальные ставки "
        "отличаются в разы. Если берёшь бенчмарк — ОБЯЗАН оценить, чем "
        "клиент отличается, и СКОРРЕКТИРОВАТЬ ставку (например ниша "
        "недвижимости в Москве → CPC выше бенчмарка). Тогда "
        "rate_origin='adjusted_benchmark', укажи baseline (что было в "
        "бенчмарке) и adjustment_reason (почему и в какую сторону "
        "поправил). Скопировал бенчмарк без изменений, потому что нет "
        "оснований корректировать — rate_origin='benchmark_asis' (менеджер "
        "увидит, что цифру надо перепроверить).\n"
        "3. Нет ни данных клиента, ни применимого бенчмарка — ставь null "
        "и rate_origin='missing'. НЕ выдумывай из головы.\n"
        "4. НИЧЕГО не перемножай и не суммируй — расчёт сделает код.\n"
        "5. Доли каналов (share_pct) в сумме = 100.\n"
        f"6. Общий бюджет: {budget_hint}.\n\n"
        f"МАТЕРИАЛЫ:\n{context}\n\n"
        'Ответь ТОЛЬКО JSON: {"total_budget": 0 или null, "channels": '
        '[{"name": "...", "share_pct": 0, "cpm": null, "cpc": null, '
        '"ctr_pct": null, "cr_pct": null, "cpa": null, '
        '"rate_origin": "client_data|methodology|adjusted_benchmark|benchmark_asis|missing", '
        '"baseline": "ставка-ориентир из бенчмарка, если корректировал", '
        '"adjustment_reason": "чем клиент отличается и почему ставка '
        'скорректирована именно так (или null)", '
        '"source": "откуда взято (цитата/заготовка)", '
        '"why": "почему канал подходит клиенту"}], '
        '"assumptions": ["что предположено"], '
        '"rationale": "3-5 предложений: почему такой сплит под потребности '
        'клиента, с опорой на встречу"}')
    try:
        data = await llm.generate_json(prompt=prompt, temperature=0.2)
    except Exception as e:
        logger.warning(f"mediaplan LLM failed: {e}")
        return {"success": False,
                "error": f"LLM недоступен для извлечения параметров: {e}"}
    if not isinstance(data, dict) or not data.get("channels"):
        return {"success": False,
                "error": "не удалось извлечь каналы из материалов — "
                         "добавьте заготовку со ставками или пример медиаплана"}

    channels = [c for c in data["channels"] if isinstance(c, dict)][:15]
    budget = total_budget or data.get("total_budget")
    table = compute_mediaplan(channels, total_budget=budget)
    missing = [c.get("name") for c in channels
               if (c.get("rate_origin") or c.get("confidence")) == "missing"]
    asis = [c.get("name") for c in channels
            if (c.get("rate_origin") or "") == "benchmark_asis"]
    if asis:
        table["notes"].append(
            "бенчмарк без корректировки (перепроверьте ставки под клиента): "
            + ", ".join(str(a) for a in asis if a))
    return {
        "success": True,
        "table": table,
        "rationale": str(data.get("rationale") or "")[:2000],
        "assumptions": [str(a)[:300] for a in (data.get("assumptions") or [])][:10],
        "channels_meta": [{
            "name": c.get("name"), "source": (c.get("source") or "")[:200],
            "rate_origin": (c.get("rate_origin") or c.get("confidence")
                            or "assumed"),
            "baseline": (str(c.get("baseline"))[:120]
                         if c.get("baseline") not in (None, "") else None),
            "adjustment_reason": (c.get("adjustment_reason") or "")[:300] or None,
            "why": (c.get("why") or "")[:300],
        } for c in channels],
        "total_budget": str(_dec(budget)) if budget not in (None, "") else None,
        "rates_missing": [m for m in missing if m],
    }
