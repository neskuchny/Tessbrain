"""Unit-тесты для core.tz.templates (W18)."""
from __future__ import annotations

import pytest
from backend.core.tz.templates import (
    DEFAULT_TEMPLATES,
    TemplateValidationError,
    TZBlock,
    TZField,
    TZTemplate,
    get_default_template,
    list_default_task_types,
    merge_template_overrides,
    validate_draft_against_template,
)

# === TZField =============================================================

def test_field_minimal() -> None:
    f = TZField.from_dict({"name": "headline"})
    assert f.name == "headline"
    assert f.type == "text"
    assert f.required is True


def test_field_invalid_type() -> None:
    with pytest.raises(TemplateValidationError):
        TZField.from_dict({"name": "x", "type": "weird"})


def test_field_missing_name() -> None:
    with pytest.raises(TemplateValidationError):
        TZField.from_dict({})


def test_field_to_dict_round_trip() -> None:
    src = {"name": "h", "type": "long_text", "required": False, "description": "d"}
    f = TZField.from_dict(src)
    assert f.to_dict() == src


# === TZBlock =============================================================

def test_block_minimal() -> None:
    b = TZBlock.from_dict({
        "name": "hero", "label": "Hero",
        "fields": [{"name": "headline"}],
    })
    assert b.name == "hero"
    assert len(b.fields) == 1


def test_block_missing_label() -> None:
    with pytest.raises(TemplateValidationError):
        TZBlock.from_dict({"name": "x", "fields": []})


def test_block_duplicate_field_names() -> None:
    with pytest.raises(TemplateValidationError):
        TZBlock.from_dict({
            "name": "x", "label": "X",
            "fields": [{"name": "h"}, {"name": "h"}],
        })


def test_block_min_items_default() -> None:
    b = TZBlock.from_dict({"name": "x", "label": "X", "fields": []})
    assert b.min_items == 1


# === TZTemplate ==========================================================

def test_template_minimal() -> None:
    tpl = TZTemplate.from_dict({
        "task_type": "landing", "name": "test",
        "blocks": [{"name": "hero", "label": "Hero", "fields": []}],
    })
    assert tpl.task_type == "landing"


def test_template_empty_blocks_rejected() -> None:
    with pytest.raises(TemplateValidationError):
        TZTemplate.from_dict({
            "task_type": "landing", "name": "x", "blocks": [],
        })


def test_template_duplicate_block_names() -> None:
    with pytest.raises(TemplateValidationError):
        TZTemplate.from_dict({
            "task_type": "landing", "name": "x",
            "blocks": [
                {"name": "h", "label": "H", "fields": []},
                {"name": "h", "label": "H2", "fields": []},
            ],
        })


def test_template_round_trip() -> None:
    src = {
        "task_type": "api", "name": "custom",
        "description": "test",
        "is_default": False,
        "blocks": [
            {"name": "endpoints", "label": "Endpoints", "required": True,
             "min_items": 1, "description_for_llm": "",
             "fields": [{"name": "items", "required": True, "type": "list", "description": ""}]},
        ],
    }
    tpl = TZTemplate.from_dict(src)
    assert tpl.to_dict() == src


# === DEFAULT_TEMPLATES ===================================================

def test_default_templates_all_valid() -> None:
    """Все seeds должны пройти from_dict без ошибок."""
    for task_type, raw in DEFAULT_TEMPLATES.items():
        tpl = TZTemplate.from_dict(raw)
        assert tpl.task_type == task_type
        assert tpl.is_default is True


def test_get_default_template_unknown() -> None:
    assert get_default_template("nonexistent") is None


def test_get_default_template_landing() -> None:
    tpl = get_default_template("landing")
    assert tpl is not None
    block_names = [b.name for b in tpl.blocks]
    assert "hero" in block_names
    assert "pricing" in block_names
    assert "final_cta" in block_names


def test_list_default_task_types_includes_main() -> None:
    types = list_default_task_types()
    assert "landing" in types
    assert "documentation" in types
    assert "presentation" in types
    assert "email_campaign" in types


# === validate_draft_against_template ====================================

def test_validate_draft_all_present() -> None:
    tpl = get_default_template("landing")
    draft = {
        block.name: {f.name: "X" for f in block.fields}
        for block in tpl.blocks
    }
    out = validate_draft_against_template(tpl, draft)
    assert out["missing_blocks"] == []
    assert out["missing_fields"] == []


def test_validate_draft_missing_required_block() -> None:
    tpl = get_default_template("landing")
    out = validate_draft_against_template(tpl, {})
    assert "hero" in out["missing_blocks"]
    assert "pricing" in out["missing_blocks"]


def test_validate_draft_optional_block_missing_ok() -> None:
    """social_proof — не required, его отсутствие не баг."""
    tpl = get_default_template("landing")
    draft = {
        block.name: {f.name: "X" for f in block.fields}
        for block in tpl.blocks if block.required
    }
    out = validate_draft_against_template(tpl, draft)
    assert "social_proof" not in out["missing_blocks"]


def test_validate_draft_missing_required_field() -> None:
    tpl = get_default_template("landing")
    draft = {block.name: {} for block in tpl.blocks if block.required}
    out = validate_draft_against_template(tpl, draft)
    # hero.headline / hero.subheadline / hero.primary_cta — все required.
    assert "hero.headline" in out["missing_fields"]


# === merge_template_overrides ===========================================

def test_merge_overrides_replaces_block() -> None:
    base = get_default_template("landing")
    override = {
        "name": "hero",
        "label": "Custom Hero",
        "required": True,
        "fields": [{"name": "tagline"}],
    }
    merged = merge_template_overrides(base, [override])
    hero = next(b for b in merged.blocks if b.name == "hero")
    assert hero.label == "Custom Hero"
    assert [f.name for f in hero.fields] == ["tagline"]
    # Остальные блоки не тронуты.
    assert any(b.name == "pricing" for b in merged.blocks)


def test_merge_overrides_marks_as_non_default() -> None:
    """custom = is_default=False (есть только один default per task_type)."""
    base = get_default_template("landing")
    merged = merge_template_overrides(base, [])
    assert merged.is_default is False


def test_merge_overrides_empty() -> None:
    """Без overrides — блоки идентичны base."""
    base = get_default_template("landing")  # api нет в defaults
    assert base is not None
    merged = merge_template_overrides(base, [])
    assert len(merged.blocks) == len(base.blocks)
