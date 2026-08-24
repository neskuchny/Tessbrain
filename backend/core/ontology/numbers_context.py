# -*- coding: utf-8 -*-
"""Поставщик цифр компании для генеративных потоков (ТЗ/таскер, Mark,
исследования, автоматизации).

Разрыв, который закрывает: генераторы (финмодель, медиаплан, ТЗ) искали
данные только в графе встреч — то есть видели цифры, которые ПРОГОВОРИЛИ,
но не реальные ряды из подключённых таблиц/CRM и не единые метрики
(план/факт, сверка «встреча vs данные»). Этот модуль отдаёт одним вызовом
детерминированную выжимку обоих источников — без LLM, дёшево, never-raise.

numbers_block(user_id, query) → {"numbers": [...], "text": "...", "sources": [...]}
- numbers: записи в формате extracted_numbers таскера
  {metric, value, unit, source, is_verified}
- text: готовый блок для вставки в промпт (Mark, TZ-generator)
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_MAX_METRICS = 10
_MAX_DATASETS = 5
_MAX_COLUMNS = 12


def _tokens(text: str) -> set:
    return {t for t in re.split(r"[^\wа-яё]+", (text or "").lower())
            if len(t) > 3}


def _tok_match(a: set, b: set) -> bool:
    """Матч с учётом русской морфологии: «выручку» ↔ «выручка» — общий
    префикс или вложение токена."""
    for ta in a:
        for tb in b:
            if ta == tb or ta.startswith(tb) or tb.startswith(ta):
                return True
            if len(ta) >= 5 and len(tb) >= 5 and ta[:5] == tb[:5]:
                return True
    return False


def numbers_block(user_id: str, query_text: str = "") -> Dict[str, Any]:
    """Выжимка цифр компании: единые метрики (+план/факт, сверка) и
    заземлённые числовые колонки датасетов (статистика по таблице).
    query_text сужает выбор (токен-матч); пусто → топ по свежести."""
    out: Dict[str, Any] = {"numbers": [], "text": "", "sources": []}
    try:
        from backend.core.ontology.dataset_registry import ontology_enabled
        if not ontology_enabled():
            return out
    except Exception:
        return out
    q_tokens = _tokens(query_text)
    lines: List[str] = []

    # ── 1. Единые метрики (встречи + данные, план/факт) ──
    try:
        from backend.core.ontology.metric_registry import metrics_for_user
        reg = metrics_for_user(user_id)
        metrics = reg.list_metrics()
        if q_tokens:
            matched = [m for m in metrics
                       if _tok_match(_tokens(m.get("name", "")), q_tokens)]
            metrics = matched or metrics      # нет матчей → общий топ
        for m in metrics[:_MAX_METRICS]:
            if not m.get("points"):
                continue
            s = reg.summary(m["name"]) or {}
            unit = m.get("unit") or ""
            for stype, p in (s.get("latest") or {}).items():
                src_ru = {"meeting": "метрика (из встреч)",
                          "dataset": "метрика (из данных)",
                          "manual": "метрика (вручную)"}.get(stype, stype)
                out["numbers"].append({
                    "metric": m["name"], "value": f"{p['value']:g}",
                    "unit": unit, "source": src_ru,
                    "is_verified": stype == "dataset"})
                per = f" за {p['period']}" if p.get("period") else ""
                lines.append(f"• {m['name']}{per}: {p['value']:g} {unit} "
                             f"[{src_ru}]")
            plan = s.get("plan") or {}
            if plan.get("note"):
                lines.append(f"  🎯 {plan['note']}")
            if s.get("note"):
                lines.append(f"  ⚖️ {s['note']}")
            out["sources"].append(f"metric:{m['name']}")
    except Exception as e:
        logger.debug(f"numbers_block metrics skipped: {e}")

    # ── 2. Датасеты: заземлённые числовые колонки → статистика ──
    try:
        from backend.core.ontology.dataset_registry import unit_label
        from backend.core.ontology.dataset_service import registry_for_user
        dreg = registry_for_user(user_id)
        cols_used = 0
        for slim in dreg.list()[:_MAX_DATASETS]:
            rec = dreg.get(slim["dataset_id"])
            if not rec:
                continue
            grounding = (rec.get("ontology") or {}).get("grounding") or {}
            profile = rec.get("profile") or {}
            title = rec.get("title") or "датасет"
            for p in profile.get("columns") or []:
                if cols_used >= _MAX_COLUMNS:
                    break
                name = p.get("name") or ""
                g = grounding.get(name) or {}
                if p.get("dtype") != "number" or not g.get("known"):
                    continue
                if q_tokens and not (
                        _tok_match(_tokens(name), q_tokens)
                        or _tok_match(_tokens(title), q_tokens)
                        or _tok_match(_tokens(g.get("kpi") or ""), q_tokens)):
                    continue
                stats = p.get("stats") or {}
                if "sum" not in stats:
                    continue
                ulabel = unit_label(p)
                src = f"таблица «{title}» (на {str(rec.get('refreshed_at', ''))[:10]})"
                out["numbers"].append({
                    "metric": g.get("kpi") or name,
                    "value": f"{stats['sum']:g}",
                    "unit": ulabel, "source": src, "is_verified": True})
                lines.append(
                    f"• {name} [{src}]: сумма {stats['sum']:g} {ulabel}, "
                    f"среднее {stats.get('mean', 0):g}, "
                    f"мин {stats.get('min', 0):g} – макс {stats.get('max', 0):g}")
                cols_used += 1
            if cols_used:
                out["sources"].append(f"dataset:{title}")
    except Exception as e:
        logger.debug(f"numbers_block datasets skipped: {e}")

    if lines:
        out["text"] = ("ЦИФРЫ КОМПАНИИ (реальные данные: метрики и "
                       "подключённые таблицы; is_verified — из данных, "
                       "не из пересказа):\n" + "\n".join(lines))
    return out
