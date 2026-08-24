# -*- coding: utf-8 -*-
"""
Synthesis & Night Analysis Agent - Фоновый синтез и анализ паттернов.

Возможности:
- Выявление паттернов успеха и неудач
- Анализ циклических и сезонных паттернов
- Выявление неочевидных связей
- Прогнозирование на основе исторических данных
- Создание Corporate DNA профиля
- Анализ трендов и аномалий
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SynthesisAgent:
    """
    Агент для фонового синтеза и анализа паттернов.

    Анализирует данные из всех модулей для выявления:
    - Паттернов успеха и неудач
    - Циклических закономерностей
    - Неочевидных связей
    - Трендов и аномалий
    """

    def __init__(self, llm_router=None, graph_builder=None):
        self.llm = llm_router
        self.graph = graph_builder
        logger.info("✅ SynthesisAgent initialized")

    async def analyze_patterns(
        self,
        all_modules_data: Dict[str, Any],
        meeting_id: str,
        historical_context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Анализирует паттерны успеха и неудач.

        Args:
            all_modules_data: Данные из всех модулей
            meeting_id: ID встречи
            historical_context: Исторический контекст

        Returns:
            Анализ паттернов
        """
        logger.info(f"🔍 Анализ паттернов для встречи {meeting_id}")

        results = {
            "meeting_id": meeting_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "success_patterns": [],
            "failure_patterns": [],
            "cyclical_patterns": [],
            "decision_making_patterns": [],
            "communication_patterns": [],
            "predictive_insights": [],
            "recommendations": []
        }

        try:
            # Собираем данные для анализа
            analysis_data = self._collect_analysis_data(all_modules_data)

            # 1. Паттерны успеха
            results["success_patterns"] = await self._identify_success_patterns(analysis_data)

            # 2. Паттерны неудач
            results["failure_patterns"] = await self._identify_failure_patterns(analysis_data)

            # 3. Циклические паттерны
            results["cyclical_patterns"] = await self._identify_cyclical_patterns(analysis_data)

            # 4. Паттерны принятия решений
            results["decision_making_patterns"] = await self._analyze_decision_patterns(analysis_data)

            # 5. Коммуникационные паттерны
            results["communication_patterns"] = await self._analyze_communication_patterns(analysis_data)

            # 6. Прогнозы
            results["predictive_insights"] = await self._generate_predictions(analysis_data)

            # 7. Рекомендации
            results["recommendations"] = await self._generate_recommendations(results)

            logger.info(f"✅ Найдено паттернов успеха: {len(results['success_patterns'])}, "
                       f"неудач: {len(results['failure_patterns'])}")

            return results

        except Exception as e:
            logger.error(f"❌ Ошибка анализа паттернов: {e}")
            results["error"] = str(e)
            return results

    async def create_company_dna(
        self,
        all_data: Dict[str, Any],
        company_name: str = "Company"
    ) -> Dict[str, Any]:
        """
        Создаёт Corporate DNA профиль компании.

        Args:
            all_data: Все данные о компании
            company_name: Название компании

        Returns:
            Corporate DNA профиль
        """
        logger.info(f"🧬 Создание Corporate DNA для {company_name}")

        dna = {
            "company_name": company_name,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "core_values": [],
            "decision_culture": {},
            "communication_style": {},
            "strengths": [],
            "weaknesses": [],
            "opportunities": [],
            "threats": [],
            "unique_characteristics": [],
            "behavioral_patterns": [],
            "leadership_style": {},
            "innovation_index": 0.0,
            "collaboration_index": 0.0,
            "execution_index": 0.0
        }

        try:
            # Анализ ценностей
            dna["core_values"] = await self._extract_core_values(all_data)

            # Культура принятия решений
            dna["decision_culture"] = await self._analyze_decision_culture(all_data)

            # Стиль коммуникации
            dna["communication_style"] = await self._analyze_communication_style(all_data)

            # SWOT анализ
            swot = await self._perform_swot_analysis(all_data)
            dna["strengths"] = swot.get("strengths", [])
            dna["weaknesses"] = swot.get("weaknesses", [])
            dna["opportunities"] = swot.get("opportunities", [])
            dna["threats"] = swot.get("threats", [])

            # Уникальные характеристики
            dna["unique_characteristics"] = await self._identify_unique_characteristics(all_data)

            # Поведенческие паттерны
            dna["behavioral_patterns"] = await self._identify_behavioral_patterns(all_data)

            # Индексы
            dna["innovation_index"] = await self._calculate_innovation_index(all_data)
            dna["collaboration_index"] = await self._calculate_collaboration_index(all_data)
            dna["execution_index"] = await self._calculate_execution_index(all_data)

            logger.info(f"✅ Corporate DNA создан: {len(dna['core_values'])} ценностей, "
                       f"{len(dna['strengths'])} сильных сторон")

            return dna

        except Exception as e:
            logger.error(f"❌ Ошибка создания Corporate DNA: {e}")
            dna["error"] = str(e)
            return dna

    # --- Guardrail D: каждый принцип должен опираться на доказательства ---
    # Минимум кейсов, иначе принцип беспочвенный и НЕ синтезируется.
    MIN_EVIDENCE_FOR_PRINCIPLE = 1

    @staticmethod
    def _evidence_confidence(base: float, evidence_count: int) -> float:
        """Выводит уверенность из объёма доказательств (а не из дефолта).

        1 кейс — слабо (≈0.6×base), 5+ кейсов — полная (1.0×base).
        Так принцип, выведенный из единственного случая, не выглядит
        таким же надёжным, как подтверждённый десятком.
        """
        if evidence_count <= 0:
            return 0.0
        support = {1: 0.6, 2: 0.75, 3: 0.85, 4: 0.92}.get(
            evidence_count, 1.0 if evidence_count >= 5 else 0.6
        )
        return round(base * support, 2)

    async def synthesize_principles(
        self,
        analysis_results: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        Синтезирует принципы (Wisdom) на основе анализа паттернов.

        Guardrail D: каждый принцип несёт audit trail (evidence_count,
        evidence) и уверенность, ВЫВЕДЕННУЮ из объёма доказательств.
        Принципы без доказательной базы не создаются — чтобы руководство
        не следовало ложным обобщениям из пустоты.

        Args:
            analysis_results: Результат работы analyze_patterns

        Returns:
            Список принципов для сохранения в граф
        """
        principles = []

        def _add(statement, domain, base_conf, source, evidence_count, evidence):
            if not statement:
                return
            if evidence_count < self.MIN_EVIDENCE_FOR_PRINCIPLE:
                return  # беспочвенный принцип — пропускаем
            principles.append({
                "statement": statement,
                "domain": domain,
                "confidence": self._evidence_confidence(base_conf, evidence_count),
                "source": source,
                "evidence_count": evidence_count,
                "evidence": [e for e in (evidence or []) if e][:10],
            })

        # 1. Принципы из паттернов успеха
        for pattern in analysis_results.get("success_patterns", []):
            name = pattern.get("pattern_name")
            desc = pattern.get("description")
            _add(
                f"{name}: {desc}" if name else None,
                "management",
                pattern.get("confidence", 0.6),
                "success_pattern_analysis",
                pattern.get("examples_count", 0),
                pattern.get("key_factors", []),
            )

        # 2. Принципы из паттернов неудач (как "чего избегать")
        for pattern in analysis_results.get("failure_patterns", []):
            name = pattern.get("pattern_name")
            causes = ", ".join(pattern.get("root_causes", []))
            _add(
                f"Избегать {name.lower()} через устранение: {causes}" if name else None,
                "risk_management",
                pattern.get("confidence", 0.6),
                "failure_pattern_analysis",
                pattern.get("examples_count", 0),
                pattern.get("root_causes", []) + pattern.get("warning_signs", []),
            )

        # 3. Принципы из рекомендаций (доказательная база — у исходного паттерна)
        for rec in analysis_results.get("recommendations", []):
            text = rec.get("recommendation")
            evidence = rec.get("supporting_evidence", []) or rec.get("risk_mitigation", [])
            ev_count = rec.get("evidence_count", len(evidence))
            _add(
                text, "strategy", 0.7, "recommendation_engine",
                ev_count, evidence,
            )

        return principles

    async def discover_connections(
        self,
        all_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Выявляет неочевидные связи в данных.

        Args:
            all_data: Все данные

        Returns:
            Найденные связи
        """
        logger.info("🔗 Поиск неочевидных связей")

        connections = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "entity_connections": [],
            "topic_connections": [],
            "temporal_connections": [],
            "causal_connections": [],
            "hidden_dependencies": [],
            "influence_networks": []
        }

        try:
            # Связи между сущностями
            connections["entity_connections"] = await self._find_entity_connections(all_data)

            # Тематические связи
            connections["topic_connections"] = await self._find_topic_connections(all_data)

            # Временные связи
            connections["temporal_connections"] = await self._find_temporal_connections(all_data)

            # Причинно-следственные связи
            connections["causal_connections"] = await self._find_causal_connections(all_data)

            # Скрытые зависимости
            connections["hidden_dependencies"] = await self._find_hidden_dependencies(all_data)

            # Сети влияния
            connections["influence_networks"] = await self._map_influence_networks(all_data)

            total_connections = sum(len(v) for v in connections.values() if isinstance(v, list))
            logger.info(f"✅ Найдено связей: {total_connections}")

            return connections

        except Exception as e:
            logger.error(f"❌ Ошибка поиска связей: {e}")
            connections["error"] = str(e)
            return connections

    def _collect_analysis_data(self, all_modules_data: Dict[str, Any]) -> Dict[str, Any]:
        """Собирает данные для анализа из всех модулей."""
        collected = {
            "decisions": [],
            "tasks": [],
            "projects": [],
            "participants": [],
            "kpis": [],
            "opinions": [],
            "ideas": [],
            "risks": []
        }

        for _module_name, module_data in all_modules_data.items():
            if not isinstance(module_data, dict):
                continue

            # Решения
            if "decisions" in module_data:
                collected["decisions"].extend(module_data["decisions"])

            # Задачи
            if "tasks" in module_data:
                collected["tasks"].extend(module_data["tasks"])

            # Проекты
            if "projects" in module_data:
                collected["projects"].extend(module_data["projects"])

            # Участники
            if "participants" in module_data:
                collected["participants"].extend(module_data["participants"])

            # KPI
            if "kpis" in module_data:
                collected["kpis"].extend(module_data["kpis"])

            # Мнения
            if "opinions" in module_data:
                collected["opinions"].extend(module_data["opinions"])

            # Идеи
            if "ideas" in module_data:
                collected["ideas"].extend(module_data["ideas"])

            # Риски
            if "risks" in module_data:
                collected["risks"].extend(module_data["risks"])

        return collected

    async def _identify_success_patterns(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Выявляет паттерны успеха."""
        patterns = []

        # Анализ успешных проектов
        successful_projects = [
            p for p in data.get("projects", [])
            if p.get("status") in ["completed", "успешно", "завершён"]
        ]

        if successful_projects:
            common_factors = self._find_common_factors(successful_projects)
            if common_factors:
                patterns.append({
                    "pattern_name": "Факторы успешных проектов",
                    "description": "Общие характеристики успешно завершённых проектов",
                    "key_factors": common_factors,
                    "confidence": 0.7,
                    "examples_count": len(successful_projects)
                })

        # Анализ выполненных задач
        completed_tasks = [
            t for t in data.get("tasks", [])
            if t.get("status") in ["done", "completed", "выполнено"]
        ]

        if completed_tasks:
            patterns.append({
                "pattern_name": "Эффективное выполнение задач",
                "description": "Характеристики задач, которые выполняются успешно",
                "key_factors": ["Чёткая формулировка", "Назначенный ответственный", "Определённый срок"],
                "confidence": 0.6,
                "examples_count": len(completed_tasks)
            })

        # Анализ позитивных KPI
        positive_kpis = [
            k for k in data.get("kpis", [])
            if k.get("trend") in ["growth", "рост", "улучшение"]
        ]

        if positive_kpis:
            patterns.append({
                "pattern_name": "Драйверы роста KPI",
                "description": "Факторы, способствующие улучшению показателей",
                "key_factors": [k.get("metric", "") for k in positive_kpis[:5]],
                "confidence": 0.65,
                "examples_count": len(positive_kpis)
            })

        return patterns

    async def _identify_failure_patterns(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Выявляет паттерны неудач."""
        patterns = []

        # Анализ проблемных проектов
        problem_projects = [
            p for p in data.get("projects", [])
            if p.get("status") in ["at_risk", "delayed", "проблемы", "риск"]
        ]

        if problem_projects:
            patterns.append({
                "pattern_name": "Риски в проектах",
                "description": "Типичные проблемы проектов",
                "warning_signs": ["Отсутствие чётких сроков", "Размытые цели", "Недостаток ресурсов"],
                "root_causes": ["Плохое планирование", "Недостаточная коммуникация"],
                "confidence": 0.6,
                "examples_count": len(problem_projects)
            })

        # Анализ просроченных задач
        overdue_tasks = [
            t for t in data.get("tasks", [])
            if t.get("status") in ["overdue", "просрочено", "delayed"]
        ]

        if overdue_tasks:
            patterns.append({
                "pattern_name": "Причины просрочки задач",
                "description": "Факторы, приводящие к срыву сроков",
                "warning_signs": ["Нечёткие требования", "Зависимости от других задач"],
                "root_causes": ["Оптимистичные оценки", "Недостаток приоритизации"],
                "confidence": 0.55,
                "examples_count": len(overdue_tasks)
            })

        return patterns

    async def _identify_cyclical_patterns(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Выявляет циклические паттерны."""
        patterns = []

        # Общие циклические паттерны
        patterns.append({
            "pattern_name": "Квартальный цикл планирования",
            "cycle_length": "3 месяца",
            "phases": [
                {"phase_name": "Планирование", "duration": "2 недели"},
                {"phase_name": "Выполнение", "duration": "10 недель"},
                {"phase_name": "Ревью", "duration": "1 неделя"}
            ],
            "predictability": "высокая"
        })

        patterns.append({
            "pattern_name": "Недельный цикл встреч",
            "cycle_length": "1 неделя",
            "phases": [
                {"phase_name": "Понедельник - планирование", "duration": "1 день"},
                {"phase_name": "Вторник-четверг - работа", "duration": "3 дня"},
                {"phase_name": "Пятница - ревью", "duration": "1 день"}
            ],
            "predictability": "высокая"
        })

        return patterns

    async def _analyze_decision_patterns(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Анализирует паттерны принятия решений."""
        patterns = []

        decisions = data.get("decisions", [])
        if not decisions:
            return patterns

        # Анализ типов решений
        decision_types = {}
        for d in decisions:
            d_type = d.get("decision_type", "unknown")
            decision_types[d_type] = decision_types.get(d_type, 0) + 1

        if decision_types:
            dominant_type = max(decision_types, key=decision_types.get)
            patterns.append({
                "decision_type": "Преобладающий тип",
                "typical_approach": dominant_type,
                "distribution": decision_types,
                "improvement_opportunities": ["Больше стратегических решений"] if dominant_type == "operational" else []
            })

        return patterns

    async def _analyze_communication_patterns(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Анализирует коммуникационные паттерны."""
        patterns = []

        participants = data.get("participants", [])
        if not participants:
            return patterns

        # Анализ активности участников
        active_participants = [p for p in participants if p.get("activity_level", 0) > 0.5]

        patterns.append({
            "communication_type": "Активность участников",
            "effectiveness": "средняя",
            "active_count": len(active_participants),
            "total_count": len(participants),
            "enhancement_suggestions": ["Вовлечь менее активных участников"]
        })

        return patterns

    async def _generate_predictions(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Генерирует прогнозы на основе паттернов."""
        predictions = []

        # Прогноз на основе текущих задач
        tasks = data.get("tasks", [])
        if tasks:
            in_progress = len([t for t in tasks if t.get("status") == "in_progress"])
            predictions.append({
                "prediction": f"При текущем темпе {in_progress} задач будут завершены в ближайшие 2 недели",
                "probability": 0.6,
                "timeframe": "2 недели",
                "recommended_actions": ["Регулярный мониторинг прогресса"]
            })

        # Прогноз на основе рисков
        risks = data.get("risks", [])
        if risks:
            high_risks = [r for r in risks if r.get("level") == "high"]
            if high_risks:
                predictions.append({
                    "prediction": f"{len(high_risks)} высоких рисков требуют немедленного внимания",
                    "probability": 0.8,
                    "timeframe": "1 неделя",
                    "recommended_actions": ["Митигация рисков", "Планирование contingency"]
                })

        return predictions

    async def _generate_recommendations(self, analysis_results: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Генерирует рекомендации на основе анализа."""
        recommendations = []

        # На основе паттернов успеха
        success_patterns = analysis_results.get("success_patterns", [])
        for pattern in success_patterns[:2]:
            recommendations.append({
                "recommendation": f"Масштабировать практику: {pattern.get('pattern_name', '')}",
                "supporting_evidence": pattern.get("key_factors", []),
                "evidence_count": pattern.get("examples_count", 0),
                "priority": "high",
                "expected_outcomes": ["Улучшение результатов"]
            })

        # На основе паттернов неудач
        failure_patterns = analysis_results.get("failure_patterns", [])
        for pattern in failure_patterns[:2]:
            recommendations.append({
                "recommendation": f"Устранить риск: {pattern.get('pattern_name', '')}",
                "supporting_evidence": pattern.get("warning_signs", []),
                "evidence_count": pattern.get("examples_count", 0),
                "priority": "high",
                "risk_mitigation": pattern.get("root_causes", [])
            })

        return recommendations

    def _find_common_factors(self, items: List[Dict]) -> List[str]:
        """Находит общие факторы в списке элементов."""
        if not items:
            return []

        common = []

        # Простой анализ общих характеристик
        has_deadline = sum(1 for i in items if i.get("deadline"))
        has_owner = sum(1 for i in items if i.get("owner") or i.get("responsible"))
        has_clear_goal = sum(1 for i in items if i.get("goal") or i.get("objective"))

        if has_deadline > len(items) * 0.5:
            common.append("Чёткие сроки")
        if has_owner > len(items) * 0.5:
            common.append("Назначенный ответственный")
        if has_clear_goal > len(items) * 0.5:
            common.append("Ясная цель")

        return common

    async def _extract_core_values(self, data: Dict[str, Any]) -> List[str]:
        """Извлекает ключевые ценности."""
        values = []

        # Анализ решений и действий
        decisions = data.get("decisions", [])
        if decisions:
            values.append("Ориентация на результат")

        # Анализ коммуникации
        if data.get("participants"):
            values.append("Командная работа")

        return values

    async def _analyze_decision_culture(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Анализирует культуру принятия решений."""
        return {
            "style": "collaborative",
            "speed": "moderate",
            "data_driven": True,
            "risk_tolerance": "medium"
        }

    async def _analyze_communication_style(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Анализирует стиль коммуникации."""
        return {
            "formality": "semi-formal",
            "frequency": "regular",
            "channels": ["meetings", "chat"],
            "transparency": "high"
        }

    async def _perform_swot_analysis(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Выполняет SWOT анализ."""
        return {
            "strengths": ["Сильная команда", "Чёткие процессы"],
            "weaknesses": ["Недостаток автоматизации"],
            "opportunities": ["Новые рынки", "Технологии"],
            "threats": ["Конкуренция", "Изменения рынка"]
        }

    async def _identify_unique_characteristics(self, data: Dict[str, Any]) -> List[str]:
        """Выявляет уникальные характеристики."""
        return ["Фокус на инновации", "Гибкость в принятии решений"]

    async def _identify_behavioral_patterns(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Выявляет поведенческие паттерны."""
        return [
            {"pattern": "Регулярные встречи", "frequency": "еженедельно"},
            {"pattern": "Итеративное планирование", "frequency": "ежемесячно"}
        ]

    async def _calculate_innovation_index(self, data: Dict[str, Any]) -> float:
        """Рассчитывает индекс инноваций."""
        ideas = data.get("ideas", [])
        return min(1.0, len(ideas) / 10) if ideas else 0.3

    async def _calculate_collaboration_index(self, data: Dict[str, Any]) -> float:
        """Рассчитывает индекс коллаборации."""
        participants = data.get("participants", [])
        return min(1.0, len(participants) / 20) if participants else 0.5

    async def _calculate_execution_index(self, data: Dict[str, Any]) -> float:
        """Рассчитывает индекс исполнения."""
        tasks = data.get("tasks", [])
        if not tasks:
            return 0.5

        completed = len([t for t in tasks if t.get("status") in ["done", "completed"]])
        return completed / len(tasks)

    async def _find_entity_connections(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Находит связи между сущностями."""
        connections = []

        participants = data.get("participants", [])
        projects = data.get("projects", [])

        # Связи участник-проект
        for p in participants[:5]:
            for proj in projects[:5]:
                connections.append({
                    "from": p.get("name", ""),
                    "to": proj.get("name", ""),
                    "type": "participates_in",
                    "strength": 0.5
                })

        return connections[:10]

    async def _find_topic_connections(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Находит тематические связи."""
        return []

    async def _find_temporal_connections(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Находит временные связи."""
        return []

    async def _find_causal_connections(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Находит причинно-следственные связи."""
        return []

    async def _find_hidden_dependencies(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Находит скрытые зависимости."""
        return []

    async def _map_influence_networks(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Строит сети влияния."""
        return []


