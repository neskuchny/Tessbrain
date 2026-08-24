# -*- coding: utf-8 -*-
"""
Decision Learning Agent - Анализ решений и создание обучающих данных.

Возможности:
- Автоматическое обнаружение решений в транскриптах
- Классификация решений по типам и важности
- Отслеживание эволюции решений
- Анализ результатов решений
- Создание обучающих данных для улучшения ИИ
"""

import logging
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class DecisionParticipant:
    """Участник принятия решения."""
    name: str
    role: str  # decision_maker, advisor, stakeholder, observer
    influence_level: float  # 0.0-1.0
    opinion: Optional[str] = None
    confidence: Optional[float] = None


@dataclass
class DecisionAlternative:
    """Альтернативный вариант решения."""
    description: str
    pros: List[str] = field(default_factory=list)
    cons: List[str] = field(default_factory=list)
    feasibility: Optional[float] = None
    cost: Optional[str] = None
    timeline: Optional[str] = None


@dataclass
class DecisionRecord:
    """Запись о решении."""
    decision_id: str
    timestamp: str
    meeting_id: Optional[str]
    decision_type: str  # strategic, tactical, operational
    urgency_level: str  # high, medium, low
    complexity_level: int  # 1-10
    description: str
    chosen_option: str
    alternatives_considered: List[DecisionAlternative]
    reasoning: str
    expected_outcomes: List[str]
    participants: List[DecisionParticipant]
    implementation_plan: Optional[Dict[str, Any]] = None
    success_metrics: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Преобразует в словарь."""
        result = asdict(self)
        result["alternatives_considered"] = [asdict(a) for a in self.alternatives_considered]
        result["participants"] = [asdict(p) for p in self.participants]
        return result


class DecisionLearningAgent:
    """
    Агент для анализа решений и создания обучающих данных.

    Возможности:
    - Обнаружение явных и неявных решений
    - Классификация по типам и важности
    - Анализ контекста и последствий
    - Создание training data для улучшения ИИ
    """

    def __init__(self, llm_router=None, graph_builder=None):
        self.llm = llm_router
        self.graph = graph_builder

        # Паттерны для выявления решений
        self.decision_patterns = {
            "explicit": [
                r"мы решили",
                r"принято решение",
                r"решаем делать",
                r"постановили",
                r"договорились",
                r"утверждаем",
                r"принимаем решение"
            ],
            "implicit": [
                r"давайте сделаем",
                r"нужно сделать",
                r"будем делать",
                r"так и поступим",
                r"идем по этому пути",
                r"выбираем этот вариант"
            ],
            "conditional": [
                r"если.*то будем",
                r"в случае.*сделаем",
                r"при условии.*решим"
            ],
            "deferred": [
                r"отложим решение",
                r"пока не решаем",
                r"вернемся к этому вопросу",
                r"нужно подумать"
            ]
        }

        # Индикаторы типов решений
        self.decision_type_indicators = {
            "strategic": [
                "стратегия", "направление", "видение", "долгосрочный",
                "развитие", "позиционирование", "конкуренция", "миссия"
            ],
            "tactical": [
                "план", "подход", "метод", "способ", "тактика",
                "реализация", "выполнение", "квартал"
            ],
            "operational": [
                "процесс", "процедура", "задача", "действие",
                "операция", "ежедневный", "текущий", "срочно"
            ]
        }

        logger.info("✅ DecisionLearningAgent initialized")

    async def analyze_decisions(
        self,
        transcript: str,
        meeting_id: Optional[str] = None,
        participants: Optional[List[str]] = None,
        existing_decisions: Optional[List[Dict]] = None
    ) -> Dict[str, Any]:
        """
        Полный анализ решений из транскрипта.

        Args:
            transcript: Транскрипт встречи
            meeting_id: ID встречи
            participants: Список участников
            existing_decisions: Уже извлечённые решения

        Returns:
            Результаты анализа с решениями и обучающими данными
        """
        logger.info(f"🔍 Анализ решений для встречи {meeting_id}")

        results = {
            "meeting_id": meeting_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "decisions": [],
            "decision_patterns": {},
            "learning_data": [],
            "statistics": {}
        }

        try:
            # 1. Обнаружение решений
            if existing_decisions:
                decisions = existing_decisions
            else:
                decisions = await self._detect_decisions(transcript, meeting_id, participants)

            results["decisions"] = [d.to_dict() if isinstance(d, DecisionRecord) else d for d in decisions]

            # 2. Классификация и приоритизация
            classified_decisions = await self._classify_decisions(decisions)
            results["decisions"] = classified_decisions

            # 3. Анализ паттернов принятия решений
            patterns = await self._analyze_decision_patterns(classified_decisions)
            results["decision_patterns"] = patterns

            # 4. Создание обучающих данных
            learning_data = await self._generate_learning_data(
                classified_decisions, transcript, meeting_id
            )
            results["learning_data"] = learning_data

            # 5. Статистика
            results["statistics"] = self._calculate_statistics(classified_decisions)

            logger.info(f"✅ Найдено решений: {len(classified_decisions)}, "
                       f"обучающих примеров: {len(learning_data)}")

            return results

        except Exception as e:
            logger.error(f"❌ Ошибка анализа решений: {e}")
            results["error"] = str(e)
            return results

    async def _detect_decisions(
        self,
        transcript: str,
        meeting_id: Optional[str],
        participants: Optional[List[str]]
    ) -> List[DecisionRecord]:
        """Обнаруживает решения в транскрипте."""
        decisions = []

        # Разбиваем на сегменты
        segments = self._segment_transcript(transcript)

        for segment in segments:
            text = segment.get("text", "")
            speaker = segment.get("speaker", "Unknown")

            # Проверяем паттерны
            for _pattern_type, patterns in self.decision_patterns.items():
                for pattern in patterns:
                    if re.search(pattern, text, re.IGNORECASE):
                        decision = DecisionRecord(
                            decision_id=f"decision_{uuid.uuid4().hex[:8]}",
                            timestamp=datetime.now(timezone.utc).isoformat(),
                            meeting_id=meeting_id,
                            decision_type=self._classify_decision_type(text),
                            urgency_level=self._assess_urgency(text),
                            complexity_level=self._assess_complexity(text),
                            description=text[:300] + "..." if len(text) > 300 else text,
                            chosen_option="",
                            alternatives_considered=[],
                            reasoning=self._extract_reasoning(text, transcript),
                            expected_outcomes=self._extract_outcomes(text, transcript),
                            participants=[DecisionParticipant(
                                name=speaker,
                                role="decision_maker",
                                influence_level=0.8
                            )]
                        )

                        # Валидация
                        if self._validate_decision(decision, transcript):
                            decisions.append(decision)
                        break

        return decisions

    async def _classify_decisions(
        self,
        decisions: List[DecisionRecord]
    ) -> List[Dict[str, Any]]:
        """Классифицирует и приоритизирует решения."""
        classified = []

        for decision in decisions:
            if isinstance(decision, DecisionRecord):
                decision_dict = decision.to_dict()
            else:
                decision_dict = decision

            # Добавляем классификацию важности
            decision_dict["importance_classification"] = {
                "business_impact": self._assess_business_impact(decision_dict),
                "urgency_score": self._calculate_urgency_score(decision_dict),
                "complexity_score": decision_dict.get("complexity_level", 5),
                "risk_level": self._assess_risk_level(decision_dict),
                "overall_priority": self._calculate_priority(decision_dict)
            }

            classified.append(decision_dict)

        # Сортируем по приоритету
        classified.sort(
            key=lambda x: {"high": 3, "medium": 2, "low": 1}.get(
                x.get("importance_classification", {}).get("overall_priority", "medium"), 2
            ),
            reverse=True
        )

        return classified

    async def _analyze_decision_patterns(
        self,
        decisions: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Анализирует паттерны принятия решений."""
        if not decisions:
            return {}

        patterns = {
            "decision_types_distribution": {},
            "urgency_distribution": {},
            "complexity_distribution": {},
            "common_patterns": [],
            "decision_makers": {},
            "success_factors": [],
            "risk_factors": []
        }

        # Распределение по типам
        for d in decisions:
            d_type = d.get("decision_type", "operational")
            patterns["decision_types_distribution"][d_type] = \
                patterns["decision_types_distribution"].get(d_type, 0) + 1

            urgency = d.get("urgency_level", "medium")
            patterns["urgency_distribution"][urgency] = \
                patterns["urgency_distribution"].get(urgency, 0) + 1

            complexity = d.get("complexity_level", 5)
            complexity_level = "high" if complexity >= 7 else "medium" if complexity >= 4 else "low"
            patterns["complexity_distribution"][complexity_level] = \
                patterns["complexity_distribution"].get(complexity_level, 0) + 1

            # Участники
            for p in d.get("participants", []):
                name = p.get("name", "Unknown")
                if name not in patterns["decision_makers"]:
                    patterns["decision_makers"][name] = {"count": 0, "roles": []}
                patterns["decision_makers"][name]["count"] += 1
                patterns["decision_makers"][name]["roles"].append(p.get("role", "unknown"))

        # Выявляем паттерны
        if patterns["decision_types_distribution"]:
            most_common_type = max(
                patterns["decision_types_distribution"],
                key=patterns["decision_types_distribution"].get
            )
            patterns["common_patterns"].append(
                f"Преобладают {most_common_type} решения"
            )

        return patterns

    async def _generate_learning_data(
        self,
        decisions: List[Dict[str, Any]],
        transcript: str,
        meeting_id: Optional[str]
    ) -> List[Dict[str, Any]]:
        """Создаёт обучающие данные для улучшения ИИ."""
        learning_data = []

        for decision in decisions:
            # Формат для обучения
            training_example = {
                "id": f"train_{decision.get('decision_id', uuid.uuid4().hex[:8])}",
                "meeting_id": meeting_id,
                "input": {
                    "context": decision.get("description", "")[:500],
                    "decision_type": decision.get("decision_type", ""),
                    "participants": [p.get("name") for p in decision.get("participants", [])],
                },
                "output": {
                    "classification": decision.get("importance_classification", {}),
                    "reasoning": decision.get("reasoning", ""),
                    "expected_outcomes": decision.get("expected_outcomes", []),
                    "risks": decision.get("risks", [])
                },
                "metadata": {
                    "source": "meeting_transcript",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "quality_score": self._assess_training_quality(decision)
                }
            }

            learning_data.append(training_example)

        return learning_data

    def _segment_transcript(self, transcript: str) -> List[Dict[str, Any]]:
        """Разбивает транскрипт на сегменты."""
        lines = transcript.split('\n')
        segments = []
        current_segment = []
        current_speaker = None

        for line in lines:
            line = line.strip()
            if not line:
                continue

            if ':' in line and len(line.split(':')[0]) < 30:
                if current_segment:
                    segments.append({
                        "speaker": current_speaker,
                        "text": '\n'.join(current_segment)
                    })

                parts = line.split(':', 1)
                current_speaker = parts[0].strip()
                current_segment = [parts[1].strip()] if len(parts) > 1 else []
            else:
                current_segment.append(line)

        if current_segment:
            segments.append({
                "speaker": current_speaker,
                "text": '\n'.join(current_segment)
            })

        return segments

    def _classify_decision_type(self, text: str) -> str:
        """Классифицирует тип решения."""
        text_lower = text.lower()

        type_scores = {}
        for d_type, indicators in self.decision_type_indicators.items():
            score = sum(1 for ind in indicators if ind in text_lower)
            type_scores[d_type] = score

        if type_scores:
            return max(type_scores, key=type_scores.get)
        return "operational"

    def _assess_urgency(self, text: str) -> str:
        """Оценивает срочность решения."""
        text_lower = text.lower()

        high_indicators = ["срочно", "немедленно", "критично", "блокирует", "горит", "asap"]
        low_indicators = ["когда-нибудь", "в будущем", "не спешим", "можно отложить"]

        if any(ind in text_lower for ind in high_indicators):
            return "high"
        if any(ind in text_lower for ind in low_indicators):
            return "low"
        return "medium"

    def _assess_complexity(self, text: str) -> int:
        """Оценивает сложность решения (1-10)."""
        text_lower = text.lower()

        high_indicators = ["интеграция", "архитектура", "стратегия", "реорганизация", "трансформация"]
        medium_indicators = ["проект", "процесс", "система", "планирование", "разработка"]
        low_indicators = ["задача", "исправление", "обновление", "настройка", "документация"]

        if any(ind in text_lower for ind in high_indicators):
            return 8
        if any(ind in text_lower for ind in medium_indicators):
            return 5
        if any(ind in text_lower for ind in low_indicators):
            return 3
        return 5

    def _extract_reasoning(self, text: str, transcript: str) -> str:
        """Извлекает обоснование решения."""
        reasoning_patterns = [
            r"потому что[^.]*\.",
            r"так как[^.]*\.",
            r"из-за того что[^.]*\.",
            r"причина в том[^.]*\.",
        ]

        for pattern in reasoning_patterns:
            matches = re.findall(pattern, transcript, re.IGNORECASE)
            if matches:
                return matches[0][:200]

        return "Обоснование не указано явно"

    def _extract_outcomes(self, text: str, transcript: str) -> List[str]:
        """Извлекает ожидаемые результаты."""
        outcome_patterns = [
            r"ожидаем[^.]*\.",
            r"планируем получить[^.]*\.",
            r"результат будет[^.]*\.",
            r"это даст нам[^.]*\.",
        ]

        outcomes = []
        for pattern in outcome_patterns:
            matches = re.findall(pattern, transcript, re.IGNORECASE)
            outcomes.extend(matches[:2])

        return outcomes[:5]

    def _validate_decision(self, decision: DecisionRecord, transcript: str) -> bool:
        """Валидирует решение."""
        description = decision.description.lower()

        if description.strip().endswith('?'):
            return False

        exclude_words = ["может быть", "возможно", "наверное", "кажется"]
        if any(word in description for word in exclude_words):
            return False

        if len(description.strip()) < 10:
            return False

        return True

    def _assess_business_impact(self, decision: Dict) -> str:
        """Оценивает влияние на бизнес."""
        description = decision.get("description", "").lower()

        high_keywords = ["доход", "прибыль", "клиенты", "рынок", "стратегия"]
        medium_keywords = ["процесс", "эффективность", "качество", "команда"]

        if any(kw in description for kw in high_keywords):
            return "high"
        if any(kw in description for kw in medium_keywords):
            return "medium"
        return "low"

    def _calculate_urgency_score(self, decision: Dict) -> float:
        """Вычисляет числовую оценку срочности."""
        urgency_map = {"high": 0.9, "medium": 0.5, "low": 0.1}
        return urgency_map.get(decision.get("urgency_level", "medium"), 0.5)

    def _assess_risk_level(self, decision: Dict) -> str:
        """Оценивает уровень риска."""
        description = decision.get("description", "").lower()
        risk_keywords = ["риск", "опасность", "проблема", "сложность", "неопределенность"]

        risk_count = sum(1 for kw in risk_keywords if kw in description)

        if risk_count >= 2:
            return "high"
        elif risk_count == 1:
            return "medium"
        return "low"

    def _calculate_priority(self, decision: Dict) -> str:
        """Вычисляет общий приоритет."""
        classification = decision.get("importance_classification", {})

        score = 0
        impact_scores = {"high": 3, "medium": 2, "low": 1}
        score += impact_scores.get(classification.get("business_impact", "medium"), 2)
        score += classification.get("urgency_score", 0.5) * 2
        score += classification.get("complexity_score", 5) * 0.1
        score += impact_scores.get(classification.get("risk_level", "medium"), 2)

        if score >= 8:
            return "high"
        elif score >= 5:
            return "medium"
        return "low"

    def _assess_training_quality(self, decision: Dict) -> float:
        """Оценивает качество обучающего примера."""
        score = 0.5

        if decision.get("reasoning") and decision["reasoning"] != "Обоснование не указано явно":
            score += 0.2
        if decision.get("expected_outcomes"):
            score += 0.15
        if len(decision.get("participants", [])) > 1:
            score += 0.1
        if decision.get("risks"):
            score += 0.05

        return min(1.0, score)

    def _calculate_statistics(self, decisions: List[Dict]) -> Dict[str, Any]:
        """Вычисляет статистику по решениям."""
        if not decisions:
            return {"total": 0}

        return {
            "total": len(decisions),
            "by_type": {
                "strategic": sum(1 for d in decisions if d.get("decision_type") == "strategic"),
                "tactical": sum(1 for d in decisions if d.get("decision_type") == "tactical"),
                "operational": sum(1 for d in decisions if d.get("decision_type") == "operational")
            },
            "by_urgency": {
                "high": sum(1 for d in decisions if d.get("urgency_level") == "high"),
                "medium": sum(1 for d in decisions if d.get("urgency_level") == "medium"),
                "low": sum(1 for d in decisions if d.get("urgency_level") == "low")
            },
            "by_priority": {
                "high": sum(1 for d in decisions if d.get("importance_classification", {}).get("overall_priority") == "high"),
                "medium": sum(1 for d in decisions if d.get("importance_classification", {}).get("overall_priority") == "medium"),
                "low": sum(1 for d in decisions if d.get("importance_classification", {}).get("overall_priority") == "low")
            },
            "avg_complexity": sum(d.get("complexity_level", 5) for d in decisions) / len(decisions)
        }


