"""AI-judge validator (W21) — LLM решает соответствует ли результат brief'у.

В отличие от structural validator (regex coverage), здесь LLM читает ТЗ +
artifacts и оценивает по существу: есть ли там нужная информация, верный
ли тон, нет ли противоречий, соответствует ли brand-context.

temperature=0 → детерминизм + prompt-cache.
Fallback при failure: middle-of-road score=5, чтобы caller не auto-approve
неоценённый результат.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from backend.core.validator.base import (
    Issue,
    IssueSeverity,
    Validator,
    ValidatorReport,
)

logger = logging.getLogger(__name__)


_AI_JUDGE_SYSTEM_PROMPT = """\
Ты — критик. Тебе дают: исходную задачу, ТЗ (которое было передано
исполнителю) и результат (artifacts от executor'а). Оцени соответствие
результата ТЗ.

Что ищешь:
- Все ли требования из ТЗ выполнены? (содержание, структура, конкретика)
- Есть ли противоречия между ТЗ и результатом?
- Соответствует ли тон/стиль контексту компании (если описан)?
- Не выдумал ли executor цифры/факты которых в ТЗ не было?
- Нет ли пустых stub'ов и TBD?

Шкала:
- 9-10 — результат можно отдавать клиенту/use в проде без правок
- 7-8 — minor правки, mostly OK
- 5-6 — нужны 1-2 итерации
- 3-4 — много пробелов, executor не понял задачу
- 1-2 — практически unusable

Severity issues:
- blocker — задача catastrophically failed (нечего показывать)
- error — серьёзная проблема (отсутствует требование, есть противоречие)
- warning — мелкая правка нужна
- info — наблюдение, не баг

Формат ответа — ТОЛЬКО JSON:
{
  "score": 0..10,
  "verdict": "approve" | "minor_revisions" | "major_revisions" | "reject",
  "issues": [
    {"severity": "error", "category": "missing_requirement",
     "message": "...", "location": "section X", "fix_hint": "..."}
  ],
  "rationale": "1-2 предложения"
}
"""


def _coerce_severity(raw: Any) -> IssueSeverity:
    if isinstance(raw, str):
        s = raw.strip().lower()
        try:
            return IssueSeverity(s)
        except ValueError:
            pass
    return IssueSeverity.WARNING   # default


def _summarize_artifacts(artifacts: list[dict[str, Any]], max_chars: int = 12000) -> str:
    """Скомпилировать artifacts в одну строку для prompt'а."""
    parts: list[str] = []
    used = 0
    for a in artifacts or []:
        if not isinstance(a, dict):
            continue
        name = a.get("name") or "unnamed"
        content = a.get("content")
        if not isinstance(content, str):
            kind = a.get("kind", "binary")
            size = a.get("size_bytes", 0)
            parts.append(f"--- [{name}] ({kind}, {size} bytes, no content) ---")
            continue
        budget = max_chars - used
        if budget < 200:
            parts.append(f"--- [{name}] (truncated; remaining budget exhausted) ---")
            break
        snippet = content[:budget]
        parts.append(f"--- [{name}] ---\n{snippet}")
        used += len(snippet)
    return "\n\n".join(parts) or "[no artifacts]"


def _parse_response(raw: Any) -> tuple[float, list[Issue], str]:
    """Парс ответа LLM в (score, issues, rationale). Tolerant."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return 5.0, [], ""
    if not isinstance(raw, dict):
        return 5.0, [], ""

    try:
        score = float(raw.get("score", 5))
    except (TypeError, ValueError):
        score = 5.0
    score = max(0.0, min(10.0, score))

    issues: list[Issue] = []
    issues_raw = raw.get("issues") or []
    if isinstance(issues_raw, list):
        for item in issues_raw[:30]:
            if not isinstance(item, dict):
                continue
            msg = str(item.get("message") or "").strip()
            if not msg:
                continue
            issues.append(Issue(
                severity=_coerce_severity(item.get("severity")),
                category=str(item.get("category") or "ai_finding")[:80],
                message=msg[:500],
                location=str(item.get("location") or "")[:200] or None,
                fix_hint=str(item.get("fix_hint") or "")[:300],
            ))

    rationale = str(raw.get("rationale", ""))[:500]
    return score, issues, rationale


class AIJudgeValidator(Validator):
    name = "ai_judge"

    def __init__(self, *, llm_router: Any = None) -> None:
        self.llm_router = llm_router

    async def validate(
        self,
        *,
        task_description: str,
        tz_markdown: str,
        artifacts: list[dict[str, Any]],
        brand_context_block: str = "",
        **_kwargs: Any,
    ) -> ValidatorReport:
        report = ValidatorReport(validator=self.name, score=5.0)

        if self.llm_router is None:
            report.error = "no llm_router"
            report.summary = "AI judge unavailable — falling back to neutral score"
            return report

        if not artifacts:
            report.score = 0.0
            report.summary = "no artifacts to judge"
            report.issues.append(Issue(
                severity=IssueSeverity.BLOCKER,
                category="empty_artifacts",
                message="executor вернул пустой список artifacts",
            ))
            return report

        artifacts_text = _summarize_artifacts(artifacts)

        prompt_parts = [
            f"<original_task>\n{task_description.strip()[:3000]}\n</original_task>",
            f"<tz>\n{tz_markdown.strip()[:18000]}\n</tz>",
            f"<artifacts>\n{artifacts_text}\n</artifacts>",
        ]
        if brand_context_block:
            prompt_parts.append(brand_context_block)
        prompt_parts.append("Верни JSON-объект согласно правилам system prompt'а.")
        prompt = "\n\n".join(prompt_parts)

        try:
            raw = await self.llm_router.generate_json(
                prompt=prompt,
                system_prompt=_AI_JUDGE_SYSTEM_PROMPT,
                temperature=0,
                max_tokens=2000,
            )
        except Exception as exc:
            logger.warning("ai_judge: LLM call failed: %s", exc)
            report.error = f"LLM call failed: {exc}"
            report.summary = "AI judge LLM error — falling back to neutral score"
            return report

        score, issues, rationale = _parse_response(raw)
        report.score = score
        report.issues = issues
        report.summary = rationale or f"AI judge score={score}"
        report.metadata = {"raw_score": score, "issue_count": len(issues)}
        return report


__all__ = ["AIJudgeValidator"]
