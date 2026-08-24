# -*- coding: utf-8 -*-
"""
Agent Automation Service — агентные автоматизации в стиле OpenClaw.

Вместо жёстких ActionType, задача описывается на естественном языке,
а AI-агент (run_react_loop_bigllm) сам выбирает и вызывает инструменты.

Поддерживает:
- CRUD для agent_automations (Supabase)
- Выполнение через BigLLM ReAct loop
- Trace (полный лог шагов агента)
- LLM usage tracking
- Cron, once, event-triggered расписания
"""
import asyncio
import json
import logging
import os
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
from croniter import croniter

logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
from backend.core.utils.meetflow_path import default_meetflow_path as _dmp
MEETFLOW_PATH = _dmp()

TABLE_NAME = "agent_automations"
LOGS_TABLE = "agent_automation_logs"


@dataclass
class AgentAutomation:
    """Модель агентной автоматизации."""
    id: str
    user_id: str
    name: str
    instruction: str

    schedule_type: str = "once"
    execute_at: Optional[str] = None
    cron_expression: Optional[str] = None
    interval_seconds: Optional[int] = None

    trigger_type: str = "schedule"
    event_type: Optional[str] = None
    event_filter: Optional[Dict[str, Any]] = None

    conditions: Optional[List[Dict[str, Any]]] = None

    model_tier: str = "standard"
    max_react_steps: int = 5

    status: str = "active"
    last_run_at: Optional[str] = None
    last_run_status: Optional[str] = None
    last_error: Optional[str] = None
    run_count: int = 0

    max_runs: Optional[int] = None
    expires_at: Optional[str] = None

    last_run_trace: Optional[List[Dict[str, Any]]] = None

    total_tokens_used: int = 0
    total_cost_usd: float = 0.0
    last_run_tokens: int = 0
    last_run_cost_usd: float = 0.0

    description: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    # === W16 Skills extension ============================================
    # Skill = повторно используемая автоматизация с параметризованным
    # шаблоном и явной schema. См. backend/core/skills/.
    is_skill: bool = False
    instruction_template: Optional[str] = None  # текст с {param}-плейсхолдерами
    parameters_schema: Optional[List[Dict[str, Any]]] = None
    parent_skill_id: Optional[str] = None
    version: int = 1
    shared_in_tenant: bool = False
    source_conversation_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentAutomation":
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            user_id=data.get("user_id", ""),
            name=data.get("name", ""),
            instruction=data.get("instruction", ""),
            schedule_type=data.get("schedule_type", "once"),
            execute_at=data.get("execute_at"),
            cron_expression=data.get("cron_expression"),
            interval_seconds=data.get("interval_seconds"),
            trigger_type=data.get("trigger_type", "schedule"),
            event_type=data.get("event_type"),
            event_filter=data.get("event_filter"),
            conditions=data.get("conditions"),
            model_tier=data.get("model_tier", "standard"),
            max_react_steps=data.get("max_react_steps", 5),
            status=data.get("status", "active"),
            last_run_at=data.get("last_run_at"),
            last_run_status=data.get("last_run_status"),
            last_error=data.get("last_error"),
            run_count=data.get("run_count", 0),
            max_runs=data.get("max_runs"),
            expires_at=data.get("expires_at"),
            last_run_trace=data.get("last_run_trace"),
            total_tokens_used=data.get("total_tokens_used", 0),
            total_cost_usd=data.get("total_cost_usd", 0.0),
            last_run_tokens=data.get("last_run_tokens", 0),
            last_run_cost_usd=data.get("last_run_cost_usd", 0.0),
            description=data.get("description"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            # W16:
            is_skill=bool(data.get("is_skill", False)),
            instruction_template=data.get("instruction_template"),
            parameters_schema=data.get("parameters_schema"),
            parent_skill_id=data.get("parent_skill_id"),
            version=int(data.get("version", 1) or 1),
            shared_in_tenant=bool(data.get("shared_in_tenant", False)),
            source_conversation_id=data.get("source_conversation_id"),
        )


