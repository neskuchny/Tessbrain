"""Structural validator (W21) — markdown blocks vs template.

Берёт первый markdown-artifact из `artifacts` и проверяет что в нём
есть все required-блоки и required-fields из переданного TZ template
(W18). Никакого LLM — чистый regex / парсинг.

Score:
- 10 — все required-блоки и поля покрыты, нет TBD-плейсхолдеров
- -2 за каждый missing required block (BLOCKER если ≥3)
- -1 за каждое missing required field
- -0.5 за каждый TBD/[?] плейсхолдер
- BLOCKER если markdown пустой
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from backend.core.tz.iteration import parse_blocks
from backend.core.validator.base import (
    Issue,
    IssueSeverity,
    Validator,
    ValidatorReport,
)

logger = logging.getLogger(__name__)


_PLACEHOLDER_RE = re.compile(
    r"(\[TBD\]|\[?\?\??\?\]|\<заполнить\>|\<TBD\>|\{\{[^{}]+\}\})",
    re.IGNORECASE,
)
_TEXT_EXTS = {".md", ".markdown", ".txt", ".rst", ".html"}


def _pick_markdown(artifacts: list[dict[str, Any]]) -> Optional[str]:
    """Выбрать первый текстовый artifact, который похож на готовый ответ.

    Эвристика:
    1. .md / .markdown / .txt / .rst — приоритет
    2. иначе любой kind=file с content
    """
    candidates: list[str] = []
    for a in artifacts:
        if not isinstance(a, dict):
            continue
        if a.get("kind") not in {None, "file", "text"}:
            continue
        content = a.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        name = (a.get("name") or "").lower()
        ext_match = any(name.endswith(ext) for ext in _TEXT_EXTS)
        if ext_match:
            candidates.insert(0, content)   # priority
        else:
            candidates.append(content)
    return candidates[0] if candidates else None


class StructuralValidator(Validator):
    """Markdown-blocks coverage validator.

    Принимает kwarg `template` — TZTemplate-dict. Если template не передан,
    проверяем только эвристически (TBD-плейсхолдеры, длина).
    """

    name = "structural"

    async def validate(
        self,
        *,
        task_description: str,
        tz_markdown: str,
        artifacts: list[dict[str, Any]],
        template: Optional[dict[str, Any]] = None,
        **_kwargs: Any,
    ) -> ValidatorReport:
        report = ValidatorReport(validator=self.name, score=10.0)

        markdown = _pick_markdown(artifacts)
        if not markdown:
            report.issues.append(Issue(
                severity=IssueSeverity.BLOCKER,
                category="missing_artifact",
                message="нет текстовых artifact'ов для проверки",
            ))
            report.score = 0.0
            report.summary = "no markdown artifacts found"
            return report

        deductions = 0.0

        # Проверка template-блоков.
        present_blocks: set[str] = set()
        if template:
            try:
                parsed = parse_blocks(markdown)
                present_blocks = {b.name for b in parsed}
            except Exception as exc:
                logger.warning("structural: parse_blocks failed: %s", exc)

            blocks_def = template.get("blocks") or []
            missing_blocks = 0
            for block in blocks_def:
                if not isinstance(block, dict):
                    continue
                bname = block.get("name")
                if not bname or not block.get("required", True):
                    continue
                if bname.lower() not in {p.lower() for p in present_blocks}:
                    missing_blocks += 1
                    report.issues.append(Issue(
                        severity=IssueSeverity.ERROR,
                        category="missing_block",
                        message=f"required block {bname!r} not found in artifact",
                        location=bname,
                        fix_hint="executor пропустил блок; нужно перегенерировать",
                    ))
                    deductions += 2.0
                else:
                    # Проверим required fields (по содержимому соответствующей секции).
                    block_content = _extract_section(markdown, bname)
                    for f in block.get("fields") or []:
                        if not isinstance(f, dict) or not f.get("required", True):
                            continue
                        fname = f.get("name") or ""
                        if not _field_appears(block_content, fname):
                            report.issues.append(Issue(
                                severity=IssueSeverity.WARNING,
                                category="missing_field",
                                message=f"field {bname}.{fname} not detected",
                                location=f"{bname}.{fname}",
                            ))
                            deductions += 1.0

            if missing_blocks >= 3:
                # too many missing blocks — это catastrophic.
                report.issues.append(Issue(
                    severity=IssueSeverity.BLOCKER,
                    category="major_template_gap",
                    message=f"missing {missing_blocks} required blocks — task likely failed",
                ))

        # Поиск placeholder'ов / TBD.
        placeholders = _PLACEHOLDER_RE.findall(markdown)
        if placeholders:
            for p in placeholders[:10]:
                report.issues.append(Issue(
                    severity=IssueSeverity.WARNING,
                    category="placeholder",
                    message=f"unresolved placeholder: {p}",
                ))
            deductions += 0.5 * min(len(placeholders), 6)  # cap at -3

        # Длина — fallback heuristic.
        if len(markdown) < 200:
            report.issues.append(Issue(
                severity=IssueSeverity.ERROR,
                category="too_short",
                message=f"artifact suspiciously short ({len(markdown)} chars)",
            ))
            deductions += 2.0

        report.score = max(0.0, 10.0 - deductions)
        report.summary = (
            f"checked {len(template.get('blocks', [])) if template else 0} required blocks; "
            f"{report.error_count} errors, {report.warning_count} warnings"
        )
        report.metadata = {
            "blocks_present": sorted(present_blocks),
            "deductions": round(deductions, 2),
            "placeholders_count": len(placeholders),
        }
        return report


def _extract_section(markdown: str, block_name: str) -> str:
    """Вернуть содержимое блока по его slug-имени (best-effort)."""
    try:
        from backend.core.tz.iteration import parse_blocks
        for b in parse_blocks(markdown):
            if b.name.lower() == block_name.lower():
                return b.content
    except Exception:
        pass
    return ""


def _field_appears(section_text: str, field_name: str) -> bool:
    """Эвристика: упоминается ли field в тексте секции.

    Считаем «упоминается» если field_name (или snake_case → space-split) встречается
    как substring case-insensitive. Не идеально, но lower bound.
    """
    if not section_text or not field_name:
        return False
    candidates = {
        field_name,
        field_name.replace("_", " "),
        field_name.replace("_", "-"),
    }
    text_lower = section_text.lower()
    return any(c.lower() in text_lower for c in candidates)


__all__ = ["StructuralValidator"]
