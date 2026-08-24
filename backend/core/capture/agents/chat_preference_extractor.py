# -*- coding: utf-8 -*-
"""
Chat Preference Extractor — извлечение предпочтений пользователя из диалогов.

Работает в двух режимах:
1. **Реал-тайм (лёгкий)**: После каждого сообщения — быстрая проверка
   на наличие явных предпочтений/правил (regex + эвристика, без LLM).
2. **Batch (ночной)**: Раз в день/ночь — глубокий LLM-анализ серии
   сообщений, извлечение неявных предпочтений, обновление Rule Book.

Извлекаемые данные:
- Явные правила ("отвечай кратко", "используй русский", "не добавляй эмодзи")
- Неявные предпочтения (пользователь всегда переформулирует → слишком сложно)
- Контекстные знания ("я работаю над проектом X", "я PM")
- Стиль коммуникации (формальный/неформальный)
- Уровень детализации (краткий/стандартный/подробный)

Сохраняет в:
- UserProfileService (SQLite) — быстрый доступ к предпочтениям
- Knowledge Graph (Neo4j/NetworkX) — узлы типа UserPreference

Файлы:
- backend/core/capture/agents/chat_preference_extractor.py (этот файл)
- backend/memory/user_profiles.py (UserProfileService — хранение)
- config/feature_flags.json → enable_user_feedback_learning=true
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ExtractedPreference:
    """Одно извлечённое предпочтение."""
    category: str  # "communication", "format", "content", "context", "rule"
    key: str  # "detail_level", "language", "no_emoji", "current_project", ...
    value: str  # "brief", "ru", "true", "Project Alpha", ...
    confidence: float = 0.8  # 0.0-1.0
    source: str = "chat"  # "chat", "feedback", "explicit", "implicit"
    raw_text: str = ""  # Исходное сообщение


@dataclass
class PreferenceExtractionResult:
    """Результат извлечения предпочтений."""
    preferences: List[ExtractedPreference] = field(default_factory=list)
    rules: List[str] = field(default_factory=list)  # Явные правила для Rule Book
    context_updates: Dict[str, str] = field(default_factory=dict)  # Обновления контекста


# ============================================================================
# Паттерны для быстрого реал-тайм извлечения (без LLM)
# ============================================================================

# Явные указания стиля
_BRIEF_PATTERNS = [
    r"отвечай\s+коротко", r"кратко", r"без\s+воды", r"покороче",
    r"не\s+расписывай", r"коротко\s+и\s+ясно", r"be\s+brief", r"keep\s+it\s+short",
]

_DETAILED_PATTERNS = [
    r"подробно", r"расписывай", r"объясни\s+детально", r"подробнее",
    r"дай\s+больше\s+деталей", r"разверни", r"be\s+detailed",
]

_NO_EMOJI_PATTERNS = [
    r"без\s+эмоджи", r"не\s+используй\s+эмоджи", r"убери\s+смайлы",
    r"no\s+emoji", r"без\s+смайл",
]

_LANGUAGE_RU_PATTERNS = [
    r"отвечай\s+(по-)?русски", r"на\s+русском", r"пиши\s+по-русски",
]

_LANGUAGE_EN_PATTERNS = [
    r"answer\s+in\s+english", r"reply\s+in\s+english", r"на\s+английском",
]

_FORMAL_PATTERNS = [
    r"формально", r"официально", r"без\s+сленга", r"деловой\s+стиль",
]

_CASUAL_PATTERNS = [
    r"неформально", r"по-простому", r"без\s+формальностей", r"проще",
]

# Контекстные фразы
_CONTEXT_PATTERNS = [
    (r"я\s+работаю\s+(?:над|в)\s+(?:проектом?\s+)?([^\.,!?]+)", "current_project"),
    (r"я\s+(?:PM|менеджер|разработчик|дизайнер|тимлид|CTO|CEO|директор)", "role"),
    (r"мой\s+(?:отдел|команда)\s+[-—]\s+([^\.,!?]+)", "department"),
    (r"я\s+(?:из|в)\s+(?:отдела?\s+)?([^\.,!?]+)", "department"),
    (r"(?:мой|наш)\s+проект\s+(?:называется?\s+)?([^\.,!?]+)", "current_project"),
]

# Явные правила (пользователь диктует как работать)
_RULE_PATTERNS = [
    r"запомни[:\s]+(.+)",
    r"правило[:\s]+(.+)",
    r"всегда\s+(.+)",
    r"никогда\s+(?:не\s+)?(.+)",
    r"(?:помни|учти)[,\s]+(?:что\s+)?(.+)",
    r"remember[:\s]+(.+)",
]


class ChatPreferenceExtractor:
    """
    Извлекает предпочтения из чат-сообщений.

    Два режима:
    - extract_realtime(message) — быстрый regex, после каждого сообщения
    - extract_batch(messages) — LLM-анализ серии сообщений (ночной)

    Использование:
    ```python
    extractor = ChatPreferenceExtractor()

    # Реал-тайм (в chat route)
    result = extractor.extract_realtime("отвечай кратко и по-русски")
    # → preferences: [("communication", "detail_level", "brief"), ...]

    # Batch (в nightly consolidation)
    result = await extractor.extract_batch(messages_list, llm_router)
    # → preferences + rules + context_updates
    ```
    """

    def __init__(self):
        self._compiled_patterns = {}
        self._compile_patterns()

    def _compile_patterns(self):
        """Предкомпиляция regex паттернов."""
        self._brief_re = [re.compile(p, re.IGNORECASE) for p in _BRIEF_PATTERNS]
        self._detailed_re = [re.compile(p, re.IGNORECASE) for p in _DETAILED_PATTERNS]
        self._no_emoji_re = [re.compile(p, re.IGNORECASE) for p in _NO_EMOJI_PATTERNS]
        self._lang_ru_re = [re.compile(p, re.IGNORECASE) for p in _LANGUAGE_RU_PATTERNS]
        self._lang_en_re = [re.compile(p, re.IGNORECASE) for p in _LANGUAGE_EN_PATTERNS]
        self._formal_re = [re.compile(p, re.IGNORECASE) for p in _FORMAL_PATTERNS]
        self._casual_re = [re.compile(p, re.IGNORECASE) for p in _CASUAL_PATTERNS]
        self._context_re = [(re.compile(p, re.IGNORECASE), cat) for p, cat in _CONTEXT_PATTERNS]
        self._rule_re = [re.compile(p, re.IGNORECASE) for p in _RULE_PATTERNS]

    def extract_realtime(self, message: str) -> PreferenceExtractionResult:
        """
        Быстрое извлечение предпочтений из одного сообщения (без LLM).

        Вызывается после каждого сообщения пользователя в chat route.
        Работает за <1ms (только regex).

        Args:
            message: Текст сообщения пользователя

        Returns:
            Извлечённые предпочтения, правила, контекстные обновления
        """
        result = PreferenceExtractionResult()

        if not message or len(message) < 3:
            return result

        text = message.strip()

        # Проверяем стиль ответов
        if any(p.search(text) for p in self._brief_re):
            result.preferences.append(ExtractedPreference(
                category="communication", key="detail_level", value="brief",
                confidence=0.9, source="explicit", raw_text=text
            ))

        if any(p.search(text) for p in self._detailed_re):
            result.preferences.append(ExtractedPreference(
                category="communication", key="detail_level", value="detailed",
                confidence=0.9, source="explicit", raw_text=text
            ))

        # Эмодзи
        if any(p.search(text) for p in self._no_emoji_re):
            result.preferences.append(ExtractedPreference(
                category="format", key="no_emoji", value="true",
                confidence=0.95, source="explicit", raw_text=text
            ))

        # Язык
        if any(p.search(text) for p in self._lang_ru_re):
            result.preferences.append(ExtractedPreference(
                category="communication", key="language", value="ru",
                confidence=0.95, source="explicit", raw_text=text
            ))
        if any(p.search(text) for p in self._lang_en_re):
            result.preferences.append(ExtractedPreference(
                category="communication", key="language", value="en",
                confidence=0.95, source="explicit", raw_text=text
            ))

        # Формальность
        if any(p.search(text) for p in self._formal_re):
            result.preferences.append(ExtractedPreference(
                category="communication", key="style", value="formal",
                confidence=0.85, source="explicit", raw_text=text
            ))
        if any(p.search(text) for p in self._casual_re):
            result.preferences.append(ExtractedPreference(
                category="communication", key="style", value="casual",
                confidence=0.85, source="explicit", raw_text=text
            ))

        # Контекст (проект, роль, отдел)
        for pattern, category in self._context_re:
            match = pattern.search(text)
            if match:
                value = match.group(1).strip() if match.lastindex else match.group(0)
                result.context_updates[category] = value
                result.preferences.append(ExtractedPreference(
                    category="context", key=category, value=value,
                    confidence=0.7, source="implicit", raw_text=text
                ))

        # Явные правила
        for pattern in self._rule_re:
            match = pattern.search(text)
            if match:
                rule_text = match.group(1).strip()
                if len(rule_text) > 5:
                    result.rules.append(rule_text)
                    result.preferences.append(ExtractedPreference(
                        category="rule", key="user_rule", value=rule_text,
                        confidence=0.85, source="explicit", raw_text=text
                    ))

        return result

    async def extract_batch(
        self,
        messages: List[Dict[str, str]],
        llm_router=None,
    ) -> PreferenceExtractionResult:
        """
        Глубокий LLM-анализ серии сообщений (для ночной консолидации).

        Анализирует батч сообщений и извлекает:
        - Неявные предпочтения (переформулирования → слишком сложно)
        - Паттерны использования (часто спрашивает про X)
        - Правила и ограничения
        - Стиль общения

        Args:
            messages: Список сообщений [{role, content}, ...]
            llm_router: LLM роутер для вызова модели

        Returns:
            Извлечённые предпочтения, правила, контекст
        """
        result = PreferenceExtractionResult()

        if not messages or not llm_router:
            return result

        # Формируем контекст из последних 50 сообщений
        recent = messages[-50:]
        conversation_text = "\n".join(
            f"{'USER' if m.get('role') == 'user' else 'ASSISTANT'}: {m.get('content', '')[:300]}"
            for m in recent
        )

        prompt = f"""Analyze the following conversation between USER and ASSISTANT.
