# -*- coding: utf-8 -*-
"""Grant-fallback хелперы для SIMA-ресурсов (P1 мульти-аккаунта).

Вынесены из routes/sima.py, чтобы (а) логика тестировалась без тяжёлой
цепочки импортов роутов, (б) паттерн переиспользовался другими ресурсами.
Семантика: сначала обычный owner-путь; не-владельцу — проверка гранта
resource_permissions; write-права применяются ОТ ИМЕНИ владельца (грант
выдал он, аудит — в permission_audit)."""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


async def update_project_with_grant(db: Any, user_id: str, project_id: str,
                                    data: dict) -> dict:
    """PATCH проекта: владелец — напрямую; write-грант — от имени владельца.
    Иначе {"error": "Project not found"} (невидимое = несуществующее)."""
    result = await db.update_project(project_id, user_id, data)
    if result:
        return result
    try:
        from backend.core.auth.resource_permissions import check
        owner_project = await db.get_project(project_id)
        if owner_project:
            owner_id = str(owner_project.get("userId") or "")
            res = await check(user_id, "write", resource_type="project",
                              resource_id=project_id, owner_id=owner_id)
            if res["allowed"] and owner_id:
                result = await db.update_project(project_id, owner_id, data)
                if result:
                    result["access_via"] = res["via"]
                    return result
    except Exception:
        logger.debug("update_project grant check skipped", exc_info=True)
    return {"error": "Project not found"}


async def artifact_with_grant(db: Any, user_id: str,
                              artifact_id: str) -> Optional[dict]:
    """Чтение артефакта: владелец — напрямую; read-грант — с пометкой
    access_via. None если нет ни владения, ни гранта."""
    artifact = await db.get_artifact(artifact_id, user_id)
    if artifact:
        return artifact
    try:
        from backend.core.auth.resource_permissions import check
        owner_artifact = await db.get_artifact(artifact_id)
        if owner_artifact:
            res = await check(user_id, "read", resource_type="artifact",
                              resource_id=artifact_id,
                              owner_id=str(owner_artifact.get("userId") or ""))
            if res["allowed"]:
                owner_artifact["access_via"] = res["via"]
                return owner_artifact
    except Exception:
        logger.debug("artifact grant check skipped", exc_info=True)
    return None
