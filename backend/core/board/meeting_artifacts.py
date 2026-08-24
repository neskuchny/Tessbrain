# -*- coding: utf-8 -*-
"""Артефакты встречи из базы знаний — единый источник для доставки и досок.

«Тянуть из встречи» разные вещи: саммари, задачи, решения, участников,
транскрипт, повестку, готовый отчёт. Один сборщик используют:
  • кнопки в карточке встречи (routes/meetflow.py);
  • узел доски «Данные встречи» (board/process_engine.py).

Реально сохранённое берём как есть (саммари/задачи/решения/участники — узел
встречи в графе; транскрипт — строка встречи в Supabase). Повестку граф не
хранит — выводим из транскрипта LLM'ом (честная деривация, не заглушка); нет
транскрипта/ключа → пусто. Never-raise, never-mock.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Типы артефактов, которые можно вытянуть из встречи.
KINDS = ("summary", "tasks", "decisions", "participants", "transcript",
         "agenda", "report")


def node_text(node: Dict[str, Any], *keys: str) -> str:
    """Первое непустое строковое поле узла графа из перечисленных ключей."""
    for k in keys:
        v = (node or {}).get(k)
        if v and str(v).strip():
            return str(v).strip()
    return ""


async def gather_context(user_id: str, meeting_uuid: str,
                         external_id: str = "", title: str = "") -> Dict[str, Any]:
    """Артефакты встречи из графа знаний (саммари/задачи/решения/участники).

    Узел встречи создаётся как meeting_{id} (id = UUID строки встречи). Пробуем
    UUID, затем внешний meeting_id, затем по названию — покрывает разные пути
    синхронизации. Строит per-user граф и закрывает его. Never-raise."""
    from backend.core.store.graph_builder import GraphBuilder
    from backend.core.store.tenant_paths import graph_path_for_user
    from backend.core.think.graph_traverser import GraphTraverser

    gb = GraphBuilder(use_networkx=None, graph_storage_path=graph_path_for_user(user_id))
    try:
        await gb.connect()
        if not gb.connected:
            return {}
        tr = GraphTraverser(gb, None)
        candidates = [f"meeting_{meeting_uuid}"]
        if external_id:
            candidates.append(f"meeting_{external_id}")
        for node_id in candidates:
            ctx = await tr.find_meeting_with_context(meeting_id=node_id)
            if ctx.get("meeting"):
                return ctx
        if title:
            ctx = await tr.find_meeting_with_context(title=title)
            if ctx.get("meeting"):
                return ctx
        return {}
    except Exception as e:
        logger.warning("gather meeting context failed: %s", e)
        return {}
    finally:
        try:
            await gb.close(save=False)
        except Exception:
            logger.debug("graph close skipped", exc_info=True)


async def _load_meeting_row(user_id: str, meeting_id: str,
                            include_transcript: bool = False) -> Dict[str, Any]:
    """Строка встречи из Supabase (заголовок/дата/внешний id/транскрипт).

    Fallback для орг-встреч: scoped-поиск по своему user_id не видит встречу
    коллеги по организации. Добираем ОДНИМ точным запросом по id (никаких
    подстрочных поисков — утечки по ilike исключены) и пускаем строку только
    если её владелец входит в allowed_tenants (свои орги и их участники)."""
    try:
        from backend.db.supabase_client import get_supabase_client
        sb = get_supabase_client()
        row = await sb.get_meeting_details(
            meeting_id, include_transcript=include_transcript, user_id=user_id)
        if row:
            return row
        try:
            from backend.core.store.tenant_scope import allowed_tenants
            allowed = allowed_tenants(user_id)
        except Exception:
            allowed = {user_id} if user_id else set()
        if len(allowed) > 1:  # есть организация — есть смысл добирать
            rows = await sb._request(
                "GET", "/rest/v1/meetings",
                params={"id": f"eq.{meeting_id}", "select": "*", "limit": "1"})
            cand = (rows or [{}])[0] if rows else {}
            if cand and str(cand.get("user_id") or "") in allowed:
                logger.info("meeting row: найдена орг-встреча коллеги "
                            "(owner=%s)", cand.get("user_id"))
                return cand
        return {}
    except Exception as e:
        logger.warning("meeting row fetch failed: %s", e)
        return {}


def _tasks_lines(ctx: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for tsk in (ctx or {}).get("tasks", []):
        desc = node_text(tsk, "description", "title", "text")
        if not desc:
            continue
        tail = " · ".join(x for x in (node_text(tsk, "assignee"),
                                      node_text(tsk, "deadline")) if x)
        out.append(f"• {desc}" + (f" ({tail})" if tail else ""))
    return out


def _decisions_lines(ctx: Dict[str, Any]) -> List[str]:
    out = []
    for d in (ctx or {}).get("decisions", []):
        t = node_text(d, "text", "description", "title")
        if t:
            out.append(f"• {t}")
    return out


def _participants_line(ctx: Dict[str, Any]) -> str:
    parts = [node_text(p, "name", "title") for p in (ctx or {}).get("participants", [])]
    parts = [p for p in parts if p]
    return ", ".join(dict.fromkeys(parts))


def format_report(details: Dict[str, Any], ctx: Dict[str, Any],
                  sections: Optional[List[str]] = None) -> str:
    """Готовый пакет «отчёт по встрече»: заголовок, саммари, участники, решения,
    задачи. Только реально сохранённое; ничего не выдумываем."""
    want = set(sections or []) or {"summary", "decisions", "tasks", "participants"}
    title = str((details or {}).get("title") or "Встреча")
    date = str((details or {}).get("date") or (details or {}).get("created_at") or "")
    lines: List[str] = [f"📋 {title}"]
    if date:
        lines.append(f"🗓 {date[:10]}")

    # Саммари: граф-узел встречи ИЛИ (фолбэк) поле summary из строки Supabase.
    # Мелкая свежая встреча часто не успевает получить summary на граф-узле,
    # а в строке MeetFlow оно уже есть — без фолбэка отчёт выходил пустой
    # болванкой «[уточнить]» при реально существующем саммари.
    summary = (node_text((ctx or {}).get("meeting") or {}, "summary")
               or str((details or {}).get("summary") or "").strip())
    if "summary" in want and summary:
        lines.append("\n📝 Саммари")
        lines.append(summary)
    if "participants" in want:
        p = _participants_line(ctx)
        if p:
            lines.append("\n👥 Участники")
            lines.append(p)
    if "decisions" in want:
        decs = _decisions_lines(ctx)
        if decs:
            lines.append("\n✅ Решения")
            lines.extend(decs)
    if "tasks" in want:
        tsk = _tasks_lines(ctx)
        if tsk:
            lines.append("\n📌 Задачи")
            lines.extend(tsk)
    return "\n".join(lines).strip()


async def _derive_agenda(user_id: str, transcript: str, summary: str,
                         lang_ru: bool = True) -> str:
    """Повестку граф не хранит — выводим темы из транскрипта/саммари LLM'ом.
    Честная деривация (не заглушка). Нет материала/ключа → пусто."""
    material = (transcript or "").strip() or (summary or "").strip()
    if len(material) < 40:
        return ""
    try:
        from backend.core.llm.workload_policy import generate_for_workload
        instr = ("Составь краткую повестку встречи — список из 3–8 обсуждавшихся "
                 "тем, по одной в строке, с «• ». Только реально затронутые темы, "
                 "без воды и выдумок." if lang_ru else
                 "Build a short meeting agenda — 3–8 bullet topics that were "
                 "actually discussed, one per line with '• '. No fluff, no invention.")
        prompt = f"{instr}\n\n===\n{material[:8000]}"
        res = await generate_for_workload(user_id or "", "chat", prompt)
        return str(res or "").strip()
    except Exception as e:
        logger.info("agenda derive skipped: %s", e)
        return ""


async def fetch_artifact(user_id: str, meeting_id: str, kind: str,
                         lang_ru: bool = True) -> Dict[str, Any]:
    """Вытянуть ОДИН артефакт встречи. Возврат:
    {kind, title, text, empty: bool, error?: str}. Never-raise, never-mock."""
    kind = (kind or "report").strip().lower()
    if kind not in KINDS:
        kind = "report"
    uid = (user_id or "").strip()
    mid = (meeting_id or "").strip()
    if not (uid and mid):
        return {"kind": kind, "title": "", "text": "", "empty": True,
                "error": "user_id и meeting_id обязательны"}

    need_transcript = kind in ("transcript", "agenda")
    row = await _load_meeting_row(uid, mid, include_transcript=need_transcript)
    if not row:
        return {"kind": kind, "title": "", "text": "", "empty": True,
                "error": "Встреча не найдена"}
    title = str(row.get("title") or "")
    external_id = str(row.get("meeting_id") or "")

    # Транскрипт — прямо из строки встречи, граф не нужен.
    if kind == "transcript":
        tx = str(row.get("transcription_text") or row.get("transcription")
                 or row.get("transcript") or "").strip()
        return {"kind": kind, "title": title, "text": tx, "empty": not tx}

    ctx = await gather_context(uid, mid, external_id, title)

    if kind == "summary":
        # Граф-узел → фолбэк на строку встречи Supabase (см. format_report).
        txt = (node_text((ctx or {}).get("meeting") or {}, "summary")
               or str(row.get("summary") or "").strip())
        return {"kind": kind, "title": title, "text": txt, "empty": not txt}
    if kind == "tasks":
        lines = _tasks_lines(ctx)
        return {"kind": kind, "title": title, "text": "\n".join(lines),
                "empty": not lines}
    if kind == "decisions":
        lines = _decisions_lines(ctx)
        return {"kind": kind, "title": title, "text": "\n".join(lines),
                "empty": not lines}
    if kind == "participants":
        p = _participants_line(ctx)
        return {"kind": kind, "title": title, "text": p, "empty": not p}
    if kind == "agenda":
        tx = str(row.get("transcription_text") or "").strip()
        summary = node_text((ctx or {}).get("meeting") or {}, "summary")
        agenda = await _derive_agenda(uid, tx, summary, lang_ru=lang_ru)
        return {"kind": kind, "title": title, "text": agenda, "empty": not agenda}

    # report — полный пакет.
    txt = format_report(row, ctx)
    body = [ln for ln in txt.split("\n") if ln.strip()]
    return {"kind": "report", "title": title, "text": txt, "empty": len(body) <= 2}


__all__ = ["KINDS", "fetch_artifact", "format_report", "gather_context", "node_text"]
