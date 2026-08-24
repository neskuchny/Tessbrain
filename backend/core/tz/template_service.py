"""TZTemplateService — CRUD над per-tenant tz_templates (W24).

Migration `090_tz_templates.sql` была добавлена в W18 — здесь только
service layer + Postgres bindings. Defaults seed'ятся на лету (не
персистятся в БД), tenant видит default + свои custom через `list()`.

Дизайн:
- Best-effort: если postgres недоступен — list() возвращает только
  defaults; create/update/delete возвращают ошибку с понятным
  message
- Структурная валидация на стороне `tz/templates.py` (W18)
- При `is_default=True` — атомарно сбрасываем флаг у других templates
  того же task_type (один default per (tenant, task_type) — partial
  unique index в миграции 090)
- ID — `tpl_<uuid8>`; генерим в service'е
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from backend.core.tz.templates import (
    DEFAULT_TEMPLATES,
    TemplateValidationError,
    TZTemplate,
    get_default_template,
    list_default_task_types,
)

logger = logging.getLogger(__name__)


def _new_id() -> str:
    return f"tpl_{uuid.uuid4().hex[:16]}"


@dataclass
class StoredTemplate:
    """То что отдаём из БД."""
    id: str
    task_type: str
    name: str
    description: str
    blocks: list[dict[str, Any]]
    is_default: bool
    created_by: Optional[str] = None
    tenant_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @classmethod
    def from_row(cls, row: Any) -> "StoredTemplate":
        get = (row.get if hasattr(row, "get")
               else lambda k, default=None: row[k] if k in row else default)
        blocks = get("blocks") or []
        if isinstance(blocks, str):
            try:
                blocks = json.loads(blocks)
            except json.JSONDecodeError:
                blocks = []
        return cls(
            id=str(get("id")),
            task_type=str(get("task_type") or ""),
            name=str(get("name") or ""),
            description=str(get("description") or ""),
            blocks=list(blocks),
            is_default=bool(get("is_default", False)),
            created_by=get("created_by"),
            tenant_id=get("tenant_id"),
            created_at=str(get("created_at")) if get("created_at") else None,
            updated_at=str(get("updated_at")) if get("updated_at") else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_type": self.task_type,
            "name": self.name,
            "description": self.description,
            "blocks": list(self.blocks),
            "is_default": self.is_default,
            "created_by": self.created_by,
            "tenant_id": self.tenant_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source": "tenant",
        }

    @classmethod
    def from_default_seed(cls, task_type: str) -> Optional["StoredTemplate"]:
        """Wrap built-in default template как StoredTemplate (id="default::task_type")."""
        seed = DEFAULT_TEMPLATES.get(task_type)
        if not seed:
            return None
        return cls(
            id=f"default::{task_type}",
            task_type=task_type,
            name=str(seed.get("name") or task_type),
            description=str(seed.get("description") or ""),
            blocks=list(seed.get("blocks") or []),
            is_default=True,
        )


class TemplateNotFound(LookupError):
    """Template id не найден или принадлежит чужому tenant'у."""


class TemplateConflict(ValueError):
    """Conflict (например, имя уже существует в tenant'е для task_type)."""


_LIST_SQL = """
SELECT id, task_type, name, description, blocks, is_default,
       created_by, tenant_id, created_at, updated_at
FROM public.tz_templates
WHERE 1 = 1
"""

_GET_SQL = """
SELECT id, task_type, name, description, blocks, is_default,
       created_by, tenant_id, created_at, updated_at
FROM public.tz_templates
WHERE id = :id
"""

_INSERT_SQL = """
INSERT INTO public.tz_templates
    (id, task_type, name, description, blocks, is_default, created_by)
VALUES (:id, :task_type, :name, :description, CAST(:blocks AS JSONB),
        :is_default, :created_by)
"""

_UPDATE_SQL_TEMPLATE = """
UPDATE public.tz_templates
   SET {assignments}
 WHERE id = :id
"""

_DELETE_SQL = "DELETE FROM public.tz_templates WHERE id = :id"

# Сброс is_default на всех остальных templates того же task_type.
# RLS гарантирует что это в текущем tenant'е.
_CLEAR_DEFAULT_SQL = """
UPDATE public.tz_templates
   SET is_default = FALSE
 WHERE task_type = :task_type AND id <> :id AND is_default = TRUE
"""