class AgentAutomationService:
    """Сервис управления агентными автоматизациями."""

    def __init__(self):
        self.supabase_url = SUPABASE_URL
        self.supabase_key = SUPABASE_KEY
        self._headers = {
            "apikey": self.supabase_key,
            "Authorization": f"Bearer {self.supabase_key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict] = None,
        json_data: Optional[Any] = None,
    ) -> Any:
        url = f"{self.supabase_url}{path}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(
                method=method,
                url=url,
                headers=self._headers,
                params=params,
                json=json_data,
            )
            response.raise_for_status()
            if response.status_code == 204:
                return None
            return response.json()

    # ==================== CRUD ====================

    async def create(
        self,
        payload: Optional[Dict[str, Any]] = None,
        *,
        user_id: str = "",
        name: str = "",
        instruction: str = "",
        schedule_type: str = "once",
        execute_at: Optional[str] = None,
        cron_expression: Optional[str] = None,
        interval_seconds: Optional[int] = None,
        trigger_type: str = "schedule",
        event_type: Optional[str] = None,
        event_filter: Optional[Dict] = None,
        conditions: Optional[List[Dict]] = None,
        model_tier: str = "standard",
        max_react_steps: int = 5,
        description: Optional[str] = None,
        max_runs: Optional[int] = None,
        expires_at: Optional[str] = None,
    ) -> AgentAutomation:
        # (фикс аудита W16) create() вызывался и словарём (skills.py,
        # preinstalled.py), и kwargs — словарь падал TypeError, из-за чего
        # создание/предустановка скиллов были полностью сломаны. Теперь
        # словарь распаковывается, неизвестные поля (is_skill,
        # instruction_template, parameters_schema, shared_in_tenant...)
        # уходят в insert как есть.
        extra_fields: Dict[str, Any] = {}
        if payload is not None:
            extra = dict(payload)
            user_id = extra.pop("user_id", user_id)
            name = extra.pop("name", name)
            instruction = extra.pop("instruction", instruction)
            schedule_type = extra.pop("schedule_type", schedule_type)
            trigger_type = extra.pop("trigger_type", trigger_type)
            execute_at = extra.pop("execute_at", execute_at)
            cron_expression = extra.pop("cron_expression", cron_expression)
            interval_seconds = extra.pop("interval_seconds", interval_seconds)
            event_type = extra.pop("event_type", event_type)
            event_filter = extra.pop("event_filter", event_filter)
            conditions = extra.pop("conditions", conditions)
            model_tier = extra.pop("model_tier", model_tier)
            max_react_steps = extra.pop("max_react_steps", max_react_steps)
            description = extra.pop("description", description)
            max_runs = extra.pop("max_runs", max_runs)
            expires_at = extra.pop("expires_at", expires_at)
            extra_fields = {k: v for k, v in extra.items() if v is not None}
        if not user_id or not name or not instruction:
            raise ValueError("create() требует user_id, name и instruction")
        automation_id = str(extra_fields.pop("id", "")) or str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        data: Dict[str, Any] = {
            "id": automation_id,
            "user_id": user_id,
            "name": name,
            "instruction": instruction,
            "schedule_type": schedule_type,
            "trigger_type": trigger_type,
            "model_tier": model_tier,
            "max_react_steps": max_react_steps,
            "status": "active",
            "run_count": 0,
            "total_tokens_used": 0,
            "total_cost_usd": 0.0,
            "last_run_tokens": 0,
            "last_run_cost_usd": 0.0,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }

        if execute_at:
            data["execute_at"] = execute_at
        if cron_expression:
            data["cron_expression"] = cron_expression
        if interval_seconds:
            data["interval_seconds"] = interval_seconds
        if event_type:
            data["event_type"] = event_type
        if event_filter:
            data["event_filter"] = event_filter
        if conditions:
            data["conditions"] = conditions
        if description:
            data["description"] = description
        if max_runs is not None:
            data["max_runs"] = max_runs
        if expires_at:
            data["expires_at"] = expires_at

        if extra_fields:
            data.update(extra_fields)

        result = await self._request("POST", f"/rest/v1/{TABLE_NAME}", json_data=data)
        automation = AgentAutomation.from_dict(result[0]) if result and len(result) > 0 else AgentAutomation.from_dict(data)

        # EventBus registration
        if trigger_type == "event" and event_type:
            try:
                from backend.core.automations.event_bus import get_event_bus
                bus = get_event_bus()
                bus.register_event_automation(
                    user_id=user_id,
                    event_type=event_type,
                    automation_id=automation.id,
                    filter_conditions=event_filter,
                )
                logger.info(f"[AgentAutoService] Registered event automation '{name}' for event '{event_type}'")
            except Exception as eb_err:
                logger.warning(f"[AgentAutoService] EventBus registration failed: {eb_err}")

        return automation

    async def get(self, automation_id: str) -> Optional[AgentAutomation]:
        params = {"id": f"eq.{automation_id}", "select": "*"}
        result = await self._request("GET", f"/rest/v1/{TABLE_NAME}", params=params)
        if result and len(result) > 0:
            return AgentAutomation.from_dict(result[0])
        return None

    async def list(
        self,
        user_id: Optional[str] = None,
        status: Optional[str] = None,
        schedule_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[AgentAutomation]:
        params: Dict[str, str] = {
            "select": "*",
            "order": "created_at.desc",
            "limit": str(limit),
            "offset": str(offset),
        }
        if user_id:
            params["user_id"] = f"eq.{user_id}"
        if status:
            params["status"] = f"eq.{status}"
        if schedule_type:
            params["schedule_type"] = f"eq.{schedule_type}"

        result = await self._request("GET", f"/rest/v1/{TABLE_NAME}", params=params)
        return [AgentAutomation.from_dict(r) for r in (result or [])]

    async def update(self, automation_id: str, updates: Dict[str, Any]) -> bool:
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        params = {"id": f"eq.{automation_id}"}
        await self._request("PATCH", f"/rest/v1/{TABLE_NAME}", params=params, json_data=updates)
        return True

    async def delete(self, automation_id: str) -> bool:
        params = {"id": f"eq.{automation_id}"}
        await self._request("DELETE", f"/rest/v1/{TABLE_NAME}", params=params)
        return True

    async def _patch_run(self, automation_id: str, fields: Dict[str, Any]) -> None:
        """Лёгкий best-effort PATCH прогресса выполнения — не должен ронять
        прогон, если БД временно недоступна."""
        try:
            await self.update(automation_id, fields)
        except Exception as e:
            logger.debug("progress patch skipped: %s", e)

    # ==================== Status actions ====================

    async def pause(self, automation_id: str) -> bool:
        return await self.update(automation_id, {"status": "paused"})

    async def resume(self, automation_id: str) -> bool:
        return await self.update(automation_id, {"status": "active"})

    async def cancel(self, automation_id: str) -> bool:
        return await self.update(automation_id, {"status": "cancelled"})

    # ==================== Logs ====================

    async def save_log(
        self,
        automation_id: str,
        status: str,
        started_at: datetime,
        completed_at: Optional[datetime] = None,
        duration_ms: int = 0,
        trace: Optional[List[Dict]] = None,
        final_answer: str = "",
        error_message: Optional[str] = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        model_tier: str = "standard",
    ) -> None:
        log_data = {
            "id": str(uuid.uuid4()),
            "automation_id": automation_id,
            "status": status,
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat() if completed_at else None,
            "duration_ms": duration_ms,
            "trace": trace or [],
            "final_answer": final_answer,
            "error_message": error_message,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_usd,
            "model_tier": model_tier,
        }
        try:
            await self._request("POST", f"/rest/v1/{LOGS_TABLE}", json_data=log_data)
        except Exception as e:
            logger.error(f"[AgentAutoService] Failed to save log: {e}")

    async def get_logs(self, automation_id: str, limit: int = 20) -> List[Dict]:
        params = {
            "automation_id": f"eq.{automation_id}",
            "select": "*",
            "order": "started_at.desc",
            "limit": str(limit),
        }
        result = await self._request("GET", f"/rest/v1/{LOGS_TABLE}", params=params)
        return result or []

    # ==================== Scheduler helpers ====================

    async def get_due_automations(self) -> List[AgentAutomation]:
        """Get automations that are due for execution (cron/once/interval)."""
        now = datetime.now(timezone.utc)
        all_active = await self.list(status="active")
        due: List[AgentAutomation] = []

        for a in all_active:
            if a.trigger_type == "event":
                continue  # event-triggered are handled by EventBus

            if a.max_runs and a.run_count >= a.max_runs:
                continue
            if a.expires_at:
                try:
                    exp = datetime.fromisoformat(a.expires_at.replace("Z", "+00:00"))
                    if now > exp:
                        continue
                except Exception:
                    logger.debug("suppressed exception", exc_info=True)

            if a.schedule_type == "once":
                if a.execute_at and a.run_count == 0:
                    try:
                        exec_time = datetime.fromisoformat(a.execute_at.replace("Z", "+00:00"))
                        if now >= exec_time:
                            due.append(a)
                    except Exception:
                        logger.debug("suppressed exception", exc_info=True)

            elif a.schedule_type == "recurring":
                last_run = None
                if a.last_run_at:
                    try:
                        last_run = datetime.fromisoformat(a.last_run_at.replace("Z", "+00:00"))
                    except Exception:
                        logger.debug("suppressed exception", exc_info=True)

                if a.cron_expression:
                    try:
                        base_time = last_run or datetime.fromisoformat(a.created_at.replace("Z", "+00:00")) if a.created_at else now - timedelta(days=1)
                        cron = croniter(a.cron_expression, base_time)
                        next_run = cron.get_next(datetime)
                        if next_run.tzinfo is None:
                            next_run = next_run.replace(tzinfo=timezone.utc)
                        if now >= next_run:
                            due.append(a)
                    except Exception:
                        logger.debug("suppressed exception", exc_info=True)

                elif a.interval_seconds and a.interval_seconds > 0:
                    if last_run is None:
                        due.append(a)
                    else:
                        next_run = last_run + timedelta(seconds=a.interval_seconds)
                        if now >= next_run:
                            due.append(a)

        return due

    # ==================== Execution ====================

    async def execute(self, automation: AgentAutomation) -> Dict[str, Any]:
        """Execute an agent automation via BigLLM ReAct loop."""
        start_time = datetime.now(timezone.utc)
        result: Dict[str, Any] = {"success": False, "message": "", "trace": [], "final_answer": ""}

        try:
            logger.info(f"[AgentAutoService] Executing: {automation.name} (instruction={automation.instruction[:80]}...)")

            # Set user context
            if MEETFLOW_PATH not in sys.path:
                sys.path.insert(0, MEETFLOW_PATH)
            try:
                from automation_tools_async import set_user_context
                set_user_context(user_id=automation.user_id)
            except Exception:
                logger.debug("suppressed exception", exc_info=True)

            # Build tools and executor
            tools_catalog, tool_executor = await self._build_tools_and_executor(automation.user_id)

            # Try to get user email for send operations
            user_email = await self._get_user_email(automation.user_id)

            # Import ReAct loop
            from backend.core.llm.router import LLMRouter
            from backend.core.local_models.ollama_functiongemma import (
                ReActResult,
                run_react_loop_bigllm,
            )

            llm_router = LLMRouter()

            # Прогресс-стриминг: пишем текущий шаг в last_run_status (существующая
            # колонка, без миграции). Фронт поллит GET и показывает «шаг N/M».
            # Персистится в БД → корректно и на нескольких воркерах.
            await self._patch_run(automation.id, {
                "last_run_status": "running", "last_run_at": start_time.isoformat()})

            async def _on_step(step: int, total: int, tool: str):
                await self._patch_run(automation.id, {
                    "last_run_status": f"running:{step}/{total}:{tool}"})

            react_result: ReActResult = await run_react_loop_bigllm(
                user_message=automation.instruction,
                tool_executor=tool_executor,
                llm_router=llm_router,
                tools=tools_catalog,
                max_steps=automation.max_react_steps,
                model_tier=automation.model_tier,
                user_email=user_email,
                on_step=_on_step,
            )

            end_time = datetime.now(timezone.utc)
            duration_ms = int((end_time - start_time).total_seconds() * 1000)

            trace_list = []
            for i, step in enumerate(react_result.steps, 1):
                trace_list.append({
                    "step": i,
                    "thought": step.thought,
                    "tool": step.tool,
                    "args": step.args,
                    "observation": step.observation[:2000] if step.observation else "",
                    "is_final": step.is_final,
                })

            run_status = "success" if react_result.success else "failed"

            # Реальная стоимость запуска (фикс аудита: раньше жёстко 0.0).
            # Цена модели tier из того же прайса, что usage_tracker.
            _in_tok = react_result.total_input_tokens
            _out_tok = react_result.total_output_tokens
            _run_cost = 0.0
            try:
                from backend.core.llm.usage_tracker import estimate_cost
                _model = ("gemini-flash-latest" if automation.model_tier == "premium"
                          else "gemini-flash-lite-latest")
                _run_cost = estimate_cost(_model, _in_tok, _out_tok)
            except Exception:
                logger.debug("agent-auto cost estimate failed", exc_info=True)

            # Update automation record
            update_data: Dict[str, Any] = {
                "last_run_at": end_time.isoformat(),
                "last_run_status": run_status,
                "run_count": automation.run_count + 1,
                "last_run_trace": trace_list,
                "last_run_tokens": _in_tok + _out_tok,
                "last_run_cost_usd": round(_run_cost, 6),
                "total_tokens_used": automation.total_tokens_used + _in_tok + _out_tok,
                "total_cost_usd": round((automation.total_cost_usd or 0.0) + _run_cost, 6),
            }

            if not react_result.success:
                update_data["last_error"] = react_result.error or "Unknown error"
            else:
                update_data["last_error"] = None

            # Mark once-tasks as completed after first run
            if automation.schedule_type == "once":
                update_data["status"] = "completed"
            elif automation.max_runs and (automation.run_count + 1) >= automation.max_runs:
                update_data["status"] = "completed"

            await self.update(automation.id, update_data)

            # Save execution log
            await self.save_log(
                automation_id=automation.id,
                status=run_status,
                started_at=start_time,
                completed_at=end_time,
                duration_ms=duration_ms,
                trace=trace_list,
                final_answer=react_result.final_answer,
                error_message=react_result.error if not react_result.success else None,
                input_tokens=react_result.total_input_tokens,
                output_tokens=react_result.total_output_tokens,
                model_tier=automation.model_tier,
            )

            result = {
                "success": react_result.success,
                "message": react_result.final_answer or "Executed",
                "trace": trace_list,
                "final_answer": react_result.final_answer,
                "tokens": react_result.total_input_tokens + react_result.total_output_tokens,
                "duration_ms": duration_ms,
                "steps_count": len(react_result.steps),
            }

        except Exception as e:
            end_time = datetime.now(timezone.utc)
            duration_ms = int((end_time - start_time).total_seconds() * 1000)
            logger.error(f"[AgentAutoService] Execution error for '{automation.name}': {e}")

            await self.update(automation.id, {
                "last_run_at": end_time.isoformat(),
                "last_run_status": "failed",
                "last_error": str(e),
                "run_count": automation.run_count + 1,
            })

            await self.save_log(
                automation_id=automation.id,
                status="failed",
                started_at=start_time,
                completed_at=end_time,
                duration_ms=duration_ms,
                error_message=str(e),
                model_tier=automation.model_tier,
            )

            result = {"success": False, "message": f"Error: {e}", "trace": [], "final_answer": ""}

        return result

    async def _get_user_email(self, user_id: str) -> str:
        """Try to get user email via Gmail integration (getProfile API)."""
        try:
            from backend.integrations.gmail_integration import GmailIntegration
            gmail = GmailIntegration(user_id=user_id)
            if await gmail.load_credentials():
                email = await gmail.get_user_email()
                if email:
                    logger.info(f"[AgentAutoService] Got user email via Gmail: {email}")
                    return email
        except Exception as e:
            logger.debug(f"[AgentAutoService] Could not get user email: {e}")
        return ""

    async def _build_tools_and_executor(self, user_id: str):
        """Build tools catalog and executor function (same as Hybrid mode)."""
        from backend.core.llm.router import LLMRouter
        from backend.core.local_models.ollama_functiongemma import get_tools_catalog
        from backend.integrations.tessent_brain_tools import TessentBrainTools

        llm_router = LLMRouter()

        graph_builder = None
        vector_indexer = None

        reasoning_engine = None
        if llm_router and graph_builder:
            try:
                from backend.core.think.reasoning_engine import ReasoningEngine
                reasoning_engine = ReasoningEngine(
                    graph_builder=graph_builder,
                    vector_indexer=vector_indexer,
                    llm_router=llm_router,
                )
            except Exception:
                logger.debug("suppressed exception", exc_info=True)

        tools = TessentBrainTools(
            graph_builder=graph_builder,
            reasoning_engine=reasoning_engine,
            user_id=user_id,
        )

        # Integration registry
        integration_registry = None
        try:
            from backend.integrations.registry import IntegrationRegistry
            integration_registry = IntegrationRegistry()
            await integration_registry.load_for_user(user_id)
        except Exception as e:
            logger.warning(f"[AgentAutoService] IntegrationRegistry load failed: {e}")

        # Build catalog
        tools_catalog = get_tools_catalog(None)
        if integration_registry:
            existing_names = {t["name"] for t in tools_catalog}
            for tool_def in integration_registry.get_all_tool_definitions():
                if tool_def["name"] not in existing_names:
                    tools_catalog.append({
                        "name": tool_def["name"],
                        "description": tool_def.get("description", ""),
                        "parameters": tool_def.get("parameters", {"type": "object", "properties": {}}),
                    })

        async def execute_tool(tool_name: str, args: dict) -> str:
            """Execute a tool by name + args."""
            try:
                if integration_registry:
                    for tool_def in integration_registry.get_all_tool_definitions():
                        if tool_def["name"] == tool_name:
                            handler = tool_def.get("handler")
                            if handler:
                                r = await handler(**args) if asyncio.iscoroutinefunction(handler) else handler(**args)
                                return json.dumps(r, ensure_ascii=False, default=str) if not isinstance(r, str) else r

                method = getattr(tools, tool_name, None)
                if method and callable(method):
                    if asyncio.iscoroutinefunction(method):
                        r = await method(**args)
                    else:
                        r = method(**args)
                    return json.dumps(r, ensure_ascii=False, default=str) if not isinstance(r, str) else r

                if tool_name == "analyze_with_llm":
                    task = str(args.get("task") or args.get("query") or "")
                    context = str(args.get("context") or "")
                    prompt = (
                        f"Задача: {task}\n\nКонтекст:\n{context[:3000]}\n\n"
                        "Выполни задачу СТРОГО по контексту выше. Не выдумывай "
                        "фактов, цифр и имён, которых в контексте нет; если данных "
                        "не хватает — честно скажи об этом.")
                    try:
                        response = await llm_router.generate(prompt=prompt, max_tokens=2000)
                        return json.dumps({"success": True, "result": str(response)}, ensure_ascii=False)
                    except Exception as llm_err:
                        return json.dumps({"error": f"LLM error: {llm_err!s}"})

                # Fallback: gmail_send via automation_tools_async (handles "me" → real email resolve)
                if tool_name == "gmail_send":
                    try:
                        if MEETFLOW_PATH not in sys.path:
                            sys.path.insert(0, MEETFLOW_PATH)
                        from automation_tools_async import send_email
                        result = await send_email(
                            to=args.get("to", "me"),
                            subject=args.get("subject", "Tessbrain — результат задачи"),
                            body=args.get("body", ""),
                            cc=args.get("cc", ""),
                        )
                        logger.info(f"[AgentAutoService] gmail_send fallback result: {result[:200]}")
                        return result
                    except Exception as mail_err:
                        logger.error(f"[AgentAutoService] gmail_send fallback error: {mail_err}")
                        return json.dumps({"error": f"Email send error: {mail_err!s}"})

                # Fallback: send_telegram via automation_tools_async
                if tool_name in ("send_telegram_message", "send_telegram"):
                    try:
                        if MEETFLOW_PATH not in sys.path:
                            sys.path.insert(0, MEETFLOW_PATH)
                        from automation_tools_async import send_telegram
                        result = await send_telegram(message=args.get("message", ""))
                        logger.info(f"[AgentAutoService] send_telegram fallback result: {result[:200]}")
                        return result
                    except Exception as tg_err:
                        logger.error(f"[AgentAutoService] send_telegram fallback error: {tg_err}")
                        return json.dumps({"error": f"Telegram send error: {tg_err!s}"})

                return json.dumps({"error": f"Tool '{tool_name}' not found."})
            except Exception as e:
                logger.error(f"[AgentAutoService] Tool exec error ({tool_name}): {e}")
                return json.dumps({"error": str(e)})

        return tools_catalog, execute_tool


# Singleton
_service: Optional[AgentAutomationService] = None


def get_agent_automation_service() -> AgentAutomationService:
    global _service
    if _service is None:
        _service = AgentAutomationService()
    return _service