Extract the user's preferences and rules for how the system should interact with them.

CONVERSATION:
{conversation_text}

Return a JSON object with:
{{
  "communication_style": "formal|casual|technical|executive",
  "detail_level": "brief|standard|detailed",
  "language": "ru|en|...",
  "topics_of_interest": ["topic1", "topic2"],
  "explicit_rules": ["rule1", "rule2"],
  "implicit_preferences": ["pref1", "pref2"],
  "current_focus": "what the user is currently working on",
  "role_hint": "perceived role of the user",
  "frustrations": ["what seems to annoy the user"]
}}

IMPORTANT:
- Base every field ONLY on what the user actually wrote/did in this conversation.
  Do not invent preferences, frustrations, or a role that is not evidenced.
- If a field is unclear from the conversation, use "unknown" / an empty list
  rather than a plausible guess.
- explicit_rules — only things the user directly stated as a rule; implicit ones
  must be grounded in observable behavior, not assumed.

Respond ONLY with valid JSON, no markdown."""

        try:
            response = await llm_router.generate(
                prompt=prompt,
                system_prompt="You are a user preference extraction system. Analyze conversations and extract preferences.",
                temperature=0.1,
                max_tokens=1000,
            )

            # Парсим JSON
            text = response.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0]

            data = json.loads(text)

            # Маппим в предпочтения
            if data.get("communication_style"):
                result.preferences.append(ExtractedPreference(
                    category="communication", key="style",
                    value=data["communication_style"],
                    confidence=0.7, source="implicit"
                ))

            if data.get("detail_level"):
                result.preferences.append(ExtractedPreference(
                    category="communication", key="detail_level",
                    value=data["detail_level"],
                    confidence=0.7, source="implicit"
                ))

            if data.get("language"):
                result.preferences.append(ExtractedPreference(
                    category="communication", key="language",
                    value=data["language"],
                    confidence=0.7, source="implicit"
                ))

            if data.get("current_focus"):
                result.context_updates["current_focus"] = data["current_focus"]

            if data.get("role_hint"):
                result.context_updates["role"] = data["role_hint"]

            for rule in data.get("explicit_rules", []):
                if rule and len(rule) > 3:
                    result.rules.append(rule)

            for pref in data.get("implicit_preferences", []):
                if pref:
                    result.preferences.append(ExtractedPreference(
                        category="implicit", key="preference",
                        value=pref, confidence=0.6, source="implicit"
                    ))

            logger.info(
                f"Batch preference extraction: "
                f"{len(result.preferences)} preferences, "
                f"{len(result.rules)} rules"
            )

        except json.JSONDecodeError:
            logger.warning("Failed to parse LLM response as JSON in preference extraction")
        except Exception as e:
            logger.error(f"Batch preference extraction error: {e}")

        return result

    async def apply_to_profile(
        self,
        result: PreferenceExtractionResult,
        user_id: str,
        profile_service=None,
    ):
        """
        Применить извлечённые предпочтения к профилю пользователя.

        Args:
            result: Результат извлечения
            user_id: ID пользователя
            profile_service: UserProfileService (опционально, создастся если нет)
        """
        if not result.preferences and not result.rules and not result.context_updates:
            return

        if profile_service is None:
            from backend.memory.user_profiles import UserProfileService
            profile_service = UserProfileService()
            await profile_service.initialize()

        updates = {}

        for pref in result.preferences:
            if pref.key == "detail_level" and pref.confidence >= 0.7:
                updates["detail_level"] = pref.value
            elif pref.key == "language" and pref.confidence >= 0.7:
                updates["preferred_language"] = pref.value
            elif pref.key == "style" and pref.confidence >= 0.7:
                updates["communication_style"] = pref.value

        for ctx_key, ctx_value in result.context_updates.items():
            if ctx_key == "current_project":
                # Добавляем в список текущих проектов
                profile = await profile_service.get_profile(user_id)
                projects = profile.current_projects or []
                if ctx_value not in projects:
                    projects.append(ctx_value)
                updates["current_projects"] = projects
            elif ctx_key == "role":
                updates["role"] = ctx_value
            elif ctx_key == "department":
                updates["department"] = ctx_value
            elif ctx_key == "current_focus":
                updates["current_focus"] = ctx_value

        if updates:
            await profile_service.update_profile(user_id, updates)
            logger.info(f"Updated user profile {user_id}: {list(updates.keys())}")


# Singleton
_extractor: Optional[ChatPreferenceExtractor] = None

def get_chat_preference_extractor() -> ChatPreferenceExtractor:
    """Получить singleton экземпляр экстрактора."""
    global _extractor
    if _extractor is None:
        _extractor = ChatPreferenceExtractor()
    return _extractor
