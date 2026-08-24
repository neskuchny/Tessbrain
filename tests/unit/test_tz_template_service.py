"""Unit-тесты для core.tz.template_service (W24)."""
from __future__ import annotations

import asyncio

import pytest
from backend.core.tz.template_service import (
    StoredTemplate,
    TemplateConflict,
    TemplateNotFound,
    TZTemplateService,
)
from backend.core.tz.templates import TemplateValidationError


def _run(coro):
    return asyncio.run(coro)


# === StoredTemplate ====================================================

def test_from_default_seed_known() -> None:
    s = StoredTemplate.from_default_seed("landing")
    assert s is not None
    assert s.task_type == "landing"
    assert s.id == "default::landing"
    assert s.is_default is True
    assert len(s.blocks) > 0


def test_from_default_seed_unknown() -> None:
    assert StoredTemplate.from_default_seed("nonexistent") is None


def test_from_row_basic() -> None:
    row = {
        "id": "tpl_abc",
        "task_type": "landing",
        "name": "custom",
        "description": "test",
        "blocks": [{"name": "hero", "label": "Hero", "fields": []}],
        "is_default": False,
        "tenant_id": "t-1",
    }
    s = StoredTemplate.from_row(row)
    assert s.id == "tpl_abc"
    assert s.is_default is False
    assert len(s.blocks) == 1


def test_from_row_string_jsonb() -> None:
    """JSONB как stringified JSON — должны раcпарсить."""
    row = {
        "id": "tpl_x",
        "task_type": "api",
        "name": "x",
        "description": "",
        "blocks": '[{"name":"e","label":"E","fields":[]}]',
        "is_default": False,
    }
    s = StoredTemplate.from_row(row)
    assert len(s.blocks) == 1


def test_from_row_invalid_jsonb_string() -> None:
    """Если invalid JSON — blocks=[]."""
    row = {
        "id": "tpl_x", "task_type": "api", "name": "x",
        "description": "", "blocks": "{not json", "is_default": False,
    }
    s = StoredTemplate.from_row(row)
    assert s.blocks == []


def test_to_dict_marks_source() -> None:
    s = StoredTemplate(
        id="tpl_1", task_type="landing", name="x",
        description="", blocks=[], is_default=False,
    )
    d = s.to_dict()
    assert d["source"] == "tenant"
    assert d["id"] == "tpl_1"


# === Service без postgres (defaults-only mode) ========================

def test_list_no_postgres_returns_defaults_only() -> None:
    svc = TZTemplateService()
    rows = _run(svc.list())
    # Все returned — это defaults.
    assert all(r.is_default for r in rows)
    assert all(r.id.startswith("default::") for r in rows)
    # Минимум 4 default-seeds (landing/documentation/presentation/email_campaign).
    assert len(rows) >= 4


def test_list_filters_by_task_type() -> None:
    svc = TZTemplateService()
    rows = _run(svc.list(task_type="landing"))
    assert len(rows) == 1
    assert rows[0].task_type == "landing"


def test_list_skip_defaults_returns_empty() -> None:
    """include_defaults=False + no postgres → empty list."""
    svc = TZTemplateService()
    rows = _run(svc.list(include_defaults=False))
    assert rows == []


def test_get_default_seed() -> None:
    svc = TZTemplateService()
    s = _run(svc.get(template_id="default::landing"))
    assert s is not None
    assert s.task_type == "landing"


def test_get_unknown_default_returns_none() -> None:
    svc = TZTemplateService()
    s = _run(svc.get(template_id="default::nonexistent"))
    assert s is None


def test_get_empty_id_returns_none() -> None:
    svc = TZTemplateService()
    assert _run(svc.get(template_id="")) is None


def test_get_default_for_task_type_falls_back_to_seed() -> None:
    """Без postgres → fallback на built-in seed."""
    svc = TZTemplateService()
    s = _run(svc.get_default_for_task_type(task_type="landing"))
    assert s is not None
    assert s.id == "default::landing"


def test_create_without_postgres_raises() -> None:
    svc = TZTemplateService()
    with pytest.raises(RuntimeError) as exc:
        _run(svc.create(
            task_type="landing", name="x",
            blocks=[{"name": "h", "label": "H", "fields": []}],
        ))
    assert "postgres unavailable" in str(exc.value)


def test_update_default_raises_conflict() -> None:
    """Built-in defaults — read-only."""
    svc = TZTemplateService()
    with pytest.raises(TemplateConflict):
        _run(svc.update(template_id="default::landing", name="renamed"))


def test_set_default_on_built_in_raises() -> None:
    svc = TZTemplateService()
    with pytest.raises(TemplateConflict):
        _run(svc.set_default(template_id="default::landing"))


def test_delete_default_raises_conflict() -> None:
    svc = TZTemplateService()
    with pytest.raises(TemplateConflict):
        _run(svc.delete(template_id="default::landing"))


def test_delete_without_postgres_raises() -> None:
    svc = TZTemplateService()
    with pytest.raises(RuntimeError) as exc:
        _run(svc.delete(template_id="tpl_x"))
    assert "postgres unavailable" in str(exc.value)


# === Service с mocked postgres =========================================

