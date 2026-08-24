"""Profile curator (W7 user-adaptive responses, hermes-agent-inspired).

После сессии диалога LLM сам решает, что из переписки достойно обновления
профиля пользователя. Решения принимаются дискретными категориями, чтобы
не дать модели «дрейфовать» в произвольные изменения.

Идея взята из hermes-agent: agent-curated memory с явным шагом
«decide what to remember». В отличие от слепого `nightly_consolidation`,
который консолидирует ВСЁ — curator фильтрует шум и записывает только
то, что реально влияет на будущие ответы.

API:
    curator = ProfileCurator(llm_router=...)
    proposals = await curator.curate(messages=[...], current_profile={...})
    # proposals: список (action, field, value, confidence)
    # action ∈ {"set", "append", "remove", "noop"}
    # confidence ∈ [0, 1]; caller сам решает, какой порог применять.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class ProfileUpdate:
    """Одно предложенное обновление профиля от LLM-curator."""

    action: str          # "set" | "append" | "remove" | "noop"
    field: str           # имя поля в UserProfile (e.g. "preferred_language")
    value: Any           # новое значение (для set/append) или элемент (для remove)
    confidence: float    # 0..1, насколько LLM уверен в этом обновлении
    reason: str = ""     # короткое обоснование (для логов/UI/audit)


_ALLOWED_FIELDS = frozenset({
    # Безопасный список полей, которые curator может менять.
    # Намеренно НЕ включаем user_id, email — это identity-поля.
    "communication_style",
    "detail_level",
    "preferred_language",
    "expertise",
    "known_topics",
    "blind_spots",
    "current_projects",
    "current_focus",
    "typical_query_types",
    "decision_style",
})

_ALLOWED_ACTIONS = frozenset({"set", "append", "remove", "noop"})

_CURATE_SYSTEM_PROMPT = """\
Ты — куратор профиля пользователя в корпоративной системе. На вход тебе дают
последнюю переписку и текущий профиль. Твоя задача — найти ТОЛЬКО устойчивые
сигналы о предпочтениях/контексте пользователя и предложить минимальные
обновления.

Правила:
1. Не выдумывай. Если из переписки нельзя достоверно сделать вывод — пропускай.
2. Не меняй identity (имя, email). Только preferences и context.
3. Никогда не понижай confidence ниже 0.5 — это будет отброшено.
4. Возвращай JSON-список UpdateProposal. Если ничего достойного — пустой список.

Допустимые поля:
- communication_style ∈ {formal, casual, technical, executive}
- detail_level ∈ {brief, standard, detailed}
- preferred_language: код языка (ru, en, de, ...)
- expertise: список тем/доменов
- known_topics: список тем, в которых пользователь силён
- blind_spots: список тем, которые требуют объяснения с нуля
- current_projects: список текущих проектов
- current_focus: что в фокусе СЕЙЧАС
- typical_query_types: типы вопросов, которые пользователь обычно задаёт
- decision_style ∈ {data-driven, intuitive, consensus-seeking}

Допустимые actions:
- "set"     — присвоить полю значение (для скаляров)
- "append"  — добавить элемент в список (для списочных полей)
- "remove"  — убрать элемент из списка
- "noop"    — ничего не менять (для отладки)

