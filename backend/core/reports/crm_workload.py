# -*- coding: utf-8 -*-
"""CRM-нагрузка сотрудника: маппинг owner_id → человек + агрегация.

Закрывает разрыв из аудита «отчёт по сотруднику»: CRM-коннекторы
(AmoCRM/Bitrix24/HubSpot/Pipedrive) отдают сделки/задачи с owner_id, но
никто не знал, ЧЕЙ это owner_id. Здесь:
1) per-user маппинг {provider: {owner_id: имя человека}} — маленький JSON
   в data/crm_owner_map/ (как остальные per-user сторы), задаётся ручкой;
2) агрегация по человеку: открытые/закрытые сделки, сумма, просроченные
   CRM-задачи — из ВСЕХ настроенных провайдеров (env_check пройден).

Колонки у провайдеров разные → generic-детект по известным именам
(owner/сумма/статус/дедлайн), а не копия логики каждого API. Строки без
распознанного owner честно пропускаются. Never-raise: CRM недоступна →
пустой результат с note, отчёт сотрудника не падает.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# generic-детект колонок (по провайдерам: amocrm/bitrix24/hubspot/pipedrive)
_OWNER_COLS = ("responsible_user_id", "ASSIGNED_BY_ID", "hubspot_owner_id",
               "owner_id", "user_id")
_VALUE_COLS = ("price", "OPPORTUNITY", "amount", "value")
_DONE_FLAGS = ("is_completed", "done", "CLOSED")
_DUE_COLS = ("complete_till", "due_date", "CLOSEDATE")
# «сделочные» и «задачные» деки по провайдерам
_DEAL_KINDS = ("leads", "deals")
_TASK_KINDS = ("tasks", "activities")


def _map_dir() -> Path:
    from backend.core.store.tenant_paths import _DATA_ROOT  # тот же корень
    d = _DATA_ROOT / "crm_owner_map"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _map_path(user_id: str) -> Path:
    from backend.core.store.tenant_paths import _require_uuid
    return _map_dir() / f"{_require_uuid(user_id, 'user_id')}.json"


def get_owner_map(user_id: str) -> Dict[str, Dict[str, str]]:
    """{provider: {owner_id: person_name}} — пусто, если не настраивали."""
    try:
        p = _map_path(user_id)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.debug("crm owner map read failed", exc_info=True)
    return {}


def set_owner_mapping(user_id: str, provider: str, owner_id: str,
                      person: str) -> Dict[str, Any]:
    """Привязать owner_id провайдера к человеку. person="" → отвязать."""
    provider = (provider or "").strip().lower()
    owner_id = str(owner_id or "").strip()
    person = (person or "").strip()
    if not provider or not owner_id:
        return {"status": "error", "message": "provider и owner_id обязательны"}
    data = get_owner_map(user_id)
    prov = data.setdefault(provider, {})
    if person:
        prov[owner_id] = person
    else:
        prov.pop(owner_id, None)
    try:
        p = _map_path(user_id)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    except Exception as e:
        return {"status": "error", "message": str(e)}
    return {"status": "success", "map": data}


def _pick(row: Dict[str, Any], cols) -> Any:
    for c in cols:
        if c in row and row[c] not in (None, ""):
            return row[c]
    return None


def _is_done(row: Dict[str, Any]) -> Optional[bool]:
    for c in _DONE_FLAGS:
        if c in row:
            v = row[c]
            if isinstance(v, bool):
                return v
            return str(v).strip().lower() in ("1", "true", "y", "yes")
    return None


def _due_date(row: Dict[str, Any]) -> Optional[date]:
    v = _pick(row, _DUE_COLS)
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)) or str(v).isdigit():  # unix ts (amocrm)
            return datetime.fromtimestamp(int(v)).date()
        return datetime.fromisoformat(str(v)[:10]).date()
    except (ValueError, OSError, OverflowError):
        return None


async def crm_workload_for_person(user_id: str, person: str) -> Dict[str, Any]:
    """Сводка CRM-нагрузки человека по всем настроенным провайдерам."""
    from backend.core.ontology.crm_connectors import get_connector, list_providers

    owner_map = get_owner_map(user_id)
    p_low = (person or "").strip().lower()
    # owner_id этого человека по провайдерам (двусторонний матч имени —
    # как в задачах отчёта)
    my_owners: Dict[str, set] = {}
    for prov, m in owner_map.items():
        ids = {oid for oid, name in m.items()
               if p_low and (p_low in str(name).lower()
                             or str(name).lower() in p_low)}
        if ids:
            my_owners[prov] = ids

    out: Dict[str, Any] = {"providers": [], "deals_open": 0, "deals_done": 0,
                           "deals_value": 0.0, "crm_tasks_open": 0,
                           "crm_tasks_overdue": 0, "notes": []}
    if not my_owners:
        out["notes"].append(
            "маппинг owner→человек не настроен (POST /crm-owner-map)")
        return out

    today = date.today()
    for prov_info in list_providers():
        prov = prov_info["provider"]
        if prov not in my_owners:
            continue
        if not prov_info.get("ready"):
            out["notes"].append(f"{prov}: {prov_info.get('env_hint')}")
            continue
        conn = get_connector(prov)
        ids = {str(i) for i in my_owners[prov]}
        stats = {"provider": prov, "deals_open": 0, "deals_done": 0,
                 "deals_value": 0.0, "tasks_open": 0, "tasks_overdue": 0}
        for kind in conn.kinds:
            if kind not in _DEAL_KINDS + _TASK_KINDS:
                continue
            try:
                _cols, rows = await conn.fetch(kind)
            except Exception as e:
                out["notes"].append(f"{prov}/{kind}: {e}")
                continue
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                owner = _pick(row, _OWNER_COLS)
                if owner is None or str(owner) not in ids:
                    continue
                done = _is_done(row)
                if kind in _DEAL_KINDS:
                    if done:
                        stats["deals_done"] += 1
                    else:
                        stats["deals_open"] += 1
                    try:
                        stats["deals_value"] += float(
                            _pick(row, _VALUE_COLS) or 0)
                    except (TypeError, ValueError):
                        pass
                else:  # задачи/активности CRM
                    if done:
                        continue
                    stats["tasks_open"] += 1
                    dd = _due_date(row)
                    if dd and dd < today:
                        stats["tasks_overdue"] += 1
        out["providers"].append(stats)
        out["deals_open"] += stats["deals_open"]
        out["deals_done"] += stats["deals_done"]
        out["deals_value"] += stats["deals_value"]
        out["crm_tasks_open"] += stats["tasks_open"]
        out["crm_tasks_overdue"] += stats["tasks_overdue"]
    return out
