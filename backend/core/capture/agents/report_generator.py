# -*- coding: utf-8 -*-
"""
Report Generator Agent - Генерация автоматических отчётов

Создаёт:
1. Executive Summary - краткая сводка для руководства
2. Weekly Report - еженедельный отчёт
3. Project Status Report - статус проектов
4. Team Performance Report - эффективность команды
5. Risk Report - отчёт по рискам

Адаптировано из tess_old/module_04_reports_analytics/
"""
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ReportSection:
    """Секция отчёта"""
    title: str
    content: str
    priority: str  # high/medium/low
    data: Dict[str, Any] = None


class ReportGenerator:
    """
    Генератор автоматических отчётов.

    Возможности:
    - Executive Summary для руководства
    - Еженедельные/месячные отчёты
    - Отчёты по проектам
    - Отчёты по рискам
    - Отчёты по команде
    """

    def __init__(self, llm_router=None, graph_builder=None):
        self.name = "report_generator"
        self.version = "1.0.0"
        self.llm = llm_router
        self.graph = graph_builder

        logger.info(f"📊 {self.name} v{self.version} initialized")

    def set_llm(self, llm_router):
        """Устанавливает LLM роутер"""
        self.llm = llm_router

    async def generate_executive_summary(
        self,
        meetings_data: List[Dict[str, Any]],
        period: str = "week"
    ) -> Dict[str, Any]:
        """
        Генерирует Executive Summary для руководства.

        Args:
            meetings_data: Данные встреч за период
            period: Период (day/week/month)

        Returns:
            Executive Summary
        """
        start_time = datetime.now(timezone.utc)
        logger.info(f"📊 Generating Executive Summary for {period}")

        result = {
            "report_type": "executive_summary",
            "period": period,
            "generated_at": start_time.isoformat(),
            "sections": {},
            "key_metrics": {},
            "recommendations": [],
            "alerts": []
        }

        try:
            # Агрегируем данные
            aggregated = self._aggregate_meetings_data(meetings_data)

            # 1. Ключевые метрики
            result["key_metrics"] = await self._calculate_key_metrics(aggregated)

            # 2. Краткая сводка
            result["sections"]["overview"] = await self._generate_overview(
                aggregated, period
            )

            # 3. Ключевые решения
            result["sections"]["key_decisions"] = await self._summarize_decisions(
                aggregated.get("decisions", [])
            )

            # 4. Критические задачи
            result["sections"]["critical_tasks"] = await self._identify_critical_tasks(
                aggregated.get("tasks", [])
            )

            # 5. Статус проектов
            result["sections"]["project_status"] = await self._summarize_project_status(
                aggregated.get("projects", [])
            )

            # 6. Риски и проблемы
            result["sections"]["risks"] = await self._summarize_risks(
                aggregated.get("risks", [])
            )

            # 7. Рекомендации
            result["recommendations"] = await self._generate_recommendations(
                aggregated, result["key_metrics"]
            )

            # 8. Алерты
            result["alerts"] = self._generate_alerts(
                result["key_metrics"],
                aggregated.get("risks", [])
            )

            # Статистика
            duration = (datetime.now(timezone.utc) - start_time).total_seconds()
            result["stats"] = {
                "duration_seconds": round(duration, 2),
                "meetings_analyzed": len(meetings_data),
                "sections_generated": len(result["sections"]),
                "alerts_count": len(result["alerts"])
            }

            logger.info(f"✅ Executive Summary generated in {duration:.2f}s")

        except Exception as e:
            logger.error(f"❌ Executive Summary error: {e}", exc_info=True)
            result["error"] = str(e)

        return result

    async def generate_project_report(
        self,
        project_name: str,
        project_data: Dict[str, Any],
        meetings_data: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Генерирует отчёт по проекту.

        Args:
            project_name: Название проекта
            project_data: Данные проекта
            meetings_data: Данные встреч по проекту

        Returns:
            Отчёт по проекту
        """
        start_time = datetime.now(timezone.utc)
        logger.info(f"📊 Generating Project Report for {project_name}")

        result = {
            "report_type": "project_report",
            "project_name": project_name,
            "generated_at": start_time.isoformat(),
            "status": {},
            "progress": {},
            "team": {},
            "tasks": {},
            "risks": {},
            "timeline": {},
            "recommendations": []
        }

        try:
            # 1. Статус проекта
            result["status"] = await self._analyze_project_status(
                project_data, meetings_data
            )

            # 2. Прогресс
            result["progress"] = await self._calculate_project_progress(
                project_data, meetings_data
            )

            # 3. Команда
            result["team"] = await self._analyze_project_team(
                meetings_data
            )

            # 4. Задачи
            result["tasks"] = await self._summarize_project_tasks(
                meetings_data
            )

            # 5. Риски
            result["risks"] = await self._analyze_project_risks(
                meetings_data
            )

            # 6. Timeline
            result["timeline"] = await self._build_project_timeline(
                meetings_data
            )

            # 7. Рекомендации
            result["recommendations"] = await self._generate_project_recommendations(
                result
            )

            logger.info(f"✅ Project Report for {project_name} generated")

        except Exception as e:
            logger.error(f"❌ Project Report error: {e}", exc_info=True)
            result["error"] = str(e)

        return result

    async def generate_weekly_report(
        self,
        meetings_data: List[Dict[str, Any]],
        week_start: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        Генерирует еженедельный отчёт.

        Args:
            meetings_data: Данные встреч за неделю
            week_start: Начало недели

        Returns:
            Еженедельный отчёт
        """
        if not week_start:
            week_start = datetime.now(timezone.utc) - timedelta(days=7)

        week_end = week_start + timedelta(days=7)

        logger.info(f"📊 Generating Weekly Report ({week_start.date()} - {week_end.date()})")

        result = {
            "report_type": "weekly_report",
            "period": {
                "start": week_start.isoformat(),
                "end": week_end.isoformat()
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "highlights": [],
            "meetings_summary": {},
            "accomplishments": [],
            "challenges": [],
            "next_week_priorities": [],
            "metrics": {}
        }

        try:
            # Агрегируем данные
            aggregated = self._aggregate_meetings_data(meetings_data)

            # 1. Highlights
            result["highlights"] = await self._extract_highlights(aggregated)

            # 2. Meetings Summary
            result["meetings_summary"] = {
                "total_meetings": len(meetings_data),
                "participants": len(set(
                    p.get("name") for m in meetings_data
                    for p in m.get("participants", []) if isinstance(p, dict)
                )),
                "decisions_made": len(aggregated.get("decisions", [])),
                "tasks_created": len(aggregated.get("tasks", []))
            }

            # 3. Accomplishments
            result["accomplishments"] = await self._identify_accomplishments(
                aggregated
            )

            # 4. Challenges
            result["challenges"] = await self._identify_challenges(
                aggregated
            )

            # 5. Next Week Priorities
            result["next_week_priorities"] = await self._determine_priorities(
                aggregated
            )

            # 6. Metrics
            result["metrics"] = await self._calculate_weekly_metrics(
                aggregated
            )

            logger.info("✅ Weekly Report generated")

        except Exception as e:
            logger.error(f"❌ Weekly Report error: {e}", exc_info=True)
            result["error"] = str(e)

        return result

    async def generate_risk_report(
        self,
        meetings_data: List[Dict[str, Any]],
        risks_data: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """
        Генерирует отчёт по рискам.

        Args:
            meetings_data: Данные встреч
            risks_data: Данные о рисках

        Returns:
            Отчёт по рискам
        """
        logger.info("📊 Generating Risk Report")

        result = {
            "report_type": "risk_report",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "risk_summary": {},
            "critical_risks": [],
            "new_risks": [],
            "mitigated_risks": [],
            "risk_trends": {},
            "recommendations": []
        }

        try:
            # Собираем все риски
            all_risks = risks_data or []
            for meeting in meetings_data:
                meeting_risks = meeting.get("risks", [])
                if meeting_risks:
                    all_risks.extend(meeting_risks)

            # 1. Risk Summary
            result["risk_summary"] = self._calculate_risk_summary(all_risks)

            # 2. Critical Risks
            result["critical_risks"] = [
                r for r in all_risks
                if isinstance(r, dict) and r.get("impact") in ["critical", "high"]
            ][:10]

            # 3. New Risks (за последнюю неделю)
            week_ago = datetime.now(timezone.utc) - timedelta(days=7)
            result["new_risks"] = [
                r for r in all_risks
                if isinstance(r, dict) and
                self._parse_date(r.get("created_at")) and
                self._parse_date(r.get("created_at")) > week_ago
            ]

            # 4. Risk Trends
            result["risk_trends"] = await self._analyze_risk_trends(all_risks)

            # 5. Recommendations
            result["recommendations"] = await self._generate_risk_recommendations(
                result["critical_risks"]
            )

            logger.info(f"✅ Risk Report generated: {len(all_risks)} risks analyzed")

        except Exception as e:
            logger.error(f"❌ Risk Report error: {e}", exc_info=True)
            result["error"] = str(e)

        return result

    def _aggregate_meetings_data(
        self,
        meetings_data: List[Dict[str, Any]]
    ) -> Dict[str, List[Any]]:
        """Агрегирует данные из всех встреч"""
        aggregated = {
            "decisions": [],
            "tasks": [],
            "participants": [],
            "entities": [],
            "projects": [],
            "risks": [],
            "ideas": [],
            "kpis": []
        }

        for meeting in meetings_data:
            if not isinstance(meeting, dict):
                continue

            for key in aggregated.keys():
                items = meeting.get(key, [])
                if isinstance(items, list):
                    aggregated[key].extend(items)

        return aggregated

    async def _calculate_key_metrics(
        self,
        aggregated: Dict[str, List[Any]]
    ) -> Dict[str, Any]:
        """Рассчитывает ключевые метрики"""
        return {
            "total_decisions": len(aggregated.get("decisions", [])),
            "total_tasks": len(aggregated.get("tasks", [])),
            "unique_participants": len(set(
                p.get("name") for p in aggregated.get("participants", [])
                if isinstance(p, dict) and p.get("name")
            )),
            "active_projects": len(set(
                p.get("name") or p.get("project_name")
                for p in aggregated.get("projects", [])
                if isinstance(p, dict)
            )),
            "identified_risks": len(aggregated.get("risks", [])),
            "new_ideas": len(aggregated.get("ideas", []))
        }

    async def _generate_overview(
        self,
        aggregated: Dict[str, List[Any]],
        period: str
    ) -> Dict[str, Any]:
        """Генерирует обзор"""
        if not self.llm:
            return {
                "summary": f"Report for {period}",
                "key_points": []
            }

        prompt = f"""
Создай КРАТКИЙ ОБЗОР для руководства на основе данных.

ПЕРИОД: {period}

ДАННЫЕ:
- Решений: {len(aggregated.get('decisions', []))}
- Задач: {len(aggregated.get('tasks', []))}
- Участников: {len(aggregated.get('participants', []))}
- Проектов: {len(aggregated.get('projects', []))}
- Рисков: {len(aggregated.get('risks', []))}

РЕШЕНИЯ (выборка):
{json.dumps(aggregated.get('decisions', [])[:5], ensure_ascii=False)[:1000]}

ЗАДАЧИ (выборка):
{json.dumps(aggregated.get('tasks', [])[:5], ensure_ascii=False)[:1000]}

Формат ответа (JSON):
{{
    "summary": "краткое резюме в 2-3 предложениях",
    "key_points": ["3-5 ключевых пунктов"],
    "overall_status": "positive/neutral/concerning",
    "attention_areas": ["области требующие внимания"]
}}
"""

        try:
            response = await self.llm.generate(prompt)
            if response:
                return self._parse_json_response(response) or {"summary": "", "key_points": []}
        except Exception as e:
            logger.error(f"Error generating overview: {e}")

        return {"summary": "", "key_points": []}

    async def _summarize_decisions(
        self,
        decisions: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Суммирует решения"""
        if not decisions:
            return {"count": 0, "key_decisions": [], "summary": "Нет решений"}

        # Сортируем по важности
        key_decisions = decisions[:10]

        return {
            "count": len(decisions),
            "key_decisions": [
                {
                    "decision": d.get("decision", d.get("description", ""))[:200],
                    "maker": d.get("decision_maker", d.get("made_by", "")),
                    "impact": d.get("impact", "medium")
                }
                for d in key_decisions if isinstance(d, dict)
            ],
            "summary": f"Принято {len(decisions)} решений"
        }

    async def _identify_critical_tasks(
        self,
        tasks: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Выявляет критические задачи"""
        critical = []
        overdue = []

        now = datetime.now(timezone.utc)

        for task in tasks:
            if not isinstance(task, dict):
                continue

            priority = task.get("priority", "").lower()
            deadline = task.get("deadline")

            if priority in ["critical", "high", "urgent"]:
                critical.append(task)

            if deadline:
                deadline_dt = self._parse_date(deadline)
                if deadline_dt and deadline_dt < now:
                    overdue.append(task)

        return {
            "total_tasks": len(tasks),
            "critical_count": len(critical),
            "overdue_count": len(overdue),
            "critical_tasks": [
                {
                    "task": t.get("task", t.get("title", ""))[:100],
                    "assignee": t.get("assignee", ""),
                    "deadline": t.get("deadline", "")
                }
                for t in critical[:5]
            ],
            "overdue_tasks": [
                {
                    "task": t.get("task", t.get("title", ""))[:100],
                    "assignee": t.get("assignee", ""),
                    "deadline": t.get("deadline", "")
                }
                for t in overdue[:5]
            ]
        }

    async def _summarize_project_status(
        self,
        projects: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Суммирует статус проектов"""
        if not projects:
            return {"count": 0, "projects": [], "summary": "Нет данных о проектах"}

        # Группируем по статусу
        by_status = {}
        for project in projects:
            if isinstance(project, dict):
                status = project.get("status", "unknown")
                if status not in by_status:
                    by_status[status] = []
                by_status[status].append(project)

        return {
            "count": len(projects),
            "by_status": {k: len(v) for k, v in by_status.items()},
            "projects": [
                {
                    "name": p.get("name", p.get("project_name", "")),
                    "status": p.get("status", "unknown"),
                    "progress": p.get("progress", "N/A")
                }
                for p in projects[:10] if isinstance(p, dict)
            ]
        }

    async def _summarize_risks(
        self,
        risks: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Суммирует риски"""
        if not risks:
            return {"count": 0, "critical": [], "summary": "Рисков не выявлено"}

        critical = [r for r in risks if isinstance(r, dict) and r.get("impact") in ["critical", "high"]]

        return {
            "count": len(risks),
            "critical_count": len(critical),
            "critical": [
                {
                    "risk": r.get("risk_name", r.get("name", ""))[:100],
                    "impact": r.get("impact", "medium"),
                    "probability": r.get("probability", 0.5)
                }
                for r in critical[:5]
            ],
            "summary": f"Выявлено {len(risks)} рисков, из них {len(critical)} критических"
        }

    async def _generate_recommendations(
        self,
        aggregated: Dict[str, List[Any]],
        metrics: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Генерирует рекомендации"""
        recommendations = []

        # Базовые рекомендации на основе метрик
        if metrics.get("identified_risks", 0) > 5:
            recommendations.append({
                "priority": "high",
                "recommendation": "Провести ревью рисков и разработать планы митигации",
                "rationale": f"Выявлено {metrics.get('identified_risks')} рисков"
            })

        tasks = aggregated.get("tasks", [])
        overdue = sum(1 for t in tasks if isinstance(t, dict) and t.get("status") == "overdue")
        if overdue > 0:
            recommendations.append({
                "priority": "high",
                "recommendation": "Пересмотреть сроки и ресурсы для просроченных задач",
                "rationale": f"{overdue} задач просрочено"
            })

        return recommendations

    def _generate_alerts(
        self,
        metrics: Dict[str, Any],
        risks: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Генерирует алерты"""
        alerts = []

        # Алерт по рискам
        critical_risks = [r for r in risks if isinstance(r, dict) and r.get("impact") == "critical"]
        if critical_risks:
            alerts.append({
                "type": "risk",
                "severity": "critical",
                "message": f"⚠️ {len(critical_risks)} критических рисков требуют немедленного внимания",
                "items": [r.get("risk_name", r.get("name", "")) for r in critical_risks[:3]]
            })

        return alerts

    def _calculate_risk_summary(
        self,
        risks: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Рассчитывает сводку по рискам"""
        if not risks:
            return {"total": 0, "by_impact": {}, "by_category": {}}

        by_impact = {}
        by_category = {}

        for risk in risks:
            if not isinstance(risk, dict):
                continue

            impact = risk.get("impact", "medium")
            category = risk.get("category", "other")

            by_impact[impact] = by_impact.get(impact, 0) + 1
            by_category[category] = by_category.get(category, 0) + 1

        return {
            "total": len(risks),
            "by_impact": by_impact,
            "by_category": by_category
        }

    async def _analyze_risk_trends(
        self,
        risks: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Анализирует тренды рисков"""
        return {
            "trend": "stable",
            "new_risks_this_week": len([
                r for r in risks
                if isinstance(r, dict) and
                self._is_recent(r.get("created_at"), days=7)
            ]),
            "resolved_this_week": 0  # TODO: track resolved risks
        }

    async def _generate_risk_recommendations(
        self,
        critical_risks: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Генерирует рекомендации по рискам"""
        recommendations = []

        for risk in critical_risks[:5]:
            if isinstance(risk, dict):
                recommendations.append({
                    "risk": risk.get("risk_name", risk.get("name", "")),
                    "recommendation": "Разработать план митигации для риска",
                    "priority": "high"
                })

        return recommendations

    # Helper methods for project report
    async def _analyze_project_status(self, project_data, meetings_data):
        return {"status": project_data.get("status", "unknown"), "health": "good"}

    async def _calculate_project_progress(self, project_data, meetings_data):
        return {"progress_percent": 50, "on_track": True}

    async def _analyze_project_team(self, meetings_data):
        participants = set()
        for m in meetings_data:
            for p in m.get("participants", []):
                if isinstance(p, dict):
                    participants.add(p.get("name", ""))
        return {"members": list(participants), "count": len(participants)}

    async def _summarize_project_tasks(self, meetings_data):
        tasks = []
        for m in meetings_data:
            tasks.extend(m.get("tasks", []))
        return {"total": len(tasks), "completed": 0, "in_progress": len(tasks)}

    async def _analyze_project_risks(self, meetings_data):
        risks = []
        for m in meetings_data:
            risks.extend(m.get("risks", []))
        return {"total": len(risks), "critical": 0}

    async def _build_project_timeline(self, meetings_data):
        return {"milestones": [], "deadlines": []}

    async def _generate_project_recommendations(self, result):
        return []

    # Helper methods for weekly report
    async def _extract_highlights(self, aggregated):
        return []

    async def _identify_accomplishments(self, aggregated):
        return []

    async def _identify_challenges(self, aggregated):
        return []

    async def _determine_priorities(self, aggregated):
        return []

    async def _calculate_weekly_metrics(self, aggregated):
        return {}

    def _parse_date(self, date_str: Optional[str]) -> Optional[datetime]:
        """Парсит дату из строки"""
        if not date_str:
            return None
        try:
            return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except Exception:
            return None

    def _is_recent(self, date_str: Optional[str], days: int = 7) -> bool:
        """Проверяет, является ли дата недавней"""
        dt = self._parse_date(date_str)
        if not dt:
            return False
        return dt > datetime.now(timezone.utc) - timedelta(days=days)

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


