# -*- coding: utf-8 -*-
"""
Ollama FunctionGemma router (local) + ReAct loop.

Usage:
    - Install Ollama and run it
    - `ollama pull functiongemma`
    - This module calls Ollama HTTP API to get a strict JSON "tool call".

Architecture (Гибридная / Agentic Workflow):
    - FunctionGemma = Диспетчер (Router/Orchestrator) — дешёвая, быстрая, локальная
    - Для анализа транскрипций / генерации текста → вызываем "большую" LLM
    - ReAct loop: route → execute → observe → route → ... → DONE
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class LocalRouteResult:
    tool: str
    args: Dict[str, Any]
    reason: str = ""
    raw_text: str = ""
    prompt_eval_count: int = 0
    eval_count: int = 0


@dataclass
class ReActStep:
    """One step in the ReAct loop."""
    thought: str = ""
    tool: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    observation: str = ""
    is_final: bool = False


@dataclass
class ReActResult:
    """Full ReAct execution result."""
    steps: List[ReActStep] = field(default_factory=list)
    final_answer: str = ""
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    success: bool = True
    error: str = ""


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Try to parse JSON from raw model output."""
    if not text:
        return None
    text = text.strip()
    # Direct JSON
    try:
        return json.loads(text)
    except Exception:
        logger.debug("suppressed exception", exc_info=True)

    # Find first {...} block
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


# === Extended Tools Catalog (English descriptions for FunctionGemma compatibility) ===

TESS_TOOLS_CATALOG = [
    # === Database Direct Access (Supabase) ===
    {
        "name": "get_all_tasks",
        "description": "Get all tasks from database and knowledge graph. Use for: show tasks, list tasks, my tasks, what tasks, zadachi.",
        "args_schema": {
            "status_filter": "string (optional) - filter by status: backlog, in_progress, done",
            "assignee_filter": "string (optional) - filter by assignee name",
            "limit": "int (optional, default 50) - maximum number of tasks",
        },
    },
    {
        "name": "get_tasks_from_database",
        "description": "Get tasks directly from Supabase database. Use for: database tasks, DB tasks, real tasks.",
        "args_schema": {
            "status": "string (optional) - filter by status: backlog, in_progress, done",
            "limit": "int (optional, default 20) - maximum tasks",
        },
    },
    {
        "name": "get_user_profile",
        "description": "Get user profile with performance analytics. Use for: user profile, employee info, performance, how is user doing.",
        "args_schema": {
            "username": "string - username to look up",
        },
    },
    {
        "name": "get_team_overview",
        "description": "Get team overview: all members, their status and tasks. Use for: team info, who is in team, team status.",
        "args_schema": {},
    },
    {
        "name": "search_in_database",
        "description": "Search in database tables (tasks, user_profiles, sprints). Use for: search, find, lookup.",
        "args_schema": {
            "query": "string - search query",
            "table": "string (optional, default tasks) - table to search: tasks, user_profiles, sprints",
        },
    },
    {
        "name": "get_sprints_info",
        "description": "Get sprints information. Use for: sprints, sprint status, current sprint.",
        "args_schema": {
            "status": "string (optional) - filter by status: active, completed, planned",
        },
    },
    # === Knowledge Graph Access ===
    {
        "name": "get_person_info",
        "description": "Get information about a person from knowledge graph. Use for: info about X, who is X, what about X.",
        "args_schema": {
            "person_name": "string - name of the person",
        },
    },
    {
        "name": "get_project_status",
        "description": "Get project status from knowledge base. Use for: project X status, how is project X.",
        "args_schema": {
            "project_name": "string - name of the project",
        },
    },
    {
        "name": "get_recent_meetings",
        "description": "Get last meetings from MeetFlow DB (Supabase) with graph fallback. Use for: recent meetings, last meetings, meeting list. Russian keywords: poslednie vstrechi, 3 poslednie vstrechi.",
        "args_schema": {
            "limit": "int (optional, default 10) - maximum meetings",
        },
    },
    {
        "name": "get_meeting_context",
        "description": "Get full meeting context (participants, decisions, tasks). Use for meeting details.",
        "args_schema": {
            "meeting_id": "string (optional) - meeting ID",
            "title": "string (optional) - meeting title",
        },
    },
    {
        "name": "search_knowledge",
        "description": "Semantic search in knowledge base. Use for complex questions about any topic.",
        "args_schema": {
            "query": "string - search query",
        },
    },
    {
        "name": "get_recent_decisions",
        "description": "Get recent decisions / what was decided (решения). Use when user asks about decisions, outcomes, resolutions. NOT for meeting list.",
        "args_schema": {
            "limit": "int (optional, default 20) - maximum decisions",
        },
    },
    {
        "name": "get_all_people",
        "description": "Get list of all people in the system. Use for: who is in team, list employees.",
        "args_schema": {
            "limit": "int (optional, default 50) - maximum results",
        },
    },
    # === MeetFlow Integration ===
    {
        "name": "get_meetflow_meetings",
        "description": "Get meetings from MeetFlow for last N days. Use for: meetings, recent meetings, what meetings.",
        "args_schema": {
            "days_back": "int (optional, default 7) - days to look back",
        },
    },
    {
        "name": "search_meetflow_meetings",
        "description": "Search meetings in MeetFlow by title or content. Use for: find meeting, search meeting.",
        "args_schema": {
            "search_term": "string - search query",
        },
    },
    {
        "name": "get_meeting_details_meetflow",
        "description": "Get meeting details including transcript from MeetFlow. Use for: meeting details, what was discussed.",
        "args_schema": {
            "meeting_id": "string - meeting ID or title",
        },
    },
    {
        "name": "get_meeting_tasks_meetflow",
        "description": "Get tasks extracted from a meeting. Use for: meeting tasks, what tasks from meeting.",
        "args_schema": {
            "meeting_id": "string - meeting ID",
        },
    },
    {
        "name": "get_yougile_tasks",
        "description": "Get tasks from YouGile task manager. Use for: yougile tasks, board tasks.",
        "args_schema": {
            "board_id": "string (optional) - board ID",
            "status": "string (optional) - status filter",
        },
    },
    {
        "name": "send_telegram_message",
        "description": "Send a message to Telegram. Use for: send to telegram, TG, отправь в телеграм, пошли в тг.",
        "args_schema": {
            "message": "string - the message text to send",
            "chat_id": "string (optional) - Telegram chat ID or @username",
        },
    },
    {
        "name": "get_yougile_boards",
        "description": "Get list of YouGile boards. Use for: yougile boards, task boards.",
        "args_schema": {},
    },
    {
        "name": "create_yougile_task",
        "description": "Create task in YouGile. Use for: create yougile task, add to yougile.",
        "args_schema": {
            "title": "string - task title",
            "column_id": "string (optional) - column ID",
            "description": "string (optional) - description",
            "assignee": "string (optional) - assignee",
        },
    },
    {
        "name": "list_calendar_events",
        "description": "Get events from Google Calendar. Use for: calendar, schedule, events.",
        "args_schema": {
            "days_ahead": "int (optional, default 7) - days ahead to look",
        },
    },
    # === Actions ===
    {
        "name": "analyze_with_llm",
        "description": "Analyze text with big LLM (Gemini/GPT). Use for: analyze, summarize, write, generate.",
        "args_schema": {
            "task": "string - what to do (e.g.: extract key decisions, write letter)",
            "context": "string - context/data for analysis",
        },
    },
    {
        "name": "send_telegram",
        "description": "Send message to Telegram. Use for: send to telegram, notify, message.",
        "args_schema": {
            "message": "string - message text",
            "chat_id": "string (optional) - chat ID",
        },
    },
    {
        "name": "create_task",
        "description": "Create a new task in knowledge graph. Use for: create task, add task, new task.",
        "args_schema": {
            "title": "string - task title",
            "description": "string (optional) - description",
            "assignee": "string (optional) - assignee",
            "deadline": "string (optional) - deadline",
        },
    },
    # === Email / Gmail ===
    {
        "name": "gmail_send",
        "description": "Send email via Gmail. Use for: send email, email someone, отправь на почту, отправь по email, напиши письмо.",
        "args_schema": {
            "to": "string - recipient email address",
            "subject": "string - email subject",
            "body": "string - email body text",
        },
    },
    {
        "name": "gmail_read",
        "description": "Read recent emails from Gmail inbox. Use for: check email, read mail, прочитай почту, что на почте, новые письма.",
        "args_schema": {
            "max_results": "number (optional) - max emails to return, default 10",
        },
    },
    {
        "name": "gmail_search",
        "description": "Search emails in Gmail. Use for: find email, search mail, найди письмо, поищи на почте.",
        "args_schema": {
            "query": "string - search query (subject, sender, keywords)",
            "max_results": "number (optional) - max results, default 10",
        },
    },
    {
        "name": "done",
        "description": "Complete execution. Use when task is fully completed.",
        "args_schema": {
            "answer": "string - final answer to user",
        },
    },
    # === Automations Management ===
    {
        "name": "create_scheduled_automation",
        "description": "Create a scheduled/recurring automation. Use when user says: every morning digest, daily report at 9, remind me weekly, каждый день дайджест, каждое утро отчёт, напоминай каждую неделю, создай автоматизацию.",
        "args_schema": {
            "name": "string - automation name (e.g. 'Утренний дайджест', 'Weekly report')",
            "action_type": "string - one of: send_telegram, send_email, send_slack, llm_process, llm_extract_and_send, create_yougile_task, custom_agent_task",
            "action_data": "object - depends on action_type. For send_telegram: {message, chat_id}. For llm_process: {prompt, source_type, output_type, output_target}. For send_email: {to, subject, body}",
            "schedule_description": "string - human-readable schedule: 'каждый день в 9:00', 'every monday at 10am', 'каждые 2 часа', 'через 30 минут', 'once tomorrow at 9'",
            "description": "string (optional) - what this automation does",
        },
    },
    {
        "name": "list_scheduled_automations",
        "description": "List user's automations. Use for: my automations, list automations, show scheduled tasks, мои автоматизации, какие автоматизации настроены.",
        "args_schema": {
            "status": "string (optional) - filter: active, paused, completed, failed",
        },
    },
    {
        "name": "cancel_scheduled_automation",
        "description": "Cancel/delete an automation by name or ID. Use for: cancel automation, remove automation, отмени автоматизацию, удали автоматизацию.",
        "args_schema": {
            "automation_id": "string (optional) - automation ID",
            "automation_name": "string (optional) - automation name to search and cancel",
        },
    },
]


