"""TZ iteration — LLM-driven delta для конкретных блоков (W18).

Сценарий: юзер получил готовое ТЗ, прочитал, говорит «hero не нравится,
у нас 50K клиентов не 5K, исправь». Мы:
1. Парсим feedback → понимаем какой(ие) блок(и) затронуты + что просят
2. Запускаем LLM с (full ТЗ + feedback + затронутый блок) → возвращает
   обновлённую версию ТОЛЬКО этого блока (или нескольких)
3. Caller вставляет revised блок обратно в markdown

НЕ перегенерируем ТЗ целиком — это и быстрее (1 блок ≈ 200-400 токенов
output) и стабильнее (остальные блоки не сдвигаются).

Дизайн:
- Pure helpers; LLM передаётся caller'ом.
- Markdown-aware: парсим H1/H2 заголовки чтобы найти block boundaries.
- Толерантно к разным стилям markdown (## Hero / ### hero / **Hero**).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


_DELTA_SYSTEM_PROMPT = """\
Ты — редактор технических заданий. Тебе дают ТЗ (Markdown) и feedback
от пользователя. Найди какой блок(и) пользователь хочет изменить и
верни обновлённую версию ТОЛЬКО этих блоков, сохранив всю смысловую
структуру (заголовки, списки, метрики).

Правила:
1. Не трогай блоки которые пользователь не упомянул.
2. Сохраняй стиль и тон оригинального ТЗ.
3. Если пользователь даёт конкретные цифры или факты — используй их
   дословно. Не округляй, не интерпретируй.
4. Заголовок блока (H2/H3 ~ "## ..." или "### ...") сохраняй как было.

Формат — ТОЛЬКО JSON:
{
  "affected_blocks": ["hero"],     # имена блоков (lower-case slug)
  "revised_markdown": {
     "hero": "## Hero\\n\\n... обновлённое содержимое ...",
     "other_block": "..."
  },
  "summary_for_user": "Заменил цифру 5K на 50K в hero subheadline."
}

Если пользователь просит что-то не относящееся к блокам — верни
`affected_blocks: []` и `revised_markdown: {}`, а в summary объясни
почему.
"""


# Markdown-headers: до 4 уровней, поддерживаем `**Hero**` тоже.
_HEADER_RE = re.compile(
    r"^(?:#{1,4}\s+|\*\*\s*)(?P<title>.+?)(?:\s*\*\*)?\s*$",
    re.MULTILINE,
)


@dataclass
class BlockSection:
    """Один распарсенный блок markdown'а."""
    name: str          # lower-case slug
    title: str          # original heading text
    start: int          # offset начала секции (включая heading)
    end: int            # offset конца (исключая)
    content: str        # raw markdown секции

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "start": self.start,
            "end": self.end,
            "length": len(self.content),
        }


def _slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^\w\s\-]", "", text)
    text = re.sub(r"[\s\-]+", "_", text)
    return text.strip("_")[:64]


def parse_blocks(markdown: str) -> list[BlockSection]:
    """Разрезать markdown на блоки по заголовкам.

    Если в тексте нет заголовков — возвращаем один блок name='body'.
    """
    if not markdown:
        return []
    matches = list(_HEADER_RE.finditer(markdown))
    if not matches:
        return [BlockSection(
            name="body", title="body",
            start=0, end=len(markdown), content=markdown,
        )]

    out: list[BlockSection] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        title = m.group("title").strip()
        out.append(BlockSection(
            name=_slugify(title) or f"block_{i}",
            title=title,
            start=start,
            end=end,
            content=markdown[start:end],
        ))
    return out


@dataclass
class DeltaResult:
    affected_blocks: list[str] = field(default_factory=list)
    revised_markdown_per_block: dict[str, str] = field(default_factory=dict)
    summary_for_user: str = ""
    raw_response: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "affected_blocks": list(self.affected_blocks),
            "revised_markdown_per_block": dict(self.revised_markdown_per_block),
            "summary_for_user": self.summary_for_user,
            "error": self.error,
        }


