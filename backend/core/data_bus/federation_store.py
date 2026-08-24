# -*- coding: utf-8 -*-
"""Хранилище федеративных связей между организациями.

Связь — объект уровня развёртывания, а не тенанта: у неё две стороны, и
хранить её «в тенанте A» значило бы, что сторона B видит своё отношение
только с чужих слов. Поэтому один файл на развёртывание, как у реестра
организаций, с файловым локом и атомарной записью.

Инварианты, за которые отвечает именно хранилище (правила переходов — в
federation.py):
  - на пару организаций не больше ОДНОЙ незакрытой связи (предложенной
    или действующей): иначе «какая политика действует» перестаёт иметь
    единственный ответ;
  - закрытые связи (отклонённые и прекращённые) не удаляются — история
    отношений с чужой компанией нужна для аудита.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from backend.core.data_bus.federation import (
    ACTIVE,
    PROPOSED,
    FederationLink,
    pair_key,
)

logger = logging.getLogger(__name__)

_OPEN_STATES = (PROPOSED, ACTIVE)


def _path() -> str:
    """Один файл на развёртывание — рядом с реестром организаций."""
    try:
        from backend.core.store.tenant_paths import _DATA_ROOT
        root = str(_DATA_ROOT)
    except Exception:
        root = os.getenv("TESSENT_DATA_DIR", "data")
    return os.path.join(root, "federation", "links.json")


class FederationStore:
    """Связи всего развёртывания. Read-modify-write под файловым локом."""

    def __init__(self, path: Optional[str] = None):
        self._path = path or _path()

    # ── чтение ──────────────────────────────────────────────────────────
    def _load(self) -> List[dict]:
        try:
            if os.path.exists(self._path):
                with open(self._path, encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    return list(d.get("links") or [])
        except Exception:
            logger.warning("federation store load failed", exc_info=True)
        return []

    def list_for_org(self, org_id: str,
                     include_closed: bool = False) -> List[dict]:
        org = str(org_id or "")
        out = []
        for raw in self._load():
            if org not in (raw.get("org_a"), raw.get("org_b")):
                continue
            if not include_closed and raw.get("status") not in _OPEN_STATES:
                continue
            out.append(raw)
        return sorted(out, key=lambda r: r.get("created_at", ""), reverse=True)

    def get(self, link_id: str) -> Optional[FederationLink]:
        for raw in self._load():
            if raw.get("id") == link_id:
                return FederationLink.from_dict(raw)
        return None

    def open_link_between(self, org_a: str,
                          org_b: str) -> Optional[FederationLink]:
        """Незакрытая связь пары, если есть. Основа для resolve_access."""
        key = pair_key(org_a, org_b)
        for raw in self._load():
            if raw.get("status") not in _OPEN_STATES:
                continue
            if pair_key(raw.get("org_a", ""), raw.get("org_b", "")) == key:
                return FederationLink.from_dict(raw)
        return None

    def active_link_between(self, org_a: str,
                            org_b: str) -> Optional[FederationLink]:
        link = self.open_link_between(org_a, org_b)
        return link if (link and link.status == ACTIVE) else None

    # ── запись ──────────────────────────────────────────────────────────
    def _write(self, links: List[dict]) -> None:
        from backend.core.store.tenant_io import atomic_write_json
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        atomic_write_json(self._path, {"links": links})

    def _locked(self, fn):
        from backend.core.store.tenant_io import file_lock
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        with file_lock(self._path):
            return fn()

    def add(self, link: FederationLink) -> Dict[str, Any]:
        """Сохранить новую связь. Отказ, если пара уже имеет незакрытую."""
        def _impl():
            links = self._load()
            key = pair_key(link.org_a, link.org_b)
            for raw in links:
                if raw.get("status") in _OPEN_STATES and pair_key(
                        raw.get("org_a", ""), raw.get("org_b", "")) == key:
                    return {"ok": False,
                            "error": "между этими организациями уже есть "
                                     "предложенная или действующая связь",
                            "existing_id": raw.get("id")}
            links.append(link.to_dict())
            self._write(links)
            return {"ok": True, "link": link.to_dict()}
        return self._locked(_impl)

    def save(self, link: FederationLink) -> Dict[str, Any]:
        """Перезаписать существующую связь (после смены состояния)."""
        def _impl():
            links = self._load()
            for i, raw in enumerate(links):
                if raw.get("id") == link.id:
                    links[i] = link.to_dict()
                    self._write(links)
                    return {"ok": True, "link": link.to_dict()}
            return {"ok": False, "error": "связь не найдена"}
        return self._locked(_impl)


_store: Optional[FederationStore] = None


def get_federation_store() -> FederationStore:
    global _store
    if _store is None:
        _store = FederationStore()
    return _store
