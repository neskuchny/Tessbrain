"""Brand context: tone-of-voice / стиль из snapshot'ов компании и отдела (W18).

Не отдельная сущность — переиспользуем существующий `EnhancedSnapshot`
слой (CompanySnapshot, DepartmentSnapshot). Здесь только wrapper:
- асинхронный load (best-effort, никогда не raises)
- сериализация в короткий блок текста для injection в Generate-стадию
- LLM сам решает как писать — мы не форсим конкретный стиль, мы даём
  «вот как ваша компания обычно говорит, вот что вы продаёте».
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)


_MAX_FIELD_LEN = 600
_MAX_VOICE_SAMPLES = 3


@dataclass
class BrandContext:
    """Что мы передаём LLM-генератору ТЗ как «контекст компании»."""
    company_name: str = ""
    company_summary: str = ""
    industry: str = ""
    products: list[str] = field(default_factory=list)
    audience: str = ""              # ICP / target audience description
    tone_of_voice: str = ""          # "формальный, экспертный" или free-form
    voice_samples: list[str] = field(default_factory=list)
    department_name: str = ""
    department_focus: str = ""        # roadmap / KPIs / ownership
    has_data: bool = False            # False = ничего полезного не нашли

    def to_dict(self) -> dict[str, Any]:
        return {
            "company_name": self.company_name,
            "company_summary": self.company_summary,
            "industry": self.industry,
            "products": list(self.products),
            "audience": self.audience,
            "tone_of_voice": self.tone_of_voice,
            "voice_samples": list(self.voice_samples),
            "department_name": self.department_name,
            "department_focus": self.department_focus,
            "has_data": self.has_data,
        }

    def to_prompt_block(self) -> str:
        """Скомпилировать в короткий текст-блок для system_prompt'а.

        Empty если has_data=False — caller должен решить inject'ить или нет.
        """
        if not self.has_data:
            return ""
        lines: list[str] = ["<brand_context>"]
        if self.company_name:
            lines.append(f"company: {self.company_name}")
        if self.industry:
            lines.append(f"industry: {self.industry}")
        if self.company_summary:
            lines.append(f"summary: {_clip(self.company_summary)}")
        if self.products:
            lines.append("products: " + ", ".join(self.products[:8]))
        if self.audience:
            lines.append(f"audience: {_clip(self.audience)}")
        if self.tone_of_voice:
            lines.append(f"tone: {_clip(self.tone_of_voice, 200)}")
        if self.voice_samples:
            lines.append("voice_samples (примеры как мы обычно пишем):")
            for i, sample in enumerate(self.voice_samples[:_MAX_VOICE_SAMPLES], 1):
                lines.append(f"  [{i}] {_clip(sample, 300)}")
        if self.department_name:
            lines.append(f"department: {self.department_name}")
        if self.department_focus:
            lines.append(f"department_focus: {_clip(self.department_focus, 300)}")
        lines.append("</brand_context>")
        lines.append(
            "Use this context to match the company's voice. Don't force "
            "a specific style — adapt naturally based on the samples."
        )
        return "\n".join(lines)


def _clip(text: str, length: int = _MAX_FIELD_LEN) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= length:
        return text
    return text[:length].rstrip() + "…"


def from_snapshots(
    *,
    company_snapshot: Optional[Mapping[str, Any]] = None,
    department_snapshot: Optional[Mapping[str, Any]] = None,
) -> BrandContext:
    """Сконструировать BrandContext из dict-снапшотов.

    Толерантно к разным схемам — пытаемся понять знакомые ключи и
    fall through. Никаких raises.
    """
    ctx = BrandContext()

    if isinstance(company_snapshot, Mapping):
        ctx.company_name = str(
            company_snapshot.get("name") or company_snapshot.get("company_name") or ""
        )
        ctx.company_summary = str(
            company_snapshot.get("summary")
            or company_snapshot.get("description")
            or company_snapshot.get("about") or ""
        )
        ctx.industry = str(company_snapshot.get("industry") or "")
        products = (
            company_snapshot.get("products")
            or company_snapshot.get("offerings")
            or []
        )
        if isinstance(products, list):
            ctx.products = [str(p) for p in products if p][:10]
        ctx.audience = str(
            company_snapshot.get("audience")
            or company_snapshot.get("icp")
            or company_snapshot.get("target_audience") or ""
        )
        ctx.tone_of_voice = str(
            company_snapshot.get("tone_of_voice")
            or company_snapshot.get("voice")
            or company_snapshot.get("brand_voice") or ""
        )
        samples = (
            company_snapshot.get("voice_samples")
            or company_snapshot.get("brand_samples")
            or []
        )
        if isinstance(samples, list):
            ctx.voice_samples = [str(s) for s in samples if s][:_MAX_VOICE_SAMPLES]

    if isinstance(department_snapshot, Mapping):
        ctx.department_name = str(
            department_snapshot.get("name")
            or department_snapshot.get("department_name") or ""
        )
        ctx.department_focus = str(
            department_snapshot.get("focus")
            or department_snapshot.get("summary")
            or department_snapshot.get("kpis") or ""
        )

    ctx.has_data = bool(
        ctx.company_name or ctx.company_summary or ctx.products
        or ctx.tone_of_voice or ctx.voice_samples or ctx.department_focus
    )
    return ctx


async def load_brand_context(
    *,
    user_id: Optional[str] = None,
    department_id: Optional[str] = None,
    snapshot_loader: Any = None,
) -> BrandContext:
    """Best-effort load: тянем CompanySnapshot и (опц) DepartmentSnapshot
    через caller-supplied loader.

    Дизайн: НЕ хардкодим какой именно loader/сервис использовать — caller
    передаёт что-то с методами `.load_company()` и `.load_department(id)`.
    Это позволяет protect'нуть unit-тесты и держать модуль легковесным.

    При любой ошибке возвращаем empty BrandContext (`has_data=False`).
    """
    if snapshot_loader is None:
        return BrandContext()

    company = department = None
    try:
        if hasattr(snapshot_loader, "load_company"):
            company = await snapshot_loader.load_company(user_id=user_id)
    except Exception as exc:
        logger.debug("brand_context: load_company failed: %s", exc)
    try:
        if department_id and hasattr(snapshot_loader, "load_department"):
            department = await snapshot_loader.load_department(department_id)
    except Exception as exc:
        logger.debug("brand_context: load_department failed: %s", exc)

    return from_snapshots(
        company_snapshot=company,
        department_snapshot=department,
    )


__all__ = [
    "BrandContext",
    "from_snapshots",
    "load_brand_context",
]
