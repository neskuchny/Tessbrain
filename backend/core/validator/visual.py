"""Visual-diff validator (W21 + W22 full implementation).

Pipeline:
1. Найти renderable artifacts (HTML / URL)
2. Render через Playwright на разных viewports (desktop + mobile)
3. Multimodal LLM (Qwen2-VL / GPT-4V / Claude vision) оценивает
   соответствие ТЗ
4. Score + concrete UI issues → ValidatorReport

Graceful degradation:
- Playwright не установлен → score 5.0 + tool_missing
- Multimodal LLM не сконфигурен → score 5.0 + tool_missing
- Render отдельного viewport упал → продолжаем с остальными
- Multimodal вернул не-JSON → используем raw text как summary

Конфигурация — через env (см. core/llm/multimodal.py):
  MULTIMODAL_BASE_URL, MULTIMODAL_MODEL, MULTIMODAL_API_KEY
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from backend.core.validator.base import (
    Issue,
    IssueSeverity,
    Validator,
    ValidatorReport,
)

logger = logging.getLogger(__name__)


_HTML_EXTS = (".html", ".htm")
_DEFAULT_VIEWPORTS = (
    ("desktop", 1440, 900),
    ("mobile", 375, 812),
)


_VISUAL_SYSTEM_PROMPT = """\
Ты — UX/UI критик. Тебе дают: исходную задачу (ТЗ) и screenshots
готового результата на разных viewports. Твоя задача — оценить
соответствие визуала ТЗ.

Что искать:
- Все ли блоки из ТЗ присутствуют визуально (hero, pricing, CTA, ...)
- Адаптивность: mobile-version читаема, кнопки нажимаемы пальцем
- Брендовая консистентность: цвета, типографика, тон визуала
- Иерархия: главный CTA выделяется, важная инфа выше fold
- Очевидные баги: налезающие элементы, обрезанный текст, белые пятна

Severity:
- blocker — кардинально не соответствует ТЗ (нечего показывать)
- error — отсутствует обязательный блок / серьёзный визуальный баг
- warning — нужна полировка
- info — наблюдение

