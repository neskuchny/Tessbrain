# -*- coding: utf-8 -*-
"""Temporal compare — несколько временных срезов одного объекта.

issue #112 follow-up (запрос Сергея «найти управленческую ошибку во
времени»). Композирует:
  - TemporalTracker.get_version_at_date() — snapshot на дату
  - простой field-level diff между snapshot'ами
  - опционально: compute_observer_drift() — где система переоценивала
  - human-readable narrative

Пример запроса:
  GET /api/v1/temporal/compare
      ?user_id=u-1&entity_type=project&entity_id=acme_pipeline
      &dates=2026-05-15,2026-06-01,2026-06-20
      &include_drift=true

Возвращает:
  {
    "entity_id": "acme_pipeline",
    "slices": [
      {"date": "2026-05-15", "found": true, "version": 1, "snapshot": {...}},
      {"date": "2026-06-01", "found": true, "version": 2, "snapshot": {...}},
      {"date": "2026-06-20", "found": true, "version": 4, "snapshot": {...}}
    ],
    "deltas": [
      {"from_date": "2026-05-15", "to_date": "2026-06-01",
       "added": {...}, "removed": {...}, "changed": {...}},
      {"from_date": "2026-06-01", "to_date": "2026-06-20", ...}
    ],
    "drift": {... если include_drift=true ...},
    "narrative": "За периoд май → июнь добавлены 3 задачи, статус
                  сменился с planning на in_progress…"
  }
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def diff_snapshots(
    before: Optional[Dict[str, Any]],
    after: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Field-level diff двух snapshot-словарей.

    Возвращает:
      - added: ключи в after, нет в before
      - removed: ключи в before, нет в after
      - changed: ключи в обоих, значения разные ({"field": [old, new]})

    Безопасно обрабатывает None (отсутствующий snapshot — отдельный case
    «версии на дату не было»).
    """
    b = before or {}
    a = after or {}
    if not isinstance(b, dict) or not isinstance(a, dict):
        return {"added": {}, "removed": {}, "changed": {},
                "note": "snapshots are not dict — diff skipped"}

    keys_b = set(b.keys())
    keys_a = set(a.keys())

    added = {k: a[k] for k in keys_a - keys_b}
    removed = {k: b[k] for k in keys_b - keys_a}
    changed: Dict[str, Any] = {}
    for k in keys_a & keys_b:
        if b[k] != a[k]:
            # Для больших структур (списки/dict) сохраняем оба значения,
            # caller может сделать deeper diff если нужно.
            changed[k] = [b[k], a[k]]

    return {"added": added, "removed": removed, "changed": changed}


def _summarize_delta(delta: Dict[str, Any]) -> str:
    """Короткая человекочитаемая строка по дельте."""
    parts = []
    n_added = len(delta.get("added") or {})
    n_removed = len(delta.get("removed") or {})
    n_changed = len(delta.get("changed") or {})
    if n_added:
        parts.append(f"добавлено {n_added}")
    if n_changed:
        parts.append(f"изменено {n_changed}")
    if n_removed:
        parts.append(f"удалено {n_removed}")
    if not parts:
        return "без изменений"
    return ", ".join(parts) + " полей"


def build_narrative(slices: List[Dict[str, Any]],
                    deltas: List[Dict[str, Any]]) -> str:
    """Простой narrative без LLM — детерминистическая сборка.

    Пример:
      «3 среза: 2026-05-15 (v1) → 2026-06-01 (v2): добавлено 2 поля,
       изменено 1 поле. → 2026-06-20 (v4): изменено 3 поля.»

    LLM-нарратив можно прицепить отдельным эндпоинтом если понадобится
    «editorial» подача (как в HomeTab tour-builder).
    """
    if not slices:
        return "Нет данных за указанные даты."

    found_count = sum(1 for s in slices if s.get("found"))
    if found_count == 0:
        return ("Ни на одну из дат не было сохранённой версии объекта. "
                "Возможно, объект ещё не отслеживался в TemporalTracker.")

    parts = [f"{len(slices)} среза(ов), найдено {found_count}."]
    for d in deltas:
        parts.append(
            f"{d['from_date']} → {d['to_date']}: {_summarize_delta(d)}.")
    return " ".join(parts)


