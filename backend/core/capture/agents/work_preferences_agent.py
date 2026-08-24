# -*- coding: utf-8 -*-
"""
Work Preferences Agent - Анализ рабочих предпочтений участников.

Определяет:
1. Предпочтительный режим работы (удалённо/офис/гибрид)
2. Стиль работы (самостоятельно/в команде)
3. Предпочтения по задачам (творческие/рутинные/аналитические)
4. Временные предпочтения (утро/вечер, дедлайны)
5. Предпочтения по обратной связи
6. Условия для продуктивности

Адаптировано из tess_old/module_01_nlp_analysis/work_preferences_agent.py
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class WorkPreferencesProfile:
    """Профиль рабочих предпочтений"""
    name: str

    # Режим работы
    work_mode_preference: str = ""  # remote/office/hybrid
    collaboration_style: str = ""  # solo/team/mixed

    # Типы задач
    preferred_task_types: List[str] = field(default_factory=list)
    avoided_task_types: List[str] = field(default_factory=list)

    # Временные предпочтения
    peak_productivity_time: str = ""  # morning/afternoon/evening
    deadline_approach: str = ""  # early_bird/just_in_time/procrastinator
    meeting_preferences: str = ""  # minimal/moderate/frequent

    # Обратная связь
    feedback_preference: str = ""  # frequent/periodic/as_needed
    recognition_style: str = ""  # public/private/both

    # Условия продуктивности
    productivity_conditions: List[str] = field(default_factory=list)
    distractions: List[str] = field(default_factory=list)

    # Автономность
    autonomy_level: str = ""  # high/medium/low
    structure_preference: str = ""  # structured/flexible/adaptive


class WorkPreferencesAgent:
    """
    Агент анализа рабочих предпочтений.

    Помогает понять как человек предпочитает работать
    для оптимизации рабочих процессов и назначения задач.
    """

    def __init__(self, llm_router=None, graph_builder=None):
        self.name = "work_preferences_agent"
        self.version = "1.0.0"
        self.llm = llm_router
        self.graph = graph_builder

        logger.info(f"⚙️ {self.name} v{self.version} initialized")

    def set_llm(self, llm_router):
        """Устанавливает LLM роутер"""
        self.llm = llm_router

    async def analyze(
        self,
        transcript: str,
        participants: List[Dict[str, Any]],
        meeting_context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Анализирует рабочие предпочтения участников.

        Args:
            transcript: Транскрипт встречи
            participants: Список участников
            meeting_context: Контекст встречи

        Returns:
            Анализ рабочих предпочтений
        """
        start_time = datetime.now(timezone.utc)
        logger.info(f"⚙️ Analyzing work preferences for {len(participants)} participants")

        result = {
            "preference_profiles": [],
            "team_work_patterns": {},
            "compatibility_matrix": [],
            "task_assignment_insights": [],
            "recommendations": [],
            "stats": {}
        }

        if not self.llm:
            logger.warning("LLM not available for work preferences analysis")
            return result

        try:
            # 1. Анализируем предпочтения каждого участника
            result["preference_profiles"] = await self._analyze_individual_preferences(
                transcript, participants, meeting_context
            )

            # 2. Анализируем командные паттерны работы
            result["team_work_patterns"] = await self._analyze_team_patterns(
                result["preference_profiles"]
            )

            # 3. Строим матрицу совместимости
            result["compatibility_matrix"] = await self._build_compatibility_matrix(
                result["preference_profiles"]
            )

            # 4. Генерируем инсайты для назначения задач
            result["task_assignment_insights"] = await self._generate_task_insights(
                result["preference_profiles"]
            )

            # 5. Генерируем рекомендации
            result["recommendations"] = await self._generate_recommendations(
                result["preference_profiles"],
                result["team_work_patterns"]
            )

            # Статистика
            duration = (datetime.now(timezone.utc) - start_time).total_seconds()
            result["stats"] = {
                "duration_seconds": round(duration, 2),
                "profiles_analyzed": len(result["preference_profiles"]),
                "compatibility_pairs": len(result["compatibility_matrix"]),
                "recommendations_count": len(result["recommendations"])
            }

            logger.info(f"✅ Work preferences analysis complete in {duration:.2f}s")

        except Exception as e:
            logger.error(f"❌ Work preferences analysis error: {e}", exc_info=True)
            result["error"] = str(e)

        return result

    async def _analyze_individual_preferences(
        self,
        transcript: str,
        participants: List[Dict[str, Any]],
        meeting_context: Optional[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Анализирует рабочие предпочтения каждого участника"""
        profiles = []

        participants_to_analyze = participants[:10]

        for participant in participants_to_analyze:
            name = participant.get("name") or participant.get("participant_name") or "Unknown"
            role = participant.get("role") or ""

            participant_speech = self._extract_participant_speech(transcript, name)

            if len(participant_speech) < 50:
                continue

            prompt = f"""
Проанализируй РАБОЧИЕ ПРЕДПОЧТЕНИЯ участника на основе его высказываний.

УЧАСТНИК: {name}
РОЛЬ: {role}

ВЫСКАЗЫВАНИЯ:
{participant_speech[:3000]}

Определи предпочтения в формате JSON:
{{
    "name": "{name}",

    "work_mode": {{
        "preference": "remote/office/hybrid (если можно определить)",
        "flexibility_need": "high/medium/low",
        "evidence": "на чём основан вывод"
    }},

    "collaboration_style": {{
        "preference": "solo/team/mixed",
        "team_size_preference": "small/medium/large",
        "cross_functional_comfort": "high/medium/low"
    }},

    "task_preferences": {{
        "preferred_types": ["типы задач, которые нравятся"],
        "strengths_tasks": ["задачи, в которых силён"],
        "avoided_types": ["типы задач, которых избегает"],
        "challenge_appetite": "high/medium/low"
    }},

    "time_management": {{
        "peak_productivity": "morning/afternoon/evening/flexible",
        "deadline_style": "early_finisher/steady_pace/last_minute",
        "multitasking": "prefers/tolerates/avoids",
        "meeting_tolerance": "minimal/moderate/high"
    }},

    "feedback_preferences": {{
        "frequency": "continuous/periodic/milestone_based",
        "format": "written/verbal/both",
        "detail_level": "high_level/detailed",
        "recognition_style": "public/private/both"
    }},

    "autonomy_needs": {{
        "level": "high/medium/low",
        "decision_making": "independent/consultative/delegated",
        "guidance_need": "minimal/moderate/high",
        "structure_preference": "structured/flexible/adaptive"
    }},

    "productivity_factors": {{
        "enablers": ["что помогает быть продуктивным"],
        "blockers": ["что мешает продуктивности"],
        "ideal_conditions": ["идеальные условия для работы"]
    }},

    "management_tips": ["как эффективно управлять этим сотрудником"],
    "task_assignment_tips": ["какие задачи лучше назначать"]
}}

ВАЖНО — ПРЕДПОЧТЕНИЯ ТОЛЬКО ИЗ СКАЗАННОГО:
- Основывайся ТОЛЬКО на том, что человек реально говорил о своей работе, ритме,
  форматах, инструментах в этих фрагментах. Не додумывай предпочтения.
- Данных недостаточно → ставь "unknown", а не правдоподобную догадку.
- Наблюдение о рабочем стиле, а не оценка человека; формулируй нейтрально.
"""

            try:
                response = await self.llm.generate(prompt)
                if response:
                    profile = self._parse_json_response(response)
                    if profile:
                        profile["name"] = name
                        profile["role"] = role
                        profiles.append(profile)
            except Exception as e:
                logger.error(f"Error analyzing work preferences for {name}: {e}")

        return profiles

    async def _analyze_team_patterns(
        self,
        profiles: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Анализирует командные паттерны работы"""
        if not profiles:
            return {}

        # Агрегируем данные
        work_modes = []
        collaboration_styles = []
        deadline_styles = []
        autonomy_levels = []

        for p in profiles:
            work_mode = p.get("work_mode", {})
            if isinstance(work_mode, dict):
                work_modes.append(work_mode.get("preference", "unknown"))

            collab = p.get("collaboration_style", {})
            if isinstance(collab, dict):
                collaboration_styles.append(collab.get("preference", "unknown"))

            time_mgmt = p.get("time_management", {})
            if isinstance(time_mgmt, dict):
                deadline_styles.append(time_mgmt.get("deadline_style", "unknown"))

            autonomy = p.get("autonomy_needs", {})
            if isinstance(autonomy, dict):
                autonomy_levels.append(autonomy.get("level", "unknown"))

        from collections import Counter

        return {
            "work_mode_distribution": dict(Counter(work_modes)),
            "collaboration_distribution": dict(Counter(collaboration_styles)),
            "deadline_style_distribution": dict(Counter(deadline_styles)),
            "autonomy_distribution": dict(Counter(autonomy_levels)),
            "team_size": len(profiles),
            "diversity_score": self._calculate_diversity_score(profiles)
        }

    def _calculate_diversity_score(self, profiles: List[Dict[str, Any]]) -> float:
        """Вычисляет показатель разнообразия предпочтений в команде"""
        if len(profiles) < 2:
            return 0.0

        # Простой расчёт на основе уникальности предпочтений
        unique_prefs = set()
        for p in profiles:
            work_mode = p.get("work_mode", {})
            if isinstance(work_mode, dict):
                unique_prefs.add(work_mode.get("preference", ""))

            collab = p.get("collaboration_style", {})
            if isinstance(collab, dict):
                unique_prefs.add(collab.get("preference", ""))

        # Нормализуем от 0 до 1
        max_possible = len(profiles) * 2  # work_mode + collaboration
        return min(len(unique_prefs) / max_possible, 1.0)

    async def _build_compatibility_matrix(
        self,
        profiles: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Строит матрицу совместимости для совместной работы"""
        if len(profiles) < 2:
            return []

        compatibility = []

        for i, p1 in enumerate(profiles):
            for p2 in profiles[i+1:]:
                name1 = p1.get("name", "")
                name2 = p2.get("name", "")

                # Оцениваем совместимость
                score = 0.5  # Базовый балл
                factors = []

                # Совместимость по стилю коллаборации
                collab1 = p1.get("collaboration_style", {})
                collab2 = p2.get("collaboration_style", {})

                if isinstance(collab1, dict) and isinstance(collab2, dict):
                    if collab1.get("preference") == collab2.get("preference"):
                        score += 0.15
                        factors.append("same_collaboration_style")
                    elif collab1.get("preference") == "mixed" or collab2.get("preference") == "mixed":
                        score += 0.1
                        factors.append("flexible_collaboration")

                # Совместимость по автономности
                auto1 = p1.get("autonomy_needs", {})
                auto2 = p2.get("autonomy_needs", {})

                if isinstance(auto1, dict) and isinstance(auto2, dict):
                    # Комплементарность: один высокий + один низкий = хорошо для менторства
                    levels = [auto1.get("level", ""), auto2.get("level", "")]
                    if "high" in levels and "low" in levels:
                        score += 0.1
                        factors.append("complementary_autonomy")
                    elif levels[0] == levels[1]:
                        score += 0.05
                        factors.append("similar_autonomy")

                # Совместимость по времени
                time1 = p1.get("time_management", {})
                time2 = p2.get("time_management", {})

                if isinstance(time1, dict) and isinstance(time2, dict):
                    if time1.get("peak_productivity") == time2.get("peak_productivity"):
                        score += 0.1
                        factors.append("same_peak_time")

                compatibility.append({
                    "pair": [name1, name2],
                    "compatibility_score": round(min(score, 1.0), 2),
                    "positive_factors": factors,
                    "collaboration_potential": "high" if score > 0.7 else "medium" if score > 0.5 else "low",
                    "recommended_tasks": self._suggest_joint_tasks(p1, p2)
                })

        # Сортируем по совместимости
        compatibility.sort(key=lambda x: x["compatibility_score"], reverse=True)

        return compatibility

    def _suggest_joint_tasks(
        self,
        p1: Dict[str, Any],
        p2: Dict[str, Any]
    ) -> List[str]:
        """Предлагает типы задач для совместной работы"""
        suggestions = []

        # Находим пересечение предпочитаемых задач
        tasks1 = p1.get("task_preferences", {})
        tasks2 = p2.get("task_preferences", {})

        if isinstance(tasks1, dict) and isinstance(tasks2, dict):
            preferred1 = set(tasks1.get("preferred_types", []))
            preferred2 = set(tasks2.get("preferred_types", []))

            common = preferred1 & preferred2
            if common:
                suggestions.extend(list(common)[:3])

        if not suggestions:
            suggestions.append("collaborative_projects")

        return suggestions

    async def _generate_task_insights(
        self,
        profiles: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Генерирует инсайты для назначения задач"""
        insights = []

        for profile in profiles:
            name = profile.get("name", "")
            task_prefs = profile.get("task_preferences", {})
            autonomy = profile.get("autonomy_needs", {})

            insight = {
                "participant": name,
                "best_suited_for": [],
                "avoid_assigning": [],
                "optimal_conditions": [],
                "management_approach": ""
            }

            if isinstance(task_prefs, dict):
                insight["best_suited_for"] = task_prefs.get("preferred_types", [])[:5]
                insight["avoid_assigning"] = task_prefs.get("avoided_types", [])[:3]

            if isinstance(autonomy, dict):
                level = autonomy.get("level", "medium")
                if level == "high":
                    insight["management_approach"] = "Минимальный контроль, чёткие цели, свобода в методах"
                    insight["optimal_conditions"].append("независимая работа")
                elif level == "low":
                    insight["management_approach"] = "Регулярные check-ins, чёткие инструкции, поддержка"
                    insight["optimal_conditions"].append("структурированные задачи")
                else:
                    insight["management_approach"] = "Баланс автономии и поддержки"

            # Добавляем из productivity_factors
            prod = profile.get("productivity_factors", {})
            if isinstance(prod, dict):
                insight["optimal_conditions"].extend(prod.get("enablers", [])[:3])

            insights.append(insight)

        return insights

    async def _generate_recommendations(
        self,
        profiles: List[Dict[str, Any]],
        team_patterns: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Генерирует рекомендации по организации работы"""
        if not profiles:
            return []

        prompt = f"""
На основе рабочих предпочтений команды, сгенерируй РЕКОМЕНДАЦИИ.

ПРОФИЛИ:
{json.dumps([{{"name": p.get("name"), "work_mode": p.get("work_mode"), "collaboration_style": p.get("collaboration_style"), "autonomy_needs": p.get("autonomy_needs")}} for p in profiles[:5]], ensure_ascii=False)}

КОМАНДНЫЕ ПАТТЕРНЫ:
{json.dumps(team_patterns, ensure_ascii=False)}

Создай рекомендации:

Формат (JSON массив):
[
    {{
        "category": "work_organization/task_assignment/meetings/collaboration/productivity",
        "target": "имя или 'команда'",
        "recommendation": "конкретная рекомендация",
        "rationale": "почему это важно",
        "implementation": "как реализовать",
        "expected_benefit": "ожидаемый эффект",
        "priority": "high/medium/low"
    }}
]

Дай 5-8 практических рекомендаций.
"""

        try:
            response = await self.llm.generate(prompt)
            if response:
                recs = self._parse_json_response(response)
                if isinstance(recs, list):
                    return recs
        except Exception as e:
            logger.error(f"Error generating recommendations: {e}")

        return []

    def _extract_participant_speech(self, transcript: str, name: str) -> str:
        """Извлекает высказывания участника"""
        if not transcript or not name:
            return ""

        speeches = []
        lines = transcript.split('\n')

        name.lower()
        first_name = name.split()[0].lower() if name else ""

        speaker_patterns = [
            f"{name}:",
            f"{name.upper()}:",
            f"[{name}]",
        ]
        if first_name:
            speaker_patterns.append(f"{first_name.capitalize()}:")

        for line in lines:
            line_stripped = line.strip()
            is_speaker = any(
                line_stripped.lower().startswith(p.lower())
                for p in speaker_patterns
            )

            if is_speaker:
                for p in speaker_patterns:
                    if line_stripped.lower().startswith(p.lower()):
                        text = line_stripped[len(p):].strip()
                        if text:
                            speeches.append(text)
                        break

        return '\n'.join(speeches)

    def _parse_json_response(self, response: str) -> Any:
        """Парсит JSON из ответа LLM"""
        if not response:
            return None

        response = response.strip()
        if response.startswith('```json'):
            response = response[7:]
        if response.startswith('```'):
            response = response[3:]
        if response.endswith('```'):
            response = response[:-3]

        try:
            return json.loads(response.strip())
        except json.JSONDecodeError:
            import re
            json_match = re.search(r'[\[\{].*[\]\}]', response, re.DOTALL)
            if json_match:
                try:
                    return json.loads(json_match.group())
                except Exception:
                    logger.debug("suppressed exception", exc_info=True)

        return None


