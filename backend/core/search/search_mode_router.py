# -*- coding: utf-8 -*-
"""
SearchModeRouter - Автоматическая классификация запросов по режимам поиска.

Определяет оптимальный режим поиска на основе:
- Эвристик (ключевые слова, длина запроса, паттерны)
- Классификации типа запроса (приветствие, факт, аналитика, ТЗ)

Режимы:
- INSTANT: <1 сек, $0 — снапшоты без LLM (приветствия, обзоры, "как дела?")
- QUICK: 3-5 сек, ~$0.001 — 1 LLM вызов, без reflection (простые факты)
- STANDARD: 8-15 сек, ~$0.005 — полный поиск + reflection (аналитика)
- DEEP: 30-120 сек, ~$0.02 — multi-step с расширением (исследования)
- TASKER: 60-180 сек, ~$0.05 — генерация ТЗ, 6-этапный процесс
"""

import logging
import os
import re
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class SearchMode(str, Enum):
    """Режимы поиска"""
    INSTANT = "instant"
    QUICK = "quick"
    STANDARD = "standard"
    DEEP = "deep"
    TASKER = "tasker"


# ═══════════════════════════════════════════════════════════════════════
# ПАТТЕРНЫ ДЛЯ КЛАССИФИКАЦИИ
# ═══════════════════════════════════════════════════════════════════════

# INSTANT: простые приветствия и общие обзоры
INSTANT_PATTERNS = [
    # Приветствия
    r"^(привет|здравствуй|добрый|доброе|добрый\s+день|hi|hello|hey)\b",
    # Общие обзоры
    r"^(обзор|дашборд|итого|общая\s+картина|статус\s+компании)\b",
    r"\b(как\s+дела|что\s+нового|как\s+дела\s+в\s+компании)\b",
    # Простые справки
    r"^(список\s+проектов|список\s+сотрудников|все\s+проекты)\b",
    r"^(покажи|показать)\s+(компанию|обзор|дашборд)\b",
]

# TASKER: генерация ТЗ / создание чего-то
TASKER_PATTERNS = [
    r"\b(сделай|создай|напиши|разработай|подготовь|составь)\s+(тз|техническое\s+задание|спецификаци|план)\b",
    r"\b(сделай|создай|разработай)\s+(финмодель|фин\.\s*модель|финансовую\s+модель)\b",
    r"\b(сделай|создай|разработай)\s+(лендинг|landing|сайт|страниц)\b",
    r"\b(сделай|создай|подготовь)\s+(отчёт|отчет|доклад|презентаци)\b",
    r"\b(напиши|составь|подготовь)\s+(бизнес-план|бизнес\s+план|стратеги)\b",
    r"\btз\s+(для|на|по)\b",
    r"\b(техническое\s+задание)\b",
]

# DEEP: сложные исследования, "почему", сравнения, причины
DEEP_PATTERNS = [
    r"\b(почему|зачем|из-за\s+чего|причин[аы])\b",
    r"\b(сравни|сравнить|сравнение|versus|vs\.?)\b",
    r"\b(проанализируй|анализ|аналитика|аналитик[уа]|исследуй|исследование)\b",
    r"\b(выяви|обнаруж|найди\s+проблем|найди\s+риск|найди\s+противореч)\b",
    r"\b(тренд|динамик[аи]|изменени[яе]\s+за)\b",
    r"\b(как\s+связан|взаимосвяз|корреляци|влияни[ея])\b",
    r"\b(оцени|оценка|оценить)\s+(качество|эффективность|результат)\b",
    r"\b(рекомендац|что\s+посоветуешь|как\s+улучшить|как\s+исправить)\b",
    r"\b(комплексн|всесторонн|полн[ыа]й\s+анализ|глубок)\b",
]

# QUICK: простые фактовые вопросы
QUICK_PATTERNS = [
    r"^(кто|что|где|когда|какой|какая|какое|какие|сколько)\b",
    r"\b(статус|прогресс)\s+\w+",
    r"\b(кто\s+ответственн|кто\s+ведёт|кто\s+ведет|кто\s+делает)\b",
    r"\b(дата|дедлайн|срок|deadline)\b",
    r"\b(контакт|email|телефон|номер)\b",
    r"\b(последн[яе]|недавн)\b.{0,30}(встреч|решени|задач)",
]


