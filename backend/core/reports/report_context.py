# -*- coding: utf-8 -*-
"""Контекст методологического отчёта — из ЗНАНИЙ компании, а не из транскриптов.

Как было (и почему это неверно): отчёт «уровня McKinsey» строился на СЫРЫХ
транскриптах последних 25 встреч, каждый обрезанный до 5000 символов. Это плохо
дважды. Во-первых, 25 встреч не описывают компанию — у тенанта их сотни, и
случайные последние не дают картины. Во-вторых, сырой транскрипт — худший вид
контекста: система уже извлекла из него знания (решения, достижения, цели,
риски, KPI), а отчёт этот труд игнорировал и заново «вычитывал» смысл из
обрезков.

Как стало: контекст собирается из того, что мозг УЖЕ понял про компанию:
  1. Снапшот компании — синтез по ВСЕЙ памяти (продукты, проекты, люди, OKR,
     SWOT, проблемы, KPI, health) — без срезов списков;
  2. Граф за период — решения, достижения и провалы, цели, риски (с датами);
  3. Саммари встреч за период — ВСЕ, не 25, и целиком (саммари уже сжато);
  4. Честная статистика покрытия: сколько встреч в периоде, что вошло.

ОБРЕЗОК НЕТ. Если материала объективно больше бюджета модели, он не режется, а
СЖИМАЕТСЯ map-reduce'ом (LLM сворачивает пачками) — ни один факт не выпадает
молча, и в stats видно, что произошло. Never-raise: отказ любого источника
оставляет остальные, отчёт не падает.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Бюджет символов на блок саммари встреч. НЕ обрезка: при превышении блок
# сворачивается LLM'ом (map-reduce), а не отрезается.
_SUMMARY_BUDGET_CHARS = 120_000
_CONDENSE_BATCH_CHARS = 30_000


def _s(v: Any) -> str:
    return str(v or "").strip()


def _fmt_list(items: List[Any], *, bullet: str = "  •") -> List[str]:
    """Список объектов/строк → строки. БЕЗ срезов: показываем всё, что есть."""
    out: List[str] = []
    for it in items or []:
        if isinstance(it, dict):
            name = _s(it.get("name") or it.get("title") or it.get("goal")
                      or it.get("text") or it.get("description"))
            if not name:
                continue
            extra = []
            for k in ("status", "role", "stage", "priority", "deadline", "owner"):
                if _s(it.get(k)):
                    extra.append(f"{k}={_s(it[k])}")
            desc = _s(it.get("description"))
            line = f"{bullet} {name}"
            if extra:
                line += f" [{', '.join(extra)}]"
            if desc and desc != name:
                line += f" — {desc}"
            out.append(line)
        else:
            t = _s(it)
            if t:
                out.append(f"{bullet} {t}")
    return out


def format_company_snapshot(snap: Any) -> str:
    """Снапшот компании → текст ЦЕЛИКОМ (в отличие от to_text: без max_length
    и без срезов вроде products[:8] — для отчёта нужна вся картина)."""
    if snap is None:
        return ""
    d = snap.to_dict() if hasattr(snap, "to_dict") else dict(snap or {})
    L: List[str] = ["## ПРОФИЛЬ КОМПАНИИ (синтез по всей памяти)"]

    ident = [("Название", "name"), ("Отрасль", "industry"), ("Стадия", "stage"),
             ("Размер", "size"), ("Локация", "location"), ("Основана", "founded"),
             ("Статус", "current_status")]
    for label, key in ident:
        if _s(d.get(key)):
            L.append(f"{label}: {_s(d[key])}")
    for label, key in [("Описание", "description"), ("Миссия", "mission"),
                       ("Бизнес-модель", "business_model"),
                       ("Модель выручки", "revenue_model"),
                       ("Целевой рынок", "target_market")]:
        if _s(d.get(key)):
            L.append(f"{label}: {_s(d[key])}")

    blocks = [
        ("Продукты", "products"), ("Департаменты", "departments"),
        ("Команды", "teams"), ("Ключевые люди", "key_people"),
        ("Активные проекты", "active_projects"),
        ("Планируемые проекты", "planned_projects"),
        ("Стратегические цели", "strategic_goals"), ("OKR", "okrs"),
        ("Приоритеты сейчас", "current_priorities"),
        ("Вызовы сейчас", "current_challenges"),
        ("Сильные стороны", "strengths"), ("Слабые стороны", "weaknesses"),
        ("Возможности", "opportunities"), ("Угрозы", "threats"),
        ("Недавние достижения", "recent_achievements"),
        ("Текущие проблемы", "current_problems"),
        ("Конкуренты", "competitors"), ("Стек", "tech_stack"),
        ("Инструменты", "tools"), ("Методологии", "methodologies"),
    ]
    for title, key in blocks:
        rows = _fmt_list(d.get(key) or [])
        if rows:
            L.append(f"\n### {title} ({len(rows)})")
            L.extend(rows)

    # Числа — отдельно и явно: именно они идут в арифметическую проверку.
    for title, key in [("KPI", "kpis"), ("Финансовые KPI", "financial_kpis")]:
        val = d.get(key)
        if isinstance(val, dict) and val:
            L.append(f"\n### {title}")
            for k, v in val.items():
                L.append(f"  • {k}: {v}")
        elif isinstance(val, list) and val:
            L.append(f"\n### {title}")
            L.extend(_fmt_list(val))
    for label, key in [("Health score", "health_score"),
                       ("Productivity score", "productivity_score")]:
        if d.get(key) not in (None, ""):
            L.append(f"{label}: {d[key]}")
    if isinstance(d.get("trends"), (list, dict)) and d.get("trends"):
        L.append("\n### Тренды")
        L.extend(_fmt_list(d["trends"] if isinstance(d["trends"], list)
                           else [f"{k}: {v}" for k, v in d["trends"].items()]))
    return "\n".join(L)


async def _company_snapshot_text(user_id: str) -> str:
    """Снапшот компании (never-raise).

    storage_path — ОБЯЗАТЕЛЬНО per-user. Дефолтный `data/snapshots` общий: с ним
    __init__ поднимает с диска легаси-снапшот всех тенантов сразу, и в отчёт
    попадает чужая компания (та же грабля, что чинилась в snapshots.py).
    force_regenerate=False: берём то, что построила ночная консолидация, не
    дёргаем LLM на каждый отчёт."""
    try:
        from backend.core.sleep.enhanced_snapshot import EnhancedSnapshotGenerator
        from backend.core.store.graph_view import merged_graph_view_for_user
        from backend.core.store.tenant_paths import snapshots_dir_for_user
        gb = await merged_graph_view_for_user(user_id, use_networkx=None)
        try:
            gen = EnhancedSnapshotGenerator(
                graph_builder=gb,
                storage_path=str(snapshots_dir_for_user(user_id)),
            )
            gen.user_id = user_id
            snap = await gen.get_company_snapshot(force_regenerate=False)
            return format_company_snapshot(snap)
        finally:
            try:
                await gb.close(save=False)
            except Exception:
                pass
    except Exception as e:
        logger.warning("report context: снапшот компании недоступен: %s", e)
        return ""


def _matches_focus(text: str, keywords: Optional[List[str]]) -> bool:
    """Относится ли текст к фокусу роли. Пустой фокус = относится всё.

    Это ФИЛЬТР РЕЛЕВАНТНОСТИ, а не обрезка: ролевой отчёт («неделя глазами
    маркетинга») по определению смотрит на свой срез; сколько отсеяно —
    честно видно в stats."""
    if not keywords:
        return True
    low = (text or "").lower()
    return any(k in low for k in keywords)


async def _domain_snapshots_text(user_id: str,
                                 domains: Optional[List[str]]) -> str:
    """Накопленные ночные снапшоты доменов (маркетинг/продажи/...): хроника
    с датами, что сработало/нет — то, чего нет в общем снапшоте компании."""
    if not domains:
        return ""
    parts: List[str] = []
    try:
        from backend.core.sleep.domain_snapshots import (
            load_domains,
            read_domain_snapshot,
        )
        registry = load_domains()
        for d in domains:
            body = read_domain_snapshot(user_id, d, max_chars=20_000)
            if body.strip():
                title = _s((registry.get(d) or {}).get("title")) or d
                parts.append(f"## НАКОПЛЕННАЯ КАРТИНА: {title}\n{body.strip()}")
    except Exception as e:
        logger.warning("report context: доменные снапшоты недоступны: %s", e)
    return "\n\n".join(parts)


async def _graph_facts_text(user_id: str, days_back: int,
                            keywords: Optional[List[str]] = None) -> tuple:
    """Решения / достижения / цели / риски из графа за период.

    Это и есть «то, что мозг уже понял»: компактно, датировано, с атрибуцией —
    в отличие от сырого транскрипта. Тенант-скоуп обязателен."""
    since = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
    want = [("Decision", "Решения"), ("Achievement", "Достижения и провалы"),
            ("Goal", "Цели"), ("Risk", "Риски"), ("Insight", "Инсайты")]
    blocks: List[str] = []
    counts: Dict[str, int] = {}
    try:
        from backend.core.store.graph_view import merged_graph_view_for_user
        gb = await merged_graph_view_for_user(user_id, use_networkx=None)
        if not getattr(gb, "connected", False):
            return "", counts
        try:
            for label, title in want:
                try:
                    nodes = await gb.get_all_nodes_async(
                        label=label, limit=2000, tenant_id=user_id or None)
                except Exception:
                    continue
                rows: List[str] = []
                for n in nodes or []:
                    dt = _s(n.get("date") or n.get("created_at")
                            or n.get("timestamp"))
                    if dt and dt < since:
                        continue          # вне периода
                    text = _s(n.get("summary") or n.get("description")
                              or n.get("name") or n.get("title") or n.get("text"))
                    if not text:
                        continue
                    if not _matches_focus(text, keywords):
                        continue      # вне фокуса роли (см. _matches_focus)
                    meta = []
                    for k in ("status", "impact", "priority_level", "outcome",
                              "root_cause", "owner", "assignee"):
                        if _s(n.get(k)):
                            meta.append(f"{k}={_s(n[k])}")
                    line = f"  • [{dt[:10] or 'дата н/д'}] {text}"
                    if meta:
                        line += f" ({', '.join(meta)})"
                    rows.append(line)
                if rows:
                    counts[label] = len(rows)
                    blocks.append(f"\n### {title} ({len(rows)})")
                    blocks.extend(rows)
        finally:
            try:
                await gb.close(save=False)
            except Exception:
                pass
    except Exception as e:
        logger.warning("report context: факты графа недоступны: %s", e)
    text = ("## ЧТО ПРОИЗОШЛО ЗА ПЕРИОД (из графа знаний)"
            + "\n".join(blocks)) if blocks else ""
    return text, counts


async def _meeting_summaries(user_id: str, *, days_back: int,
                             project_id: Optional[str],
                             folder_id: Optional[str],
                             keywords: Optional[List[str]] = None) -> tuple:
    """Саммари ВСЕХ встреч периода (не 25) и БЕЗ обрезки каждого.

    Саммари — уже сжатая выжимка, её резать бессмысленно и вредно."""
    since = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
    try:
        from backend.db.supabase_client import get_supabase_client
        params = {
            "user_id": f"eq.{user_id}",
            "created_at": f"gte.{since}",
            "select": "id,title,created_at,summary",
            "order": "created_at.asc",
            "limit": "2000",
        }
        if project_id:
            params["project_id"] = f"eq.{project_id}"
        if folder_id:
            params["folder_id"] = f"eq.{folder_id}"
        rows = await get_supabase_client()._request(
            "GET", "/rest/v1/meetings", params=params)
    except Exception as e:
        logger.warning("report context: саммари встреч недоступны: %s", e)
        return "", 0, 0

    rows = rows or []
    parts: List[str] = []
    used = 0
    for m in rows:
        s = _s(m.get("summary"))
        if not s:
            continue
        if not _matches_focus(f"{_s(m.get('title'))}\n{s}", keywords):
            continue      # встреча не про фокус роли
        used += 1
        parts.append(f"\n--- {_s(m.get('title')) or 'Встреча'} "
                     f"({_s(m.get('created_at'))[:10]}) ---\n{s}")
    text = ("## ХРОНОЛОГИЯ ВСТРЕЧ ЗА ПЕРИОД (саммари)" + "\n".join(parts)) if parts else ""
    return text, len(rows), used


async def _condense(user_id: str, text: str, *, what: str) -> str:
    """Свернуть объёмный блок map-reduce'ом. НЕ обрезка: модель сжимает пачками,
    сохраняя факты, даты и цифры. Сбой → возвращаем исходный текст как есть."""
    try:
        from backend.core.llm.workload_policy import generate_for_workload
        chunks: List[str] = []
        buf: List[str] = []
        size = 0
        for line in text.split("\n"):
            buf.append(line)
            size += len(line) + 1
            if size >= _CONDENSE_BATCH_CHARS:
                chunks.append("\n".join(buf))
                buf, size = [], 0
        if buf:
            chunks.append("\n".join(buf))

        outs: List[str] = []
        for i, ch in enumerate(chunks, 1):
            prompt = (
                f"Сожми фрагмент {i}/{len(chunks)} материалов компании ({what}). "
                "Сохрани ВСЕ: даты, цифры, имена, решения, проблемы, результаты. "
                "Убери только повторы и воду. Ничего не выдумывай и не обобщай "
                "в ущерб фактам.\n\n" + ch)
            got = await generate_for_workload(user_id or "", "chat", prompt)
            outs.append(_s(got) or ch)
        logger.info("report context: %s свёрнут map-reduce (%d пачек, %d→%d симв)",
                    what, len(chunks), len(text), len("\n".join(outs)))
        return "\n".join(outs)
    except Exception as e:
        logger.warning("report context: сжатие пропущено (%s) — отдаём как есть", e)
        return text


async def build_company_context(user_id: str, *, days_back: int = 90,
                                project_id: Optional[str] = None,
                                folder_id: Optional[str] = None,
                                domains: Optional[List[str]] = None,
                                focus_keywords: Optional[List[str]] = None
                                ) -> Dict[str, Any]:
    """Собрать контекст отчёта из знаний компании.

    domains/focus_keywords — фокус РОЛИ (маркетинг/продажи/технологии):
    факты графа и саммари встреч фильтруются по релевантности фокусу, плюс
    подмешивается накопленный ночной снапшот домена. Без фокуса — вся
    компания, как раньше. Если keywords не заданы, но заданы domains —
    keywords берутся из реестра доменов.

    Возврат: {"text": str, "stats": {...}}. text пуст → вызывающий сам решает,
    что делать (например, честно сказать «данных нет»)."""
    keywords = [k.lower() for k in (focus_keywords or []) if _s(k)]
    if domains and not keywords:
        try:
            from backend.core.sleep.domain_snapshots import load_domains
            reg = load_domains()
            for d in domains:
                keywords.extend(
                    str(k).lower() for k in (reg.get(d) or {}).get("keywords", []))
        except Exception:
            logger.debug("focus keywords from registry skipped", exc_info=True)

    snap_text = await _company_snapshot_text(user_id)
    domain_text = await _domain_snapshots_text(user_id, domains)
    facts_text, fact_counts = await _graph_facts_text(
        user_id, days_back, keywords or None)
    sum_text, meetings_total, meetings_used = await _meeting_summaries(
        user_id, days_back=days_back, project_id=project_id,
        folder_id=folder_id, keywords=keywords or None)

    condensed = False
    if len(sum_text) > _SUMMARY_BUDGET_CHARS:
        sum_text = await _condense(user_id, sum_text, what="саммари встреч")
        condensed = True

    blocks = [b for b in (snap_text, domain_text, facts_text, sum_text) if b]
    text = "\n\n".join(blocks)
    stats = {
        "has_snapshot": bool(snap_text),
        "focus_domains": list(domains or []),
        "focus_keywords_used": len(keywords),
        "has_domain_snapshot": bool(domain_text),
        "graph_facts": fact_counts,
        "graph_facts_total": sum(fact_counts.values()) if fact_counts else 0,
        "meetings_in_period": meetings_total,
        "meetings_with_summary": meetings_used,
        "summaries_condensed": condensed,
        "context_chars": len(text),
        "days_back": days_back,
        "source": "knowledge",   # не сырые транскрипты
    }
    logger.info("📚 Контекст отчёта: снапшот=%s, фактов графа=%d, встреч=%d/%d, "
                "%d символов%s", stats["has_snapshot"], stats["graph_facts_total"],
                meetings_used, meetings_total, stats["context_chars"],
                " (свёрнут)" if condensed else "")
    return {"text": text, "stats": stats}


__all__ = ["build_company_context", "format_company_snapshot"]
