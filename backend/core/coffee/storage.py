"""Persistent storage для Coffee artifacts (Phase B.2 final).

Coffee orchestrator (orchestrator.py) генерирует RolePipelineOutput'ы и
шлёт через delivery.py. Этот модуль добавляет persistent layer:

- save_artifact(output, meeting_id, ...) → artifact_id
  При re-generation помечает прошлые pending как superseded.
- list_artifacts(meeting_id=..., user_id=..., status=...) → list
  UI/Mini Tess читают историю через GET /coffee/artifacts/{meeting_id}.
- update_delivery_status(artifact_id, channel, external_ref, error?)
  После delivery.deliver_artifact orchestrator зовёт сюда чтобы
  закрепить факт доставки.

Все методы best-effort. При недоступности БД молча возвращают
None/[]/False — Coffee scenario продолжает работать как раньше.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _pg_text_array(items: Any) -> str:
    """Audit-фикс: безопасный Postgres text[] literal.

    Каждый элемент оборачивается в двойные кавычки, внутри escaping
    `\\` и `"`. Пустой список → '{}' (валидный пустой array). Без этого
    запятая/quote в элементе ломали бы парсер.
    """
    out = []
    for c in items:
        s = str(c)
        s = s.replace("\\", "\\\\").replace('"', '\\"')
        out.append(f'"{s}"')
    return "{" + ",".join(out) + "}"


async def save_artifact(
    *,
    pipeline_output: Any,
    meeting_id: str,
    user_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> Optional[str]:
    """Сохранить артефакт в БД, помечая прошлые этого же типа superseded.

    Args:
        pipeline_output: RolePipelineOutput
        meeting_id: к какой встрече привязано
        user_id: override из pipeline_output.participant_id
        tenant_id: optional, для RLS совместимости

    Returns:
        artifact_id (UUID string) или None при ошибке.
    """
    try:
        from backend.db.postgres import get_postgres
        from sqlalchemy import text
        pg = await get_postgres()

        uid = user_id or getattr(pipeline_output, "participant_id", None) or ""
        if not uid:
            return None
        tid = tenant_id or uid

        artifact_id = str(uuid.uuid4())
        role_val = getattr(pipeline_output, "role", None)
        role_str = role_val.value if hasattr(role_val, "value") else str(role_val or "other")

        content_structured = getattr(pipeline_output, "content_structured", {}) or {}
        if not isinstance(content_structured, dict):
            content_structured = {"raw": content_structured}

        async with pg.session(apply_tenant=False) as session:
            # 1. Помечаем прошлые pending этого типа superseded
            # Audit-фикс: asyncpg строг к UUID — CAST на все UUID-параметры,
            # как в INSERT ниже и во всех sibling-модулях.
            await session.execute(
                text("""
                    UPDATE public.coffee_artifacts
                    SET status = 'superseded',
                        superseded_by = CAST(:new_id AS UUID)
                    WHERE meeting_id = CAST(:mid AS UUID)
                      AND user_id = CAST(:uid AS UUID)
                      AND artifact_type = :atype
                      AND status = 'pending'
                """),
                {
                    "new_id": artifact_id,
                    "mid": meeting_id,
                    "uid": uid,
                    "atype": getattr(pipeline_output, "artifact_type", ""),
                },
            )

            # 2. Вставляем новый артефакт
            await session.execute(
                text("""
                    INSERT INTO public.coffee_artifacts
                        (id, user_id, tenant_id, meeting_id, role, artifact_type,
                         title, content_markdown, content_structured,
                         generation_duration_ms, generation_cost_usd,
                         recommended_channels, recommended_executor, status)
                    VALUES
                        (CAST(:aid AS UUID), CAST(:uid AS UUID),
                         CAST(:tid AS UUID), CAST(:mid AS UUID),
                         :role, :atype, :title, :md, CAST(:struct AS JSONB),
                         :dur_ms, :cost, CAST(:channels AS TEXT[]),
                         :executor, 'pending')
                """),
                {
                    "aid": artifact_id,
                    "uid": uid,
                    "tid": tid,
                    "mid": meeting_id,
                    "role": role_str,
                    "atype": getattr(pipeline_output, "artifact_type", ""),
                    "title": getattr(pipeline_output, "title", "") or "Артефакт",
                    "md": getattr(pipeline_output, "content_markdown", "") or "",
                    "struct": json.dumps(content_structured, ensure_ascii=False, default=str),
                    "dur_ms": int(getattr(pipeline_output, "generation_duration_ms", 0)),
                    "cost": float(getattr(pipeline_output, "generation_cost_usd", 0.0)),
                    # Audit-фикс: безопасный array literal с экранированием.
                    # Старый "{" + ",".join(str(c)) + "}" корраптится если
                    # элемент содержит запятую/кавычку/обратный слэш.
                    "channels": _pg_text_array(
                        getattr(pipeline_output, "recommended_channels", []) or []
                    ),
                    "executor": getattr(pipeline_output, "recommended_executor", None),
                },
            )
        return artifact_id
    except Exception as exc:
        logger.warning("save_artifact failed for meeting=%s: %s", meeting_id, exc)
        return None


async def update_delivery_status(
    *,
    artifact_id: str,
    delivered_channel: Optional[str] = None,
    delivered_external_ref: Optional[str] = None,
    success: bool = True,
    error_message: Optional[str] = None,
) -> bool:
    """После delivery.deliver_artifact зафиксировать факт.

    success=True → status='delivered', delivered_channel/at/external_ref заполнены
    success=False → status='failed', delivery_attempts++, last_delivery_error
    """
    try:
        from backend.db.postgres import get_postgres
        from sqlalchemy import text
        pg = await get_postgres()
        if success:
            sql = """
                UPDATE public.coffee_artifacts
                SET status = 'delivered',
                    delivered_channel = :ch,
                    delivered_at = now(),
                    delivered_external_ref = :ref,
                    delivery_attempts = delivery_attempts + 1
                WHERE id = CAST(:aid AS UUID)
            """
            params = {"aid": artifact_id, "ch": delivered_channel, "ref": delivered_external_ref}
        else:
            # Audit-фикс: оба SET-expression'а оцениваются против OLD строки.
            # Старая формула `delivery_attempts >= 3` срабатывает только на 4-й
            # неудаче. Сравниваем `(delivery_attempts + 1) >= 3` — это номер
            # ТЕКУЩЕЙ попытки, так что 3-я неудача переводит в 'failed'.
            sql = """
                UPDATE public.coffee_artifacts
                SET status = CASE WHEN (delivery_attempts + 1) >= 3 THEN 'failed'
                                  ELSE 'pending' END,
                    delivery_attempts = delivery_attempts + 1,
                    last_delivery_error = :err
                WHERE id = CAST(:aid AS UUID)
            """
            params = {"aid": artifact_id, "err": (error_message or "")[:1000]}
        async with pg.session(apply_tenant=False) as session:
            await session.execute(text(sql), params)
        return True
    except Exception as exc:
        logger.warning("update_delivery_status failed for %s: %s", artifact_id, exc)
        return False


async def get_artifact(artifact_id: str) -> Optional[dict[str, Any]]:
    """Audit-фикс: точечная выборка одного артефакта по id.

    Используется drain_runner — до фикса он сканировал list_artifacts
    с capped limit=200, и для активных юзеров (>200 артефактов) старые
    parked-сигналы переставали находить свой артефакт. Возвращает None
    если не существует. Best-effort.
    """
    try:
        from backend.db.postgres import get_postgres
        from sqlalchemy import text
        pg = await get_postgres()
        async with pg.session(apply_tenant=False) as session:
            r = await session.execute(
                text("""
                    SELECT id, user_id, meeting_id, role, artifact_type, title,
                           content_markdown, content_structured,
                           generated_at, generation_duration_ms,
                           recommended_channels, recommended_executor,
                           delivered_channel, delivered_at, delivered_external_ref,
                           delivery_attempts, last_delivery_error, status
                    FROM public.coffee_artifacts
                    WHERE id = CAST(:aid AS UUID)
                    LIMIT 1
                """),
                {"aid": artifact_id},
            )
            row = r.first()
        if not row:
            return None
        structured = row[7]
        if isinstance(structured, str):
            try:
                structured = json.loads(structured)
            except Exception:
                structured = {}
        return {
            "id": str(row[0]),
            "user_id": str(row[1]),
            "meeting_id": str(row[2]),
            "role": row[3],
            "artifact_type": row[4],
            "title": row[5],
            "content_markdown": row[6],
            "content_structured": structured or {},
            "generated_at": row[8].isoformat() if row[8] else None,
            "generation_duration_ms": int(row[9] or 0),
            "recommended_channels": list(row[10] or []),
            "recommended_executor": row[11],
            "delivered_channel": row[12],
            "delivered_at": row[13].isoformat() if row[13] else None,
            "delivered_external_ref": row[14],
            "delivery_attempts": int(row[15] or 0),
            "last_delivery_error": row[16],
            "status": row[17],
        }
    except Exception as exc:
        logger.warning("get_artifact failed for %s: %s", artifact_id, exc)
        return None


async def list_artifacts(
    *,
    meeting_id: Optional[str] = None,
    user_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Список артефактов для UI.

    Любая комбинация фильтров. Возвращает в обратном хронологическом порядке.
    """
    try:
        from backend.db.postgres import get_postgres
        from sqlalchemy import text
        pg = await get_postgres()

        conditions: list[str] = []
        params: dict[str, Any] = {"lim": int(limit)}
        if meeting_id:
            conditions.append("meeting_id = CAST(:mid AS UUID)")
            params["mid"] = meeting_id
        if user_id:
            conditions.append("user_id = CAST(:uid AS UUID)")
            params["uid"] = user_id
        if status:
            conditions.append("status = :st")
            params["st"] = status

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = f"""
            SELECT id, user_id, meeting_id, role, artifact_type, title,
                   content_markdown, content_structured,
                   generated_at, generation_duration_ms,
                   recommended_channels, recommended_executor,
                   delivered_channel, delivered_at, delivered_external_ref,
                   delivery_attempts, last_delivery_error, status
            FROM public.coffee_artifacts
            {where}
            ORDER BY generated_at DESC
            LIMIT :lim
        """
        async with pg.session(apply_tenant=False) as session:
            rows = await session.execute(text(sql), params)
            out: list[dict[str, Any]] = []
            for r in rows:
                structured = r[7]
                if isinstance(structured, str):
                    try:
                        structured = json.loads(structured)
                    except Exception:
                        structured = {}
                out.append({
                    "id": str(r[0]),
                    "user_id": str(r[1]),
                    "meeting_id": str(r[2]),
                    "role": r[3],
                    "artifact_type": r[4],
                    "title": r[5],
                    "content_markdown": r[6],
                    "content_structured": structured or {},
                    "generated_at": r[8].isoformat() if r[8] else None,
                    "generation_duration_ms": int(r[9] or 0),
                    "recommended_channels": list(r[10] or []),
                    "recommended_executor": r[11],
                    "delivered_channel": r[12],
                    "delivered_at": r[13].isoformat() if r[13] else None,
                    "delivered_external_ref": r[14],
                    "delivery_attempts": int(r[15] or 0),
                    "last_delivery_error": r[16],
                    "status": r[17],
                })
            return out
    except Exception as exc:
        logger.warning("list_artifacts failed: %s", exc)
        return []


__all__ = ["get_artifact", "list_artifacts", "save_artifact", "update_delivery_status"]
