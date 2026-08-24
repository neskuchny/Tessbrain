# -*- coding: utf-8 -*-
"""
Personal Interests Agent - Выявление личных интересов участников.

Анализирует:
1. Профессиональные интересы и увлечения
2. Темы, которые вызывают энтузиазм
3. Личные проекты и инициативы
4. Ценности и приоритеты
5. Карьерные устремления
6. Хобби и внерабочие интересы (если упоминаются)

Адаптировано из tess_old/module_01_nlp_analysis/personal_interests_agent.py
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class PersonalInterestsProfile:
    """Профиль интересов участника"""
    name: str

    # Профессиональные интересы
    professional_interests: List[str] = field(default_factory=list)
    expertise_areas: List[str] = field(default_factory=list)
    learning_interests: List[str] = field(default_factory=list)

    # Темы энтузиазма
    enthusiasm_topics: List[str] = field(default_factory=list)
    passion_indicators: List[str] = field(default_factory=list)

    # Ценности
    core_values: List[str] = field(default_factory=list)
    priorities: List[str] = field(default_factory=list)

    # Карьера
    career_aspirations: List[str] = field(default_factory=list)
    growth_areas: List[str] = field(default_factory=list)

    # Личное (если упоминается)
    personal_mentions: List[str] = field(default_factory=list)
    hobbies: List[str] = field(default_factory=list)


class PersonalInterestsAgent:
    """
    Агент выявления личных интересов.

    Помогает понять что мотивирует человека, какие темы
    его увлекают и как лучше с ним взаимодействовать.
    """

    def __init__(self, llm_router=None, graph_builder=None):
        self.name = "personal_interests_agent"
        self.version = "1.0.0"
        self.llm = llm_router
        self.graph = graph_builder

        logger.info(f"🎯 {self.name} v{self.version} initialized")

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
        Анализирует интересы участников.

        Args:
            transcript: Транскрипт встречи
            participants: Список участников
            meeting_context: Контекст встречи

        Returns:
            Анализ интересов
        """
        start_time = datetime.now(timezone.utc)
        logger.info(f"🎯 Analyzing personal interests for {len(participants)} participants")

        result = {
            "interest_profiles": [],
            "team_interests": {},
            "common_ground": [],
            "engagement_insights": [],
            "recommendations": [],
            "stats": {}
        }

        if not self.llm:
            logger.warning("LLM not available for interests analysis")
            return result

        try:
            # 1. Анализируем интересы каждого участника
            result["interest_profiles"] = await self._analyze_individual_interests(
                transcript, participants, meeting_context
            )

            # 2. Выявляем командные интересы
            result["team_interests"] = await self._analyze_team_interests(
                result["interest_profiles"]
            )

            # 3. Находим общие точки соприкосновения
            result["common_ground"] = await self._find_common_ground(
                result["interest_profiles"]
            )

            # 4. Анализируем вовлечённость
            result["engagement_insights"] = await self._analyze_engagement(
                transcript, participants, result["interest_profiles"]
            )

            # 5. Генерируем рекомендации
            result["recommendations"] = await self._generate_recommendations(
                result["interest_profiles"],
                result["common_ground"]
            )

            # Статистика
            duration = (datetime.now(timezone.utc) - start_time).total_seconds()
            result["stats"] = {
                "duration_seconds": round(duration, 2),
                "profiles_analyzed": len(result["interest_profiles"]),
                "common_interests_found": len(result["common_ground"]),
                "recommendations_count": len(result["recommendations"])
            }

            logger.info(f"✅ Interests analysis complete in {duration:.2f}s")

        except Exception as e:
            logger.error(f"❌ Interests analysis error: {e}", exc_info=True)
            result["error"] = str(e)

        return result

    async def _analyze_individual_interests(
        self,
        transcript: str,
        participants: List[Dict[str, Any]],
        meeting_context: Optional[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Анализирует интересы каждого участника"""
        profiles = []

        participants_to_analyze = participants[:10]

        for participant in participants_to_analyze:
            name = participant.get("name") or participant.get("participant_name") or "Unknown"
            role = participant.get("role") or ""

            # Извлекаем высказывания участника
            participant_speech = self._extract_participant_speech(transcript, name)

            if len(participant_speech) < 50:
                continue

            prompt = f"""
Проанализируй ИНТЕРЕСЫ участника на основе его высказываний на встрече.

УЧАСТНИК: {name}
РОЛЬ: {role}

ВЫСКАЗЫВАНИЯ:
{participant_speech[:3000]}

Определи интересы и создай профиль в формате JSON:
{{
    "name": "{name}",

    "professional_interests": [
        {{
            "topic": "тема/область интереса",
            "intensity": "high/medium/low",
            "evidence": "на чём основан вывод"
        }}
    ],

    "expertise_areas": ["области, в которых человек демонстрирует экспертизу"],

    "learning_interests": ["чему хочет научиться, что изучает"],

    "enthusiasm_topics": [
        {{
            "topic": "тема, вызывающая энтузиазм",
            "indicators": ["признаки энтузиазма в речи"]
        }}
    ],

    "core_values": ["ценности, которые проявляются в высказываниях"],

    "priorities": ["что важно для этого человека"],

    "career_signals": {{
        "aspirations": ["карьерные устремления если проявляются"],
        "growth_interests": ["области для профессионального роста"],
        "leadership_interest": "low/medium/high"
    }},

    "personal_mentions": ["личные темы, если упоминаются (хобби, семья и т.д.)"],

    "engagement_triggers": ["темы, которые повышают вовлечённость"],
    "disengagement_signs": ["темы, которые снижают интерес"],

    "motivation_insights": ["что мотивирует этого человека"],

    "interaction_tips": ["как использовать интересы для лучшего взаимодействия"]
}}

ВАЖНО:
- Основывайся ТОЛЬКО на том, что человек сам сказал о своих интересах/жизни.
- Не выдумывай интересы и не додумывай личное по косвенным намёкам.
- Мало данных → честно укажи это, а не правдоподобную догадку.
- Это личная сфера: фиксируй нейтрально, без оценок и чувствительных выводов о
  человеке; только то, чем он сам поделился в рабочем контексте.
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
                logger.error(f"Error analyzing interests for {name}: {e}")

        return profiles

    async def _analyze_team_interests(
        self,
        profiles: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Анализирует интересы команды в целом"""
        if not profiles:
            return {}

        # Собираем все интересы
        all_professional = []
        all_expertise = []
        all_values = []
        all_enthusiasm = []

        for p in profiles:
            if isinstance(p.get("professional_interests"), list):
                for interest in p["professional_interests"]:
                    if isinstance(interest, dict):
                        all_professional.append(interest.get("topic", ""))
                    else:
                        all_professional.append(str(interest))

            all_expertise.extend(p.get("expertise_areas", []))
            all_values.extend(p.get("core_values", []))

            if isinstance(p.get("enthusiasm_topics"), list):
                for topic in p["enthusiasm_topics"]:
                    if isinstance(topic, dict):
                        all_enthusiasm.append(topic.get("topic", ""))
                    else:
                        all_enthusiasm.append(str(topic))

        # Подсчитываем частоту
        from collections import Counter

        return {
            "top_professional_interests": dict(Counter(all_professional).most_common(10)),
            "top_expertise_areas": dict(Counter(all_expertise).most_common(10)),
            "shared_values": dict(Counter(all_values).most_common(10)),
            "enthusiasm_topics": dict(Counter(all_enthusiasm).most_common(10)),
            "team_size": len(profiles)
        }

    async def _find_common_ground(
        self,
        profiles: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Находит общие точки соприкосновения между участниками"""
        if len(profiles) < 2:
            return []

        common_ground = []

        # Сравниваем пары участников
        for i, p1 in enumerate(profiles):
            for p2 in profiles[i+1:]:
                name1 = p1.get("name", "")
                name2 = p2.get("name", "")

                # Ищем общие интересы
                interests1 = set()
                interests2 = set()

                for interest in p1.get("professional_interests", []):
                    if isinstance(interest, dict):
                        interests1.add(interest.get("topic", "").lower())
                    else:
                        interests1.add(str(interest).lower())

                for interest in p2.get("professional_interests", []):
                    if isinstance(interest, dict):
                        interests2.add(interest.get("topic", "").lower())
                    else:
                        interests2.add(str(interest).lower())

                common_interests = interests1 & interests2

                # Общие ценности
                values1 = set(v.lower() for v in p1.get("core_values", []))
                values2 = set(v.lower() for v in p2.get("core_values", []))
                common_values = values1 & values2

                if common_interests or common_values:
                    common_ground.append({
                        "participants": [name1, name2],
                        "common_interests": list(common_interests),
                        "common_values": list(common_values),
                        "collaboration_potential": "high" if len(common_interests) > 2 else "medium"
                    })

        return common_ground

    async def _analyze_engagement(
        self,
        transcript: str,
        participants: List[Dict[str, Any]],
        profiles: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Анализирует вовлечённость участников"""
        insights = []

        if not self.llm or not profiles:
            return insights

        prompt = f"""
Проанализируй ВОВЛЕЧЁННОСТЬ участников на основе их интересов.

ПРОФИЛИ ИНТЕРЕСОВ:
{json.dumps([{{"name": p.get("name"), "enthusiasm_topics": p.get("enthusiasm_topics"), "engagement_triggers": p.get("engagement_triggers")}} for p in profiles[:5]], ensure_ascii=False)}

ФРАГМЕНТ ТРАНСКРИПТА:
{transcript[:2000]}

Определи для каждого участника:

Формат (JSON массив):
[
    {{
        "participant": "имя",
        "engagement_level": "high/medium/low",
        "peak_engagement_moments": ["моменты максимальной вовлечённости"],
        "engagement_drivers": ["что повышает вовлечённость"],
        "disengagement_triggers": ["что снижает вовлечённость"],
        "untapped_interests": ["интересы, которые не были задействованы"],
        "engagement_tips": ["как повысить вовлечённость этого участника"]
    }}
]
"""

        try:
            response = await self.llm.generate(prompt)
            if response:
                result = self._parse_json_response(response)
                if isinstance(result, list):
                    return result
        except Exception as e:
            logger.error(f"Error analyzing engagement: {e}")

        return insights

    async def _generate_recommendations(
        self,
        profiles: List[Dict[str, Any]],
        common_ground: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Генерирует рекомендации по использованию интересов"""
        if not profiles:
            return []

        prompt = f"""
На основе анализа интересов, сгенерируй РЕКОМЕНДАЦИИ.

ПРОФИЛИ ИНТЕРЕСОВ:
{json.dumps([{{"name": p.get("name"), "professional_interests": p.get("professional_interests"), "motivation_insights": p.get("motivation_insights")}} for p in profiles[:5]], ensure_ascii=False)}

ОБЩИЕ ТОЧКИ СОПРИКОСНОВЕНИЯ:
{json.dumps(common_ground[:5], ensure_ascii=False)}

Создай рекомендации:

Формат (JSON массив):
[
    {{
        "category": "motivation/collaboration/development/engagement/team_building",
        "target": "имя или 'команда'",
        "recommendation": "конкретная рекомендация",
        "based_on": "на каких интересах основана",
        "expected_outcome": "ожидаемый результат",
        "implementation": "как реализовать",
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

    async def get_aggregated_interests(
        self,
        person_name: str,
        meeting_analyses: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """
        Агрегирует интересы человека из нескольких встреч.
        """
        if not meeting_analyses:
            return None

        all_interests = []
        all_values = []
        all_expertise = []

        for analysis in meeting_analyses:
            for profile in analysis.get("interest_profiles", []):
                if person_name.lower() in profile.get("name", "").lower():
                    # Собираем интересы
                    for interest in profile.get("professional_interests", []):
                        if isinstance(interest, dict):
                            all_interests.append(interest.get("topic", ""))
                        else:
                            all_interests.append(str(interest))

                    all_values.extend(profile.get("core_values", []))
                    all_expertise.extend(profile.get("expertise_areas", []))

        if not all_interests and not all_values:
            return None

        from collections import Counter

        return {
            "name": person_name,
            "top_interests": dict(Counter(all_interests).most_common(10)),
            "consistent_values": dict(Counter(all_values).most_common(5)),
            "expertise_areas": dict(Counter(all_expertise).most_common(5)),
            "meetings_analyzed": len(meeting_analyses)
        }


