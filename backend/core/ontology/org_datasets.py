# -*- coding: utf-8 -*-
"""
Org-shared датасеты с группами доступа (Glean-перенос №5 +
роадмап DATA_ONTOLOGY «org-shared row-level»).

Идея: пользователь делится датасетом с организацией — копия уходит в
org-пространство (data/orgs/{org}/datasets/) с access_groups; коллеги
видят его в своей галерее и спрашивают цифрами, НЕ видя личных датасетов
друг друга.

БЕЗОПАСНОСТЬ ПО ПОСТРОЕНИЮ (требование «не сломать данные»):
- личный реестр пользователя НЕ изменяется при шаринге (share = копия);
- членство в org и группы — из существующего user_org_mapping.json
  (источник правды не дублируем);
- видимость: access_groups пуст → вся организация; иначе пересечение
  с группами пользователя (role/access_groups из mapping);
- не член org / не в группе → датасет невидим и неспрашиваем (403-стиль).

Честное отличие от Glean: это ГРУППОВЫЕ права уровня организации, а не
зеркалирование ACL систем-источников — то отдельный дальний пункт.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
from typing import List, Optional

from backend.core.ontology.dataset_registry import DatasetRegistry

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


# ──────────────────────────────────────────────────────────────────────
# Членство (читаем существующий mapping, не плодим источников правды)
# ──────────────────────────────────────────────────────────────────────

def _load_mapping(mapping_path: Optional[str] = None) -> dict:
    if mapping_path is None:
        from backend.core.store.tenant_paths import user_org_mapping_path
        mapping_path = user_org_mapping_path()
    try:
        with open(mapping_path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def user_membership(user_id: str,
                    mapping_path: Optional[str] = None) -> Optional[dict]:
    """{org_id, role, groups} пользователя или None (не в организации)."""
    rec = _load_mapping(mapping_path).get(str(user_id))
    if not rec or not rec.get("org_id"):
        return None
    groups = set(rec.get("access_groups") or [])
    if rec.get("role"):
        groups.add(str(rec["role"]))
    return {"org_id": str(rec["org_id"]), "role": rec.get("role"),
            "groups": groups}


def can_see(dataset_rec: dict, membership: Optional[dict]) -> bool:
    """Видимость org-датасета: member org'а И (групп нет → все;
    есть → пересечение с группами пользователя)."""
    if not membership:
        return False
    groups = set(dataset_rec.get("access_groups") or [])
    if not groups:
        return True
    return bool(groups & membership["groups"])


# ──────────────────────────────────────────────────────────────────────
# Org-реестр (тот же DatasetRegistry, другой путь)
# ──────────────────────────────────────────────────────────────────────

def org_registry(org_id: str) -> DatasetRegistry:
    from backend.core.store.tenant_paths import org_graph_dir
    d = org_graph_dir(org_id) / "datasets"
    d.mkdir(parents=True, exist_ok=True)
    return DatasetRegistry(str(d / "index.json"))


def share_dataset(user_id: str, dataset_id: str, *,
                  access_groups: Optional[List[str]] = None,
                  mapping_path: Optional[str] = None) -> dict:
    """Поделиться личным датасетом с организацией (КОПИЯ: личный реестр
    не изменяется). access_groups=None/[] → видно всей организации."""
    membership = user_membership(user_id, mapping_path)
    if not membership:
        return {"success": False,
                "error": "пользователь не состоит в организации"}
    from backend.core.ontology.dataset_service import registry_for_user
    personal = registry_for_user(user_id)
    rec = personal.get(dataset_id)
    if not rec:
        return {"success": False, "error": "датасет не найден"}
    rows = personal.load_rows(dataset_id)

    oreg = org_registry(membership["org_id"])
    shared = oreg.register(
        title=rec.get("title", ""),
        columns=rec.get("columns") or [],
        rows=rows,
        source=dict(rec.get("source") or {"kind": "inline"}),
        ontology=rec.get("ontology") or {})
    # org-метаданные (access_groups, кто и когда поделился)
    oreg.update(shared["dataset_id"], {
        "access_groups": [str(g) for g in (access_groups or [])],
        "shared_by": user_id,
        "shared_at": _now_iso(),
        "origin_dataset_id": dataset_id,
    })
    return {"success": True, "org_id": membership["org_id"],
            "org_dataset_id": shared["dataset_id"],
            "access_groups": access_groups or [],
            "visible_to": ("вся организация" if not access_groups
                           else f"группы: {', '.join(access_groups)}")}


def list_visible_org_datasets(user_id: str,
                              mapping_path: Optional[str] = None
                              ) -> List[dict]:
    """Org-датасеты, видимые пользователю (членство + группы)."""
    membership = user_membership(user_id, mapping_path)
    if not membership:
        return []
    out = []
    oreg = org_registry(membership["org_id"])
    for slim in oreg.list():
        full = oreg.get(slim["dataset_id"]) or {}
        if can_see(full, membership):
            slim["scope"] = "org"
            slim["shared_by"] = full.get("shared_by")
            slim["access_groups"] = full.get("access_groups") or []
            out.append(slim)
    return out


async def ask_org_dataset(user_id: str, org_dataset_id: str, question: str,
                          mapping_path: Optional[str] = None) -> dict:
    """Вопрос цифрами к org-датасету С ПРОВЕРКОЙ ПРАВ. Невидимый
    датасет неотличим от несуществующего (не раскрываем наличие)."""
    membership = user_membership(user_id, mapping_path)
    if not membership:
        return {"success": False, "error": "датасет не найден"}
    oreg = org_registry(membership["org_id"])
    rec = oreg.get(org_dataset_id)
    if not rec or not can_see(rec, membership):
        return {"success": False, "error": "датасет не найден"}

    # тот же конвейер план→код→оценка, но против org-реестра
    # (registry-параметр, без глобальных подмен — concurrency-safe)
    from backend.core.ontology.dataset_service import ask_dataset
    return await ask_dataset(user_id, org_dataset_id, question,
                             registry=oreg)
