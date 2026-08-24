# -*- coding: utf-8 -*-
"""Мост capture → persona_observations для когнитивных измерений (Ф1.3).

CogniDimensionsAgent извлекает профили ПО ИМЕНАМ участников встречи, а
persona_observations хранит строки ПО АККАУНТАМ (user_id). Этот модуль решает
атрибуцию «имя из транскрипта → аккаунт» и персистит каждому сшитому человеку
его собственный профиль:

1. **Сшивка Ф0 (единственный путь для сотрудников):** имя → EntityResolver
   (тенант владельца) → канонический person_entity_id → membership.find_user_
   by_person_entity → аккаунт. Профиль сотрудника пишется ТОЛЬКО при
   подтверждённом «это я» — принцип «сшивка с согласия человека»: голое
   совпадение имени («Иван» из транскрипта ↔ аккаунт «Иван Петров») слишком
   часто указывает на другого человека (клиент, тёзка), а профиль — вещь
   чувствительная. Резолвер вызывается только если в орге вообще есть сшивки,
   и созданная резолвером НОВАЯ сущность (is_new) сшивкой не считается.
2. **Совпадение имени — только для ВЛАДЕЛЬЦА ингеста** (его тенант, он точно
   участник своих встреч): порог 0.75 (токен-костяк имени).
3. Не сшилось — строка НЕ пишется (наблюдение о неизвестно-ком бесполезно
   и опасно: чужой профиль в чужой персоне хуже пустого).

Метод атрибуции и счёт записываются в payload — потребитель видит, откуда
взялась строка. Never-raise: любая ошибка → пропуск строки, не падение ингеста.

Здесь же обратный конвертер payload → cogni-схема (для агрегатора персоны):
он живёт рядом с местом, где формат payload определён.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from backend.core.cogni.dimensions import (
    EQUALIZER_AXES,
    VITKI_DOMAINS,
    default_cogni,
    set_equalizer_axis,
    set_light_cone,
    set_phase_state,
    set_vitok,
)

logger = logging.getLogger(__name__)

AGENT_TYPE = "cogni_dimensions_agent"

# Порог совпадения имени аккаунта с именем из транскрипта: 0.75 = общий
# токен-костяк (Максим ↔ Максим Белухин). Ниже — не атрибутируем.
_NAME_MATCH_MIN = 0.75


# ── payload → cogni-схема (читается агрегатором персоны) ──

def cogni_from_observation_payload(
    payload: Dict[str, Any], meeting_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Собрать валидный cogni-dict из payload наблюдения агента.

    Payload — канонизированный профиль одного человека (см.
    CogniDimensionsAgent.normalize): vitki/light_cone/equalizer/phase_state
    с цитатами. Цитаты становятся evidence; ссылка на встречу — отдельным
    элементом evidence. Мусор игнорируется (свою нормализацию делает схема)."""
    c = default_cogni()
    if not isinstance(payload, dict):
        return c
    mref = f"meeting:{meeting_id}" if meeting_id else None

    def _ev(measure: Dict[str, Any]) -> List[str]:
        ev = []
        q = str(measure.get("quote") or "").strip()
        if q:
            ev.append(q)
        if mref:
            ev.append(mref)
        return ev

    vitki = payload.get("vitki")
    if isinstance(vitki, dict):
        for domain in VITKI_DOMAINS:
            m = vitki.get(domain)
            if isinstance(m, dict) and m.get("level") is not None:
                set_vitok(c, domain, m.get("level"),
                          float(m.get("confidence") or 0.0), _ev(m))

    lc = payload.get("light_cone")
    if isinstance(lc, dict) and any(lc.get(k) for k in ("horizon", "scale", "depth")):
        set_light_cone(c, horizon=lc.get("horizon"), scale=lc.get("scale"),
                       depth=lc.get("depth"),
                       confidence=float(lc.get("confidence") or 0.0),
                       evidence=_ev(lc))

    eq = payload.get("equalizer")
    if isinstance(eq, dict):
        for axis in EQUALIZER_AXES:
            m = eq.get(axis)
            if isinstance(m, dict) and m.get("value") is not None:
                set_equalizer_axis(c, axis, float(m.get("value") or 0.0),
                                   float(m.get("confidence") or 0.0))

    ps = payload.get("phase_state")
    if isinstance(ps, dict) and ps.get("state"):
        note = str(ps.get("note") or "").strip() or str(ps.get("quote") or "").strip()
        set_phase_state(c, ps.get("state"),
                        float(ps.get("confidence") or 0.0), note)
    return c


# ── атрибуция имя → аккаунт ──

def _org_has_stitches(org_id: Optional[str]) -> bool:
    """Есть ли в орге хоть один подтверждённый «это я» (иначе резолвер не
    зовём вовсе — resolve() имеет write-side-effect find-or-create)."""
    if not org_id:
        return False
    try:
        from backend.core.ingest import membership
        return any(m.get("person_entity_id")
                   for m in membership.list_members(org_id))
    except Exception:
        logger.debug("cogni: stitch presence check skipped", exc_info=True)
        return False


async def _resolve_by_stitch(
    name: str, owner_uid: str, org_id: Optional[str], entity_resolver: Any,
) -> Optional[str]:
    """Имя → канонический Person (в графе владельца) → сшитый аккаунт (Ф0).

    Свежесозданная резолвером сущность (is_new) сшивкой быть не может: раз её
    не было, никто не подтверждал против неё «это я». Слаг-идентификаторы
    производны от имени и совпадают между графами — без этой проверки чужое
    имя из транскрипта могло бы «попасть» в сшивку однофамильца."""
    if not (entity_resolver and org_id):
        return None
    try:
        r = await entity_resolver.resolve(
            name=name, entity_type="person", organization_id=owner_uid)
        pid = getattr(r, "canonical_id", None)
        if not pid or getattr(r, "is_new", False):
            return None
        from backend.core.ingest import membership
        return membership.find_user_by_person_entity(str(pid), org_id)
    except Exception:
        logger.debug("cogni stitch resolve skipped for %r", name, exc_info=True)
        return None


