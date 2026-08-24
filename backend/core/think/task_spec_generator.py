import logging

from ..llm.router import LLMRouter
from .reasoning_engine import ReasoningEngine, SearchStrategy

logger = logging.getLogger(__name__)

class TaskSpecGenerator:
    """
    Генератор технических заданий (Task Specifications).

    Использует контекст из базы знаний (Graph + Vector) для превращения
    короткого заголовка задачи в полноценное ТЗ.
    """

    def __init__(self, reasoning_engine: ReasoningEngine, llm_router: LLMRouter):
        self.reasoning = reasoning_engine
        self.llm = llm_router

    async def generate_spec(self, task_title: str, user_description: str = "") -> str:
        """
        Генерировать ТЗ для задачи.

        Args:
            task_title: Заголовок задачи (напр. "Сделать лендинг")
            user_description: Дополнительное описание от пользователя

        Returns:
            Текст ТЗ в формате Markdown
        """
        logger.info(f"📝 Generating spec for task: {task_title}")

        # 1. Собираем контекст через ReasoningEngine
        # Спрашиваем систему о теме задачи, чтобы найти проекты, людей и похожие задачи
        search_query = f"Контекст и требования для задачи: {task_title}. {user_description}"

        # Используем STANDARD стратегию для сбора контекста
        context_result = await self.reasoning.reason(
            query=search_query,
            strategy=SearchStrategy.STANDARD
        )

        context_text = context_result.answer
        if not context_text:
            context_text = "Контекст не найден."

        # 2. Генерируем ТЗ с помощью LLM
        prompt = f"""Ты — опытный Product Manager и Технический Писатель.
Твоя задача — превратить короткое описание задачи в подробное Техническое Задание (ТЗ).

📌 ЗАДАЧА:
Заголовок: "{task_title}"
Описание пользователя: "{user_description}"

📚 КОНТЕКСТ ИЗ БАЗЫ ЗНАНИЙ:
{context_text}

ТРЕБОВАНИЯ К ТЗ:
1. Структура: Цель, Критерии успеха (DoD), Детали реализации, Риски.
2. Используй контекст: если в базе знаний упомянуты конкретные технологии, люди или проекты — используй их.
3. Будь конкретным: избегай воды.
4. Формат: Markdown.

СГЕНЕРИРУЙ ТЗ:"""

        try:
            spec = await self.llm.generate(
                prompt=prompt,
                temperature=0.5,
                max_tokens=2000
            )
            logger.info("✅ Spec generated successfully")
            return spec

        except Exception as e:
            logger.error(f"❌ Failed to generate spec: {e}")
            # Fallback - возвращаем просто описание
            return f"# Задача: {task_title}\n\n{user_description}"

