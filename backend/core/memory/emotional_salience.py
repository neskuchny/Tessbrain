# -*- coding: utf-8 -*-
"""
Emotional Salience Analyzer - Анализ эмоциональной значимости.

Принцип: Информация с высокой эмоциональной окраской запоминается лучше.

Применение в Tessbrain:
- Определение срочности по ключевым словам
- Анализ негативного/позитивного тона
- Приоритизация важной информации
- Усиление веса эмоционально значимых сущностей

Использование:
```python
from backend.core.memory.emotional_salience import EmotionalSalienceAnalyzer

analyzer = EmotionalSalienceAnalyzer()

# Быстрый анализ по ключевым словам
result = analyzer.analyze_text("СРОЧНО! Критическая ошибка в продакшене!")
logger.info(result["urgency_score"])  # 0.9
logger.info(result["factors"])  # ["urgency_keyword:СРОЧНО", "negative:критическая ошибка"]

# Глубокий анализ с LLM
result = await analyzer.analyze_with_llm(text, llm_router)
```
"""
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class SalienceResult:
    """Результат анализа эмоциональной значимости."""
    urgency_score: float = 0.0          # 0.0 - 1.0
    negative_sentiment: float = 0.0     # 0.0 - 1.0
    positive_sentiment: float = 0.0     # 0.0 - 1.0
    importance_score: float = 0.0       # 0.0 - 1.0
    overall_salience: float = 0.0       # 0.0 - 1.0
    factors: List[str] = field(default_factory=list)
    priority_level: str = "normal"      # low, normal, high, critical


