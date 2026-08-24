# -*- coding: utf-8 -*-
"""
Incident Monitoring Agent - Мониторинг инцидентов и проблем.

Выявляет:
1. Упоминания проблем, сбоев, инцидентов
2. Жалобы и негативные сигналы
3. Риски и потенциальные проблемы
4. Блокеры и препятствия
5. Эскалации и критические ситуации
6. Паттерны повторяющихся проблем

Адаптировано из tess_old/module_01_nlp_analysis/incident_monitoring_agent.py
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class IncidentSeverity(Enum):
    """Уровни критичности инцидентов"""
    CRITICAL = "critical"  # Требует немедленного внимания
    HIGH = "high"          # Серьёзная проблема
    MEDIUM = "medium"      # Умеренная проблема
    LOW = "low"            # Незначительная проблема
    INFO = "info"          # Информационное упоминание


class IncidentType(Enum):
    """Типы инцидентов"""
    TECHNICAL = "technical"        # Технические сбои
    PROCESS = "process"            # Проблемы процессов
    RESOURCE = "resource"          # Нехватка ресурсов
    COMMUNICATION = "communication" # Проблемы коммуникации
    DEADLINE = "deadline"          # Проблемы со сроками
    QUALITY = "quality"            # Проблемы качества
    CONFLICT = "conflict"          # Конфликты
    EXTERNAL = "external"          # Внешние факторы
    BLOCKER = "blocker"            # Блокеры
    ESCALATION = "escalation"      # Эскалации


@dataclass
class Incident:
    """Структура инцидента"""
    incident_id: str
    title: str
    description: str
    incident_type: str
    severity: str
    reported_by: str = ""
    affected_parties: List[str] = field(default_factory=list)
    affected_projects: List[str] = field(default_factory=list)
    root_cause: str = ""
    current_status: str = ""  # new/investigating/resolved/escalated
    resolution: str = ""
    action_items: List[str] = field(default_factory=list)
    timeline: str = ""
    risk_level: str = ""
    meeting_id: str = ""
    detected_at: str = ""


class IncidentMonitoringAgent:
    """
    Агент мониторинга инцидентов.

    Автоматически выявляет проблемы, риски и инциденты
    из транскриптов встреч для проактивного управления.
    """

    def __init__(self, llm_router=None, graph_builder=None):
        self.name = "incident_monitoring_agent"
        self.version = "1.0.0"
        self.llm = llm_router
        self.graph = graph_builder

        # Ключевые слова для быстрого детектирования
        self.incident_keywords = {
            "critical": [
                "критично", "срочно", "авария", "упал", "не работает",
                "полностью", "катастрофа", "emergency", "critical", "down"
            ],
            "high": [
                "проблема", "сбой", "ошибка", "баг", "не могу", "блокер",
                "застряли", "срыв", "риск", "опасность", "угроза"
            ],
            "medium": [
                "сложность", "затруднение", "задержка", "отставание",
                "недоработка", "неудобно", "жалоба", "issue"
            ],
            "low": [
                "мелочь", "небольшая проблема", "minor", "небольшой баг",
                "косметический", "пожелание"
            ]
        }

        logger.info(f"🚨 {self.name} v{self.version} initialized")

    def set_llm(self, llm_router):
        """Устанавливает LLM роутер"""
        self.llm = llm_router

    async def analyze(
        self,
        transcript: str,
        meeting_context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Анализирует транскрипт на наличие инцидентов.

        Args:
            transcript: Транскрипт встречи
            meeting_context: Контекст встречи

        Returns:
            Анализ инцидентов
        """
        start_time = datetime.now(timezone.utc)
        meeting_id = meeting_context.get("meeting_id", "") if meeting_context else ""

        logger.info(f"🚨 Scanning for incidents in meeting {meeting_id}")

        result = {
            "incidents": [],
            "risks": [],
            "blockers": [],
            "escalations": [],
            "patterns": [],
            "summary": {},
            "recommendations": [],
            "stats": {}
        }

        try:
            # 1. Быстрое сканирование по ключевым словам
            keyword_hits = self._quick_scan(transcript)

            # 2. Детальный LLM-анализ если есть потенциальные инциденты
            if keyword_hits["total_hits"] > 0 or len(transcript) > 500:
                # 2.1 Извлекаем инциденты
                result["incidents"] = await self._extract_incidents(
                    transcript, meeting_context
                )

                # 2.2 Выявляем риски
                result["risks"] = await self._identify_risks(
                    transcript, meeting_context
                )

                # 2.3 Находим блокеры
                result["blockers"] = await self._find_blockers(
                    transcript, meeting_context
                )

                # 2.4 Детектируем эскалации
                result["escalations"] = await self._detect_escalations(
                    transcript, meeting_context
                )

            # 3. Анализируем паттерны (если есть история)
            if self.graph:
                result["patterns"] = await self._analyze_patterns(meeting_id)

            # 4. Генерируем рекомендации
            result["recommendations"] = await self._generate_recommendations(
                result["incidents"],
                result["risks"],
                result["blockers"]
            )

            # 5. Формируем summary
            result["summary"] = self._create_summary(result)

            # Статистика
            duration = (datetime.now(timezone.utc) - start_time).total_seconds()
            result["stats"] = {
                "duration_seconds": round(duration, 2),
                "keyword_hits": keyword_hits,
                "incidents_found": len(result["incidents"]),
                "risks_identified": len(result["risks"]),
                "blockers_found": len(result["blockers"]),
                "escalations_detected": len(result["escalations"])
            }

            logger.info(
                f"✅ Incident analysis complete: "
                f"{len(result['incidents'])} incidents, "
                f"{len(result['risks'])} risks, "
                f"{len(result['blockers'])} blockers"
            )

        except Exception as e:
            logger.error(f"❌ Incident analysis error: {e}", exc_info=True)
            result["error"] = str(e)

        return result

    def _quick_scan(self, transcript: str) -> Dict[str, Any]:
        """Быстрое сканирование по ключевым словам"""
        transcript_lower = transcript.lower()

        hits = {
            "critical": 0,
            "high": 0,
            "medium": 0,
            "low": 0,
            "total_hits": 0,
            "matched_keywords": []
        }

        for severity, keywords in self.incident_keywords.items():
            for keyword in keywords:
                count = transcript_lower.count(keyword.lower())
                if count > 0:
                    hits[severity] += count
                    hits["total_hits"] += count
                    hits["matched_keywords"].append({
                        "keyword": keyword,
                        "severity": severity,
                        "count": count
                    })

        return hits

    async def _extract_incidents(
        self,
        transcript: str,
        meeting_context: Optional[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Извлекает инциденты из транскрипта"""
        if not self.llm:
            return []

        meeting_id = meeting_context.get("meeting_id", "") if meeting_context else ""

        prompt = f"""
Проанализируй транскрипт встречи и извлеки все ИНЦИДЕНТЫ, ПРОБЛЕМЫ и СБОИ.

ТРАНСКРИПТ:
{transcript[:4000]}

Найди и опиши каждый инцидент в формате JSON:
[
    {{
        "incident_id": "уникальный ID (inc_001, inc_002...)",
        "title": "краткое название инцидента",
        "description": "подробное описание",
        "incident_type": "technical/process/resource/communication/deadline/quality/conflict/external",
        "severity": "critical/high/medium/low",
        "reported_by": "кто сообщил о проблеме",
        "affected_parties": ["кто затронут"],
        "affected_projects": ["какие проекты затронуты"],
        "root_cause": "корневая причина если упоминается",
        "current_status": "new/investigating/resolved/escalated",
        "resolution": "решение если есть",
        "action_items": ["действия для решения"],
        "timeline": "когда возникло/когда нужно решить",
        "quote": "цитата из транскрипта"
    }}
]

ПРАВИЛА:
1. Включай ТОЛЬКО реальные проблемы из транскрипта; quote обязателен и дословен.
2. Не выдумывай инциденты и не завышай масштаб; severity — по сказанному.
3. Описывай проблему, а не «виноватого»: не приписывай вину человеку без прямых
   слов об этом в тексте.
4. Если инцидентов нет, верни пустой массив [].
"""

        try:
            response = await self.llm.generate(prompt)
            if response:
                incidents = self._parse_json_response(response)
                if isinstance(incidents, list):
                    # Добавляем метаданные
                    for inc in incidents:
                        inc["meeting_id"] = meeting_id
                        inc["detected_at"] = datetime.now(timezone.utc).isoformat()
                    return incidents
        except Exception as e:
            logger.error(f"Error extracting incidents: {e}")

        return []

    async def _identify_risks(
        self,
        transcript: str,
        meeting_context: Optional[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Выявляет риски из транскрипта"""
        if not self.llm:
            return []

        prompt = f"""
Проанализируй транскрипт и выяви РИСКИ и ПОТЕНЦИАЛЬНЫЕ ПРОБЛЕМЫ.

ТРАНСКРИПТ:
{transcript[:3000]}

Найди риски в формате JSON:
[
    {{
        "risk_id": "risk_001",
        "title": "название риска",
        "description": "описание риска",
        "category": "technical/business/resource/timeline/quality/external",
        "probability": "high/medium/low",
        "impact": "critical/high/medium/low",
        "risk_score": "probability x impact (critical/high/medium/low)",
        "indicators": ["признаки, указывающие на риск"],
        "affected_areas": ["что может быть затронуто"],
        "mitigation_suggestions": ["как снизить риск"],
        "owner": "кто должен управлять риском",
        "timeline": "когда может реализоваться"
    }}
]

ВАЖНО: Ищи ПОТЕНЦИАЛЬНЫЕ проблемы, не только текущие.
"""

        try:
            response = await self.llm.generate(prompt)
            if response:
                risks = self._parse_json_response(response)
                if isinstance(risks, list):
                    return risks
        except Exception as e:
            logger.error(f"Error identifying risks: {e}")

        return []

    async def _find_blockers(
        self,
        transcript: str,
        meeting_context: Optional[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Находит блокеры"""
        if not self.llm:
            return []

        prompt = f"""
Найди все БЛОКЕРЫ и ПРЕПЯТСТВИЯ в транскрипте.

ТРАНСКРИПТ:
{transcript[:3000]}

Блокер - это то, что ОСТАНАВЛИВАЕТ или СУЩЕСТВЕННО ЗАМЕДЛЯЕТ работу.

Формат JSON:
[
    {{
        "blocker_id": "blk_001",
        "title": "название блокера",
        "description": "подробное описание",
        "blocker_type": "technical/dependency/resource/decision/external/knowledge",
        "blocking": ["что заблокировано"],
        "blocked_by": "что/кто блокирует",
        "duration": "как долго существует блокер",
        "impact": "влияние на работу",
        "unblock_requirements": ["что нужно для разблокировки"],
        "owner": "кто отвечает за разблокировку",
        "urgency": "critical/high/medium/low"
    }}
]
"""

        try:
            response = await self.llm.generate(prompt)
            if response:
                blockers = self._parse_json_response(response)
                if isinstance(blockers, list):
                    return blockers
        except Exception as e:
            logger.error(f"Error finding blockers: {e}")

        return []

    async def _detect_escalations(
        self,
        transcript: str,
        meeting_context: Optional[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Детектирует эскалации"""
        if not self.llm:
            return []

        prompt = f"""
Найди признаки ЭСКАЛАЦИЙ в транскрипте.

Эскалация - это когда проблема передаётся на более высокий уровень,
или когда ситуация обостряется и требует внимания руководства.

ТРАНСКРИПТ:
{transcript[:3000]}

Формат JSON:
[
    {{
        "escalation_id": "esc_001",
        "issue": "суть проблемы",
        "escalated_from": "от кого/откуда",
        "escalated_to": "к кому/куда",
        "reason": "причина эскалации",
        "urgency": "critical/high/medium",
        "current_status": "описание текущего статуса",
        "required_action": "какое действие требуется",
        "deadline": "срок если упоминается"
    }}
]
"""

        try:
            response = await self.llm.generate(prompt)
            if response:
                escalations = self._parse_json_response(response)
                if isinstance(escalations, list):
                    return escalations
        except Exception as e:
            logger.error(f"Error detecting escalations: {e}")

        return []

    async def _analyze_patterns(self, meeting_id: str) -> List[Dict[str, Any]]:
        """Анализирует паттерны повторяющихся проблем"""
        patterns = []

        if not self.graph:
            return patterns

        try:
            # Ищем похожие инциденты в графе
            all_nodes = await self.graph.get_all_nodes_async()

            incident_nodes = [
                n for n in all_nodes
                if n.get("_label") == "Incident"
            ]

            if len(incident_nodes) < 2:
                return patterns

            # Группируем по типу
            from collections import Counter

            types = [n.get("incident_type", "unknown") for n in incident_nodes]
            type_counts = Counter(types)

            for inc_type, count in type_counts.most_common(5):
                if count >= 2:
                    patterns.append({
                        "pattern_type": "recurring_incident_type",
                        "incident_type": inc_type,
                        "occurrences": count,
                        "recommendation": f"Обратить внимание на повторяющиеся {inc_type} инциденты"
                    })

        except Exception as e:
            logger.error(f"Error analyzing patterns: {e}")

        return patterns

    async def _generate_recommendations(
        self,
        incidents: List[Dict[str, Any]],
        risks: List[Dict[str, Any]],
        blockers: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Генерирует рекомендации"""
        recommendations = []

        # Рекомендации по критичным инцидентам
        critical_incidents = [
            i for i in incidents
            if i.get("severity") in ["critical", "high"]
        ]

        if critical_incidents:
            recommendations.append({
                "priority": "critical",
                "category": "immediate_action",
                "recommendation": f"Немедленно адресовать {len(critical_incidents)} критичных инцидентов",
                "details": [i.get("title") for i in critical_incidents[:3]]
            })

        # Рекомендации по рискам
        high_risks = [
            r for r in risks
            if r.get("risk_score") in ["critical", "high"]
        ]

        if high_risks:
            recommendations.append({
                "priority": "high",
                "category": "risk_mitigation",
                "recommendation": f"Разработать план митигации для {len(high_risks)} высоких рисков",
                "details": [r.get("title") for r in high_risks[:3]]
            })

        # Рекомендации по блокерам
        if blockers:
            recommendations.append({
                "priority": "high",
                "category": "unblock",
                "recommendation": f"Разблокировать {len(blockers)} блокеров для продолжения работы",
                "details": [b.get("title") for b in blockers[:3]]
            })

        return recommendations

    def _create_summary(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Создаёт сводку по инцидентам"""
        incidents = result.get("incidents", [])
        risks = result.get("risks", [])
        blockers = result.get("blockers", [])
        escalations = result.get("escalations", [])

        # Считаем по severity
        severity_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        for inc in incidents:
            sev = inc.get("severity", "low")
            if sev in severity_counts:
                severity_counts[sev] += 1

        # Определяем общий статус
        if severity_counts["critical"] > 0:
            overall_status = "critical"
        elif severity_counts["high"] > 0 or len(blockers) > 0:
            overall_status = "attention_needed"
        elif severity_counts["medium"] > 0:
            overall_status = "monitoring"
        else:
            overall_status = "healthy"

        return {
            "overall_status": overall_status,
            "total_incidents": len(incidents),
            "severity_breakdown": severity_counts,
            "total_risks": len(risks),
            "total_blockers": len(blockers),
            "total_escalations": len(escalations),
            "requires_immediate_attention": severity_counts["critical"] > 0 or len(escalations) > 0,
            "summary_text": self._generate_summary_text(
                overall_status, incidents, risks, blockers, escalations
            )
        }

    def _generate_summary_text(
        self,
        status: str,
        incidents: List,
        risks: List,
        blockers: List,
        escalations: List
    ) -> str:
        """Генерирует текстовую сводку"""
        if status == "healthy" and not incidents and not risks:
            return "На встрече не выявлено значимых инцидентов или рисков."

        parts = []

        if incidents:
            critical = sum(1 for i in incidents if i.get("severity") == "critical")
            high = sum(1 for i in incidents if i.get("severity") == "high")

            if critical > 0:
                parts.append(f"🚨 {critical} критических инцидентов требуют немедленного внимания")
            if high > 0:
                parts.append(f"⚠️ {high} серьёзных проблем требуют решения")

        if blockers:
            parts.append(f"🛑 {len(blockers)} блокеров останавливают работу")

        if escalations:
            parts.append(f"📢 {len(escalations)} эскалаций требуют внимания руководства")

        if risks:
            high_risks = sum(1 for r in risks if r.get("risk_score") in ["critical", "high"])
            if high_risks > 0:
                parts.append(f"⚡ {high_risks} высоких рисков требуют митигации")

        return ". ".join(parts) + "." if parts else "Ситуация под контролем."

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

    async def save_incidents_to_graph(
        self,
        incidents: List[Dict[str, Any]],
        meeting_id: str
    ):
        """Сохраняет инциденты в граф"""
        if not self.graph or not incidents:
            return

        try:
            for incident in incidents:
                inc_id = incident.get("incident_id", f"inc_{meeting_id}_{len(incidents)}")

                await self.graph.create_node(
                    node_id=inc_id,
                    label="Incident",
                    properties={
                        "title": incident.get("title", ""),
                        "description": incident.get("description", ""),
                        "incident_type": incident.get("incident_type", ""),
                        "severity": incident.get("severity", "medium"),
                        "status": incident.get("current_status", "new"),
                        "meeting_id": meeting_id,
                        "detected_at": incident.get("detected_at", datetime.now(timezone.utc).isoformat()),
                        "reported_by": incident.get("reported_by", ""),
                        "affected_projects": incident.get("affected_projects", [])
                    }
                )

                # Связываем с встречей
                await self.graph.create_relationship(
                    from_id=f"meeting_{meeting_id}",
                    to_id=inc_id,
                    rel_type="HAS_INCIDENT"
                )

            logger.info(f"💾 Saved {len(incidents)} incidents to graph")

        except Exception as e:
            logger.error(f"Error saving incidents to graph: {e}")