async def compare_temporal_slices(
    *,
    user_id: str,
    entity_type: str,
    entity_id: str,
    dates: List[str],
    include_drift: bool = False,
) -> Dict[str, Any]:
    """Главный entry-point. Собирает срезы по датам, diff'ает соседние,
    опционально добавляет observer drift.

    user_id — нужен для TemporalTracker (per-user изоляция данных) и для
    observer_drift query.

    entity_type ('project'|'task'|'decision'|'person'|...) сейчас
    используется только как метаинформация — TemporalTracker берёт по
    entity_id напрямую. В будущем поможет разрешать конфликты id.
    """
    if not dates:
        return {"error": "dates parameter is empty"}
    # Дедуп + сортировка по возрастанию — диф между соседними имеет смысл
    dates_sorted = sorted(set(d.strip() for d in dates if d and d.strip()))

    try:
        from backend.core.temporal.temporal_tracker import get_temporal_tracker
        tracker = get_temporal_tracker(user_id)
        # Lazy init: get_version_at_date сама вызовет initialize()
    except Exception as e:
        logger.error("temporal_tracker init failed: %s", e)
        return {"error": f"tracker unavailable: {e}"}

    slices: List[Dict[str, Any]] = []
    for date in dates_sorted:
        try:
            v = await tracker.get_version_at_date(entity_id, date)
        except Exception as e:
            logger.warning("get_version_at_date failed (date=%s): %s", date, e)
            v = None
        if v is None:
            slices.append({"date": date, "found": False,
                           "snapshot": None, "version": None})
        else:
            slices.append({
                "date": date,
                "found": True,
                "version": v.get("version"),
                "snapshot": v.get("data"),
                "recorded_at": v.get("timestamp"),
                "changed_by": v.get("changed_by"),
                "change_type": v.get("change_type"),
            })

    # Дифф между соседними найденными срезами
    deltas: List[Dict[str, Any]] = []
    for i in range(len(slices) - 1):
        before, after = slices[i], slices[i + 1]
        delta = diff_snapshots(before.get("snapshot"), after.get("snapshot"))
        deltas.append({
            "from_date": before["date"],
            "to_date": after["date"],
            **delta,
        })

    result: Dict[str, Any] = {
        "user_id": user_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "slices": slices,
        "deltas": deltas,
        "narrative": build_narrative(slices, deltas),
    }

    if include_drift:
        # Observer drift — отдельный сигнал «где система переоценивала».
        # Best-effort; если wisdom_engine недоступен — поле просто пустое.
        try:
            from backend.core.wisdom.wisdom_engine import get_wisdom_engine
            engine = await get_wisdom_engine()
            drift = await engine.compute_observer_drift(user_id, tension_type=None)
            result["drift"] = drift
        except Exception as e:
            logger.info("observer_drift unavailable (non-fatal): %s", e)
            result["drift"] = {"error": str(e)}

    return result


# ═══════════════════════════════════════════════════════════════════════
# Срез КОМПАНИИ на дату (а не одного объекта)
# ═══════════════════════════════════════════════════════════════════════
# Данные для этого лежали в сторе с самого начала: версии person/decision/
# task/project пишутся на каждой встрече. Не хватало только сборки — сложить
# «последнюю версию каждой сущности на дату» в один срез. Собирается из
# list_entities() + get_version_at_date() без новых таблиц и без LLM.

# Сколько сущностей берём в срез. Компания на несколько сотен объектов
# помещается целиком; выше — режем и ЧЕСТНО сообщаем об этом в ответе,
# чтобы обрезанный срез не выглядел полным.
DEFAULT_STATE_LIMIT = 500


def _is_foreign(data: Any, allowed: Optional[set]) -> bool:
    """Запись с ЧУЖИМ штампом tenant_id. Без штампа — своя (CREATE-ветка
    knowledge_sync tenant_id в data не пишет). Тот же контракт, что в
    read-фильтре API, чтобы срез не протёк между тенантами."""
    if not allowed or not isinstance(data, dict):
        return False
    t = data.get("tenant_id")
    return t not in (None, "") and str(t) not in allowed


