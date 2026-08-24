# -*- coding: utf-8 -*-
"""
Goals Tracking Agent - Отслеживание и анализ целей.

Расширенные возможности (адаптировано из tess_old/module_17_goals_tracking):
- Извлечение целей из встреч (явных и неявных)
- Отслеживание прогресса по целям
- **Goal Conflict Detector** — выявление конфликтов между целями (LLM-based)
- **Goal Alignment** — проверка согласованности целей между уровнями (LLM-based)
- **Goal Reasoning** — анализ истинных мотивов за целями (LLM-based)
- Связывание целей с задачами и проектами
"""

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class GoalsTrackingAgent:
    """
    Агент для отслеживания и анализа целей.

    Извлекает цели на всех уровнях:
    - Компания: стратегические цели
    - Отдел: цели подразделений
    - Проект: цели проектов
    - Человек: персональные цели
    - Задача: цели конкретных задач
    """

    def __init__(self, llm_router=None, graph_builder=None):
        self.llm = llm_router
        self.graph = graph_builder

        # Индикаторы целей
        self.goal_keywords = [
            "цель", "достичь", "хотим", "планируем", "стремимся",
            "задача", "результат", "метрика", "KPI", "показатель",
            "стратегия", "видение", "миссия", "направление",
            "ожидаем", "намерены", "собираемся"
        ]

        logger.info("✅ GoalsTrackingAgent initialized")

    async def analyze_goals(
        self,
        meeting_data: Dict[str, Any],
        meeting_id: str,
        context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Полный анализ целей из встречи.

        Args:
            meeting_data: Данные встречи
            meeting_id: ID встречи
            context: Дополнительный контекст

        Returns:
            Результаты анализа целей
        """
        logger.info(f"🎯 Анализ целей для встречи {meeting_id}")

        results = {
            "meeting_id": meeting_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "goals": [],
            "progress_updates": [],
            "conflicts": [],
            "alignment_report": {},
            "statistics": {}
        }

        try:
            # 1. Извлечение целей
            goals = await self._extract_goals(meeting_data, context)
            results["goals"] = goals

            if not goals:
                logger.info("   ℹ️ Цели не найдены")
                return results

            # 2. Анализ причин и мотивов
            enriched_goals = await self._analyze_reasoning(goals, meeting_data)
            results["goals"] = enriched_goals

            # 3. Отслеживание прогресса
            progress = await self._track_progress(enriched_goals, meeting_data)
            results["progress_updates"] = progress

            # 4. Выявление конфликтов
            conflicts = await self._detect_conflicts(enriched_goals)
            results["conflicts"] = conflicts

            # 5. Проверка согласованности
            alignment = await self._check_alignment(enriched_goals)
            results["alignment_report"] = alignment

            # 6. Статистика
            results["statistics"] = self._calculate_statistics(enriched_goals, conflicts)

            logger.info(f"✅ Найдено целей: {len(goals)}, конфликтов: {len(conflicts)}")

            return results

        except Exception as e:
            logger.error(f"❌ Ошибка анализа целей: {e}")
            results["error"] = str(e)
            return results

    async def _extract_goals(
        self,
        meeting_data: Dict[str, Any],
        context: Optional[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Извлекает цели из данных встречи."""
        goals = []

        # Проверяем наличие индикаторов целей
        transcript = meeting_data.get("transcript", "")
        if not self._has_goal_indicators(transcript, meeting_data):
            return goals

        # Извлекаем из транскрипта
        transcript_goals = await self._extract_from_transcript(transcript)
        goals.extend(transcript_goals)

        # Извлекаем из решений
        decisions = meeting_data.get("decisions", [])
        decision_goals = await self._extract_from_decisions(decisions)
        goals.extend(decision_goals)

        # Извлекаем из задач
        tasks = meeting_data.get("tasks", [])
        task_goals = await self._extract_from_tasks(tasks)
        goals.extend(task_goals)

        # Извлекаем из проектов
        projects = meeting_data.get("projects", [])
        project_goals = await self._extract_from_projects(projects)
        goals.extend(project_goals)

        # Отсекаем «цели» из сырых предложений транскрипта: _extract_from_transcript
        # делает целью ЛЮБУЮ фразу с ключевым словом (status="unclear",
        # entity_name="Сотрудник"), из-за чего в UI сыпались обрывки реплик
        # («Саша 27:17 Э, да, мы говорили…»). Это шум, не цели.
        goals = [g for g in goals if g.get("status") != "unclear"]

        # Генерируем ID и дедуплицируем
        unique_goals = self._deduplicate_goals(goals)

        return unique_goals

    def _has_goal_indicators(self, transcript: str, meeting_data: Dict) -> bool:
        """Проверяет наличие индикаторов целей."""
        transcript_lower = transcript.lower()

        keyword_count = sum(1 for kw in self.goal_keywords if kw in transcript_lower)
        has_decisions = len(meeting_data.get("decisions", [])) > 0
        has_projects = len(meeting_data.get("projects", [])) > 0

        return keyword_count >= 2 or has_decisions or has_projects

    async def _extract_from_transcript(self, transcript: str) -> List[Dict[str, Any]]:
        """Извлекает цели из транскрипта."""
        goals = []

        if not transcript:
            return goals

        # Разбиваем на предложения
        sentences = transcript.replace('\n', ' ').split('.')

        for sentence in sentences:
            sentence = sentence.strip()
            if len(sentence) < 10:
                continue

            sentence_lower = sentence.lower()

            # Проверяем наличие индикаторов цели
            if any(kw in sentence_lower for kw in self.goal_keywords):
                # Определяем уровень цели
                level = self._determine_goal_level(sentence)

                goal = {
                    "level": level,
                    "entity_name": self._extract_entity_name(sentence, level),
                    "goal_statement": sentence[:200],
                    "why_reason": "",
                    "why_stated": "",
                    "metrics_quantitative": [],
                    "metrics_qualitative": [],
                    "success_criteria": "",
                    "pathway": "",
                    "timeline": self._extract_timeline(sentence),
                    "status": "unclear",
                    "confidence": 0.5,
                    "is_stated_explicitly": True,
                    "source_quote": sentence[:100],
                    "related_entities": []
                }

                goals.append(goal)

        return goals[:10]  # Ограничиваем количество

    async def _extract_from_decisions(self, decisions: List[Dict]) -> List[Dict[str, Any]]:
        """Извлекает цели из решений."""
        goals = []

        for decision in decisions[:5]:
            decision_text = decision.get("decision", decision.get("description", ""))
            if not decision_text:
                continue

            goal = {
                "level": "task",
                "entity_name": "Решение",
                "goal_statement": f"Реализовать: {decision_text[:150]}",
                "why_reason": decision.get("reasoning", ""),
                "why_stated": "",
                "metrics_quantitative": [],
                "metrics_qualitative": [],
                "success_criteria": "Решение выполнено",
                "pathway": "",
                "timeline": decision.get("deadline", ""),
                "status": "not_started",
                "confidence": 0.7,
                "is_stated_explicitly": True,
                "source_quote": decision_text[:100],
                "related_entities": []
            }

            goals.append(goal)

        return goals

    async def _extract_from_tasks(self, tasks: List[Dict]) -> List[Dict[str, Any]]:
        """Извлекает цели из задач."""
        goals = []

        for task in tasks[:5]:
            task_text = task.get("task", task.get("description", ""))
            if not task_text:
                continue

            goal = {
                "level": "task",
                "entity_name": task.get("assignee", "Задача"),
                "goal_statement": f"Выполнить: {task_text[:150]}",
                "why_reason": "",
                "why_stated": "",
                "metrics_quantitative": [],
                "metrics_qualitative": [],
                "success_criteria": "Задача выполнена",
                "pathway": "",
                "timeline": task.get("deadline", ""),
                "status": task.get("status", "not_started"),
                "confidence": 0.8,
                "is_stated_explicitly": True,
                "source_quote": task_text[:100],
                "related_entities": [task.get("assignee", "")] if task.get("assignee") else []
            }

            goals.append(goal)

        return goals

    async def _extract_from_projects(self, projects: List[Dict]) -> List[Dict[str, Any]]:
        """Извлекает цели из проектов."""
        goals = []

        for project in projects[:5]:
            project_name = project.get("name", project.get("project_name", ""))
            if not project_name:
                continue

            goal = {
                "level": "project",
                "entity_name": project_name,
                "goal_statement": f"Завершить проект: {project_name}",
                "why_reason": project.get("description", ""),
                "why_stated": "",
                "metrics_quantitative": [],
                "metrics_qualitative": [],
                "success_criteria": "Проект завершён",
                "pathway": "",
                "timeline": project.get("deadline", ""),
                "status": project.get("status", "in_progress"),
                "confidence": 0.9,
                "is_stated_explicitly": True,
                "source_quote": project_name,
                "related_entities": []
            }

            goals.append(goal)

        return goals

    def _determine_goal_level(self, text: str) -> str:
        """Определяет уровень цели."""
        text_lower = text.lower()

        if any(kw in text_lower for kw in ["компания", "организация", "стратегия", "миссия"]):
            return "company"
        if any(kw in text_lower for kw in ["отдел", "департамент", "подразделение"]):
            return "department"
        if any(kw in text_lower for kw in ["проект", "продукт", "система"]):
            return "project"
        if any(kw in text_lower for kw in ["я", "мне", "мой", "личн"]):
            return "person"

        return "task"

    def _extract_entity_name(self, text: str, level: str) -> str:
        """Извлекает название сущности."""
        level_defaults = {
            "company": "Компания",
            "department": "Отдел",
            "project": "Проект",
            "person": "Сотрудник",
            "task": "Задача"
        }
        return level_defaults.get(level, "Неизвестно")

    def _extract_timeline(self, text: str) -> str:
        """Извлекает временные рамки."""
        text_lower = text.lower()

        timeline_indicators = {
            "сегодня": "сегодня",
            "завтра": "завтра",
            "неделя": "1 неделя",
            "месяц": "1 месяц",
            "квартал": "1 квартал",
            "год": "1 год"
        }

        for indicator, timeline in timeline_indicators.items():
            if indicator in text_lower:
                return timeline

        return ""

    def _deduplicate_goals(self, goals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Дедуплицирует цели."""
        unique = []
        seen_ids = set()

        for goal in goals:
            # Генерируем ID
            key = f"{goal.get('level', '')}_{goal.get('goal_statement', '')[:50]}"
            goal_id = hashlib.md5(key.encode()).hexdigest()[:16]

            if goal_id not in seen_ids:
                goal["goal_id"] = goal_id
                unique.append(goal)
                seen_ids.add(goal_id)

        return unique

    async def _analyze_reasoning(
        self,
        goals: List[Dict[str, Any]],
        meeting_data: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        Анализирует истинные причины и мотивы целей.

        Определяет:
        - Совпадают ли заявленные и реальные причины
        - Скрытые мотивы (политические, карьерные, личные)
        - Рациональность и достижимость целей
        - Признаки манипуляции или PR
        """
        enriched_goals = []

        transcript = meeting_data.get("transcript", "")
        participants = meeting_data.get("participants", [])

        for goal in goals:
            # Пробуем LLM-based анализ для каждой цели
            if self.llm and transcript:
                try:
                    reasoning = await self._analyze_goal_reasoning_llm(goal, transcript, participants)
                    if reasoning:
                        goal["reasoning_analysis"] = reasoning
                except Exception as e:
                    logger.debug(f"LLM reasoning analysis failed for goal: {e}")

            # Fallback: базовое обогащение
            if not goal.get("why_reason"):
                goal["why_reason"] = "Причина не указана явно"

            # Оцениваем уверенность
            if goal.get("is_stated_explicitly"):
                goal["confidence"] = min(goal.get("confidence", 0.5) + 0.2, 1.0)

            enriched_goals.append(goal)

        return enriched_goals

    async def _analyze_goal_reasoning_llm(
        self,
        goal: Dict[str, Any],
        transcript: str,
        participants: List[Dict]
    ) -> Dict[str, Any]:
        """LLM-based анализ истинных мотивов цели."""
        participants_text = ", ".join([p.get('name', '') for p in participants if p.get('name')])

        prompt = f"""Проанализируй истинные причины и мотивы за целью.

ЦЕЛЬ:
- Уровень: {goal.get('level', '')}
- Сущность: {goal.get('entity_name', '')}
- Формулировка: {goal.get('goal_statement', '')}
- Заявленная причина: {goal.get('why_stated', '') or goal.get('why_reason', '')}

ТРАНСКРИПТ (отрывок):
{transcript[:3000]}

УЧАСТНИКИ: {participants_text}

ЗАДАЧА: Определи мотивы за целью — СТРОГО по свидетельствам из транскрипта.

Правила (это чувствительный анализ о людях — не домысливай):
- Каждый мотив в true_motivations ОБЯЗАН иметь evidence — конкретную фразу/факт
  из встречи. Нет свидетельства → не добавляй мотив.
- По умолчанию считай заявленную причину искренней (stated_vs_real=aligned,
  has_hidden_agenda=false). Отклоняйся ТОЛЬКО при явных сигналах в тексте.
- «Скрытые мотивы», «манипуляция», «PR» — тяжёлые ярлыки; ставь их лишь при
  прямых доказательствах, с низким confidence при сомнении. Презумпция
  добросовестности.
- Если данных мало — честно: alignment=aligned, confidence низкий, мотивы пустые.

Анализируй по этим правилам:
1. Совпадают ли заявленные и реальные причины (только если есть сигналы)?
2. Есть ли ПОДТВЕРЖДЁННЫЕ в тексте дополнительные мотивы?
3. Рациональность и достижимость — по сказанному об этом.

Верни ТОЛЬКО валидный JSON:
{{
  "alignment": {{
    "stated_vs_real": "aligned|partially_aligned|misaligned",
    "confidence": 0.0-1.0,
    "explanation": "Почему совпадают/не совпадают"
  }},
  "true_motivations": [
    {{
      "type": "rational|political|personal|career|defensive|pr",
      "description": "Описание мотива",
      "evidence": "Что в встрече указывает на это"
    }}
  ],
  "goal_quality": {{
    "is_rational": true/false,
    "is_achievable": true/false,
    "is_measurable": true/false,
    "has_hidden_agenda": true/false,
    "quality_score": 0-10
  }},
  "red_flags": ["Тревожный сигнал 1"],
  "recommendations": ["Рекомендация 1"]
}}"""

        try:
            response = await self.llm.generate(
                prompt=prompt,
                system_prompt="Ты - эксперт по организационной психологии. Анализируй истинные мотивы за целями. Возвращай ТОЛЬКО валидный JSON.",
                temperature=0.3
            )

            if not response:
                return {}

            # Парсим JSON
            response = response.strip()
            if response.startswith("```"):
                response = response.split("```")[1]
                if response.startswith("json"):
                    response = response[4:]
            response = response.strip()

            reasoning = json.loads(response)
            return reasoning

        except Exception as e:
            logger.debug(f"LLM reasoning parsing failed: {e}")
            return {}

    async def _track_progress(
        self,
        goals: List[Dict[str, Any]],
        meeting_data: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Отслеживает прогресс по целям."""
        progress_updates = []

        for goal in goals:
            update = {
                "goal_id": goal.get("goal_id", ""),
                "goal_statement": goal.get("goal_statement", "")[:100],
                "previous_status": "unknown",
                "current_status": goal.get("status", "unclear"),
                "progress_percentage": 0,
                "blockers": [],
                "next_steps": [],
                "updated_at": datetime.now(timezone.utc).isoformat()
            }

            # Оцениваем прогресс по статусу
            status_progress = {
                "not_started": 0,
                "in_progress": 50,
                "at_risk": 30,
                "completed": 100,
                "achieved": 100
            }
            update["progress_percentage"] = status_progress.get(goal.get("status", ""), 0)

            progress_updates.append(update)

        return progress_updates

    async def _detect_conflicts(self, goals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Выявляет конфликты между целями.

        Использует LLM для глубокого анализа типов конфликтов:
        - Ресурсный конфликт
        - Приоритетный конфликт
        - Стратегический конфликт
        - Временной конфликт
        - Метрический конфликт
        - Иерархический конфликт
        """
        conflicts = []

        if len(goals) < 2:
            return conflicts

        # Сначала пробуем LLM-based анализ
        if self.llm:
            try:
                llm_conflicts = await self._detect_conflicts_llm(goals)
                if llm_conflicts:
                    return llm_conflicts
            except Exception as e:
                logger.warning(f"LLM conflict detection failed, using fallback: {e}")

        # Fallback: простая проверка пар
        for i, goal1 in enumerate(goals):
            for goal2 in goals[i+1:]:
                conflict = self._check_goal_conflict(goal1, goal2)
                if conflict:
                    conflicts.append(conflict)

        return conflicts

    async def _detect_conflicts_llm(self, goals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """LLM-based выявление конфликтов между целями."""
        # Группируем цели по уровням
        self._group_goals_by_level(goals)

        # Форматируем цели для LLM
        goals_text = []
        for i, goal in enumerate(goals, 1):
            goals_text.append(f"""
{i}. [{goal.get('level', '')}] {goal.get('entity_name', '')}
   ID: {goal.get('goal_id', '')}
   Цель: {goal.get('goal_statement', '')}
   Зачем: {goal.get('why_reason', '')}
   Timeline: {goal.get('timeline', '')}
""")

        prompt = f"""Проанализируй цели и найди противоречия между ними.

ЦЕЛИ:
{"".join(goals_text)}

ТИПЫ КОНФЛИКТОВ:
1. **resource** - Ресурсный конфликт: Цели требуют одних и тех же ресурсов
2. **priority** - Приоритетный конфликт: Цели противоречат по приоритетам
3. **strategic** - Стратегический конфликт: Цели ведут в разные стороны
4. **temporal** - Временной конфликт: Цели не могут быть достигнуты одновременно
5. **metric** - Метрический конфликт: Метрики одной цели вредят другой
6. **hierarchical** - Иерархический конфликт: Цели нижнего уровня противоречат верхнему

Верни ТОЛЬКО валидный JSON массив:
[
  {{
    "conflict_id": "уникальный_id",
    "conflict_type": "resource|priority|strategic|temporal|metric|hierarchical",
    "severity": "low|medium|high|critical",
    "goal1_id": "id первой цели",
    "goal2_id": "id второй цели",
    "description": "Описание конфликта",
    "impact": "Какое влияние оказывает конфликт",
    "resolution_suggestions": ["Предложение 1", "Предложение 2"],
    "who_should_resolve": "Кто должен решить"
  }}
]

Если конфликтов НЕТ - верни []
Будь критичен, но объективен."""

        try:
            response = await self.llm.generate(
                prompt=prompt,
                system_prompt="Ты - эксперт по стратегическому планированию. Находи конфликты между целями и предлагай решения. Возвращай ТОЛЬКО валидный JSON.",
                temperature=0.3
            )

            if not response:
                return []

            # Парсим JSON
            response = response.strip()
            if response.startswith("```"):
                response = response.split("```")[1]
                if response.startswith("json"):
                    response = response[4:]
            response = response.strip()

            conflicts = json.loads(response)

            if isinstance(conflicts, list):
                return conflicts

            return []

        except Exception as e:
            logger.warning(f"LLM conflict parsing failed: {e}")
            return []

    def _group_goals_by_level(self, goals: List[Dict[str, Any]]) -> Dict[str, List[Dict]]:
        """Группирует цели по уровням."""
        grouped = {
            "company": [],
            "department": [],
            "project": [],
            "person": [],
            "task": []
        }

        for goal in goals:
            level = goal.get("level", "unknown")
            if level in grouped:
                grouped[level].append(goal)

        return grouped

    def _check_goal_conflict(
        self,
        goal1: Dict[str, Any],
        goal2: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Проверяет конфликт между двумя целями (fallback без LLM)."""
        # Проверяем конфликт по временным рамкам
        if goal1.get("timeline") and goal2.get("timeline"):
            if goal1["timeline"] == goal2["timeline"]:
                # Потенциальный конфликт ресурсов
                if goal1.get("level") == goal2.get("level"):
                    return {
                        "conflict_id": f"conflict_{goal1.get('goal_id', '')[:8]}_{goal2.get('goal_id', '')[:8]}",
                        "goal1_id": goal1.get("goal_id", ""),
                        "goal2_id": goal2.get("goal_id", ""),
                        "conflict_type": "resource",
                        "severity": "medium",
                        "description": "Возможный конфликт ресурсов при одновременном выполнении",
                        "impact": "Может потребоваться распределение ресурсов",
                        "resolution_suggestions": ["Приоритизировать цели", "Распределить ресурсы"],
                        "who_should_resolve": "Руководитель проекта"
                    }

        return None

    async def _check_alignment(self, goals: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Проверяет согласованность целей между уровнями.

        Анализирует:
        - Вертикальное выравнивание (поддерживают ли цели нижнего уровня цели верхнего)
        - Горизонтальное выравнивание (согласованы ли цели на одном уровне)
        - Каскадирование (правильно ли декомпозируются цели сверху вниз)
        - Единство направления (все ли цели ведут в одну сторону)
        """
        # Базовая структура
        alignment = {
            "alignment_score": 7,
            "status": "partially_aligned",
            "aligned_goals_count": 0,
            "misaligned_goals_count": 0,
            "level_distribution": {},
            "vertical_alignment": {"score": 7, "issues": [], "strong_connections": []},
            "horizontal_alignment": {"score": 7, "issues": [], "synergies": []},
            "cascading_quality": {"score": 7, "well_cascaded": [], "poorly_cascaded": []},
            "recommendations": [],
            "missing_goals": []
        }

        if len(goals) < 2:
            alignment["status"] = "insufficient_data"
            return alignment

        # Подсчёт по уровням
        goals_by_level = self._group_goals_by_level(goals)
        for level, level_goals in goals_by_level.items():
            if level_goals:
                alignment["level_distribution"][level] = len(level_goals)

        # Пробуем LLM-based анализ
        if self.llm:
            try:
                llm_alignment = await self._check_alignment_llm(goals, goals_by_level)
                if llm_alignment and llm_alignment.get("alignment_score"):
                    llm_alignment["level_distribution"] = alignment["level_distribution"]
                    return llm_alignment
            except Exception as e:
                logger.warning(f"LLM alignment check failed, using fallback: {e}")

        # Fallback: простая оценка
        if alignment["level_distribution"].get("company", 0) > 0:
            alignment["alignment_score"] = 8
            alignment["status"] = "aligned"
            alignment["aligned_goals_count"] = len(goals)
        else:
            alignment["aligned_goals_count"] = len(goals) // 2
            alignment["misaligned_goals_count"] = len(goals) - alignment["aligned_goals_count"]

        # Рекомендации
        if alignment["alignment_score"] < 7:
            alignment["recommendations"].append({
                "priority": "high",
                "action": "Определить стратегические цели компании",
                "affected_goals": []
            })
        if not alignment["level_distribution"].get("company"):
            alignment["missing_goals"].append({
                "level": "company",
                "description": "Не хватает целей на уровне компании"
            })

        return alignment

    async def _check_alignment_llm(
        self,
        goals: List[Dict[str, Any]],
        goals_by_level: Dict[str, List[Dict]]
    ) -> Dict[str, Any]:
        """LLM-based анализ согласованности целей."""
        # Форматируем цели по уровням
        levels_text = []
        for level, level_goals in goals_by_level.items():
            if not level_goals:
                continue

            levels_text.append(f"\n## {level.upper()}")
            for i, goal in enumerate(level_goals, 1):
                levels_text.append(f"{i}. {goal.get('entity_name', '')}: {goal.get('goal_statement', '')}")
                levels_text.append(f"   Зачем: {goal.get('why_reason', '')}")

        prompt = f"""Проанализируй согласованность целей между уровнями организации.

ЦЕЛИ ПО УРОВНЯМ:
{"".join(levels_text)}

ЗАДАЧА: Оцени насколько цели согласованы между собой.

Проверь:
1. **Вертикальное выравнивание**: Поддерживают ли цели нижнего уровня цели верхнего
2. **Горизонтальное выравнивание**: Согласованы ли цели на одном уровне
3. **Каскадирование**: Правильно ли декомпозируются цели сверху вниз
4. **Единство направления**: Все ли цели ведут в одну сторону

Верни ТОЛЬКО валидный JSON:
{{
  "alignment_score": 0-10,
  "status": "aligned|partially_aligned|misaligned",
  "aligned_goals_count": число,
  "misaligned_goals_count": число,
  "vertical_alignment": {{
    "score": 0-10,
    "issues": ["Проблема 1"],
    "strong_connections": []
  }},
  "horizontal_alignment": {{
    "score": 0-10,
    "issues": ["Проблема 1"],
    "synergies": ["Синергия 1"]
  }},
  "cascading_quality": {{
    "score": 0-10,
    "well_cascaded": ["Хорошо каскадированная цель"],
    "poorly_cascaded": ["Плохо каскадированная цель"]
  }},
  "recommendations": [
    {{"priority": "high|medium|low", "action": "Что нужно сделать", "affected_goals": []}}
  ],
  "missing_goals": [
    {{"level": "уровень", "description": "Какой цели не хватает"}}
  ]
}}"""

        try:
            response = await self.llm.generate(
                prompt=prompt,
                system_prompt="Ты - эксперт по стратегическому выравниванию целей. Проверяй согласованность целей между уровнями организации. Возвращай ТОЛЬКО валидный JSON.",
                temperature=0.3
            )

            if not response:
                return {}

            # Парсим JSON
            response = response.strip()
            if response.startswith("```"):
                response = response.split("```")[1]
                if response.startswith("json"):
                    response = response[4:]
            response = response.strip()

            alignment = json.loads(response)
            return alignment

        except Exception as e:
            logger.warning(f"LLM alignment parsing failed: {e}")
            return {}

    def _calculate_statistics(
        self,
        goals: List[Dict[str, Any]],
        conflicts: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Рассчитывает статистику."""
        stats = {
            "total_goals": len(goals),
            "by_level": {},
            "by_status": {},
            "conflicts_count": len(conflicts),
            "avg_confidence": 0.0,
            "explicit_count": 0,
            "implicit_count": 0
        }

        if not goals:
            return stats

        for goal in goals:
            # По уровням
            level = goal.get("level", "unknown")
            stats["by_level"][level] = stats["by_level"].get(level, 0) + 1

            # По статусам
            status = goal.get("status", "unknown")
            stats["by_status"][status] = stats["by_status"].get(status, 0) + 1

            # Явные/неявные
            if goal.get("is_stated_explicitly"):
                stats["explicit_count"] += 1
            else:
                stats["implicit_count"] += 1

        # Средняя уверенность
        confidences = [g.get("confidence", 0.5) for g in goals]
        stats["avg_confidence"] = sum(confidences) / len(confidences)

        return stats