Формат — ТОЛЬКО JSON:
{
  "score": 0..10,
  "verdict": "approve" | "minor" | "major" | "reject",
  "issues": [
    {"severity": "warning", "category": "spacing", "message": "...",
     "location": "hero block", "fix_hint": "..."}
  ],
  "rationale": "1-2 предложения"
}
"""


def _has_renderable_artifact(artifacts: list[dict[str, Any]]) -> bool:
    for a in artifacts or []:
        if not isinstance(a, dict):
            continue
        name = (a.get("name") or "").lower()
        if any(name.endswith(ext) for ext in _HTML_EXTS):
            return True
        if a.get("kind") == "url":
            return True
    return False


def _pick_renderable_source(
    artifacts: list[dict[str, Any]],
) -> Optional[str]:
    """Выбрать первый renderable artifact и вернуть source-string."""
    for a in artifacts or []:
        if not isinstance(a, dict):
            continue
        name = (a.get("name") or "").lower()
        if a.get("kind") == "url":
            url = a.get("url") or a.get("path") or a.get("content") or ""
            if isinstance(url, str) and url.strip():
                return url.strip()
        if any(name.endswith(ext) for ext in _HTML_EXTS):
            content = a.get("content") or a.get("path")
            if isinstance(content, str) and content.strip():
                return content.strip()
    return None


def _coerce_severity(raw: Any) -> IssueSeverity:
    if isinstance(raw, str):
        try:
            return IssueSeverity(raw.strip().lower())
        except ValueError:
            pass
    return IssueSeverity.WARNING


def _parse_judge_response(content: Any) -> tuple[float, list[Issue], str]:
    """Распарс multimodal-judge JSON в (score, issues, rationale)."""
    if not isinstance(content, dict):
        return 5.0, [], ""
    try:
        score = float(content.get("score", 5))
    except (TypeError, ValueError):
        score = 5.0
    score = max(0.0, min(10.0, score))

    issues: list[Issue] = []
    raw_issues = content.get("issues") or []
    if isinstance(raw_issues, list):
        for it in raw_issues[:30]:
            if not isinstance(it, dict):
                continue
            msg = str(it.get("message") or "").strip()
            if not msg:
                continue
            issues.append(Issue(
                severity=_coerce_severity(it.get("severity")),
                category=str(it.get("category") or "ui_finding")[:80],
                message=msg[:500],
                location=str(it.get("location") or "")[:200] or None,
                fix_hint=str(it.get("fix_hint") or "")[:300],
            ))

    rationale = str(content.get("rationale", ""))[:500]
    return score, issues, rationale


class VisualDiffValidator(Validator):
    """Полная имплементация: Playwright + multimodal LLM."""

    name = "visual_diff"

    def __init__(self, *, multimodal_client: Any = None) -> None:
        self.multimodal_client = multimodal_client

    async def validate(
        self,
        *,
        task_description: str,
        tz_markdown: str,
        artifacts: list[dict[str, Any]],
        viewports: tuple = _DEFAULT_VIEWPORTS,
        enterprise_mode: bool = False,
        **_kwargs: Any,
    ) -> ValidatorReport:
        report = ValidatorReport(validator=self.name, score=5.0)

        if not _has_renderable_artifact(artifacts):
            report.score = 10.0
            report.summary = "no renderable HTML/URL artifacts; skipping"
            report.metadata = {"applicable": False}
            return report

        source = _pick_renderable_source(artifacts)
        if not source:
            report.score = 10.0
            report.summary = "renderable artifact found but no usable source"
            report.metadata = {"applicable": False}
            return report

        # Render screenshots.
        try:
            from backend.core.validator.screenshot import (
                ScreenshotError,
                screenshot,
            )
            shots = await screenshot(
                source=source,
                viewports=viewports,
                enterprise_mode=enterprise_mode,
            )
        except ScreenshotError as exc:
            logger.warning("visual_diff: screenshot failed: %s", exc)
            report.summary = f"Playwright unavailable: {exc}"
            report.metadata = {"applicable": True, "tool_missing": "playwright"}
            return report
        except Exception as exc:
            logger.exception("visual_diff: unexpected screenshot error")
            report.error = f"screenshot failed: {exc}"
            report.summary = "screenshot unexpected error"
            return report

        if not shots:
            report.score = 0.0
            report.summary = "all viewport renders failed"
            report.issues.append(Issue(
                severity=IssueSeverity.BLOCKER,
                category="render_failed",
                message="Playwright failed to render any viewport",
            ))
            return report

        # Multimodal judge.
        client = self.multimodal_client
        if client is None:
            try:
                from backend.core.llm.multimodal import MultimodalClient
                client = MultimodalClient()
            except Exception as exc:
                logger.debug("visual_diff: cannot build multimodal client: %s", exc)
                client = None

        if client is None or not getattr(client, "enabled", False):
            report.summary = "multimodal LLM not configured"
            report.metadata = {
                "applicable": True,
                "tool_missing": "multimodal_llm",
                "screenshots": [s.to_metadata_dict() for s in shots],
            }
            return report

        prompt = (
            f"<task>\n{task_description.strip()[:3000]}\n</task>\n\n"
            f"<tz>\n{tz_markdown.strip()[:8000]}\n</tz>\n\n"
            "Я приложил screenshot'ы в порядке: "
            + ", ".join(s.viewport for s in shots)
            + ". Верни JSON-объект согласно правилам system prompt'а."
        )
        try:
            response = await client.judge(
                images=[s.png_bytes for s in shots],
                user_prompt=prompt,
                system_prompt=_VISUAL_SYSTEM_PROMPT,
            )
        except Exception as exc:
            logger.exception("visual_diff: judge call failed")
            report.error = f"multimodal judge failed: {exc}"
            report.summary = "multimodal judge crashed"
            report.metadata = {
                "applicable": True,
                "screenshots": [s.to_metadata_dict() for s in shots],
            }
            return report

        if not response.success:
            report.error = response.error
            report.summary = f"multimodal judge error: {response.error}"
            report.metadata = {
                "applicable": True,
                "screenshots": [s.to_metadata_dict() for s in shots],
            }
            return report

        if response.content is None:
            report.summary = "multimodal returned non-JSON response"
            report.metadata = {
                "applicable": True,
                "raw_text_excerpt": (response.raw_text or "")[:500],
                "screenshots": [s.to_metadata_dict() for s in shots],
            }
            return report

        score, issues, rationale = _parse_judge_response(response.content)
        report.score = score
        report.issues = issues
        report.summary = rationale or f"visual judge score={score}"
        report.metadata = {
            "applicable": True,
            "implemented": True,
            "screenshots": [s.to_metadata_dict() for s in shots],
            "issue_count": len(issues),
        }
        return report


__all__ = ["VisualDiffValidator"]