class TZTemplateService:
    """Service над `public.tz_templates`.

    `postgres` — async PostgresClient. `None` → в режиме defaults-only
    (list возвращает seeds, create/update/delete возвращают ошибку).
    """

    def __init__(self, *, postgres: Any = None) -> None:
        self.postgres = postgres

    @property
    def has_db(self) -> bool:
        return self.postgres is not None

    # === Read ============================================================

    async def list(
        self,
        *,
        task_type: Optional[str] = None,
        include_defaults: bool = True,
    ) -> list[StoredTemplate]:
        """Список tenant'овских templates + (опц) defaults.

        Без БД возвращаем только defaults.
        """
        out: list[StoredTemplate] = []

        # Defaults seeds (in-memory).
        if include_defaults:
            for tt in list_default_task_types():
                if task_type and tt != task_type:
                    continue
                seed = StoredTemplate.from_default_seed(tt)
                if seed is not None:
                    out.append(seed)

        # Tenant-specific.
        if self.has_db:
            sql = _LIST_SQL
            params: dict[str, Any] = {}
            if task_type:
                sql += " AND task_type = :task_type"
                params["task_type"] = task_type
            sql += " ORDER BY task_type, name"
            try:
                async with self.postgres.session() as session:
                    result = await session.execute(sql, params)
                    rows = await _all_rows(result)
            except Exception as exc:
                logger.warning("TZTemplateService.list failed: %s", exc)
                rows = []
            out.extend(StoredTemplate.from_row(r) for r in rows)

        return out

    async def get(self, *, template_id: str) -> Optional[StoredTemplate]:
        if not template_id:
            return None
        if template_id.startswith("default::"):
            tt = template_id.split("::", 1)[1]
            return StoredTemplate.from_default_seed(tt)
        if not self.has_db:
            return None
        try:
            async with self.postgres.session() as session:
                result = await session.execute(_GET_SQL, {"id": template_id})
                row = await _first_row(result)
        except Exception as exc:
            logger.warning("TZTemplateService.get failed: %s", exc)
            return None
        return StoredTemplate.from_row(row) if row else None

    async def get_default_for_task_type(
        self,
        *,
        task_type: str,
    ) -> Optional[StoredTemplate]:
        """Resolve default template для task_type.

        Order: tenant's is_default=true → built-in seed → None.
        """
        if self.has_db:
            try:
                async with self.postgres.session() as session:
                    result = await session.execute(
                        _LIST_SQL + " AND task_type = :task_type AND is_default = TRUE LIMIT 1",
                        {"task_type": task_type},
                    )
                    row = await _first_row(result)
            except Exception as exc:
                logger.warning("get_default_for_task_type failed: %s", exc)
                row = None
            if row:
                return StoredTemplate.from_row(row)
        return StoredTemplate.from_default_seed(task_type)

    # === Write ===========================================================

    async def create(
        self,
        *,
        task_type: str,
        name: str,
        blocks: list[dict[str, Any]],
        description: str = "",
        is_default: bool = False,
        created_by: Optional[str] = None,
    ) -> StoredTemplate:
        """Создать custom template."""
        if not self.has_db:
            raise RuntimeError("postgres unavailable; cannot create custom templates")

        # Структурная валидация (W18 helpers).
        TZTemplate.from_dict({
            "task_type": task_type,
            "name": name,
            "description": description,
            "blocks": blocks,
            "is_default": is_default,
        })

        record_id = _new_id()
        params = {
            "id": record_id,
            "task_type": task_type,
            "name": name,
            "description": description,
            "blocks": json.dumps(blocks, ensure_ascii=False),
            "is_default": bool(is_default),
            "created_by": created_by,
        }

        try:
            async with self.postgres.session() as session:
                if is_default:
                    # Атомарно сбрасываем default'ы у других того же task_type.
                    await session.execute(
                        _CLEAR_DEFAULT_SQL,
                        {"task_type": task_type, "id": record_id},
                    )
                await session.execute(_INSERT_SQL, params)
        except Exception as exc:
            msg = str(exc).lower()
            if "unique" in msg or "duplicate" in msg:
                raise TemplateConflict(
                    f"template name {name!r} already exists for task_type "
                    f"{task_type!r} in this tenant"
                ) from exc
            logger.warning("TZTemplateService.create failed: %s", exc)
            raise

        return StoredTemplate(
            id=record_id,
            task_type=task_type,
            name=name,
            description=description,
            blocks=list(blocks),
            is_default=bool(is_default),
            created_by=created_by,
        )

    async def update(
        self,
        *,
        template_id: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        blocks: Optional[list[dict[str, Any]]] = None,
    ) -> StoredTemplate:
        """Update mutable fields. is_default — отдельным методом."""
        if template_id.startswith("default::"):
            raise TemplateConflict("built-in default templates are read-only")
        if not self.has_db:
            raise RuntimeError("postgres unavailable; cannot update")

        existing = await self.get(template_id=template_id)
        if existing is None:
            raise TemplateNotFound(f"template {template_id!r} not found")

        new_blocks = blocks if blocks is not None else existing.blocks
        new_name = name if name is not None else existing.name
        new_description = description if description is not None else existing.description

        # Re-validate.
        TZTemplate.from_dict({
            "task_type": existing.task_type,
            "name": new_name,
            "description": new_description,
            "blocks": new_blocks,
        })

        assignments = []
        params: dict[str, Any] = {"id": template_id}
        if name is not None:
            assignments.append("name = :name")
            params["name"] = new_name
        if description is not None:
            assignments.append("description = :description")
            params["description"] = new_description
        if blocks is not None:
            assignments.append("blocks = CAST(:blocks AS JSONB)")
            params["blocks"] = json.dumps(new_blocks, ensure_ascii=False)

        if not assignments:
            return existing  # nothing to update

        sql = _UPDATE_SQL_TEMPLATE.format(assignments=", ".join(assignments))
        try:
            async with self.postgres.session() as session:
                await session.execute(sql, params)
        except Exception as exc:
            msg = str(exc).lower()
            if "unique" in msg or "duplicate" in msg:
                raise TemplateConflict(
                    "name conflict with existing template in same task_type"
                ) from exc
            logger.warning("TZTemplateService.update failed: %s", exc)
            raise

        existing.name = new_name
        existing.description = new_description
        existing.blocks = list(new_blocks)
        return existing

    async def set_default(self, *, template_id: str) -> StoredTemplate:
        """Сделать template дефолтным для своего task_type.

        Атомарно сбрасывает is_default у других templates того же
        task_type в этом tenant'е.
        """
        if template_id.startswith("default::"):
            raise TemplateConflict("built-in default cannot be overridden via set_default")
        if not self.has_db:
            raise RuntimeError("postgres unavailable; cannot set default")

        existing = await self.get(template_id=template_id)
        if existing is None:
            raise TemplateNotFound(f"template {template_id!r} not found")

        try:
            async with self.postgres.session() as session:
                await session.execute(
                    _CLEAR_DEFAULT_SQL,
                    {"task_type": existing.task_type, "id": template_id},
                )
                await session.execute(
                    "UPDATE public.tz_templates SET is_default = TRUE WHERE id = :id",
                    {"id": template_id},
                )
        except Exception as exc:
            logger.warning("TZTemplateService.set_default failed: %s", exc)
            raise

        existing.is_default = True
        return existing

    async def delete(self, *, template_id: str) -> bool:
        if template_id.startswith("default::"):
            raise TemplateConflict("built-in default templates cannot be deleted")
        if not self.has_db:
            raise RuntimeError("postgres unavailable; cannot delete")

        try:
            async with self.postgres.session() as session:
                await session.execute(_DELETE_SQL, {"id": template_id})
        except Exception as exc:
            logger.warning("TZTemplateService.delete failed: %s", exc)
            return False
        return True


