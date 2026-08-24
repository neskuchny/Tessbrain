"""Two-pass TZ generation: draft → critique → revise (W18).

Зачем: один LLM-проход даёт «среднее» ТЗ — общее, без конкретики.
Two-pass даёт LLM шанс самокритично пройтись и исправить.

Pipeline:
  1. Draft: генерим markdown ТЗ как обычно (caller вызывает свой generator).
  2. Critique: LLM смотрит на draft + task + brand_context и пишет конкретную
     критику: «hero subheadline слишком общий, нужны цифры», «pricing блок
     не указывает валюту».
  3. Revise: LLM применяет критику и возвращает improved markdown.

Этот модуль не знает про templates / brand_context / collected_data —
caller передаёт всё что собрал, мы только оркестрируем 3 LLM-вызова.

При любой ошибке возвращаем последний валидный draft (никогда не теряем
работу первого прохода).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


_CRITIQUE_SYSTEM_PROMPT = """\
Ты — критик технических заданий. Тебе дают исходную задачу и черновик ТЗ.
Найди конкретные проблемы которые нужно исправить, чтобы ТЗ было готово
для AI-исполнителя.

Что искать:
- Магические плейсхолдеры: `[TBD]`, `???`, `<заполнить>`, `{{...}}`.
- Размытые формулировки: «достаточно», «много», «большая часть».
- Отсутствие конкретных цифр там где они уместны (количество фич, размеры,
  сроки, числовые KPI).
- Противоречия между блоками.
- Несоответствие тон-of-voice (если в контексте есть voice samples).
- Required-блоки которые покрыты слабо или с пометкой «дополнить позже».

Формат — ТОЛЬКО JSON:
{
  "issues": [
    {"block": "hero", "issue": "subheadline слишком общий, добавить цифру роста"},
    {"block": "pricing", "issue": "не указана валюта"}
  ],
  "overall_quality": 0..10,
  "needs_revision": true | false
}

Если issues нет — `issues: []`, `needs_revision: false`.
"""


_REVISE_SYSTEM_PROMPT = """\
Ты — редактор технических заданий. Тебе дают:
1. Исходную задачу.
2. Черновик ТЗ (Markdown).
3. Список конкретных проблем от критика.
4. (опц) Brand context — как обычно пишет компания.

Твоя задача — вернуть **исправленный markdown** который устраняет ВСЕ
указанные проблемы и не теряет существующее содержание. Не добавляй
комментарии «я исправил X» — только сам обновлённый markdown.

