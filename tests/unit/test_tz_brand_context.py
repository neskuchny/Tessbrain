"""Unit-тесты для core.tz.brand_context (W18)."""
from __future__ import annotations

import asyncio

from backend.core.tz.brand_context import (
    BrandContext,
    from_snapshots,
    load_brand_context,
)


def _run(coro):
    return asyncio.run(coro)


# === from_snapshots ======================================================

def test_from_snapshots_empty_no_data() -> None:
    ctx = from_snapshots()
    assert ctx.has_data is False


def test_from_snapshots_company_basic() -> None:
    ctx = from_snapshots(company_snapshot={
        "name": "Acme",
        "industry": "SaaS",
        "products": ["Widget", "Gadget"],
    })
    assert ctx.company_name == "Acme"
    assert ctx.products == ["Widget", "Gadget"]
    assert ctx.has_data is True


def test_from_snapshots_alternative_keys() -> None:
    """Толерантность к разным конвенциям ключей."""
    ctx = from_snapshots(company_snapshot={
        "company_name": "Acme",
        "description": "We make widgets",
        "icp": "SMB SaaS companies",
        "voice": "friendly, technical",
    })
    assert ctx.company_name == "Acme"
    assert ctx.company_summary.startswith("We make")
    assert ctx.audience.startswith("SMB")
    assert ctx.tone_of_voice == "friendly, technical"


def test_from_snapshots_voice_samples_capped() -> None:
    """Не больше 3 voice samples."""
    ctx = from_snapshots(company_snapshot={
        "name": "X",
        "voice_samples": [f"sample {i}" for i in range(10)],
    })
    assert len(ctx.voice_samples) == 3


def test_from_snapshots_department() -> None:
    ctx = from_snapshots(
        company_snapshot={"name": "Acme"},
        department_snapshot={"name": "Engineering", "focus": "platform infra"},
    )
    assert ctx.department_name == "Engineering"
    assert ctx.department_focus == "platform infra"


def test_from_snapshots_filters_non_string_products() -> None:
    ctx = from_snapshots(company_snapshot={
        "name": "X",
        "products": [None, "valid", 123, ""],
    })
    # None и пустая строка falsy → отфильтрованы; 123 → "123".
    assert "valid" in ctx.products


# === to_prompt_block =====================================================

def test_to_prompt_block_empty() -> None:
    """has_data=False → пустой блок."""
    assert from_snapshots().to_prompt_block() == ""


def test_to_prompt_block_includes_company_name() -> None:
    ctx = from_snapshots(company_snapshot={"name": "Acme"})
    block = ctx.to_prompt_block()
    assert "Acme" in block
    assert block.startswith("<brand_context>")
    assert "</brand_context>" in block
    assert "match the company's voice" in block


def test_to_prompt_block_includes_voice_samples() -> None:
    ctx = from_snapshots(company_snapshot={
        "name": "X",
        "voice_samples": ["First sample", "Second one", "Third"],
    })
    block = ctx.to_prompt_block()
    assert "First sample" in block
    assert "[1]" in block


def test_to_prompt_block_truncates_long_fields() -> None:
    long = "x" * 5000
    ctx = from_snapshots(company_snapshot={"name": "X", "description": long})
    block = ctx.to_prompt_block()
    # Должен быть truncated с "…".
    assert "…" in block


# === load_brand_context ==================================================

class _FakeLoader:
    def __init__(self, company=None, department=None, raise_company=False) -> None:
        self.company = company
        self.department = department
        self.raise_company = raise_company

    async def load_company(self, *, user_id):
        if self.raise_company:
            raise RuntimeError("boom")
        return self.company

    async def load_department(self, dept_id):
        return self.department


def test_load_brand_context_no_loader() -> None:
    ctx = _run(load_brand_context())
    assert isinstance(ctx, BrandContext)
    assert ctx.has_data is False


def test_load_brand_context_basic() -> None:
    loader = _FakeLoader(company={"name": "Acme", "industry": "SaaS"})
    ctx = _run(load_brand_context(user_id="u-1", snapshot_loader=loader))
    assert ctx.company_name == "Acme"


def test_load_brand_context_department_optional() -> None:
    """Без department_id — не вызываем load_department."""
    loader = _FakeLoader(company={"name": "X"}, department={"name": "should not load"})
    ctx = _run(load_brand_context(user_id="u-1", snapshot_loader=loader))
    assert ctx.department_name == ""  # не передали department_id


def test_load_brand_context_with_department() -> None:
    loader = _FakeLoader(
        company={"name": "X"},
        department={"name": "Sales", "focus": "Q4 push"},
    )
    ctx = _run(load_brand_context(
        user_id="u-1", department_id="d-1", snapshot_loader=loader,
    ))
    assert ctx.department_name == "Sales"


def test_load_brand_context_handles_loader_failure() -> None:
    """Loader падает → возвращаем пустой BrandContext, никаких raises."""
    loader = _FakeLoader(raise_company=True)
    ctx = _run(load_brand_context(user_id="u-1", snapshot_loader=loader))
    assert isinstance(ctx, BrandContext)
    assert ctx.has_data is False
