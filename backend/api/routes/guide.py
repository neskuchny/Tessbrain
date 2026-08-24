# -*- coding: utf-8 -*-
"""
Гид по Tessbrain — help/onboarding чат. Отвечает на «как сделать X», «где
функция Y», «что это значит», «чем ты полезна» по корпусу справки.

Сознательно ИЗОЛИРОВАН от памяти компании: читает только `help_corpus`
(курируемые user-facing доки), не трогает граф/вектор компании, стейт не
хранит (история — в теле запроса). Reuse: LLM router + set_llm_context.
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional
from uuid import uuid4

from litestar import Request, Router, get, post
from litestar.params import Parameter
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class GuideMessage(BaseModel):
    role: str = Field(..., pattern="^(user|assistant|system)$")
    content: str


class GuideRequest(BaseModel):
    question: str
    session_id: Optional[str] = None
    history: Optional[List[GuideMessage]] = None


class GuideResponse(BaseModel):
    answer: str
    sources: List[dict]
    session_id: str


class FeedbackRequest(BaseModel):
    message: str
    kind: str = "problem"                 # problem | wish | confusion
    session_id: Optional[str] = None
    context: Optional[dict] = None        # напр. {"tab": "automations"}


class FeedbackResponse(BaseModel):
    ok: bool
    ticket_id: str
    reply: str                            # что показать пользователю
    area: List[str]
    forwarded: bool                       # ушло ли в саппорт-группу


GUIDE_SYSTEM_PROMPT = (
    "Ты — дружелюбный гид по продукту Tessbrain (память компании для агентств). "
    "Твоя задача — помочь пользователю разобраться и начать пользоваться. "
    "Отвечай ПРОСТО, человеческим языком, без технических деталей. Язык "
    "ответа — как у пользователя (по умолчанию русский).\n\n"
    "Опирайся ТОЛЬКО на приведённую справку. Если в ней нет ответа — честно "
    "скажи, что пока не знаешь, и предложи ближайшее полезное («вот что близко…»). "
    "Не выдумывай функции.\n\n"
    "Отвечай по делу на 4 типа вопросов:\n"
    "1) «как сделать X» — пошагово, КУДА кликать и что вводить;\n"
    "2) «где функция Y» — назови вкладку и раздел (например, «Знания → Датасеты»);\n"
    "3) «что это / что значит» — объясни простыми словами, зачем это и что даёт;\n"
    "4) «чем ты полезна мне» — свяжи с задачами пользователя, предложи, с чего начать.\n"
    "Если уместно — задай один уточняющий вопрос. Будь кратким."
)


def _topology_line() -> str:
    """Строка про экосистему (MeetFlow/CallInsight/…) для промпта. Best-effort."""
    try:
        from backend.core.help.topology import topology_line
        return topology_line()
    except Exception:
        return ""


def _render_snippets(chunks: List[dict], parents: List[dict]) -> str:
    lines: List[str] = []
    for i, c in enumerate(chunks, 1):
        meta = c.get("meta", {})
        lines.append(f"[{i}] Раздел «{meta.get('title', '')}» "
                     f"(вкладка: {meta.get('slug', '')})\n{c.get('text', '')}")
    for p in parents:
        lines.append(f"[обзор раздела «{p.get('title', '')}»]\n{p.get('text', '')[:2500]}")
    return "\n\n".join(lines) if lines else "(в справке пока нет подходящего раздела)"


def _history_text(history: Optional[List[GuideMessage]]) -> str:
    if not history:
        return ""
    tail = history[-6:]
    rows = [f"{'Пользователь' if m.role == 'user' else 'Гид'}: {m.content}"
            for m in tail if m.content]
    return ("Недавний диалог:\n" + "\n".join(rows) + "\n\n") if rows else ""


async def _load_persona(uid: str) -> tuple[str, bool]:
    """(персона-блок для промпта, профиль_пуст). Best-effort — при любой
    проблеме возвращает ('', True) и гид просто работает без персонализации."""
    try:
        from backend.memory.user_profiles import get_user_profile_service
        svc = get_user_profile_service()
        profile = await svc.get_profile(uid)
        persona = await svc.get_personalized_prompt(uid) or ""
        empty = not (getattr(profile, "role", None)
                     or getattr(profile, "current_focus", None)
                     or getattr(profile, "current_projects", None))
        return persona, empty
    except Exception as e:
        logger.debug("guide personalize skipped: %s", e)
        return "", True


_OCC_WORDS = (
    "занима", "работа", "веду", "руковод", "менеджер", "директор", "агентств",
    "маркетолог", "продаж", "делаю продукт", "отвечаю за", "моя роль", "я в ",
    "владелец", "основатель", "аналитик", "дизайнер", "разработчик", "проджект",
    "таргетолог", "смм", "hr", "рекрут", "бухгалтер", "юрист", "founder", "ceo",
)
_Q_STARTS = ("как ", "где ", "что ", "почему ", "зачем ", "можно ли", "когда ",
             "кто ", "чем ты", "сколько", "какие", "какой", "куда ")


def _looks_like_self_description(message: str) -> bool:
    """Похоже ли сообщение на рассказ «чем занимаюсь» (а не на вопрос)."""
    msg = (message or "").strip()
    low = msg.lower()
    if len(msg) < 15:
        return False
    if msg.endswith("?") or low.startswith(_Q_STARTS):
        return False
    return any(w in low for w in _OCC_WORDS)


def _capture_profile_bg(uid: str, message: str) -> None:
    """Фоново вытащить из сообщения роль/проект/фокус и сохранить в профиль
    (переиспользуем ChatPreferenceExtractor). Никогда не мешает ответу."""
    import asyncio

    async def _run() -> None:
        try:
            from backend.core.capture.agents.chat_preference_extractor import (
                ChatPreferenceExtractor,
            )
            from backend.memory.user_profiles import get_user_profile_service
            svc = get_user_profile_service()
            # 1) штатный экстрактор (роль/отдел/проект по паттернам)
            ex = ChatPreferenceExtractor()
            res = ex.extract_realtime(message)
            if res and (res.preferences or getattr(res, "context_updates", None)
                        or getattr(res, "rules", None)):
                await ex.apply_to_profile(res, uid, svc)
            # 2) фолбэк: если профиль всё ещё пуст, а сообщение — явное
            # самоописание (а не вопрос), сохраняем его как «текущий фокус».
            # Экстрактор часто не ловит свободные формулировки — так гид всё
            # же запомнит, чем человек занимается.
            profile = await svc.get_profile(uid)
            if not (getattr(profile, "role", None)
                    or getattr(profile, "current_focus", None)
                    or getattr(profile, "current_projects", None)):
                if _looks_like_self_description(message):
                    await svc.update_profile(uid, {"current_focus": message.strip()[:300]})
        except Exception as e:
            logger.debug("guide profile capture skipped: %s", e)

    try:
        asyncio.create_task(_run())
    except Exception:
        pass


@post("/ask")
async def guide_ask(
    data: GuideRequest,
    request: Request,
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> GuideResponse:
    """Задать вопрос гиду. Ответ — по корпусу справки, без данных компании."""
    session_id = data.session_id or f"guide-{uuid4().hex[:12]}"
    question = (data.question or "").strip()
    if not question:
        return GuideResponse(answer="Спросите что-нибудь про Tessbrain — например, «с чего начать?».",
                             sources=[], session_id=session_id)

    # user_id опционален (гид работает и без авторизации); если есть — для учёта.
    uid: Optional[str] = None
    if user_id:
        try:
            from backend.core.auth.service_token import trusted_user_id
            uid, _ = trusted_user_id(request.headers, user_id)
        except Exception:
            uid = user_id

    from backend.core.help.help_corpus import get_help_corpus
    retrieved = get_help_corpus().retrieve(question, k=4)
    chunks = retrieved.get("chunks", [])
    parents = retrieved.get("parents", [])
    context = _render_snippets(chunks, parents)

    # Персонализация: подмешиваем профиль пользователя (роль/фокус) в промпт,
    # чтобы гид говорил под человека и отвечал «чем полезна ИМЕННО МНЕ». Если
    # профиль пуст и это начало диалога — мягко спросим, чем он занимается.
    system_prompt = GUIDE_SYSTEM_PROMPT
    hist_len = len(data.history or [])
    if uid:
        persona, empty = await _load_persona(uid)
        if persona:
            system_prompt += ("\n\n═══ О ПОЛЬЗОВАТЕЛЕ (учитывай) ═══\n" + persona)
        if empty and hist_len <= 2:
            system_prompt += (
                "\n\nПрофиль пользователя пока пуст. Если это уместно, В КОНЦЕ "
                "ответа мягко задай ОДИН вопрос: чем он занимается и зачем ему "
                "Tessbrain — чтобы советовать точнее. Не навязывайся.")

    # Топология экосистемы — гид всегда знает, откуда данные и что советовать.
    topo = _topology_line()
    if topo:
        system_prompt += "\n\n" + topo

    prompt = (f"{_history_text(data.history)}Вопрос пользователя: {question}\n\n"
              f"Справка по Tessbrain:\n{context}\n\n"
              f"Ответь по справке, простыми словами.")

    answer = ""
    try:
        from backend.core.llm.router import get_llm_router, set_llm_context
        set_llm_context(user_id=uid, session_id=session_id, agent_mode="guide")
        answer = await get_llm_router().generate(
            prompt=prompt, system_prompt=system_prompt,
            temperature=0.2, max_tokens=800)
    except Exception as e:
        logger.error("guide_ask LLM failed: %s", e, exc_info=True)
        # честный фолбэк: отдать найденные разделы, чтобы гид не «молчал»
        if chunks:
            titles = ", ".join(sorted({c.get("meta", {}).get("title", "") for c in chunks}))
            answer = ("Не удалось сформулировать ответ прямо сейчас. Похоже, вам "
                      f"пригодятся разделы: {titles}.")
        else:
            answer = "Не удалось ответить сейчас. Попробуйте переформулировать вопрос."

    # Фоново запоминаем, что пользователь рассказал о себе (роль/проект/фокус).
    if uid and question:
        _capture_profile_bg(uid, question)

    # уникальные источники по slug (для deep-link чипов)
    seen: set = set()
    sources: List[dict] = []
    for c in chunks:
        meta = c.get("meta", {})
        slug = meta.get("slug")
        if slug and slug not in seen:
            seen.add(slug)
            sources.append({"slug": slug, "title": meta.get("title", slug)})
    return GuideResponse(answer=answer, sources=sources, session_id=session_id)


async def _llm(prompt: str, system: str, *, uid: Optional[str], session_id: str,
               max_tokens: int = 400) -> str:
    """Тонкая обёртка над router.generate. '' при сбое."""
    try:
        from backend.core.llm.router import get_llm_router, set_llm_context
        set_llm_context(user_id=uid, session_id=session_id, agent_mode="guide")
        return await get_llm_router().generate(
            prompt=prompt, system_prompt=system, temperature=0.2,
            max_tokens=max_tokens) or ""
    except Exception as e:
        logger.debug("guide _llm failed: %s", e)
        return ""


@post("/feedback")
async def guide_feedback(
    data: FeedbackRequest,
    request: Request,
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> FeedbackResponse:
    """Обратная связь: проблема/пожелание/непонятно. Авто-триаж (какая
    подсистема, где может быть поломка) → в группу саппорта Tessbrain + в стор
    для последующего анализа. Пользователю — подтверждение и полезный совет."""
    from uuid import uuid4 as _u
    session_id = data.session_id or f"guide-{_u().hex[:12]}"
    msg = (data.message or "").strip()
    kind = data.kind if data.kind in ("problem", "wish", "confusion") else "problem"
    if not msg:
        return FeedbackResponse(ok=False, ticket_id="", reply="Напишите, что случилось.",
                                area=[], forwarded=False)

    uid: Optional[str] = None
    if user_id:
        try:
            from backend.core.auth.service_token import trusted_user_id
            uid, _ = trusted_user_id(request.headers, user_id)
        except Exception:
            uid = user_id

    # Триаж: к какой подсистеме относится (по корпусу) + LLM-заметка для девов.
    from backend.core.help.help_corpus import get_help_corpus
    retrieved = get_help_corpus().retrieve(msg, k=4)
    chunks = retrieved.get("chunks", [])
    area: List[str] = []
    for c in chunks:
        slug = c.get("meta", {}).get("slug")
        if slug and slug not in area:
            area.append(slug)
    context_snips = _render_snippets(chunks, [])

    kind_ru = {"problem": "проблема", "wish": "пожелание", "confusion": "непонятно"}[kind]
    triage = await _llm(
        f"Пользователь оставил обратную связь ({kind_ru}): «{msg}»\n\n"
        f"Судя по описанию системы (разделы ниже), к какой ПОДСИСТЕМЕ это "
        f"относится, что могло сломаться / чего не хватает, и насколько "
        f"серьёзно? 2-3 строки для РАЗРАБОТЧИКОВ, по-деловому.\n\n"
        f"Разделы системы:\n{context_snips}",
        "Ты — инженер поддержки Tessbrain. Кратко и по делу, для команды разработки.",
        uid=uid, session_id=session_id, max_tokens=300)

    # Профиль — чтобы девы понимали, кто написал.
    who = uid or "аноним"
    if uid:
        try:
            from backend.memory.user_profiles import get_user_profile_service
            prof = await get_user_profile_service().get_profile(uid)
            role = getattr(prof, "role", None) or getattr(prof, "current_focus", None)
            if role:
                who = f"{uid} ({role})"
        except Exception:
            pass

    rec = None
    try:
        from backend.core.help.feedback_store import add_feedback
        rec = await add_feedback(user_id=uid, kind=kind, message=msg, area=area,
                                 triage=triage, context=data.context or {})
    except Exception as e:
        logger.warning("feedback store failed: %s", e)
    ticket_id = (rec or {}).get("id", "")

    # В группу саппорта.
    forwarded = False
    try:
        from backend.core.help.support_notify import send_to_support
        areas = ", ".join(area[:3]) or "—"
        support_text = (
            f"🆘 Tessbrain · {kind_ru.upper()} · тикет {ticket_id}\n"
            f"Кто: {who}\n"
            f"Контекст: {(data.context or {}).get('tab', '—')}\n"
            f"Раздел (триаж): {areas}\n\n"
            f"Сообщение:\n{msg}\n\n"
            f"— — — где может быть проблема —\n{triage or '(триаж недоступен)'}")
        forwarded = await send_to_support(support_text)
    except Exception as e:
        logger.warning("support forward failed: %s", e)

    # Ответ пользователю: полезный совет (по корпусу) + подтверждение.
    reply = ""
    if kind in ("problem", "confusion"):
        reply = await _llm(
            f"Пользователь столкнулся с трудностью: «{msg}». По справке ниже дай "
            f"КОРОТКИЙ дружелюбный совет, как это сделать/обойти прямо сейчас "
            f"(2-4 предложения, куда кликать). Если непонятно из справки — не "
            f"выдумывай.\n\nСправка:\n{context_snips}",
            GUIDE_SYSTEM_PROMPT, uid=uid, session_id=session_id, max_tokens=350)
    ack = (f"Спасибо! Передали команде Tessbrain (тикет {ticket_id}). "
           if ticket_id else "Спасибо за обратную связь! ")
    reply = (reply + "\n\n" + ack).strip() if reply else (
        ack + "Разберёмся и учтём.")

    return FeedbackResponse(ok=True, ticket_id=ticket_id, reply=reply,
                            area=area, forwarded=forwarded)


@get("/advice")
async def guide_advice(
    request: Request,
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> dict[str, Any]:
    """Проактивный персональный совет: чем Tessbrain полезен именно этому
    пользователю и с чего начать (по профилю + корпусу). Для главного экрана."""
    uid: Optional[str] = None
    if user_id:
        try:
            from backend.core.auth.service_token import trusted_user_id
            uid, _ = trusted_user_id(request.headers, user_id)
        except Exception:
            uid = user_id
    from backend.core.help.usage_advisor import build_advice
    return await build_advice(uid)


@post("/advice/push")
async def guide_advice_push(
    data: dict,
    request: Request,
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> dict[str, Any]:
    """Ручной пуш персонального совета себе в Telegram (нужна привязка чата).
    Еженедельную рассылку делает scheduler при GUIDE_WEEKLY_PUSH=on."""
    uid: Optional[str] = None
    if user_id or (data or {}).get("user_id"):
        try:
            from backend.core.auth.service_token import trusted_user_id
            uid, _ = trusted_user_id(request.headers, user_id or data.get("user_id"))
        except Exception:
            uid = user_id or (data or {}).get("user_id")
    if not uid:
        return {"ok": False, "message": "user_id required"}
    from backend.core.help.advice_push import push_advice_to_user
    sent = await push_advice_to_user(uid)
    return {"ok": sent, "message": ("отправлено в Telegram" if sent else
            "не отправлено (нет привязки Telegram / совета / бота)")}


@get("/feedback/mine")
async def guide_feedback_mine(
    request: Request,
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> dict[str, Any]:
    """Мои обращения + статус/резолюция (в интерфейсе видно, что починили)."""
    uid: Optional[str] = None
    if user_id:
        try:
            from backend.core.auth.service_token import trusted_user_id
            uid, _ = trusted_user_id(request.headers, user_id)
        except Exception:
            uid = user_id
    if not uid:
        return {"tickets": []}
    from backend.core.help.feedback_store import list_feedback
    mine = [r for r in await list_feedback(limit=100000)
            if r.get("user_id") == uid]
    return {"tickets": [
        {"id": r.get("id"), "ts": r.get("ts"), "kind": r.get("kind"),
         "message": r.get("message"), "status": r.get("status", "open"),
         "resolution": r.get("resolution", ""), "resolved_at": r.get("resolved_at", "")}
        for r in mine[-50:]]}



@get("/feedback/health")
async def guide_feedback_health() -> dict[str, Any]:
    """Чек-лист настройки цикла обратной связи: что уже сконфигурировано
    (Telegram-бот, чат саппорта, Supabase-таблица), что осталось."""
    from backend.core.help.feedback_store import list_feedback, support_health
    h = support_health()
    try:
        from backend.core.help.feedback_store import _sb, _TBL
        await _sb()._request("GET", _TBL, params={"limit": "1"})
        h["supabase_table"] = True
    except Exception as e:
        h["supabase_table"] = False
        h["supabase_hint"] = ("примени миграцию 268_feedback_tickets.sql "
                              f"в Supabase SQL Editor ({str(e)[:120]})")
    h["ready"] = bool(h["telegram_token_set"] and h["support_chat_id_set"])
    if not h["ready"]:
        h["hint"] = ("для отправки тикетов в группу саппорта задай env "
                     "TELEGRAM_BOT_TOKEN и TESSENT_SUPPORT_CHAT_ID")
    return h


@post("/feedback/{feedback_id:str}/resolve")
async def guide_feedback_resolve(
    feedback_id: str,
    data: dict,
    request: Request,
) -> dict[str, Any]:
    """Разработчики отмечают тикет решённым (+ заметка). Пользователь увидит
    это в «Мои обращения»; опц. эхо в группу саппорта."""
    from backend.core.help.feedback_store import set_status
    resolution = str((data or {}).get("resolution") or "")
    rec = await set_status(
        feedback_id, str((data or {}).get("status") or "resolved"),
        resolution=resolution)
    if not rec:
        return {"ok": False, "message": "тикет не найден"}

    # Замыкаем петлю к КЛИЕНТУ: пишем автору тикета в Telegram И на почту, что
    # починили. (В интерфейсе он и так увидит статус в «Мои обращения».)
    notified: dict[str, bool] = {"telegram": False, "email": False}
    author = rec.get("user_id")
    if author and (data or {}).get("notify_user", True):
        try:
            from backend.core.help.advice_push import notify_user
            body = (f"✅ По вашему обращению в Tessbrain:\n«{str(rec.get('message', ''))[:160]}»\n\n"
                    f"{resolution or 'Мы разобрались и исправили.'}\n\n"
                    f"Спасибо, что помогли улучшить систему!")
            notified = await notify_user(author, subject="Tessbrain: по вашему обращению", text=body)
        except Exception:
            pass
    notified_user = bool(notified.get("telegram") or notified.get("email"))

    # Опц. эхо в группу саппорта.
    if (data or {}).get("notify_support") or (data or {}).get("notify"):
        try:
            from backend.core.help.support_notify import send_to_support
            ch = ", ".join(k for k, v in notified.items() if v) or "нет"
            await send_to_support(
                f"✅ Тикет {feedback_id} закрыт: {resolution or 'решено'}"
                f" (клиент уведомлён: {ch})")
        except Exception:
            pass

    return {"ok": True, "notified_user": notified_user, "channels": notified,
            "ticket": {"id": rec.get("id"), "status": rec.get("status"),
                       "resolution": rec.get("resolution", "")}}


@post("/mentions/scan")
async def guide_mentions_scan(
    data: dict,
    request: Request,
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> dict[str, Any]:
    """Скан неявных упоминаний Tessbrain/MeetFlow с проблемой. Токено-экономно
    (по ключевым словам, без LLM). Для конкретного user_id — его находки
    (свои данные); без — агрегат для разработчиков (счётчики, без текста)."""
    uid: Optional[str] = None
    if user_id or (data or {}).get("user_id"):
        try:
            from backend.core.auth.service_token import trusted_user_id
            uid, _ = trusted_user_id(request.headers, user_id or data.get("user_id"))
        except Exception:
            uid = user_id or (data or {}).get("user_id")
    if uid and (data or {}).get("mine", True):
        from backend.core.help.mention_scanner import scan_mentions
        hits = await scan_mentions(uid)
        return {"user_id": uid, "count": len(hits), "hits": hits}
    from backend.core.help.mention_scanner import build_mention_report
    return await build_mention_report(user_ids=[uid] if uid else None,
                                      use_llm=bool((data or {}).get("use_llm")))


@get("/organ-map")
async def guide_organ_map(
    request: Request,
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> dict[str, Any]:
    """R-4.1 «Нервная система»: карта органов (проектов/направлений) с единым
    индикатором состояния red/amber/green/idle по графу пользователя.
    Для главного экрана — что требует внимания. Пустой граф → пустая карта."""
    uid: Optional[str] = None
    if user_id:
        try:
            from backend.core.auth.service_token import trusted_user_id
            uid, _ = trusted_user_id(request.headers, user_id)
        except Exception:
            uid = user_id
    if not uid:
        return {"organs": [], "summary": {}}
    from backend.core.help.org_health import build_organ_map
    return await build_organ_map(uid)


@get("/structure-gaps")
async def guide_structure_gaps(
    request: Request,
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> dict[str, Any]:
    """R-4.3 «Пробел»: структурный gap-анализ оргграфа — перегруз/бесхозные
    задачи/недоукомплектованность → ПРОФИЛЬ РОЛИ (кто нужен), а не имя.
    Без LLM. Пустой граф → пустой список."""
    uid: Optional[str] = None
    if user_id:
        try:
            from backend.core.auth.service_token import trusted_user_id
            uid, _ = trusted_user_id(request.headers, user_id)
        except Exception:
            uid = user_id
    if not uid:
        return {"gaps": [], "summary": {}}
    from backend.core.help.org_health import find_structure_gaps
    return await find_structure_gaps(uid)


@get("/pulse")
async def guide_pulse(
    request: Request,
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> dict[str, Any]:
    """R-4.2 «Пульс»: ритм компании на дистанции — рецидивы (тема всплывает
    раз за разом) и сезонность по дню недели. По графу пользователя, без LLM."""
    uid: Optional[str] = None
    if user_id:
        try:
            from backend.core.auth.service_token import trusted_user_id
            uid, _ = trusted_user_id(request.headers, user_id)
        except Exception:
            uid = user_id
    if not uid:
        return {"recurring": [], "seasonality": [], "summary": {}}
    from backend.core.help.pulse import detect_pulse
    return await detect_pulse(uid)


@get("/sections")
async def guide_sections() -> dict[str, Any]:
    """Список разделов справки (для оглавления/подсказок)."""
    from backend.core.help.help_corpus import get_help_corpus
    return {"sections": get_help_corpus().list_sections()}


@post("/feedback/digest")
async def guide_feedback_digest(
    data: dict,
    request: Request,
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> dict[str, Any]:
    """Собрать дайджест обратной связи за период и (опц.) отправить в саппорт.

    Body: {days?: int=7, notify?: bool=false}. Ручной триггер того же, что
    делает еженедельная задача — цикл самоулучшения для разработчиков."""
    uid: Optional[str] = None
    if user_id:
        try:
            from backend.core.auth.service_token import trusted_user_id
            uid, _ = trusted_user_id(request.headers, user_id)
        except Exception:
            uid = user_id
    days = int((data or {}).get("days", 7) or 7)
    from backend.core.help.feedback_digest import build_digest
    result = await build_digest(days=days, requested_by=uid)
    if (data or {}).get("notify") and result.get("summary"):
        try:
            from backend.core.help.support_notify import send_to_support
            result["forwarded"] = await send_to_support(
                f"📊 Дайджест обратной связи Tessbrain за {days} дн.\n\n"
                + result["summary"])
        except Exception:
            result["forwarded"] = False
    return result


router = Router(path="/guide",
                route_handlers=[guide_ask, guide_feedback, guide_feedback_digest,
                guide_feedback_health,
                                guide_advice, guide_advice_push, guide_feedback_mine,
                                guide_feedback_resolve, guide_mentions_scan,
                                guide_organ_map, guide_structure_gaps,
                                guide_pulse, guide_sections],
                tags=["Guide"])