class _FakeSession:
    def __init__(self, db) -> None:
        self.db = db

    async def execute(self, sql, params=None):
        params = params or {}
        sql_lower = sql.lower().strip()
        if sql_lower.startswith("insert"):
            self.db["rows"].append({
                "id": params["id"],
                "task_type": params["task_type"],
                "name": params["name"],
                "description": params["description"],
                "blocks": params["blocks"],   # JSON string
                "is_default": params["is_default"],
                "created_by": params["created_by"],
                "tenant_id": "t-1",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            })
            return _FakeResult([])
        if sql_lower.startswith("select"):
            if "where id = :id" in sql_lower:
                row = next(
                    (r for r in self.db["rows"] if r["id"] == params.get("id")),
                    None,
                )
                return _FakeResult([row] if row else [])
            # list — фильтруем по task_type если указан.
            rows = list(self.db["rows"])
            if "task_type" in params:
                rows = [r for r in rows if r["task_type"] == params["task_type"]]
            if "is_default = TRUE" in sql.upper():
                rows = [r for r in rows if r["is_default"]]
            return _FakeResult(rows)
        if sql_lower.startswith("update"):
            # Specialized CLEAR_DEFAULT SQL (sets is_default=FALSE для других).
            if ("set is_default = false" in sql_lower
                    and "task_type = :task_type" in sql_lower
                    and "id <> :id" in sql_lower):
                for r in self.db["rows"]:
                    if (r["task_type"] == params.get("task_type")
                            and r["id"] != params.get("id")):
                        r["is_default"] = False
                return _FakeResult([])

            # Generic UPDATE по id.
            target_id = params.get("id")
            for r in self.db["rows"]:
                if r["id"] != target_id:
                    continue
                if "name = :name" in sql:
                    r["name"] = params["name"]
                if "description = :description" in sql:
                    r["description"] = params["description"]
                if "blocks = cast" in sql_lower:
                    r["blocks"] = params["blocks"]
                if "set is_default = true" in sql_lower:
                    r["is_default"] = True
            return _FakeResult([])
        if sql_lower.startswith("delete"):
            self.db["rows"] = [r for r in self.db["rows"] if r["id"] != params.get("id")]
            return _FakeResult([])
        return _FakeResult([])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass


class _FakeResult:
    def __init__(self, rows) -> None:
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows

    def mappings(self):
        return self


class _FakePostgres:
    def __init__(self) -> None:
        self.db = {"rows": []}

    def session(self):
        return _FakeSession(self.db)


def test_create_with_postgres() -> None:
    pg = _FakePostgres()
    svc = TZTemplateService(postgres=pg)
    created = _run(svc.create(
        task_type="landing",
        name="custom",
        description="test",
        blocks=[{"name": "hero", "label": "Hero", "fields": []}],
        created_by="u-1",
    ))
    assert created.task_type == "landing"
    assert created.id.startswith("tpl_")
    # Сохранилось в нашем in-memory db
    assert len(pg.db["rows"]) == 1


def test_create_invalid_blocks_raises_validation() -> None:
    pg = _FakePostgres()
    svc = TZTemplateService(postgres=pg)
    with pytest.raises(TemplateValidationError):
        _run(svc.create(
            task_type="landing",
            name="x",
            blocks=[{"label": "Hero"}],  # missing name in block
        ))


def test_set_default_clears_others() -> None:
    pg = _FakePostgres()
    svc = TZTemplateService(postgres=pg)
    a = _run(svc.create(
        task_type="landing", name="a", is_default=True,
        blocks=[{"name": "h", "label": "H", "fields": []}],
    ))
    b = _run(svc.create(
        task_type="landing", name="b", is_default=False,
        blocks=[{"name": "h", "label": "H", "fields": []}],
    ))
    # Сначала a — default, b — нет.
    assert pg.db["rows"][0]["is_default"] is True
    assert pg.db["rows"][1]["is_default"] is False

    # set_default(b) → a сбрасывается, b становится default.
    _run(svc.set_default(template_id=b.id))
    by_id = {r["id"]: r for r in pg.db["rows"]}
    assert by_id[a.id]["is_default"] is False
    assert by_id[b.id]["is_default"] is True


def test_update_modifies_only_specified_fields() -> None:
    pg = _FakePostgres()
    svc = TZTemplateService(postgres=pg)
    created = _run(svc.create(
        task_type="landing", name="orig", description="orig desc",
        blocks=[{"name": "h", "label": "H", "fields": []}],
    ))
    _run(svc.update(template_id=created.id, name="renamed"))
    row = pg.db["rows"][0]
    assert row["name"] == "renamed"
    assert row["description"] == "orig desc"  # untouched


def test_update_unknown_template_raises_not_found() -> None:
    pg = _FakePostgres()
    svc = TZTemplateService(postgres=pg)
    with pytest.raises(TemplateNotFound):
        _run(svc.update(template_id="tpl_does_not_exist", name="x"))


def test_delete_existing_returns_true() -> None:
    pg = _FakePostgres()
    svc = TZTemplateService(postgres=pg)
    created = _run(svc.create(
        task_type="landing", name="x",
        blocks=[{"name": "h", "label": "H", "fields": []}],
    ))
    ok = _run(svc.delete(template_id=created.id))
    assert ok is True
    assert pg.db["rows"] == []


def test_get_default_for_task_type_uses_tenant_when_present() -> None:
    """Tenant's default winning over built-in seed."""
    pg = _FakePostgres()
    svc = TZTemplateService(postgres=pg)
    _run(svc.create(
        task_type="landing", name="custom-default", is_default=True,
        blocks=[{"name": "h", "label": "H", "fields": []}],
    ))
    s = _run(svc.get_default_for_task_type(task_type="landing"))
    assert s is not None
    assert s.name == "custom-default"
    assert s.is_default is True
