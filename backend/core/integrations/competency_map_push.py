# -*- coding: utf-8 -*-
"""Исходящий пуш в карту компетенций — POST /api/v1/ingest/tessent.

Реализация по их подтверждённому контракту (docs/integrations/
COMPETENCY_MAP_ANSWER.md): cogni нормализуем из наших витков 1..4 в их
шкалу 0..1, шлём department-сущности РАНЬШЕ person-сущностей (их находка:
`parent` требует существующего родителя — нарушение порядка не ломает
пуш, просто человек уедет без привязки и починится следующим прогоном по
`external_id`), `fill_missing` всегда `false` (по их же просьбе — не
плодим вторую, расходящуюся оценку).

Три замка на когнитивный профиль — те же, что у person_profile.py:
подтверждённая сшивка «это я», тот же орг, для чужого профиля — только
founder/admin/manager запускает пуш. Человек без сшивки/данных не молча
теряется — попадает в stats.skipped с причиной.

Deny-by-default: ENABLE_COMPETENCY_MAP_PUSH (OFF). dry_run считает и
формирует payload'ы без единого сетевого вызова — работает всегда.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

VITKI_DOMAINS = ("professional", "technical", "leadership", "personal")


def push_enabled() -> bool:
    return os.environ.get("ENABLE_COMPETENCY_MAP_PUSH", "").strip().lower() in (
        "1", "on", "true", "yes")


def _target() -> Tuple[str, str]:
    url = (os.environ.get("COMPETENCY_MAP_URL", "") or "").strip().rstrip("/")
    token = (os.environ.get("COMPETENCY_MAP_TOKEN", "") or "").strip()
    return url, token


def cogni_vitki_to_01(cogni: Optional[Dict[str, Any]]
                      ) -> Tuple[Dict[str, float], Dict[str, float], Dict[str, List[str]]]:
    """Витки 1..4 → их шкала 0..1. Возврат: (signals_01, confidence, evidence).

    Домен без уровня (level=None — измерение не накоплено) не попадает в
    signals вовсе: у них «частичный профиль — нормально», досылать 0.5
    как «не знаю» было бы ложью."""
    signals: Dict[str, float] = {}
    confidence: Dict[str, float] = {}
    evidence: Dict[str, List[str]] = {}
    vitki = (cogni or {}).get("vitki") or {}
    for domain in VITKI_DOMAINS:
        v = vitki.get(domain) or {}
        level = v.get("level")
        if level is None:
            continue
        try:
            lvl = int(level)
        except (TypeError, ValueError):
            continue
        signals[domain] = round((lvl - 1) / 3, 4)
        conf = v.get("confidence")
        if isinstance(conf, (int, float)):
            confidence[domain] = float(conf)
        ev = [str(e).strip() for e in (v.get("evidence") or []) if str(e).strip()]
        if ev:
            evidence[domain] = ev[:5]
    return signals, confidence, evidence


async def _department_nodes(gb, user_id: str) -> Dict[str, str]:
    """Имя отдела (как в membership.department) → id узла графа."""
    out: Dict[str, str] = {}
    try:
        _tid = user_id or None
        nodes = await gb.get_all_nodes_async(label="Department", limit=500,
                                             tenant_id=_tid,
                                             strict_tenant=bool(_tid))
    except Exception:
        return out
    for n in nodes or []:
        name = str(n.get("name") or "").strip()
        nid = str(n.get("id") or "").strip()
        if name and nid:
            # ключ — нормализованный (для матча с membership.department),
            # значение хранит ОРИГИНАЛЬНОЕ написание: «IT» не должно
            # превратиться в «It» в чужой карте
            out[name.lower()] = {"id": nid, "label": name}
    return out


def _facts_from_snapshot(snap: Any) -> List[str]:
    """Факты человека из PersonSnapshot: достижения, решения, сильные стороны.

    Именно это обещано карте компетенций как источник `facts` — их сторона
    может добрать по ним недостающие вертикали (у себя, не у нас). Берём
    только реально заполненное; пустой снапшот → пустой список."""
    facts: List[str] = []
    for item in (getattr(snap, "recent_achievements", None) or [])[:3]:
        s = str(item).strip()
        if s:
            facts.append(s)
    for d in (getattr(snap, "decisions", None) or [])[:3]:
        s = str((d or {}).get("summary") if isinstance(d, dict) else d).strip()
        if s:
            facts.append(f"решение: {s}")
    for item in (getattr(snap, "strengths", None) or [])[:2]:
        s = str(item).strip()
        if s:
            facts.append(f"сильная сторона: {s}")
    role = str(getattr(snap, "role", "") or "").strip()
    if role:
        facts.insert(0, f"роль: {role}")
    return facts[:8]


async def build_payloads(user_id: str) -> Dict[str, Any]:
    """Собрать все payload'ы пуша (отделы, потом люди). Сети не касается."""
    from backend.core.identity.person_profile import profile_for_person
    from backend.core.ingest import membership
    from backend.core.store.graph_view import merged_graph_view_for_user

    org_id = membership.get_org_for_user(user_id)
    if not org_id:
        return {"status": "error",
                "message": "нет организации: пуш имеет смысл только для команды"}
    role = membership.get_user_org_base_role(user_id)
    if role not in ("founder", "admin", "manager"):
        return {"status": "error",
                "message": f"роль {role or 'участник'} не может запускать "
                           "выгрузку профилей команды"}

    from backend.core.sleep.enhanced_snapshot import (
        get_enhanced_snapshot_generator,
    )

    person_names: Dict[str, str] = {}
    dept_ids: Dict[str, str] = {}
    snapshots: Dict[str, Any] = {}
    gb = await merged_graph_view_for_user(user_id, use_networkx=None)
    try:
        dept_ids = await _department_nodes(gb, user_id)
        try:
            _tid = user_id or None
            for n in (await gb.get_all_nodes_async(
                    label="Person", limit=5000, tenant_id=_tid,
                    strict_tenant=bool(_tid)) or []):
                nid = str(n.get("id") or "").strip()
                name = str(n.get("name") or "").strip()
                if nid and name:
                    person_names[nid] = name
        except Exception:
            logger.debug("competency push: person names unavailable",
                        exc_info=True)
        # PersonSnapshot — источник facts (обещано карте компетенций).
        # ЧИТАЕМ УЖЕ ПОСТРОЕННОЕ, не генерируем: get_person_snapshot() при
        # взведённом маркере пересборки делает обход графа и вызов LLM на
        # КАЖДОГО человека — в организации на 200 человек это 200 LLM-вызовов
        # и 200 записей на диск внутри одного HTTP-запроса, причём и на
        # dry_run, который обещан как «без единого сетевого вызова».
        try:
            gen = get_enhanced_snapshot_generator(gb, user_id=user_id)
            cached = getattr(gen, "_person_snapshots", None) or {}
            for m in membership.list_members(org_id):
                pid = m.get("person_entity_id")
                if pid and pid not in snapshots:
                    snapshots[pid] = cached.get(pid)
        except Exception:
            logger.warning("competency push: снапшоты людей недоступны",
                           exc_info=True)
    finally:
        try:
            await gb.close(save=False)
        except Exception:
            pass

    dept_payloads = [{"label": rec["label"], "kind": "department",
                      "external_id": rec["id"], "fill_missing": False}
                     for rec in dept_ids.values()]

    person_payloads: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    for m in membership.list_members(org_id):
        target_uid = m.get("user_id")
        person_entity_id = m.get("person_entity_id")
        if not person_entity_id:
            skipped.append({"user_id": target_uid or "",
                            "reason": "не подтверждена сшивка «это я»"})
            continue
        try:
            profile = await profile_for_person(person_entity_id, org_id, user_id)
        except Exception:
            logger.warning("competency push: profile_for_person failed",
                           exc_info=True)
            profile = None
        cogni = (profile or {}).get("cogni")
        signals, confidence, evidence = cogni_vitki_to_01(cogni)

        snap = snapshots.get(person_entity_id)
        facts = _facts_from_snapshot(snap) if snap is not None else []

        if not signals and not facts:
            skipped.append({"user_id": target_uid or "",
                            "reason": "нет накопленных cogni-измерений и фактов"})
            continue

        payload: Dict[str, Any] = {
            "label": person_names.get(person_entity_id, person_entity_id),
            "kind": "person",
            "external_id": person_entity_id,
            "fill_missing": False,
        }
        dept_name = str(m.get("department") or "").strip().lower()
        if dept_name and dept_name in dept_ids:
            payload["parent"] = dept_ids[dept_name]["id"]
        if signals:
            payload["cogni"] = signals
            payload["cogni_meta"] = {"scale_source": "vitki_1_4",
                                     "confidence": confidence,
                                     "evidence": evidence}
        if facts:
            payload["facts"] = facts
        person_payloads.append(payload)

    return {"status": "success", "departments": dept_payloads,
            "people": person_payloads, "skipped": skipped}


