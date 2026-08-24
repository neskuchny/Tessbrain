"""REST endpoints для Persona service (Phase A.1).

Контракт стабилен — Mini Tess может строить против него прямо сейчас.
Body schemas в MINI_TESS_INTEGRATION.md.

Эндпоинты:
- GET  /persona/me                — agregated Persona текущего user'а
- POST /persona/me/clients        — push client archetype from Mini Tess
- DELETE /persona/me/clients/{id} — удалить client archetype

Skeleton: пока PersonaAggregator не реализован (следующий коммит фазы A),
GET возвращает minimal Persona если её ещё нет (status_code=200,
source_meetings=0, confidence=0). Mini Tess может показать «профиль
строится, нужно ещё встреч».
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from litestar import Router, delete, get, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter

from backend.api.middleware.auth_middleware import get_user_id_from_token
from backend.core.persona import (
    ClientArchetype,
    ClientInteraction,
    Persona,
    PersonaProvenanceEntry,
    ProvenanceSource,
)
from backend.core.persona.store import get_persona_store

logger = logging.getLogger(__name__)


def _extract_user(authorization: Optional[str]) -> str:
    """Извлечь user_id из JWT. Бросает 401 если токен невалидный."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")
    try:
        user_id = get_user_id_from_token(authorization)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}") from exc
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing sub/user_id")
    return user_id


@get("/me", status_code=200)
async def get_my_persona(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    rebuild: bool = Parameter(query="rebuild", default=False),
) -> dict[str, Any]:
    """Получить agregated Persona текущего пользователя.

    Если Persona ещё не построена (например, у founder'а 0 встреч), возвращаем
    пустую структуру с source_meetings=0. Это валидный ответ — клиент решает
    как отображать (например, "профиль ещё строится, нужно ≥3 встреч").

    Query-параметр `?rebuild=true` принудительно перестраивает Persona из
    свежих meetings (медленнее: ~5-15s, тратит немного LLM-токенов если
    активна communication-агрегация). Без него — отдаём cached/persisted
    результат, обычно ≤200ms. Phase A.2: автоматический rebuild по taskiq
    после ингеста новой встречи, тогда `rebuild=true` понадобится только
    для ручного refresh из UI.

    Контракт response в MINI_TESS_INTEGRATION.md §3.1.
    """
    user_id = _extract_user(authorization)
    store = get_persona_store()
    persona: Optional[Persona] = None

    if rebuild:
        # Принудительная пересборка из meetings — может занять секунды
        try:
            from backend.core.persona.aggregator import get_persona_aggregator
            agg = get_persona_aggregator()
            # LLM-роутер инжектируется на стороне chat-пути, для /persona/me
            # пока без LLM-обогащения (только behavioral + development).
            # Phase A.2: вынести запуск aggregator в taskiq + автоматический
            # планировщик после новых встреч; rebuild=true оставим как
            # ручной trigger из UI.
            persona = await agg.build_for_user(user_id=user_id)
        except Exception as exc:
            logger.warning("persona rebuild failed for %s: %s", user_id, exc)

    if persona is None:
        persona = await store.get(user_id)

    if persona is None:
        # Возвращаем заглушку с user_id и пустыми полями — Mini Tess
        # покажет «профиль ещё строится». 200 OK, не 404 — потому что
        # «нет данных» это валидное состояние.
        persona = Persona(user_id=user_id)
    return persona.to_public_dict()