# === Row helpers (compat across asyncpg/sqlalchemy result shapes) =====

async def _first_row(result: Any) -> Optional[Any]:
    if hasattr(result, "first"):
        try:
            row = result.first()
            if hasattr(row, "_mapping"):
                return dict(row._mapping)
            return row
        except Exception:
            pass
    if hasattr(result, "fetchone"):
        try:
            row = await result.fetchone() if hasattr(result.fetchone, "__await__") else result.fetchone()
            return dict(row) if row else None
        except Exception:
            return None
    return None


async def _all_rows(result: Any) -> list[Any]:
    if hasattr(result, "mappings"):
        try:
            return [dict(m) for m in result.mappings().all()]
        except Exception:
            pass
    if hasattr(result, "all"):
        try:
            rows = result.all()
            return [
                dict(r._mapping) if hasattr(r, "_mapping") else dict(r)
                for r in rows
            ]
        except Exception:
            return []
    return []


def reraise_validation(exc: Exception) -> None:
    """Helper для caller'ов: преобразовать TemplateValidationError в HTTP 400."""
    if isinstance(exc, TemplateValidationError):
        raise ValueError(str(exc)) from exc


__all__ = [
    "StoredTemplate",
    "TZTemplateService",
    "TemplateConflict",
    "TemplateNotFound",
]
