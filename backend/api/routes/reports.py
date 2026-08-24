# -*- coding: utf-8 -*-
"""
API endpoints для генерации отчётов.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

from litestar import Request, Router, get, post
from litestar.controller import Controller
from litestar.exceptions import HTTPException
from pydantic import BaseModel

from backend.core.capture.agents import ReportGenerator
from backend.core.llm.router import get_llm_router
from backend.core.store.graph_builder import GraphBuilder

logger = logging.getLogger(__name__)


def _require_auth(request: Any) -> str:
    """Отчёты строятся по общему legacy-графу — раньше endpoints были ВООБЩЕ
    без авторизации (любой аноним генерил отчёт по содержимому графа).
    Минимальный гейт: валидный Bearer обязателен."""
    auth = (request.headers.get("authorization")
            or request.headers.get("Authorization"))
    from backend.api.middleware.auth_middleware import get_user_id_from_token
    try:
        uid = get_user_id_from_token(auth)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")
    if not uid:
        raise HTTPException(status_code=401, detail="authentication required")
    return uid


class ReportRequest(BaseModel):
    """Запрос на генерацию отчёта"""
    report_type: str  # executive_summary, weekly, project, risk
    period: Optional[str] = "week"  # day, week, month
    project_name: Optional[str] = None
    meeting_ids: Optional[List[str]] = None


# Глобальные объекты
_report_generator: Optional[ReportGenerator] = None


async def get_report_generator() -> ReportGenerator:
    """Получить генератор отчётов"""
    global _report_generator
    if _report_generator is None:
        llm = get_llm_router()
        graph = GraphBuilder(graph_storage_path="data/tessent_brain_graph.json")
        await graph.connect()
        _report_generator = ReportGenerator(llm_router=llm, graph_builder=graph)
    return _report_generator


class ReportsController(Controller):
    """Контроллер для работы с отчётами"""

    path = "/reports"

    @post("/generate")
    async def generate_report(self, request: Request, data: ReportRequest) -> dict:
        """
        Генерирует отчёт указанного типа.

        Args:
            data: Параметры отчёта

        Returns:
            Сгенерированный отчёт
        """
        _require_auth(request)
        logger.info(f"📊 Generating {data.report_type} report")

        try:
            generator = await get_report_generator()

            # Получаем данные встреч
            meetings_data = await self._get_meetings_data(
                meeting_ids=data.meeting_ids,
                period=data.period
            )

            if data.report_type == "executive_summary":
                report = await generator.generate_executive_summary(
                    meetings_data=meetings_data,
                    period=data.period
                )
            elif data.report_type == "weekly":
                report = await generator.generate_weekly_report(
                    meetings_data=meetings_data
                )
            elif data.report_type == "project" and data.project_name:
                # Фильтруем встречи по проекту
                project_meetings = [
                    m for m in meetings_data
                    if data.project_name.lower() in str(m.get("project", "")).lower()
                ]
                report = await generator.generate_project_report(
                    project_name=data.project_name,
                    project_data={},
                    meetings_data=project_meetings
                )
            elif data.report_type == "risk":
                report = await generator.generate_risk_report(
                    meetings_data=meetings_data
                )
            else:
                return {
                    "status": "error",
                    "error": f"Unknown report type: {data.report_type}"
                }

            return {
                "status": "success",
                "report": report
            }

        except Exception as e:
            logger.error(f"❌ Report generation error: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e)
            }

    @get("/executive-summary")
    async def get_executive_summary(
        self,
        request: Request,
        period: str = "week"
    ) -> dict:
        """
        Получает Executive Summary за период.

        Args:
            period: Период (day/week/month)

        Returns:
            Executive Summary
        """
        _require_auth(request)
        logger.info(f"📊 Getting Executive Summary for {period}")

        try:
            generator = await get_report_generator()
            meetings_data = await self._get_meetings_data(period=period)

            report = await generator.generate_executive_summary(
                meetings_data=meetings_data,
                period=period
            )

            return {
                "status": "success",
                "report": report
            }

        except Exception as e:
            logger.error(f"❌ Executive Summary error: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e)
            }

    @get("/weekly")
    async def get_weekly_report(self, request: Request) -> dict:
        """
        Получает еженедельный отчёт.

        Returns:
            Еженедельный отчёт
        """
        _require_auth(request)
        logger.info("📊 Getting Weekly Report")

        try:
            generator = await get_report_generator()
            meetings_data = await self._get_meetings_data(period="week")

            report = await generator.generate_weekly_report(
                meetings_data=meetings_data
            )

            return {
                "status": "success",
                "report": report
            }

        except Exception as e:
            logger.error(f"❌ Weekly Report error: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e)
            }

    @get("/project/{project_name:str}")
    async def get_project_report(self, request: Request, project_name: str) -> dict:
        """
        Получает отчёт по проекту.

        Args:
            project_name: Название проекта

        Returns:
            Отчёт по проекту
        """
        _require_auth(request)
        logger.info(f"📊 Getting Project Report for {project_name}")

        try:
            generator = await get_report_generator()
            meetings_data = await self._get_meetings_data(period="month")

            # Фильтруем по проекту
            project_meetings = [
                m for m in meetings_data
                if project_name.lower() in str(m.get("project", "")).lower()
            ]

            report = await generator.generate_project_report(
                project_name=project_name,
                project_data={},
                meetings_data=project_meetings
            )

            return {
                "status": "success",
                "report": report
            }

        except Exception as e:
            logger.error(f"❌ Project Report error: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e)
            }

    @get("/risks")
    async def get_risk_report(self, request: Request) -> dict:
        """
        Получает отчёт по рискам.

        Returns:
            Отчёт по рискам
        """
        _require_auth(request)
        logger.info("📊 Getting Risk Report")

        try:
            generator = await get_report_generator()
            meetings_data = await self._get_meetings_data(period="month")

            report = await generator.generate_risk_report(
                meetings_data=meetings_data
            )

            return {
                "status": "success",
                "report": report
            }

        except Exception as e:
            logger.error(f"❌ Risk Report error: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e)
            }

    async def _get_meetings_data(
        self,
        meeting_ids: Optional[List[str]] = None,
        period: Optional[str] = "week"
    ) -> List[dict]:
        """Получает данные встреч из графа"""
        try:
            graph = GraphBuilder(graph_storage_path="data/tessent_brain_graph.json")
            await graph.connect()

            nodes = graph.get_all_nodes()

            # Фильтруем встречи
            meetings = [n for n in nodes if n.get("_label") == "Meeting"]

            # Фильтруем по ID если указаны
            if meeting_ids:
                meetings = [m for m in meetings if m.get("id") in meeting_ids]

            # Фильтруем по периоду
            if period and not meeting_ids:
                now = datetime.now(timezone.utc)

                if period == "day":
                    cutoff = now - timedelta(days=1)
                elif period == "week":
                    cutoff = now - timedelta(days=7)
                elif period == "month":
                    cutoff = now - timedelta(days=30)
                else:
                    cutoff = now - timedelta(days=7)

                filtered = []
                for m in meetings:
                    date_str = m.get("date") or m.get("created_at")
                    if date_str:
                        try:
                            meeting_date = datetime.fromisoformat(
                                date_str.replace("Z", "+00:00")
                            )
                            if meeting_date > cutoff:
                                filtered.append(m)
                        except Exception:
                            filtered.append(m)  # Include if can't parse date
                    else:
                        filtered.append(m)

                meetings = filtered

            # Обогащаем данные связанными узлами
            enriched = []
            for meeting in meetings:
                meeting_id = meeting.get("id")

                # Собираем связанные данные
                meeting_data = dict(meeting)
                meeting_data["tasks"] = [
                    n for n in nodes
                    if n.get("_label") == "Task" and n.get("meeting_id") == meeting_id
                ]
                meeting_data["decisions"] = [
                    n for n in nodes
                    if n.get("_label") == "Decision" and n.get("meeting_id") == meeting_id
                ]
                meeting_data["participants"] = [
                    n for n in nodes
                    if n.get("_label") == "Person" and meeting_id in str(n.get("meetings", []))
                ]

                enriched.append(meeting_data)

            return enriched

        except Exception as e:
            logger.error(f"Error getting meetings data: {e}")
            return []


# Router
router = Router(path="", route_handlers=[ReportsController])


