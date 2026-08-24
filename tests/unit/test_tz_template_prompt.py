"""Unit-тесты для core.tz.template_prompt (W25)."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from backend.core.tz.template_prompt import (
    _clip,
    _normalize_template,
    make_tenant_template_resolver,
    render_template_prompt,
    required_block_names,
    static_template_resolver,
)


def _run(coro):
    return asyncio.run(coro)


# === _clip =============================================================

def test_clip_passthrough_short() -> None:
    assert _clip("hello", 100) == "hello"


def test_clip_truncates_with_ellipsis() -> None:
    out = _clip("x" * 100, 50)
    assert len(out) == 51   # 50 chars + ellipsis
    assert out.endswith("…")


def test_clip_empty() -> None:
    assert _clip(None, 50) == ""
    assert _clip("", 50) == ""


def test_clip_strips_whitespace() -> None:
    assert _clip("  hello  ", 100) == "hello"


# === _normalize_template ===============================================

def test_normalize_dict_passthrough() -> None:
    out = _normalize_template({"name": "x", "blocks": []})
    assert out == {"name": "x", "blocks": []}


def test_normalize_none_returns_none() -> None:
    assert _normalize_template(None) is None


def test_normalize_dataclass_via_to_dict() -> None:
    @dataclass
    class _Fake:
        x: int = 0

        def to_dict(self):
            return {"name": "from_dataclass", "blocks": [{"name": "h"}]}

    out = _normalize_template(_Fake())
    assert out["name"] == "from_dataclass"


def test_normalize_unsupported_type_returns_none() -> None:
    assert _normalize_template("string") is None
    assert _normalize_template(42) is None


# === render_template_prompt ============================================

def test_render_empty_returns_empty() -> None:
    assert render_template_prompt(None) == ""
    assert render_template_prompt({}) == ""
    assert render_template_prompt({"blocks": []}) == ""


def test_render_minimal_block() -> None:
    out = render_template_prompt({
        "name": "test_landing",
        "blocks": [{"name": "hero", "label": "Hero", "fields": []}],
    })
    assert "test_landing" in out
    assert "hero" in out
    assert "required" in out
    assert "Hero" in out


def test_render_includes_description() -> None:
    out = render_template_prompt({
        "name": "x",
        "description": "Шаблон для лендинга",
        "blocks": [{"name": "hero", "label": "Hero"}],
    })
    assert "Шаблон для лендинга" in out


def test_render_optional_block_marked() -> None:
    out = render_template_prompt({
        "blocks": [
            {"name": "hero", "label": "Hero", "required": True},
            {"name": "social_proof", "label": "Social", "required": False},
        ],
    })
    assert "hero (required)" in out
    assert "social_proof (optional)" in out


def test_render_includes_field_details() -> None:
    out = render_template_prompt({
        "blocks": [{
            "name": "hero", "label": "Hero",
            "fields": [
                {"name": "headline", "type": "text",
                 "description": "Главный заголовок"},
                {"name": "cta", "type": "text", "required": True},
            ],
        }],
    })
    assert "headline" in out
    assert "cta" in out
    assert "Главный заголовок" in out


def test_render_skips_invalid_blocks() -> None:
    out = render_template_prompt({
        "blocks": [
            {"name": "valid", "label": "V"},
            "not a dict",
            {"label": "no name"},  # пропускается
            None,
        ],
    })
    assert "valid" in out
    # Не должно быть "no name" или "not a dict".
    assert "not a dict" not in out


def test_render_caps_at_max_blocks() -> None:
    """Не больше 30 блоков попадает в prompt."""
    blocks = [
        {"name": f"b{i}", "label": f"L{i}", "fields": []}
        for i in range(50)
    ]
    out = render_template_prompt({"blocks": blocks})
    # Проверяем что b29 есть, а b40 — нет (cap=30).
    assert "b0" in out
    assert "b29" in out
    assert "b40" not in out


def test_render_caps_fields_per_block() -> None:
    fields = [{"name": f"f{i}", "type": "text"} for i in range(20)]
    out = render_template_prompt({
        "blocks": [{"name": "x", "label": "X", "fields": fields}],
    })
    assert "f0" in out
    # cap=12
    assert "f15" not in out


def test_render_no_named_blocks_returns_empty() -> None:
    out = render_template_prompt({
        "blocks": [{"label": "no name 1"}, {"label": "no name 2"}],
    })
    assert out == ""


def test_render_includes_assumption_hint() -> None:
    """В конце должна быть инструкция о required-блоках без данных."""
    out = render_template_prompt({
        "blocks": [{"name": "hero", "label": "Hero"}],
    })
    assert "assumption" in out.lower() or "open question" in out.lower()


# === required_block_names ==============================================

def test_required_names_basic() -> None:
    out = required_block_names({
        "blocks": [
            {"name": "hero", "required": True},
            {"name": "social", "required": False},
            {"name": "pricing", "required": True},
        ],
    })
    assert out == ["hero", "pricing"]


def test_required_names_default_required_true() -> None:
    out = required_block_names({
        "blocks": [
            {"name": "hero"},   # default required=True
        ],
    })
    assert out == ["hero"]


def test_required_names_skips_invalid() -> None:
    out = required_block_names({
        "blocks": [
            {"name": "valid"},
            None,
            {"required": True},   # no name
            {"name": "", "required": True},
            "string",
        ],
    })
    assert out == ["valid"]


def test_required_names_empty() -> None:
    assert required_block_names(None) == []
    assert required_block_names({}) == []
    assert required_block_names({"blocks": []}) == []


# === Resolvers =========================================================

class _FakeService:
    def __init__(self, by_type: dict, raise_on=None) -> None:
        self.by_type = by_type
        self.raise_on = raise_on
        self.calls = []

    async def get_default_for_task_type(self, *, task_type):
        self.calls.append(task_type)
        if self.raise_on and task_type == self.raise_on:
            raise RuntimeError("simulated db error")
        stored = self.by_type.get(task_type)
        if stored is None:
            return None
        # Mimic StoredTemplate.to_dict()
        return _StoredStub(stored)


class _StoredStub:
    def __init__(self, data: dict) -> None:
        self._data = data

    def to_dict(self):
        return dict(self._data)


def test_tenant_resolver_returns_dict_when_found() -> None:
    svc = _FakeService(by_type={
        "landing": {"name": "x", "blocks": [{"name": "hero"}]},
    })
    resolver = make_tenant_template_resolver(svc)
    out = _run(resolver("landing"))
    assert out is not None
    assert out["name"] == "x"


def test_tenant_resolver_returns_none_for_missing() -> None:
    resolver = make_tenant_template_resolver(_FakeService(by_type={}))
    out = _run(resolver("nonexistent"))
    assert out is None


def test_tenant_resolver_handles_service_failure() -> None:
    """Service raises → resolver returns None (best-effort)."""
    svc = _FakeService(by_type={"landing": {}}, raise_on="landing")
    resolver = make_tenant_template_resolver(svc)
    out = _run(resolver("landing"))
    assert out is None


def test_tenant_resolver_no_service_returns_none() -> None:
    resolver = make_tenant_template_resolver(None)
    out = _run(resolver("landing"))
    assert out is None


def test_tenant_resolver_empty_task_type_returns_none() -> None:
    svc = _FakeService(by_type={"landing": {}})
    resolver = make_tenant_template_resolver(svc)
    out = _run(resolver(""))
    assert out is None
    # Service не должен быть вызван с пустым task_type.
    assert svc.calls == []


# === static_template_resolver ==========================================

def test_static_resolver_returns_match() -> None:
    resolver = static_template_resolver({
        "landing": {"name": "z"},
    })
    assert _run(resolver("landing")) == {"name": "z"}


def test_static_resolver_returns_none_for_unknown() -> None:
    resolver = static_template_resolver({"landing": {}})
    assert _run(resolver("api")) is None


def test_static_resolver_empty_dict() -> None:
    resolver = static_template_resolver({})
    assert _run(resolver("anything")) is None


# === build_tz_template_resolver (W29) ==================================


def _install_fake_postgres_module(get_pg_coro):
    """Зарегистрировать stub-пакет backend.db.postgres с заданным get_postgres.

    builder делает `from backend.db.postgres import get_postgres` динамически,
    поэтому реальный модуль (с tenacity-зависимостью) не нужен.
    """
    import sys
    import types

    db_pkg = sys.modules.get("backend.db") or types.ModuleType("backend.db")
    db_pkg.__path__ = []  # type: ignore[attr-defined]
    sys.modules["backend.db"] = db_pkg

    pg_mod = types.ModuleType("backend.db.postgres")
    pg_mod.get_postgres = get_pg_coro  # type: ignore[attr-defined]
    sys.modules["backend.db.postgres"] = pg_mod


def test_build_resolver_returns_callable_without_postgres() -> None:
    """Без БД builder всё равно возвращает resolver (defaults-only mode)."""
    from backend.core.tz import template_prompt as tp

    async def _fail_pg():
        raise RuntimeError("no pg")

    _install_fake_postgres_module(_fail_pg)
    resolver = _run(tp.build_tz_template_resolver())
    assert resolver is not None
    # Resolver вернёт seed для known task_type (built-in default::landing).
    out = _run(resolver("landing"))
    assert out is not None
    assert isinstance(out, dict)


def test_build_resolver_returns_none_for_unknown_task_type() -> None:
    from backend.core.tz import template_prompt as tp

    async def _fail_pg():
        raise RuntimeError("no pg")

    _install_fake_postgres_module(_fail_pg)
    resolver = _run(tp.build_tz_template_resolver())
    assert _run(resolver("nonexistent_task_type_xxx")) is None


def test_build_resolver_uses_postgres_when_available(monkeypatch) -> None:
    """С postgres → resolver предпочитает tenant'овский default над seed."""
    from backend.core.tz import template_prompt as tp

    captured = {}

    class _FakePostgres:
        pass  # passed through to TZTemplateService

    async def _ok_pg():
        captured["called"] = True
        return _FakePostgres()

    _install_fake_postgres_module(_ok_pg)

    # Заменим TZTemplateService чтобы не лезть в SQL.
    class _FakeService:
        def __init__(self, *, postgres) -> None:
            captured["postgres"] = postgres

        async def get_default_for_task_type(self, *, task_type):
            class _Stub:
                def to_dict(self_inner):
                    return {"name": "tenant-custom", "blocks": []}
            return _Stub() if task_type == "landing" else None

    monkeypatch.setattr(
        "backend.core.tz.template_service.TZTemplateService", _FakeService,
    )

    resolver = _run(tp.build_tz_template_resolver())
    assert captured.get("called") is True
    out = _run(resolver("landing"))
    assert out == {"name": "tenant-custom", "blocks": []}
