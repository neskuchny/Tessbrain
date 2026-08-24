# -*- coding: utf-8 -*-
"""Мост «Исследования Марка → контекст чата».

Пользователь хочет ПОГОВОРИТЬ с исследованием: «что им всем сказать?
какой посыл понравится?». Чат умеет брать документы в контекст (сайдбар →
Контекст чата → Документы) — сюда и складываем: один KB-документ
«Аудитория и персоны: <продукт>» с отчётом, карточками персон и историей
панелей реакций. Upsert по названию — повторный экспорт обновляет, а не
плодит дубли. Дальше обычный чат Brain отвечает по этому документу.
"""
from __future__ import annotations

import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

_TABLE = "/rest/v1/documents"
_CAP = 60000


def _persona_md(p: dict) -> str:
    md = [f"### {p.get('name')}",
          f"- Сегмент: {p.get('segment') or '—'}",
          f"- Портрет: {p.get('portrait') or '—'}"]
    if p.get("goals"):
        md.append("- Цели: " + "; ".join(p["goals"]))
    for pain in (p.get("pains") or []):
        icon = "📍" if pain.get("origin") == "field" else "💡"
        line = f"- {icon} Боль: {pain.get('text')}"
        if pain.get("origin") == "field" and pain.get("quote"):
            line += f" («{pain['quote']}»)"
        md.append(line)
    if p.get("lexicon"):
        md.append("- Её лексикон: " + ", ".join(p["lexicon"]))
    if p.get("objections"):
        md.append("- Что оттолкнёт: " + "; ".join(p["objections"]))
    if p.get("tone"):
        md.append(f"- Тон для неё: {p['tone']}")
    return "\n".join(md)


def _simulation_md(s: dict) -> str:
    md = [f"### Посыл: «{s.get('message')}»",
          f"Доверие панели: {s.get('avg_trust')}/5 · откликнулись: "
          f"{s.get('would_respond')}/{s.get('panel_size')}"]
    for r in (s.get("reactions") or []):
        md.append(f"- **{r.get('persona')}** ({r.get('trust_1_5')}/5, "
                  f"{'откликнется' if r.get('would_respond') else 'мимо'}): "
                  f"«{r.get('first_impression')}»")
        if r.get("repels"):
            md.append(f"  - Оттолкнуло: {'; '.join(r['repels'])}")
        if r.get("say_it_better"):
            md.append(f"  - Сказать ей так: «{r['say_it_better']}»")
    sk = s.get("skeptic") or {}
    if sk.get("fix_first"):
        md.append(f"- 🗡 Скептик, первая правка: {sk['fix_first']}")
    return "\n".join(md)


def build_audience_doc_md(*, product: str, run: Optional[dict],
                          personas: List[dict],
                          simulations: List[dict]) -> str:
    md = [f"# Аудитория и персоны: {product}", "",
          "_Материал исследования Марка для обсуждения в чате: 📍 — из "
          "полевого корпуса (цитаты реальных людей), 💡 — гипотеза. "
          "Симуляции — гипотезы, мерило — реальная кампания._", ""]
    if personas:
        md += ["## Персоны (собраны из реальных текстов аудитории)", ""]
        md += [_persona_md(p) + "\n" for p in personas]
    if simulations:
        md += ["## Панели реакций (последние)", ""]
        md += [_simulation_md(s) + "\n" for s in simulations[-5:]]
    if run and run.get("report_markdown"):
        md += ["## Полный отчёт исследования", "",
               run["report_markdown"]]
    return "\n".join(md)[:_CAP]


async def upsert_kb_document(user_id: str, *, title: str,
                             content: str) -> dict:
    """Создать/обновить документ в KB (таблица documents) — тот самый
    список, из которого чат берёт документы в контекст."""
    from backend.db.supabase_client import get_supabase_client
    sb = get_supabase_client()
    cleaned = content.replace("\x00", "")
    preview = cleaned[:500] + ("..." if len(cleaned) > 500 else "")
    existing = await sb._request(
        "GET", _TABLE,
        params={"user_id": f"eq.{user_id}", "title": f"eq.{title}",
                "select": "id", "limit": "1"})
    if existing:
        doc_id = existing[0]["id"]
        await sb._request(
            "PATCH", _TABLE,
            params={"id": f"eq.{doc_id}"},
            json_data={"content": cleaned, "content_preview": preview,
                       "file_size": len(cleaned.encode("utf-8"))},
            headers={"Prefer": "return=minimal"})
        return {"id": doc_id, "updated": True}
    rows = await sb._request(
        "POST", _TABLE,
        json_data={"title": title, "content": cleaned,
                   "doc_type": "research", "user_id": user_id,
                   "content_preview": preview,
                   "file_size": len(cleaned.encode("utf-8")),
                   "metadata": {"source": "mark_research"},
                   "status": "active", "sync_status": "not_synced"},
        headers={"Prefer": "return=representation"})
    return {"id": (rows[0]["id"] if rows else None), "updated": False}


async def export_audience_to_chat(user_id: str,
                                  run_id: Optional[str] = None) -> dict:
    """Персоны + панели + отчёт → KB-документ для контекста чата."""
    from backend.core.marketing.persona_engine import (
        list_personas, list_simulations)
    from backend.core.marketing.research_engine import get_run

    run = get_run(user_id, run_id) if run_id else None
    personas = list_personas(user_id)
    if run_id:
        scoped = [p for p in personas if p.get("run_id") == run_id]
        personas = scoped or personas
    if not personas and not run:
        return {"success": False,
                "error": "нет ни персон, ни исследования — сначала соберите"}
    product = ((run or {}).get("product")
               or (personas[0].get("product") if personas else "")
               or "аудитория")
    md = build_audience_doc_md(
        product=product, run=run, personas=personas,
        simulations=list_simulations(user_id))
    title = f"Аудитория и персоны: {product}"[:120]
    try:
        res = await upsert_kb_document(user_id, title=title, content=md)
    except Exception as e:
        logger.warning(f"audience doc upsert failed: {e}")
        return {"success": False,
                "error": f"не удалось сохранить документ в базу знаний: {e}"}
    return {"success": True, "document_id": res.get("id"), "title": title,
            "updated": res.get("updated", False),
            "hint": "Документ в базе знаний: в чате откройте «Контекст "
                    "чата» → Документы, отметьте его — и спрашивайте Brain "
                    "(«какой посыл им понравится?»)."}
