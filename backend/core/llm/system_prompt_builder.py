"""System prompt builder — обогащает базовый system prompt профилем пользователя.

Используется в LLMRouter.generate(personalize=True). Финальный prompt =
base_system + краткий personality preamble. Преамбула короткая (≤500 chars)
чтобы не съедать context window и не мешать prompt-cache (Phase 4b).

Quality guarantees:
- Preamble добавляется ТОЛЬКО при personalize=True. Default behavior — без
  изменений (cache hit-rate W4b сохраняется).
- Preamble генерится detalистично из dict-профиля; нет LLM-вызова.
- Empty/missing профиль — preamble не добавляется (no-op).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Хедер preamble — заметный для модели разделитель.
_PREAMBLE_HEADER = "User context (auto-curated, optional, follow if relevant):"


def build_personalization_preamble(profile: Optional[dict[str, Any]]) -> str:
    """Сформировать короткое описание пользователя для system prompt'а.

    Возвращает пустую строку, если профиль None / отсутствуют значимые поля.
    """
    if not profile:
        return ""

    lines: list[str] = []

    # Identity / role context — короткой строкой.
    role = profile.get("role")
    department = profile.get("department")
    if role or department:
        bits = [role, department]
        lines.append("- role: " + ", ".join(b for b in bits if b))

    # Preferences — главный сигнал для адаптации стиля.
    style = profile.get("communication_style")
    detail = profile.get("detail_level")
    lang = profile.get("preferred_language")
    pref_bits = []
    if style:
        pref_bits.append(f"style={_str(style)}")
    if detail:
        pref_bits.append(f"detail={_str(detail)}")
    if lang:
        pref_bits.append(f"lang={lang}")
    if pref_bits:
        lines.append("- prefs: " + ", ".join(pref_bits))

    # Контекст — что сейчас в фокусе.
    focus = profile.get("current_focus")
    projects = profile.get("current_projects") or []
    if focus or projects:
        ctx_bits = []
        if focus:
            ctx_bits.append(f"focus='{focus}'")
        if projects:
            ctx_bits.append("projects=" + ", ".join(projects[:3]))
        lines.append("- context: " + "; ".join(ctx_bits))

    # Экспертиза — чтобы LLM знал, какой уровень объяснений использовать.
    expertise = profile.get("expertise") or []
    known = profile.get("known_topics") or []
    if expertise or known:
        exp_bits = []
        if expertise:
            exp_bits.append("expert in: " + ", ".join(expertise[:5]))
        if known:
            exp_bits.append("knows: " + ", ".join(known[:5]))
        lines.append("- expertise: " + "; ".join(exp_bits))

    # Slep'ые пятна — где НЕЛЬЗЯ опускать объяснения.
    blind = profile.get("blind_spots") or []
    if blind:
        lines.append("- explain-when-mentioned: " + ", ".join(blind[:5]))

    # Decision style — говорит модели, как структурировать рекомендации.
    decision = profile.get("decision_style")
    if decision:
        lines.append(f"- decision-style: {decision}")

    if not lines:
        return ""

    body = "\n".join(lines)
    # Жёсткий cap — не перегружаем context.
    if len(body) > 800:
        body = body[:800] + "..."

    return f"{_PREAMBLE_HEADER}\n{body}"


def _str(value: Any) -> str:
    """Унифицированный str() для enum-like объектов."""
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def merge_with_base(base_system: Optional[str], preamble: str) -> Optional[str]:
    """Соединить базовый system prompt с personalization preamble.

    Преамбула идёт ПЕРЕД основным prompt'ом, чтобы модель прочитала
    про юзера до бизнес-инструкций. Если базовый prompt пустой —
    возвращаем только преамбулу.
    """
    if not preamble:
        return base_system
    if not base_system:
        return preamble
    return f"{preamble}\n\n{base_system}"
