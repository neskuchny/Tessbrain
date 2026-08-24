# -*- coding: utf-8 -*-
"""
Communication Style Agent - Анализ стиля коммуникации участников.

Определяет:
1. Тип коммуникатора (аналитик/экспрессивный/драйвер/дружелюбный)
2. Предпочтительные каналы и форматы
3. Паттерны речи и лексика
4. Эмоциональный тон
5. Уровень детализации
6. Скорость принятия информации

Адаптировано из tess_old/module_01_nlp_analysis/communication_style_agent.py
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class CommunicationProfile:
    """Профиль коммуникации участника"""
    name: str
    # Основной тип коммуникатора (Social Styles Model)
    communicator_type: str = ""  # analytical/expressive/driver/amiable

    # Характеристики
    assertiveness: str = ""  # low/medium/high
    responsiveness: str = ""  # low/medium/high (эмоциональность)

    # Предпочтения
    preferred_channels: List[str] = field(default_factory=list)  # email/chat/call/meeting
    preferred_format: str = ""  # bullet_points/narrative/visual/data
    detail_level: str = ""  # high_level/detailed/exhaustive

    # Паттерны речи
    speech_pace: str = ""  # slow/moderate/fast
    vocabulary_complexity: str = ""  # simple/moderate/complex
    use_of_jargon: str = ""  # minimal/moderate/heavy
    question_style: str = ""  # direct/indirect/rhetorical

    # Эмоциональный профиль
    emotional_tone: str = ""  # neutral/positive/negative/variable
    expressiveness: str = ""  # reserved/moderate/expressive
    humor_usage: str = ""  # none/occasional/frequent

    # Взаимодействие
    listening_style: str = ""  # active/passive/selective
    feedback_style: str = ""  # direct/diplomatic/avoidant
    conflict_communication: str = ""  # confrontational/collaborative/avoidant

    # Рекомендации
    how_to_communicate: List[str] = field(default_factory=list)
    what_to_avoid: List[str] = field(default_factory=list)


class CommunicationStyleAgent:
    """
    Агент анализа стиля коммуникации.

    Анализирует как участники общаются и даёт рекомендации
    по эффективному взаимодействию с ними.
    """

    def __init__(self, llm_router=None, graph_builder=None):
        self.name = "communication_style_agent"
        self.version = "1.0.0"
        self.llm = llm_router
        self.graph = graph_builder

        logger.info(f"💬 {self.name} v{self.version} initialized")

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
        Анализирует стили коммуникации участников.

        Args:
            transcript: Транскрипт встречи
            participants: Список участников
            meeting_context: Контекст встречи

        Returns:
            Анализ стилей коммуникации
        """
        start_time = datetime.now(timezone.utc)
        logger.info(f"💬 Analyzing communication styles for {len(participants)} participants")

        result = {
            "communication_profiles": [],
            "team_communication_patterns": {},
            "interaction_matrix": [],
            "recommendations": [],
            "stats": {}
        }

        if not self.llm:
            logger.warning("LLM not available for communication analysis")
            return result

        try:
            # 1. Анализируем стиль каждого участника
            result["communication_profiles"] = await self._analyze_individual_styles(
                transcript, participants, meeting_context
            )

            # 2. Анализируем командные паттерны
            result["team_communication_patterns"] = await self._analyze_team_patterns(
                transcript, participants, result["communication_profiles"]
            )

            # 3. Строим матрицу взаимодействий
            result["interaction_matrix"] = await self._build_interaction_matrix(
                transcript, participants
            )

            # 4. Генерируем рекомендации
            result["recommendations"] = await self._generate_recommendations(
                result["communication_profiles"],
                result["team_communication_patterns"]
            )

            # Статистика
            duration = (datetime.now(timezone.utc) - start_time).total_seconds()
            result["stats"] = {
                "duration_seconds": round(duration, 2),
                "profiles_analyzed": len(result["communication_profiles"]),
                "patterns_identified": len(result["team_communication_patterns"]),
                "recommendations_count": len(result["recommendations"])
            }

            logger.info(f"✅ Communication analysis complete in {duration:.2f}s")

        except Exception as e:
            logger.error(f"❌ Communication analysis error: {e}", exc_info=True)
            result["error"] = str(e)

        return result

    async def _analyze_individual_styles(
        self,
        transcript: str,
        participants: List[Dict[str, Any]],
        meeting_context: Optional[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Анализирует индивидуальные стили коммуникации"""
        profiles = []

        # Ограничиваем количество участников
        participants_to_analyze = participants[:10]

        for participant in participants_to_analyze:
            name = participant.get("name") or participant.get("participant_name") or "Unknown"
            role = participant.get("role") or ""

            # Извлекаем реплики участника
            participant_speech = self._extract_participant_speech(transcript, name)

            if len(participant_speech) < 50:
                # Недостаточно данных для анализа
                continue

            prompt = f"""
Проанализируй СТИЛЬ КОММУНИКАЦИИ участника на основе его высказываний.

УЧАСТНИК: {name}
РОЛЬ: {role}

ВЫСКАЗЫВАНИЯ УЧАСТНИКА:
{participant_speech[:3000]}

Создай профиль коммуникации в формате JSON:
{{
    "name": "{name}",
    "communicator_type": "analytical/expressive/driver/amiable (Social Styles Model)",
    "communicator_type_confidence": 0.0-1.0,

    "assertiveness": "low/medium/high (насколько настойчив в высказываниях)",
    "responsiveness": "low/medium/high (эмоциональная отзывчивость)",

    "preferred_format": "bullet_points/narrative/visual/data (какой формат предпочитает)",
    "detail_level": "high_level/detailed/exhaustive",

    "speech_pace": "slow/moderate/fast",
    "vocabulary_complexity": "simple/moderate/complex",
    "use_of_jargon": "minimal/moderate/heavy",
    "question_style": "direct/indirect/rhetorical",

    "emotional_tone": "neutral/positive/negative/variable",
    "expressiveness": "reserved/moderate/expressive",
    "humor_usage": "none/occasional/frequent",

    "listening_indicators": ["признаки стиля слушания"],
    "feedback_style": "direct/diplomatic/avoidant",
    "conflict_communication": "confrontational/collaborative/avoidant",

    "speech_patterns": ["характерные речевые паттерны"],
    "favorite_phrases": ["любимые фразы/слова"],
    "communication_strengths": ["сильные стороны в коммуникации"],
    "communication_weaknesses": ["слабые стороны"],

    "how_to_communicate": ["3-5 рекомендаций как эффективно общаться с этим человеком"],
    "what_to_avoid": ["2-3 вещи, которых следует избегать в общении"]
}}

ВАЖНО — СТИЛЬ ПО РЕАЛЬНЫМ РЕПЛИКАМ:
- Основывайся ТОЛЬКО на том, КАК человек реально формулировал в этих фрагментах
  (лексика, длина фраз, метафоры, тон). Не додумывай стиль, которого нет в тексте.
- Мало реплик → низкая уверенность и пустые/осторожные поля, а не догадка.
- Наблюдение о манере общения, а не оценка личности; формулируй нейтрально.
- speech_patterns/примеры фраз — дословно из текста, не сочиняй за человека.
"""

            try:
                response = await self.llm.generate(prompt)
                if response:
                    profile = self._parse_json_response(response)
                    if profile:
                        profile["name"] = name
                        profile["role"] = role
                        profile["speech_sample_length"] = len(participant_speech)
                        profiles.append(profile)
            except Exception as e:
                logger.error(f"Error analyzing communication style for {name}: {e}")

        return profiles

    async def _analyze_team_patterns(
        self,
        transcript: str,
        participants: List[Dict[str, Any]],
        profiles: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Анализирует командные паттерны коммуникации"""
        if not profiles:
            return {}

        prompt = f"""
Проанализируй КОМАНДНЫЕ ПАТТЕРНЫ КОММУНИКАЦИИ на встрече.

КОЛИЧЕСТВО УЧАСТНИКОВ: {len(participants)}

ПРОФИЛИ КОММУНИКАЦИИ:
{json.dumps([{{"name": p.get("name"), "type": p.get("communicator_type"), "assertiveness": p.get("assertiveness")}} for p in profiles], ensure_ascii=False)}

ФРАГМЕНТ ТРАНСКРИПТА:
{transcript[:2500]}

Определи командные паттерны:

Формат ответа (JSON):
{{
    "dominant_communication_style": "какой стиль преобладает в команде",
    "communication_balance": "сбалансировано/доминирование одних/пассивность других",

    "turn_taking_pattern": "как распределяется время говорения",
    "interruption_frequency": "low/medium/high",
    "silence_comfort": "comfortable/uncomfortable (как команда относится к паузам)",

    "information_flow": "top_down/bottom_up/circular/chaotic",
    "decision_communication": "как обсуждаются и сообщаются решения",

    "conflict_patterns": ["как проявляются разногласия"],
    "consensus_building": "как достигается согласие",

    "formality_level": "formal/semi_formal/informal",
    "emotional_climate": "описание эмоционального климата",

    "communication_barriers": ["выявленные барьеры в коммуникации"],
    "communication_enablers": ["что способствует хорошей коммуникации"],

    "improvement_opportunities": ["возможности для улучшения командной коммуникации"]
}}
"""

        try:
            response = await self.llm.generate(prompt)
            if response:
                patterns = self._parse_json_response(response)
                if isinstance(patterns, dict):
                    return patterns
        except Exception as e:
            logger.error(f"Error analyzing team patterns: {e}")

        return {}

    async def _build_interaction_matrix(
        self,
        transcript: str,
        participants: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Строит матрицу взаимодействий между участниками"""
        interactions = []

        # Извлекаем имена
        names = [p.get("name") or p.get("participant_name") for p in participants[:8]]
        names = [n for n in names if n]

        if len(names) < 2:
            return interactions

        prompt = f"""
Проанализируй ВЗАИМОДЕЙСТВИЯ между участниками встречи.

УЧАСТНИКИ: {", ".join(names)}

ТРАНСКРИПТ:
{transcript[:3000]}

Для каждой пары участников, которые взаимодействовали, определи:

Формат ответа (JSON массив):
[
    {{
        "from": "имя участника 1",
        "to": "имя участника 2",
        "interaction_count": число взаимодействий,
        "interaction_type": "supportive/challenging/neutral/collaborative",
        "tone": "positive/neutral/negative/mixed",
        "topics": ["темы обсуждения между ними"],
        "dynamics": "описание динамики взаимодействия",
        "notable_exchanges": ["примечательные моменты"]
    }}
]

Включи только пары, которые реально взаимодействовали в транскрипте.
"""

        try:
            response = await self.llm.generate(prompt)
            if response:
                matrix = self._parse_json_response(response)
                if isinstance(matrix, list):
                    return matrix
        except Exception as e:
            logger.error(f"Error building interaction matrix: {e}")

        return interactions

    async def _generate_recommendations(
        self,
        profiles: List[Dict[str, Any]],
        team_patterns: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Генерирует рекомендации по улучшению коммуникации"""
        if not profiles:
            return []

        prompt = f"""
На основе анализа стилей коммуникации, сгенерируй РЕКОМЕНДАЦИИ.

ПРОФИЛИ:
{json.dumps([{{"name": p.get("name"), "type": p.get("communicator_type"), "strengths": p.get("communication_strengths"), "weaknesses": p.get("communication_weaknesses")}} for p in profiles[:5]], ensure_ascii=False)}

КОМАНДНЫЕ ПАТТЕРНЫ:
{json.dumps(team_patterns, ensure_ascii=False)[:1000]}

Создай практические рекомендации:

Формат (JSON массив):
[
    {{
        "category": "individual/team/meeting_format/tools",
        "target": "для кого (имя или 'команда')",
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
        """Извлекает высказывания конкретного участника"""
        if not transcript or not name:
            return ""

        speeches = []
        lines = transcript.split('\n')

        name.lower()
        first_name = name.split()[0].lower() if name else ""

        # Паттерны для определения говорящего
        speaker_patterns = [
            f"{name}:",
            f"{name.upper()}:",
            f"[{name}]",
            f"({name})",
        ]
        if first_name:
            speaker_patterns.extend([
                f"{first_name.capitalize()}:",
                f"[{first_name.capitalize()}]",
            ])

        current_speaker_match = False

        for line in lines:
            line_stripped = line.strip()

            # Проверяем, начинается ли строка с имени говорящего
            is_speaker_line = any(
                line_stripped.lower().startswith(p.lower())
                for p in speaker_patterns
            )

            if is_speaker_line:
                current_speaker_match = True
                # Извлекаем текст после имени
                for p in speaker_patterns:
                    if line_stripped.lower().startswith(p.lower()):
                        text = line_stripped[len(p):].strip()
                        if text:
                            speeches.append(text)
                        break
            elif current_speaker_match and line_stripped and not any(
                line_stripped.lower().startswith(f"{n.lower()}:")
                for n in [p.get("name", "") for p in []]  # Другие участники
            ):
                # Продолжение речи текущего говорящего
                if not line_stripped.endswith(':') and len(line_stripped) > 10:
                    speeches.append(line_stripped)
            elif ':' in line_stripped[:30]:
                # Новый говорящий
                current_speaker_match = False

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

    async def get_profile_for_person(
        self,
        person_name: str,
        transcripts: List[str]
    ) -> Optional[Dict[str, Any]]:
        """
        Получает агрегированный профиль коммуникации для человека
        на основе нескольких встреч.
        """
        if not transcripts or not self.llm:
            return None

        # Собираем все высказывания человека
        all_speech = []
        for transcript in transcripts:
            speech = self._extract_participant_speech(transcript, person_name)
            if speech:
                all_speech.append(speech)

        if not all_speech:
            return None

        combined_speech = '\n---\n'.join(all_speech)[:5000]

        prompt = f"""
Создай АГРЕГИРОВАННЫЙ ПРОФИЛЬ КОММУНИКАЦИИ на основе высказываний с нескольких встреч.

УЧАСТНИК: {person_name}
КОЛИЧЕСТВО ВСТРЕЧ: {len(transcripts)}

ВЫСКАЗЫВАНИЯ:
{combined_speech}

Формат ответа (JSON):
{{
    "name": "{person_name}",
    "communicator_type": "analytical/expressive/driver/amiable",
    "confidence": 0.0-1.0,

    "consistent_patterns": ["устойчивые паттерны коммуникации"],
    "situational_variations": ["как меняется стиль в разных ситуациях"],

    "strengths": ["сильные стороны"],
    "development_areas": ["области для развития"],

    "best_practices_for_interaction": ["лучшие практики взаимодействия"],
    "common_misunderstandings": ["типичные недопонимания"],

    "summary": "краткое резюме стиля коммуникации"
}}
"""

        try:
            response = await self.llm.generate(prompt)
            if response:
                return self._parse_json_response(response)
        except Exception as e:
            logger.error(f"Error getting aggregated profile for {person_name}: {e}")

        return None


