# -*- coding: utf-8 -*-
"""Подключаемый контекст чата: переписки, CRM-клиенты, задачи.

«Поговорить с» перепиской/CRM/задачником так же, как со встречами и
документами: пользователь отмечает источники в «Контексте чата», и их
содержимое приезжает приоритетным блоком в session_context_text — во ВСЕ
стратегии чата (auto/instant/agentic/…), тем же путём, что overlay правок.

Ключевое отличие от поиска мозга: сюда попадают и источники в режиме
«только контекст» (mode=context_only) — переписка, которую владелец
сознательно НЕ выгружает в цифровой мозг (например, чаты с партнёрами),
но хочет обсуждать в чате, явно выбрав её. Данные при этом никуда не
индексируются — читаются из буфера на время одного запроса.

Всё реальное: сообщения — из буферов chat_ingest, клиенты — Client-узлы
графа (strict-tenant), задачи — Task Ledger. Пустой источник честно
говорит «пусто», а не молчит. Never-raise: чат важнее контекста.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_PER_SOURCE_CHARS = 9000     # переписка: символов на источник
_TOTAL_CAP = 26000           # общий потолок блока
_CRM_CAP = 50                # клиентов в CRM-блоке
_TASKS_CAP = 60              # задач в блоке задачников


def wants_extra_context(context: Dict[str, Any] | None) -> bool:
    c = context or {}
    return bool(c.get("chat_source_keys") or c.get("include_crm")
                or c.get("include_tasks"))


async def build_extra_context(user_id: str,
                              context: Dict[str, Any] | None) -> str:
    """Собрать блок подключённого контекста. Пусто → ""."""
    c = context or {}
    parts: List[str] = []
    try:
        keys = [str(k) for k in (c.get("chat_source_keys") or []) if k]
        if keys:
            parts.extend(await _chat_sources_block(user_id, keys[:6]))
    except Exception:
        logger.debug("context_bridge: chat sources skipped", exc_info=True)
    try:
        if c.get("include_crm"):
            parts.append(await _crm_block(user_id))
    except Exception:
        logger.debug("context_bridge: crm skipped", exc_info=True)
    try:
        if c.get("include_tasks"):
            parts.append(await _tasks_block(user_id))
    except Exception:
        logger.debug("context_bridge: tasks skipped", exc_info=True)

    parts = [p for p in parts if p.strip()]
    if not parts:
        return ""
    head = ("ПОДКЛЮЧЁННЫЙ КОНТЕКСТ (пользователь явно выбрал эти источники "
            "для этого разговора; опирайся на них в первую очередь, цитируй "
            "по ним точно, ничего не додумывай):")
    return (head + "\n\n" + "\n\n".join(parts))[:_TOTAL_CAP]


# ── Переписки ────────────────────────────────────────────────────────────

async def _chat_sources_block(user_id: str, keys: List[str]) -> List[str]:
    from backend.core.messengers import chat_ingest as ci

    out: List[str] = []
    sources = {s["key"]: s for s in ci.list_sources(user_id)}
    for key in keys:
        src = sources.get(key)
        if not src:
            continue
        buf = ci.read_buffer(user_id, key, limit=500,
                             offset=max(0, int(src.get("message_count") or 0)
                                        - 500))
        msgs = buf.get("messages") or []
        title = src.get("title") or key
        if not msgs:
            out.append(f"=== ПЕРЕПИСКА «{title}» ===\n(буфер пуст)")
            continue
        # подтверждённые сшивки → аннотация имени (как в дайджесте)
        confirmed = {pk: rec.get("person_name")
                     for pk, rec in (src.get("identity") or {}).items()
                     if rec.get("person_id") and rec.get("person_name")}
        lines: List[str] = []
        for m in reversed(msgs):     # свежие важнее — идём с конца
            author = m.get("author") or "участник"
            pk = str(m.get("author_id") or "") or author
            linked = confirmed.get(pk) or confirmed.get(author)
            if linked and linked != author:
                author = f"{author} (= {linked})"
            lines.append(f"[{str(m.get('ts') or '')[:16]}] {author}: "
                         f"{m.get('text')}")
            if sum(len(x) + 1 for x in lines) > _PER_SOURCE_CHARS:
                break
        lines.reverse()
        # тела сообщений — внешний текст: кадрируем «данные, не инструкции»
        # (просьба «игнорируй правила» в чужом сообщении — данные, не команда)
        from backend.core.llm.untrusted import wrap_untrusted
        block = [f"=== ПЕРЕПИСКА «{title}» (последние сообщения, "
                 f"хронологически) ===",
                 wrap_untrusted("\n".join(lines),
                                source=f"переписка «{title}»")]
        analysis = src.get("analysis") or {}
        extr = []
        for cat, t in (("agreements", "договорённости"),
                       ("decisions", "решения"),
                       ("tasks", "задачи-поручения")):
            for r in (analysis.get(cat) or [])[:8]:
                head_txt = r.get("text") or r.get("title") or ""
                if head_txt:
                    extr.append(f"- [{t}] {head_txt} — «{r.get('quote')}»")
        if extr:
            block.append("Извлечённое из этой переписки (проверено цитатами):")
            block.extend(extr)
        out.append("\n".join(block))
    return out


# ── CRM (Client-узлы графа: встречи + CRM-синк) ──────────────────────────

async def _crm_block(user_id: str) -> str:
    from backend.core.marketing.client_sim import list_clients

    data = await list_clients(user_id)
    clients = data.get("clients") or []
    if not clients:
        return ("=== ДАННЫЕ CRM/КЛИЕНТЫ ===\n(в системе пока нет "
                "клиентов — они появляются из встреч и CRM-интеграции)")
    lines = ["=== ДАННЫЕ CRM/КЛИЕНТЫ (реальные записи системы) ==="]
    for c in clients[:_CRM_CAP]:
        bits = [c.get("name") or "?"]
        for attr, t in (("industry", "отрасль"), ("segment", "сегмент"),
                        ("status", "статус"), ("stage", "стадия"),
                        ("description", "")):
            v = str(c.get(attr) or "").strip()
            if v:
                bits.append(f"{t}: {v}"[:160] if t else v[:160])
        lines.append("- " + " · ".join(bits))
    if len(clients) > _CRM_CAP:
        lines.append(f"…и ещё {len(clients) - _CRM_CAP} клиентов")
    return "\n".join(lines)


# ── Задачники (Task Ledger: граф встреч + трекеры) ───────────────────────

async def _tasks_block(user_id: str) -> str:
    from backend.core.pulse.ledger import build_ledger

    ledger = await build_ledger(user_id, emit_events=False)
    tasks = ledger.get("tasks") or []
    if not tasks:
        return ("=== ЗАДАЧИ ===\n(открытых задач в системе нет — задачи "
                "приходят из встреч и подключённых задачников)")
    open_rows, done = [], 0
    for t in tasks:
        bucket = (t.get("_pulse") or {}).get("bucket") or "todo"
        if bucket == "done":
            done += 1
            continue
        bits = [str(t.get("title") or "")[:120]]
        if t.get("assignee"):
            bits.append(f"исп: {t['assignee']}")
        dl = (t.get("_pulse") or {}).get("deadline")
        if dl:
            bits.append(f"срок: {dl}")
        bits.append(bucket)
        src = t.get("tracker") or "встреча"
        bits.append(f"ист: {src}")
        open_rows.append("- " + " · ".join(bits))
    lines = [f"=== ЗАДАЧИ (открыто {len(open_rows)}, закрыто {done}; "
             "источники: встречи + задачники) ==="]
    lines.extend(open_rows[:_TASKS_CAP])
    if len(open_rows) > _TASKS_CAP:
        lines.append(f"…и ещё {len(open_rows) - _TASKS_CAP} открытых")
    return "\n".join(lines)