def apply_delta(
    original_markdown: str,
    delta: DeltaResult,
) -> str:
    """Применить delta к оригиналу. Если что-то не сходится — возвращаем оригинал.

    Алгоритм:
    1. Парсим оригинал на блоки.
    2. Для каждого `affected_blocks` ищем блок по name (slug) — заменяем
       его content на `revised_markdown_per_block[name]`.
    3. Не упомянутые блоки оставляем как были.
    4. Если revised блок не найден — добавляем в конец (но логируем warn).
    """
    if not delta.affected_blocks or not delta.revised_markdown_per_block:
        return original_markdown
    blocks = parse_blocks(original_markdown)
    if not blocks:
        return original_markdown

    by_name = {b.name: b for b in blocks}

    # Собираем результат, заменяя нужные блоки.
    out_parts: list[str] = []
    for b in blocks:
        if b.name in delta.affected_blocks:
            replacement = delta.revised_markdown_per_block.get(b.name)
            if replacement:
                out_parts.append(replacement.rstrip() + "\n\n")
                continue
        out_parts.append(b.content if b.content.endswith("\n") else b.content + "\n")

    # Если в delta был блок которого нет в оригинале — append'нём в конец.
    extra_blocks = [
        name for name in delta.affected_blocks if name not in by_name
    ]
    for name in extra_blocks:
        rep = delta.revised_markdown_per_block.get(name)
        if rep:
            logger.warning(
                "iteration.apply_delta: block %r not in original; appending",
                name,
            )
            out_parts.append("\n" + rep.rstrip() + "\n")

    return "".join(out_parts).rstrip() + "\n"


def _parse_response(raw: Any) -> DeltaResult:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return DeltaResult(error="LLM returned non-JSON")
    if not isinstance(raw, dict):
        return DeltaResult(error=f"LLM response is {type(raw).__name__}, expected dict")

    affected = raw.get("affected_blocks") or []
    if not isinstance(affected, list):
        affected = []
    affected = [str(b).strip().lower() for b in affected if b]

    revised_raw = raw.get("revised_markdown") or {}
    revised: dict[str, str] = {}
    if isinstance(revised_raw, dict):
        for k, v in revised_raw.items():
            if isinstance(v, str) and v.strip():
                revised[str(k).strip().lower()] = v

    summary = str(raw.get("summary_for_user", ""))[:500]

    return DeltaResult(
        affected_blocks=affected,
        revised_markdown_per_block=revised,
        summary_for_user=summary,
        raw_response=json.dumps(raw, ensure_ascii=False)[:2000],
    )


async def generate_delta(
    *,
    original_markdown: str,
    user_feedback: str,
    brand_context_block: str = "",
    llm_router: Any,
) -> DeltaResult:
    """Сгенерить delta для feedback'а пользователя.

    Никогда не raises — error попадает в `DeltaResult.error`.
    """
    if not original_markdown.strip():
        return DeltaResult(error="empty original markdown")
    if not user_feedback.strip():
        return DeltaResult(error="empty user feedback")
    if llm_router is None:
        return DeltaResult(error="no llm_router provided")

    prompt_parts = [
        f"<original_tz>\n{original_markdown.strip()[:24000]}\n</original_tz>",
        f"<user_feedback>\n{user_feedback.strip()[:2000]}\n</user_feedback>",
    ]
    if brand_context_block:
        prompt_parts.append(brand_context_block)
    prompt_parts.append("Верни JSON-объект согласно правилам system prompt'а.")
    prompt = "\n\n".join(prompt_parts)

    try:
        raw = await llm_router.generate_json(
            prompt=prompt,
            system_prompt=_DELTA_SYSTEM_PROMPT,
            temperature=0,
            max_tokens=4000,
        )
    except Exception as exc:
        logger.warning("iteration.generate_delta: LLM call failed: %s", exc)
        return DeltaResult(error=f"LLM call failed: {exc}")

    return _parse_response(raw)


__all__ = [
    "BlockSection",
    "DeltaResult",
    "apply_delta",
    "generate_delta",
    "parse_blocks",
]