class SearchModeRouter:
    """
    Маршрутизатор запросов по режимам поиска.

    Использование:
    ```python
    router = SearchModeRouter()
    mode = router.classify(query)  # → SearchMode.QUICK
    ```
    """

    def __init__(self):
        # Предкомпиляция regex
        self._instant_re = [re.compile(p, re.IGNORECASE) for p in INSTANT_PATTERNS]
        self._tasker_re = [re.compile(p, re.IGNORECASE) for p in TASKER_PATTERNS]
        self._deep_re = [re.compile(p, re.IGNORECASE) for p in DEEP_PATTERNS]
        self._quick_re = [re.compile(p, re.IGNORECASE) for p in QUICK_PATTERNS]
        # Потолок режима: на слабом железе DEEP неподъёмен (минуты). Оператор
        # ставит TESSENT_MAX_SEARCH_MODE=standard → DEEP автоматически
        # понижается до STANDARD (ответ за ~30-60с вместо 4-9 мин).
        self._max_mode = os.getenv("TESSENT_MAX_SEARCH_MODE", "").strip().lower()

    def _cap(self, mode: "SearchMode") -> "SearchMode":
        """Ограничить режим сверху через TESSENT_MAX_SEARCH_MODE."""
        order = {SearchMode.INSTANT: 0, SearchMode.QUICK: 1,
                 SearchMode.STANDARD: 2, SearchMode.DEEP: 3}
        cap = {"quick": SearchMode.QUICK, "standard": SearchMode.STANDARD,
               "deep": SearchMode.DEEP}.get(self._max_mode)
        if cap and order.get(mode, 0) > order[cap]:
            logger.info(f"🎯 Router: {mode.value} → capped {cap.value} (MAX_SEARCH_MODE)")
            return cap
        return mode

    def classify(self, query: str, has_context: bool = False) -> SearchMode:
        """
        Классифицировать запрос по режиму поиска.

        Args:
            query: Текст запроса пользователя
            has_context: Есть ли контекст (фильтры по встречам/документам)

        Returns:
            SearchMode — оптимальный режим поиска
        """
        if not query or not query.strip():
            return SearchMode.INSTANT

        query_clean = query.strip()
        query_lower = query_clean.lower()
        word_count = len(query_clean.split())

        # ─── 1. TASKER: проверяем сначала, т.к. "сделай ТЗ" содержит глагол ─────
        for pattern in self._tasker_re:
            if pattern.search(query_lower):
                logger.info("🎯 Router: TASKER (pattern match)")
                return SearchMode.TASKER

        # ─── 2. INSTANT: приветствия, очень короткие или обзорные запросы ────
        if word_count <= 2:
            # Очень короткие запросы ("привет", "статус", "проекты")
            for pattern in self._instant_re:
                if pattern.search(query_lower):
                    logger.info("🎯 Router: INSTANT (short + pattern)")
                    return SearchMode.INSTANT
            # Одно слово — скорее всего quick факт
            if word_count == 1:
                logger.info("🎯 Router: QUICK (single word)")
                return SearchMode.QUICK

        # INSTANT по паттернам
        for pattern in self._instant_re:
            if pattern.search(query_lower):
                logger.info("🎯 Router: INSTANT (pattern match)")
                return SearchMode.INSTANT

        # ─── 3. DEEP: аналитика, исследования, "почему" ─────────────────────
        deep_score = 0
        for pattern in self._deep_re:
            if pattern.search(query_lower):
                deep_score += 1

        # Длинные запросы (>15 слов) более вероятно нуждаются в deep
        if word_count > 15:
            deep_score += 1

        # Множественные условия
        if query_lower.count(" и ") >= 2:
            deep_score += 1

        if deep_score >= 2:
            logger.info(f"🎯 Router: DEEP (score={deep_score})")
            return self._cap(SearchMode.DEEP)

        if deep_score == 1 and word_count > 8:
            logger.info("🎯 Router: DEEP (score=1, long query)")
            return self._cap(SearchMode.DEEP)

        # ─── 4. QUICK: простые факты ───────────────────────────────────────
        for pattern in self._quick_re:
            if pattern.search(query_lower):
                logger.info("🎯 Router: QUICK (pattern match)")
                return SearchMode.QUICK

        # Короткие запросы без deep-маркеров → QUICK
        if word_count <= 5:
            logger.info("🎯 Router: QUICK (short query)")
            return SearchMode.QUICK

        # ─── 5. STANDARD: всё остальное ────────────────────────────────────
        logger.info("🎯 Router: STANDARD (default)")
        return SearchMode.STANDARD

    async def classify_with_llm_fallback(self, query: str, llm_router=None) -> SearchMode:
        """
        Классификация с LLM fallback для неочевидных запросов.

        Сначала пробует regex, если уверенность низкая (medium-length query,
        не попал ни в один паттерн) — вызывает LLM для уточнения.

        Args:
            query: Текст запроса
            llm_router: LLMRouter для LLM вызова (опционально)
        """
        # 1. Быстрая regex-классификация
        mode = self.classify(query)

        # 2. Проверяем, нужен ли LLM fallback
        # LLM нужен только если regex вернул STANDARD (дефолт) и запрос средней длины
        query_words = len(query.strip().split())
        if mode != SearchMode.STANDARD or not llm_router or query_words < 5 or query_words > 20:
            return mode

        # 3. LLM классификация
        try:
            prompt = f"""Классифицируй запрос пользователя к корпоративной базе знаний в один из режимов поиска:

INSTANT — простые приветствия, обзоры ("привет", "как дела в компании", "покажи дашборд")
QUICK — простые фактовые вопросы ("кто ведёт проект X?", "статус задачи Y", "когда дедлайн?")
STANDARD — обычные вопросы средней сложности ("расскажи про проект", "что обсуждали на встрече")
DEEP — сложная аналитика, причины, сравнения ("почему упали продажи?", "сравни эффективность отделов")
TASKER — создание документов, ТЗ, отчётов ("сделай ТЗ на лендинг", "подготовь финмодель")

Запрос: "{query}"

Ответь ОДНИМ словом: INSTANT, QUICK, STANDARD, DEEP или TASKER."""

            result = await llm_router.generate(
                prompt=prompt,
                system_prompt="Ты классификатор запросов. Отвечай ОДНИМ словом.",
                max_tokens=10,
            )

            result_clean = result.strip().upper()
            mode_map = {
                "INSTANT": SearchMode.INSTANT,
                "QUICK": SearchMode.QUICK,
                "STANDARD": SearchMode.STANDARD,
                "DEEP": SearchMode.DEEP,
                "TASKER": SearchMode.TASKER,
            }

            if result_clean in mode_map:
                llm_mode = self._cap(mode_map[result_clean])
                logger.info(f"🎯 Router: {llm_mode.value} (LLM override from STANDARD)")
                return llm_mode

        except Exception as e:
            logger.warning(f"LLM classification failed, using regex result: {e}")

        return mode

    def explain(self, query: str) -> dict:
        """
        Объяснить почему выбран этот режим (для дебага).

        Returns:
            dict с mode, reasons, scores
        """
        query_lower = query.strip().lower()
        word_count = len(query.strip().split())

        reasons = []

        # Проверяем все паттерны
        for pattern in self._tasker_re:
            if pattern.search(query_lower):
                reasons.append(f"TASKER pattern: {pattern.pattern}")

        for pattern in self._instant_re:
            if pattern.search(query_lower):
                reasons.append(f"INSTANT pattern: {pattern.pattern}")

        deep_score = 0
        for pattern in self._deep_re:
            if pattern.search(query_lower):
                deep_score += 1
                reasons.append(f"DEEP pattern: {pattern.pattern}")

        for pattern in self._quick_re:
            if pattern.search(query_lower):
                reasons.append(f"QUICK pattern: {pattern.pattern}")

        mode = self.classify(query)

        return {
            "mode": mode.value,
            "word_count": word_count,
            "deep_score": deep_score,
            "reasons": reasons,
        }


# Singleton
_router_instance: Optional[SearchModeRouter] = None


def get_search_mode_router() -> SearchModeRouter:
    """Получить синглтон SearchModeRouter."""
    global _router_instance
    if _router_instance is None:
        _router_instance = SearchModeRouter()
    return _router_instance