def get_tools_catalog(allowed_names: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Get tools catalog, optionally filtered by allowed names."""
    if allowed_names:
        allowed = set(allowed_names)
        return [t for t in TESS_TOOLS_CATALOG if t["name"] in allowed]
    return TESS_TOOLS_CATALOG.copy()


def _convert_to_ollama_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert our tools catalog format to Ollama native tools format."""
    ollama_tools = []
    for t in tools:
        # Build parameters schema from args_schema
        properties = {}
        required = []

        args_schema = t.get("args_schema", {})
        for arg_name, arg_desc in args_schema.items():
            # Parse type from description
            arg_type = "string"
            desc_lower = arg_desc.lower()
            is_optional = "optional" in desc_lower or "default" in desc_lower

            if "int" in desc_lower:
                arg_type = "integer"
            elif "bool" in desc_lower:
                arg_type = "boolean"

            properties[arg_name] = {
                "type": arg_type,
                "description": arg_desc,
            }

            if not is_optional:
                required.append(arg_name)

        ollama_tools.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                }
            }
        })

    return ollama_tools


async def route_with_functiongemma(
    user_message: str,
    tools: Optional[List[Dict[str, Any]]] = None,
    allowed_tool_names: Optional[List[str]] = None,
    ollama_model: str = "functiongemma",
    ollama_base_url: str = "http://localhost:11434",
    timeout_seconds: int = 60,
    conversation_history: Optional[List[Dict[str, str]]] = None,
    use_skill_router: bool = True,
) -> LocalRouteResult:
    """
    Ask local FunctionGemma (via Ollama) to select a tool + args.
    Uses Ollama native Tools API for reliable function calling.

    With use_skill_router=True (default), SkillRouter first narrows down tools
    from 23+ to 3-5 based on message keywords, dramatically improving accuracy.

    Returns LocalRouteResult with token-ish counters from Ollama:
      prompt_eval_count / eval_count
    """
    # --- Step 0: Skill Router narrows tools ---
    skill_name = None
    if use_skill_router and not conversation_history:
        # Only use SkillRouter for initial routing, not for ReAct follow-ups
        try:
            from backend.core.auto.skill_router import SkillRouter
            skill_router = SkillRouter()
            skill_name, skill_tools = skill_router.route(user_message, allowed_tool_names)

            # Filter tools to skill-relevant ones.
            # `tools` param may already contain integration tools (from IntegrationRegistry in agents.py).
            # We filter from BOTH the static catalog AND any extra tools passed in.
            if skill_tools:
                skill_set = set(skill_tools)

                # 1. Get tools from static catalog
                static_tools = get_tools_catalog(skill_tools)

                # 2. Keep integration tools that are already in `tools` param and match skill_tools
                extra_tools = []
                if tools:
                    static_names = {t["name"] for t in static_tools}
                    for t in tools:
                        name = t.get("name", "")
                        if name in skill_set and name not in static_names:
                            extra_tools.append(t)

                tools = static_tools + extra_tools

                if extra_tools:
                    logger.info(f"[FunctionGemma] Integration tools kept from SkillRouter: {[t['name'] for t in extra_tools]}")
                logger.info(
                    f"[FunctionGemma] SkillRouter selected skill='{skill_name}', "
                    f"narrowed to {len(tools)} tools: {[t['name'] for t in tools]}"
                )
        except Exception as e:
            logger.warning(f"[FunctionGemma] SkillRouter error, using full catalog: {e}")
            # Fallback: use full catalog
            if tools is None:
                tools = get_tools_catalog(allowed_tool_names)

    if tools is None:
        tools = get_tools_catalog(allowed_tool_names)

    # --- Hard guardrails (deterministic) ---
    # Only apply on FIRST step (no conversation_history).
    # On follow-up ReAct steps, FunctionGemma must decide freely based on observation.
    msg_l = (user_message or "").lower()
    is_follow_up = bool(conversation_history)
    logger.info(f"[FunctionGemma] Hard rules check: msg_l={msg_l[:50] if msg_l else 'empty'}, skill={skill_name}, follow_up={is_follow_up}")
    if msg_l and not is_follow_up:
        # Extract first integer (e.g., "3 последние ...")
        m_num = re.search(r"\b(\d{1,3})\b", msg_l)
        n = int(m_num.group(1)) if m_num else None

        # Check for meetings/tasks keywords (including transliterated versions for encoding issues)
        meeting_keywords = ["встреч", "meeting", "vstrech", "vstrechi", "митинг"]
        task_keywords = ["задач", "task", "zadach", "задани"]
        asks_meetings = any(kw in msg_l for kw in meeting_keywords)
        asks_tasks = any(kw in msg_l for kw in task_keywords)

        available_tool_names = set(t.get("name") for t in tools if t.get("name"))
        logger.info(f"[FunctionGemma] asks_meetings={asks_meetings}, asks_tasks={asks_tasks}, available={available_tool_names}")

        if asks_meetings and not asks_tasks:
            if "get_recent_meetings" in available_tool_names:
                return LocalRouteResult(
                    tool="get_recent_meetings",
                    args={"limit": n} if n else {},
                    reason=f"Hard rule: user asked about meetings (skill={skill_name})",
                    raw_text="",
                )
            if "get_meetflow_meetings" in available_tool_names:
                return LocalRouteResult(
                    tool="get_meetflow_meetings",
                    args={"days_back": 7},
                    reason=f"Hard rule: user asked about meetings/MeetFlow (skill={skill_name})",
                    raw_text="",
                )

        if asks_tasks and not asks_meetings:
            if "get_all_tasks" in available_tool_names:
                return LocalRouteResult(
                    tool="get_all_tasks",
                    args={"limit": n} if n else {},
                    reason=f"Hard rule: user asked about tasks (skill={skill_name})",
                    raw_text="",
                )

        # Combined request: meetings + tasks (e.g., "найди встречу, достань задачу, отправь")
        # Start with meetings — subsequent steps will extract tasks from them
        if asks_meetings and asks_tasks:
            if "get_recent_meetings" in available_tool_names:
                return LocalRouteResult(
                    tool="get_recent_meetings",
                    args={"limit": n or 1},
                    reason=f"Hard rule: combined meetings+tasks request, starting with meetings (skill={skill_name})",
                    raw_text="",
                )
            if "get_meetflow_meetings" in available_tool_names:
                return LocalRouteResult(
                    tool="get_meetflow_meetings",
                    args={"days_back": 7},
                    reason=f"Hard rule: combined meetings+tasks request via MeetFlow (skill={skill_name})",
                    raw_text="",
                )

        # Hard rules for send actions — only fire when the request is a PURE send
        # (not "find X then send to Y" which needs ReAct multi-step).
        # If the message also references meetings/tasks/data to fetch first,
        # let ReAct handle it step-by-step.
        needs_data_first = asks_meetings or asks_tasks or any(
            kw in msg_l for kw in ["найди", "найти", "достань", "достать", "извлеки",
                                    "get me", "find", "extract", "последн", "recent"]
        )

        # Hard rule: explicit send to Telegram (only pure sends)
        tg_send_keywords = ["отправь в тг", "отправь в телег", "пошли в тг", "пошли в телег",
                            "скинь в тг", "скинь в телег", "кинь в тг", "кинь в телег",
                            "send to telegram", "send to tg", "forward to tg"]
        if not needs_data_first and any(kw in msg_l for kw in tg_send_keywords):
            for tool_name in ("send_telegram_message", "send_telegram"):
                if tool_name in available_tool_names:
                    return LocalRouteResult(
                        tool=tool_name,
                        args={"message": user_message, "chat_id": ""},
                        reason="Hard rule: user asked to send to Telegram",
                        raw_text="",
                    )

        # Hard rule: explicit send email (only pure sends)
        email_send_keywords = [
            "отправь на почту", "отправь мне на почту", "пошли на почту", "пошли мне на почту",
            "скинь на почту", "скинь мне на почту", "кинь на почту", "кинь мне на почту",
            "отправь по email", "отправь по имейл", "отправь по емейл",
            "напиши письмо", "send email", "send mail", "email me", "send by email",
        ]
        if not needs_data_first and any(kw in msg_l for kw in email_send_keywords):
            if "gmail_send" in available_tool_names:
                return LocalRouteResult(
                    tool="gmail_send",
                    args={"to": "", "subject": "", "body": user_message},
                    reason="Hard rule: user asked to send email",
                    raw_text="",
                )

    # Convert to Ollama native tools format
    ollama_tools = _convert_to_ollama_tools(tools)

    # Build messages
    messages = []

    # System instruction to force tool use (kept English-only to avoid encoding issues)
    messages.append(
        {
            "role": "system",
            "content": (
                "You are TessRouter. Your job is to select and call the SINGLE best tool from the provided tools.\n"
                "Always respond using a tool call when any tool can help.\n"
                "Extract simple arguments from the user request (e.g. numbers like 3 -> limit=3).\n"
                "Examples:\n"
                "- 'kakie byli 3 poslednie vstrechi?' -> call get_recent_meetings with limit=3\n"
                "- 'pokazhi poslednie 3 zadachi' -> call get_all_tasks with limit=3\n"
                "If no tool fits, call done with a short explanation."
            ),
        }
    )

    # Add conversation history if provided (for ReAct loop)
    if conversation_history:
        # Override system prompt with ReAct-aware instructions
        messages[0] = {
            "role": "system",
            "content": (
                "You are TessRouter executing a multi-step task.\n"
                "Previous steps and their results are in the conversation history.\n"
                "Select the NEXT tool to call based on the task progress.\n"
                "If the task is complete, call 'done' with answer summarizing results.\n"
                "Do NOT repeat tools you already called unless needed with different args.\n"
                "Examples of next steps:\n"
                "- After getting meetings data -> call gmail_send to email it\n"
                "- After getting task data -> call send_telegram_message to forward it\n"
                "- After all steps done -> call done with final answer\n"
            ),
        }
        messages.extend(conversation_history)

    messages.append({"role": "user", "content": user_message})

    payload = {
        "model": ollama_model,
        "stream": False,
        "messages": messages,
        "tools": ollama_tools,  # Use native Ollama tools API
    }

    url = f"{ollama_base_url.rstrip('/')}/api/chat"
    async with httpx.AsyncClient(timeout=float(timeout_seconds)) as client:
        try:
            r = await client.post(url, json=payload)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            logger.error(f"Ollama HTTP error: {e}")
            return LocalRouteResult(
                tool="none",
                args={},
                reason=f"Ollama connection error: {e}",
                raw_text="",
            )

    # Parse response - check for tool_calls in message
    message = data.get("message", {})
    tool_calls = message.get("tool_calls", [])
    content = message.get("content", "")

    if tool_calls:
        # Use the first tool call
        first_call = tool_calls[0]
        func_info = first_call.get("function", {})
        tool_name = func_info.get("name", "none")
        tool_args = func_info.get("arguments", {})

        # Ensure args is a dict
        if isinstance(tool_args, str):
            try:
                tool_args = json.loads(tool_args)
            except Exception:
                tool_args = {}

        return LocalRouteResult(
            tool=tool_name,
            args=tool_args if isinstance(tool_args, dict) else {},
            reason="FunctionGemma selected tool via native API",
            raw_text=json.dumps(tool_calls, ensure_ascii=False),
            prompt_eval_count=int(data.get("prompt_eval_count") or 0),
            eval_count=int(data.get("eval_count") or 0),
        )

    # Fallback: try to parse JSON from content (old method)
    if content:
        parsed = _extract_json(content)
        if parsed:
            return LocalRouteResult(
                tool=str(parsed.get("tool") or "none"),
                args=parsed.get("args") if isinstance(parsed.get("args"), dict) else {},
                reason=str(parsed.get("reason") or ""),
                raw_text=content,
                prompt_eval_count=int(data.get("prompt_eval_count") or 0),
                eval_count=int(data.get("eval_count") or 0),
            )

    # No tool call detected
    return LocalRouteResult(
        tool="none",
        args={},
        reason=content or "No tool selected by FunctionGemma",
        raw_text=content,
        prompt_eval_count=int(data.get("prompt_eval_count") or 0),
        eval_count=int(data.get("eval_count") or 0),
    )