async def get_company_state_at_date(
    *,
    user_id: str,
    date: str,
    entity_types: Optional[List[str]] = None,
    limit: int = DEFAULT_STATE_LIMIT,
    allowed_tenants: Optional[set] = None,
) -> Dict[str, Any]:
    """Состояние компании на дату: последняя версия каждой сущности на этот
    момент, сгруппированная по типу.

    Возвращает:
      {
        "date": "2026-03-15",
        "counts": {"person": 12, "project": 4, ...},
        "entities": {"person": [{entity_id, name, snapshot, version, as_of}, ...]},
        "total": 16,
        "scanned": 40,          # сколько сущностей вообще просмотрено
        "truncated": false,     # упёрлись ли в limit
      }

    «Ещё не существовало на эту дату» и «существует, но пустое» — разные
    случаи: первые в срез не попадают вовсе, и их видно по разнице
    scanned/total.
    """
    if not date or not str(date).strip():
        return {"error": "date parameter is empty"}

    try:
        from backend.core.temporal.temporal_tracker import get_temporal_tracker
        tracker = get_temporal_tracker(user_id)
    except Exception as e:
        logger.error("temporal_tracker init failed: %s", e)
        return {"error": f"tracker unavailable: {e}"}

    wanted = {t.strip() for t in entity_types if t and t.strip()} if entity_types else None

    try:
        listed = await tracker.list_entities(limit=max(int(limit), 1))
    except Exception as e:
        logger.error("list_entities failed: %s", e)
        return {"error": f"list_entities failed: {e}"}

    entities: Dict[str, List[Dict[str, Any]]] = {}
    scanned = 0
    total = 0
    for ent in listed:
        etype = ent.get("entity_type") or "unknown"
        if wanted and etype not in wanted:
            continue
        scanned += 1
        eid = ent.get("entity_id")
        if not eid:
            continue
        try:
            version = await tracker.get_version_at_date(eid, date)
        except Exception as e:
            logger.warning("get_version_at_date failed (%s): %s", eid, e)
            continue
        if not version:
            # Сущности на эту дату ещё не существовало — это не ошибка.
            continue
        payload = version.get("data") or {}
        if _is_foreign(payload, allowed_tenants):
            continue
        entities.setdefault(etype, []).append({
            "entity_id": eid,
            "name": payload.get("name") or payload.get("title")
            or payload.get("summary") or eid,
            "snapshot": payload,
            "version": version.get("version"),
            "as_of": version.get("timestamp"),
        })
        total += 1

    return {
        "date": date,
        "user_id": user_id,
        "counts": {k: len(v) for k, v in sorted(entities.items())},
        "entities": entities,
        "total": total,
        "scanned": scanned,
        "truncated": len(listed) >= int(limit),
    }


def diff_company_states(
    before: Dict[str, Any],
    after: Dict[str, Any],
) -> Dict[str, Any]:
    """Разница между двумя срезами компании — по сущностям, а не по полям.

    appeared — были заведены между датами; disappeared — существовали
    раньше, но на поздней дате версии нет (редкий случай: удаление/
    tombstone); changed — снапшот объекта отличается, с полевым диффом.
    """
    def _index(state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for etype, items in (state.get("entities") or {}).items():
            for it in items:
                out[f"{etype}:{it['entity_id']}"] = it
        return out

    b, a = _index(before), _index(after)
    keys_b, keys_a = set(b), set(a)

    appeared = [a[k] for k in sorted(keys_a - keys_b)]
    disappeared = [b[k] for k in sorted(keys_b - keys_a)]
    changed = []
    for k in sorted(keys_a & keys_b):
        delta = diff_snapshots(b[k].get("snapshot"), a[k].get("snapshot"))
        if delta.get("added") or delta.get("removed") or delta.get("changed"):
            changed.append({
                "entity_id": a[k]["entity_id"],
                "name": a[k].get("name"),
                "delta": delta,
            })

    return {
        "from_date": before.get("date"),
        "to_date": after.get("date"),
        "appeared": appeared,
        "disappeared": disappeared,
        "changed": changed,
        "summary": (
            f"появилось {len(appeared)}, изменилось {len(changed)}, "
            f"исчезло {len(disappeared)}"
        ),
    }


async def compare_company_states(
    *,
    user_id: str,
    dates: List[str],
    entity_types: Optional[List[str]] = None,
    limit: int = DEFAULT_STATE_LIMIT,
    allowed_tenants: Optional[set] = None,
) -> Dict[str, Any]:
    """Несколько срезов компании рядом + разница между соседними.

    Это то, что бизнес называет «как было на 15 марта, на 1 мая и сегодня».
    """
    if not dates:
        return {"error": "dates parameter is empty"}
    dates_sorted = sorted(set(d.strip() for d in dates if d and d.strip()))
    if not dates_sorted:
        return {"error": "dates parameter is empty"}

    states = []
    for d in dates_sorted:
        st = await get_company_state_at_date(
            user_id=user_id, date=d, entity_types=entity_types,
            limit=limit, allowed_tenants=allowed_tenants,
        )
        if "error" in st:
            return st
        states.append(st)

    deltas = [diff_company_states(states[i], states[i + 1])
              for i in range(len(states) - 1)]

    parts = [f"{len(states)} срез(ов) компании."]
    for d in deltas:
        parts.append(f"{d['from_date']} → {d['to_date']}: {d['summary']}.")

    return {
        "user_id": user_id,
        "dates": dates_sorted,
        "states": states,
        "deltas": deltas,
        "narrative": " ".join(parts),
    }
