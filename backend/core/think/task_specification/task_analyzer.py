# -*- coding: utf-8 -*-
"""
Task Analyzer Agent - глубокий анализ задачи.

Анализирует:
- Истинные цели и скрытые мотивы
- Стейкхолдеров и их интересы
- Риски и ограничения
- Сложность и приоритет
"""

import json
from typing import Any, Dict, List, Optional

from .base_agent import BaseTaskAgent


class TaskAnalyzerAgent(BaseTaskAgent):
    """Агент глубокого анализа задачи."""

    def __init__(self, storage=None, llm_router=None):
        super().__init__("TaskAnalyzer", storage, llm_router)

    async def analyze(
        self,
        task_description: str,
        task_id: str,
        additional_context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Глубокий анализ задачи.

        Args:
            task_description: Описание задачи
            task_id: ID задачи
            additional_context: Дополнительный контекст

        Returns:
            Результат анализа задачи
        """
        self.log_start(f"Анализ задачи: {task_description[:100]}...")

        # 1. Поиск похожих задач в истории
        similar_tasks = await self._find_similar_tasks(task_description)

        # 2. Извлечение сущностей
        entities = self.extract_entities(task_description)

        # 3. Сбор контекста
        context_data = await self._gather_context(task_description, entities)

        # 4. LLM анализ
        analysis = await self._llm_analyze(
            task_description,
            similar_tasks,
            context_data,
            additional_context
        )

        # 5. P2: Замыкание зависимостей — «что ещё надо сделать, чтобы закрыть».
        # Заземляем на граф + синтезируем actionable-список. Best-effort.
        dependency_closure: Dict[str, Any] = {}
        try:
            from .dependency_closure import compute_dependency_closure
            closure = await compute_dependency_closure(
                task_description=task_description,
                entities=entities,
                llm_analysis={"analysis": analysis},
                graph_search=self.search_graph,
                llm_generate=self.generate_llm_response,
            )
            dependency_closure = closure.to_dict()
        except Exception as exc:  # не валим анализ из-за closure
            self.log_error(f"Не удалось посчитать dependency closure: {exc}")

        # 6. Обогащение метаданными
        result = {
            "task_id": task_id,
            "original_task": task_description,
            "analysis": analysis,
            "dependency_closure": dependency_closure,
            "entities_extracted": entities,
            "similar_tasks_found": len(similar_tasks),
            "context_sources": len(context_data),
            "metadata": {
                "agent": self.name,
                "timestamp": self.get_timestamp(),
                "version": "1.1.0"
            }
        }

        self.log_complete(f"Анализ завершен. Сложность: {analysis.get('complexity', {}).get('level', 'unknown')}")
        return result

    async def _find_similar_tasks(self, task_description: str) -> List[Dict[str, Any]]:
        """Поиск похожих задач в истории."""
        similar = []

        # Векторный поиск
        vector_results = await self.search_vector(
            f"задача {task_description[:200]}",
            top_k=5
        )
        similar.extend(vector_results)

        # Поиск по графу
        graph_results = await self.search_graph(
            f"выполнение задача {task_description[:100]}",
            depth=2
        )
        similar.extend(graph_results)

        return similar[:10]

    async def _gather_context(
        self,
        task_description: str,
        entities: Dict[str, List[str]]
    ) -> List[Dict[str, Any]]:
        """Сбор контекста для задачи."""
        context = []

        # Поиск по описанию
        desc_results = await self.search_vector(task_description, top_k=5)
        context.extend(desc_results)

        # Поиск по проектам
        for project in entities.get("projects", [])[:3]:
            proj_results = await self.search_vector(f"проект {project}", top_k=3)
            context.extend(proj_results)

        # Поиск по технологиям
        for tech in entities.get("technologies", [])[:3]:
            tech_results = await self.search_vector(f"технология {tech}", top_k=2)
            context.extend(tech_results)

        return context[:20]

    async def _get_stakeholder_profiles(self, stakeholder_names: List[str],
                                        user_id: str = "") -> Dict[str, str]:
        """Получить профили стейкхолдеров из Enhanced Snapshots (per-user)."""
        profiles = {}

        try:
            from backend.core.config import flags
            if not flags.enable_enhanced_snapshots:
                return profiles

            from backend.core.sleep.enhanced_snapshot import get_enhanced_snapshot_generator
            generator = get_enhanced_snapshot_generator(user_id=user_id)

            for name in stakeholder_names[:5]:  # Ограничиваем количество
                # Пробуем найти по имени
                person_id = name.lower().replace(" ", "_")
                snapshot = await generator.get_person_snapshot(person_id)

                if snapshot:
                    profiles[name] = snapshot.to_text(max_length=500)
        except Exception as e:
            self.log_error(f"Не удалось получить профили стейкхолдеров: {e}")

        return profiles

    async def _analyze_emotional_priority(self, task_description: str) -> Dict[str, Any]:
        """Анализ эмоциональной приоритетности задачи."""
        try:
            from backend.core.config import flags
            if not flags.enable_emotional_salience:
                return {}

            from backend.core.memory.emotional_salience import get_emotional_analyzer
            analyzer = get_emotional_analyzer()

            result = analyzer.analyze_text(task_description)
            return {
                "urgency_score": result.get("urgency_score", 0),
                "priority_level": result.get("priority_level", "normal"),
                "emotional_factors": result.get("factors", []),
            }
        except Exception as e:
            self.log_error(f"Ошибка анализа эмоциональной приоритетности: {e}")
            return {}

    async def _llm_analyze(
        self,
        task_description: str,
        similar_tasks: List[Dict[str, Any]],
        context_data: List[Dict[str, Any]],
        additional_context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """LLM анализ задачи."""

        # НОВОЕ: Анализ эмоциональной приоритетности
        emotional_analysis = await self._analyze_emotional_priority(task_description)

        emotional_section = ""
        if emotional_analysis:
            emotional_section = f"""
ЭМОЦИОНАЛЬНЫЙ АНАЛИЗ:
- Срочность: {emotional_analysis.get('urgency_score', 0):.1f}/1.0
- Приоритет: {emotional_analysis.get('priority_level', 'normal')}
- Факторы: {', '.join(emotional_analysis.get('emotional_factors', [])[:5])}

Учитывай эту информацию при определении приоритета и сложности задачи.
"""

        system_prompt = f"""Ты — ведущий бизнес-аналитик с экспертизой в управлении проектами.
Твоя задача — провести глубокий анализ задачи.
{emotional_section}
ПРИНЦИПЫ:
1. ГЛУБИНА — анализируй не только поверхностные требования, но и скрытые мотивы
2. КОНТЕКСТ — учитывай весь доступный контекст и исторический опыт
3. ПРАГМАТИЗМ — фокусируйся на практических аспектах выполнения
4. ПРЕДВИДЕНИЕ — прогнозируй потенциальные проблемы

Возвращай ТОЛЬКО валидный JSON без markdown-разметки."""

        similar_summary = self._format_similar_tasks(similar_tasks)
        context_summary = self._format_context(context_data)
        additional_info = json.dumps(additional_context, ensure_ascii=False) if additional_context else "Нет"

        prompt = f"""Проанализируй задачу и верни JSON:

ЗАДАЧА: {task_description}

ПОХОЖИЕ ЗАДАЧИ ИЗ ИСТОРИИ:
{similar_summary}

КОНТЕКСТ ИЗ БАЗЫ ЗНАНИЙ:
{context_summary}

ДОПОЛНИТЕЛЬНЫЙ КОНТЕКСТ:
{additional_info}

Верни JSON со структурой:
{{
    "understanding": {{
        "primary_goal": "основная цель задачи",
        "secondary_goals": ["дополнительные цели"],
        "hidden_requirements": ["неявные требования"],
        "success_criteria": ["критерии успеха"]
    }},
    "classification": {{
        "task_type": "development|analysis|content|planning|design|marketing|other",
        "domain": "предметная область",
        "category": "категория задачи"
    }},
    "complexity": {{
        "level": "low|medium|high|critical",
        "score": 1-10,
        "factors": ["факторы сложности"],
        "estimated_hours": "оценка времени в часах"
    }},
    "stakeholders": {{
        "primary": {{
            "role": "роль основного стейкхолдера",
            "interests": ["интересы"],
            "expectations": ["ожидания"]
        }},
        "secondary": [
            {{"role": "роль", "impact": "влияние"}}
        ]
    }},
    "risks": [
        {{
            "risk": "описание риска",
            "probability": "low|medium|high",
            "impact": "low|medium|high",
            "mitigation": "способ снижения"
        }}
    ],
    "dependencies": {{
        "technical": ["технические зависимости"],
        "business": ["бизнес-зависимости"],
        "resource": ["ресурсные зависимости"]
    }},
    "recommended_approach": {{
        "methodology": "рекомендуемый подход",
        "key_phases": ["ключевые фазы"],
        "critical_path": ["критический путь"]
    }},
    "insights_from_history": {{
        "patterns": ["найденные паттерны"],
        "lessons_learned": ["уроки из прошлого"],
        "best_practices": ["лучшие практики"]
    }}
}}"""

        return await self.generate_llm_response(prompt, system_prompt)

    def _format_similar_tasks(self, tasks: List[Dict[str, Any]]) -> str:
        """Форматирование похожих задач."""
        if not tasks:
            return "Похожие задачи не найдены."

        formatted = []
        for i, task in enumerate(tasks[:5], 1):
            content = task.get('content', task.get('text', str(task)))[:300]
            source = task.get('source', task.get('meeting_id', 'unknown'))
            formatted.append(f"{i}. [{source}] {content}")

        return "\n".join(formatted)

    def _format_context(self, context: List[Dict[str, Any]]) -> str:
        """Форматирование контекста."""
        if not context:
            return "Контекст не найден."

        formatted = []
        for i, item in enumerate(context[:10], 1):
            content = item.get('content', item.get('text', str(item)))[:200]
            formatted.append(f"{i}. {content}")

        return "\n".join(formatted)