async def run_react_loop(
    user_message: str,
    tool_executor,  # async callable: (tool_name, args) -> str
    allowed_tool_names: Optional[List[str]] = None,
    max_steps: int = 10,
    ollama_model: str = "functiongemma",
    ollama_base_url: str = "http://localhost:11434",
    tools: Optional[List[Dict[str, Any]]] = None,
) -> ReActResult:
    """
    Run ReAct (Reason + Act) loop with FunctionGemma as orchestrator.

    Args:
        user_message: Initial user request
        tool_executor: Async function that executes tools: (tool_name, args) -> observation_str
        allowed_tool_names: Optional list of allowed tool names
        max_steps: Maximum number of steps before forcing completion
        ollama_model: Ollama model name
        ollama_base_url: Ollama API base URL
        tools: Pre-built tools catalog (includes integration tools). If None, built from allowed_tool_names.

    Returns:
        ReActResult with all steps and final answer
    """
    result = ReActResult()
    conversation_history: List[Dict[str, str]] = []

    # Initial message
    current_message = user_message

    # === Pre-run SkillRouter ONCE before the loop ===
    # This creates a focused tool set for the ENTIRE multi-step task.
    # All subsequent steps use this same tool set (no tool loss between steps).
    skill_filtered_tools = None
    try:
        from backend.core.auto.skill_router import SkillRouter
        skill_router = SkillRouter()
        _skill_name, skill_tool_names = skill_router.route(user_message, allowed_tool_names)
        if skill_tool_names:
            skill_set = set(skill_tool_names)
            static_tools = get_tools_catalog(skill_tool_names)
            extra_tools = []
            if tools:
                static_names = {t["name"] for t in static_tools}
                for t in tools:
                    name = t.get("name", "")
                    if name in skill_set and name not in static_names:
                        extra_tools.append(t)
            skill_filtered_tools = static_tools + extra_tools
            logger.info(
                f"[ReAct] Pre-filtered tools via SkillRouter skill='{_skill_name}', "
                f"{len(skill_filtered_tools)} tools: {[t['name'] for t in skill_filtered_tools]}"
            )
    except Exception as e:
        logger.warning(f"[ReAct] SkillRouter pre-filter failed, using full catalog: {e}")

    # Use skill-filtered tools if available, otherwise full catalog
    react_tools = skill_filtered_tools or tools or get_tools_catalog(allowed_tool_names)

    for step_num in range(max_steps):
        try:
            # Get next action from FunctionGemma
            # use_skill_router=False because we already pre-filtered above
            route = await route_with_functiongemma(
                user_message=current_message,
                tools=react_tools,
                allowed_tool_names=allowed_tool_names,
                ollama_model=ollama_model,
                ollama_base_url=ollama_base_url,
                conversation_history=conversation_history,
                use_skill_router=False,
            )

            result.total_input_tokens += route.prompt_eval_count
            result.total_output_tokens += route.eval_count

            step = ReActStep(
                thought=route.reason,
                tool=route.tool,
                args=route.args,
            )

            # Check if done
            if route.tool == "done":
                step.is_final = True
                result.final_answer = str(route.args.get("answer") or route.reason or "Задача выполнена.")
                result.steps.append(step)
                break

            # Check if no tool selected
            if route.tool == "none":
                step.observation = f"Инструмент не выбран. Причина: {route.reason}"
                result.steps.append(step)
                # Try to get final answer
                result.final_answer = route.reason or "Не удалось выполнить запрос с доступными инструментами."
                break

            # Execute tool
            try:
                observation = await tool_executor(route.tool, route.args)
                step.observation = observation
            except Exception as e:
                step.observation = f"Ошибка выполнения {route.tool}: {e!s}"
                logger.error(f"Tool execution error: {e}")

            result.steps.append(step)

            # Update conversation history for next iteration
            conversation_history.append({
                "role": "assistant",
                "content": json.dumps({
                    "tool": route.tool,
                    "args": route.args,
                    "reason": route.reason,
                }, ensure_ascii=False),
            })
            conversation_history.append({
                "role": "user",
                "content": f"Результат выполнения {route.tool}:\n{step.observation}\n\nЧто делать дальше? Если задача выполнена, используй инструмент 'done' с финальным ответом.",
            })

            # Update current message for next iteration.
            # Include original request so FunctionGemma remembers the full task.
            obs_preview = step.observation[:500] if len(step.observation) > 500 else step.observation
            current_message = (
                f"Оригинальный запрос пользователя: {user_message}\n\n"
                f"Результат шага {step_num + 1} ({route.tool}): {obs_preview}\n\n"
                f"Продолжай выполнение оригинального запроса. "
                f"Если всё готово, используй инструмент 'done'."
            )

        except httpx.HTTPError as e:
            result.success = False
            result.error = f"Ошибка связи с Ollama: {e!s}"
            logger.error(f"Ollama HTTP error: {e}")
            break
        except Exception as e:
            result.success = False
            result.error = f"Ошибка ReAct loop: {e!s}"
            logger.exception(f"ReAct loop error: {e}")
            break

    # If we hit max steps without completion
    if not result.final_answer and result.steps:
        last_observations = "\n".join([
            f"- {s.tool}: {s.observation[:200]}..." if len(s.observation) > 200 else f"- {s.tool}: {s.observation}"
            for s in result.steps[-3:]  # Last 3 steps
        ])
        result.final_answer = f"Выполнено {len(result.steps)} шагов. Последние результаты:\n{last_observations}"

    return result


