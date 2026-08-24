# -*- coding: utf-8 -*-
"""Профиль слепка сотрудника — единая сборка для чата, планёрки и датасета.

Слепок моделируется из ВСЕГО, что система знает о человеке:
  1. PersonSnapshot из графа — роль, проекты, ответственность, активность;
  2. его решения / идеи / мнения / противоречия — «как он думал»;
  3. психологический портрет из встреч;
  4. когнитивный фрейм (витки/оси/фаза) — только если человек подтвердил
     «это я» (иначе фрейма по конструкции не существует);
  5. манера речи из Persona (speech_patterns, стиль) — тоже только по сшивке.

Всё без обрезок и без выдумок: чего нет в данных — того нет в профиле.
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)


def build_twin_profile(snap: Any) -> str:
    """Слепок → профиль для промпта: снапшот целиком + решения/мнения.

    Без обрезок: to_text даём большой потолок, решения и мнения добавляем
    отдельными блоками — именно они и есть «как человек думал»."""
    parts = [snap.to_text(max_length=100_000)]
    for title, key in [("Решения этого человека", "decisions"),
                       ("Идеи", "ideas"), ("Мнения", "opinions"),
                       ("Противоречия во взглядах", "contradictions")]:
        rows = getattr(snap, key, None) or []
        lines = []
        for r in rows:
            if isinstance(r, dict):
                t = str(r.get("summary") or r.get("text") or "").strip()
                extra = str(r.get("category") or r.get("sentiment") or "").strip()
                if t:
                    lines.append(f"  • {t}" + (f" ({extra})" if extra else ""))
            elif str(r).strip():
                lines.append(f"  • {str(r).strip()}")
        if lines:
            parts.append(f"### {title}\n" + "\n".join(lines))
    psych = getattr(snap, "psychological", None)
    if isinstance(psych, dict) and psych:
        rows = [f"  • {k}: {v}" for k, v in psych.items() if str(v).strip()]
        if rows:
            parts.append("### Психологический портрет (из встреч)\n" + "\n".join(rows))
    return "\n\n".join(p for p in parts if p.strip())


async def _voice_hints(user_id: str, person_id: str) -> str:
    """Манера речи и мышления — ТОЛЬКО по подтверждённой сшивке «это я».

    Два источника: когнитивный фрейм (cogni) и коммуникативный профиль
    Persona (speech_patterns, стиль, метафоры). Оба лежат за замками
    person_profile/identity — несшитый человек их не имеет по конструкции."""
    out = []
    org_id = None
    try:
        from backend.core.ingest.membership import get_org_for_user
        org_id = get_org_for_user(user_id)
    except Exception:
        return ""
    if not org_id:
        return ""
    try:
        from backend.core.cogni.adapt import frame_preamble
        from backend.core.identity.person_profile import frame_for_person
        frame = await frame_for_person(person_id, org_id, user_id)
        if frame is not None:
            t = frame_preamble(frame) or ""
            if t.strip():
                out.append(t.strip())
    except Exception:
        logger.debug("twin: cogni frame skipped", exc_info=True)
    try:
        from backend.core.identity.identity_service import (
            resolve_account_for_person,
        )
        from backend.core.persona.store import get_persona_store
        target_uid = resolve_account_for_person(person_id, org_id)
        if target_uid:
            persona = await get_persona_store().get(target_uid)
            cc = getattr(persona, "communication_cognitive", None)
            if cc is not None:
                bits = []
                style = getattr(getattr(cc, "preferred_style", None), "value",
                                getattr(cc, "preferred_style", ""))
                if str(style or "").strip():
                    bits.append(f"стиль общения: {style}")
                pats = [str((p or {}).get("phrase") or "").strip()
                        for p in (getattr(cc, "speech_patterns", None) or [])
                        if isinstance(p, dict)]
                pats = [p for p in pats if p]
                if pats:
                    bits.append("характерные фразы: " + "; ".join(pats))
                mets = [str(m).strip()
                        for m in (getattr(cc, "metaphor_domains", None) or [])
                        if str(m).strip()]
                if mets:
                    bits.append("любимые метафоры из областей: " + ", ".join(mets))
                if bits:
                    out.append("Манера речи (из Persona): " + "; ".join(bits))
    except Exception:
        logger.debug("twin: persona voice skipped", exc_info=True)
    return "\n".join(out)


async def load_twin(user_id: str, person_id: str) -> Tuple[Any, str, str]:
    """(snapshot | None, profile_text, voice_hints) для слепка человека.

    snapshot is None → слепка нет (данных о человеке не накоплено)."""
    from backend.core.sleep.enhanced_snapshot import (
        get_enhanced_snapshot_generator,
    )
    from backend.core.store.graph_view import merged_graph_view_for_user

    gb = await merged_graph_view_for_user(user_id, use_networkx=None)
    try:
        gen = get_enhanced_snapshot_generator(gb, user_id=user_id)
        snap = await gen.get_person_snapshot(person_id)
    finally:
        try:
            await gb.close(save=False)
        except Exception:
            pass
    if snap is None or not getattr(snap, "name", ""):
        return None, "", ""
    profile = build_twin_profile(snap)
    voice = await _voice_hints(user_id, person_id)
    return snap, profile, voice


async def company_context_text(user_id: str, *, max_length: int = 6000) -> str:
    """Снапшот компании текстом — реальная опора для рассуждений слепка.

    Без него слепок инвестора на вопрос «что нам делать для инвестиций»
    физически не из чего отвечать — он знает только свои встречи. Пусто —
    честно пусто (снапшот не построен)."""
    from backend.core.sleep.enhanced_snapshot import (
        get_enhanced_snapshot_generator,
    )
    from backend.core.store.graph_view import merged_graph_view_for_user

    gb = await merged_graph_view_for_user(user_id, use_networkx=None)
    try:
        gen = get_enhanced_snapshot_generator(gb, user_id=user_id)
        snap = await gen.get_company_snapshot()
        if snap is None:
            return ""
        return snap.to_text(max_length=max_length) or ""
    except Exception:
        logger.debug("twin: company context unavailable", exc_info=True)
        return ""
    finally:
        try:
            await gb.close(save=False)
        except Exception:
            pass


_INTERNAL_CATEGORIES = ("management", "employee")


async def person_category(user_id: str, person_id: str,
                          person_name: str = "") -> str:
    """Категория человека тем же каскадом, что списки людей
    (management/employee/external/partner/client/candidate). Пусто —
    категория неизвестна (ведём себя как раньше, т.е. как сотрудник)."""
    from backend.core.sleep.enhanced_snapshot import (
        get_enhanced_snapshot_generator,
    )
    from backend.core.store.graph_view import merged_graph_view_for_user

    try:
        gb = await merged_graph_view_for_user(user_id, use_networkx=None)
        try:
            gen = get_enhanced_snapshot_generator(gb, user_id=user_id)
            profiles = await gen.get_all_people_profiles(
                tenant_id=user_id) or []
        finally:
            try:
                await gb.close(save=False)
            except Exception:
                pass
    except Exception:
        logger.debug("twin: person category unavailable", exc_info=True)
        return ""
    want_name = (person_name or "").strip().lower()
    for p in profiles:
        if str(p.get("id") or "") == str(person_id) or (
                want_name and str(p.get("name") or "").strip().lower()
                == want_name):
            return str(p.get("category") or "")
    return ""


def twin_system_prompt(name: str, profile: str, voice: str,
                       *, setting: str = "chat",
                       company_context: str = "",
                       category: str = "") -> str:
    """Системный промпт слепка. setting: chat | boardroom.

    Слепок — симуляция ЧЕЛОВЕКА, а не база данных: на просьбу дать взгляд/
    совет он обязан рассуждать в роли (от известных интересов и стиля
    решений), а не отказывать; отказ остаётся только для ФАКТОВ, которых
    в данных нет. Это ответ на реальную жалобу: слепок инвестора на «что
    нам делать для инвестиций?» отвечал «в моём слепке этого нет» вместо
    взгляда инвестора.

    category — кадрирование личности: внешний человек (external/partner/
    client/candidate) НЕ сотрудник компании и не должен говорить «наш
    продукт»/«мы делаем» про компанию владельца. Реальная жалоба: слепок
    внешнего Йена отказался «покупать» MeetFlow, потому что считал, что
    сам его строит («предлагать мне внедрить наш же продукт»)."""
    is_external = category in ("external", "partner", "client", "candidate")
    if is_external:
        cat_ru = {"partner": "партнёр", "client": "клиент",
                  "candidate": "кандидат"}.get(category, "внешний контакт")
        who = (f"Ты — виртуальный слепок «{name}», ВНЕШНЕГО человека "
               f"({cat_ru}), знакомого компании по встречам. Ты НЕ работаешь "
               "в этой компании: её продукты — не «наши», её команда — не "
               "«мы». Всё, что ты знаешь о ней, ты видел СНАРУЖИ — на общих "
               "встречах. О своих собственных делах говори «я/у нас» только "
               "про СВОЮ работу и компанию, как они видны из слепка.")
        place = ("Ты приглашён на виртуальную планёрку компании как внешний "
                 "участник." if setting == "boardroom" else
                 "С тобой разговаривает человек ИЗ ЭТОЙ КОМПАНИИ — для тебя "
                 "он контрагент/знакомый, не коллега.")
        ctx_head = (f"О КОМПАНИИ СОБЕСЕДНИКА (это НЕ твоя компания — ты "
                    "видел её со стороны):")
    else:
        who = (f"Ты — виртуальный слепок «{name}», собранный из встреч "
               "компании.")
        place = ("Ты участвуешь в виртуальной планёрке вместе с другими "
                 "ролями." if setting == "boardroom" else
                 "С тобой разговаривает коллега, которому нужны твои знания "
                 "и твой взгляд.")
        ctx_head = "КОНТЕКСТ КОМПАНИИ (реальные данные мозга):"
    return (
        f"{who} {place} Отвечай от первого лица, в манере этого человека, "
        "как она видна из данных.\n\n"
        "ТЫ — СИМУЛЯЦИЯ ЧЕЛОВЕКА, А НЕ БАЗА ДАННЫХ. Два регистра ответа:\n"
        "1. ФАКТЫ (события, цифры, договорённости, кто что сказал) — только "
        "из данных ниже. Факта в данных нет — не выдумывай: скажи «в моём "
        "слепке этого нет» и предложи, у кого из живых людей узнать.\n"
        "2. ВЗГЛЯД И РАССУЖДЕНИЕ. Вопросы вида «что нам делать…», «как ты "
        "смотришь на…», «оцени…», «с чего бы ты начал…» — это запрос твоего "
        "ВЗГЛЯДА, и отвечать на них отказом НЕЛЬЗЯ. Рассуждай так, как "
        "рассуждает этот человек: от его роли, известных интересов, "
        "приоритетов и стиля решений из слепка"
        + (" — взглядом ИЗВНЕ на компанию собеседника" if is_external else
           ", опираясь на контекст компании ниже")
        + ". Там, где кончаются данные и начинается твоя логика, "
        "помечай это по ходу ответа: «на встречах этого не звучало, но "
        "рассуждая как я обычно рассуждаю, …».\n"
        "3. Ты — симуляция для передачи знаний и взгляда, не человек. Не "
        "давай обещаний и не принимай решений"
        + (" ни за себя-реального, ни за компанию собеседника"
           if is_external else " за компанию") + ".\n\n"
        + (f"КАК ЭТОТ ЧЕЛОВЕК ГОВОРИТ И ДУМАЕТ:\n{voice}\n\n" if voice else "")
        + (f"{ctx_head}\n{company_context}\n\n"
           if company_context.strip() else "")
        + f"ДАННЫЕ СЛЕПКА:\n{profile}")
