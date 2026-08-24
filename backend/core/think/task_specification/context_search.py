# -*- coding: utf-8 -*-
"""
Context Search Agent - мультимодальный поиск контекста.

Выполняет:
- Векторный поиск по базе знаний
- Графовый поиск связей
- Поиск мнений стейкхолдеров
- Поиск исторических паттернов
"""

import json
from typing import Any, Dict, List

from .base_agent import BaseTaskAgent


class ContextSearchAgent(BaseTaskAgent):
    """Агент мультимодального поиска контекста."""

    def __init__(self, storage=None, llm_router=None):
        super().__init__("ContextSearch", storage, llm_router)

    async def search(
        self,
        task_analysis: Dict[str, Any],
        task_id: str
    ) -> Dict[str, Any]:
        """
        Комплексный поиск контекста для задачи.

        Args:
            task_analysis: Результат анализа задачи
            task_id: ID задачи

        Returns:
            Полный контекст для выполнения задачи
        """
        self.log_start(f"Поиск контекста для задачи: {task_id}")

        # Извлекаем параметры поиска
        search_params = self._extract_search_params(task_analysis)

        # 1. Многоуровневый поиск
        context_layers = await self._multilayer_search(search_params)

        # 2. Поиск мнений стейкхолдеров
        stakeholder_context = await self._search_stakeholder_opinions(search_params)

        # 3. Поиск исторических паттернов
        historical_patterns = await self._search_historical_patterns(search_params)

        # 4. Поиск связанных проектов
        related_projects = await self._search_related_projects(search_params)

        # 5. Анализ и ранжирование контекста
        analyzed_context = await self._analyze_context({
            "layers": context_layers,
            "stakeholders": stakeholder_context,
            "history": historical_patterns,
            "projects": related_projects
        }, search_params)

        result = {
            "task_id": task_id,
            "search_params": search_params,
            "context": {
                "layers": context_layers,
                "stakeholders": stakeholder_context,
                "history": historical_patterns,
                "projects": related_projects
            },
            "analysis": analyzed_context,
            "stats": {
                "total_sources": self._count_sources(context_layers),
                "stakeholder_insights": len(stakeholder_context.get("insights", [])),
                "historical_patterns": len(historical_patterns.get("success_patterns", [])),
                "related_projects": len(related_projects)
            },
            "metadata": {
                "agent": self.name,
                "timestamp": self.get_timestamp()
            }
        }

        self.log_complete(f"Найдено {result['stats']['total_sources']} источников контекста")
        return result

    def _extract_search_params(self, task_analysis: Dict[str, Any]) -> Dict[str, Any]:
        """Извлечение параметров поиска из анализа."""
        analysis = task_analysis.get("analysis", {})

        return {
            "task_description": task_analysis.get("original_task", ""),
            "primary_goal": analysis.get("understanding", {}).get("primary_goal", ""),
            "task_type": analysis.get("classification", {}).get("task_type", ""),
            "domain": analysis.get("classification", {}).get("domain", ""),
            "stakeholders": analysis.get("stakeholders", {}),
            "dependencies": analysis.get("dependencies", {}),
            "technologies": task_analysis.get("entities_extracted", {}).get("technologies", []),
            "projects": task_analysis.get("entities_extracted", {}).get("projects", [])
        }

    async def _get_top_mentioned_entities(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Получить топ упоминаемых сущностей для приоритизации."""
        try:
            from backend.core.config import flags
            if not flags.enable_frequency_tracking:
                return []

            from backend.core.memory.frequency_tracker import get_frequency_tracker
            tracker = get_frequency_tracker()
            return tracker.get_top_mentioned(limit=limit)
        except Exception as e:
            self.log_error(f"Не удалось получить top_mentioned: {e}")
            return []

    async def _multilayer_search(self, params: Dict[str, Any]) -> Dict[str, List[Dict]]:
        """Многоуровневый поиск контекста с учётом весов."""
        layers = {}

        # НОВОЕ: Получаем топ упоминаемых сущностей для приоритизации
        top_entities = await self._get_top_mentioned_entities(limit=10)
        if top_entities:
            layers["top_mentioned"] = [
                {
                    "id": e.get("id", ""),
                    "name": e.get("name", ""),
                    "frequency_score": e.get("frequency_score", 0),
                    "total_mentions": e.get("total_mentions", 0),
                    "source": "frequency_tracker"
                }
                for e in top_entities[:5]
            ]
            self.log_info(f"Найдено {len(top_entities)} часто упоминаемых сущностей")

        # Слой 1: Поиск по основной цели
        if params.get("primary_goal"):
            layers["goal_context"] = await self.search_vector(
                f"цель {params['primary_goal']}", top_k=8
            )

        # Слой 2: Поиск по типу задачи
        if params.get("task_type"):
            layers["type_context"] = await self.search_vector(
                f"задача {params['task_type']}", top_k=6
            )

        # Слой 3: Поиск по домену
        if params.get("domain"):
            layers["domain_context"] = await self.search_vector(
                f"область {params['domain']}", top_k=5
            )

        # Слой 4: Поиск по технологиям
        tech_context = []
        for tech in params.get("technologies", [])[:3]:
            results = await self.search_vector(f"технология {tech}", top_k=3)
            tech_context.extend(results)
        if tech_context:
            layers["tech_context"] = tech_context

        # Слой 5: Поиск по проектам
        project_context = []
        for project in params.get("projects", [])[:3]:
            results = await self.search_vector(f"проект {project}", top_k=3)
            project_context.extend(results)
        if project_context:
            layers["project_context"] = project_context

        # Слой 6: Графовый поиск связей
        if params.get("task_description"):
            layers["graph_context"] = await self.search_graph(
                params["task_description"][:200], depth=3
            )

        return layers

    async def _search_stakeholder_opinions(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Поиск мнений и предпочтений стейкхолдеров."""
        stakeholder_data = params.get("stakeholders", {})
        result = {"insights": [], "preferences": [], "decisions": []}

        # Основной стейкхолдер
        primary = stakeholder_data.get("primary", {})
        if primary.get("role"):
            role = primary["role"]

            opinions = await self.search_vector(f"мнение {role}", top_k=3)
            result["insights"].extend(opinions)

            prefs = await self.search_vector(f"предпочтения {role}", top_k=3)
            result["preferences"].extend(prefs)

        # Вторичные стейкхолдеры
        for secondary in stakeholder_data.get("secondary", [])[:3]:
            if secondary.get("role"):
                opinions = await self.search_vector(
                    f"мнение {secondary['role']}", top_k=2
                )
                result["insights"].extend(opinions)

        return result

    async def _search_historical_patterns(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Поиск исторических паттернов успеха и неудач."""
        task_type = params.get("task_type", "")
        goal = params.get("primary_goal", "")

        result = {
            "success_patterns": [],
            "failure_patterns": [],
            "lessons_learned": []
        }

        # Успешные паттерны
        success_queries = [
            f"успешно {task_type}",
            f"успех {goal[:50]}",
            "лучшие практики",
            "положительный результат"
        ]

        for query in success_queries:
            if query.strip():
                results = await self.search_vector(query, top_k=3)
                result["success_patterns"].extend(results)

        # Паттерны неудач
        failure_queries = [
            f"проблемы {task_type}",
            f"ошибки {goal[:50]}",
            "негативный опыт"
        ]

        for query in failure_queries:
            if query.strip():
                results = await self.search_vector(query, top_k=2)
                result["failure_patterns"].extend(results)

        # Уроки
        lessons = await self.search_vector("уроки опыт выводы", top_k=3)
        result["lessons_learned"] = lessons

        return result

    async def _search_related_projects(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Поиск связанных проектов."""
        related = []

        # По типу задачи
        if params.get("task_type"):
            results = await self.search_vector(
                f"проект {params['task_type']}", top_k=4
            )
            related.extend(results)

        # По домену
        if params.get("domain"):
            results = await self.search_vector(
                f"проект {params['domain']}", top_k=3
            )
            related.extend(results)

        # Дедупликация
        seen_ids = set()
        unique = []
        for item in related:
            item_id = item.get('id', str(item))
            if item_id not in seen_ids:
                seen_ids.add(item_id)
                unique.append(item)

        return unique[:10]

    async def _analyze_context(
        self,
        raw_context: Dict[str, Any],
        params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """LLM анализ и ранжирование контекста."""

        system_prompt = """Ты — эксперт по анализу контекста.
Твоя задача — проанализировать найденный контекст и выделить ключевые инсайты.

Возвращай ТОЛЬКО валидный JSON без markdown-разметки."""

        context_summary = self._format_context_summary(raw_context)

        prompt = f"""Проанализируй контекст для задачи и верни JSON:

ПАРАМЕТРЫ ЗАДАЧИ:
{json.dumps(params, ensure_ascii=False, indent=2)}

НАЙДЕННЫЙ КОНТЕКСТ:
{context_summary}

Верни JSON со структурой:
{{
    "key_insights": [
        {{
            "insight": "ключевой инсайт",
            "relevance": "high|medium|low",
            "source": "откуда получен"
        }}
    ],
    "stakeholder_profile": {{
        "communication_style": "стиль коммуникации",
        "decision_pattern": "паттерн принятия решений",
        "key_concerns": ["основные беспокойства"]
    }},
    "success_factors": [
        {{
            "factor": "фактор успеха",
            "importance": "high|medium|low",
            "how_to_apply": "как применить"
        }}
    ],
    "risk_factors": [
        {{
            "risk": "риск из контекста",
            "mitigation": "как избежать"
        }}
    ],
    "contextual_requirements": {{
        "must_have": ["обязательные элементы"],
        "nice_to_have": ["желательные элементы"],
        "constraints": ["ограничения"]
    }},
    "recommended_approach": {{
        "strategy": "рекомендуемая стратегия",
        "key_steps": ["ключевые шаги"],
        "resources_needed": ["необходимые ресурсы"]
    }}
}}"""

        return await self.generate_llm_response(prompt, system_prompt)

    def _format_context_summary(self, context: Dict[str, Any]) -> str:
        """Форматирование контекста для LLM."""
        sections = []

        # Слои контекста
        layers = context.get("layers", {})
        for layer_name, items in layers.items():
            if items:
                sections.append(f"\n=== {layer_name.upper()} ({len(items)} элементов) ===")
                for item in items[:3]:
                    content = item.get('content', item.get('text', str(item)))[:150]
                    sections.append(f"- {content}")

        # Стейкхолдеры
        stakeholders = context.get("stakeholders", {})
        if stakeholders.get("insights"):
            sections.append(f"\n=== МНЕНИЯ СТЕЙКХОЛДЕРОВ ({len(stakeholders['insights'])} элементов) ===")
            for item in stakeholders["insights"][:3]:
                content = item.get('content', str(item))[:150]
                sections.append(f"- {content}")

        # История
        history = context.get("history", {})
        if history.get("success_patterns"):
            sections.append(f"\n=== УСПЕШНЫЕ ПАТТЕРНЫ ({len(history['success_patterns'])} элементов) ===")
            for item in history["success_patterns"][:3]:
                content = item.get('content', str(item))[:150]
                sections.append(f"- {content}")

        return "\n".join(sections) if sections else "Контекст не найден"

    def _count_sources(self, layers: Dict[str, List]) -> int:
        """Подсчет общего количества источников."""
        total = 0
        for items in layers.values():
            if isinstance(items, list):
                total += len(items)
        return total

