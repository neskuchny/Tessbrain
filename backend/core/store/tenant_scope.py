# -*- coding: utf-8 -*-
"""Свои tenant-ы для read-фильтров общего графа (анти-протечка).

Neo4j — ОБЩИЙ на все аккаунты; узлы штампуются tenant_id=user_id создателя
(create_node setdefault), а внутри организации мозг общий. Поэтому «свой»
tenant для чтения — не один id, а множество: сам пользователь, его организации
и коллеги по этим организациям. NULL/'' (legacy до multi-tenant) множеством не
покрывается — это решает вызывающий код (permissive-ветка как в
_tenant_cypher_clause).

Появился из P0-инцидента: Person чужого тенанта попала в панель «Эволюция
объекта» — резолвер сущностей матчил по имени по всему общему графу.
"""
from __future__ import annotations

from typing import Any, Optional, Set


def allowed_tenants(user_id: Optional[str]) -> Set[str]:
    """Множество своих tenant-ов: user_id + его организации + их участники.

    Пустое множество только при пустом user_id. Never-raise."""
    uid = str(user_id or "").strip()
    if not uid:
        return set()
    allowed: Set[str] = {uid}
    try:
        from backend.core.ingest.membership import (
            get_orgs_for_user,
            list_members,
        )
        for m in get_orgs_for_user(uid):
            org = str(m.get("org_id") or "").strip()
            if not org:
                continue
            allowed.add(org)
            try:
                for member in list_members(org):
                    mu = str(member.get("user_id") or "").strip()
                    if mu:
                        allowed.add(mu)
            except Exception:
                pass
    except Exception:
        pass
    return allowed


def is_own_tenant(value: Any, allowed: Set[str]) -> bool:
    """True, если штамп tenant_id свой или legacy (None/'' — до multi-tenant).

    Permissive по legacy — согласовано с _tenant_cypher_clause по умолчанию:
    старые узлы без штампа не должны молча пропасть."""
    if value in (None, ""):
        return True
    return str(value) in allowed


def canonical_tenant(user_id: Optional[str]) -> str:
    """Канонический tenant хранения: org пользователя или сам user_id.

    Согласован с get_temporal_tracker (data/temporal/{tenant}.db) и векторным
    хранилищем."""
    uid = str(user_id or "").strip()
    if not uid:
        return uid
    try:
        from backend.core.ingest.membership import get_org_for_user
        return get_org_for_user(uid) or uid
    except Exception:
        return uid


__all__ = ["allowed_tenants", "is_own_tenant", "canonical_tenant"]