Формат:
[
  {"action": "set", "field": "preferred_language", "value": "en",
   "confidence": 0.85, "reason": "user wrote in English in last 3 messages"},
  ...
]
"""


class ProfileCurator:
    """LLM-куратор профиля пользователя.

    Не зависит от хранилища — возвращает ProposedUpdate'ы. Применение
    delegate'ит UserProfileService (или другому слою), чтобы curator
    оставался pure logic.
    """

    def __init__(self, llm_router=None, *, min_confidence: float = 0.6) -> None:
        self.llm_router = llm_router
        self.min_confidence = min_confidence

    async def curate(
        self,
        *,
        messages: list[dict[str, str]],
        current_profile: dict[str, Any],
    ) -> list[ProfileUpdate]:
        """Проанализировать переписку и вернуть список proposals.

        Args:
            messages: history в формате [{"role": "user"|"assistant", "content": "..."}]
            current_profile: текущий профиль как dict (см. UserProfile.to_dict()).
        """
        if not messages:
            return []
        if self.llm_router is None:
            logger.debug("ProfileCurator: no llm_router, skipping")
            return []

        prompt = self._build_prompt(messages=messages, current_profile=current_profile)

        try:
            # temperature=0 → curator должен быть детерминирован
            # (заодно поедет prompt-cache из W2).
            raw = await self.llm_router.generate_json(
                prompt=prompt,
                system_prompt=_CURATE_SYSTEM_PROMPT,
                temperature=0,
                max_tokens=2000,
            )
        except Exception as exc:
            logger.warning("ProfileCurator: LLM call failed: %s", exc)
            return []

        return self._parse_proposals(raw)

    def _build_prompt(
        self,
        *,
        messages: list[dict[str, str]],
        current_profile: dict[str, Any],
    ) -> str:
        # Обрезаем messages до последних N — длинные сессии не должны
        # перегружать curator.
        tail = messages[-20:]
        history_lines = []
        for m in tail:
            role = m.get("role", "user")
            content = (m.get("content") or "")[:500]
            history_lines.append(f"[{role}] {content}")
        history_str = "\n".join(history_lines)

        # Профиль сериализуем компактно.
        safe_profile = {
            k: v for k, v in current_profile.items()
            if k in _ALLOWED_FIELDS or k in {"user_id", "name", "role"}
        }

        return (
            f"<current_profile>\n{json.dumps(safe_profile, ensure_ascii=False, indent=2)}\n</current_profile>\n\n"
            f"<recent_messages>\n{history_str}\n</recent_messages>\n\n"
            "Вернёт JSON-список predict'ов согласно правилам в system prompt'е."
        )

    def _parse_proposals(self, raw: Any) -> list[ProfileUpdate]:
        """Привести ответ LLM к списку ProfileUpdate, отфильтровав мусор."""
        items: list[Any] = []
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            # Иногда модель возвращает {"updates": [...]} вместо чистого list'а.
            items = raw.get("updates") or raw.get("proposals") or []

        out: list[ProfileUpdate] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            action = str(item.get("action", "noop"))
            field = str(item.get("field", ""))
            value = item.get("value")
            try:
                confidence = float(item.get("confidence", 0))
            except (TypeError, ValueError):
                confidence = 0.0
            reason = str(item.get("reason", ""))[:200]

            # Гарды.
            if action not in _ALLOWED_ACTIONS:
                logger.debug("ProfileCurator: skip unknown action=%r", action)
                continue
            if action == "noop":
                continue
            if field not in _ALLOWED_FIELDS:
                logger.debug("ProfileCurator: skip non-allowlisted field=%r", field)
                continue
            if confidence < self.min_confidence:
                continue

            out.append(ProfileUpdate(
                action=action,
                field=field,
                value=value,
                confidence=confidence,
                reason=reason,
            ))

        return out


def apply_proposals(profile: dict[str, Any], proposals: list[ProfileUpdate]) -> dict[str, Any]:
    """Применить список ProfileUpdate к dict-профилю.

    Pure function — возвращает новый dict, не мутирует входной.
    Не имеет сетевых вызовов; UserProfileService после этого
    persist'ит результат в БД.
    """
    new = dict(profile)
    for p in proposals:
        if p.action == "set":
            new[p.field] = p.value
        elif p.action == "append":
            current = new.get(p.field) or []
            if not isinstance(current, list):
                logger.warning(
                    "apply_proposals: cannot append to non-list field %r",
                    p.field,
                )
                continue
            if p.value not in current:
                new[p.field] = [*current, p.value]
        elif p.action == "remove":
            current = new.get(p.field) or []
            if isinstance(current, list) and p.value in current:
                new[p.field] = [x for x in current if x != p.value]
    return new