@post("/me/clients", status_code=200)
async def push_client_archetype(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Mini Tess пушит client archetype в Persona founder'а.

    Body schema:
        {
          "client_id": "uuid_or_external",
          "name": "ACME Corp",
          "archetype": "enterprise_innovator",
          "industry": "fintech",
          "deal_stage": "negotiation",
          "interactions": [{"date": "...", "channel": "...", "summary": "..."}],
          "founder_notes": "...",
          "extracted_features": {"decision_style": "data_driven", ...}
        }

    Идемпотентно по client_id (upsert).
    """
    user_id = _extract_user(authorization)
    client_id = data.get("client_id")
    if not client_id:
        raise HTTPException(status_code=400, detail="client_id required")
    name = data.get("name")
    if not name:
        raise HTTPException(status_code=400, detail="name required")

    store = get_persona_store()
    persona = await store.get(user_id) or Persona(user_id=user_id)

    # Преобразуем interactions
    interactions = []
    for raw in data.get("interactions") or []:
        if not isinstance(raw, dict):
            continue
        interactions.append(
            ClientInteraction(
                date=raw.get("date", ""),
                channel=raw.get("channel", "unknown"),
                summary=raw.get("summary", ""),
                sentiment=raw.get("sentiment"),
            )
        )

    archetype = ClientArchetype(
        client_id=client_id,
        name=name,
        archetype=data.get("archetype"),
        industry=data.get("industry"),
        deal_stage=data.get("deal_stage"),
        interactions=interactions,
        founder_notes=data.get("founder_notes"),
        extracted_features=data.get("extracted_features") or {},
    )

    # Upsert по client_id
    existing_idx = next(
        (i for i, c in enumerate(persona.client_profiles) if c.client_id == client_id),
        None,
    )
    if existing_idx is not None:
        persona.client_profiles[existing_idx] = archetype
    else:
        persona.client_profiles.append(archetype)

    # Provenance: это пришло из Mini Tess
    await store.record_provenance(
        user_id,
        PersonaProvenanceEntry(
            field_path=f"client_profiles[{client_id}]",
            source_type=ProvenanceSource.MINI_TESS_INPUT,
            agent_or_actor="mini_tess",
            confidence=1.0,
        ),
    )
    await store.upsert(persona)

    return {
        "success": True,
        "client_id": client_id,
        "total_clients": len(persona.client_profiles),
    }


@delete("/me/clients/{client_id:str}", status_code=200)
async def delete_client_archetype(
    client_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Удалить client archetype из Persona founder'а."""
    user_id = _extract_user(authorization)
    store = get_persona_store()
    persona = await store.get(user_id)
    if persona is None:
        return {"success": True, "deleted": False, "reason": "no_persona"}
    before = len(persona.client_profiles)
    persona.client_profiles = [
        c for c in persona.client_profiles if c.client_id != client_id
    ]
    deleted = before > len(persona.client_profiles)
    if deleted:
        await store.upsert(persona)
    return {"success": True, "deleted": deleted}


# ─────────────────────────────────────────────────────────────────
# Phase A.3: manual-override + audit + sales-pack
# ─────────────────────────────────────────────────────────────────


@post("/me/override", status_code=200)
async def manual_override_field(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Founder вручную перезаписывает одно поле Persona.

    После override это поле приоритизируется над автоматической агрегацией —
    при следующем rebuild aggregator увидит ProvenanceSource.MANUAL и не
    тронет значение. UI: «изменить» на каждом отображаемом поле.

    Body:
        {
          "field_path": "behavioral.channel_preferences",
          "value": ["telegram", "email_long"],
          "note": "optional founder rationale, идёт в raw_evidence"
        }

    field_path — dotted-path внутри Persona. Поддерживаем top-level:
      - communication_cognitive.{preferred_style, vocabulary_register,
        metaphor_domains, abstraction_level, preferred_language}
      - behavioral.{channel_preferences, peak_hours_utc,
        do_not_disturb_windows_utc}
      - development.{active_goals, decision_style}
      - strengths_weaknesses.{strong, developing, blind_spots}

    Сложные поля (client_profiles, provenance) — через свои endpoint'ы.
    """
    user_id = _extract_user(authorization)
    field_path = data.get("field_path")
    if not field_path or not isinstance(field_path, str):
        raise HTTPException(status_code=400, detail="field_path required")
    if "value" not in data:
        raise HTTPException(status_code=400, detail="value required")
    value = data.get("value")
    note = data.get("note") or ""

    from backend.core.persona.external import (
        SOURCE_MANUAL,
        apply_field,
        record_external_field,
    )

    store = get_persona_store()
    persona = await store.get(user_id) or Persona(user_id=user_id)

    # Apply через общий whitelisted apply_field (с type-coercion enum'ов).
    # allow_extended=False: /me/override правит только типизированные поля,
    # free-form extended.* — исключительно через /me/self (audit HIGH-1).
    ok, err = apply_field(persona, field_path, value, allow_extended=False)
    if not ok:
        raise HTTPException(status_code=400, detail=err)

    await store.upsert(persona)
    # Phase A.5: пишем в durable persona_external_sources, чтобы ближайший
    # rebuild НЕ затёр override (раньше это был баг — provenance MANUAL
    # игнорировался агрегатором).
    await record_external_field(
        user_id=user_id, field_path=field_path, value=value,
        source_type=SOURCE_MANUAL, actor=f"user:{user_id}",
        confidence=1.0, note=note or None,
    )
    await store.record_provenance(
        user_id,
        PersonaProvenanceEntry(
            field_path=field_path,
            source_type=ProvenanceSource.MANUAL,
            agent_or_actor=f"user:{user_id}",
            confidence=1.0,
            raw_evidence=note or None,
        ),
    )
    logger.info("[persona] manual override %s for user %s", field_path, user_id)
    return {"success": True, "field_path": field_path, "source_type": "manual"}


@post("/me/self", status_code=200)
async def push_self_profile(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    x_source_app: Optional[str] = Parameter(header="X-Source-App", default=None),
) -> dict[str, Any]:
    """Phase A.5: внешний клиент (Mini Tess) пушит профиль сотрудника целиком.

    Канал записи для S1/S2/S3 (→ типизированные поля Persona) + S4 cogni /
    narrative (→ free-form extended.*). Mini Tess делает маппинг на своей
    стороне и шлёт уже в наших терминах. Значения durable и переживают
    rebuild (persona_external_sources), provenance = mini_tess_input.

    Body:
        {
          "fields": {                                  # whitelisted section.field
            "development.active_goals": ["enter_us_market"],
            "strengths_weaknesses.strong": [{"area": "sales", "confidence": 0.8}],
            "development.decision_style": "data_driven"
          },
          "extended": {                                # free-form vendor-секции
            "cogni": {...},        # S4: vitki / light_cone / nash_traps / equalizer
            "biography": {...}     # narrative: roles_history / milestones
          },
          "source": "mini_tess",                       # optional actor label
          "note": "from employee_profile B2 enrich"    # optional
        }

    Возвращает per-field результат: applied / rejected (+ причина).
    """
    user_id = _extract_user(authorization)
    fields = data.get("fields") if isinstance(data.get("fields"), dict) else {}
    extended = data.get("extended") if isinstance(data.get("extended"), dict) else {}
    if not fields and not extended:
        raise HTTPException(status_code=400, detail="fields or extended required")
    actor = str(data.get("source") or x_source_app or "mini_tess")
    note = data.get("note") or None

    from backend.core.persona.external import (
        SOURCE_MINI_TESS,
        apply_field,
        record_external_field,
    )

    store = get_persona_store()
    persona = await store.get(user_id) or Persona(user_id=user_id)

    applied: list[str] = []
    rejected: list[dict[str, str]] = []

    # 1. Типизированные поля (whitelisted).
    for fpath, value in fields.items():
        ok, err = apply_field(persona, str(fpath), value)
        if not ok:
            rejected.append({"field_path": str(fpath), "reason": err})
            continue
        applied.append(str(fpath))
        await record_external_field(
            user_id=user_id, field_path=str(fpath), value=value,
            source_type=SOURCE_MINI_TESS, actor=actor, note=note,
        )

    # 2. Free-form extended-секции (S4 cogni / narrative).
    for key, value in extended.items():
        fpath = f"extended.{key}"
        ok, err = apply_field(persona, fpath, value)
        if not ok:
            rejected.append({"field_path": fpath, "reason": err})
            continue
        applied.append(fpath)
        await record_external_field(
            user_id=user_id, field_path=fpath, value=value,
            source_type=SOURCE_MINI_TESS, actor=actor, note=note,
        )

    await store.upsert(persona)
    for fpath in applied:
        try:
            await store.record_provenance(
                user_id,
                PersonaProvenanceEntry(
                    field_path=fpath,
                    source_type=ProvenanceSource.MINI_TESS_INPUT,
                    agent_or_actor=actor,
                    confidence=1.0,
                    raw_evidence=note,
                ),
            )
        except Exception:
            logger.debug("self-push provenance skipped for %s", fpath, exc_info=True)

    logger.info("[persona] self-push %d applied / %d rejected for user %s (actor=%s)",
                len(applied), len(rejected), user_id, actor)
    # Audit-фикс (A.5 LOW-7): success отражает, применилось ли хоть что-то.
    # Раньше всегда возвращали success:true даже когда все поля отклонены —
    # клиент, проверяющий только success, думал что push сработал.
    return {
        "success": bool(applied),
        "applied": applied,
        "applied_count": len(applied),
        "rejected": rejected,
        "source_type": SOURCE_MINI_TESS,
    }


@get("/me/export", status_code=200)
async def export_persona_archive(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Phase A.5: полный архив Persona для преемственности / 360-review.

    Включает публичный профиль (вместе с extended cogni/biography) и список
    авторитетных external-полей с provenance. Это формат «виртуального
    сотрудника»: ушёл человек → новый учится у его слепка.
    """
    user_id = _extract_user(authorization)
    store = get_persona_store()
    persona = await store.get(user_id)
    if persona is None:
        raise HTTPException(status_code=404, detail="persona not built yet")

    from backend.core.persona.external import list_external_fields
    external = await list_external_fields(user_id)
    return {
        "schema_version": persona.schema_version,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "persona": persona.to_public_dict(),
        "external_sources": external,
        "external_count": len(external),
    }


@get("/me/audit", status_code=200)
async def get_persona_audit(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    field_path: Optional[str] = Parameter(query="field_path", default=None),
    limit: int = Parameter(query="limit", default=50),
) -> dict[str, Any]:
    """Provenance history Persona для UI «откуда это знает Tessbrain».

    Если field_path задан — фильтрует историю одного поля.
    Без него — все последние записи. Phase A.4: добавим pagination.
    """
    user_id = _extract_user(authorization)
    history: list[dict[str, Any]] = []
    try:
        from backend.db.postgres import get_postgres
        from backend.db.postgres_tenant import set_persona_tenant
        from sqlalchemy import text
        pg = await get_postgres()
        async with pg.session(apply_tenant=False) as session:
            # Phase A.6: persona_field_history под RLS — tenant=user_id.
            await set_persona_tenant(session, user_id)
            if field_path:
                rows = await session.execute(
                    text("""
                        SELECT field_path, source_type, agent_or_actor,
                               meeting_ids, confidence, raw_evidence, recorded_at
                        FROM public.persona_field_history
                        WHERE user_id = :uid AND field_path = :fp
                        ORDER BY recorded_at DESC LIMIT :lim
                    """),
                    {"uid": user_id, "fp": field_path, "lim": int(limit)},
                )
            else:
                rows = await session.execute(
                    text("""
                        SELECT field_path, source_type, agent_or_actor,
                               meeting_ids, confidence, raw_evidence, recorded_at
                        FROM public.persona_field_history
                        WHERE user_id = :uid
                        ORDER BY recorded_at DESC LIMIT :lim
                    """),
                    {"uid": user_id, "lim": int(limit)},
                )
            for r in rows:
                history.append({
                    "field_path": r[0],
                    "source_type": r[1],
                    "agent_or_actor": r[2],
                    "meeting_ids": list(r[3] or []),
                    "confidence": float(r[4] or 0.0),
                    "raw_evidence": r[5],
                    "recorded_at": r[6].isoformat() if r[6] else None,
                })
    except Exception as exc:
        logger.warning("persona audit query failed: %s", exc)
    return {
        "user_id": user_id,
        "field_path_filter": field_path,
        "total": len(history),
        "history": history,
    }


@get("/me/sales-pack", status_code=200)
async def get_sales_pack(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    client_id: str = Parameter(query="client_id", default=""),
    situation_override: Optional[str] = Parameter(query="situation", default=None),
) -> dict[str, Any]:
    """Phase A.3: sales-pack под клиента через WisdomEngine pattern matching.

    Что делает:
    1. Берёт ClientArchetype из Persona (по client_id, push от Mini Tess)
    2. Строит WisdomQuery (situation = архетип + индустрия + deal_stage,
       tension_type производный от deal_stage)
    3. WisdomEngine ищет похожие прошлые случаи в графе, возвращает
       wisdom_narrative + similar_patterns + recommendation
    4. Собираем sales-pack из этих частей + базовой инфы клиента

    Response shape (см. MINI_TESS_INTEGRATION.md §3.3).

    Если client_id не найден в Persona — 404.
    Если WisdomEngine недоступен → возвращаем reduced-pack только из
    Persona-данных, без pattern matching (best-effort).
    """
    user_id = _extract_user(authorization)
    if not client_id:
        raise HTTPException(status_code=400, detail="client_id query param required")

    store = get_persona_store()
    persona = await store.get(user_id)
    if persona is None:
        raise HTTPException(status_code=404, detail="Persona not built yet")

    client = next(
        (c for c in persona.client_profiles if c.client_id == client_id), None,
    )
    if client is None:
        raise HTTPException(
            status_code=404,
            detail=f"Client {client_id} not in Persona — push it via /persona/me/clients first",
        )

    # Базовые talking points всегда — даже без WisdomEngine
    sales_pack: dict[str, Any] = {
        "client_id": client.client_id,
        "client_name": client.name,
        "archetype": client.archetype,
        "industry": client.industry,
        "deal_stage": client.deal_stage,
        "interactions_count": len(client.interactions),
        "founder_notes": client.founder_notes,
        "extracted_features": dict(client.extracted_features),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "talking_points": [],
        "risks_to_address": [],
        "recommended_strategy": None,
        "similar_past_clients": [],
        "next_action_draft": None,
    }

    # Базовые talking_points выводим из features+archetype без LLM
    if client.archetype == "enterprise_innovator":
        sales_pack["talking_points"].append("Подчеркнуть security/compliance story и references")
        sales_pack["talking_points"].append("Спросить про roadmap и долгосрочные приоритеты")
    elif client.archetype == "corporate_buyer":
        sales_pack["talking_points"].append("TCO calculator + SLA-договор как основные артефакты")
        sales_pack["risks_to_address"].append("Procurement-цикл может занять 3-9 мес — план B на это время")
    elif client.archetype == "startup_explorer":
        sales_pack["talking_points"].append("Speed to value, demo сразу — никаких long discoveries")
        sales_pack["recommended_strategy"] = "Закрыть на trial в первую неделю"
    elif client.archetype == "government_regulated":
        sales_pack["risks_to_address"].append("Compliance frameworks (ФЗ-152) — иметь готовый pack")
        sales_pack["risks_to_address"].append("Тендер обязателен, 12-24 мес процесс")
    elif client.archetype == "consultancy_reseller":
        sales_pack["talking_points"].append("Partnership-frame, не product-frame. Revenue share / margin")
    elif client.archetype == "individual_power_user":
        sales_pack["talking_points"].append("Глубина одной фичи + privacy + integration с personal stack")

    # Pattern matching через WisdomEngine — если есть исторические данные
    situation = situation_override or (
        f"Sales meeting with {client.name} ({client.industry or 'unknown industry'}), "
        f"archetype={client.archetype or 'unknown'}, deal_stage={client.deal_stage or 'unknown'}. "
        f"Founder notes: {(client.founder_notes or '')[:300]}"
    )
    try:
        from backend.core.wisdom.bwe_models import WisdomQuery
        from backend.core.wisdom.wisdom_engine import get_wisdom_engine

        we = await get_wisdom_engine()
        wq = WisdomQuery(
            user_id=user_id,
            situation=situation,
            tension_type=_tension_from_deal_stage(client.deal_stage),
            company_vitek=1,
            market_context=client.industry or "",
            max_patterns=5,
        )
        wisdom_response = await we.query(wq)
        sales_pack["recommended_strategy"] = (
            wisdom_response.recommendation
            or wisdom_response.wisdom_narrative[:400]
            or sales_pack["recommended_strategy"]
        )
        sales_pack["wisdom_narrative"] = wisdom_response.wisdom_narrative
        sales_pack["wisdom_confidence"] = wisdom_response.overall_confidence
        sales_pack["wisdom_cases_analyzed"] = wisdom_response.total_cases_analyzed
        sales_pack["similar_past_clients"] = [
            {
                "pattern_id": p.pattern_id,
                "pattern_name": p.pattern_name,
                "similarity": p.similarity_score,
                "confidence": p.confidence,
                "total_cases": p.total_cases,
                "outcomes": dict(p.outcome_distribution),
            }
            for p in wisdom_response.similar_patterns[:3]
        ]
    except Exception as exc:
        logger.warning("[persona] sales-pack: WisdomEngine unavailable, reduced pack: %s", exc)
        sales_pack["wisdom_narrative"] = None
        sales_pack["wisdom_confidence"] = "unavailable"

    return sales_pack


def _tension_from_deal_stage(deal_stage: Optional[str]) -> str:
    """Mapping deal_stage → tension_type для WisdomQuery.

    WisdomEngine использует tension_type для отделения паттернов
    «как закрыть discovery» от «как закрыть negotiation». Маппинг
    эвристический; уточним по статистике использования.
    """
    if not deal_stage:
        return "client_engagement"
    mapping = {
        "discovery": "client_qualification",
        "qualification": "client_qualification",
        "proposal": "client_negotiation",
        "negotiation": "client_negotiation",
        "stale": "client_reactivation",
        "lost": "client_recovery",
        "won": "client_expansion",
    }
    return mapping.get(deal_stage, "client_engagement")


# Imports used by Phase A.3 handlers (sales-pack timestamps)
from datetime import datetime, timezone  # noqa: E402


router = Router(
    path="/persona",
    route_handlers=[
        get_my_persona,
        push_client_archetype,
        delete_client_archetype,
        manual_override_field,
        push_self_profile,
        export_persona_archive,
        get_persona_audit,
        get_sales_pack,
    ],
    tags=["Persona"],
)