async def _safe_emit(on_step, step: int, total: int, tool: str) -> None:
    """Best-effort уведомление о прогрессе (никогда не ломает петлю)."""
    if on_step is None:
        return
    try:
        res = on_step(step, total, tool)
        if hasattr(res, "__await__"):
            await res
    except Exception:
        logger.debug("progress on_step failed", exc_info=True)


async def run_react_loop_bigllm(
    user_message: str,
    tool_executor,  # async callable: (tool_name, args) -> str
    llm_router,     # LLMRouter with generate(prompt, max_tokens, ...)
    tools: List[Dict[str, Any]],
    max_steps: int = 5,
    model_tier: str = "standard",
    user_email: str = "",
    on_step=None,   # опц. callback(step:int, total:int, tool:str) — прогресс
) -> ReActResult:
    """
    ReAct loop powered by Big LLM (Gemini/GPT) instead of FunctionGemma.

    FunctionGemma (270M) handles step 1 routing well but fails on follow-up steps
    because it can't reason over conversation history + observations.
    Big LLM can do genuine multi-step reasoning: get data → analyze → send.

    Architecture:
        Step 1: FunctionGemma (fast, local) routes the first tool via hard rules/SkillRouter
        Steps 2+: Big LLM analyzes observations and selects next tool
        Final: Big LLM synthesizes a user-friendly answer
    """
    result = ReActResult()
    steps_history = []

    # Step 1: Use FunctionGemma for initial routing (fast, deterministic)
    try:
        from backend.core.auto.skill_router import SkillRouter
        skill_router = SkillRouter()
        _skill_name, skill_tool_names = skill_router.route(user_message, None)

        if skill_tool_names:
            skill_set = set(skill_tool_names)
            initial_tools = get_tools_catalog(skill_tool_names)
            static_names = {t["name"] for t in initial_tools}
            for t in tools:
                name = t.get("name", "")
                if name in skill_set and name not in static_names:
                    initial_tools.append(t)
        else:
            initial_tools = tools

        # FunctionGemma selects step 1 tool
        step1_route = await route_with_functiongemma(
            user_message=user_message,
            tools=initial_tools,
            use_skill_router=False,
        )

        result.total_input_tokens += step1_route.prompt_eval_count
        result.total_output_tokens += step1_route.eval_count

        logger.info(f"[BigLLM ReAct] FunctionGemma step1 result: tool={step1_route.tool}, args={str(step1_route.args)[:200]}, reason={step1_route.reason[:100] if step1_route.reason else 'None'}")

        _reason_l = (step1_route.reason or "").lower()
        _ollama_down = ("connection error" in _reason_l
                        or ("ollama" in _reason_l and "error" in _reason_l))

        if step1_route.tool in ("none", "done") and not _ollama_down:
            result.final_answer = step1_route.reason or "Не удалось определить действие."
            result.steps.append(ReActStep(
                thought=step1_route.reason,
                tool=step1_route.tool,
                args=step1_route.args,
                is_final=True,
            ))
            return result

        if step1_route.tool in ("none", "done"):
            # Ollama недоступна → гибрид НЕ должен зависеть от локальной
            # модели (фикс аудита: раньше возвращал «Ollama connection
            # error» с success=True). Шаг 1 выберет Big LLM в петле ниже.
            logger.info("[BigLLM ReAct] FunctionGemma недоступна — шаг 1 передан Big LLM")
        else:
            # Execute step 1
            try:
                observation = await tool_executor(step1_route.tool, step1_route.args)
            except Exception as e:
                observation = f"Ошибка: {e!s}"

            step1 = ReActStep(
                thought=step1_route.reason,
                tool=step1_route.tool,
                args=step1_route.args,
                observation=observation,
            )
            result.steps.append(step1)
            steps_history.append({"tool": step1_route.tool, "args": step1_route.args, "result": observation[:1000]})

            logger.info(f"[BigLLM ReAct] Step 1: {step1_route.tool} → observation len={len(observation)}")
            await _safe_emit(on_step, 1, max_steps, step1_route.tool)

    except Exception as e:
        logger.error(f"[BigLLM ReAct] Step 1 failed: {e}")
        result.success = False
        result.error = str(e)
        return result

    # Steps 2+: Big LLM orchestration
    tools_desc = "\n".join([
        f"- {t['name']}: {t.get('description', '')}"
        for t in (initial_tools if skill_tool_names else tools)
    ])

    last_tool_used = None
    repeat_count = 0

    # Если шаг 1 не исполнен (FunctionGemma недоступна) — Big LLM получает
    # полный бюджет шагов и выбирает первый инструмент сам.
    _start = 1 if steps_history else 0
    for step_num in range(_start, max_steps):
        await _safe_emit(on_step, step_num + 1, max_steps, "думаю…")
        try:
            history_text = "\n".join([
                f"Шаг {i+1}: вызвал {s['tool']}({json.dumps(s['args'], ensure_ascii=False)[:200]})\nРезультат: {s['result'][:800]}"
                for i, s in enumerate(steps_history)
            ]) or "— шагов ещё не было, выбери ПЕРВЫЙ инструмент"

            # Detect send keywords in user message
            msg_lower = user_message.lower()
            wants_email = any(kw in msg_lower for kw in ["почт", "email", "mail", "письм"])
            wants_tg = any(kw in msg_lower for kw in ["телег", "тг", "tg", "telegram"])
            has_data = len(steps_history) >= 1 and len(steps_history[-1].get("result", "")) > 50

            # Build already_called list (from OpenClaw)
            already_called = ", ".join(set(s['tool'] for s in steps_history)) if steps_history else ""

            # Build repeat_warning (from OpenClaw)
            repeat_warning = ""
            if steps_history:
                last_tool = steps_history[-1]['tool']
                same_count = sum(1 for s in steps_history if s['tool'] == last_tool)
                if same_count >= 2:
                    repeat_warning = f"\n⚠️ ВНИМАНИЕ: Инструмент '{last_tool}' уже вызывался {same_count} раз. ЗАПРЕЩЕНО вызывать его снова! Используй данные из результатов выше и переходи к СЛЕДУЮЩЕМУ шагу.\n"

            # Build send hint — but only if we have REAL data (not just meeting IDs)
            send_hint = ""
            email_target = user_email if user_email else "me"
            # Check if we already have substantive data (tasks, details) vs just meeting list
            last_tool_was_data_fetch = steps_history and steps_history[-1]['tool'] in (
                'get_recent_meetings', 'get_meetflow_meetings', 'search_meetflow_meetings'
            )
            has_substantive_data = has_data and not last_tool_was_data_fetch

            if last_tool_was_data_fetch and (wants_email or wants_tg):
                # We only have meeting list — need to get details first
                send_hint = '\n\nHINT: У тебя пока только СПИСОК встреч (ID). Чтобы отправить саммари, СНАЧАЛА вызови get_meeting_tasks_meetflow(meeting_id="<id>") или get_meeting_details_meetflow(meeting_id="<id>") для получения задач/деталей. НЕ отправляй raw JSON с ID!'
            elif wants_email and has_substantive_data:
                send_hint = f'\n\nHINT: Пользователь просил отправить на почту. Детальные данные УЖЕ ПОЛУЧЕНЫ. Следующий шаг — gmail_send(to="{email_target}", subject="Саммари встречи", body="<КРАТКОЕ саммари из предыдущих шагов, max 500 символов>").'
            elif wants_tg and has_substantive_data:
                send_hint = '\n\nHINT: Пользователь просил отправить в телеграм. Детальные данные УЖЕ ПОЛУЧЕНЫ. Следующий шаг — send_telegram_message(message="<КРАТКОЕ саммари из предыдущих шагов>").'

            # Check if last step returned an error (new: smart error detection)
            error_hint = ""
            if steps_history:
                last_result = steps_history[-1].get("result", "")
                if last_result.startswith("[ERROR]") or '"error"' in last_result or '"success": false' in last_result or '"success":false' in last_result:
                    failed_tool = steps_history[-1]['tool']
                    error_hint = f'\n\n⚠️ ПРЕДЫДУЩИЙ ШАГ ВЕРНУЛ ОШИБКУ: {last_result[:300]}.\nИнструмент "{failed_tool}" НЕ РАБОТАЕТ — НЕ повторяй его! Попробуй альтернативный способ или верни done с объяснением.'

            orchestrator_prompt = f"""Ты — AI-агент Tessbrain, выполняющий задачи пользователя шаг за шагом.

ЗАДАЧА ПОЛЬЗОВАТЕЛЯ: "{user_message}"

ВЫПОЛНЕННЫЕ ШАГИ:
{history_text}
{repeat_warning}
ДОСТУПНЫЕ ИНСТРУМЕНТЫ:
{tools_desc}

WORKFLOW ДЛЯ ТИПИЧНЫХ ЗАДАЧ:
1. "найди встречу" → get_recent_meetings() → получишь список встреч с id
2. "достань задачи/детали" → get_meeting_details_meetflow(meeting_id="<id из шага 1>") или get_meeting_tasks_meetflow(meeting_id="...")
3. "отправь на почту" → gmail_send(to="{email_target}", subject="...", body="<КРАТКОЕ саммари из шага 2, max 500 символов>")
4. "отправь в телеграм" → send_telegram_message(message="<данные>")

КРИТИЧЕСКИЕ ПРАВИЛА:
1. НИКОГДА не вызывай один и тот же инструмент дважды с теми же аргументами — используй РЕЗУЛЬТАТ предыдущего вызова.
2. НЕ отправляй raw JSON на почту/в телеграм! Сначала ПОЛУЧИ детальные данные (задачи, решения), потом составь КРАТКОЕ саммари и отправь.
3. Если get_recent_meetings вернул список встреч — ВОЗЬМИ meeting_id и вызови get_meeting_tasks_meetflow или get_meeting_details_meetflow для получения задач.
4. body в gmail_send должен быть ЧЕЛОВЕКО-ЧИТАЕМЫМ КРАТКИМ саммари (max 500 символов). НЕ копируй JSON, НЕ копируй весь транскрипт.
5. Если инструмент вернул ошибку (error, success=false) — НЕ вызывай его повторно. Попробуй альтернативу или верни done с описанием проблемы.
6. НИКОГДА не вызывай analyze_with_llm для отправки. analyze_with_llm только для анализа.
7. reason должен быть коротким (1 предложение).
8. Отвечай СТРОГО JSON — одной строкой, БЕЗ переносов, БЕЗ markdown, БЕЗ ```json

Уже вызывались: [{already_called}]

Что делать дальше? Варианты ответа:
A) Задача ПОЛНОСТЬЮ выполнена: {{"action": "done", "answer": "Подробный ответ пользователю"}}
B) Нужен ещё шаг: {{"action": "call", "tool": "имя", "args": {{"param": "value"}}, "reason": "зачем"}}{send_hint}{error_hint}"""

            # model_tier: "standard" → Gemini 2.0 Flash, "premium" → Gemini 3 Flash
            from backend.core.llm.router import ModelTier
            tier = ModelTier.PREMIUM if model_tier == "premium" else ModelTier.STANDARD
            response_text = await llm_router.generate(
                prompt=orchestrator_prompt,
                max_tokens=8000,
                temperature=0.2,
                model_tier=tier,
            )
            logger.info(f"[BigLLM ReAct] Step {step_num+1} LLM response: {response_text[:300]}")

            # Parse JSON (with robust fallback for truncated responses)
            parsed = _try_parse_react_json(response_text, steps_history, logger, step_num + 1)
            if not parsed:
                _last = steps_history[-1]["result"][:500] if steps_history else "(нет данных)"
                result.final_answer = f"Выполнено шагов: {len(result.steps)}. Последний результат:\n{_last}"
                break
            action = parsed.get("action", "done")

            if action == "done":
                result.final_answer = parsed.get("answer", "Задача выполнена.")
                result.steps.append(ReActStep(
                    thought="Big LLM determined task is complete",
                    tool="done",
                    args={"answer": result.final_answer},
                    is_final=True,
                ))
                break

            # Execute next tool
            next_tool = parsed.get("tool", "none")
            next_args = parsed.get("args", {})
            reason = parsed.get("reason", "")

            if next_tool == "none":
                result.final_answer = reason or "Нет подходящего инструмента."
                break

            # Anti-loop: detect repeated tool calls
            if next_tool == last_tool_used:
                repeat_count += 1
                if repeat_count >= 2:
                    logger.warning(f"[BigLLM ReAct] Loop detected: {next_tool} called {repeat_count+1} times in a row, forcing done")
                    # Check if last observation was an error — if so, don't retry the same tool
                    last_obs = steps_history[-1].get("result", "") if steps_history else ""
                    last_was_error = '"error"' in last_obs or '"success": false' in last_obs or '"success":false' in last_obs

                    if last_was_error:
                        # Tool is broken, stop with explanation
                        result.final_answer = f"Данные получены, но не удалось отправить ({next_tool} вернул ошибку). Результат:\n{steps_history[-2]['result'][:800] if len(steps_history) >= 2 else steps_history[-1]['result'][:800]}"
                        result.success = True
                        break
                    elif wants_tg and has_data:
                        next_tool = "send_telegram_message"
                        last_data = steps_history[-1]["result"][:2500]
                        next_args = {"message": last_data}
                        reason = "Anti-loop: forced send_telegram_message"
                        logger.info("[BigLLM ReAct] Anti-loop override: forcing send_telegram_message")
                    elif wants_email and has_data and user_email:
                        next_tool = "gmail_send"
                        last_data = steps_history[-1]["result"][:2500]
                        next_args = {"to": user_email, "subject": "Данные из Tessbrain", "body": last_data}
                        reason = "Anti-loop: forced gmail_send"
                        logger.info(f"[BigLLM ReAct] Anti-loop override: forcing gmail_send to {user_email}")
                    else:
                        result.final_answer = f"Выполнено {len(result.steps)} шагов. Результат:\n{steps_history[-1]['result'][:500]}"
                        break
            else:
                repeat_count = 0
            last_tool_used = next_tool

            try:
                observation = await tool_executor(next_tool, next_args)
            except Exception as e:
                observation = json.dumps({"error": str(e), "success": False}, ensure_ascii=False)

            # Detect tool errors and mark them clearly for LLM
            obs_is_error = False
            try:
                obs_parsed = json.loads(observation) if observation.startswith("{") else None
                if obs_parsed and (obs_parsed.get("error") or obs_parsed.get("success") is False):
                    obs_is_error = True
                    logger.warning(f"[BigLLM ReAct] Step {step_num+1}: {next_tool} returned ERROR: {observation[:200]}")
            except (json.JSONDecodeError, TypeError):
                pass

            step = ReActStep(
                thought=reason,
                tool=next_tool,
                args=next_args,
                observation=observation,
            )
            result.steps.append(step)
            # Prepend ERROR marker so LLM sees it clearly in history
            result_text = f"[ERROR] {observation[:1000]}" if obs_is_error else observation[:1000]
            steps_history.append({"tool": next_tool, "args": next_args, "result": result_text})

            logger.info(f"[BigLLM ReAct] Step {step_num+1}: {next_tool} → observation len={len(observation)}{' [ERROR]' if obs_is_error else ''}")

        except Exception as e:
            logger.error(f"[BigLLM ReAct] Step {step_num+1} error: {e}")
            result.final_answer = f"Ошибка на шаге {step_num+1}: {e!s}"
            result.success = False
            break

    # Final answer if not set
    if not result.final_answer and result.steps:
        last_obs = result.steps[-1].observation or ""
        result.final_answer = f"Выполнено {len(result.steps)} шагов. Последний результат:\n{last_obs[:500]}"
    if not result.final_answer:
        result.final_answer = "Не удалось выполнить задачу: ни один шаг не дал результата."
        result.success = False

    # НЕ перезаписываем success=True безусловно: ошибки петли должны быть
    # видны вызывающему коду (фикс аудита: «маскирует ошибки»).
    return result


