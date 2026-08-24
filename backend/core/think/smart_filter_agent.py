# -*- coding: utf-8 -*-
"""
Smart Filter Agent - интеллектуальная фильтрация результатов поиска с помощью LLM.

Функции:
- LLM-фильтрация нерелевантных результатов
- Умное ранжирование по контексту
- Дедупликация семантически похожих результатов
- Группировка по темам
"""
import json
import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SmartFilterAgent:
    """
    Агент для интеллектуальной фильтрации результатов поиска.

    Использует LLM для:
    - Оценки релевантности каждого результата
    - Ранжирования по важности
    - Группировки по темам
    - Удаления дубликатов
    """

    def __init__(self, llm_client=None):
        """
        Args:
            llm_client: LLM клиент для интеллектуальной фильтрации
        """
        self.llm_client = llm_client
        self._cache = {}

    async def filter_results(
        self,
        query: str,
        results: List[Dict[str, Any]],
        max_results: int = 20,
        min_relevance: float = 0.5
    ) -> List[Dict[str, Any]]:
        """
        Отфильтровать и ранжировать результаты поиска.

        Args:
            query: Исходный запрос
            results: Список результатов для фильтрации
            max_results: Максимальное количество результатов
            min_relevance: Минимальный порог релевантности (0-1)

        Returns:
            Отфильтрованные и ранжированные результаты
        """
        if not results:
            return []

        logger.info(f"🔍 SmartFilterAgent: фильтрация {len(results)} результатов для '{query[:50]}...'")

        # 1. Быстрая предварительная фильтрация
        pre_filtered = self._quick_filter(query, results)

        # 2. LLM-оценка релевантности (если доступен LLM)
        if self.llm_client and len(pre_filtered) > 5:
            scored = await self._llm_score_relevance(query, pre_filtered)
        else:
            scored = self._heuristic_score(query, pre_filtered)

        # 3. Фильтрация по порогу релевантности
        filtered = [r for r in scored if r.get("relevance_score", 0) >= min_relevance]

        # 4. Дедупликация
        deduplicated = self._deduplicate(filtered)

        # 5. Сортировка по релевантности
        sorted_results = sorted(deduplicated, key=lambda x: -x.get("relevance_score", 0))

        # 6. Ограничение количества
        final = sorted_results[:max_results]

        logger.info(f"   ✅ После фильтрации: {len(final)} результатов (из {len(results)})")

        return final

    def _quick_filter(self, query: str, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Быстрая предварительная фильтрация."""
        query_words = set(query.lower().split())
        filtered = []

        for result in results:
            # Извлекаем текст из результата
            text = self._extract_text(result).lower()

            # Проверяем наличие хотя бы одного слова из запроса
            if any(word in text for word in query_words if len(word) > 2):
                filtered.append(result)
            elif result.get("score", 0) > 0.5:  # Или высокий векторный score
                filtered.append(result)

        return filtered if filtered else results[:50]  # Fallback

    def _extract_text(self, result: Dict[str, Any]) -> str:
        """Извлечь текст из результата для анализа."""
        parts = []

        # Пробуем разные поля
        for field in ["title", "name", "summary", "description", "text", "content"]:
            if field in result:
                parts.append(str(result[field]))

        # Из metadata
        if "metadata" in result:
            meta = result["metadata"]
            for field in ["title", "name", "summary", "description"]:
                if field in meta:
                    parts.append(str(meta[field]))

        return " ".join(parts)

    async def _llm_score_relevance(
        self,
        query: str,
        results: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Оценить релевантность с помощью LLM."""
        # Ограничиваем количество для LLM
        batch = results[:30]

        # Формируем описания результатов
        items = []
        for i, r in enumerate(batch):
            text = self._extract_text(r)[:200]
            items.append(f"{i+1}. {text}")

        prompt = f"""Оцени релевантность каждого результата для запроса.

Запрос: "{query}"

Результаты:
{chr(10).join(items)}

Для каждого результата укажи оценку релевантности от 0 до 1.
Ответь в формате JSON: {{"scores": [0.8, 0.5, 0.9, ...]}}

Только JSON, без пояснений."""

        try:
            response = await self.llm_client.generate(
                prompt=prompt,
                temperature=0.1,
                max_tokens=500
            )

            text = response.get("text", "") if isinstance(response, dict) else str(response)

            # Парсим JSON
            import re
            json_match = re.search(r'\{[^}]+\}', text, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                scores = data.get("scores", [])

                # Применяем оценки
                for i, r in enumerate(batch):
                    if i < len(scores):
                        r["relevance_score"] = float(scores[i])
                    else:
                        r["relevance_score"] = 0.5

                return batch

        except Exception as e:
            logger.warning(f"⚠️ LLM scoring failed: {e}")

        # Fallback на эвристику
        return self._heuristic_score(query, batch)

    def _heuristic_score(self, query: str, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Эвристическая оценка релевантности."""
        query_words = set(query.lower().split())

        for result in results:
            text = self._extract_text(result).lower()

            # Базовый score из векторного поиска
            base_score = result.get("score", 0.5)

            # Бонус за совпадение слов
            word_matches = sum(1 for word in query_words if word in text and len(word) > 2)
            word_bonus = min(0.3, word_matches * 0.1)

            # Бонус за точное совпадение фразы
            phrase_bonus = 0.2 if query.lower() in text else 0

            # Итоговый score
            result["relevance_score"] = min(1.0, base_score + word_bonus + phrase_bonus)

        return results

    def _deduplicate(self, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Удалить семантические дубликаты."""
        seen_texts = set()
        deduplicated = []

        for result in results:
            text = self._extract_text(result).lower()[:100]  # Первые 100 символов

            # Простая дедупликация по началу текста
            if text not in seen_texts:
                seen_texts.add(text)
                deduplicated.append(result)

        return deduplicated

    async def group_by_topic(
        self,
        query: str,
        results: List[Dict[str, Any]],
        num_groups: int = 5
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Сгруппировать результаты по темам.

        Args:
            query: Исходный запрос
            results: Результаты для группировки
            num_groups: Количество групп

        Returns:
            Словарь {тема: [результаты]}
        """
        if not results:
            return {}

        # Простая группировка по типу/label
        groups = defaultdict(list)

        for result in results:
            # Определяем группу
            label = result.get("_label") or result.get("metadata", {}).get("label", "Другое")
            groups[label].append(result)

        # Если слишком много групп, объединяем мелкие
        if len(groups) > num_groups:
            sorted_groups = sorted(groups.items(), key=lambda x: -len(x[1]))
            main_groups = dict(sorted_groups[:num_groups-1])

            # Остальные в "Другое"
            other = []
            for _name, items in sorted_groups[num_groups-1:]:
                other.extend(items)
            if other:
                main_groups["Другое"] = other

            return main_groups

        return dict(groups)

    async def rank_by_importance(
        self,
        query: str,
        results: List[Dict[str, Any]],
        context: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        Ранжировать результаты по важности с учётом контекста.

        Args:
            query: Исходный запрос
            results: Результаты для ранжирования
            context: Дополнительный контекст (роль пользователя, проект и т.д.)

        Returns:
            Ранжированные результаты
        """
        context = context or {}

        for result in results:
            importance = result.get("relevance_score", 0.5)

            # Бонус за тип
            label = result.get("_label") or result.get("metadata", {}).get("label", "")
            type_bonuses = {
                "Decision": 0.15,  # Решения важны
                "Task": 0.1,       # Задачи важны
                "Person": 0.1,     # Люди важны
                "KPI": 0.1,        # Метрики важны
                "Meeting": 0.05,   # Встречи менее важны
            }
            importance += type_bonuses.get(label, 0)

            # Бонус за свежесть (если есть дата)
            date = result.get("date") or result.get("metadata", {}).get("date", "")
            if date:
                # Простая проверка на текущий год
                if "2025" in str(date) or "2024" in str(date):
                    importance += 0.05

            # Контекстные бонусы
            if context.get("project"):
                project_name = context["project"].lower()
                text = self._extract_text(result).lower()
                if project_name in text:
                    importance += 0.1

            result["importance_score"] = min(1.0, importance)

        # Сортируем по важности
        return sorted(results, key=lambda x: -x.get("importance_score", 0))

    async def extract_key_insights(
        self,
        query: str,
        results: List[Dict[str, Any]],
        max_insights: int = 5
    ) -> List[str]:
        """
        Извлечь ключевые инсайты из результатов.

        Args:
            query: Исходный запрос
            results: Результаты поиска
            max_insights: Максимальное количество инсайтов

        Returns:
            Список ключевых инсайтов
        """
        if not results:
            return ["Данные по запросу не найдены."]

        insights = []

        # Группируем по типам
        by_type = defaultdict(list)
        for r in results:
            label = r.get("_label") or r.get("metadata", {}).get("label", "other")
            by_type[label].append(r)

        # Генерируем инсайты по типам
        if "Person" in by_type:
            people = [self._extract_text(r)[:50] for r in by_type["Person"][:3]]
            insights.append(f"Упоминаются люди: {', '.join(people)}")

        if "Task" in by_type:
            tasks_count = len(by_type["Task"])
            insights.append(f"Найдено {tasks_count} связанных задач")

        if "Decision" in by_type:
            decisions = by_type["Decision"][:2]
            for d in decisions:
                summary = d.get("summary") or d.get("metadata", {}).get("summary", "")
                if summary:
                    insights.append(f"Решение: {summary[:100]}")

        if "KPI" in by_type:
            kpis = by_type["KPI"][:3]
            for k in kpis:
                name = k.get("name") or k.get("metadata", {}).get("name", "")
                value = k.get("value") or k.get("metadata", {}).get("value", "")
                if name and value:
                    insights.append(f"KPI: {name} = {value}")

        if "Meeting" in by_type:
            meetings_count = len(by_type["Meeting"])
            insights.append(f"Найдено {meetings_count} связанных встреч")

        return insights[:max_insights] if insights else ["Релевантные данные найдены, но ключевые инсайты не выделены."]

    def get_filter_stats(self, original_count: int, filtered_count: int) -> Dict[str, Any]:
        """Получить статистику фильтрации."""
        return {
            "original": original_count,
            "filtered": filtered_count,
            "removed": original_count - filtered_count,
            "removal_rate": round((original_count - filtered_count) / original_count * 100, 1) if original_count > 0 else 0
        }