async def _owner_name(owner_uid: str) -> str:
    """Имя аккаунта владельца ингеста (пустая строка, если не заполнено)."""
    try:
        from backend.core.identity.identity_service import _account_identity
        ident = await _account_identity(owner_uid)
        return str(ident.get("name") or "")
    except Exception:
        logger.debug("cogni: owner identity skipped", exc_info=True)
        return ""


def _match_owner_name(name: str, owner_name: str) -> float:
    """Счёт совпадения имени профиля с именем аккаунта владельца (0 = нет)."""
    if not (name and owner_name):
        return 0.0
    try:
        from backend.core.identity.identity_service import _name_score
        s = _name_score(owner_name, name)
        return s if s >= _NAME_MATCH_MIN else 0.0
    except Exception:
        return 0.0


async def persist_cogni_observations(
    owner_uid: str,
    meeting_id: str,
    profiles: List[Dict[str, Any]],
    *,
    entity_resolver: Any = None,
    rebuild_personas: bool = True,
) -> Dict[str, Any]:
    """Атрибутировать профили участников к аккаунтам и записать наблюдения.

    Возвращает {"saved": N, "skipped": M, "attributed": {uid: name}}.
    Строка пишется под user_id САМОГО человека (tenant = его же), чтобы
    агрегатор его персоны видел её при обычном чтении. Never-raise."""
    out: Dict[str, Any] = {"saved": 0, "skipped": 0, "attributed": {}}
    if not (owner_uid and meeting_id) or not profiles:
        return out
    try:
        from backend.core.ingest import membership
        org_id = membership.get_org_for_user(owner_uid)
    except Exception:
        org_id = None

    owner_name = await _owner_name(owner_uid)
    # резолвер зовём только когда сшивки в орге вообще существуют:
    # у resolve() есть write-side-effect (find-or-create)
    stitch_enabled = _org_has_stitches(org_id)

    # лучший профиль на аккаунт (дубли имён «Максим»/«Максим Белухин» на одной
    # встрече не должны перетирать друг друга произвольно)
    best: Dict[str, Dict[str, Any]] = {}
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        name = str(profile.get("name") or "").strip()
        if not name:
            out["skipped"] += 1
            continue

        uid: Optional[str] = None
        method, score = "", 0.0
        if stitch_enabled:
            stitched = await _resolve_by_stitch(
                name, owner_uid, org_id, entity_resolver)
            if stitched:
                uid, method, score = stitched, "stitch", 1.0
        if not uid:
            s = _match_owner_name(name, owner_name)
            if s > 0:
                uid, method, score = owner_uid, "owner_name_match", s

        if not uid:
            out["skipped"] += 1
            continue

        enriched = {**profile,
                    "attribution": {"method": method, "score": round(score, 2),
                                    "matched_name": name, "owner_uid": owner_uid}}
        cur = best.get(uid)
        if cur is None or float(enriched.get("confidence") or 0.0) > float(
                cur.get("confidence") or 0.0):
            best[uid] = enriched

    if not best:
        return out

    try:
        from backend.core.persona.observations import save_observation
    except Exception:
        logger.warning("cogni: observations store unavailable", exc_info=True)
        return out

    for uid, payload in best.items():
        # явный 0 от агента — честный 0, а не «нет данных» (иначе 0 → 0.5)
        raw_conf = payload.get("confidence")
        row_conf = 0.5 if raw_conf is None else max(0.0, min(1.0, float(raw_conf or 0.0)))
        try:
            ok = await save_observation(
                user_id=uid, meeting_id=meeting_id, agent_type=AGENT_TYPE,
                payload=payload,
                confidence=row_conf,
                source_type="capture_agent",
            )
        except Exception:
            logger.debug("cogni: save_observation failed for %s", uid,
                         exc_info=True)
            ok = False
        if ok:
            out["saved"] += 1
            out["attributed"][uid] = payload.get("name")
        else:
            out["skipped"] += 1

    # Замыкание контура: пересобрать персоны затронутых аккаунтов, чтобы
    # extended["cogni"] обновился без ручного ?rebuild=true. Фоном — сборка
    # персоны тяжёлая (тянет 30 встреч на аккаунт), нельзя держать ею ингест.
    if rebuild_personas and out["attributed"]:
        uids = list(out["attributed"])
        if len(uids) > 10:
            logger.info("cogni: пересборка персон урезана до 10 из %d", len(uids))
        _spawn_persona_rebuilds(uids[:10])
    return out


# держим ссылки на фоновые задачи, иначе их соберёт GC до завершения
_bg_tasks: set = set()


def _spawn_persona_rebuilds(uids: List[str]) -> None:
    """Фоновая best-effort пересборка персон (не блокирует ингест встречи)."""
    import asyncio

    async def _run() -> None:
        try:
            from backend.core.persona.aggregator import get_persona_aggregator
            agg = get_persona_aggregator()
        except Exception:
            logger.debug("cogni: aggregator unavailable", exc_info=True)
            return
        for uid in uids:
            try:
                await agg.build_for_user(user_id=uid)
            except Exception:
                logger.debug("cogni: persona rebuild skipped for %s", uid,
                             exc_info=True)

    try:
        task = asyncio.get_running_loop().create_task(_run())
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
    except RuntimeError:
        logger.debug("cogni: no running loop, rebuild skipped")