_AGENT_TOOL_ACTIONS = frozenset({"skill_manage", "delegate_to_agent", "execute_code"})


def _normalize_react_action(parsed: dict) -> dict:
    """If LLM emits `{"action": "<tool>", "args": {...}}` for an agent_oc tool
    instead of the canonical `{"action": "call", "tool": "<tool>", "args": ...}`,
    rewrite it to canonical form. The Hermes prompt advertises `skill_manage(...)`
    via skill_runtime.py:89-95; LLMs frequently flatten that into action=<tool>.
    Without this fallback the parser silently drops the step with "Cannot parse JSON".
    """
    action = parsed.get("action")
    if action in _AGENT_TOOL_ACTIONS and "tool" not in parsed:
        parsed = {
            "action": "call",
            "tool": action,
            "args": parsed.get("args") if isinstance(parsed.get("args"), dict) else {},
            "reason": parsed.get("reason", ""),
        }
    return parsed


def _try_parse_react_json(response_text: str, steps_history: list, logger, step_num: int) -> Optional[dict]:
    """
    Try to parse LLM response as JSON action. Handles:
    - Clean JSON
    - JSON wrapped in markdown code fences
    - Truncated JSON (extracts tool + args via regex)
    - LLM flattening agent_oc tools into action=<tool> (see _normalize_react_action)
    Returns parsed dict or None if unparseable.
    """
    # 1. Try direct parse
    try:
        json_match = re.search(r'\{[\s\S]*\}', response_text)
        if json_match:
            parsed = json.loads(json_match.group(0))
            return _normalize_react_action(parsed)
    except json.JSONDecodeError:
        logger.debug("suppressed exception", exc_info=True)

    # 2. Truncated JSON — extract fields via regex.
    # 2a. Check for truncated done: `{"action": "done", "answer": "..."}` where
    # the answer string was cut off mid-content (no closing quote). LLM models
    # routinely truncate around model max_output_tokens. We grab the partial
    # answer so the user sees something useful, not raw step-N JSON observation.
    action_match = re.search(r'"action"\s*:\s*"([^"]+)"', response_text)
    if action_match and action_match.group(1) == "done":
        ans_match = re.search(
            r'"answer"\s*:\s*"((?:[^"\\]|\\.)*)',
            response_text,
            re.DOTALL,
        )
        if ans_match:
            partial = ans_match.group(1)
            try:
                # Unescape JSON-style sequences (\n, \", \uXXXX, ...)
                decoded = bytes(partial, "utf-8").decode("unicode_escape")
            except UnicodeDecodeError:
                decoded = partial
            logger.info(
                f"[OpenClaw ReAct] Step {step_num+1}: recovered truncated done "
                f"answer ({len(decoded)} chars) — output likely hit max_tokens"
            )
            return {
                "action": "done",
                "answer": decoded + "\n\n_(ответ был обрезан моделью; "
                                    "при необходимости попроси продолжить)_",
            }

    tool_match = re.search(r'"tool"\s*:\s*"([^"]+)"', response_text)
    if not tool_match:
        # 2b. LLM may have emitted `"action": "<agent_tool>"` instead of `"tool": ...`.
        # Recover the tool name from action field. args may be incomplete (truncated),
        # we'll pass through what we can extract.
        if action_match and action_match.group(1) in _AGENT_TOOL_ACTIONS:
            recovered_tool = action_match.group(1)
            logger.info(
                f"[OpenClaw ReAct] Step {step_num+1}: recovered agent tool "
                f"'{recovered_tool}' from flattened action= field"
            )
            return {
                "action": "call",
                "tool": recovered_tool,
                "args": {},
                "reason": "",
            }
        logger.warning(f"[OpenClaw ReAct] Step {step_num+1}: Cannot parse JSON, no tool found")
        return None

    tool_name = tool_match.group(1)

    # Extract all string args individually
    args = {}
    # Find the args block start
    args_start = re.search(r'"args"\s*:\s*\{', response_text)
    if args_start:
        args_text = response_text[args_start.end():]
        # Extract key-value pairs: "key": "value" — handle escaped quotes in values
        for m in re.finditer(r'"(\w+)"\s*:\s*"((?:[^"\\]|\\.)*)"', args_text):
            key, val = m.group(1), m.group(2)
            # Stop if we hit reason (it's outside args)
            if key == "reason":
                break
            args[key] = val.replace('\\"', '"').replace('\\n', '\n')

    reason_match = re.search(r'"reason"\s*:\s*"((?:[^"\\]|\\.)*)"', response_text)
    reason = reason_match.group(1) if reason_match else "Recovered from truncated JSON"

    logger.info(f"[OpenClaw ReAct] Repaired truncated JSON: tool={tool_name}, args_keys={list(args.keys())}")

    return {
        "action": "call",
        "tool": tool_name,
        "args": args,
        "reason": reason,
    }


