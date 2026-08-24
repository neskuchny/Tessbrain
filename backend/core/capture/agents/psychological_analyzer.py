# -*- coding: utf-8 -*-
"""
Psychological Analyzer Agent - Психологический анализ участников

Анализирует:
1. Скрытые мотивы и истинные намерения
2. Психологические профили личности
3. Групповая динамика и конфликты
4. Прогнозирование поведения
5. Рекомендации по управлению

Адаптировано из tess_old/module_08_psychological_analysis/
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class PersonalityProfile:
    """Психологический профиль личности"""
    name: str
    personality_type: str = ""  # DISC, MBTI-like
    dominant_traits: List[str] = field(default_factory=list)
    communication_style: str = ""
    motivation_drivers: List[str] = field(default_factory=list)
    fears_concerns: List[str] = field(default_factory=list)
    decision_style: str = ""
    stress_response: str = ""
    conflict_approach: str = ""
    leadership_style: str = ""
    team_role: str = ""
    strengths: List[str] = field(default_factory=list)
    development_areas: List[str] = field(default_factory=list)


@dataclass
class GroupDynamics:
    """Групповая динамика команды"""
    trust_level: str = ""  # low/medium/high
    psychological_safety: str = ""
    hidden_conflicts: List[str] = field(default_factory=list)
    alliances: List[Dict[str, Any]] = field(default_factory=list)
    power_dynamics: List[Dict[str, Any]] = field(default_factory=list)
    collective_fears: List[str] = field(default_factory=list)
    team_culture: str = ""


class PsychologicalAnalyzer:
    """
    Агент психологического анализа участников встреч.

    Возможности:
    - Создание психологических профилей
    - Анализ скрытых мотивов
    - Прогнозирование поведения
    - Выявление конфликтов
    - Рекомендации по управлению
    """

    def __init__(self, llm_router=None, graph_builder=None):
        self.name = "psychological_analyzer"
        self.version = "1.0.0"
        self.llm = llm_router
        self.graph = graph_builder

        logger.info(f"🧠 {self.name} v{self.version} initialized")

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
        Полный психологический анализ встречи.

        Args:
            transcript: Транскрипт встречи
            participants: Список участников
            meeting_context: Контекст встречи

        Returns:
            Комплексный психологический анализ
        """
        start_time = datetime.now(timezone.utc)
        logger.info(f"🧠 Starting psychological analysis for {len(participants)} participants")

        result = {
            "personality_profiles": [],
            "hidden_motives": [],
            "group_dynamics": {},
            "behavioral_predictions": [],
            "conflict_risks": [],
            "management_recommendations": [],
            "stats": {}
        }

        try:
            # 1. Создаём психологические профили
            result["personality_profiles"] = await self._create_personality_profiles(
                transcript, participants, meeting_context
            )

            # 2. Анализируем скрытые мотивы
            result["hidden_motives"] = await self._analyze_hidden_motives(
                transcript, participants, meeting_context
            )

            # 3. Анализируем групповую динамику
            result["group_dynamics"] = await self._analyze_group_dynamics(
                transcript, participants, meeting_context
            )

            # 4. Прогнозируем поведение
            result["behavioral_predictions"] = await self._predict_behavior(
                result["personality_profiles"],
                result["hidden_motives"]
            )

            # 5. Выявляем риски конфликтов
            result["conflict_risks"] = await self._identify_conflict_risks(
                result["personality_profiles"],
                result["group_dynamics"]
            )

            # 6. Генерируем рекомендации
            result["management_recommendations"] = await self._generate_recommendations(
                result["personality_profiles"],
                result["hidden_motives"],
                result["conflict_risks"]
            )

            # Статистика
            duration = (datetime.now(timezone.utc) - start_time).total_seconds()
            result["stats"] = {
                "duration_seconds": round(duration, 2),
                "profiles_created": len(result["personality_profiles"]),
                "motives_identified": len(result["hidden_motives"]),
                "conflicts_detected": len(result["conflict_risks"]),
                "recommendations_generated": len(result["management_recommendations"])
            }

            logger.info(f"✅ Psychological analysis complete in {duration:.2f}s")

        except Exception as e:
            logger.error(f"❌ Psychological analysis error: {e}", exc_info=True)
            result["error"] = str(e)

        return result

    async def _create_personality_profiles(
        self,
        transcript: str,
        participants: List[Dict[str, Any]],
        meeting_context: Optional[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Создаёт психологические профили участников"""
        profiles = []

        if not self.llm:
            logger.warning("LLM not available for personality profiling")
            return profiles

        # Ограничиваем количество участников для анализа
        participants_to_analyze = participants[:10]

        # Опц. БАТЧ-режим (флаг PSYCH_BATCH=on, по умолчанию ВЫКЛ): один LLM-вызов
        # на всех участников вместо N. Экономит вызовы; включать ПОСЛЕ A/B на
        # качество (батч может «усреднять» профили). При любом сбое — откат на
        # проверенный per-участника цикл ниже, поэтому включение безопасно.
        import os as _os
        if (_os.getenv("PSYCH_BATCH", "").strip().lower() in ("1", "on", "true", "yes")
                and len(participants_to_analyze) > 1):
            batched = await self._create_profiles_batched(
                transcript, participants_to_analyze, meeting_context)
            if batched:
                logger.info("🧠 PSYCH_BATCH: %d профилей одним вызовом", len(batched))
                return batched
            logger.warning("PSYCH_BATCH: батч не удался — откат на per-участника")

        for participant in participants_to_analyze:
            name = participant.get("name") or participant.get("participant_name") or "Unknown"
            role = participant.get("role") or participant.get("functional_role") or ""

            prompt = f"""
Создай ПСИХОЛОГИЧЕСКИЙ ПРОФИЛЬ участника встречи на основе его поведения и высказываний.

УЧАСТНИК: {name}
РОЛЬ: {role}

КОНТЕКСТ ВСТРЕЧИ:
{json.dumps(meeting_context or {}, ensure_ascii=False)[:500]}

ФРАГМЕНТЫ ТРАНСКРИПТА (где упоминается участник):
{self._extract_participant_mentions(transcript, name)[:2000]}

Создай профиль в формате JSON:
{{
    "name": "{name}",
    "personality_type": "D/I/S/C или комбинация (DISC модель)",
    "dominant_traits": ["3-5 доминирующих черт характера"],
    "communication_style": "краткое описание стиля общения",
    "motivation_drivers": ["что мотивирует этого человека"],
    "fears_concerns": ["чего опасается или избегает"],
    "decision_style": "как принимает решения (аналитик/интуит/консенсус/быстро)",
    "stress_response": "как реагирует на стресс",
    "conflict_approach": "подход к конфликтам (избегание/конфронтация/компромисс)",
    "leadership_style": "стиль лидерства если есть",
    "team_role": "роль в команде (лидер/генератор идей/исполнитель/аналитик)",
    "strengths": ["3-5 сильных сторон"],
    "development_areas": ["2-3 зоны для развития"],
    "management_tips": ["2-3 рекомендации как эффективно работать с этим человеком"]
}}

ВАЖНО — ПРОФИЛЬ ПО НАБЛЮДАЕМОМУ ПОВЕДЕНИЮ, НЕ ДИАГНОЗ:
- Основывайся ТОЛЬКО на том, что человек реально говорил и делал в этих
  фрагментах. Каждое утверждение должно опираться на поведение из текста.
- Если реплик мало или их нет — честно верни короткий профиль с пометкой
  «недостаточно данных» в нужных полях. Не додумывай черты, страхи, мотивацию.
- Это рабочее наблюдение о стиле в контакте, а НЕ клиническая оценка. Не ставь
  психиатрических/медицинских ярлыков, не оценивай личность как «хорошую/плохую».
- Формулируй нейтрально и уважительно (профиль читают коллеги и сам человек).
- DISC/стиль — гипотеза по одной встрече, а не приговор; помечай неуверенность.
"""

            try:
                response = await self.llm.generate(prompt)
                if response:
                    # Пытаемся распарсить JSON
                    profile = self._parse_json_response(response)
                    if profile:
                        profile["name"] = name
                        profile["role"] = role
                        profiles.append(profile)
            except Exception as e:
                logger.error(f"Error creating profile for {name}: {e}")

        return profiles

    async def _create_profiles_batched(
        self,
        transcript: str,
        participants: List[Dict[str, Any]],
        meeting_context: Optional[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Один LLM-вызов на ВСЕХ участников → массив профилей. [] при сбое.

        Порядок объектов в ответе = порядку участников; имя/роль подстраховываем
        из исходного списка. Требуем покрытие ≥ половины участников, иначе — []
        (тогда вызывающий откатывается на per-участника)."""
        names = [(p.get("name") or p.get("participant_name") or "Unknown") for p in participants]
        roles = [(p.get("role") or p.get("functional_role") or "") for p in participants]
        blocks = []
        for i, p in enumerate(participants):
            blocks.append(
                f"УЧАСТНИК: {names[i]}\nРОЛЬ: {roles[i]}\nФРАГМЕНТЫ ТРАНСКРИПТА:\n"
                f"{self._extract_participant_mentions(transcript, names[i])[:1200]}")

        fields = ('{"name","personality_type","dominant_traits","communication_style",'
                  '"motivation_drivers","fears_concerns","decision_style","stress_response",'
                  '"conflict_approach","leadership_style","team_role","strengths",'
                  '"development_areas","management_tips"}')
        prompt = f"""Создай ПСИХОЛОГИЧЕСКИЙ ПРОФИЛЬ для КАЖДОГО участника ниже.

КОНТЕКСТ ВСТРЕЧИ:
{json.dumps(meeting_context or {}, ensure_ascii=False)[:500]}

УЧАСТНИКИ (по одному блоку на человека):
{chr(10).join(blocks)}

Верни СТРОГО JSON-МАССИВ: по одному объекту на участника, в ТОМ ЖЕ ПОРЯДКЕ.
Поля объекта (как в одиночном профиле): {fields}
ВАЖНО:
- Для КАЖДОГО — отдельный профиль по ЕГО фрагментам; НЕ усредняй между людьми.
- Только наблюдаемое в тексте поведение; черт/страхов/мотивов, которых нет в
  репликах, не выдумывай. Мало данных о человеке → короткий профиль с пометкой
  «недостаточно данных», а не догадки.
- Это рабочее наблюдение о стиле, НЕ клинический диагноз; формулируй нейтрально и
  уважительно, без медицинских ярлыков и оценок «хороший/плохой»."""

        try:
            response = await self.llm.generate(prompt)
            arr = self._parse_json_response(response)
            if not isinstance(arr, list) or not arr:
                return []
            profiles: List[Dict[str, Any]] = []
            for i, obj in enumerate(arr):
                if not isinstance(obj, dict):
                    continue
                obj["name"] = obj.get("name") or (names[i] if i < len(names) else "Unknown")
                obj["role"] = obj.get("role") or (roles[i] if i < len(roles) else "")
                profiles.append(obj)
            if len(profiles) < max(1, len(participants) // 2):
                return []  # слишком мало — считаем неудачей, вызывающий откатится
            return profiles
        except Exception as e:
            logger.error(f"batched psych profiles failed: {e}")
            return []

    async def _analyze_hidden_motives(
        self,
        transcript: str,
        participants: List[Dict[str, Any]],
        meeting_context: Optional[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Анализирует скрытые мотивы участников"""
        if not self.llm:
            return []

        participants_names = [p.get("name") or p.get("participant_name") for p in participants[:10]]
        participants_str = ", ".join([n for n in participants_names if n])

        prompt = f"""
Проанализируй СКРЫТЫЕ МОТИВЫ участников встречи.

УЧАСТНИКИ: {participants_str}

ТРАНСКРИПТ (фрагмент):
{transcript[:3000]}

Для каждого участника определи:

1. ИСТИННЫЕ НАМЕРЕНИЯ - что человек реально хочет (не то, что говорит)
2. ЛИЧНАЯ ПОВЕСТКА - какие личные цели преследует
3. СТРАХИ И ОПАСЕНИЯ - чего боится или избегает
4. СТРЕМЛЕНИЕ К ВЛАСТИ - хочет ли влиять/контролировать
5. ЗАЩИТНЫЕ МЕХАНИЗМЫ - как защищает свои интересы

Формат ответа (JSON массив):
[
    {{
        "participant": "имя",
        "stated_position": "что говорит открыто",
        "true_intentions": ["реальные намерения"],
        "personal_agenda": ["личные цели"],
        "fears": ["страхи и опасения"],
        "power_seeking": "описание стремления к власти/влиянию",
        "defense_mechanisms": ["защитные механизмы"],
        "manipulation_signs": ["признаки манипуляции если есть"],
        "authenticity_score": 0.0-1.0,
        "risk_level": "low/medium/high"
    }}
]

ВАЖНО: Будь объективным, ищи РЕАЛЬНЫЕ паттерны, не выдумывай.
"""

        try:
            response = await self.llm.generate(prompt)
            if response:
                motives = self._parse_json_response(response)
                if isinstance(motives, list):
                    return motives
        except Exception as e:
            logger.error(f"Error analyzing hidden motives: {e}")

        return []

    async def _analyze_group_dynamics(
        self,
        transcript: str,
        participants: List[Dict[str, Any]],
        meeting_context: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Анализирует групповую динамику"""
        if not self.llm:
            return {}

        prompt = f"""
Проанализируй ГРУППОВУЮ ДИНАМИКУ на встрече.

КОЛИЧЕСТВО УЧАСТНИКОВ: {len(participants)}

ТРАНСКРИПТ (фрагмент):
{transcript[:3000]}

Определи:

1. УРОВЕНЬ ДОВЕРИЯ в группе (low/medium/high)
2. ПСИХОЛОГИЧЕСКАЯ БЕЗОПАСНОСТЬ - могут ли люди свободно высказываться
3. СКРЫТЫЕ КОНФЛИКТЫ - напряжения между участниками
4. АЛЬЯНСЫ - кто с кем объединяется
5. ДИНАМИКА ВЛАСТИ - кто реально влияет на решения
6. КОЛЛЕКТИВНЫЕ СТРАХИ - общие опасения группы
7. КУЛЬТУРА КОМАНДЫ - как принято взаимодействовать

Формат ответа (JSON):
{{
    "trust_level": "low/medium/high",
    "trust_indicators": ["признаки уровня доверия"],
    "psychological_safety": "low/medium/high",
    "safety_indicators": ["признаки безопасности"],
    "hidden_conflicts": [
        {{
            "parties": ["участники конфликта"],
            "nature": "суть конфликта",
            "intensity": "low/medium/high",
            "trigger_topics": ["темы-триггеры"]
        }}
    ],
    "alliances": [
        {{
            "members": ["участники альянса"],
            "basis": "на чём основан альянс",
            "strength": "low/medium/high"
        }}
    ],
    "power_dynamics": [
        {{
            "influencer": "кто влияет",
            "influenced": ["на кого влияет"],
            "influence_type": "тип влияния",
            "legitimacy": "легитимность влияния"
        }}
    ],
    "collective_fears": ["общие страхи группы"],
    "team_culture": {{
        "decision_making": "как принимаются решения",
        "communication_norms": "нормы общения",
        "conflict_resolution": "как решаются конфликты",
        "innovation_attitude": "отношение к новому"
    }},
    "group_maturity": "forming/storming/norming/performing",
    "improvement_areas": ["области для улучшения групповой динамики"]
}}
"""

        try:
            response = await self.llm.generate(prompt)
            if response:
                dynamics = self._parse_json_response(response)
                if isinstance(dynamics, dict):
                    return dynamics
        except Exception as e:
            logger.error(f"Error analyzing group dynamics: {e}")

        return {}

    async def _predict_behavior(
        self,
        profiles: List[Dict[str, Any]],
        hidden_motives: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Прогнозирует поведение участников"""
        predictions = []

        if not self.llm or not profiles:
            return predictions

        prompt = f"""
На основе психологических профилей и скрытых мотивов, спрогнозируй ПОВЕДЕНИЕ участников.

ПРОФИЛИ:
{json.dumps(profiles[:5], ensure_ascii=False)[:2000]}

СКРЫТЫЕ МОТИВЫ:
{json.dumps(hidden_motives[:5], ensure_ascii=False)[:1500]}

Для каждого участника дай прогноз:

Формат (JSON массив):
[
    {{
        "participant": "имя",
        "stress_reaction": "как отреагирует на стресс/давление",
        "conflict_behavior": "как поведёт себя в конфликте",
        "change_adaptation": "как адаптируется к изменениям",
        "deadline_pressure": "как справится с дедлайнами",
        "criticism_response": "как отреагирует на критику",
        "leadership_emergence": "проявит ли лидерство и когда",
        "collaboration_style": "как будет сотрудничать",
        "likely_triggers": ["что может вызвать негативную реакцию"],
        "positive_triggers": ["что мотивирует на позитив"],
        "prediction_confidence": 0.0-1.0
    }}
]
"""

        try:
            response = await self.llm.generate(prompt)
            if response:
                preds = self._parse_json_response(response)
                if isinstance(preds, list):
                    return preds
        except Exception as e:
            logger.error(f"Error predicting behavior: {e}")

        return predictions

    async def _identify_conflict_risks(
        self,
        profiles: List[Dict[str, Any]],
        group_dynamics: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Выявляет риски конфликтов"""
        risks = []

        if not self.llm:
            return risks

        prompt = f"""
На основе профилей и групповой динамики, выяви РИСКИ КОНФЛИКТОВ.

ПРОФИЛИ:
{json.dumps(profiles[:5], ensure_ascii=False)[:1500]}

ГРУППОВАЯ ДИНАМИКА:
{json.dumps(group_dynamics, ensure_ascii=False)[:1500]}

Определи потенциальные конфликты:

Формат (JSON массив):
[
    {{
        "conflict_type": "тип конфликта (личностный/ролевой/ценностный/ресурсный)",
        "parties": ["участники конфликта"],
        "root_cause": "корневая причина",
        "triggers": ["что может спровоцировать"],
        "probability": 0.0-1.0,
        "impact": "low/medium/high/critical",
        "early_warning_signs": ["ранние признаки"],
        "prevention_strategies": ["как предотвратить"],
        "resolution_approach": "как разрешить если возникнет",
        "timeline": "когда может проявиться"
    }}
]
"""

        try:
            response = await self.llm.generate(prompt)
            if response:
                conflict_risks = self._parse_json_response(response)
                if isinstance(conflict_risks, list):
                    return conflict_risks
        except Exception as e:
            logger.error(f"Error identifying conflict risks: {e}")

        return risks

    async def _generate_recommendations(
        self,
        profiles: List[Dict[str, Any]],
        hidden_motives: List[Dict[str, Any]],
        conflict_risks: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Генерирует рекомендации по управлению"""
        recommendations = []

        if not self.llm:
            return recommendations

        prompt = f"""
На основе психологического анализа, сгенерируй РЕКОМЕНДАЦИИ ПО УПРАВЛЕНИЮ командой.

ПРОФИЛИ (краткое):
{json.dumps([{"name": p.get("name"), "type": p.get("personality_type"), "strengths": p.get("strengths")} for p in profiles[:5]], ensure_ascii=False)}

РИСКИ КОНФЛИКТОВ:
{json.dumps(conflict_risks[:3], ensure_ascii=False)[:1000]}

Создай практические рекомендации:

Формат (JSON массив):
[
    {{
        "category": "категория (коммуникация/мотивация/конфликты/развитие/лидерство)",
        "recommendation": "конкретная рекомендация",
        "target": "для кого (конкретный человек или вся команда)",
        "rationale": "психологическое обоснование",
        "implementation": "как реализовать",
        "expected_outcome": "ожидаемый результат",
        "priority": "high/medium/low",
        "timing": "когда применять"
    }}
]

Дай 5-10 практических рекомендаций.
"""

        try:
            response = await self.llm.generate(prompt)
            if response:
                recs = self._parse_json_response(response)
                if isinstance(recs, list):
                    return recs
        except Exception as e:
            logger.error(f"Error generating recommendations: {e}")

        return recommendations

    def _extract_participant_mentions(self, transcript: str, name: str) -> str:
        """Извлекает фрагменты транскрипта с упоминанием участника"""
        if not transcript or not name:
            return ""

        mentions = []
        lines = transcript.split('\n')

        name_lower = name.lower()
        first_name = name.split()[0].lower() if name else ""

        for i, line in enumerate(lines):
            line_lower = line.lower()
            if name_lower in line_lower or (first_name and first_name in line_lower):
                # Берём контекст: строку до, текущую и строку после
                start = max(0, i - 1)
                end = min(len(lines), i + 2)
                context = '\n'.join(lines[start:end])
                mentions.append(context)

        return '\n---\n'.join(mentions[:10])  # Максимум 10 упоминаний

    def _parse_json_response(self, response: str) -> Any:
        """Парсит JSON из ответа LLM"""
        if not response:
            return None

        # Убираем markdown
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
            # Пытаемся найти JSON в тексте
            import re
            json_match = re.search(r'[\[\{].*[\]\}]', response, re.DOTALL)
            if json_match:
                try:
                    return json.loads(json_match.group())
                except Exception:
                    logger.debug("suppressed exception", exc_info=True)

        return None

    async def get_participant_profile(self, participant_name: str) -> Optional[Dict[str, Any]]:
        """Получает сохранённый профиль участника из графа"""
        if not self.graph:
            return None

        try:
            nodes = await self.graph.get_all_nodes_async()
            for node in nodes:
                if node.get("_label") == "PsychologicalProfile":
                    if participant_name.lower() in (node.get("name") or "").lower():
                        return node
        except Exception as e:
            logger.error(f"Error getting profile for {participant_name}: {e}")

        return None

    async def save_profiles_to_graph(
        self,
        profiles: List[Dict[str, Any]],
        meeting_id: str
    ):
        """Сохраняет психологические профили в граф"""
        if not self.graph:
            return

        try:
            for profile in profiles:
                name = profile.get("name", "Unknown")
                profile_id = f"psych_profile:{name}:{meeting_id}"

                await self.graph.create_node(
                    node_id=profile_id,
                    label="PsychologicalProfile",
                    properties={
                        "name": name,
                        "meeting_id": meeting_id,
                        "personality_type": profile.get("personality_type", ""),
                        "dominant_traits": profile.get("dominant_traits", []),
                        "communication_style": profile.get("communication_style", ""),
                        "motivation_drivers": profile.get("motivation_drivers", []),
                        "team_role": profile.get("team_role", ""),
                        "strengths": profile.get("strengths", []),
                        "created_at": datetime.now(timezone.utc).isoformat()
                    }
                )

                # Связываем с Person если есть
                all_nodes = await self.graph.get_all_nodes_async()
                person_nodes = [n for n in all_nodes
                               if n.get("_label") == "Person" and
                               name.lower() in (n.get("name") or "").lower()]

                for person in person_nodes:
                    await self.graph._create_relationship(
                        from_id=person.get("id"),
                        to_id=profile_id,
                        rel_type="HAS_PSYCHOLOGICAL_PROFILE",
                        properties={"meeting_id": meeting_id}
                    )

            logger.info(f"💾 Saved {len(profiles)} psychological profiles to graph")

        except Exception as e:
            logger.error(f"Error saving profiles to graph: {e}")


