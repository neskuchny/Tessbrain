"""Template prompt builder (W25): рендер TZ template в LLM prompt-block.

Используется EnhancedTZGenerator для injection per-tenant template
структуры в Generate-стадию. Без этого helper'а generator работал
только с in-memory TASK_TYPES seeds — теперь tenant'овский custom
template подхватывается автоматически.

Дизайн:
- Pure: никаких side-effects, никаких raises на пустых/неправильных
  входах — fallback на пустую строку, caller использует свой default.
- Output — короткий текстовый блок (~500-1500 chars), готовый к
  включению в system/user prompt.
- Поддерживает 2 формата:
  * `dict` (как из миграции 090 / TZTemplateService.list)
  * `TZTemplate` dataclass (W18)

Async resolver:
- `make_tenant_template_resolver(service)` → async callable
  `(task_type) -> Optional[dict]` — caller передаёт его в
  EnhancedTZGenerator. Resolver пытается найти tenant'овский default
  через service, при отсутствии fall'ит на seed.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


# Длинна полей жёстко ограничена — не раздуваем prompt.
_MAX_DESCRIPTION_LEN = 400
_MAX_FIELD_DESCRIPTION_LEN = 200
_MAX_BLOCKS = 30
_MAX_FIELDS_PER_BLOCK = 12


def _clip(text: Any, length: int) -> str:
    if not text:
        return ""
    s = str(text).strip()
    if len(s) <= length:
        return s
    return s[:length].rstrip() + "…"


def _normalize_template(template: Any) -> Optional[dict[str, Any]]:
    """Привести TZTemplate-dataclass или dict к единому dict-формату.

    Returns None если входу нечего show'ить (нет blocks).
    """
    if template is None:
        return None
    if hasattr(template, "to_dict") and callable(template.to_dict):
        try:
            return template.to_dict()
        except Exception:
            return None
    if isinstance(template, dict):
        return template
    return None


def render_template_prompt(template: Any) -> str:
    """Рендер шаблона в готовый prompt-block.

    Формат:
        ШАБЛОН ДЛЯ ЭТОГО ТИПА ЗАДАЧИ ("default_landing"):
        Структура должна включать следующие блоки:
        - hero (required): Первый экран...
            • headline (required, text): Главный заголовок ≤ 80 знаков
            • subheadline (required, text): ...
        - pain_points (required): 3-5 болей...
        ...

    Returns:
        Готовый текст. Пустая строка если template пустой.
    """
    norm = _normalize_template(template)
    if not norm:
        return ""

    blocks = norm.get("blocks") or []
    if not blocks:
        return ""

    name = _clip(norm.get("name"), 100)
    description = _clip(norm.get("description"), _MAX_DESCRIPTION_LEN)

    # W43: i18n-aware заголовок шаблона.
    try:
        from backend.core.i18n import t as _i18n_t
        _header_text = _i18n_t("tz.template_block_header")
        _structure_text = _i18n_t("tz.template_structure_intro")
    except Exception:
        _header_text = "ШАБЛОН ДЛЯ ЭТОГО ТИПА ЗАДАЧИ"
        _structure_text = "Структура должна включать следующие блоки:"

    lines: list[str] = []
    header = _header_text
    if name:
        header += f' ("{name}")'
    lines.append(header + ":")
    if description:
        lines.append(description)
    lines.append("")
    lines.append(_structure_text)

    blocks_used = 0
    for b in blocks:
        if blocks_used >= _MAX_BLOCKS:
            break
        if not isinstance(b, dict):
            continue
        bname = _clip(b.get("name"), 80)
        if not bname:
            continue
        blocks_used += 1
        label = _clip(b.get("label"), 100)
        required = b.get("required", True)
        marker = "required" if required else "optional"
        block_desc = _clip(b.get("description_for_llm"), _MAX_DESCRIPTION_LEN)
        title = label or bname
        head = f"- {bname} ({marker}): {title}"
        if block_desc and block_desc.lower() != title.lower():
            head += f" — {block_desc}"
        lines.append(head)

        fields = b.get("fields") or []
        for fi, f in enumerate(fields):
            if fi >= _MAX_FIELDS_PER_BLOCK:
                break
            if not isinstance(f, dict):
                continue
            fname = _clip(f.get("name"), 80)
            if not fname:
                continue
            ftype = _clip(f.get("type"), 30) or "text"
            freq = "required" if f.get("required", True) else "optional"
            fdesc = _clip(f.get("description"), _MAX_FIELD_DESCRIPTION_LEN)
            line = f"    • {fname} ({freq}, {ftype})"
            if fdesc:
                line += f": {fdesc}"
            lines.append(line)

    if blocks_used == 0:
        return ""

    lines.append("")
    try:
        from backend.core.i18n import t as _i18n_t
        _hint_text = _i18n_t("tz.template_assumption_hint")
    except Exception:
        _hint_text = (
            "Если какой-то required-блок невозможно покрыть из имеющихся "
            "данных — явно отметь это в ТЗ как assumption / open question, "
            "не выдумывай контент."
        )
    lines.append(_hint_text)
    return "\n".join(lines)


def required_block_names(template: Any) -> list[str]:
    """Извлечь имена required-блоков для последующей валидации (W21)."""
    norm = _normalize_template(template)
    if not norm:
        return []
    out: list[str] = []
    for b in norm.get("blocks") or []:
        if not isinstance(b, dict):
            continue
        if b.get("required", True):
            name = (b.get("name") or "").strip()
            if name:
                out.append(name)
    return out


# === Async resolver factories ============================================


TemplateResolver = Callable[[str], Awaitable[Optional[dict[str, Any]]]]


def make_tenant_template_resolver(service: Any) -> TemplateResolver:
    """Сделать async-resolver, который тянет default-template для
    task_type из TZTemplateService.

    Если service не передан / упал — возвращает None (caller fallback'ит
    на свой код).
    """
    async def _resolver(task_type: str) -> Optional[dict[str, Any]]:
        if not task_type or service is None:
            return None
        try:
            stored = await service.get_default_for_task_type(task_type=task_type)
        except Exception as exc:
            logger.debug("template_resolver: service failed for %r: %s", task_type, exc)
            return None
        if stored is None:
            return None
        try:
            return stored.to_dict()
        except Exception:
            return None

    return _resolver


def static_template_resolver(
    by_task_type: dict[str, Any],
) -> TemplateResolver:
    """Сделать sync-проектируемый resolver из готового dict.

    Полезно для тестов / non-DB режима.
    """
    async def _resolver(task_type: str) -> Optional[dict[str, Any]]:
        return by_task_type.get(task_type)

    return _resolver


async def build_tz_template_resolver() -> Optional[TemplateResolver]:
    """W29: production-ready async builder.

    Best-effort: lazily берёт `PostgresClient`, оборачивает в
    `TZTemplateService`, возвращает resolver готовый к передаче в
    `EnhancedTZGenerator(template_resolver=...)`.

    Возвращает None если ничего не сложилось — caller безопасно
    игнорирует (generator работает по seeds).
    """
    try:
        from backend.core.tz.template_service import TZTemplateService
    except Exception as exc:
        logger.debug("build_tz_template_resolver: service import failed: %s", exc)
        return None

    postgres = None
    try:
        from backend.db.postgres import get_postgres
        postgres = await get_postgres()
    except Exception as exc:
        logger.debug("build_tz_template_resolver: postgres unavailable: %s", exc)

    service = TZTemplateService(postgres=postgres)
    return make_tenant_template_resolver(service)


__all__ = [
    "TemplateResolver",
    "build_tz_template_resolver",
    "make_tenant_template_resolver",
    "render_template_prompt",
    "required_block_names",
    "static_template_resolver",
]