class EmotionalSalienceAnalyzer:
    """
    Анализатор эмоциональной значимости текста.

    Определяет:
    1. Срочность (urgency)
    2. Негативный тон (проблемы, ошибки)
    3. Позитивный тон (достижения, успехи)
    4. Общую важность
    """

    # Ключевые слова для определения срочности (русский + английский)
    URGENCY_KEYWORDS = {
        # Высокая срочность (0.9)
        "high": [
            "срочно", "немедленно", "критично", "критическ", "asap", "urgent",
            "emergency", "экстренно", "сейчас же", "прямо сейчас", "горит",
            "дедлайн сегодня", "deadline today", "блокер", "blocker",
        ],
        # Средняя срочность (0.6)
        "medium": [
            "важно", "приоритет", "скоро", "быстро", "как можно скорее",
            "important", "priority", "soon", "quickly", "нужно сделать",
            "не забыть", "обязательно", "необходимо",
        ],
        # Низкая срочность (0.3)
        "low": [
            "желательно", "по возможности", "когда будет время", "nice to have",
            "можно потом", "не срочно", "в свободное время",
        ],
    }

    # Ключевые слова для негативного тона
    NEGATIVE_KEYWORDS = {
        # Высокий негатив (0.8)
        "high": [
            "ошибка", "баг", "bug", "error", "сломал", "broken", "упал",
            "crash", "провал", "fail", "проблема", "problem", "issue",
            "критическ", "critical", "блокирует", "blocking", "не работает",
            "doesn't work", "потеряли", "lost", "удалили", "deleted",
        ],
        # Средний негатив (0.5)
        "medium": [
            "задержка", "delay", "отстаём", "behind", "риск", "risk",
            "сложност", "difficulty", "непонятно", "unclear", "путаница",
            "confusion", "недовольн", "unhappy", "жалоба", "complaint",
        ],
        # Низкий негатив (0.3)
        "low": [
            "замечание", "note", "предупреждение", "warning", "внимание",
            "attention", "осторожно", "careful", "учесть", "consider",
        ],
    }

    # Ключевые слова для позитивного тона
    POSITIVE_KEYWORDS = {
        # Высокий позитив (0.8)
        "high": [
            "отлично", "excellent", "прекрасно", "great", "успех", "success",
            "достижение", "achievement", "победа", "win", "завершили",
            "completed", "готово", "done", "работает", "works", "решили",
            "solved", "fixed", "исправили",
        ],
        # Средний позитив (0.5)
        "medium": [
            "хорошо", "good", "неплохо", "not bad", "прогресс", "progress",
            "улучшение", "improvement", "движемся", "moving", "продвинулись",
        ],
        # Низкий позитив (0.3)
        "low": [
            "нормально", "okay", "приемлемо", "acceptable", "стабильно",
            "stable", "без изменений", "no changes",
        ],
    }

    # Ключевые слова важности
    IMPORTANCE_KEYWORDS = {
        "high": [
            "ключевой", "key", "основной", "main", "главный", "primary",
            "стратегическ", "strategic", "критическ", "critical",
            "обязательно", "must", "необходимо", "required",
        ],
        "medium": [
            "важный", "important", "значимый", "significant", "существенный",
            "substantial", "нужный", "needed",
        ],
        "low": [
            "второстепенный", "secondary", "дополнительный", "additional",
            "опциональный", "optional", "можно пропустить", "can skip",
        ],
    }

    def __init__(self):
        """Инициализация анализатора."""
        # Компилируем регулярные выражения для эффективности
        self._compile_patterns()

    def _compile_patterns(self):
        """Компилируем паттерны для быстрого поиска."""
        self._urgency_patterns = {}
        self._negative_patterns = {}
        self._positive_patterns = {}
        self._importance_patterns = {}

        for level, keywords in self.URGENCY_KEYWORDS.items():
            pattern = r'\b(' + '|'.join(re.escape(k) for k in keywords) + r')\b'
            self._urgency_patterns[level] = re.compile(pattern, re.IGNORECASE)

        for level, keywords in self.NEGATIVE_KEYWORDS.items():
            pattern = r'\b(' + '|'.join(re.escape(k) for k in keywords) + r')\b'
            self._negative_patterns[level] = re.compile(pattern, re.IGNORECASE)

        for level, keywords in self.POSITIVE_KEYWORDS.items():
            pattern = r'\b(' + '|'.join(re.escape(k) for k in keywords) + r')\b'
            self._positive_patterns[level] = re.compile(pattern, re.IGNORECASE)

        for level, keywords in self.IMPORTANCE_KEYWORDS.items():
            pattern = r'\b(' + '|'.join(re.escape(k) for k in keywords) + r')\b'
            self._importance_patterns[level] = re.compile(pattern, re.IGNORECASE)

    def analyze_text(self, text: str) -> Dict[str, Any]:
        """
        Быстрый анализ текста по ключевым словам.

        Args:
            text: Текст для анализа

        Returns:
            Результат анализа с scores и factors
        """
        factors = []

        # Анализ срочности
        urgency_score = 0.0
        for level, pattern in self._urgency_patterns.items():
            matches = pattern.findall(text)
            if matches:
                level_score = {"high": 0.9, "medium": 0.6, "low": 0.3}[level]
                urgency_score = max(urgency_score, level_score)
                for match in matches[:3]:  # Ограничиваем количество факторов
                    factors.append(f"urgency:{match.lower()}")

        # Анализ негатива
        negative_score = 0.0
        for level, pattern in self._negative_patterns.items():
            matches = pattern.findall(text)
            if matches:
                level_score = {"high": 0.8, "medium": 0.5, "low": 0.3}[level]
                negative_score = max(negative_score, level_score)
                for match in matches[:3]:
                    factors.append(f"negative:{match.lower()}")

        # Анализ позитива
        positive_score = 0.0
        for level, pattern in self._positive_patterns.items():
            matches = pattern.findall(text)
            if matches:
                level_score = {"high": 0.8, "medium": 0.5, "low": 0.3}[level]
                positive_score = max(positive_score, level_score)
                for match in matches[:3]:
                    factors.append(f"positive:{match.lower()}")

        # Анализ важности
        importance_score = 0.0
        for level, pattern in self._importance_patterns.items():
            matches = pattern.findall(text)
            if matches:
                level_score = {"high": 0.8, "medium": 0.5, "low": 0.3}[level]
                importance_score = max(importance_score, level_score)
                for match in matches[:3]:
                    factors.append(f"importance:{match.lower()}")

        # Дополнительные эвристики

        # Восклицательные знаки увеличивают срочность
        exclamation_count = text.count("!")
        if exclamation_count >= 3:
            urgency_score = min(1.0, urgency_score + 0.2)
            factors.append("exclamation_marks")
        elif exclamation_count >= 1:
            urgency_score = min(1.0, urgency_score + 0.1)

        # CAPS увеличивает эмоциональность
        caps_ratio = sum(1 for c in text if c.isupper()) / max(len(text), 1)
        if caps_ratio > 0.3:
            urgency_score = min(1.0, urgency_score + 0.15)
            factors.append("caps_text")

        # Вопросительные знаки могут указывать на неопределённость
        question_count = text.count("?")
        if question_count >= 2:
            factors.append("multiple_questions")

        # Вычисляем общую значимость
        # Формула: max(urgency, negative) * 0.5 + importance * 0.3 + (negative - positive) * 0.2
        overall_salience = (
            max(urgency_score, negative_score) * 0.5 +
            importance_score * 0.3 +
            max(0, negative_score - positive_score) * 0.2
        )
        overall_salience = min(1.0, max(0.0, overall_salience))

        # Определяем уровень приоритета
        if urgency_score >= 0.8 or (negative_score >= 0.7 and importance_score >= 0.5):
            priority_level = "critical"
        elif urgency_score >= 0.5 or negative_score >= 0.5:
            priority_level = "high"
        elif importance_score >= 0.5 or positive_score >= 0.5:
            priority_level = "normal"
        else:
            priority_level = "low"

        return {
            "urgency_score": urgency_score,
            "negative_sentiment": negative_score,
            "positive_sentiment": positive_score,
            "importance_score": importance_score,
            "overall_salience": overall_salience,
            "factors": factors,
            "priority_level": priority_level,
        }

    async def analyze_with_llm(
        self,
        text: str,
        llm_router,
        context: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Глубокий анализ с использованием LLM.

        Args:
            text: Текст для анализа
            llm_router: LLM Router для генерации
            context: Дополнительный контекст

        Returns:
            Результат анализа
        """
        try:
            from backend.core.config import flags
            if not flags.enable_emotional_salience:
                return self.analyze_text(text)

            # Сначала делаем быстрый анализ
            quick_result = self.analyze_text(text)

            # Если явных маркеров нет, используем LLM
            if quick_result["overall_salience"] < 0.3:
                prompt = f"""Проанализируй эмоциональную значимость текста.

Текст: {text[:1000]}

{f"Контекст: {context}" if context else ""}

Оцени по шкале 0-10:
1. Срочность (urgency) - насколько требует немедленного внимания
2. Негативность (negative) - наличие проблем, ошибок, рисков
3. Позитивность (positive) - наличие успехов, достижений
4. Важность (importance) - значимость для бизнеса

Верни JSON:
{{
    "urgency": 0-10,
    "negative": 0-10,
    "positive": 0-10,
    "importance": 0-10,
    "reasoning": "краткое объяснение"
}}"""

                try:
                    llm_result = await llm_router.generate_json(
                        prompt=prompt,
                        system_prompt="Ты эксперт по анализу тональности и приоритизации."
                    )

                    # Преобразуем в наш формат
                    return {
                        "urgency_score": llm_result.get("urgency", 0) / 10.0,
                        "negative_sentiment": llm_result.get("negative", 0) / 10.0,
                        "positive_sentiment": llm_result.get("positive", 0) / 10.0,
                        "importance_score": llm_result.get("importance", 0) / 10.0,
                        "overall_salience": self._calculate_overall(llm_result),
                        "factors": [f"llm_reasoning:{llm_result.get('reasoning', '')[:100]}"],
                        "priority_level": self._determine_priority(llm_result),
                        "source": "llm",
                    }
                except Exception as e:
                    logger.warning(f"LLM analysis failed, using quick result: {e}")

            return quick_result

        except Exception as e:
            logger.error(f"❌ Emotional salience analysis failed: {e}")
            return self.analyze_text(text)

    def _calculate_overall(self, llm_result: Dict[str, Any]) -> float:
        """Вычислить общую значимость из LLM результата."""
        urgency = llm_result.get("urgency", 0) / 10.0
        negative = llm_result.get("negative", 0) / 10.0
        importance = llm_result.get("importance", 0) / 10.0
        positive = llm_result.get("positive", 0) / 10.0

        return min(1.0, (
            max(urgency, negative) * 0.5 +
            importance * 0.3 +
            max(0, negative - positive) * 0.2
        ))

    def _determine_priority(self, llm_result: Dict[str, Any]) -> str:
        """Определить уровень приоритета из LLM результата."""
        urgency = llm_result.get("urgency", 0)
        negative = llm_result.get("negative", 0)
        importance = llm_result.get("importance", 0)

        if urgency >= 8 or (negative >= 7 and importance >= 5):
            return "critical"
        elif urgency >= 5 or negative >= 5:
            return "high"
        elif importance >= 5:
            return "normal"
        else:
            return "low"

    def calculate_entity_salience(
        self,
        entity_mentions: List[Dict[str, Any]]
    ) -> float:
        """
        Вычислить общую эмоциональную значимость сущности
        на основе всех её упоминаний.

        Args:
            entity_mentions: Список упоминаний с контекстом

        Returns:
            Агрегированная значимость (0.0 - 1.0)
        """
        if not entity_mentions:
            return 0.0

        salience_scores = []

        for mention in entity_mentions:
            text = mention.get("context", "") or mention.get("text", "")
            if text:
                result = self.analyze_text(text)
                salience_scores.append(result["overall_salience"])

        if not salience_scores:
            return 0.0

        # Используем взвешенное среднее с акцентом на максимальные значения
        max_salience = max(salience_scores)
        avg_salience = sum(salience_scores) / len(salience_scores)

        # 60% максимум + 40% среднее
        return max_salience * 0.6 + avg_salience * 0.4


# Singleton
_analyzer_instance: Optional[EmotionalSalienceAnalyzer] = None


def get_emotional_analyzer() -> EmotionalSalienceAnalyzer:
    """Получить экземпляр EmotionalSalienceAnalyzer."""
    global _analyzer_instance
    if _analyzer_instance is None:
        _analyzer_instance = EmotionalSalienceAnalyzer()
    return _analyzer_instance