async def push_to_competency_map(user_id: str, *, dry_run: bool = False
                                 ) -> Dict[str, Any]:
    """Полный цикл: собрать payload'ы → (dry_run: вернуть план) → отправить.

    Порядок важен (их находка): все отделы, потом все люди."""
    built = await build_payloads(user_id)
    if built["status"] != "success":
        return built
    if dry_run:
        return {**built, "status": "dry_run"}
    if not push_enabled():
        return {"status": "disabled",
                "message": "COMPETENCY_MAP_URL настроен, но "
                           "ENABLE_COMPETENCY_MAP_PUSH выключен. "
                           "Предпросмотр — dry_run=true."}
    url, token = _target()
    if not (url and token):
        return {"status": "error",
                "message": "COMPETENCY_MAP_URL/COMPETENCY_MAP_TOKEN не заданы"}

    import httpx
    sent, failed = [], []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for payload in built["departments"] + built["people"]:
            try:
                r = await client.post(
                    f"{url}/api/v1/ingest/tessent", json=payload,
                    headers={"Authorization": f"Bearer {token}"})
                if r.status_code < 300:
                    sent.append({"label": payload["label"],
                                 "external_id": payload["external_id"]})
                else:
                    failed.append({"label": payload["label"],
                                   "external_id": payload["external_id"],
                                   "status": r.status_code})
            except Exception as e:
                failed.append({"label": payload["label"],
                               "external_id": payload["external_id"],
                               "error": str(e)})

    logger.info("🧭 competency map push: %d ok, %d failed, %d skipped",
                len(sent), len(failed), len(built["skipped"]))
    # Честный статус: всё упало — это не «success». Иначе оператор увидит
    # «успех» при полностью недоступном API получателя.
    status = "success" if sent else ("error" if failed else "success")
    return {"status": status, "sent": sent, "failed": failed,
            "skipped": built["skipped"],
            "message": ("ни один payload не доставлен" if failed and not sent
                        else "")}