Если есть voice samples в brand context — ориентируйся на них для тона,
но не копируй фразы 1:1.
"""


@dataclass
class CritiqueResult:
    issues: list[dict[str, str]] = field(default_factory=list)
    overall_quality: float = 5.0
    needs_revision: bool = False
    raw_response: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "issues": list(self.issues),
            "overall_quality": self.overall_quality,
            "needs_revision": self.needs_revision,
        }


@dataclass
class TwoPassResult:
    draft_markdown: str
    critique: CritiqueResult
    final_markdown: str
    revised: bool             # True если применили revise pass
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft_markdown_length": len(self.draft_markdown),
            "final_markdown_length": len(self.final_markdown),
            "critique": self.critique.to_dict(),
            "revised": self.revised,
            "error": self.error,
        }


def _parse_critique(raw: Any) -> CritiqueResult:
    if isinstance(raw, str):
        import json
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}

    issues_raw = raw.get("issues") or []
    issues: list[dict[str, str]] = []
    if isinstance(issues_raw, list):
        for item in issues_raw[:30]:
            if isinstance(item, dict):
                block = str(item.get("block") or "general")[:100]
                issue = str(item.get("issue") or "")[:500]
                if issue:
                    issues.append({"block": block, "issue": issue})

    try:
        quality = float(raw.get("overall_quality", 5))
    except (TypeError, ValueError):
        quality = 5.0
    quality = max(0.0, min(10.0, quality))

    needs_revision = bool(raw.get("needs_revision", bool(issues)))

    return CritiqueResult(
        issues=issues,
        overall_quality=quality,
        needs_revision=needs_revision,
        raw_response=str(raw)[:1500],
    )


async def critique_draft(
    *,
    task_description: str,
    draft_markdown: str,
    brand_context_block: str = "",
    llm_router: Any,
) -> CritiqueResult:
    """Запустить critique-pass."""
    if llm_router is None or not draft_markdown.strip():
        return CritiqueResult(needs_revision=False)

    prompt_parts = [
        f"<task>\n{task_description.strip()[:3000]}\n</task>",
        f"<draft>\n{draft_markdown.strip()[:20000]}\n</draft>",
    ]
    if brand_context_block:
        prompt_parts.append(brand_context_block)
    prompt_parts.append("Верни JSON-объект согласно правилам system prompt'а.")
    prompt = "\n\n".join(prompt_parts)

    try:
        raw = await llm_router.generate_json(
            prompt=prompt,
            system_prompt=_CRITIQUE_SYSTEM_PROMPT,
            temperature=0,
            max_tokens=1500,
        )
    except Exception as exc:
        logger.warning("two_pass.critique: LLM call failed: %s", exc)
        return CritiqueResult(needs_revision=False)

    return _parse_critique(raw)


async def revise_draft(
    *,
    task_description: str,
    draft_markdown: str,
    issues: list[dict[str, str]],
    brand_context_block: str = "",
    llm_router: Any,
) -> str:
    """Применить critique-issues. Возвращает revised markdown.

    При ошибке возвращаем оригинальный draft без изменений (никогда не
    теряем работу первого прохода).
    """
    if llm_router is None or not draft_markdown.strip() or not issues:
        return draft_markdown

    issues_block = "\n".join(
        f"- [{i.get('block', 'general')}] {i.get('issue', '')}" for i in issues
    )
    prompt_parts = [
        f"<task>\n{task_description.strip()[:3000]}\n</task>",
        f"<draft>\n{draft_markdown.strip()[:20000]}\n</draft>",
        f"<issues>\n{issues_block}\n</issues>",
    ]
    if brand_context_block:
        prompt_parts.append(brand_context_block)
    prompt_parts.append(
        "Верни ТОЛЬКО исправленный markdown. Без преамбул, без объяснений."
    )
    prompt = "\n\n".join(prompt_parts)

    try:
        revised = await llm_router.generate(
            prompt=prompt,
            system_prompt=_REVISE_SYSTEM_PROMPT,
            temperature=0,
            max_tokens=8000,
        )
    except Exception as exc:
        logger.warning("two_pass.revise: LLM call failed: %s", exc)
        return draft_markdown

    revised = (revised or "").strip()
    if not revised:
        return draft_markdown
    # Если revise вернул что-то совсем короткое — fallback на draft.
    if len(revised) < min(len(draft_markdown) * 0.3, 200):
        logger.warning(
            "two_pass.revise: suspicious short output (%d vs %d), keeping draft",
            len(revised), len(draft_markdown),
        )
        return draft_markdown
    return revised


async def two_pass_generate(
    *,
    task_description: str,
    draft_markdown: str,
    brand_context_block: str = "",
    llm_router: Any,
    min_quality_for_revision: float = 8.0,
) -> TwoPassResult:
    """Полный two-pass flow.

    Если critique даёт quality ≥ `min_quality_for_revision` ИЛИ
    `needs_revision=False` — пропускаем revise (draft уже хорош).
    """
    critique = await critique_draft(
        task_description=task_description,
        draft_markdown=draft_markdown,
        brand_context_block=brand_context_block,
        llm_router=llm_router,
    )

    if not critique.needs_revision or critique.overall_quality >= min_quality_for_revision:
        return TwoPassResult(
            draft_markdown=draft_markdown,
            critique=critique,
            final_markdown=draft_markdown,
            revised=False,
        )

    if not critique.issues:
        return TwoPassResult(
            draft_markdown=draft_markdown,
            critique=critique,
            final_markdown=draft_markdown,
            revised=False,
        )

    final = await revise_draft(
        task_description=task_description,
        draft_markdown=draft_markdown,
        issues=critique.issues,
        brand_context_block=brand_context_block,
        llm_router=llm_router,
    )

    return TwoPassResult(
        draft_markdown=draft_markdown,
        critique=critique,
        final_markdown=final,
        revised=(final != draft_markdown),
    )


__all__ = [
    "CritiqueResult",
    "TwoPassResult",
    "critique_draft",
    "revise_draft",
    "two_pass_generate",
]
