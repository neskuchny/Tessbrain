"""TZ sufficiency-judge — агент оценивает «достаточно ли ТЗ для executor'а» (W18).

Не хардкод-чеклист (как договаривались), а LLM-судья. Принимает:
- сгенерированный markdown ТЗ
- задачу (исходную просьбу пользователя)
- (опц) template со списком required-блоков

Возвращает:
- `score` ∈ [0..10]
- `verdict` ∈ {"sufficient", "needs_clarification", "insufficient"}
- `gaps` — что ещё нужно прояснить (вопросы к юзеру или чем дополнить контекст)
- `concrete_issues` — конкретные проблемы (противоречия, нечёткие cifры, missing
  блоки, "?" placeholders в тексте)

Дизайн:
- temperature=0 → детерминизм + prompt-cache.
- Безопасный fallback: при ошибке LLM возвращаем `score=5, verdict="needs_clarification"`
  (не блокируем generation pipeline — caller может продолжить вручную).
- Идемпотентен: пере-judge не меняет вердикт если ТЗ не менялось.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


_JUDGE_SYSTEM_PROMPT = """\
Ты — критик технических заданий (ТЗ). Тебе дают исходную задачу и
сгенерированное ТЗ. Твоя цель — оценить, сможет ли AI-исполнитель
(агентная система типа Claude Code, OpenHands, Codex) выполнить эту
задачу от начала до конца БЕЗ дополнительных уточнений у человека.

Что значит «достаточно для executor'а»:
- Указаны конкретные требования: размеры, тексты, структура, шаги.
- Нет магических плейсхолдеров типа `[TBD]`, `???`, `<заполнить>`,
  `{{insert here}}`.
- Числа — реальные, не «примерно X». Если число неизвестно, должен быть
  явный assumption с источником.
- Нет противоречий между блоками.
- Все required-блоки покрыты содержательно (не «TBD»).

Шкала score:
- 9-10 — executor пойдёт и сделает без вопросов.
- 7-8 — minor gaps, можно отдавать с пометкой assumptions.
- 5-6 — нужны 1-2 уточнения у человека, не больше.
- 3-4 — много пробелов, придётся переспрашивать каждый блок.
- 1-2 — практически unusable.

Verdict:
- "sufficient" — score ≥ 7
- "needs_clarification" — 4..6
- "insufficient" — ≤ 3

Формат ответа — ТОЛЬКО JSON:
{
  "score": 0..10,
  "verdict": "sufficient" | "needs_clarification" | "insufficient",
  "gaps": ["вопрос или нужный контекст 1", "..."],
  "concrete_issues": ["проблема 1", "..."],
  "rationale": "1-2 предложения почему такой score"
}
"""


@dataclass
class JudgeResult:
    score: float
    verdict: str
    gaps: list[str] = field(default_factory=list)
    concrete_issues: list[str] = field(default_factory=list)
    rationale: str = ""
    raw_response: Optional[str] = None

    @property
    def is_sufficient(self) -> bool:
        return self.verdict == "sufficient"

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "verdict": self.verdict,
            "gaps": list(self.gaps),
            "concrete_issues": list(self.concrete_issues),
            "rationale": self.rationale,
            "is_sufficient": self.is_sufficient,
        }


def _coerce_verdict(raw: Any, score: float) -> str:
    """Нормализовать verdict; fallback по score если LLM забыл."""
    if isinstance(raw, str):
        v = raw.strip().lower().replace("-", "_")
        if v in {"sufficient", "needs_clarification", "insufficient"}:
            return v
    # Fallback по score
    if score >= 7:
        return "sufficient"
    if score >= 4:
        return "needs_clarification"
    return "insufficient"


def _coerce_score(raw: Any) -> float:
    try:
        s = float(raw)
    except (TypeError, ValueError):
        return 5.0
    return max(0.0, min(10.0, s))


def _coerce_str_list(raw: Any, max_items: int = 20) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append(item.strip()[:500])
        if len(out) >= max_items:
            break
    return out


def _parse_response(raw: Any) -> JudgeResult:
    """Защищённый parser — никогда не raises."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("validator_judge: LLM returned non-JSON string")
            raw = {}
    if not isinstance(raw, dict):
        raw = {}

    score = _coerce_score(raw.get("score", 5))
    verdict = _coerce_verdict(raw.get("verdict"), score)
    gaps = _coerce_str_list(raw.get("gaps"))
    issues = _coerce_str_list(raw.get("concrete_issues"))
    rationale = str(raw.get("rationale", "") or "")[:500]

    return JudgeResult(
        score=score,
        verdict=verdict,
        gaps=gaps,
        concrete_issues=issues,
        rationale=rationale,
        raw_response=json.dumps(raw, ensure_ascii=False)[:1500] if raw else None,
    )


def _fallback_result() -> JudgeResult:
    """Когда LLM упал / роутера нет — middle-of-the-road verdict.

    Не sufficient (чтобы caller не отдал ТЗ executor'у вслепую) и не
    insufficient (чтобы не блокировать UI).
    """
    return JudgeResult(
        score=5.0,
        verdict="needs_clarification",
        gaps=["validator_judge LLM unavailable; manual review recommended"],
    )


async def judge_specification(
    *,
    task_description: str,
    tz_markdown: str,
    template_required_blocks: Optional[list[str]] = None,
    llm_router: Any = None,
) -> JudgeResult:
    """Запустить sufficiency-judge на готовом ТЗ.

    Args:
        task_description: исходная задача от пользователя.
        tz_markdown: сгенерированный markdown ТЗ.
        template_required_blocks: список имён required-блоков из шаблона
            (если используется templates), для подсказки judge'у что
            должно быть покрыто.
        llm_router: LLMRouter с `generate_json()`.
    """
    if llm_router is None:
        return _fallback_result()
    if not tz_markdown.strip():
        return JudgeResult(
            score=0.0,
            verdict="insufficient",
            gaps=["empty ТЗ"],
            concrete_issues=["ТЗ is empty"],
        )

    required_hint = ""
    if template_required_blocks:
        required_hint = (
            "\n\nRequired blocks per template (должны быть содержательно покрыты):\n  "
            + ", ".join(template_required_blocks)
        )

    prompt = (
        f"<original_task>\n{task_description.strip()[:4000]}\n</original_task>\n\n"
        f"<generated_tz>\n{tz_markdown.strip()[:24000]}\n</generated_tz>"
        + required_hint
        + "\n\nВерни JSON-объект согласно правилам system prompt'а."
    )

    try:
        raw = await llm_router.generate_json(
            prompt=prompt,
            system_prompt=_JUDGE_SYSTEM_PROMPT,
            temperature=0,
            max_tokens=1200,
        )
    except Exception as exc:
        logger.warning("validator_judge: LLM call failed: %s", exc)
        return _fallback_result()

    return _parse_response(raw)


__all__ = ["JudgeResult", "judge_specification"]