async def run_react_loop_pure_llm(
    user_message: str,
    tool_executor,  # async callable: (tool_name, args) -> str
    llm_router,     # LLMRouter with generate(prompt, max_tokens, ...)
    tools: List[Dict[str, Any]],
    max_steps: int = 8,
    model_tier: str = "standard",
) -> ReActResult:
    """
    Pure LLM ReAct loop — OpenClaw-style.

    ALL steps (including step 1) are handled by Big LLM (Gemini/GPT).
    No FunctionGemma involved. The LLM has full autonomy to plan and execute.

    This gives the agent maximum flexibility for complex multi-step tasks
    since it can reason about the full task from the start.
    """
    result = ReActResult()
    steps_history: list = []

    tools_desc = "\n".join([
        f"- {t['name']}: {t.get('description', '')}. params: {json.dumps(t.get('parameters', {}).get('properties', {}), ensure_ascii=False)[:200]}"
        for t in tools
    ])

    for step_num in range(max_steps):
        try:
            if steps_history:
                history_text = "\n".join([
                    f"Шаг {i+1}: вызвал {s['tool']}({json.dumps(s['args'], ensure_ascii=False)[:200]})\nРезультат: {s['result'][:800]}"
                    for i, s in enumerate(steps_history)
                ])
                already_called = ", ".join(set(s['tool'] for s in steps_history))
            else:
                history_text = "(ещё ни одного шага не сделано)"
                already_called = ""

            repeat_warning = ""
            if steps_history:
                last_tool = steps_history[-1]['tool']
                same_count = sum(1 for s in steps_history if s['tool'] == last_tool)
                if same_count >= 2:
                    repeat_warning = f"\n⚠️ ВНИМАНИЕ: Инструмент '{last_tool}' уже вызывался {same_count} раз. ЗАПРЕЩЕНО вызывать его снова! Используй данные из результатов выше и переходи к СЛЕДУЮЩЕМУ шагу.\n"

            # Check if last step returned an error (from BigLLM improvements)
            oc_error_hint = ""
            if steps_history:
                last_result = steps_history[-1].get("result", "")
                if '"error"' in last_result or '"success": false' in last_result or '"success":false' in last_result:
                    failed_tool = steps_history[-1]['tool']
                    oc_error_hint = f'\n⚠️ ПРЕДЫДУЩИЙ ШАГ ВЕРНУЛ ОШИБКУ: {last_result[:300]}.\nИнструмент "{failed_tool}" НЕ РАБОТАЕТ — НЕ повторяй его! Попробуй альтернативу или верни done.\n'

            prompt = f"""Ты — AI-агент Tessbrain, выполняющий задачи пользователя шаг за шагом.

ЗАДАЧА ПОЛЬЗОВАТЕЛЯ: "{user_message}"

ВЫПОЛНЕННЫЕ ШАГИ:
{history_text}
{repeat_warning}{oc_error_hint}
ДОСТУПНЫЕ ИНСТРУМЕНТЫ:
{tools_desc}

WORKFLOW ДЛЯ ТИПИЧНЫХ ЗАДАЧ:
1. "найди встречу" → get_recent_meetings() → получишь список встреч с id
2. "достань задачи/детали из встречи" → get_meeting_details_meetflow(meeting_id="<id из шага 1>") → получишь задачи, решения, транскрипт
3. "отправь на почту" → gmail_send(to="me", subject="...", body="<данные из шага 2>")
4. "отправь в телеграм" → send_telegram_message(message="<данные>")

КРИТИЧЕСКИЕ ПРАВИЛА:
1. НИКОГДА не вызывай один и тот же инструмент дважды с теми же аргументами — используй РЕЗУЛЬТАТ предыдущего вызова
2. Если get_recent_meetings уже вернул список встреч — ВОЗЬМИ meeting_id из результата и вызови get_meeting_details_meetflow(meeting_id)
3. Если данные уже получены — ИСПОЛЬЗУЙ их для отправки (gmail_send, send_telegram_message), не запрашивай заново
4. to="me" в gmail_send отправит письмо на почту пользователя
5. Отвечай СТРОГО JSON — одной строкой, БЕЗ переносов, БЕЗ markdown, БЕЗ ```json
6. body в gmail_send должен быть КРАТКИМ саммари (max 500 символов), не копируй весь транскрипт
7. reason должен быть коротким (1 предложение)
8. Если инструмент вернул ошибку (error, success=false) — НЕ вызывай его повторно. Попробуй альтернативу или верни done.

Уже вызывались: [{already_called}]

Что делать дальше? Варианты ответа:
A) Задача ПОЛНОСТЬЮ выполнена: {{"action": "done", "answer": "Подробный ответ"}}
B) Нужен ещё шаг: {{"action": "call", "tool": "имя", "args": {{"param": "value"}}, "reason": "зачем"}}"""

            from backend.core.llm.router import ModelTier
            tier = ModelTier.PREMIUM if model_tier == "premium" else ModelTier.STANDARD
            response_text = await llm_router.generate(
                prompt=prompt,
                max_tokens=8000,
                temperature=0.1,
                model_tier=tier,
            )
            response_text = response_text.strip()
            # Remove markdown code fences if present
            if response_text.startswith("```"):
                response_text = re.sub(r'^```(?:json)?\s*', '', response_text)
                response_text = re.sub(r'\s*```\s*$', '', response_text)
                response_text = response_text.strip()

            logger.info(f"[OpenClaw ReAct] Step {step_num+1} LLM response ({len(response_text)} chars): {response_text[:300]}")

            parsed = _try_parse_react_json(response_text, steps_history, logger, step_num)
            if parsed is None:
                if result.steps:
                    result.final_answer = f"Выполнено {len(result.steps)} шагов. Последний результат:\n{steps_history[-1]['result'][:800]}"
                else:
                    result.final_answer = response_text[:500]
                result.success = True
                break

            action = parsed.get("action", "done")

            if action == "done":
                result.final_answer = parsed.get("answer", "Задача выполнена.")
                result.steps.append(ReActStep(
                    thought="Agent determined task is complete",
                    tool="done",
                    args={"answer": result.final_answer},
                    is_final=True,
                ))
                result.success = True
                break

            next_tool = parsed.get("tool", "none")
            next_args = parsed.get("args", {})
            reason = parsed.get("reason", "")

            # Anti-loop: if same tool called 3+ times with same args, force stop
            if steps_history:
                same_calls = sum(1 for s in steps_history if s['tool'] == next_tool and json.dumps(s['args'], sort_keys=True) == json.dumps(next_args, sort_keys=True))
                if same_calls >= 2:
                    logger.warning(f"[OpenClaw ReAct] Loop detected: {next_tool} called {same_calls+1} times with same args, forcing done")
                    last_data = steps_history[-1]['result']
                    result.final_answer = f"Данные получены:\n{last_data[:1000]}"
                    result.steps.append(ReActStep(thought="Loop detected, returning collected data", tool="done", args={}, is_final=True))
                    result.success = True
                    break

            if next_tool == "none":
                result.final_answer = reason or "Нет подходящего инструмента."
                result.success = True
                break

            try:
                observation = await tool_executor(next_tool, next_args)
            except Exception as e:
                observation = json.dumps({"error": str(e), "success": False}, ensure_ascii=False)

            # Detect tool errors and mark them clearly for LLM
            obs_is_error = False
            try:
                obs_parsed = json.loads(observation) if observation.startswith("{") else None
                if obs_parsed and (obs_parsed.get("error") or obs_parsed.get("success") is False):
                    obs_is_error = True
            except (json.JSONDecodeError, TypeError):
                pass

            step = ReActStep(
                thought=reason,
                tool=next_tool,
                args=next_args,
                observation=observation,
            )
            result.steps.append(step)
            result_text = f"[ERROR] {observation[:1500]}" if obs_is_error else observation[:1500]
            steps_history.append({"tool": next_tool, "args": next_args, "result": result_text})

            logger.info(f"[OpenClaw ReAct] Step {step_num+1}: {next_tool} → observation len={len(observation)}{' [ERROR]' if obs_is_error else ''}")

        except Exception as e:
            logger.error(f"[OpenClaw ReAct] Step {step_num+1} error: {e}", exc_info=True)
            if steps_history:
                # Don't lose already collected data on error — try to synthesize answer
                all_data = "\n---\n".join([f"{s['tool']}: {s['result'][:500]}" for s in steps_history])
                result.final_answer = f"Выполнено {len(steps_history)} шагов (ошибка на шаге {step_num+1}: {e!s}).\n\nСобранные данные:\n{all_data[:1500]}"
                result.success = True
            else:
                result.final_answer = f"Ошибка на шаге {step_num+1}: {e!s}"
                result.success = False
            break

    if not result.final_answer and result.steps:
        # Synthesize final answer from all collected data
        all_data = "\n---\n".join([f"{s['tool']}: {s['result'][:500]}" for s in steps_history]) if steps_history else ""
        last_obs = result.steps[-1].observation or ""
        result.final_answer = f"Выполнено {len(result.steps)} шагов.\n\nПоследний результат:\n{last_obs[:800]}"
        result.success = True

    return result
