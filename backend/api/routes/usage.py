# -*- coding: utf-8 -*-
"""
API endpoints для просмотра статистики использования LLM.
"""
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from litestar import Controller, Request, get, post
from litestar.params import Parameter
from pydantic import BaseModel

logger = logging.getLogger(__name__)

from ...core.llm.usage_tracker import get_usage_tracker, track_image_usage, track_usage


class TrackUsageRequest(BaseModel):
    """Запрос для трекинга использования LLM"""
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    model_tier: str = "standard"
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    agent_mode: Optional[str] = None
    request_type: Optional[str] = None
    # issue #110: атрибуция «по сущности» (опционально)
    document_id: Optional[str] = None
    meeting_id: Optional[str] = None
    job_id: Optional[str] = None
    surface: Optional[str] = None
    success: bool = True
    error: Optional[str] = None
    latency_ms: int = 0


class TrackImageRequest(BaseModel):
    """Запрос для трекинга генерации изображений"""
    provider: str
    model: str
    quality: str = "medium"
    size: str = "1024x1024"
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    success: bool = True
    error: Optional[str] = None
    latency_ms: int = 0
    prompt_length: int = 0


class UsageController(Controller):
    """Контроллер для статистики использования LLM"""

    path = "/usage"
    tags = ["Usage"]

    @get("/quota")
    async def get_quota(
        self,
        request: Request,
        user_id: Optional[str] = Parameter(query="user_id", default=None),
        tenant_id: Optional[str] = Parameter(query="tenant_id", default=None),
    ) -> Dict[str, Any]:
        """Текущий статус LLM-квоты для пользователя/тенанта.

        Возвращает limit / used / remaining / reset_at. 0 в limit = безлимит.
        Анти-IDOR: при валидном токене user_id берётся из него; спуфинг
        чужого → 403."""
        try:
            from litestar.exceptions import PermissionDeniedException

            from ...core.auth.service_token import trusted_user_id
            uid, src = trusted_user_id(request.headers, user_id or "")
            if src != "unverified" and uid:
                user_id = uid
        except PermissionError:
            raise PermissionDeniedException("token not authorized for requested user_id")
        except Exception:
            pass
        from ...core.llm.quota import get_quota_status
        return await get_quota_status(user_id=user_id, tenant_id=tenant_id)

    @get("/cache-stats")
    async def get_prompt_cache_stats(self) -> Dict[str, Any]:
        """Статистика exact-match prompt cache (Phase 4b).

        Показывает сколько LLM-вызовов сэкономил кэш (hit_rate) и сколько
        ошибок Redis ловилось (errors).
        """
        from ...core.llm.prompt_cache import get_cache_stats
        return get_cache_stats()

    @post("/track")
    async def track_llm_usage(self, data: TrackUsageRequest) -> Dict[str, Any]:
        """
        Записать использование LLM.

        Используется для трекинга вызовов из внешних источников (например, Next.js API routes).
        """
        record = track_usage(
            provider=data.provider,
            model=data.model,
            input_tokens=data.input_tokens,
            output_tokens=data.output_tokens,
            cached_tokens=data.cached_tokens,
            model_tier=data.model_tier,
            user_id=data.user_id,
            session_id=data.session_id,
            agent_mode=data.agent_mode,
            request_type=data.request_type,
            document_id=data.document_id,
            meeting_id=data.meeting_id,
            job_id=data.job_id,
            surface=data.surface,
            success=data.success,
            error=data.error,
            latency_ms=data.latency_ms,
        )

        return {
            "success": True,
            "record": {
                "id": record.id,
                "model": record.model,
                "total_tokens": record.total_tokens,
                "total_cost": record.total_cost,
                "total_cost_formatted": f"${record.total_cost:.6f}",
            }
        }

    @post("/track-image")
    async def track_image_generation(self, data: TrackImageRequest) -> Dict[str, Any]:
        """
        Записать использование генерации изображений.

        Используется для трекинга генерации изображений на вкладке "Доска".
        """
        record = track_image_usage(
            provider=data.provider,
            model=data.model,
            quality=data.quality,
            size=data.size,
            user_id=data.user_id,
            session_id=data.session_id,
            success=data.success,
            error=data.error,
            latency_ms=data.latency_ms,
            prompt_length=data.prompt_length,
        )

        return {
            "success": True,
            "record": {
                "id": record.id,
                "model": record.model,
                "total_cost": record.total_cost,
                "total_cost_formatted": f"${record.total_cost:.6f}",
            }
        }

    @post("/seed-demo")
    async def seed_demo_tenant(
        self,
        user_id: Optional[str] = Parameter(query="user_id", default=None),
        tenant_id: Optional[str] = Parameter(query="tenant_id", default=None),
    ) -> Dict[str, Any]:
        """Seed демо-данных для wow-screen — HomeTab показывает не пустую
        страницу новому юзеру.

        После вызова: /api/v1/insights/weekly начинает возвращать insights,
        /api/v1/usage/by-meeting/{id} имеет данные для sparkline.

        Идемпотентно: если tenant уже имеет >5 ingest-записей — пропуск.
        """
        from ...core.llm.demo_seed import seed_demo_data
        return seed_demo_data(user_id=user_id, tenant_id=tenant_id)

    @get("/stats")
    async def get_stats(
        self,
        period: str = Parameter(query="period", default="today"),  # today, week, month, all
        tenant_id: Optional[str] = Parameter(query="tenant_id", default=None),
    ) -> Dict[str, Any]:
        """
        Получить статистику использования LLM.

        Args:
            period: Период (today, week, month, all)
            tenant_id: P2 #11 — отфильтровать по конкретной org. Без него —
                агрегат по всем (с per-tenant breakdown в `by_tenant`).
        """
        tracker = get_usage_tracker()

        if period == "today":
            return {
                "period": "today",
                "date": datetime.now().strftime("%Y-%m-%d"),
                **tracker.get_daily_stats(tenant_id=tenant_id)
            }
        elif period == "session":
            return {
                "period": "session",
                **tracker.get_session_stats()
            }
        elif period == "all":
            return {
                "period": "all",
                **tracker.get_total_stats()
            }
        elif period == "week":
            # Статистика за последние 7 дней
            stats = []
            total_cost = 0.0
            total_tokens = 0
            total_requests = 0

            for i in range(7):
                date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
                day_stats = tracker.get_daily_stats(date, tenant_id=tenant_id)
                stats.append({
                    "date": date,
                    **day_stats
                })
                total_cost += day_stats.get("total_cost", 0)
                total_tokens += day_stats.get("total_tokens", 0)
                total_requests += day_stats.get("requests_count", 0)

            return {
                "period": "week",
                "tenant_id": tenant_id,
                "total_cost": round(total_cost, 4),
                "total_cost_formatted": f"${total_cost:.4f}",
                "total_tokens": total_tokens,
                "total_requests": total_requests,
                "daily_stats": stats
            }
        else:
            return {"error": f"Unknown period: {period}"}

    @get("/recent")
    async def get_recent_usage(
        self,
        limit: int = Parameter(query="limit", default=50),
    ) -> Dict[str, Any]:
        """
        Получить последние записи использования.

        Args:
            limit: Количество записей (макс. 100)
        """
        tracker = get_usage_tracker()
        limit = min(limit, 100)

        records = tracker.get_recent_usage(limit)

        return {
            "count": len(records),
            "records": records
        }

    @get("/daily/{date:str}")
    async def get_daily_usage(
        self,
        date: str,
    ) -> Dict[str, Any]:
        """
        Получить статистику за конкретный день.

        Args:
            date: Дата в формате YYYY-MM-DD
        """
        tracker = get_usage_tracker()
        return tracker.get_daily_stats(date)

    @get("/summary")
    async def get_summary(self) -> Dict[str, Any]:
        """Получить сводную статистику"""
        tracker = get_usage_tracker()

        today = tracker.get_daily_stats()
        total = tracker.get_total_stats()
        session = tracker.get_session_stats()

        return {
            "session": {
                "requests": session.get("requests_count", 0),
                "tokens": session.get("total_input_tokens", 0) + session.get("total_output_tokens", 0),
                "cost": session.get("total_cost_formatted", "$0.0000"),
            },
            "today": {
                "requests": today.get("requests_count", 0),
                "tokens": today.get("total_tokens", 0),
                "cost": today.get("total_cost_formatted", "$0.0000"),
                "by_model": today.get("by_model", {}),
            },
            "all_time": {
                "requests": total.get("requests_count", 0),
                "tokens": total.get("total_tokens", 0),
                "cost": total.get("total_cost_formatted", "$0.0000"),
                "first_request": total.get("first_request"),
                "last_request": total.get("last_request"),
            },
            "pricing_info": {
                "gemini-flash-lite-latest": {"input": "$0.075/1M", "output": "$0.30/1M"},
                "gemini-flash-latest": {"input": "$0.50/1M", "output": "$3.00/1M"},
                "gpt-4o": {"input": "$2.50/1M", "output": "$10.00/1M"},
                "gpt-4o-mini": {"input": "$0.15/1M", "output": "$0.60/1M"},
            }
        }

    @get("/by-operation")
    async def get_usage_by_operation(
        self,
        period: str = Parameter(query="period", default="today"),  # today, week, month, all
    ) -> Dict[str, Any]:
        """
        Получить статистику использования по типам операций.

        Группирует расходы по:
        - agent_mode: brain, mark, sync, documents, templates, workflow, capture, night_processor
        - request_type: chat, knowledge_extraction, document_generation, etc.
        """
        tracker = get_usage_tracker()

        # Используем SQL напрямую вместо парсинга в Python — быстрее и точнее
        import sqlite3
        from datetime import datetime, timedelta
        from datetime import timezone as tz

        now_utc = datetime.now(tz.utc)
        now_local = datetime.now()

        if period == "today":
            # Начало сегодняшнего дня по ЛОКАЛЬНОМУ времени, конвертированное в UTC
            local_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            # Разница между локальным и UTC (например, для UTC+3 это 3 часа)
            utc_offset = now_local - now_utc.replace(tzinfo=None)
            start_date_utc = local_start - utc_offset
        elif period == "week":
            start_date_utc = now_utc.replace(tzinfo=None) - timedelta(days=7)
        elif period == "month":
            start_date_utc = now_utc.replace(tzinfo=None) - timedelta(days=30)
        else:
            start_date_utc = datetime.min

        start_iso = start_date_utc.isoformat()

        # Запрашиваем из SQLite напрямую — быстрее чем парсить 10000 записей
        try:
            conn = sqlite3.connect(tracker.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute("""
                SELECT * FROM llm_usage
                WHERE timestamp >= ?
                ORDER BY timestamp DESC
            """, (start_iso,))

            records = [dict(row) for row in cursor.fetchall()]
            conn.close()
        except Exception as e:
            logger.error(f"Failed to query usage: {e}")
            records = tracker.get_recent_usage(10000)

        # Группируем по agent_mode и request_type
        by_agent_mode = {}
        by_request_type = {}
        by_model = {}
        total_cost = 0.0
        total_tokens = 0

        for record in records:

            cost = record.get("total_cost", 0) or record.get("cost", 0) or 0
            tokens = record.get("total_tokens") or ((record.get("input_tokens", 0) or 0) + (record.get("output_tokens", 0) or 0))

            total_cost += cost
            total_tokens += tokens

            # По agent_mode
            agent_mode = record.get("agent_mode") or "unknown"
            if agent_mode not in by_agent_mode:
                by_agent_mode[agent_mode] = {"requests": 0, "tokens": 0, "cost": 0.0}
            by_agent_mode[agent_mode]["requests"] += 1
            by_agent_mode[agent_mode]["tokens"] += tokens
            by_agent_mode[agent_mode]["cost"] += cost

            # По request_type
            request_type = record.get("request_type") or "unknown"
            if request_type not in by_request_type:
                by_request_type[request_type] = {"requests": 0, "tokens": 0, "cost": 0.0}
            by_request_type[request_type]["requests"] += 1
            by_request_type[request_type]["tokens"] += tokens
            by_request_type[request_type]["cost"] += cost

            # По модели
            model = record.get("model") or "unknown"
            if model not in by_model:
                by_model[model] = {"requests": 0, "tokens": 0, "cost": 0.0}
            by_model[model]["requests"] += 1
            by_model[model]["tokens"] += tokens
            by_model[model]["cost"] += cost

        # Форматируем стоимость
        for data in by_agent_mode.values():
            data["cost_formatted"] = f"${data['cost']:.4f}"
        for data in by_request_type.values():
            data["cost_formatted"] = f"${data['cost']:.4f}"
        for data in by_model.values():
            data["cost_formatted"] = f"${data['cost']:.4f}"

        return {
            "period": period,
            "total": {
                "tokens": total_tokens,
                "cost": round(total_cost, 4),
                "cost_formatted": f"${total_cost:.4f}"
            },
            "by_agent_mode": by_agent_mode,
            "by_request_type": by_request_type,
            "by_model": by_model
        }

    # ────────────────────────────────────────────────────────────────────
    # issue #110: атрибуция «по сущности»
    # GET /usage/by-document/{id}, /by-meeting/{id}, /by-job/{id}, /attribution
    # Отвечают на вопрос: «сколько стоил прогон документа/встречи/job-а X?»
    # ────────────────────────────────────────────────────────────────────

    @get("/by-document/{document_id:str}")
    async def get_by_document(
        self,
        document_id: str,
        tenant_id: Optional[str] = Parameter(query="tenant_id", default=None),
    ) -> Dict[str, Any]:
        """Стоимость прогона конкретного документа.

        Суммирует все LLM-вызовы, у которых `document_id` совпадает.
        tenant_id — опциональный фильтр (рекомендуется для multi-tenant)."""
        from ...core.llm.attribution import aggregate
        return aggregate("document_id", document_id, tenant_id)

    @get("/by-meeting/{meeting_id:str}")
    async def get_by_meeting(
        self,
        meeting_id: str,
        tenant_id: Optional[str] = Parameter(query="tenant_id", default=None),
    ) -> Dict[str, Any]:
        """Стоимость прогона конкретной встречи (транскрипт → онтология
        → факты → дайджест)."""
        from ...core.llm.attribution import aggregate
        return aggregate("meeting_id", meeting_id, tenant_id)

    @get("/by-job/{job_id:str}")
    async def get_by_job(
        self,
        job_id: str,
        tenant_id: Optional[str] = Parameter(query="tenant_id", default=None),
    ) -> Dict[str, Any]:
        """Стоимость конкретной фоновой задачи (taskiq-job).

        Закрывает сценарий «массовый ingest CRM сожрал лимит — теперь
        видно, какая именно задача и сколько»."""
        from ...core.llm.attribution import aggregate
        return aggregate("job_id", job_id, tenant_id)

    @get("/attribution")
    async def get_attribution_top(
        self,
        group_by: str = Parameter(query="group_by", default="meeting_id"),
        period: str = Parameter(query="period", default="week"),
        surface: Optional[str] = Parameter(query="surface", default=None),
        tenant_id: Optional[str] = Parameter(query="tenant_id", default=None),
        limit: int = Parameter(query="limit", default=20),
    ) -> Dict[str, Any]:
        """Топ-N сущностей по стоимости за период.

        Args:
            group_by: meeting_id | document_id | job_id | surface | model
            period: today | week | month | all
            surface: фильтр по surface (chat|agent|ingest|ontology|…)
            tenant_id: фильтр по тенанту (обяз. на проде)
            limit: max 100
        """
        allowed = {"meeting_id", "document_id", "job_id", "surface", "model"}
        if group_by not in allowed:
            return {"error": f"group_by must be one of {sorted(allowed)}"}
        limit = max(1, min(int(limit), 100))
        from ...core.llm.attribution import top
        return top(group_by, period, surface, tenant_id, limit)


# ────────────────────────────────────────────────────────────────────────
# Legacy-хелперы (старая реализация). Перенесена в backend.core.llm.attribution
# для лёгкого импорта в тестах (роуты тянут autogen). Оставляю тонкие
# обёртки для обратной совместимости — если кто-то импортирует _attribution_*
# отсюда напрямую.
# ────────────────────────────────────────────────────────────────────────


def _attribution_aggregate(column: str, value: str, tenant_id: Optional[str]) -> Dict[str, Any]:
    """Тонкий шим для обратной совместимости — реализация в
    `backend.core.llm.attribution.aggregate`."""
    from ...core.llm.attribution import aggregate
    return aggregate(column, value, tenant_id)


def _attribution_top(
    group_by: str,
    period: str,
    surface: Optional[str],
    tenant_id: Optional[str],
    limit: int,
) -> Dict[str, Any]:
    """Тонкий шим для обратной совместимости — реализация в
    `backend.core.llm.attribution.top`."""
    from ...core.llm.attribution import top
    return top(group_by, period, surface, tenant_id, limit)
