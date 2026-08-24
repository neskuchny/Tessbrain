# -*- coding: utf-8 -*-
"""Хранилище слоя агентов: реестр и задачи, per-организация.

В отличие от федеративных связей (объект двух сторон → файл на
развёртывание) агент и его задачи принадлежат ОДНОЙ организации — той,
что его зарегистрировала. Поэтому файл на организацию, как у остальных
per-tenant примитивов.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from backend.core.data_bus.agent_layer import AgentTask, ExternalAgent

logger = logging.getLogger(__name__)


def _path(org_id: str) -> str:
    try:
        from backend.core.store.tenant_paths import _DATA_ROOT
        root = str(_DATA_ROOT)
    except Exception:
        root = os.getenv("TESSENT_DATA_DIR", "data")
    safe = "".join(ch for ch in str(org_id) if ch.isalnum() or ch in "-_")
    return os.path.join(root, "agent_layer", f"{safe or 'default'}.json")


class AgentLayerStore:
    def __init__(self, org_id: str, path: Optional[str] = None):
        self._org = str(org_id)
        self._path = path or _path(org_id)

    # ── io ──────────────────────────────────────────────────────────────
    def _load(self) -> Dict[str, list]:
        try:
            if os.path.exists(self._path):
                with open(self._path, encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    return {"agents": list(d.get("agents") or []),
                            "tasks": list(d.get("tasks") or [])}
        except Exception:
            logger.warning("agent layer store load failed", exc_info=True)
        return {"agents": [], "tasks": []}

    def _write(self, data: Dict[str, list]) -> None:
        from backend.core.store.tenant_io import atomic_write_json
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        atomic_write_json(self._path, data)

    def _locked(self, fn):
        from backend.core.store.tenant_io import file_lock
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        with file_lock(self._path):
            return fn()

    # ── агенты ──────────────────────────────────────────────────────────
    def list_agents(self, include_retired: bool = False) -> List[dict]:
        out = self._load()["agents"]
        if not include_retired:
            out = [a for a in out if a.get("status") != "retired"]
        return sorted(out, key=lambda a: a.get("created_at", ""), reverse=True)

    def get_agent(self, agent_id: str) -> Optional[ExternalAgent]:
        for raw in self._load()["agents"]:
            if raw.get("id") == agent_id:
                return ExternalAgent.from_dict(raw)
        return None

    def find_agent_by_channel(self, channel_kind: str,
                              channel_id: str) -> Optional[ExternalAgent]:
        """Агент по его каналу — так внешняя сторона опознаётся по ключу."""
        for raw in self._load()["agents"]:
            if (raw.get("channel_kind") == channel_kind
                    and raw.get("channel_id") == channel_id
                    and raw.get("status") == "active"):
                return ExternalAgent.from_dict(raw)
        return None

    def save_agent(self, agent: ExternalAgent) -> Dict[str, Any]:
        def _impl():
            data = self._load()
            for i, raw in enumerate(data["agents"]):
                if raw.get("id") == agent.id:
                    data["agents"][i] = agent.to_dict()
                    self._write(data)
                    return {"ok": True, "agent": agent.to_dict()}
            data["agents"].append(agent.to_dict())
            self._write(data)
            return {"ok": True, "agent": agent.to_dict()}
        return self._locked(_impl)

    # ── задачи ──────────────────────────────────────────────────────────
    def list_tasks(self, *, agent_id: Optional[str] = None,
                   status: Optional[str] = None) -> List[dict]:
        out = self._load()["tasks"]
        if agent_id:
            out = [t for t in out if t.get("agent_id") == agent_id]
        if status:
            out = [t for t in out if t.get("status") == status]
        return sorted(out, key=lambda t: t.get("created_at", ""), reverse=True)

    def get_task(self, task_id: str) -> Optional[AgentTask]:
        for raw in self._load()["tasks"]:
            if raw.get("id") == task_id:
                return AgentTask.from_dict(raw)
        return None

    def save_task(self, task: AgentTask) -> Dict[str, Any]:
        def _impl():
            data = self._load()
            for i, raw in enumerate(data["tasks"]):
                if raw.get("id") == task.id:
                    data["tasks"][i] = task.to_dict()
                    self._write(data)
                    return {"ok": True, "task": task.to_dict()}
            data["tasks"].append(task.to_dict())
            data["tasks"] = data["tasks"][-500:]
            self._write(data)
            return {"ok": True, "task": task.to_dict()}
        return self._locked(_impl)
