# -*- coding: utf-8 -*-
"""Разговор со слепком сотрудника — ВНУТРЕННИЙ (в пределах своего тенанта).

Сценарий: сменился директор по маркетингу → новый разговаривает со слепком
предыдущего («как ты принимал решения по X, что бы поменял»). Слепок собирает
backend/core/twin: PersonSnapshot (роль, проекты, решения, мнения) + манера
речи из Persona и когнитивный фрейм — если человек подтвердил «это я».

Границы честности и безопасности:
- слепок строится ТОЛЬКО из данных встреч этого тенанта; ФАКТЫ вне данных
  слепок обязан отклонять («в моём слепке этого нет»), но на просьбу дать
  взгляд/совет — рассуждает В РОЛИ (от известных интересов и стиля,
  с пометкой, где кончаются данные), а не отказывает;
- каждый ответ несёт дисклеймер «это симуляция, не человек»;
- доступ — только внутри тенанта владельца данных; передача слепка ДРУГОЙ
  компании идёт через отдельный контур согласий (backend/core/consent) и
  выключена флагом ENABLE_PROFILE_EXCHANGE.

/twin/training-export — датасет для ДООБУЧЕНИЯ ИИ-копии сотрудника (JSONL,
messages-формат): выгрузка реальных решений/мнений человека как обучающих
примеров. Само дообучение — вне системы.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from litestar import Request, Router, post
from litestar.exceptions import HTTPException
from pydantic import BaseModel

from backend.core.twin.profile import build_twin_profile  # noqa: F401 (реэкспорт)

logger = logging.getLogger(__name__)


def _resolve_user(request: Request, user_id: Optional[str]) -> str:
    try:
        from backend.core.auth.service_token import trusted_user_id
        uid, _src = trusted_user_id(request.headers, user_id or "")
    except PermissionError:
        raise HTTPException(status_code=403, detail="token/user_id mismatch")
    except Exception:
        uid = user_id or ""
    if not uid:
        raise HTTPException(status_code=401, detail="user_id required")
    return uid


class TwinAskRequest(BaseModel):
    person_id: str          # id Person-узла или имя (снапшоты умеют оба)
    question: str
    user_id: Optional[str] = None
    lang: Optional[str] = None   # язык интерфейса → язык ответа слепка


class TwinExportRequest(BaseModel):
    person_id: str
    user_id: Optional[str] = None


class TwinAccessLogRequest(BaseModel):
    person_id: Optional[str] = None   # чей слепок; пусто = свой собственный
    user_id: Optional[str] = None
    limit: int = 100


class TwinTeamRequest(BaseModel):
    user_id: Optional[str] = None
    include_indirect: bool = False   # вся ветка под руководителем, не только прямые


_MAX_GROUNDING_ITEMS = 10


def _grounding(snap: Any, voice: str) -> Dict[str, Any]:
    """Опора ответа: встречи и разделы профиля, из которых собран слепок.

    Слепок говорит от лица человека — значит должен показывать, откуда
    взялось основание. Данные уже лежат в снапшоте; здесь их только
    раскрываем наружу, ничего не досчитывая."""
    meetings = []
    for m in (getattr(snap, "recent_meetings", None) or [])[:_MAX_GROUNDING_ITEMS]:
        if not isinstance(m, dict):
            continue
        meetings.append({
            "meeting_id": m.get("meeting_id") or m.get("id") or "",
            "title": m.get("title") or "",
            "date": m.get("date") or "",
            "role": m.get("role_in_meeting") or "",
        })

    # Какие разделы профиля реально непустые — по ним слепок и рассуждает.
    sections = []
    for attr, label in (("decisions", "решения"), ("opinions", "мнения"),
                        ("ideas", "идеи"), ("current_projects", "проекты"),
                        ("skills", "компетенции"),
                        ("responsibilities", "зона ответственности"),
                        ("strengths", "сильные стороны")):
        n = len(getattr(snap, attr, None) or [])
        if n:
            sections.append({"section": label, "items": n})
    if voice:
        sections.append({"section": "манера речи (Persona)", "items": 1})

    return {
        "meetings": meetings,
        "meetings_total": int(getattr(snap, "meetings_participated", 0) or 0),
        "sections": sections,
        "note": ("Ответ построен на этих встречах и разделах профиля. "
                 "Всё, чего в них нет, слепок не знает."),
    }


@post("/ask")
async def twin_ask(data: TwinAskRequest, request: Request) -> Dict[str, Any]:
    uid = _resolve_user(request, data.user_id)
    question = (data.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question required")

    from backend.core.twin.profile import (
        company_context_text,
        load_twin,
        person_category,
        twin_system_prompt,
    )
    # Кто спрашивает чужой слепок — вопрос не праздный: слепок говорит от лица
    # человека, которого в комнате нет. Проверка строгая только при включённом
    # TWIN_ASK_REQUIRE_CONSENT; по умолчанию решение «разрешено», но само
    # обращение фиксируется в журнале владельца в любом режиме.
    from backend.core.twin import access_log
    from backend.core.twin.policy import check_twin_access
    decision = check_twin_access(person_id=data.person_id, asker_uid=uid)
    if not decision.allowed:
        access_log.record(person_id=data.person_id, asker_uid=uid,
                          tenant_uid=uid, question_chars=len(question),
                          granted=False, reason=decision.reason)
        raise HTTPException(status_code=403, detail=decision.message)

    snap, profile, voice = await load_twin(uid, data.person_id)
    if snap is None:
        return {"status": "no_data",
                "message": (f"Слепка для «{data.person_id}» нет: система не "
                            "накопила данных об этом человеке из встреч.")}

    # контекст компании обязателен: без него слепок физически не из чего
    # рассуждать о вопросах шире собственных встреч («что нам делать для
    # инвестиций?» у слепка инвестора)
    company = await company_context_text(uid)
    # внешний человек (инвестор/партнёр/клиент) не должен считать себя
    # сотрудником: кадрирование промпта зависит от категории
    category = await person_category(uid, data.person_id,
                                     getattr(snap, "name", ""))
    system_prompt = twin_system_prompt(snap.name, profile, voice,
                                       company_context=company,
                                       category=category)

    # промпт слепка написан по-русски: без явной инструкции англоязычный
    # пользователь получал английский интерфейс и русский ответ слепка
    from backend.core.llm.lang import lang_instruction
    from backend.core.llm.workload_policy import generate_for_workload
    answer = await generate_for_workload(
        uid, "chat", f"{system_prompt}{lang_instruction(data.lang)}"
                     f"\n\n---\n\nВОПРОС: {question}")
    if not (answer or "").strip():
        raise HTTPException(status_code=503,
                            detail="модель недоступна, попробуйте позже")

    access_log.record(person_id=data.person_id, asker_uid=uid, tenant_uid=uid,
                      person_name=getattr(snap, "name", ""),
                      question_chars=len(question), granted=True,
                      reason=decision.reason)

    return {
        "status": "success",
        "person": snap.name,
        "answer": answer.strip(),
        "disclaimer": ("Это симуляция на основе данных встреч, а не мнение "
                       "реального человека. Важное подтверждайте у людей."),
        # На чём слепок стоит: без этого ответ невозможно перепроверить, а
        # «похоже на правду» — плохой критерий, когда речь от лица человека.
        "grounded_on": _grounding(snap, voice),
        "profile_stats": {
            "version": getattr(snap, "version", None),
            "meetings_participated": getattr(snap, "meetings_participated", 0),
            "has_voice_hints": bool(voice),
            "profile_chars": len(profile),
        },
    }


@post("/training-export")
async def twin_training_export(data: TwinExportRequest,
                               request: Request) -> Dict[str, Any]:
    """Датасет дообучения ИИ-копии сотрудника (JSONL в ответе)."""
    uid = _resolve_user(request, data.user_id)
    from backend.core.twin.training_export import build_training_dataset
    return await build_training_dataset(uid, data.person_id)


@post("/access-log")
async def twin_access_log(data: TwinAccessLogRequest,
                          request: Request) -> Dict[str, Any]:
    """«Кто обращался к моему слепку» — журнал для самого человека.

    Свой журнал видит владелец слепка (аккаунт, сшитый с Person-узлом);
    чужой — только администратор организации. Иначе журнал обращений сам
    стал бы способом следить за коллегами."""
    uid = _resolve_user(request, data.user_id)

    from backend.core.identity.identity_service import resolve_person_for_account
    from backend.core.twin import access_log

    my_person = resolve_person_for_account(uid)
    target = (data.person_id or "").strip() or (my_person or "")
    if not target:
        return {"status": "not_linked",
                "message": ("Ваш аккаунт не сшит с человеком в графе — "
                            "подтвердите «это я», чтобы видеть обращения "
                            "к своему слепку."),
                "entries": [], "summary": {"total": 0}}

    is_self = bool(my_person) and access_log.normalize_person_key(target) == \
        access_log.normalize_person_key(my_person)
    viewer_role = "self"
    if not is_self:
        # Чужой журнал видят те, кто отвечает за человека по работе:
        # руководитель по цепочке подчинения и администратор организации.
        # Всем прочим он закрыт — иначе журнал обращений сам стал бы
        # инструментом слежки за коллегами.
        viewer_role = ""
        try:
            from backend.core.ingest import membership
            from backend.core.twin.policy import is_manager_of

            org_id = membership.get_org_for_user(uid)
            owner_uid = (membership.find_user_by_person_entity(target, org_id)
                         if org_id else None)
            if owner_uid and org_id and is_manager_of(uid, owner_uid, org_id):
                viewer_role = "manager"
            elif org_id and membership.can_manage_org(uid, org_id):
                viewer_role = "admin"
        except Exception:
            logger.debug("twin access-log: проверка прав не удалась",
                         exc_info=True)
        if not viewer_role:
            raise HTTPException(
                status_code=403,
                detail=("журнал обращений к слепку доступен самому человеку, "
                        "его руководителю и администратору организации"))

    # Слепок зовут и по id узла, и по имени — журналы обоих написаний сливаем.
    aliases = []
    try:
        from backend.core.twin.profile import load_twin
        snap, _p, _v = await load_twin(uid, target)
        nm = getattr(snap, "name", "") if snap else ""
        if nm:
            aliases.append(nm)
        pid = getattr(snap, "person_id", "") if snap else ""
        if pid:
            aliases.append(pid)
    except Exception:
        logger.debug("twin access-log: имя слепка не резолвится", exc_info=True)

    return {
        "status": "success",
        "person_id": target,
        "is_self": is_self,
        "viewer_role": viewer_role,   # self / manager / admin
        "entries": access_log.list_for_person(target, limit=data.limit,
                                              aliases=aliases),
        "summary": access_log.summary_for_person(target, aliases=aliases),
        "note": ("Текст вопросов не сохраняется — только факт обращения: "
                 "иначе журнал раскрывал бы приватное самого спрашивающего."),
    }


@post("/my-team")
async def twin_my_team(data: TwinTeamRequest, request: Request) -> Dict[str, Any]:
    """Слепки моих подчинённых — рабочий список для руководителя.

    Показывает прямых подчинённых (и, при need_deep, всю ветку под
    руководителем) вместе со сводкой обращений к их слепкам: кого спрашивают,
    как часто. Это управленческая картина, а не слежка — текст вопросов
    здесь тоже недоступен, только факты обращений."""
    uid = _resolve_user(request, data.user_id)

    from backend.core.ingest import membership
    from backend.core.twin import access_log
    from backend.core.twin.policy import manager_chain

    org_id = membership.get_org_for_user(uid)
    if not org_id:
        return {"status": "no_org", "team": [],
                "message": "Аккаунт не состоит в организации."}

    members = membership.list_members(org_id)
    team = []
    for m in members:
        member_uid = str(m.get("user_id") or "")
        if not member_uid or member_uid == uid:
            continue
        direct = str(m.get("reports_to") or "").strip() == uid
        if direct:
            depth = 1
        elif data.include_indirect:
            chain = manager_chain(member_uid, org_id)
            if uid not in chain:
                continue
            depth = chain.index(uid) + 1
        else:
            continue

        person_id = str(m.get("person_entity_id") or "")
        row = {
            "user_id": member_uid,
            "person_id": person_id or None,
            "role": m.get("role"),
            "department": m.get("department"),
            "depth": depth,          # 1 = прямой подчинённый
            "has_twin": bool(person_id),
        }
        # Сводка обращений есть только у сшитых людей: без person_entity_id
        # слепок не с чем сопоставить, и врать «0 обращений» нельзя.
        row["access_summary"] = (access_log.summary_for_person(person_id)
                                 if person_id else None)
        team.append(row)

    team.sort(key=lambda r: (r["depth"], str(r.get("department") or ""),
                             r["user_id"]))
    unlinked = [r["user_id"] for r in team if not r["has_twin"]]
    return {
        "status": "success",
        "team": team,
        "unlinked_count": len(unlinked),
        "note": ("Слепок есть только у сотрудников, подтвердивших сшивку "
                 "«это я». По остальным данных о слепке нет — это пропуск "
                 "в данных, а не отсутствие обращений."
                 if unlinked else
                 "Все подчинённые сшиты со своими Person-узлами."),
    }


class TwinSelfExportRequest(BaseModel):
    user_id: Optional[str] = None
    include_report: bool = True
    include_portrait: bool = True


@post("/my-export")
async def twin_my_export(data: TwinSelfExportRequest,
                         request: Request) -> Dict[str, Any]:
    """Выгрузка СВОЕГО слепка — сотрудник забирает свои данные себе.

    Единственный субъект этого эндпоинта — сам человек: person_id берётся
    только из ПОДТВЕРЖДЁННОЙ сшивки «это я» текущего аккаунта. Чужой слепок
    здесь выгрузить нельзя в принципе — параметра person_id нет.

    Внутри: проекты, достижения и сложности, сильные стороны и зоны роста,
    психологический профиль, отчёт по сотруднику, RPG-портрет. Денежные
    суммы затёрты, финансовые KPI не включаются — это данные компании,
    а не человека (см. backend/core/twin/self_export.py).
    """
    uid = _resolve_user(request, data.user_id)
    try:
        from backend.core.identity.identity_service import (
            resolve_person_for_account,
        )
        person_id = resolve_person_for_account(uid)
    except Exception:
        person_id = None
    if not person_id:
        raise HTTPException(
            status_code=404,
            detail="Аккаунт не сшит с Person-сущностью. Подтвердите «это я» "
                   "в профиле — без сшивки система не знает, чьи данные "
                   "выгружать, и выгружать чужие наугад не станет.",
        )
    from backend.core.twin.self_export import build_self_export
    export = await build_self_export(
        user_id=uid,
        person_id=person_id,
        include_report=data.include_report,
        include_portrait=data.include_portrait,
    )
    # Запись в журнал обращений: выгрузка — тоже обращение к слепку,
    # пусть и своё собственное. Владелец увидит его в своём же журнале.
    try:
        from backend.core.twin.access_log import record
        record(person_id=person_id, asker_uid=uid, tenant_uid=uid,
               person_name=str(export.get("name") or ""),
               question_chars=0, granted=True, reason="self_export")
    except Exception:
        logger.debug("self export access log skipped", exc_info=True)
    return {"success": True, "export": export}


router = Router(path="/twin",
                route_handlers=[twin_ask, twin_training_export,
                                twin_access_log, twin_my_team,
                                twin_my_export],
                tags=["Person Twin"])
