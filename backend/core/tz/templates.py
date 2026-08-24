"""TZ templates: per-tenant customizable шаблоны (W18).

Pure-Python helpers без зависимостей кроме stdlib. Шаблоны живут в
БД (миграция 090), здесь только parsing/validation/merge + default seeds.

Дизайн:
- Шаблон = упорядоченный список блоков. Блок = `{name, label, required,
  fields[], min_items?, description_for_llm}`.
- Field = `{name, required, type, description}`. type — informational,
  не enum (LLM сам решает форматирование).
- DEFAULT_TEMPLATES — стартовый seed для task_types (landing, api, ...).
  Tenant может через UI/CRUD-endpoint скопировать default и отредактировать.
- Validation чисто структурная — проверяем что нужные ключи есть,
  имена уникальны, типы правильные. LLM не вызывается.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

_REQUIRED_BLOCK_KEYS = ("name", "label")
_ALLOWED_FIELD_TYPES = frozenset({
    "text", "long_text", "list", "number", "url", "image_brief", "code", "table", "any",
})


class TemplateValidationError(ValueError):
    """Шаблон не прошёл структурную валидацию."""


@dataclass(frozen=True)
class TZField:
    name: str
    required: bool = True
    type: str = "text"
    description: str = ""

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "TZField":
        if not isinstance(d, Mapping):
            raise TemplateValidationError(f"field must be a dict, got {type(d).__name__}")
        name = d.get("name")
        if not isinstance(name, str) or not name:
            raise TemplateValidationError("field.name is required")
        ftype = d.get("type", "text")
        if ftype not in _ALLOWED_FIELD_TYPES:
            raise TemplateValidationError(
                f"field.type {ftype!r} not in {sorted(_ALLOWED_FIELD_TYPES)}"
            )
        return cls(
            name=name,
            required=bool(d.get("required", True)),
            type=ftype,
            description=str(d.get("description", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "required": self.required,
            "type": self.type,
            "description": self.description,
        }


@dataclass(frozen=True)
class TZBlock:
    name: str
    label: str
    required: bool = True
    min_items: int = 1                 # для блоков-списков (e.g. features)
    description_for_llm: str = ""
    fields: list[TZField] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "TZBlock":
        if not isinstance(d, Mapping):
            raise TemplateValidationError(f"block must be a dict, got {type(d).__name__}")
        for k in _REQUIRED_BLOCK_KEYS:
            if not d.get(k):
                raise TemplateValidationError(f"block.{k} is required")
        raw_fields = d.get("fields") or []
        if not isinstance(raw_fields, list):
            raise TemplateValidationError("block.fields must be a list")
        fields = [TZField.from_dict(f) for f in raw_fields]
        if len({f.name for f in fields}) != len(fields):
            raise TemplateValidationError(
                f"duplicate field names in block {d['name']!r}"
            )
        return cls(
            name=str(d["name"]),
            label=str(d["label"]),
            required=bool(d.get("required", True)),
            min_items=int(d.get("min_items", 1) or 1),
            description_for_llm=str(d.get("description_for_llm", "")),
            fields=fields,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "required": self.required,
            "min_items": self.min_items,
            "description_for_llm": self.description_for_llm,
            "fields": [f.to_dict() for f in self.fields],
        }


@dataclass(frozen=True)
class TZTemplate:
    task_type: str
    name: str
    description: str = ""
    blocks: list[TZBlock] = field(default_factory=list)
    is_default: bool = False

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "TZTemplate":
        if not isinstance(d, Mapping):
            raise TemplateValidationError("template must be a dict")
        task_type = d.get("task_type")
        if not isinstance(task_type, str) or not task_type:
            raise TemplateValidationError("template.task_type is required")
        tname = d.get("name")
        if not isinstance(tname, str) or not tname:
            raise TemplateValidationError("template.name is required")
        raw_blocks = d.get("blocks") or []
        if not isinstance(raw_blocks, list) or not raw_blocks:
            raise TemplateValidationError("template.blocks must be a non-empty list")
        blocks = [TZBlock.from_dict(b) for b in raw_blocks]
        if len({b.name for b in blocks}) != len(blocks):
            raise TemplateValidationError("duplicate block names in template")
        return cls(
            task_type=task_type,
            name=tname,
            description=str(d.get("description", "")),
            blocks=blocks,
            is_default=bool(d.get("is_default", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_type": self.task_type,
            "name": self.name,
            "description": self.description,
            "blocks": [b.to_dict() for b in self.blocks],
            "is_default": self.is_default,
        }


# === Default seeds =======================================================
# Это «общие» шаблоны которые применяются если у tenant'а нет своих.
# Tenant может скопировать → отредактировать → задать как default.

DEFAULT_TEMPLATES: dict[str, dict[str, Any]] = {
    "landing": {
        "task_type": "landing",
        "name": "default_landing",
        "description": "Дефолтный шаблон для генерации ТЗ на лендинг.",
        "is_default": True,
        "blocks": [
            {
                "name": "hero",
                "label": "Hero",
                "required": True,
                "description_for_llm": "Первый экран. Должен моментально объяснять value proposition.",
                "fields": [
                    {"name": "headline", "required": True, "type": "text",
                     "description": "Главный заголовок ≤ 80 знаков"},
                    {"name": "subheadline", "required": True, "type": "text",
                     "description": "Подзаголовок 1-2 строки"},
                    {"name": "primary_cta", "required": True, "type": "text",
                     "description": "Текст основной CTA-кнопки"},
                    {"name": "hero_image_brief", "required": False, "type": "image_brief",
                     "description": "Описание визуала: что изображено, тон, формат"},
                ],
            },
            {
                "name": "pain_points",
                "label": "Pain points",
                "required": True,
                "description_for_llm": "3-5 болей целевой аудитории. Конкретно, на их языке.",
                "fields": [
                    {"name": "audience_pain", "required": True, "type": "list",
                     "description": "Список болей, ≥3 шт"},
                ],
            },
            {
                "name": "solution",
                "label": "Solution",
                "required": True,
                "fields": [
                    {"name": "summary", "required": True, "type": "long_text"},
                    {"name": "how_it_works", "required": True, "type": "list"},
                ],
            },
            {
                "name": "features",
                "label": "Features",
                "required": True,
                "min_items": 3,
                "fields": [
                    {"name": "items", "required": True, "type": "list",
                     "description": "Список фич, ≥3 шт. Каждая — заголовок + 1-2 строки."},
                ],
            },
            {
                "name": "social_proof",
                "label": "Social proof",
                "required": False,
                "fields": [
                    {"name": "logos_or_quotes", "required": False, "type": "list"},
                ],
            },
            {
                "name": "pricing",
                "label": "Pricing",
                "required": True,
                "fields": [
                    {"name": "tiers", "required": True, "type": "table"},
                ],
            },
            {
                "name": "faq",
                "label": "FAQ",
                "required": False,
                "fields": [
                    {"name": "items", "required": False, "type": "list"},
                ],
            },
            {
                "name": "final_cta",
                "label": "Final CTA",
                "required": True,
                "fields": [
                    {"name": "text", "required": True, "type": "text"},
                    {"name": "button_label", "required": True, "type": "text"},
                ],
            },
        ],
    },
    "documentation": {
        "task_type": "documentation",
        "name": "default_documentation",
        "description": "Документация продукта/API.",
        "is_default": True,
        "blocks": [
            {"name": "overview", "label": "Overview", "required": True,
             "fields": [{"name": "summary", "required": True, "type": "long_text"}]},
            {"name": "getting_started", "label": "Getting started", "required": True,
             "fields": [{"name": "steps", "required": True, "type": "list"}]},
            {"name": "concepts", "label": "Core concepts", "required": True,
             "fields": [{"name": "items", "required": True, "type": "list"}]},
            {"name": "api_reference", "label": "API reference", "required": False,
             "fields": [{"name": "endpoints", "required": False, "type": "list"}]},
            {"name": "examples", "label": "Examples", "required": True,
             "fields": [{"name": "items", "required": True, "type": "list"}]},
            {"name": "troubleshooting", "label": "Troubleshooting", "required": False,
             "fields": [{"name": "items", "required": False, "type": "list"}]},
        ],
    },
    "presentation": {
        "task_type": "presentation",
        "name": "default_presentation",
        "description": "Презентация / pitch deck.",
        "is_default": True,
        "blocks": [
            {"name": "title", "label": "Title slide", "required": True,
             "fields": [{"name": "title", "required": True, "type": "text"},
                        {"name": "subtitle", "required": False, "type": "text"}]},
            {"name": "agenda", "label": "Agenda", "required": True,
             "fields": [{"name": "items", "required": True, "type": "list"}]},
            {"name": "problem", "label": "Problem", "required": True,
             "fields": [{"name": "summary", "required": True, "type": "long_text"}]},
            {"name": "solution", "label": "Solution", "required": True,
             "fields": [{"name": "summary", "required": True, "type": "long_text"}]},
            {"name": "demo", "label": "Demo / How it works", "required": False,
             "fields": [{"name": "steps", "required": False, "type": "list"}]},
            {"name": "roadmap", "label": "Roadmap", "required": False,
             "fields": [{"name": "items", "required": False, "type": "list"}]},
            {"name": "ask", "label": "Ask / Next steps", "required": True,
             "fields": [{"name": "next_steps", "required": True, "type": "list"}]},
        ],
    },
    "email_campaign": {
        "task_type": "email_campaign",
        "name": "default_email_campaign",
        "description": "Email-рассылка / письмо клиенту.",
        "is_default": True,
        "blocks": [
            {"name": "subject", "label": "Subject line", "required": True,
             "fields": [{"name": "primary", "required": True, "type": "text",
                         "description": "≤60 знаков, без CAPS"}]},
            {"name": "hook", "label": "Hook (preview text)", "required": True,
             "fields": [{"name": "text", "required": True, "type": "text"}]},
            {"name": "body", "label": "Body", "required": True,
             "fields": [{"name": "paragraphs", "required": True, "type": "list"}]},
            {"name": "cta", "label": "CTA", "required": True,
             "fields": [{"name": "label", "required": True, "type": "text"},
                        {"name": "url", "required": True, "type": "url"}]},
            {"name": "unsubscribe", "label": "Unsubscribe footer", "required": True,
             "fields": [{"name": "text", "required": True, "type": "text"}]},
        ],
    },
}


def get_default_template(task_type: str) -> Optional[TZTemplate]:
    """Вернуть default seed-template для task_type, если есть."""
    raw = DEFAULT_TEMPLATES.get(task_type)
    if not raw:
        return None
    # deepcopy чтобы caller-мутации не ломали seed.
    return TZTemplate.from_dict(copy.deepcopy(raw))


def list_default_task_types() -> list[str]:
    """Список task_types для которых есть defaults."""
    return sorted(DEFAULT_TEMPLATES)


# === Validation хелперы для draft-блоков =================================

def validate_draft_against_template(
    template: TZTemplate,
    draft: Mapping[str, Any],
) -> dict[str, list[str]]:
    """Проверить что draft заполнил все required блоки и required fields.

    Args:
        template: целевой шаблон.
        draft: dict вида `{block_name: {field_name: value, ...}, ...}`.

    Returns:
        dict с ключами `missing_blocks` и `missing_fields`.
        Пустые списки = всё в порядке.
    """
    missing_blocks: list[str] = []
    missing_fields: list[str] = []

    for block in template.blocks:
        block_data = draft.get(block.name) if isinstance(draft, Mapping) else None
        # block_data is None → блока нет в draft. {} → блок есть, но пустой.
        # Required block считаем missing только если совсем нет (None).
        if block.required and block_data is None:
            missing_blocks.append(block.name)
            continue
        if not isinstance(block_data, Mapping):
            continue
        for f in block.fields:
            if not f.required:
                continue
            if not block_data.get(f.name):
                missing_fields.append(f"{block.name}.{f.name}")

    return {"missing_blocks": missing_blocks, "missing_fields": missing_fields}


def merge_template_overrides(
    base: TZTemplate,
    overrides: Iterable[Mapping[str, Any]],
) -> TZTemplate:
    """Применить per-tenant overrides к default шаблону.

    overrides — это список `{block_name, ...field-overrides}` который заменяет
    или дополняет блоки base. Простая замена по `block.name`. НЕ deep-merge
    fields — если caller хочет частичный merge, копирует целиком.

    Этот helper нужен для UI «начни с default → отредактируй блок» —
    серверный endpoint вызывает merge, потом сохраняет результат как
    custom template.
    """
    by_name: dict[str, TZBlock] = {b.name: b for b in base.blocks}
    for ov in overrides:
        if not isinstance(ov, Mapping):
            continue
        if "name" not in ov:
            continue
        block = TZBlock.from_dict(ov)
        by_name[block.name] = block
    return TZTemplate(
        task_type=base.task_type,
        name=base.name,
        description=base.description,
        is_default=False,           # override создаёт custom — не default
        blocks=list(by_name.values()),
    )


__all__ = [
    "DEFAULT_TEMPLATES",
    "TZBlock",
    "TZField",
    "TZTemplate",
    "TemplateValidationError",
    "get_default_template",
    "list_default_task_types",
    "merge_template_overrides",
    "validate_draft_against_template",
]
