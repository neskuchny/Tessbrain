"""LLM-enricher для bookmarks (W17).

Берёт `FetchResult` и юзерскую подсказку (опц), просит LLM сгенерить:
- title (если со страницы не вытащился)
- description (1-3 строки, на каком языке — ИИ сам выберет по контенту)
- tags (3-7 коротких меток)

Дизайн:
- Pure delegating: caller передаёт llm_router; модуль просто формирует
  prompt и парсит JSON.
- temperature=0 → детерминизм + prompt-cache hit на повторных URL'ах.
- При любой ошибке LLM возвращаем `EnrichResult` с тем что есть (fallback
  на page title, пустые tags) — bookmark не должен «не сохраниться»
  только потому что LLM лежит.
- Не делаем свои embeddings здесь — это делает BookmarkService через
  существующий VectorIndexer/ollama_embeddings слой.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


_ENRICH_SYSTEM_PROMPT = """\
Ты — ассистент которому показывают URL и текст страницы. Извлеки структурированные
метаданные для регистрации этой ссылки в корпоративной базе знаний.

Твоя цель — чтобы коллеги через несколько месяцев могли найти эту ссылку
поиском по «что в ней» (а не по точному URL).

Правила:
1. title: короткое (≤120 символов), описывает суть страницы. Если на странице
   уже есть нормальный <title> — можешь его использовать.
2. description: 1-3 предложения. На каком языке писать — определяй по тексту
   страницы (русский/английский/...).
3. tags: 3-7 коротких меток, каждая 1-3 слова, lowercase, существительные/
   терминология. Без хеш-символов. Примеры: "marketing presentation",
   "методология продаж", "api reference", "onboarding guide".
4. Не выдумывай содержимое. Если текста мало — отдай tag "unknown" и короткий
   description с упоминанием домена/URL.

Формат ответа — ТОЛЬКО JSON:
{
  "title": "...",
  "description": "...",
  "tags": ["...", "..."]
}
"""


@dataclass
class EnrichResult:
    title: str
    description: str
    tags: list[str]
    raw_response: Optional[str] = None  # для debug

    def to_dict(self) -> dict[str, Any]:
        return {"title": self.title, "description": self.description, "tags": list(self.tags)}


def _coerce_tags(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for v in value:
        if not isinstance(v, str):
            continue
        t = v.strip().lstrip("#").lower()
        if not t or t in seen:
            continue
        if len(t) > 50:
            t = t[:50]
        seen.add(t)
        out.append(t)
        if len(out) >= 10:
            break
    return out


def _fallback(
    *,
    page_title: str,
    user_hint: Optional[str],
    url: str,
) -> EnrichResult:
    """Если LLM упал / не дал валидный JSON — возвращаем что есть."""
    title = (page_title or user_hint or url)[:200]
    description = (user_hint or page_title or url)[:500]
    return EnrichResult(title=title, description=description, tags=[])


async def enrich(
    *,
    url: str,
    page_title: str,
    page_text: str,
    user_hint: Optional[str] = None,
    llm_router: Any,
) -> EnrichResult:
    """Сгенерить title + description + tags через LLM.

    Args:
        url: исходный URL — для контекста LLM.
        page_title: <title> со страницы (может быть пустым).
        page_text: plain-text тело страницы (обрезано caller'ом до ~20K).
        user_hint: что юзер написал сам при добавлении (опц., имеет
                   приоритет над LLM-выдумкой при пустом тексте).
        llm_router: LLMRouter с `generate_json()`.

    Returns:
        EnrichResult — никогда не raises.
    """
    if llm_router is None:
        return _fallback(page_title=page_title, user_hint=user_hint, url=url)

    snippet = (page_text or "")[:8000]
    prompt = (
        f"<url>{url}</url>\n"
        f"<page_title>{page_title or ''}</page_title>\n"
        + (f"<user_hint>{user_hint}</user_hint>\n" if user_hint else "")
        + f"<page_text>\n{snippet}\n</page_text>\n\n"
        "Верни JSON-объект согласно правилам system prompt'а."
    )

    try:
        raw = await llm_router.generate_json(
            prompt=prompt,
            system_prompt=_ENRICH_SYSTEM_PROMPT,
            temperature=0,
            max_tokens=600,
        )
    except Exception as exc:
        logger.warning("bookmark enricher: LLM call failed: %s", exc)
        return _fallback(page_title=page_title, user_hint=user_hint, url=url)

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("bookmark enricher: LLM returned non-JSON string")
            return _fallback(page_title=page_title, user_hint=user_hint, url=url)
    if not isinstance(raw, dict):
        return _fallback(page_title=page_title, user_hint=user_hint, url=url)

    title = (str(raw.get("title") or "").strip()
             or page_title.strip()
             or (user_hint or "").strip()
             or url)[:200]
    description = (str(raw.get("description") or "").strip()
                   or (user_hint or "").strip()
                   or page_title.strip())[:1000]
    tags = _coerce_tags(raw.get("tags"))

    return EnrichResult(
        title=title,
        description=description,
        tags=tags,
        raw_response=json.dumps(raw, ensure_ascii=False)[:1000],
    )


__all__ = ["EnrichResult", "enrich"]
