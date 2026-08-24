"""
TESSENT BRAIN - External Agents Integration API
Прокси для работы с внешними агентными системами (Mark001, MeetFlow Automation, Gemma 3n)
"""
import asyncio
import json
import logging
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx
from litestar import Router, get, post
from litestar.params import Parameter
from pydantic import BaseModel, Field

from backend.core.llm.gemma_client import get_gemma_client
from backend.core.llm.usage_tracker import _usage_context, track_usage
from backend.core.think import TaskSpecificationSystem, create_task_specification_system
from backend.db.supabase_client import get_supabase_client

logger = logging.getLogger(__name__)

# Task Specification System instance
# Аудит-фикс: per-user кэш (раньше один синглтон с захардкоженным user_id).
_task_spec_systems: dict[str, TaskSpecificationSystem] = {}

# === Configuration ===
MARK001_URL = os.getenv("MARK001_API_URL", "http://localhost:8004")
MEETFLOW_URL = os.getenv("MEETFLOW_API_URL", "http://localhost:8005")


# === Helper functions for Tess pretty formatting ===

def _fmt_dt(s: str) -> str:
    """Format datetime string to human-readable format."""
    if not s:
        return ""
    try:
        s2 = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return s


def _looks_garbled(text: str) -> bool:
    """Check if text looks garbled (encoding issues)."""
    if not text:
        return False
    q = text.count("?")
    return q >= 3 or "N?" in text


def _pretty_from_observation(tool_name: str, observation: str) -> Optional[str]:
    """Convert tool observation JSON to human-readable format."""
    if not observation:
        return None
    data_obj = None

    if isinstance(observation, dict):
        data_obj = observation
    elif isinstance(observation, (bytes, bytearray)):
        try:
            observation = observation.decode("utf-8", errors="replace")
        except Exception:
            return None

    if data_obj is None:
        try:
            data_obj = json.loads(observation)
        except Exception:
            try:
                import re
                m = re.search(r"\{[\s\S]*\}", observation)
                if m:
                    data_obj = json.loads(m.group(0))
            except Exception:
                data_obj = None

    if not isinstance(data_obj, dict):
        return None

    # Meetings
    if tool_name in ("get_recent_meetings", "get_meetflow_meetings"):
        meetings = data_obj.get("meetings") or []
        if not meetings:
            return "Встречи не найдены для текущего аккаунта."
        lines = []
        for idx, m in enumerate(meetings, 1):
            title = str(m.get("title") or "").strip()
            if _looks_garbled(title):
                title = "Без названия"
            when = _fmt_dt(str(m.get("created_at") or m.get("date") or ""))
            mid = str(m.get("id") or "")
            suffix = f" — {when}" if when else ""
            if mid:
                lines.append(f"{idx}. **{title}**{suffix} (`{mid[:8]}`)")
            else:
                lines.append(f"{idx}. **{title}**{suffix}")
        return "**Последние встречи:**\n" + "\n".join(lines)

    # Tasks
    if tool_name in ("get_all_tasks", "get_tasks_from_database", "get_yougile_tasks", "get_tasks_from_board"):
        tasks = data_obj.get("tasks") or []
        if not tasks:
            return "Задачи не найдены."
        lines = []
        for idx, t in enumerate(tasks, 1):
            title = str(t.get("title") or t.get("name") or "").strip()
            if _looks_garbled(title):
                title = "Без названия"
            status = str(t.get("status") or "").strip()
            assignee = str(t.get("assignee") or "").strip()
            due = _fmt_dt(str(t.get("due_date") or t.get("deadline") or ""))
            meta_parts = []
            if status:
                meta_parts.append(status)
            if assignee:
                meta_parts.append(f"исп.: {assignee}")
            if due:
                meta_parts.append(f"дедлайн: {due}")
            meta = f" ({', '.join(meta_parts)})" if meta_parts else ""
            lines.append(f"{idx}. **{title}**{meta}")
        return "**Последние задачи:**\n" + "\n".join(lines)

    return None


def _msg_has_any(msg: str, patterns: list[str]) -> bool:
    """Case-insensitive regex/substring check for any pattern."""
    if not msg:
        return False
    text = msg.lower()
    for p in patterns:
        try:
            if re.search(p, text):
                return True
        except re.error:
            if p in text:
                return True
    return False


def _extract_goal_flags(user_message: str) -> dict[str, bool]:
    """Extract high-level user intent flags for post-validation."""
    m = (user_message or "").lower()
    return {
        "wants_email": _msg_has_any(m, [r"\bemail\b", r"\bmail\b", r"почт", r"gmail", r"на e-?mail"]),
        "wants_telegram": _msg_has_any(m, [r"telegram", r"телеграм", r"\bтг\b", r"\btg\b"]),
        "wants_yougile": _msg_has_any(m, [r"yougile", r"юг[аи]л", r"дедлайн", r"deadline"]),
        "wants_meeting_data": _msg_has_any(m, [r"встреч", r"meeting", r"митинг"]),
    }


def _react_goal_check(user_message: str, steps: list[Any], final_text: str) -> tuple[bool, list[str]]:
    """
    Validate if critical goal actions were actually executed in ReAct modes.
    Returns (goal_reached, missing_requirements).
    """
    flags = _extract_goal_flags(user_message)
    tools_used = {str(getattr(s, "tool", "") or "") for s in (steps or [])}
    missing: list[str] = []

    if flags["wants_meeting_data"]:
        meeting_tools = {
            "get_recent_meetings",
            "get_meetflow_meetings",
            "search_meetflow_meetings",
            "get_meeting_details_meetflow",
            "get_meeting_context",
            "get_meeting_tasks_meetflow",
        }
        if not (tools_used & meeting_tools):
            missing.append("meeting_lookup")

    if flags["wants_email"] and "gmail_send" not in tools_used:
        missing.append("email_delivery")

    if flags["wants_telegram"] and not ({"send_telegram_message", "send_telegram"} & tools_used):
        missing.append("telegram_delivery")

    if flags["wants_yougile"] and not ({"create_yougile_task", "create_task"} & tools_used):
        missing.append("yougile_update")

    # If model explicitly says it cannot proceed, treat as incomplete even with success=True.
    if _msg_has_any(final_text or "", [r"не могу", r"уточн", r"не хватает", r"cannot", r"need more"]):
        missing.append("needs_clarification")

    return len(missing) == 0, missing


def _tasks_goal_check(user_message: str, response_text: str) -> tuple[bool, list[str]]:
    """
    Validate goal completion for Tasks mode (text-only response, no step trace).
    Conservative: marks incomplete when requested delivery/update is not confirmed.
    """
    flags = _extract_goal_flags(user_message)
    text = (response_text or "").lower()
    missing: list[str] = []

    incomplete_markers = [
        r"не могу",
        r"уточн",
        r"не хватает прав",
        r"мне нужно",
        r"чтобы я смог",
        r"please specify",
        r"```tool_code",
        r"default_api\.",
        r"contextanalyzer,",
        r"integrationagent,",
    ]
    if _msg_has_any(text, incomplete_markers):
        missing.append("needs_clarification")

    email_done = _msg_has_any(text, [r"письмо отправлено", r"на почту отправ", r"email отправ", r"gmail"]) and not _msg_has_any(
        text, [r"не могу.*отправ", r"не хватает прав"]
    )
    tg_done = _msg_has_any(text, [r"в telegram отправ", r"в телеграм отправ", r"успешно отправ"]) and not _msg_has_any(
        text, [r"не могу.*telegram", r"не могу.*телеграм"]
    )
    yougile_done = _msg_has_any(text, [r"yougile", r"задач[ау].*создан", r"дедлайн.*установ", r"дедлайн.*обнов"])

    if flags["wants_email"] and not email_done:
        missing.append("email_delivery")
    if flags["wants_telegram"] and not tg_done:
        missing.append("telegram_delivery")
    if flags["wants_yougile"] and not yougile_done:
        missing.append("yougile_update")

    return len(missing) == 0, missing

# Paths to agent systems
# Путь к пакету Mark001: env → папка mark001_async РЯДОМ с tessent_brain
# (репозиторий переезжал: захардкоженный абсолютный путь ломал direct-
# режим ошибкой «No module named 'agents'») → старый дефолт.
def _default_mark001_path() -> str:
    env = os.getenv("MARK001_PATH", "")
    if env:
        return env
    try:
        from pathlib import Path as _P
        sibling = _P(__file__).resolve().parents[3].parent / "mark001_async"
        if (sibling / "agents").exists():
            return str(sibling)
    except Exception:
        pass
    # Последний рубеж — пусто, а не путь с чьей-то машины. Раньше здесь
    # стоял абсолютный путь одного компьютера: где угодно ещё он давал
    # не «не настроено», а невнятную ошибку импорта по несуществующему
    # каталогу. Пустая строка честнее — и не тащит чужую файловую
    # систему в открытый репозиторий. Настраивается через MARK001_PATH.
    return ""


MARK001_PATH = _default_mark001_path()
from backend.core.utils.meetflow_path import default_meetflow_path as _default_meetflow_path

MEETFLOW_PATH = _default_meetflow_path()


def _extract_reminder_text(message: str) -> str:
    """Суть напоминания из команды пользователя (без эмодзи).

    «Через 2 часа отправь напоминание о созвоне» → «Напоминание: о созвоне»
    — в Telegram должна прийти СУТЬ, а не инструкция целиком (жалоба юзера:
    в карточке лежала вся команда). Темпоральная часть вырезается в ЛЮБОМ
    месте фразы (не только в начале), затем глагол-обёртка и слово
    «напомни/напоминание»; если после чистки пусто — исходный текст."""
    import re as _re
    t = (message or "").strip()
    t = _re.sub(r"(?i)\bчерез\s+\d+\s*(минут\w*|час\w*|день|дня|дней|секунд\w*)\b", "", t)
    t = _re.sub(r"(?i)\bчерез\s+(полчаса|час)\b", "", t)
    t = _re.sub(r"(?i)\bзавтра(\s+(утром|вечером|днём|днем))?\b", "", t)
    t = _re.sub(r"(?i)\bв\s+\d{1,2}[:.]\d{2}\b", "", t)
    t = _re.sub(r"(?i)^\W*(отправь|пошли|пришли|напиши|сообщи|скинь|кинь|создай|сделай)(\s+мне)?(\s+в\s+(тг|телеграм|телеграмм|telegram))?\s*", "", t.strip())
    t = _re.sub(r"(?i)^\s*напоминани[ея]\s+|^\s*напомни(?:ть)?\b\s*(мне\b\s*)?", "", t)
    t = _re.sub(r"\s{2,}", " ", t).strip(" ,.!:;—–-")
    if len(t) < 3:
        return (message or "").strip()
    return f"Напоминание: {t}"


# === Temporal delay parser ===

def _parse_time_delay(msg_lower: str) -> int:
    """
    Парсит темпоральные модификаторы из сообщения.

    Поддерживает:
        - "через 2 часа" / "через час" / "через полчаса"
        - "через 30 минут" / "через 5 мин"
        - "in 2 hours" / "in 30 minutes" / "in 1 hour"
        - "завтра утром" → ~12 часов

    Returns:
        Задержка в секундах (0 = нет задержки)
    """
    import re

    # Русский: "через N часов/минут"
    m = re.search(r"через\s+(\d+)\s*(час|ч\.?)", msg_lower)
    if m:
        return int(m.group(1)) * 3600

    m = re.search(r"через\s+(\d+)\s*(минут|мин\.?)", msg_lower)
    if m:
        return int(m.group(1)) * 60

    if "через час" in msg_lower:
        return 3600
    if "через полчаса" in msg_lower:
        return 1800

    # English: "in N hours/minutes"
    m = re.search(r"in\s+(\d+)\s*hour", msg_lower)
    if m:
        return int(m.group(1)) * 3600

    m = re.search(r"in\s+(\d+)\s*min", msg_lower)
    if m:
        return int(m.group(1)) * 60

    if "in 1 hour" in msg_lower or "in an hour" in msg_lower:
        return 3600
    if "in 30 min" in msg_lower or "in half an hour" in msg_lower:
        return 1800

    # "завтра утром" → примерно 12-15 часов (условно)
    if "завтра утром" in msg_lower or "tomorrow morning" in msg_lower:
        return 14 * 3600  # ~14 часов
    if "завтра" in msg_lower or "tomorrow" in msg_lower:
        return 24 * 3600

    return 0


# === Human-readable schedule parser ===

def _parse_schedule_description(desc: str) -> dict:
    """
    Парсит человекочитаемое расписание в параметры автоматизации.

    Поддерживает:
        - "каждый день в 9:00" → cron "0 9 * * *"
        - "every monday at 10am" → cron "0 10 * * 1"
        - "каждые 2 часа" → interval 7200
        - "каждые 30 минут" → interval 1800
        - "через 30 минут" → once, execute_at +30min
        - "once tomorrow at 9" → once, execute_at tomorrow 09:00
        - "каждое утро" → cron "0 9 * * *"
        - "каждый вечер" → cron "0 18 * * *"

    Returns:
        dict с ключами: schedule_type, cron_expression?, interval_seconds?, execute_at?
    """
    import re
    from datetime import timedelta
    from datetime import timezone as tz

    d = desc.lower().strip()

    # --- Одноразовые ---

    # "через N часов/минут"
    delay = _parse_time_delay(d)
    if delay > 0:
        execute_at = datetime.now(tz.utc) + timedelta(seconds=delay)
        return {"schedule_type": "once", "execute_at": execute_at.isoformat()}

    # "once tomorrow at 9" / "завтра в 9"
    m = re.search(r"(?:завтра|tomorrow)\s*(?:в|at)?\s*(\d{1,2})(?::(\d{2}))?", d)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        tomorrow = datetime.now(tz.utc) + timedelta(days=1)
        execute_at = tomorrow.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return {"schedule_type": "once", "execute_at": execute_at.isoformat()}

    # "сегодня в 18:00" / "today at 6pm"
    m = re.search(r"(?:сегодня|today)\s*(?:в|at)?\s*(\d{1,2})(?::(\d{2}))?", d)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        execute_at = datetime.now(tz.utc).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if execute_at < datetime.now(tz.utc):
            execute_at += timedelta(days=1)
        return {"schedule_type": "once", "execute_at": execute_at.isoformat()}

    # --- Повторяющиеся (interval) ---

    # "каждые N часов" / "every N hours"
    m = re.search(r"(?:каждые|every)\s+(\d+)\s*(?:час|hour|ч\.?|hr)", d)
    if m:
        return {"schedule_type": "recurring", "interval_seconds": int(m.group(1)) * 3600}

    # "каждые N минут" / "every N minutes"
    m = re.search(r"(?:каждые|every)\s+(\d+)\s*(?:минут|мин|min)", d)
    if m:
        return {"schedule_type": "recurring", "interval_seconds": int(m.group(1)) * 60}

    # "каждый час" / "every hour"
    if re.search(r"(?:каждый час|every hour|ежечасн)", d):
        return {"schedule_type": "recurring", "interval_seconds": 3600}

    # --- Повторяющиеся (cron) ---

    # Дни недели
    day_map = {
        "понедельник": "1", "monday": "1", "пн": "1", "mon": "1",
        "вторник": "2", "tuesday": "2", "вт": "2", "tue": "2",
        "среда": "3", "среду": "3", "wednesday": "3", "ср": "3", "wed": "3",
        "четверг": "4", "thursday": "4", "чт": "4", "thu": "4",
        "пятница": "5", "пятницу": "5", "friday": "5", "пт": "5", "fri": "5",
        "суббота": "6", "субботу": "6", "saturday": "6", "сб": "6", "sat": "6",
        "воскресенье": "0", "sunday": "0", "вс": "0", "sun": "0",
    }

    # "каждый понедельник в 10:00" / "every monday at 10"
    for day_name, day_num in day_map.items():
        if day_name in d:
            m = re.search(r"(?:в|at)\s*(\d{1,2})(?::(\d{2}))?", d)
            hour = int(m.group(1)) if m else 9
            minute = int(m.group(2) or 0) if m else 0
            return {"schedule_type": "recurring", "cron_expression": f"{minute} {hour} * * {day_num}"}

    # "каждый день в 9:00" / "every day at 9"
    m = re.search(r"(?:каждый день|ежедневно|every day|daily)\s*(?:в|at)?\s*(\d{1,2})(?::(\d{2}))?", d)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        return {"schedule_type": "recurring", "cron_expression": f"{minute} {hour} * * *"}

    # "каждое утро" / "every morning"
    if re.search(r"(?:каждое утро|every morning|утренн)", d):
        return {"schedule_type": "recurring", "cron_expression": "0 9 * * *"}

    # "каждый вечер" / "every evening"
    if re.search(r"(?:каждый вечер|every evening|вечерн)", d):
        return {"schedule_type": "recurring", "cron_expression": "0 18 * * *"}

    # "каждую неделю" / "every week" / "еженедельно" / "weekly"
    if re.search(r"(?:каждую неделю|еженедельно|every week|weekly)", d):
        return {"schedule_type": "recurring", "cron_expression": "0 9 * * 1"}  # Mon 9am

    # "каждый день" / "ежедневно" без времени
    if re.search(r"(?:каждый день|ежедневно|every day|daily)", d):
        return {"schedule_type": "recurring", "cron_expression": "0 9 * * *"}

    # Fallback: если ничего не совпало — пытаемся найти хотя бы время
    m = re.search(r"(?:в|at)\s*(\d{1,2})(?::(\d{2}))?", d)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        return {"schedule_type": "recurring", "cron_expression": f"{minute} {hour} * * *"}

    # Совсем ничего — разовая через 1 час
    return {"schedule_type": "once", "execute_at": (datetime.now(tz.utc) + timedelta(hours=1)).isoformat()}


# === Models ===

class AgentChatRequest(BaseModel):
    """Запрос к агентной системе"""
    message: str = Field(..., description="Сообщение пользователя")
    session_id: Optional[str] = None
    context: dict[str, Any] = Field(default_factory=dict)
    agent_mode: str = Field(default="brain", description="brain | mark | automation | transcripts")
    model_tier: str = Field(default="standard", description="standard | premium - уровень модели")
    chat_history: list[dict] = Field(default_factory=list, description="История предыдущих сообщений")
    llm_profile_id: Optional[str] = Field(
        default=None,
        description=(
            "Phase 1 multi-provider: ID LLM-профиля для этого конкретного "
            "запроса. Перекрывает tenant-wide default (is_default=TRUE). "
            "Используется когда юзер хочет выбрать модель в чате — "
            "анализ через Claude, генерация через DeepSeek и т.п. "
            "Если профиль не найден/disabled — fallback на default. "
            "Также можно передать через context['llm_profile_id']."
        ),
    )


class AgentChatResponse(BaseModel):
    """Ответ от агентной системы"""
    success: bool
    message: str
    session_id: str
    agent_mode: str
    agents_involved: list[str] = Field(default_factory=list)
    execution_time_ms: int = 0
    sources: list[dict] = Field(default_factory=list)


class AgentStatus(BaseModel):
    """Статус агентной системы"""
    name: str
    status: str  # online | offline | error
    url: str
    agents_count: int = 0
    last_check: str


class TaskSpecRequest(BaseModel):
    """Запрос на генерацию ТЗ"""
    task_description: str = Field(..., description="Описание задачи")
    additional_context: dict[str, Any] = Field(default_factory=dict)
    execution_mode: str = Field(default="plan_only", description="plan_only | supervised | autonomous")
    skip_execution: bool = Field(default=False, description="Пропустить этап выполнения")
    # Аудит: per-user граф/векторы. Раньше был захардкожен ОДИН user_id для всех
    # — /tasks/process использовал чужой граф (мульти-тенант утечка). Теперь
    # граф берётся по этому user_id.
    user_id: str | None = Field(default=None, description="ID пользователя (граф/векторы)")


class TaskSpecResponse(BaseModel):
    """Ответ с результатом генерации ТЗ"""
    success: bool
    task_id: str
    status: str
    specification_id: Optional[str] = None
    stages_completed: list[str] = Field(default_factory=list)
    markdown_spec: Optional[str] = None
    execution_plan: Optional[dict] = None
    file_paths: dict[str, str] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    execution_time_ms: int = 0


# === State ===
_mark001_agents = None
_meetflow_agents = {}  # keyed by model_tier: {"standard": agents, "premium": agents}


# === Helper Functions ===

def estimate_tokens(text: str) -> int:
    """Примерная оценка токенов (4 символа ≈ 1 токен)"""
    return len(text) // 4 if text else 0


def track_external_agent_usage(
    agent_mode: str,
    input_text: str,
    output_text: str,
    user_id: str | None = None,
    session_id: str | None = None,
    model: str = "gemini-flash-lite-latest",
    execution_time_ms: int = 0
):
    """
    Трекинг использования внешних агентов (Mark001, MeetFlow).
    Эти агенты используют свои LLM клиенты, поэтому трекаем вручную.
    """
    input_tokens = estimate_tokens(input_text)
    output_tokens = estimate_tokens(output_text)

    track_usage(
        provider="google",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=0,
        request_type="chat",
        latency_ms=execution_time_ms,
        user_id=user_id,
        session_id=session_id,
        agent_mode=agent_mode,
        success=True
    )

    logger.debug(f"📊 Tracked {agent_mode}: {input_tokens}+{output_tokens} tokens")


async def check_agent_health(url: str) -> bool:
    """Проверить доступность агентной системы"""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get(f"{url}/health")
            return res.status_code == 200
    except Exception:
        return False


async def call_mark001_api(message: str, session_id: Optional[str] = None, context: dict | None = None, model_tier: str = "standard") -> dict:
    """Вызвать Mark001 Marketing Agents через API"""
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            res = await client.post(
                f"{MARK001_URL}/api/v1/marketing/task",
                json={
                    "message": message,
                    "session_id": session_id,
                    "context": context or {},
                    "model_tier": model_tier
                }
            )
            if res.status_code == 200:
                return res.json()
            else:
                return {"success": False, "error": f"HTTP {res.status_code}: {res.text}"}
    except Exception as e:
        logger.error(f"Mark001 API error: {e}")
        return {"success": False, "error": str(e)}


async def call_meetflow_api(message: str, session_id: Optional[str] = None, user_id: Optional[str] = None) -> dict:
    """Вызвать MeetFlow Automation через API"""
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            res = await client.post(
                f"{MEETFLOW_URL}/api/v1/automation/task",
                json={
                    "message": message,
                    "session_id": session_id,
                    "user_id": user_id
                }
            )
            if res.status_code == 200:
                return res.json()
            else:
                return {"success": False, "error": f"HTTP {res.status_code}: {res.text}"}
    except Exception as e:
        logger.error(f"MeetFlow API error: {e}")
        return {"success": False, "error": str(e)}


async def init_mark001_direct(model_tier: str = "standard"):
    """Инициализировать Mark001 агентов напрямую (если API недоступен).

    Кэш ПО TIER — премиум-тумблер фронта теперь реально меняет модель роя
    (модели берутся из mark001_async/models.json, не из кода)."""
    global _mark001_agents
    if _mark001_agents is None:
        _mark001_agents = {}
    if model_tier in _mark001_agents:
        return _mark001_agents[model_tier]

    try:
        # Add path to sys.path
        # Пустой MARK001_PATH означает «не настроено»; класть его в
        # sys.path нельзя — пустая строка там значит текущий каталог и
        # тихо подменяет модули.
        if MARK001_PATH and MARK001_PATH not in sys.path:
            sys.path.insert(0, MARK001_PATH)

        from agents import create_marketing_agents
        try:
            agents = create_marketing_agents(model_tier=model_tier)
        except TypeError:
            # старая версия пакета без параметра — совместимость
            agents = create_marketing_agents()
        _mark001_agents[model_tier] = agents
        logger.info(f"✅ Mark001 agents initialized (tier={model_tier}): "
                    f"{list(agents.keys())}")
        return agents
    except Exception as e:
        logger.error(f"❌ Failed to init Mark001 agents: {e}")
        return None


async def init_meetflow_direct(model_tier: str = "standard"):
    """Инициализировать MeetFlow агентов напрямую (если API недоступен). Кеширует по model_tier."""
    global _meetflow_agents
    if model_tier in _meetflow_agents:
        return _meetflow_agents[model_tier]

    try:
        if MEETFLOW_PATH not in sys.path:
            sys.path.insert(0, MEETFLOW_PATH)

        from autogen_async import create_automation_agents
        agents = create_automation_agents(model_tier=model_tier)
        _meetflow_agents[model_tier] = agents
        logger.info(f"✅ MeetFlow agents initialized (tier={model_tier}): {list(agents.keys())}")
        return agents
    except Exception as e:
        logger.error(f"❌ Failed to init MeetFlow agents (tier={model_tier}): {e}")
        return None


def _ag2_usage_totals(agents: dict, manager=None) -> tuple[int, int]:
    """Суммарные РЕАЛЬНЫЕ токены AG2-клиентов (prompt/completion).

    AG2 копит usage в client.total_usage_summary за жизнь процесса —
    считаем ДЕЛЬТУ до/после прогона, а не абсолют."""
    tin = tout = 0
    holders = list(agents.values()) + ([manager] if manager is not None else [])
    for a in holders:
        cl = getattr(a, "client", None)
        summ = getattr(cl, "total_usage_summary", None) if cl else None
        if not isinstance(summ, dict):
            continue
        for v in summ.values():
            if isinstance(v, dict):
                tin += int(v.get("prompt_tokens") or 0)
                tout += int(v.get("completion_tokens") or 0)
    return tin, tout


async def run_mark001_direct(message: str, session_id: str, model_tier: str = "standard", chat_history: list | None = None, mode: str = "") -> dict:
    """Запустить Mark001 агентов напрямую (модель по tier из models.json).

    mode: "solo" | "swarm" | "" — выбор пользователя из UI перекрывает env
    TESSENT_MARK_SOLO (пусто = поведение по env, как раньше)."""
    try:
        # Пустой MARK001_PATH означает «не настроено»; класть его в
        # sys.path нельзя — пустая строка там значит текущий каталог и
        # тихо подменяет модули.
        if MARK001_PATH and MARK001_PATH not in sys.path:
            sys.path.insert(0, MARK001_PATH)

        # СОЛО-РЕЖИМ: один специалист-СКИЛЛ (тот же системный промпт, что у
        # агента роя) со всеми инструментами в ReAct-цикле — без 16
        # посредников. Сбой → фолбэк на GroupChat.
        _solo = (mode == "solo") if mode in ("solo", "swarm") else (
            os.getenv("TESSENT_MARK_SOLO", "0") == "1")
        if _solo:
            try:
                from agents.solo_executor import run_solo_task
                _solo_start = datetime.now()
                answer, involved, meta = await run_solo_task(
                    message, model_tier=model_tier,
                    chat_history=chat_history or [])
                _solo_ms = int((datetime.now() - _solo_start
                                ).total_seconds() * 1000)
                model_used = ""
                try:
                    from config import Config as _MarkCfg
                    model_used = _MarkCfg.resolve_model(model_tier)
                except Exception:
                    pass
                _u = meta.get("usage") or {}
                logger.info(f"✅ Solo mode: skill={meta.get('skill')}, "
                            f"turns={meta.get('turns')}, {_solo_ms}ms")
                return {
                    "success": True, "response": answer,
                    "session_id": session_id,
                    "agents_involved": involved,
                    "execution_time_ms": _solo_ms,
                    "appendix": meta.get("appendix") or "",
                    "usage": {**_u, "model": model_used},
                }
            except Exception as e:
                logger.warning(f"solo mode failed → фолбэк на GroupChat: {e}")

        from agents import create_marketing_chat, get_chat_response

        agents = await init_mark001_direct(model_tier=model_tier)
        if not agents:
            return {"success": False, "error": "Failed to initialize agents"}

        usage_before = _ag2_usage_totals(agents)

        start = datetime.now()
        try:
            group_chat, _manager, _result = await create_marketing_chat(
                agents=agents,
                message=message,
                max_rounds=12,  # с send_introductions=False укладываемся
                chat_history=chat_history or [],
                model_tier=model_tier,
            )
        except TypeError:
            # старая версия пакета без model_tier — совместимость
            group_chat, _manager, _result = await create_marketing_chat(
                agents=agents,
                message=message,
                max_rounds=12,
                chat_history=chat_history or []
            )

        try:
            response_text = get_chat_response(group_chat)
        except Exception as e:
            logger.error(f"Error extracting chat response: {e}")
            response_text = None

        # get_chat_response берёт ОДНО сообщение — цифры инструментов и
        # черновики специалистов терялись. Собираем их: appendix уходит
        # пользователю приложением (0 LLM), digest — в senior review (premium).
        _appendix = ""
        _digest = ""
        try:
            from agents.marketing_agents import (
                build_chat_digest,
                collect_work_appendix,
            )
            _msgs = getattr(group_chat, "messages", []) or []
            _appendix = collect_work_appendix(_msgs, response_text or "")
            _digest = build_chat_digest(_msgs)
        except Exception:
            logger.debug("mark appendix/digest skipped", exc_info=True)

        # If no response, try to get last meaningful message
        if not response_text or len(response_text) < 50:
            for msg in reversed(getattr(group_chat, "messages", [])):
                if isinstance(msg, dict):
                    content = msg.get("content", "")
                    name = msg.get("name", "")
                    if name != "User" and content and len(content) > 100:
                        response_text = content
                        break

        agents_involved = set()
        for msg in getattr(group_chat, "messages", []):
            if isinstance(msg, dict):
                name = msg.get("name", "")
                if name and name != "User":
                    agents_involved.add(name)

        execution_time = int((datetime.now() - start).total_seconds() * 1000)

        # РЕАЛЬНЫЕ токены прогона (дельта AG2-клиентов) — раньше учитывалась
        # оценка len//4 от входа/выхода чата, занижая расход роя в 10-50×.
        usage_after = _ag2_usage_totals(agents, _manager)
        real_in = max(0, usage_after[0] - usage_before[0])
        real_out = max(0, usage_after[1] - usage_before[1])
        model_used = ""
        try:
            from config import Config as _MarkCfg
            model_used = _MarkCfg.resolve_model(model_tier)
        except Exception:
            pass

        return {
            "success": True,
            "response": response_text or "Агенты завершили работу, но не сформировали текстовый ответ.",
            "session_id": session_id,
            "agents_involved": list(agents_involved),
            "execution_time_ms": execution_time,
            "appendix": _appendix,
            "work_digest": _digest,
            "usage": {"input_tokens": real_in, "output_tokens": real_out,
                      "model": model_used},
        }
    except Exception as e:
        logger.error(f"Mark001 direct error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {"success": False, "error": str(e)}


async def _build_mark_company_context(user_id: str | None) -> str:
    """Контекст компании из Tessbrain для заземления маркетинговых агентов.

    Раньше рой Mark был «generic»: знал только текст запроса, но не компанию
    (продукты/KPI/цели/сильные стороны). Подмешиваем компактную выжимку из
    снапшота компании, чтобы любая маркетинговая задача решалась под РЕАЛЬНЫЙ
    бизнес. Best-effort — никогда не ломает Mark.
    """
    if not user_id:
        return ""
    try:
        from backend.core.sleep.enhanced_snapshot import get_enhanced_snapshot_generator
        from backend.core.store.graph_view import merged_graph_view_for_user

        gb = await merged_graph_view_for_user(user_id, use_networkx=None)
        gen = get_enhanced_snapshot_generator(gb, user_id=user_id)
        gen.user_id = user_id
        snap = await gen.get_company_snapshot()
        # Прошлые маркетинговые наработки Mark (накопленная память).
        # Тот же фильтр качества, что в recall: без него в контекст лезли
        # эхо задач и реплики диалога («У тебя это должно быть в данных»).
        _past_marketing: list[str] = []
        try:
            arts = await gb.find_nodes_by_label("Knowledge", limit=50, tenant_id=user_id)
            mk = []
            for a in arts or []:
                if not isinstance(a, dict) or a.get("category") != "marketing":
                    continue
                _content = str(a.get("content") or "").strip()
                _lowc = _content[:300].lower()
                if (not a.get("name") or len(_content) < 200
                        or _lowc.startswith("[контекст") or "[задача]" in _lowc):
                    continue
                mk.append(a)
            mk.sort(key=lambda a: a.get("created_at", ""), reverse=True)
            _past_marketing = [a.get("name", "") for a in mk[:3] if a.get("name")]
        except Exception:
            pass
        try:
            await gb.close(save=False)
        except Exception:
            pass
        if not snap:
            return ""

        def _g(attr):
            return getattr(snap, attr, None)

        parts: list[str] = []
        if _g("name"): parts.append(f"Компания: {snap.name}")
        if _g("industry"): parts.append(f"Отрасль: {snap.industry}")
        if _g("target_market"): parts.append(f"Целевой рынок: {snap.target_market}")
        if _g("business_model"): parts.append(f"Бизнес-модель: {snap.business_model}")
        if _g("description"): parts.append(f"О компании: {str(snap.description)[:300]}")
        prods = _g("products") or []
        if prods:
            parts.append("Продукты: " + ", ".join(
                p.get("name", "") for p in prods[:6] if isinstance(p, dict) and p.get("name")))
        kpis = _g("kpis") or []
        if kpis:
            parts.append("KPI: " + "; ".join(
                f"{k.get('name')}={k.get('current_value')}" for k in kpis[:6]
                if isinstance(k, dict) and k.get("name")))
        goals = _g("strategic_goals") or []
        if goals:
            parts.append("Стратегические цели: " + "; ".join(
                g.get("goal", "") for g in goals[:5] if isinstance(g, dict) and g.get("goal")))
        if _g("strengths"):
            parts.append("Сильные стороны: " + ", ".join(snap.strengths[:5]))
        if _g("weaknesses"):
            parts.append("Зоны роста: " + ", ".join(snap.weaknesses[:5]))
        # КОНКУРЕНТЫ из памяти — чтобы «наш основной конкурент» рой определял
        # САМ, не переспрашивая пользователя (люди не любят вопросы).
        comps = _g("competitors") or []
        if comps:
            parts.append("Конкуренты (из памяти компании, «основной» = первый): "
                         + ", ".join(str(c) for c in comps[:6]))
        # ДОМЕННЫЕ СНАПШОТЫ (строятся ночью, см. core/sleep/domain_snapshots):
        # свой домен (маркетинг) — целиком: партнёрства, каналы, что
        # сработало, хроника; смежный (продажи) — кратко: что реально
        # закрывается и с какими возражениями — посылы Mark опираются на
        # живые сделки. Фолбэк до первой ночи — заголовки наработок.
        _mkt_md = _sales_md = ""
        try:
            from backend.core.sleep.domain_snapshots import read_domain_snapshot
            _mkt_md = read_domain_snapshot(user_id, "marketing", 2500)
            _sales_md = read_domain_snapshot(user_id, "sales", 800)
        except Exception:
            logger.debug("domain snapshots read skipped", exc_info=True)
        if _mkt_md:
            parts.append("\n[МАРКЕТИНГ-СНАПШОТ: что происходит в маркетинге "
                         "сейчас и история — опирайся на это]\n" + _mkt_md)
        elif _past_marketing:
            parts.append("Прошлые маркетинговые наработки (опирайся на них): "
                         + "; ".join(_past_marketing))
        if _sales_md:
            parts.append("\n[Смежный домен — ПРОДАЖИ (кратко): что реально "
                         "закрывается и что буксует]\n" + _sales_md)
        # РЕАЛЬНЫЕ ЦИФРЫ (единые метрики план/факт + подключённые таблицы/
        # CRM): бюджеты/чеки/объёмы в медиапланах Mark — из данных, а не из
        # пересказов. Never-raise.
        try:
            from backend.core.ontology.numbers_context import numbers_block
            _nb = numbers_block(user_id, "")
            if _nb.get("text"):
                parts.append("\n" + _nb["text"])
        except Exception:
            logger.debug("mark numbers_block skipped", exc_info=True)
        return "\n".join(parts)
    except Exception as e:
        logger.debug(f"mark company context failed: {e}")
        return ""


async def _resolve_mentioned_project_snapshots(user_id: str | None, message: str) -> str:
    """Снапшоты проектов, упомянутых в задаче Mark (по имени, дёшево: читаем
    ГОТОВЫЕ файлы из snapshots_by_user/<uid>/projects/, без генерации)."""
    if not user_id or not message:
        return ""
    try:
        import json as _json

        from backend.core.store.tenant_paths import snapshots_dir_for_user
        pdir = snapshots_dir_for_user(user_id) / "projects"
        if not pdir.exists():
            return ""
        msg = message.lower()
        parts: list[str] = []
        for f in pdir.glob("*.json"):
            try:
                d = _json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            nm = (d.get("name") or "").strip()
            # имя проекта из 2+ символов, встречается в тексте задачи
            if len(nm) >= 3 and nm.lower() in msg:
                line = (f"Проект «{nm}»: статус {d.get('status', '?')}, "
                        f"прогресс {d.get('progress', 0)}%, задач "
                        f"{d.get('tasks_completed', 0)}/{d.get('tasks_total', 0)}")
                if d.get("lead"):
                    line += f", лид {d['lead']}"
                if d.get("ai_summary"):
                    line += f". Состояние: {str(d['ai_summary'])[:300]}"
                parts.append(line)
            if len(parts) >= 3:
                break
        if parts:
            return "[Проекты из задачи — реальное состояние]\n" + "\n".join(parts)
        return ""
    except Exception as e:
        logger.debug(f"mark project snapshots failed: {e}")
        return ""


def _extract_entity_candidates(message: str) -> list:
    """Имена-кандидаты из задачи для ТОЧЕЧНОГО поиска по индексу (НЕ сканируя
    весь граф — критично при тысячах клиентов). Источники: кавычки, имена
    после маркеров (клиент/партнёр/для/по/компания/бренд), Capitalized-фразы.
    """
    if not message:
        return []
    import re
    cands: list = []
    for m in re.findall(r"[«\"'(]([^»\"')]{2,60})[»\"')]", message):
        cands.append(m.strip())
    for m in re.findall(
        r"(?:клиент[аеуы]?|партнёр[аеуы]?|партнер[аеуы]?|компани[июеяй]|"
        r"бренд[аеуы]?|для|по|у)\s+"
        r"([A-ZА-ЯЁ][\wА-Яа-яёЁ\-.]+(?:\s+[A-ZА-ЯЁ][\wА-Яа-яёЁ\-.]+){0,2})",
        message):
        cands.append(m.strip())
    for m in re.findall(
        r"\b([A-ZА-ЯЁ][\wА-Яа-яёЁ\-.]{2,}(?:\s+[A-ZА-ЯЁ][\wА-Яа-яёЁ\-.]{2,}){0,2})\b",
        message):
        cands.append(m.strip())
    out: list = []
    seen: set = set()
    for c in cands:
        cl = c.lower()
        if len(c) >= 4 and cl not in seen:
            seen.add(cl)
            out.append(c)
    return out[:8]


async def _resolve_mentioned_entities_context(user_id: str | None, message: str,
                                              max_entities: int = 4) -> str:
    """Достать из графа данные по сущностям/клиентам, УПОМЯНУТЫМ в задаче.

    МАСШТАБИРУЕМО: извлекаем имена-кандидаты из задачи и ищем ТОЛЬКО их через
    индексированный search_nodes (а не сканируем весь граф). Так одинаково
    быстро и при 5 клиентах, и при тысячах; в контекст попадают только
    реально упомянутые. Без лишних LLM-вызовов. Best-effort.
    """
    if not user_id or not message or len(message) < 4:
        return ""
    _VALID = {"Company", "Organization", "Project", "Product",
              "Person", "Team", "Department", "Entity"}
    try:
        from backend.core.store.graph_view import merged_graph_view_for_user

        candidates = _extract_entity_candidates(message)
        if not candidates:
            return ""
        gb = await merged_graph_view_for_user(user_id, use_networkx=None)
        found: list = []
        seen: set = set()
        for cand in candidates:
            try:
                hits = await gb.search_nodes(query=cand, limit=5, tenant_id=user_id)
            except Exception:
                hits = []
            cl = cand.lower()
            best = None
            for h in (hits or []):
                nm = (h.get("name") or "").strip()
                if not nm:
                    continue
                nl = nm.lower()
                if cl == nl:            # точное совпадение — приоритет
                    best = h
                    break
                if cl in nl and best is None:   # узел содержит кандидата
                    best = h
            if best:
                key = (best.get("name") or "").lower()
                label = best.get("_label") or best.get("label") or "Entity"
                if key not in seen and str(label) in _VALID:
                    seen.add(key)
                    found.append((label, best))
            if len(found) >= max_entities:
                break
        found = found[:max_entities]
        if not found:
            try:
                await gb.close(save=False)
            except Exception:
                pass
            return ""

        parts = ["[Данные по упомянутым клиентам/сущностям из базы — используй их]"]
        for label, n in found:
            nid = n.get("id")
            line = f"- {n.get('name')} ({label})"
            for f in ("role", "description", "status", "department", "industry"):
                if n.get(f):
                    line += f"; {f}: {str(n[f])[:120]}"
            parts.append(line)
            try:
                rels = await gb.get_node_relationships(nid)
                neigh: list = []
                decisions: list = []
                ideas: list = []
                opinions: list = []
                psych: dict = {}
                for e in (rels.get("outgoing", []) + rels.get("incoming", []))[:30]:
                    tid = e.get("target") or e.get("source")
                    nd = await gb.get_node_by_id(tid)
                    if not nd:
                        continue
                    lbl = str(nd.get("_label") or nd.get("label") or "").lower()
                    nm = nd.get("name") or nd.get("summary") or nd.get("title")
                    if lbl == "psychologicalprofile":
                        psych = {k: nd.get(k) for k in (
                            "personality_type", "team_role", "leadership_style",
                            "communication_style", "dominant_traits",
                            "motivation_drivers", "strengths") if nd.get(k)}
                    elif lbl == "decision" and nm:
                        decisions.append(str(nm)[:90])
                    elif lbl == "idea" and nm:
                        ideas.append(str(nm)[:90])
                    elif lbl == "opinion" and nm:
                        opinions.append(str(nm)[:90])
                    elif nm:
                        neigh.append(f"{nm}[{e.get('type')}]")
                if psych:
                    _ps = "; ".join(
                        f"{k}: {', '.join(v) if isinstance(v, list) else v}"
                        for k, v in psych.items() if v)
                    parts.append(f"  психопрофиль: {_ps}")
                if decisions:
                    parts.append("  решения: " + " | ".join(decisions[:5]))
                if ideas:
                    parts.append("  идеи: " + " | ".join(ideas[:5]))
                if opinions:
                    parts.append("  мнения: " + " | ".join(opinions[:5]))
                if neigh:
                    parts.append("  связи: " + ", ".join(neigh[:10]))
            except Exception:
                pass
        try:
            await gb.close(save=False)
        except Exception:
            pass
        return "\n".join(parts)
    except Exception as e:
        logger.debug(f"mentioned entities context failed: {e}")
        return ""


async def _mark_brief_gate(message: str, known_context: str = "") -> str:
    """Бриф-гейт: синьор сначала спрашивает, потом делает.

    Если творческая задача поставлена без продукта/цели/аудитории — НЕ жжём
    рой (16 агентов × 12 раундов), а возвращаем 3-4 уточняющих вопроса.
    Один дешёвый вызов. Пусто = задача достаточна, запускаем рой.
    Отключение: TESSENT_MARK_BRIEF_GATE=0."""
    if os.getenv("TESSENT_MARK_BRIEF_GATE", "1") == "0":
        return ""
    try:
        import json as _json
        import re as _re

        from backend.core.llm import get_llm_router
        llm = get_llm_router()
        if llm is None:
            return ""
        resp = await llm.generate(
            prompt=(
                "Ты — опытный маркетолог, принимающий задачу в работу. Люди НЕ "
                "любят вопросы: вопрос — крайняя мера. У команды есть доступ "
                "к памяти компании — ЦА, УТП, позиционирование, конкурентов "
                "она берёт ОТТУДА сама.\n"
                + (f"ЧТО УЖЕ ИЗВЕСТНО ИЗ ПАМЯТИ КОМПАНИИ:\n"
                   f"{known_context[:1200]}\n\n" if known_context else "")
                + f"Задача клиента: «{message[:800]}»\n\n"
                "enough=false ТОЛЬКО если в задаче есть объект, который "
                "НЕЛЬЗЯ определить НИ из задачи, НИ из памяти выше («тот "
                "клиент» без имени, URL которого нигде нет). Если объект "
                "есть в памяти (например конкурент в списке конкурентов) — "
                "enough=true. НЕ спрашивай ЦА/УТП/цель/воронку. Максимум 2 "
                "вопроса. "
                'СТРОГО JSON: {"enough": true|false, "questions": ["..."]}'),
            temperature=0.1, max_tokens=200)
        text = resp.get("text", "") if isinstance(resp, dict) else str(resp)
        m = _re.search(r"\{[\s\S]*\}", text)
        data = _json.loads(m.group(0)) if m else {}
        if isinstance(data, dict) and data.get("enough") is False:
            qs = [str(q).strip() for q in (data.get("questions") or [])
                  if str(q).strip()][:4]
            if qs:
                return ("Прежде чем включать команду, уточню пару вещей — "
                        "так результат будет в разы точнее:\n\n"
                        + "\n".join(f"{i}. {q}" for i, q in enumerate(qs, 1))
                        + "\n\nОтветьте в свободной форме — и я запущу "
                          "команду с полным брифом.")
    except Exception:
        logger.debug("mark brief gate skipped", exc_info=True)
    return ""


async def _mark_senior_review(task: str, response: str,
                              model_tier: str = "standard",
                              work_digest: str = "") -> str:
    """«Выпускающий маркетинг-директор»: финальный проход по работе роя.

    Чеклист синьора: конкретика вместо воды, KPI у каждой рекомендации,
    учтён контекст компании, есть next steps. Возвращает улучшенный ответ
    (или исходный при сбое/деградации). Один вызов через наш router —
    учитывается в затратах автоматически. TESSENT_MARK_SENIOR_REVIEW=0 — выкл.

    work_digest (только premium): выжимка ВСЕЙ переписки роя — ревьюер видит
    работу целиком (цифры инструментов, черновики специалистов), а не одно
    выбранное сообщение, и собирает итог без потерь."""
    if os.getenv("TESSENT_MARK_SENIOR_REVIEW", "1") == "0":
        return response
    if not response or len(response) < 400:
        return response
    _digest_block = ""
    if work_digest and model_tier == "premium":
        _digest_block = (
            "\nПОЛНАЯ РАБОТА КОМАНДЫ (хронология, включая данные "
            "инструментов — используй факты и цифры отсюда, если их нет в "
            f"итоге):\n{work_digest[:8000]}\n"
        )
    try:
        from backend.core.llm import get_llm_router
        llm = get_llm_router()
        if llm is None:
            return response
        resp = await llm.generate(
            prompt=(
                "Ты — выпускающий маркетинг-директор с 15-летним опытом. "
                "Команда подготовила работу — доведи её до уровня senior.\n\n"
                f"ЗАДАЧА КЛИЕНТА: {task[:600]}\n\n"
                f"ИТОГ КОМАНДЫ:\n{response[:9000]}\n"
                f"{_digest_block}\n"
                "ЧЕКЛИСТ:\n"
                "1. Отвечает ли работа на задачу клиента? Уберись всё не по делу.\n"
                "2. Конкретика вместо воды: каналы, форматы, сроки, цифры. "
                "Generic-фразы («повысить узнаваемость», «работать с ЦА») "
                "замени конкретикой или удали.\n"
                "3. У КАЖДОЙ рекомендации — измеримый KPI и способ проверки.\n"
                "4. Если использован контекст компании — усиль привязку; если "
                "предложение противоречит контексту — исправь.\n"
                "5. В конце — «Следующие шаги»: 3-5 действий с владельцами "
                "и сроками.\n\n"
                "Верни ТОЛЬКО готовую улучшенную работу (markdown, на русском), "
                "без комментариев о правках. Сохрани полезные факты и цитаты."),
            temperature=0.3, max_tokens=3500,
            **({"model_tier": __import__(
                "backend.core.llm.router", fromlist=["ModelTier"]
            ).ModelTier.PREMIUM} if model_tier == "premium" else {}))
        text = (resp.get("text", "") if isinstance(resp, dict)
                else str(resp or "")).strip()
        # защита от деградации: принимаем только сопоставимый по объёму ответ
        if text and len(text) > len(response) * 0.5:
            return text
    except Exception:
        logger.debug("mark senior review skipped", exc_info=True)
    return response


async def _recall_mark_outputs(user_id: str | None, message: str,
                               limit: int = 2) -> str:
    """Прошлые наработки Mark по теме задачи (Knowledge category=marketing).

    Замыкает память кампаний: _persist_mark_output сохраняет результаты,
    а этот метод возвращает релевантные обратно при новой задаче — рой
    продолжает работу по клиенту, а не начинает с нуля. Релевантность —
    пересечение значимых слов задачи с названием наработки."""
    if not user_id or not message:
        return ""
    try:
        import re as _re

        from backend.core.store.graph_view import merged_graph_view_for_user
        gb = await merged_graph_view_for_user(user_id, use_networkx=None)
        try:
            nodes = await gb.find_nodes_by_label(
                "Knowledge", limit=300, tenant_id=user_id, strict_tenant=True)
        finally:
            try:
                await gb.close(save=False)
            except Exception:
                pass
        words = {w for w in _re.findall(r"[а-яёa-z]{4,}", message.lower())}
        scored = []
        for n in nodes or []:
            if (n.get("category") or "") != "marketing":
                continue
            # Гигиена: не подтягиваем мусорные записи (эхо обогащённой
            # задачи «[Контекст агентства...]» / «[Задача]», короткие
            # реплики диалога) — они замусоривают контекст новой задачи.
            content = str(n.get("content") or "").strip()
            _lowc = content[:300].lower()
            if (len(content) < 200 or _lowc.startswith("[контекст")
                    or "[задача]" in _lowc):
                continue
            name = (n.get("name") or "").lower()
            overlap = sum(1 for w in words if w in name)
            if overlap >= 2:
                scored.append((overlap, n))
        scored.sort(key=lambda x: (-x[0], x[1].get("created_at") or ""))
        parts = []
        for _, n in scored[:limit]:
            parts.append(f"• {n.get('name', '')}: "
                         f"{str(n.get('content') or '')[:500]}")
        return "\n".join(parts)
    except Exception:
        logger.debug("recall mark outputs failed", exc_info=True)
        return ""


async def _persist_mark_output(user_id: str | None, task: str,
                               response: str, agents: list | None) -> None:
    """Сохранить результат Mark в память компании (Knowledge category=marketing).

    Замыкает цикл обучения: наработки роя (стратегии, брифы, анализы конкурентов)
    накапливаются, ищутся в Brain-чате и заземляют будущие задачи Mark.
    Best-effort — никогда не ломает ответ.
    """
    response = str(response or "").strip()
    # Сохраняем только НАРАБОТКИ: содержательный результат (>=200 симв.),
    # а не эхо обогащённой задачи/контекста и не короткие реплики диалога —
    # иначе recall при следующей задаче подтянет мусор.
    _low = response[:300].lower()
    if (not user_id or len(response) < 200
            or _low.startswith("[контекст") or "[задача]" in _low):
        return
    try:
        import uuid
        from datetime import datetime, timezone

        from backend.core.store.graph_view import merged_graph_view_for_user

        gb = await merged_graph_view_for_user(user_id, use_networkx=None)
        kid = f"mkt_{uuid.uuid4().hex[:10]}"
        name = (task or "Маркетинговая наработка").strip().split("\n")[0][:90]
        await gb.create_node(
            node_id=kid, label="Knowledge",
            properties={
                "name": name,
                "category": "marketing",
                "content": str(response)[:6000],
                "tags": [str(a) for a in (agents or [])][:8],
                "importance": "medium",
                "user_id": user_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            access_group="private",
        )
        try:
            await gb.close(save=True)
        except Exception:
            pass
    except Exception as e:
        logger.debug(f"persist mark output failed: {e}")


async def run_meetflow_direct(message: str, session_id: str, user_id: str | None = None, model_tier: str = "standard") -> dict:
    """Запустить MeetFlow агентов напрямую"""
    try:
        # CRITICAL: Remove TERMINATE from user message to prevent premature termination
        message = message.replace("TERMINATE", "").replace("terminate", "").strip()

        if MEETFLOW_PATH not in sys.path:
            sys.path.insert(0, MEETFLOW_PATH)

        from autogen_async import (
            create_automation_graph_dict,
            load_automation_session,
            set_current_user_context,
            start_new_automation_chat,
        )

        if user_id:
            set_current_user_context(user_id=user_id)

        agents = await init_meetflow_direct(model_tier=model_tier)
        if not agents:
            return {"success": False, "error": "Failed to initialize agents"}

        start = datetime.now()
        graph = create_automation_graph_dict(agents)

        # === CONTEXT ENRICHMENT ===
        # Если пользователь использует местоимения ("их", "это", "вот их"),
        # обогащаем запрос контекстом из предыдущей сессии
        context_keywords = ["их", "это", "вот их", "мне их", "мне это", "отправь их", "отправь это"]
        needs_context = any(kw in message.lower() for kw in context_keywords)

        carryover_context = None
        if needs_context and session_id:
            existing_session = await load_automation_session(session_id)
            if existing_session and "messages" in existing_session:
                # Найти последний ответ с ДАННЫМИ (не статусные сообщения)
                # Приоритет: JSON с meetings > форматированный список > любой контент с данными
                data_content = None
                formatted_content = None

                for msg in reversed(existing_session["messages"]):
                    if msg.get("name") != "User" and msg.get("content"):
                        content = msg.get("content", "")

                        # Пропускаем системные сообщения, tool calls и статусные сообщения
                        skip_patterns = [
                            "TERMINATE",
                            "***** Suggested tool call",
                            "✅ Сообщение отправлено",
                            "Сообщение отправлено в Telegram",
                            "success",
                            "Failed to send"
                        ]
                        if any(pattern in content for pattern in skip_patterns):
                            continue

                        # Приоритет 1: JSON с данными (meetings, events, tasks)
                        if content.strip().startswith("{") and any(key in content for key in ["meetings", "events", "tasks", "total_found"]):
                            data_content = content
                            break

                        # Приоритет 2: Форматированный список (1. ... 2. ... или • ...)
                        if not formatted_content and ("1." in content or "•" in content or "**" in content):
                            # Проверяем что это не просто короткое сообщение
                            if len(content) > 50:
                                formatted_content = content

                # Используем найденный контекст
                carryover_context = data_content or formatted_content
                if carryover_context:
                    logger.info(f"📎 Found context to enrich message: {carryover_context[:100]}...")
                    message = f"{message}\n\n[КОНТЕКСТ ИЗ ПРЕДЫДУЩЕГО ОТВЕТА - ИСПОЛЬЗУЙ ЭТИ ДАННЫЕ]:\n{carryover_context}"
                else:
                    logger.warning("⚠️ No data context found in session history")

        main_group, _main_manager, _result, _user_proxy = await start_new_automation_chat(
            session_id=session_id,
            agents=agents,
            graph_dict=graph,
            message=message,
            openwebui_mode=True,
            model_tier=model_tier
        )

        # Extract response
        response_text = None
        agents_involved = set()
        all_messages = []

        for msg in getattr(main_group, "messages", []):
            if isinstance(msg, dict):
                name = msg.get("name", "")
                content = msg.get("content", "")
                if name and name != "User":
                    agents_involved.add(name)
                    all_messages.append(f"{name}: {content}")
                # Prefer non-JSON, human-readable responses from key agents
                if name != "User" and content:
                    # Skip system messages and tool calls if better message exists
                    if "***** Suggested tool call" not in content and not content.strip().startswith("{"):
                        response_text = content

        # --- RESPONSE PROCESSING & FORMATTING ---

        # 1. Prepare raw response
        if response_text is None:
            response_text = ""

        raw_response = response_text.replace("TERMINATE", "").strip()
        history_text = "\n".join(all_messages[-10:])

        # 2. Decide if we need LLM formatting
        should_format = (
            not raw_response or
            raw_response.startswith("{") or
            raw_response.startswith("[") or
            "project_id" in raw_response or
            "uuid" in raw_response or
            "suggested tool call" in raw_response.lower()
        )

        if should_format:
            try:
                from backend.core.llm.router import LLMRouter
                router = LLMRouter()

                input_context = raw_response if raw_response else f"История диалога агентов:\n{history_text}"

                prompt = f"""
                Ты - редактор ответов AI-ассистента Tessbrain. Твоя задача - превратить сырые данные или историю диалога в идеальный ответ для пользователя.

                Вопрос пользователя: "{message}"

                Входящие данные (ответ системы или лог):
                {input_context}

                ИНСТРУКЦИИ ПО ФОРМАТИРОВАНИЮ:
                1. Удали весь технический мусор: project_id, uuid, "TERMINATE", json-скобки.
                2. Используй Markdown для красоты:
                   - Списки: используй маркеры (* или -)
                   - Заголовки: используй жирный шрифт (**Текст**)
                   - Иконки: добавь подходящие эмодзи (📅 для встреч, 📂 для проектов, ✅ для задач)
                3. Для ПРОЕКТОВ: "📂 **Название** (Статус)"
                4. Для ВСТРЕЧ: "📅 **Название** — Время/Дата"
                5. Будь кратким, вежливым и говори только по делу.
                6. Язык ответа: РУССКИЙ.

                Сформулируй финальный ответ:
                """

                formatted_response = await router.generate(prompt, max_tokens=1000)
                response_text = formatted_response
            except Exception as e:
                logger.warning(f"Response formatting failed: {e}")
                # Fallback
                if not response_text:
                    response_text = "✅ Задача выполнена. (Ошибка форматирования ответа)"
        else:
            response_text = raw_response

        execution_time = int((datetime.now() - start).total_seconds() * 1000)

        return {
            "success": True,
            "response": response_text,
            "session_id": session_id,
            "agents_involved": list(agents_involved),
            "execution_time_ms": execution_time
        }
    except Exception as e:
        logger.error(f"MeetFlow direct error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        # Спасение ответа: групповой чат часто ПАДАЕТ уже ПОСЛЕ того, как
        # специалист дал полный ответ (директор зацикливается «что делаем
        # дальше?» и упирается в rate-limit). Пользователь получал «❌ ошибка»,
        # хотя готовый ответ лежал в сессии. Достаём лучший ответ из истории.
        try:
            from autogen_async import load_automation_session
            session = await load_automation_session(session_id)
            salvaged = None
            for msg in reversed((session or {}).get("messages", [])):
                name = msg.get("name") or ""
                content = (msg.get("content") or "").strip()
                if name in ("", "User") or not content:
                    continue
                if ("Suggested tool call" in content or content.startswith("{")
                        or "TERMINATE" in content):
                    continue
                # Монологи-предложения директора («Что делаем дальше?») —
                # не ответ на вопрос; берём последний содержательный ответ
                # специалиста, а директорские планёрки пропускаем.
                if name == "AutomationDirector" and (
                        "Что вы хотите" in content or "Что делаем" in content
                        or "Какой вектор" in content or "Жду ваших указаний" in content):
                    continue
                if len(content) > 80:
                    salvaged = content
                    break
            if salvaged:
                logger.info("MeetFlow direct: ответ спасён из сессии после сбоя "
                            f"({len(salvaged)} симв.)")
                return {"success": True,
                        "response": salvaged.replace("TERMINATE", "").strip(),
                        "session_id": session_id, "salvaged": True,
                        "agents_involved": [], "execution_time_ms": 0}
        except Exception:
            logger.debug("salvage from session failed", exc_info=True)
        return {"success": False, "error": str(e)}


from litestar.datastructures import State


async def _meeting_digests_cached(meetings_data: list,
                                  max_new_llm: int = 60) -> list:
    """Конспекты встреч с ВЕЧНЫМ кешем (data/meeting_digests/<id>.txt).

    Транскрипт не меняется → конспект считается ОДИН раз в жизни встречи и
    дальше бесплатен для всех чатов/вопросов. Новых LLM-вызовов за запрос —
    не больше max_new_llm (параллельно, семафор 8); встречи сверх лимита
    получают голову транскрипта (следующий запрос дожмёт хвост).
    Подробно: docs/TRANSCRIPTS_CHAT.md."""
    from pathlib import Path
    ddir = Path("data") / "meeting_digests"
    ddir.mkdir(parents=True, exist_ok=True)

    results: list = [None] * len(meetings_data)
    to_generate: list = []
    for i, m in enumerate(meetings_data):
        f = ddir / f"{Path(str(m['id'])).name}.txt"
        if f.is_file():
            try:
                results[i] = f.read_text(encoding="utf-8")
                continue
            except Exception:
                pass
        to_generate.append(i)

    from backend.core.llm import get_llm_router
    llm = get_llm_router()
    allowed = set(to_generate[:max_new_llm])
    sem = asyncio.Semaphore(8)

    async def _one(i: int) -> None:
        m = meetings_data[i]
        fallback = m["transcript"][:1200]
        if llm is None or i not in allowed:
            results[i] = fallback
            return
        try:
            from backend.core.llm.usage_tracker import UsageContext
            async with sem, UsageContext(agent_mode="transcripts",
                                         request_type="meeting_digest"):
                resp = await llm.generate(
                    prompt=(
                        "Сожми транскрипт встречи в конспект ≤1500 символов. "
                        "ТОЛЬКО факты из текста: принятые решения, "
                        "договорённости (кто/что/когда), названные цифры и "
                        "суммы, участники, риски/разногласия. Без воды и "
                        "оценок.\n\n"
                        f"Встреча: {m['title']} ({m['date']})\n\n"
                        f"{m['transcript'][:60_000]}"),
                    temperature=0.2, max_tokens=800)
            text = (resp.get("text", "") if isinstance(resp, dict)
                    else str(resp or "")).strip()
            if len(text) > 100:
                f = ddir / f"{Path(str(m['id'])).name}.txt"
                f.write_text(text[:2200], encoding="utf-8")
                results[i] = text[:2200]
                return
        except Exception:
            logger.debug(f"digest failed for {m['id']}", exc_info=True)
        results[i] = fallback

    if to_generate:
        logger.info(f"📚 генерирую {min(len(to_generate), max_new_llm)} новых "
                    f"конспектов (в кеше {len(meetings_data) - len(to_generate)})")
        await asyncio.gather(*[_one(i) for i in to_generate])
    return results


async def run_gemma_chat(message: str, session_id: str, context: dict, graph_builder: Any = None, model_tier: str = "standard", chat_history: list | None = None) -> dict:
    """Запустить чат с транскрипциями через Gemma 3n или Gemini (в зависимости от tier)"""
    start_time = datetime.now()
    try:
        gemma = get_gemma_client()
        supabase = get_supabase_client()

        # DEBUG: List all meetings to see what's in the database
        await supabase.debug_list_meetings(limit=5)

        # Build a mapping from graph node ID to Supabase meeting_id
        # because frontend sends "Meeting_4e6a4d71" but Supabase needs full UUID
        graph_to_supabase_id = {}
        try:
            if graph_builder and graph_builder.nx_graph:
                for node_id, node_data in graph_builder.nx_graph.nodes(data=True):
                    if node_data.get("_label") == "Meeting":
                        supabase_mid = node_data.get("meeting_id") or node_data.get("_key")
                        if supabase_mid:
                            graph_to_supabase_id[node_id] = supabase_mid
                logger.info(f"🔗 Mapped {len(graph_to_supabase_id)} graph meetings to Supabase IDs")
            else:
                logger.warning("⚠️ Graph builder is not available or empty")
        except Exception as e:
            logger.warning(f"⚠️ Could not build graph-to-supabase mapping: {e}")

        meeting_filters = context.get("meeting_filter") or [] # List of {id, title} or {id}
        if isinstance(meeting_filters, dict) or isinstance(meeting_filters, str):
            meeting_filters = [meeting_filters]

        project_filters = context.get("project_filter") or []
        if isinstance(project_filters, dict) or isinstance(project_filters, str):
            project_filters = [project_filters]

        folder_filters = context.get("folder_filter") or []
        if isinstance(folder_filters, dict) or isinstance(folder_filters, str):
            folder_filters = [folder_filters]

        # NEW: Document filters
        document_filters = context.get("document_filter") or context.get("documents") or []
        if isinstance(document_filters, dict) or isinstance(document_filters, str):
            document_filters = [document_filters]

        # ДИАГНОСТИКА фильтров Private LLM: видно ЧТО реально пришло с фронта
        # (meeting/project/folder). Если фильтр пуст, а грузятся все встречи —
        # значит фронт не прислал фильтр; если непуст, а грузит лишнее —
        # значит ниже не отрабатывает get_meetings.
        logger.info(
            "🎯 [Private LLM] filters: meetings=%d project=%s folder=%s docs=%d | "
            "ctx_user_id=%s",
            len(meeting_filters), project_filters or [], folder_filters or [],
            len(document_filters), context.get("user_id"),
        )

        all_meetings_to_fetch = set()
        all_documents_to_fetch = []
        sources = []

        # 1. Collect meeting IDs from meeting filters
        for m in meeting_filters:
            mid = None
            title = "Meeting"

            if isinstance(m, str):
                mid = m
            elif isinstance(m, dict):
                mid = m.get("id") or m.get("meeting_id")
                title = m.get("title", "Unknown Meeting")

            if mid:
                # Clean ID from frontend prefix if present.
                # БАГ (фикс): str.replace возвращает НОВУЮ строку — без
                # присваивания префикс "Meeting_" не снимался, и маппинг
                # graph→supabase промахивался (фильтр по встрече не работал).
                mid = mid.replace("Meeting_", "")

                # Map graph node ID to Supabase UUID
                supabase_id = graph_to_supabase_id.get(mid, mid)  # Fallback to mid if not found
                logger.info(f"🔗 Mapping {mid} → {supabase_id}")

                all_meetings_to_fetch.add(supabase_id)
                sources.append({"type": "meeting", "id": mid, "name": title})

        # 2. Collect meetings from projects
        if project_filters:
            ctx_user_id = context.get("user_id")
            # Не используем "system" как user_id - это вызывает ошибку UUID
            if ctx_user_id == "system" or not ctx_user_id:
                ctx_user_id = None

            for p in project_filters:
                pid = None
                p_name = "Unknown Project"

                if isinstance(p, str):
                    pid = p
                elif isinstance(p, dict):
                    pid = p.get("id")
                    p_name = p.get("name", "Unknown Project")

                if pid:
                    try:
                        # Не передаём user_id если он None - пусть метод сам разберётся
                        meetings = await supabase.get_meetings(project_id=pid, user_id=ctx_user_id)
                        logger.info(
                            "📦 project %s (%s) → %d встреч", p_name, pid, len(meetings or []))
                        for m in meetings:
                            mid = m.get("id") or m.get("meeting_id")
                            if mid:
                                all_meetings_to_fetch.add(mid)
                        sources.append({"type": "project", "id": pid, "name": p_name})
                    except Exception as e:
                        logger.warning(f"Failed to fetch meetings for project {pid}: {e}")
                        # Hide technical details in logs that might appear in UI
                        pass

        # 3. Collect meetings from folders
        if folder_filters:
            ctx_user_id_folder = context.get("user_id")
            # Не используем "system" как user_id - это вызывает ошибку UUID
            if ctx_user_id_folder == "system" or not ctx_user_id_folder:
                ctx_user_id_folder = None

            for f in folder_filters:
                fid = None
                f_name = "Unknown Folder"

                if isinstance(f, str):
                    fid = f
                elif isinstance(f, dict):
                    fid = f.get("id")
                    f_name = f.get("name", "Unknown Folder")

                if fid:
                    try:
                        meetings = await supabase.get_meetings(folder_id=fid, user_id=ctx_user_id_folder)
                        logger.info(
                            "📁 folder %s (%s) → %d встреч", f_name, fid, len(meetings or []))
                        for m in meetings:
                            mid = m.get("id") or m.get("meeting_id")
                            if mid:
                                all_meetings_to_fetch.add(mid)
                        sources.append({"type": "folder", "id": fid, "name": f_name})
                    except Exception as e:
                        logger.warning(f"Failed to fetch meetings for folder {fid}: {e}")
                        # Hide technical details in logs that might appear in UI
                        pass

        # 4. Collect documents from document filters
        if document_filters:
            for d in document_filters:
                did = None
                d_name = "Document"

                if isinstance(d, str):
                    did = d
                elif isinstance(d, dict):
                    did = d.get("id")
                    d_name = d.get("title", d.get("name", "Document"))

                if did:
                    all_documents_to_fetch.append({"id": did, "title": d_name})
                    sources.append({"type": "document", "id": did, "name": d_name})

            logger.info(f"📄 Found {len(all_documents_to_fetch)} documents to fetch")

        # 6. Fetch document contents
        document_context_text = ""
        document_fetched_count = 0

        # Тенант-скоуп для fetch'ей ниже: id встреч/документов могут прийти
        # из tool-args LLM — без фильтра по владельцу это чтение чужого тенанта
        # (service-ключ обходит RLS).
        ctx_uid = context.get("user_id")
        if ctx_uid == "system" or not ctx_uid:
            ctx_uid = None

        if all_documents_to_fetch:
            for doc_info in all_documents_to_fetch:
                did = doc_info["id"]
                d_title = doc_info["title"]

                try:
                    # Fetch document content from Supabase using REST API
                    doc_params = {
                        "id": f"eq.{did}",
                        "select": "id,title,content,doc_type,metadata"
                    }
                    if ctx_uid:
                        doc_params["user_id"] = f"eq.{ctx_uid}"
                    doc_list = await supabase._request(
                        "GET",
                        "/rest/v1/documents",
                        params=doc_params
                    )

                    if doc_list and len(doc_list) > 0:
                        doc = doc_list[0]
                        content = doc.get("content", "")
                        title = doc.get("title", d_title)
                        doc_type = doc.get("doc_type", "text")

                        if content:
                            document_context_text += f"\n\n--- DOCUMENT: {title} (type: {doc_type}) ---\n{content}\n"
                            document_fetched_count += 1
                            logger.info(f"✅ Loaded document {did}: '{title}', {len(content)} chars")
                        else:
                            logger.warning(f"⚠️ Empty content for document {did} ({title})")
                    else:
                        logger.warning(f"⚠️ Document not found: {did}")

                except Exception as e:
                    logger.error(f"❌ Error fetching document {did}: {e}")

            logger.info(f"📄 Loaded {document_fetched_count} documents for context")

        # 7. Fetch transcripts — сначала собираем ВСЕ (id, title, date, text),
        # решение «целиком или конспектами» принимается ниже по суммарному
        # объёму (см. docs/TRANSCRIPTS_CHAT.md, «гибридная сборка»).
        meetings_data: list = []
        fetched_count = 0

        for mid in all_meetings_to_fetch:
            try:
                # Fetch details once (includes transcript)
                details = await supabase.get_meeting_details(
                    mid, include_transcript=True, user_id=ctx_uid)

                if not details:
                    logger.warning(f"⚠️ Meeting details not found for {mid}")
                    continue

                title = details.get("title", "Meeting")
                date = details.get("created_at", "")
                # Поле транскрипта может называться по-разному в зависимости от версии схемы
                transcript = (
                    details.get("transcription_text") or  # Новая схема
                    details.get("transcription") or       # Альтернативное название
                    details.get("transcript") or          # Ещё одно возможное название
                    ""
                )

                if transcript:
                    meetings_data.append({"id": mid, "title": title,
                                          "date": str(date)[:10],
                                          "transcript": transcript})
                    fetched_count += 1
                else:
                    logger.warning(f"⚠️ Empty transcript for meeting {mid} ({title})")
            except Exception as e:
                logger.error(f"❌ Error fetching transcript for {mid}: {e}")

        # 6.5 ВРЕМЕННЫЕ ФАЙЛЫ ЧАТА (не в базе знаний): пользователь прикрепил
        # файл скрепкой → он живёт в data/chat_uploads и читается в контекст
        # этого диалога. В граф/документы НЕ попадает — «закинул, поработал,
        # забыл» (ретеншн 7 дней чистит в chat_upload).
        from pathlib import Path

        temp_context_text = ""
        temp_count = 0
        # Читаем ТОЛЬКО из папки текущего пользователя. Раньше все файлы
        # лежали общей кучей и брались по id — то есть чужой прикреплённый
        # файл втягивался в свой диалог, стоило узнать идентификатор.
        # Владелец теперь в пути, и чужое недостижимо по построению.
        _temp_dir = _chat_upload_dir(context.get("user_id") or "")
        for tf in (context.get("temp_files") or [])[:10]:
            fid = tf.get("id") if isinstance(tf, dict) else str(tf)
            fname = (tf.get("name") if isinstance(tf, dict) else None) or "файл"
            if not fid or _temp_dir is None:
                continue
            safe = Path(str(fid)).name
            fpath = _temp_dir / f"{safe}.txt"
            try:
                if fpath.is_file():
                    body = fpath.read_text(encoding="utf-8")[:150_000]
                    temp_context_text += (f"\n\n--- ВРЕМЕННЫЙ ФАЙЛ (только для "
                                          f"этого чата): {fname} ---\n{body}\n")
                    temp_count += 1
                    sources.append({"type": "file", "id": safe, "name": fname})
                else:
                    logger.warning(f"⚠️ temp file not found: {safe}")
            except Exception as e:
                logger.warning(f"temp file read failed {safe}: {e}")
        if temp_count:
            logger.info(f"📎 Loaded {temp_count} temp files for context")

        # 7.2 ГИБРИДНАЯ СБОРКА (docs/TRANSCRIPTS_CHAT.md): проект целиком не
        # должен ни ломаться, ни молча обрезаться. Влезает в кап → полные
        # транскрипты (лучшее качество). Не влезает → конспекты ВСЕХ встреч
        # (кеш data/meeting_digests — LLM-вызов один раз в жизни встречи) +
        # полные тексты наиболее релевантных вопросу. Отчёт видит весь
        # проект, токены предсказуемы.
        _CTX_CAP = 400_000
        full_context_text = ""
        digest_note = ""
        base_len = len(temp_context_text) + len(document_context_text)
        total_tr = sum(len(m["transcript"]) for m in meetings_data)

        if meetings_data and (base_len + total_tr <= _CTX_CAP
                              or len(meetings_data) <= 3):
            for m in meetings_data:
                full_context_text += (f"\n\n--- TRANSCRIPT OF MEETING: "
                                      f"{m['title']} ({m['date']}) ---\n"
                                      f"{m['transcript']}\n")
        elif meetings_data:
            import re as _re

            # Релевантность вопросу: частота значимых слов в транскрипте
            words = set(_re.findall(r"[а-яёa-z0-9]{4,}", message.lower()))

            def _score(m: dict) -> int:
                low = m["transcript"].lower()
                return sum(low.count(w) for w in words)

            ranked = sorted(meetings_data, key=_score, reverse=True)
            full_read = ranked[:6]
            full_ids = {id(m) for m in full_read}

            # Конспекты всех встреч (кеш; новых LLM-вызовов ≤ 60 за запрос,
            # хвост без кеша идёт головой транскрипта — честная деградация)
            digests = await _meeting_digests_cached(meetings_data)
            digest_block = "\n\n=== КОНСПЕКТЫ ВСЕХ ВЫБРАННЫХ ВСТРЕЧ ===\n"
            for m, dg in zip(meetings_data, digests):
                mark = " [прочитана целиком ниже]" if id(m) in full_ids else ""
                digest_block += (f"\n• {m['title']} ({m['date']}){mark}:\n"
                                 f"{dg}\n")

            # Полные тексты релевантных — в остаток бюджета
            budget = _CTX_CAP - base_len - len(digest_block)
            full_block = "\n\n=== ПОЛНЫЕ ТРАНСКРИПТЫ НАИБОЛЕЕ РЕЛЕВАНТНЫХ ВОПРОСУ ===\n"
            read_full = 0
            for m in full_read:
                chunk = (f"\n--- TRANSCRIPT: {m['title']} ({m['date']}) ---\n"
                         f"{m['transcript'][:60_000]}\n")
                if len(chunk) > budget:
                    break
                full_block += chunk
                budget -= len(chunk)
                read_full += 1

            full_context_text = digest_block + (full_block if read_full else "")
            digest_note = (f"📚 Материалов много: конспекты всех {len(meetings_data)} "
                           f"встреч + целиком прочитано {read_full} наиболее "
                           f"релевантных")
            logger.info(f"📚 digest mode: {len(meetings_data)} конспектов, "
                        f"{read_full} целиком (total_tr={total_tr})")

        # Подключаемый контекст (переписки / CRM / задачи): тот же
        # context_bridge, что в основном brain-чате. Без этого выбранная в
        # сайдбаре переписка в режиме «Чат по встречам и документам» молча
        # игнорировалась — пользователь выбирал источник и получал
        # ответ-заглушку.
        extra_context_text = ""
        try:
            from backend.core.messengers.context_bridge import (
                build_extra_context,
                wants_extra_context,
            )
            if wants_extra_context(context):
                extra_context_text = await build_extra_context(
                    str(context.get("user_id") or ""), context) or ""
                if extra_context_text:
                    logger.info("🔌 [Private LLM] подключён выбранный контекст "
                                "(%d симв.)", len(extra_context_text))
        except Exception:
            logger.debug("extra context bridge skipped", exc_info=True)

        # Combine all context
        combined_context = ""

        if extra_context_text:
            combined_context += "\n\n" + extra_context_text

        if temp_context_text:
            combined_context += temp_context_text

        if document_context_text:
            combined_context += document_context_text

        if full_context_text:
            combined_context += full_context_text

        # Страховочный кап (документы/файлы тоже могут быть огромными).
        if len(combined_context) > _CTX_CAP:
            combined_context = (combined_context[:_CTX_CAP]
                                + "\n\n[…контекст обрезан по лимиту — выберите "
                                  "меньше материалов для полного покрытия]")
            logger.warning(f"⚠️ context capped at {_CTX_CAP} chars")

        if not combined_context:
            # If context is empty even after fetching, warn user
            if all_meetings_to_fetch:
                return {
                    "success": True,
                    "response": "⚠️ Для выбранных встреч не найдены транскрипции (текстовые расшифровки). Пожалуйста, убедитесь, что встречи были обработаны и имеют текст.",
                    "session_id": session_id,
                    "agents_involved": ["Чат по встречам и документам"],
                    "execution_time_ms": int((datetime.now() - start_time).total_seconds() * 1000),
                    "sources": sources
                }

            if all_documents_to_fetch:
                return {
                    "success": True,
                    "response": "⚠️ Для выбранных документов не найден контент. Пожалуйста, убедитесь, что документы были загружены корректно.",
                    "session_id": session_id,
                    "agents_involved": ["Чат по встречам и документам"],
                    "execution_time_ms": int((datetime.now() - start_time).total_seconds() * 1000),
                    "sources": sources
                }

            system_prompt = """Ты - интеллектуальный ассистент Tessbrain.

Пользователь не выбрал конкретный контекст (встречи, документы), поэтому отвечай на основе своих общих знаний.

ИНСТРУКЦИИ:
1. Давай полезные и информативные ответы.
2. Если вопрос касается данных компании, предложи пользователю выбрать конкретные встречи или документы в фильтрах для более точного ответа.
3. Отвечай на русском языке.
4. Будь вежливым и профессиональным."""
        else:
            # Build context description
            context_parts = []
            if document_fetched_count > 0:
                context_parts.append(f"{document_fetched_count} документов")
            if fetched_count > 0:
                context_parts.append(f"{fetched_count} встреч")

            context_desc = " и ".join(context_parts)

            system_prompt = f"""Ты - интеллектуальный ассистент Tessbrain, анализирующий предоставленные материалы.

ИНСТРУКЦИИ:
1. Отвечай на вопросы пользователя ТОЛЬКО на основе предоставленного контекста.
2. Давай ПОДРОБНЫЕ и РАЗВЁРНУТЫЕ ответы с конкретными деталями из контекста.
3. Используй цитаты и примеры из материалов для подтверждения своих ответов.
4. Структурируй ответ с помощью списков и заголовков, если информации много.
5. Если ответа нет в контексте, честно скажи об этом.
6. Отвечай на русском языке.

Контекст содержит: {context_desc}.

Помни: пользователь ожидает детальный анализ, а не краткую сводку."""

        # 7.5 ПАМЯТЬ ДИАЛОГА: раньше история чата в этот режим не передавалась
        # вовсе — «а что я спрашивал выше?» модель не видела. Последние
        # сообщения идут перед текущим вопросом (последний ответ — щедрее,
        # чтобы работали «уточни пункт 2 из твоего ответа»).
        user_message = message
        hist = [m for m in (chat_history or [])
                if isinstance(m, dict) and str(m.get("content") or "").strip()]
        if hist:
            hist = hist[-10:]
            _last_ai = max((i for i, m in enumerate(hist)
                            if m.get("role") != "user"), default=-1)
            dlg = []
            for i, m in enumerate(hist):
                role = "Пользователь" if m.get("role") == "user" else "Ассистент"
                cap = 2000 if i == _last_ai else 400
                dlg.append(f"{role}: {str(m.get('content'))[:cap]}")
            user_message = ("ПРЕДЫДУЩИЙ ДИАЛОГ (для контекста):\n"
                            + "\n".join(dlg)
                            + f"\n\nТЕКУЩИЙ ВОПРОС: {message}")

        # 8. Call Gemma (with fallback to Gemini, using model_tier)
        result = await gemma.chat(user_message, context=combined_context, system_prompt=system_prompt, model_tier=model_tier)

        execution_time = int((datetime.now() - start_time).total_seconds() * 1000)

        response_text = result.get("response", result.get("error", "Error"))
        # Прозрачность гибридного режима: пользователь видит, каким путём
        # собран контекст (конспекты vs полные тексты).
        if digest_note and result.get("success") and isinstance(response_text, str):
            response_text += f"\n\n_{digest_note}_"

        return {
            "success": result["success"],
            "response": response_text,
            "session_id": session_id,
            "agents_involved": ["Чат по встречам и документам"],
            "execution_time_ms": execution_time,
            "sources": sources
        }

    except Exception as e:
        logger.error(f"Private LLM chat error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {"success": False, "error": str(e)}


# === Route Handlers ===

@post("/chat")
async def agent_chat(
    data: AgentChatRequest,
    state: State,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> AgentChatResponse:
    """
    Универсальный эндпоинт для работы с агентами.

    agent_mode:
    - "brain" - Tessbrain (по умолчанию)
    - "mark" - Mark001 Marketing Agents
    - "automation" - MeetFlow Automation
    - "transcripts" - Chat with Transcripts (Gemma 3n)
    """
    session_id = data.session_id or str(uuid.uuid4())
    start_time = datetime.now()

    # Анти-IDOR: весь handler ниже читает data.context["user_id"] для доступа к
    # графу/встречам. Если есть токен — его uid ПОБЕЖДАЕТ любой body user_id
    # (раньше можно было подставить чужой и получить его данные). Нет токена
    # (внутренние вызовы) — контекст как есть.
    _tok_uid = _caller_user_id(authorization)
    if _tok_uid:
        if data.context is None:
            data.context = {}
        data.context["user_id"] = _tok_uid

    # Set context for usage tracking
    ctx_token = _usage_context.set({
        "user_id": data.context.get("user_id"),
        "session_id": session_id,
        "agent_mode": data.agent_mode,
        "request_type": "chat",
        "model_tier": data.model_tier
    })

    # Phase 1 multi-provider (per-user LLM choice):
    # Если в request передан llm_profile_id — прокидываем его в LLM-router
    # context, чтобы _maybe_get_active_profile_client использовал именно этот
    # профиль вместо tenant-wide default. Принимается top-level data.llm_profile_id
    # или из context для обратной совместимости со старым frontend'ом.
    _requested_profile_id = (
        data.llm_profile_id
        or (data.context.get("llm_profile_id") if isinstance(data.context, dict) else None)
    )
    if _requested_profile_id:
        try:
            from backend.core.llm.router import set_llm_context as _set_llm_ctx
            _set_llm_ctx(
                user_id=data.context.get("user_id"),
                session_id=session_id,
                agent_mode=data.agent_mode,
                llm_profile_id=str(_requested_profile_id),
            )
            logger.info(
                "[agent_chat] using llm_profile_id=%s (per-request override) for session=%s",
                _requested_profile_id, session_id,
            )
        except Exception as exc:
            logger.debug("set_llm_context with profile_id failed: %s", exc)

    try:
        if data.agent_mode == "mark":
            # Заземляем маркетинговый рой на данные компании (best-effort):
            # подмешиваем выжимку из снапшота, чтобы агенты работали под
            # реальный бизнес, а не «вообще».
            _mark_uid = (data.context or {}).get("user_id")
            _mark_ctx = await _build_mark_company_context(_mark_uid)
            # Мульти-клиент: данные по упомянутым в задаче клиентам/сущностям
            _mark_clients = await _resolve_mentioned_entities_context(_mark_uid, data.message)
            # Снапшоты упомянутых ПРОЕКТОВ (бэклог №5): если в задаче назван
            # проект — подмешиваем его карточку (статус/вердикт ai_summary),
            # чтобы Mark работал под конкретный проект, а не «вообще».
            _mark_projects = await _resolve_mentioned_project_snapshots(_mark_uid, data.message)
            if _mark_projects:
                _mark_clients = ((_mark_clients or "") + "\n" + _mark_projects).strip()
            # Фирменный стиль из регламентов (тот же механизм, что в
            # композиторе) — Mark пишет в тоне компании, а не «вообще».
            _mark_style = ""
            try:
                from backend.api.routes.compose import _company_style_brief
                _mark_style = await _company_style_brief()
            except Exception:
                logger.debug("mark style brief skipped", exc_info=True)
            # Память кампаний: прошлые наработки Mark по этой теме/клиенту
            # (сохраняются в Knowledge category=marketing) — подтягиваем
            # обратно, чтобы рой продолжал, а не начинал с нуля.
            _mark_recall = await _recall_mark_outputs(_mark_uid, data.message)
            # Упоминания по теме из гибридного поиска — ДОПОЛНИТЕЛЬНЫЙ источник
            # (SEARCH_CONSUMERS_AUDIT, правка №5). MARK живёт на снапшотах и
            # карточках — это дизайн, не баг, поэтому блок по умолчанию
            # ВЫКЛЮЧЕН: TESSENT_MARK_HYBRID=on включает. build_if_missing=False
            # — MARK не платит за сборку движков, берёт только готовые.
            _mark_mentions = ""
            try:
                import os as _os
                if (_os.getenv("TESSENT_MARK_HYBRID", "") or "").strip().lower() in (
                        "on", "1", "true", "yes"):
                    from backend.core.search.context_fragments import (
                        topic_fragments,
                    )
                    _frags, _eng = await topic_fragments(
                        _mark_uid, data.message, top_k=4,
                        build_if_missing=False)
                    if _frags:
                        _mark_mentions = "\n---\n".join(_frags)
            except Exception:
                logger.debug("mark hybrid mentions skipped", exc_info=True)
            _mark_message = data.message
            _mark_prefix = ""
            if _mark_ctx:
                _mark_prefix += ("[Контекст агентства/аккаунта из Tessbrain]\n"
                                 f"{_mark_ctx}\n\n")
            if _mark_clients:
                _mark_prefix += f"{_mark_clients}\n\n"
            if _mark_style:
                _mark_prefix += f"{_mark_style.strip()}\n\n"
            if _mark_recall:
                _mark_prefix += ("[Прошлые наработки Mark по теме — продолжай "
                                 f"и развивай, не начинай с нуля]\n{_mark_recall}\n\n")
            if _mark_mentions:
                _mark_prefix += ("[Упоминания по теме из памяти компании]\n"
                                 f"{_mark_mentions}\n\n")
            if _mark_prefix:
                _mark_message = f"{_mark_prefix}[Задача]\n{data.message}"
            # БРИФ-ГЕЙТ (только на ПЕРВОЕ сообщение — продолжение диалога
            # уже содержит ответы): расплывчатая творческая задача →
            # уточняющие вопросы вместо запуска роя. Синьор сначала
            # спрашивает, потом делает.
            if not (data.chat_history or []):
                _gate_qs = await _mark_brief_gate(data.message, _mark_prefix)
                if _gate_qs:
                    logger.info("Mark brief gate: задаём уточняющие вопросы")
                    return AgentChatResponse(
                        success=True, message=_gate_qs,
                        session_id=str(session_id), agent_mode="mark",
                        agents_involved=["BriefTaker"],
                        execution_time_ms=0)
            # Режим из UI: «простой» (соло-скилл) или «команда» (GroupChat).
            # Пусто/не передан → как раньше, по env TESSENT_MARK_SOLO.
            _mark_mode = str((data.context or {}).get("mark_mode") or "").lower()
            # Try API first, fallback to direct
            if await check_agent_health(MARK001_URL):
                result = await call_mark001_api(_mark_message, session_id, data.context, data.model_tier)
            else:
                logger.info("Mark001 API offline, using direct mode")
                result = await run_mark001_direct(
                    _mark_message,
                    session_id,
                    model_tier=data.model_tier,
                    chat_history=data.chat_history,
                    mode=_mark_mode,
                )

            if result.get("success"):
                logger.info(f"✅ Mark001 success: {session_id}")

                response_text = str(result.get("response", ""))
                # ВЫПУСКАЮЩИЙ ДИРЕКТОР: финальный senior-проход по работе
                # роя (конкретика, KPI у рекомендаций, next steps) — один
                # вызов через наш router, в затратах учитывается сам.
                _reviewed = await _mark_senior_review(
                    data.message, response_text, data.model_tier,
                    work_digest=str(result.get("work_digest") or ""))
                if _reviewed != response_text:
                    response_text = _reviewed
                    result.setdefault("agents_involved", [])
                    if "SeniorReviewer" not in result["agents_involved"]:
                        result["agents_involved"].append("SeniorReviewer")
                # Приложение «рабочие материалы» — ПОСЛЕ ревью, чтобы ревьюер
                # его не переписал: сырые цифры/черновики идут как есть.
                _mark_appendix = str(result.get("appendix") or "")
                if _mark_appendix:
                    response_text = f"{response_text}\n{_mark_appendix}"
                # Учёт: РЕАЛЬНЫЕ токены AG2 (direct-режим отдаёт дельту по
                # всем клиентам роя); оценка len//4 — только фолбэк для
                # API-режима, где внутренности роя не видны.
                _usage = result.get("usage") or {}
                if _usage.get("input_tokens") or _usage.get("output_tokens"):
                    try:
                        track_usage(
                            provider="gemini",
                            model=_usage.get("model") or "gemini-flash-lite-latest",
                            input_tokens=int(_usage.get("input_tokens") or 0),
                            output_tokens=int(_usage.get("output_tokens") or 0),
                            user_id=data.context.get("user_id"),
                            session_id=session_id,
                            agent_mode="mark",
                            request_type="marketing_swarm",
                            latency_ms=int(result.get("execution_time_ms", 0)),
                        )
                    except Exception:
                        logger.debug("mark real usage track failed", exc_info=True)
                else:
                    track_external_agent_usage(
                        agent_mode="mark",
                        input_text=data.message,
                        output_text=response_text,
                        user_id=data.context.get("user_id"),
                        session_id=session_id,
                        model="gemini-flash-lite-latest",
                        execution_time_ms=int(result.get("execution_time_ms", 0))
                    )

                # Замыкаем цикл обучения: сохраняем наработку Mark в память
                # компании (best-effort, по исходной задаче — не по обогащённой).
                try:
                    await _persist_mark_output(
                        (data.context or {}).get("user_id"),
                        data.message,
                        response_text,
                        result.get("agents_involved"),
                    )
                except Exception:
                    logger.debug("persist mark output skipped", exc_info=True)

                try:
                    return AgentChatResponse(
                        success=True,
                        message=response_text,
                        session_id=str(session_id),
                        agent_mode="mark",
                        agents_involved=[str(a) for a in result.get("agents_involved", [])],
                        execution_time_ms=int(result.get("execution_time_ms", 0))
                    )
                except Exception as e:
                    logger.error(f"❌ Response validation error: {e}")
                    return AgentChatResponse(
                        success=False,
                        message=f"❌ Ошибка валидации ответа: {e!s}",
                        session_id=str(session_id),
                        agent_mode="mark"
                    )
            else:
                logger.error(f"❌ Mark001 failed: {result.get('error')}")
                return AgentChatResponse(
                    success=False,
                    message=f"❌ Ошибка: {result.get('error', 'Unknown error')}",
                    session_id=str(session_id),
                    agent_mode="mark"
                )

        elif data.agent_mode == "automation":
            user_id = data.context.get("user_id")
            automation_mode = data.context.get("automation_mode", "tasks")

            # Метка расходов на ВСЮ ветку Auto: внутренние Gemini-вызовы
            # (ReAct-петли, web operator, MeetFlow-форматирование) трекаются
            # в gemini_client из контекста — без этой строки они падали в
            # «unknown» (аудит: «систематическое занижение учёта»).
            try:
                from backend.core.llm.router import set_llm_context as _set_auto_ctx
                _set_auto_ctx(user_id=user_id, session_id=session_id,
                              agent_mode="automation")
            except Exception:
                logger.debug("set automation llm context failed", exc_info=True)

            # Tess: local FunctionGemma router (Ollama) + ReAct loop + Big LLM fallback
            # Architecture: SkillRouter → FunctionGemma = Dispatcher, Big LLM = Analyst/Writer
            if automation_mode == "tess":
                try:
                    # Session Memory: обогащаем сообщение контекстом предыдущих шагов
                    from backend.core.auto.session_memory import get_auto_session_store
                    from backend.core.local_models.ollama_functiongemma import (
                        ReActResult,
                        get_tools_catalog,
                        run_react_loop,
                    )
                    from backend.integrations.tessent_brain_tools import TessentBrainTools
                    auto_session_store = get_auto_session_store()
                    auto_session = auto_session_store.get_or_create(
                        session_id=str(session_id),
                        user_id=user_id or "",
                    )

                    # Обогащаем сообщение (если "отправь это" — подставить предыдущий результат)
                    original_message = data.message
                    enriched_message, was_enriched = auto_session.enrich_message(data.message)
                    if was_enriched:
                        logger.info(f"[Tess] Session memory enriched message: '{data.message[:50]}...'")
                        data.message = enriched_message

                    # Get allowed tools from context (UI checkboxes)
                    allowed_tools = data.context.get("tess_allowed_tools")
                    if isinstance(allowed_tools, list):
                        allowed_tools = [str(x) for x in allowed_tools]
                    else:
                        allowed_tools = None  # All tools allowed

                    ollama_model = os.getenv("TESS_OLLAMA_MODEL", "functiongemma")
                    ollama_base_url = os.getenv("OLLAMA_HOST", "http://localhost:11434")

                    # Build tool dependencies
                    graph_builder = getattr(state, "graph", None)
                    vector_indexer = getattr(state, "vector_indexer", None)

                    # LLMRouter: try state first, then create fresh instance.
                    # state.llm_router is often None because it's not set in app startup.
                    llm_router = getattr(state, "llm_router", None)
                    if not llm_router:
                        try:
                            from backend.core.llm.router import LLMRouter
                            llm_router = LLMRouter()
                            logger.info("[Tess] Created LLMRouter instance for BigLLM ReAct")
                        except Exception as lr_err:
                            logger.warning(f"[Tess] Could not create LLMRouter: {lr_err}")

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
                            reasoning_engine = None

                    from backend.integrations.tessent_brain_tools import (
                        build_enhanced_search,
                    )
                    tools = TessentBrainTools(
                        graph_builder=graph_builder,
                        reasoning_engine=reasoning_engine,
                        vector_indexer=vector_indexer,
                        user_id=user_id,  # Pass user_id for Supabase RLS
                        # Аудит SEARCH_CONSUMERS_AUDIT: предпочтительный
                        # Enhanced-путь search_knowledge лежал мёртвым, потому
                        # что аргумент не передавался. Сбой сборки → None →
                        # прежний legacy. TESSENT_AUTO_ENHANCED=off — откат.
                        enhanced_search=build_enhanced_search(
                            graph_builder=graph_builder,
                            vector_indexer=vector_indexer,
                            llm_router=llm_router,
                            user_id=user_id,
                        ),
                    )

                    # === Integration Registry: загружаем внешние интеграции пользователя ===
                    integration_registry = None
                    try:
                        from backend.integrations.registry import IntegrationRegistry
                        integration_registry = IntegrationRegistry()
                        await integration_registry.load_for_user(user_id or "")
                        # Логируем что загрузилось
                        loaded = integration_registry.list_loaded()
                        if loaded:
                            logger.info(f"[Tess] Integration Registry loaded: {[i['id'] for i in loaded]}")
                    except Exception as reg_err:
                        logger.warning(f"[Tess] Integration Registry failed to load: {reg_err}")
                        integration_registry = None

                    # Tool executor function for ReAct loop
                    async def execute_tool(tool_name: str, args: dict) -> str:
                        """Execute a tool and return observation string."""
                        try:
                            if tool_name == "get_all_tasks":
                                return await tools.get_all_tasks(
                                    status_filter=str(args.get("status_filter") or ""),
                                    assignee_filter=str(args.get("assignee_filter") or ""),
                                    limit=int(args.get("limit") or 50),
                                )

                            elif tool_name == "get_person_info":
                                person_name = str(args.get("person_name") or args.get("name") or "")
                                return await tools.get_person_info(person_name=person_name)

                            elif tool_name == "get_project_status":
                                project_name = str(args.get("project_name") or "")
                                return await tools.get_project_status(project_name=project_name)

                            elif tool_name == "get_recent_meetings":
                                limit = int(args.get("limit") or 10)
                                return await tools.get_recent_meetings(limit=limit)

                            elif tool_name == "get_meeting_context":
                                meeting_id = str(args.get("meeting_id") or "")
                                title = str(args.get("title") or "")
                                return await tools.get_meeting_context(meeting_id=meeting_id, title=title)

                            elif tool_name == "search_knowledge":
                                query = str(args.get("query") or "")
                                return await tools.search_knowledge(query=query)

                            elif tool_name == "get_recent_decisions":
                                limit = int(args.get("limit") or 20)
                                return await tools.get_recent_decisions(limit=limit)

                            elif tool_name == "get_all_people":
                                limit = int(args.get("limit") or 50)
                                return await tools.get_all_people(limit=limit)

                            # === New Supabase Direct Access Tools ===
                            elif tool_name == "get_tasks_from_database":
                                status = str(args.get("status") or "")
                                limit = int(args.get("limit") or 20)
                                return await tools.get_tasks_from_database(status=status, limit=limit)

                            elif tool_name == "get_user_profile":
                                username = str(args.get("username") or "")
                                return await tools.get_user_profile(username=username)

                            elif tool_name == "get_team_overview":
                                return await tools.get_team_overview()

                            elif tool_name == "search_in_database":
                                query = str(args.get("query") or "")
                                table = str(args.get("table") or "tasks")
                                return await tools.search_in_database(query=query, table=table)

                            elif tool_name == "get_sprints_info":
                                status = str(args.get("status") or "")
                                return await tools.get_sprints_info(status=status)

                            # === MeetFlow Integration Tools ===
                            elif tool_name == "get_meetflow_meetings":
                                days_back = int(args.get("days_back") or 7)
                                return await tools.get_meetflow_meetings(days_back=days_back)

                            elif tool_name == "search_meetflow_meetings":
                                search_term = str(args.get("search_term") or "")
                                return await tools.search_meetflow_meetings(search_term=search_term)

                            elif tool_name == "get_meeting_details_meetflow":
                                meeting_id = str(args.get("meeting_id") or "")
                                return await tools.get_meeting_details_meetflow(meeting_id=meeting_id)

                            elif tool_name == "get_meeting_tasks_meetflow":
                                meeting_id = str(args.get("meeting_id") or "")
                                return await tools.get_meeting_tasks_meetflow(meeting_id=meeting_id)

                            elif tool_name == "get_yougile_tasks":
                                board_id = str(args.get("board_id") or "")
                                status = str(args.get("status") or "")
                                return await tools.get_yougile_tasks(board_id=board_id, status=status)

                            elif tool_name == "get_yougile_boards":
                                return await tools.get_yougile_boards()

                            elif tool_name == "create_yougile_task":
                                title = str(args.get("title") or "")
                                column_id = str(args.get("column_id") or "")
                                description = str(args.get("description") or "")
                                assignee = str(args.get("assignee") or "")
                                return await tools.create_yougile_task(
                                    title=title,
                                    column_id=column_id,
                                    description=description,
                                    assignee=assignee
                                )

                            elif tool_name == "list_calendar_events":
                                days_ahead = int(args.get("days_ahead") or 7)
                                return await tools.list_calendar_events(days_ahead=days_ahead)

                            elif tool_name == "analyze_with_llm":
                                # Fallback to Big LLM for analysis/generation
                                task = str(args.get("task") or "")
                                context = str(args.get("context") or "")

                                if not llm_router:
                                    return json.dumps({"error": "LLM Router not available"})

                                prompt = f"""Задача: {task}

Контекст/данные для анализа:
{context}

Выполни задачу и дай структурированный ответ."""

                                try:
                                    response = await llm_router.generate(
                                        prompt=prompt,
                                        max_tokens=2000,
                                    )
                                    return json.dumps({
                                        "success": True,
                                        "result": str(response),
                                    }, ensure_ascii=False)
                                except Exception as llm_err:
                                    return json.dumps({"error": f"LLM error: {llm_err!s}"})

                            elif tool_name in ("send_telegram", "send_telegram_message"):
                                # Telegram integration
                                message = str(args.get("message") or "")
                                chat_id = str(args.get("chat_id") or "")

                                # Try TessentBrainTools method first
                                return await tools.send_telegram_message(message=message, chat_id=chat_id)

                            elif tool_name == "_send_telegram_legacy":
                                # Legacy Telegram integration (kept for backward compatibility)
                                message = str(args.get("message") or "")
                                chat_id = args.get("chat_id")

                                try:
                                    from backend.core.automations.automation_tools_async import (
                                        send_telegram_message,
                                    )
                                    result = await send_telegram_message(
                                        message=message,
                                        chat_id=chat_id,
                                        user_id=user_id,
                                    )
                                    return json.dumps(result, ensure_ascii=False)
                                except Exception as tg_err:
                                    return json.dumps({"error": f"Telegram error: {tg_err!s}"})

                            elif tool_name == "create_task":
                                title = str(args.get("title") or "")
                                description = str(args.get("description") or "")
                                assignee = str(args.get("assignee") or "")
                                deadline = str(args.get("deadline") or "")

                                return await tools.add_task_to_graph(
                                    title=title,
                                    description=description,
                                    assignee=assignee,
                                    deadline=deadline,
                                )

                            # === Automations Management Tools ===
                            elif tool_name == "create_scheduled_automation":
                                try:
                                    from backend.core.automations.automation_service import (
                                        AutomationService,
                                    )

                                    auto_service = AutomationService()
                                    auto_name = str(args.get("name") or "Автоматизация из чата")
                                    action_type = str(args.get("action_type") or "custom_agent_task")
                                    action_data = args.get("action_data") or {}
                                    if isinstance(action_data, str):
                                        try:
                                            action_data = json.loads(action_data)
                                        except Exception:
                                            action_data = {"message": action_data}

                                    schedule_desc = str(args.get("schedule_description") or "")
                                    description = str(args.get("description") or "")

                                    # Парсим расписание
                                    schedule_params = _parse_schedule_description(schedule_desc) if schedule_desc else {
                                        "schedule_type": "once",
                                        "execute_at": (datetime.now(timezone.utc) + __import__("datetime").timedelta(hours=1)).isoformat(),
                                    }

                                    automation = await auto_service.create_automation(
                                        user_id=user_id or "",
                                        name=auto_name,
                                        action_type=action_type,
                                        action_data=action_data,
                                        schedule_type=schedule_params.get("schedule_type", "once"),
                                        execute_at=datetime.fromisoformat(schedule_params["execute_at"]) if schedule_params.get("execute_at") else None,
                                        cron_expression=schedule_params.get("cron_expression"),
                                        interval_seconds=schedule_params.get("interval_seconds"),
                                        description=description or f"Создано из Tess-чата: {schedule_desc}",
                                        source_message=original_message[:500] if original_message else "",
                                        session_id=str(session_id),
                                    )

                                    # Формируем красивый ответ
                                    sched_info = ""
                                    if schedule_params.get("cron_expression"):
                                        sched_info = f"Cron: `{schedule_params['cron_expression']}`"
                                    elif schedule_params.get("interval_seconds"):
                                        iv = schedule_params["interval_seconds"]
                                        if iv >= 3600:
                                            sched_info = f"Каждые {iv // 3600} ч."
                                        else:
                                            sched_info = f"Каждые {iv // 60} мин."
                                    elif schedule_params.get("execute_at"):
                                        sched_info = f"Выполнится: {schedule_params['execute_at'][:16]}"

                                    return json.dumps({
                                        "success": True,
                                        "automation_id": automation.id,
                                        "name": auto_name,
                                        "schedule_type": schedule_params.get("schedule_type"),
                                        "schedule_info": sched_info,
                                        "action_type": action_type,
                                        "message": f"✅ Автоматизация '{auto_name}' создана! {sched_info}",
                                    }, ensure_ascii=False)
                                except Exception as auto_err:
                                    return json.dumps({"error": f"Ошибка создания автоматизации: {auto_err}"}, ensure_ascii=False)

                            elif tool_name == "list_scheduled_automations":
                                try:
                                    from backend.core.automations.automation_service import (
                                        AutomationService,
                                    )

                                    auto_service = AutomationService()
                                    status_filter = str(args.get("status") or "")

                                    automations = await auto_service.list_automations(
                                        user_id=user_id or "",
                                        status=status_filter if status_filter else None,
                                        limit=20,
                                    )

                                    if not automations:
                                        return json.dumps({"success": True, "message": "У вас нет автоматизаций.", "automations": []}, ensure_ascii=False)

                                    items = []
                                    for a in automations:
                                        sched = ""
                                        if a.cron_expression:
                                            sched = f"cron: {a.cron_expression}"
                                        elif a.interval_seconds:
                                            sched = f"каждые {a.interval_seconds // 60} мин"
                                        elif a.execute_at:
                                            sched = f"разово: {a.execute_at[:16]}"
                                        items.append({
                                            "id": a.id[:8],
                                            "name": a.name,
                                            "status": a.status,
                                            "type": a.action_type,
                                            "schedule": sched,
                                            "runs": a.run_count,
                                        })

                                    return json.dumps({"success": True, "count": len(items), "automations": items}, ensure_ascii=False)
                                except Exception as list_err:
                                    return json.dumps({"error": f"Ошибка списка автоматизаций: {list_err}"}, ensure_ascii=False)

                            elif tool_name == "cancel_scheduled_automation":
                                try:
                                    from backend.core.automations.automation_service import (
                                        AutomationService,
                                    )

                                    auto_service = AutomationService()
                                    auto_id = str(args.get("automation_id") or "")
                                    auto_name_search = str(args.get("automation_name") or "")

                                    if not auto_id and auto_name_search:
                                        # Ищем по имени
                                        automations = await auto_service.list_automations(
                                            user_id=user_id or "",
                                            status="active",
                                        )
                                        for a in automations:
                                            if auto_name_search.lower() in a.name.lower():
                                                auto_id = a.id
                                                break

                                    if not auto_id:
                                        return json.dumps({"error": f"Автоматизация не найдена: {auto_name_search or auto_id}"}, ensure_ascii=False)

                                    result = await auto_service.cancel_automation(auto_id)
                                    if result:
                                        return json.dumps({"success": True, "message": f"✅ Автоматизация '{result.name}' отменена."}, ensure_ascii=False)
                                    else:
                                        return json.dumps({"error": f"Не удалось отменить автоматизацию {auto_id}"}, ensure_ascii=False)
                                except Exception as cancel_err:
                                    return json.dumps({"error": f"Ошибка отмены: {cancel_err}"}, ensure_ascii=False)

                            elif tool_name == "done":
                                # Special: just return the answer
                                return str(args.get("answer") or "Задача выполнена.")

                            else:
                                # === Делегируем в Integration Registry (Slack, Jira, Notion, Trello, Gmail, GitHub) ===
                                if integration_registry:
                                    registry_integration = integration_registry.find_integration_for_tool(tool_name)
                                    if registry_integration:
                                        logger.info(f"[Tess] Delegating '{tool_name}' to integration '{registry_integration.integration_id}'")
                                        return await integration_registry.execute_tool(tool_name, **args)

                                # === Fallback: попробовать создать integration на лету ===
                                # Gmail, Slack и др. tools могут быть в каталоге, но integration не загрузилась
                                # (нет credentials). Сообщаем об этом явно.
                                tool_prefix = tool_name.split("_")[0] if "_" in tool_name else ""
                                integration_map = {
                                    "gmail": "Gmail",
                                    "slack": "Slack",
                                    "jira": "Jira",
                                    "trello": "Trello",
                                    "notion": "Notion",
                                    "github": "GitHub",
                                    "sheets": "Google Sheets",
                                    "docs": "Google Docs",
                                    "drive": "Google Drive",
                                    "hubspot": "HubSpot",
                                    "whatsapp": "WhatsApp",
                                    "linear": "Linear",
                                    "confluence": "Confluence",
                                    "figma": "Figma",
                                }
                                if tool_prefix in integration_map:
                                    svc_name = integration_map[tool_prefix]
                                    return json.dumps({
                                        "error": f"❌ {svc_name} не подключён. Настройте интеграцию с {svc_name} в разделе 'Интеграции' (добавьте API-ключ/OAuth токен).",
                                        "tool": tool_name,
                                        "integration_required": tool_prefix,
                                    }, ensure_ascii=False)

                                return json.dumps({"error": f"Unknown tool: {tool_name}"})

                        except Exception as e:
                            logger.error(f"Tool execution error ({tool_name}): {e}")
                            return json.dumps({"error": str(e)})

                    # Check if ReAct mode is enabled (default: single-shot for simplicity)
                    use_react = data.context.get("use_react", False)

                    # Auto-detect if user wants to send result to Telegram
                    # ВАЖНО: паттерны должны точно матчить НАМЕРЕНИЕ отправки,
                    # а не просто упоминание Telegram. "в тг-группе" != "отправь в тг"
                    msg_lower = data.message.lower()

                    # Позитивные паттерны — чёткое намерение ОТПРАВИТЬ
                    tg_send_patterns = [
                        # Русский
                        "отправь в телеграм", "отправь в тг",
                        "пошли в тг", "пошли в телеграм",
                        "скинь в тг", "скинь в телеграм",
                        "кинь в тг", "кинь в телеграм",
                        "напиши в тг", "напиши в телеграм",
                        # English
                        "send to telegram", "send telegram", "send to tg",
                        "post to telegram", "forward to telegram",
                        "share to telegram", "share via telegram",
                        "message on telegram", "notify on telegram",
                    ]
                    # Паттерны-комбинации "сделай X И отправь / do X and send"
                    tg_combo_patterns = [
                        # Русский
                        " и отправь в т", " и пошли в т", " и скинь в т",
                        " и отправь в telegram", " и пошли в telegram",
                        # English
                        " and send to t", " and forward to t",
                        " and share to t", " and post to t",
                        " then send to t",
                    ]
                    # Негативные паттерны — упоминание TG как источника, не как цели
                    tg_exclude_patterns = [
                        # Русский
                        "в тг-групп", "в тг групп", "из тг", "из телеграм",
                        "в телеграм-групп", "в телеграм групп",
                        "тг-канал", "телеграм-канал",
                        "что в тг", "что в телеграм",
                        "из тг-", "тг-бот", "телеграм-бот",
                        # English
                        "telegram group", "telegram channel", "telegram bot",
                        "from telegram", "in telegram group", "tg group",
                        "tg channel", "tg bot", "from tg",
                        "what's in telegram", "what in telegram",
                    ]

                    has_exclude = any(p in msg_lower for p in tg_exclude_patterns)
                    has_send = any(p in msg_lower for p in tg_send_patterns)
                    has_combo = any(p in msg_lower for p in tg_combo_patterns)

                    send_to_telegram = (has_send or has_combo) and not has_exclude

                    # Auto-detect complex multi-step tasks that need ReAct
                    # Включаем ReAct если запрос содержит 2+ действия или явную цепочку
                    multi_action_keywords = [
                        # Русский — цепочки действий
                        "затем отправ", "потом отправ", "после этого отправ",
                        " и создай", " и добавь", " и запиши",
                        " и отправ", " и пошли", " и скинь",
                        "достань", "достать", "извлеки",  # Требует 2 шага: найти → извлечь
                        "из встречи отправ", "оттуда отправ",  # Явная цепочка
                        # Русский — "отправь X на почту/в тг" где X требует поиска (встречу, задачу, отчёт)
                        "отправь мне на почту", "отправь на почту",
                        "пошли мне на почту", "пошли на почту",
                        "скинь мне на почту", "скинь на почту",
                        "отправь мне по email", "пошли по email",
                        # Русский — множественные адресаты
                        " на почту и ", " по email и ",
                        " в тг и ", " в телеграм и ",
                        " тому-то ", " такому-то ",
                        # Русский — отложенные действия (→ тоже multi-step: создать автоматизацию)
                        "через час", "через 2 час", "через 3 час",
                        "через минут", "через полчаса",
                        "завтра утром", "завтра в ", "сегодня в ",
                        # English
                        "then send", "then create", "then add",
                        " and create", " and add", " and write",
                        " and send", " and forward", " and share",
                        "after that send", "afterwards send",
                        "extract from", "get from meeting",
                        "send me by email", "send to email", "email me",
                        "in 1 hour", "in 2 hours", "in 3 hours",
                        "tomorrow at", "today at", "in 30 min",
                    ]
                    if any(kw in msg_lower for kw in multi_action_keywords):
                        use_react = True
                        logger.info(f"[Tess] Auto-enabled ReAct for multi-step task: {data.message[:50]}...")

                    # Дополнительная проверка: если упомянуто 2+ разных сервиса — точно multi-step
                    service_markers = [
                        (["телеграм", " тг ", "telegram"], "tg"),
                        (["почт", "email", "gmail", "на почту"], "email"),
                        (["слак", "slack"], "slack"),
                        (["jira", "джир"], "jira"),
                        (["trello", "трелло"], "trello"),
                        (["notion", "ноушен"], "notion"),
                        (["github", "гитхаб"], "github"),
                    ]
                    seen_services = set()
                    for markers, svc_name in service_markers:
                        if any(m in msg_lower for m in markers):
                            seen_services.add(svc_name)
                    if len(seen_services) >= 2:
                        use_react = True
                        logger.info(f"[Tess] Auto-enabled ReAct: {len(seen_services)} services mentioned: {seen_services}")

                    # === Temporal modifier detection ===
                    # "через 2 часа", "завтра в 9:00", "in 2 hours" → создаём отложенную автоматизацию
                    delayed_seconds = _parse_time_delay(msg_lower)

                    # === Build unified tools catalog (static + integration) ===
                    from backend.core.local_models.ollama_functiongemma import (
                        LocalRouteResult,
                        route_with_functiongemma,
                    )

                    # ВАЖНО: передаём ПОЛНЫЙ каталог (без фильтрации по UI checkboxes).
                    # SkillRouter сам сужает tools по смыслу запроса на шаге 1,
                    # а на follow-up шагах ReAct нужен полный набор для мульти-шаговых задач
                    # (e.g., шаг 1: get_recent_meetings, шаг 2: gmail_send).
                    tools_catalog = get_tools_catalog(None)

                    # Добавляем tools из Integration Registry в каталог FunctionGemma
                    if integration_registry:
                        existing_names = {t["name"] for t in tools_catalog}
                        for tool_def in integration_registry.get_all_tool_definitions():
                            if tool_def["name"] not in existing_names:
                                tools_catalog.append({
                                    "name": tool_def["name"],
                                    "description": tool_def.get("description", ""),
                                    "parameters": tool_def.get("parameters", {"type": "object", "properties": {}}),
                                })

                    if use_react:
                        max_react_steps = 8 if delayed_seconds else 5

                        # Multi-step tasks: Big LLM orchestrates, FunctionGemma does step 1 routing
                        # FunctionGemma (270M) can't reason over conversation history,
                        # so Big LLM (Gemini/GPT) handles steps 2+ with full reasoning.
                        logger.info(f"[Tess] ReAct mode: llm_router={'available' if llm_router else 'None'}")
                        if llm_router:
                            from backend.core.local_models.ollama_functiongemma import (
                                run_react_loop_bigllm,
                            )
                            react_result: ReActResult = await run_react_loop_bigllm(
                                user_message=data.message,
                                tool_executor=execute_tool,
                                llm_router=llm_router,
                                tools=tools_catalog,
                                max_steps=max_react_steps,
                                model_tier=data.model_tier or "standard",
                                user_email=getattr(data, "user_email", "") or "",
                            )
                        else:
                            # Fallback: FunctionGemma-only ReAct (may fail on multi-step)
                            react_result: ReActResult = await run_react_loop(
                                user_message=data.message,
                                tool_executor=execute_tool,
                                allowed_tool_names=None,
                                max_steps=max_react_steps,
                                ollama_model=ollama_model,
                                ollama_base_url=ollama_base_url,
                                tools=tools_catalog,
                            )
                    else:
                        # Single-shot mode: one tool call, one result (default)

                        route_result: LocalRouteResult = await route_with_functiongemma(
                            user_message=data.message,
                            tools=tools_catalog,
                            allowed_tool_names=allowed_tools,  # Pass for hard rules
                            ollama_model=ollama_model,
                            ollama_base_url=ollama_base_url,
                        )

                        # Convert single result to ReActResult format for consistent handling
                        from backend.core.local_models.ollama_functiongemma import (
                            ReActResult,
                            ReActStep,
                        )

                        if route_result.tool and route_result.tool != "none":
                            # Execute the selected tool
                            observation = await execute_tool(route_result.tool, route_result.args)

                            steps = [ReActStep(
                                thought=route_result.reason,
                                tool=route_result.tool,
                                args=route_result.args,
                                observation=observation,
                                is_final=not send_to_telegram,  # Not final if we need to send to TG
                            )]

                            # If user wanted to send to Telegram, do it automatically
                            telegram_result = None
                            if send_to_telegram and observation:
                                try:
                                    # Format the observation nicely for Telegram
                                    pretty = _pretty_from_observation(route_result.tool, observation)
                                    tg_message = pretty if pretty else observation[:1000]

                                    telegram_result = await tools.send_telegram_message(
                                        message=tg_message,
                                        chat_id=""
                                    )
                                    steps.append(ReActStep(
                                        thought="User asked to send to Telegram",
                                        tool="send_telegram_message",
                                        args={"message": tg_message[:100] + "..."},
                                        observation=telegram_result,
                                        is_final=True,
                                    ))
                                    logger.info(f"[Tess] Auto-sent to Telegram: {telegram_result}")
                                except Exception as tg_err:
                                    logger.warning(f"[Tess] Failed to send to Telegram: {tg_err}")
                                    telegram_result = json.dumps({"error": str(tg_err)})

                            react_result = ReActResult(
                                steps=steps,
                                final_answer=telegram_result if telegram_result else observation,
                                total_input_tokens=route_result.prompt_eval_count,
                                total_output_tokens=route_result.eval_count,
                                success=True,
                            )
                        else:
                            # No tool selected
                            react_result = ReActResult(
                                steps=[ReActStep(
                                    thought=route_result.reason,
                                    tool="none",
                                    args={},
                                    observation="",
                                    is_final=True,
                                )],
                                final_answer=route_result.reason or "FunctionGemma не выбрала инструмент",
                                total_input_tokens=route_result.prompt_eval_count,
                                total_output_tokens=route_result.eval_count,
                                success=False,
                            )

                    # Fallback: if FunctionGemma returned "none", try with Big LLM
                    if (not react_result.success or
                        (react_result.steps and react_result.steps[0].tool == "none") or
                        "не удалось" in react_result.final_answer.lower()):

                        logger.info("FunctionGemma returned 'none', trying Big LLM fallback...")

                        # Use Gemini to determine the right tool
                        if llm_router:
                            try:
                                from backend.core.local_models.ollama_functiongemma import (
                                    get_tools_catalog,
                                )
                                tools_list = get_tools_catalog(allowed_tools)
                                # Добавляем tools из Integration Registry для Big LLM fallback
                                if integration_registry:
                                    for tool_def in integration_registry.get_all_tool_definitions():
                                        tools_list.append({
                                            "name": tool_def["name"],
                                            "description": tool_def.get("description", ""),
                                            "parameters": tool_def.get("parameters", {"type": "object", "properties": {}}),
                                        })
                                tools_desc = "\n".join([
                                    f"- {t['name']}: {t['description']}"
                                    for t in tools_list
                                ])

                                fallback_prompt = f"""Ты — диспетчер инструментов. Пользователь спросил: "{data.message}"

Доступные инструменты:
{tools_desc}

Выбери ОДИН инструмент и верни JSON:
{{"tool": "имя_инструмента", "args": {{"параметр": "значение"}}, "reason": "почему"}}

Если запрос про задачи — используй get_all_tasks.
Если запрос про человека — используй get_person_info.
Если запрос про встречи — используй get_recent_meetings.

Ответь ТОЛЬКО JSON, без объяснений."""

                                response_text = await llm_router.generate(
                                    prompt=fallback_prompt,
                                    max_tokens=200,
                                    temperature=0.2,
                                )

                                # Parse JSON from response
                                import re
                                json_match = re.search(r'\{[\s\S]*\}', response_text)
                                if json_match:
                                    parsed = json.loads(json_match.group(0))
                                    fallback_tool = parsed.get("tool", "none")
                                    fallback_args = parsed.get("args", {})

                                    if fallback_tool != "none":
                                        # Execute the tool
                                        observation = await execute_tool(fallback_tool, fallback_args)

                                        # Session Memory: save fallback result too
                                        try:
                                            auto_session.add_turn(
                                                user_message=original_message,
                                                tool_name=fallback_tool,
                                                tool_args=fallback_args,
                                                observation=observation,
                                                pretty_result=_pretty_from_observation(fallback_tool, observation) or "",
                                            )
                                        except Exception:
                                            logger.debug("suppressed exception", exc_info=True)

                                        execution_time = int((datetime.now() - start_time).total_seconds() * 1000)

                                        return AgentChatResponse(
                                            success=True,
                                            message=(
                                                f"🧩 **Tess (Fallback LLM)** → `{fallback_tool}`\n\n"
                                                f"📊 **Результат:**\n{observation}"
                                            ),
                                            session_id=str(session_id),
                                            agent_mode="automation",
                                            agents_involved=["FunctionGemma(local)", "Gemini(fallback)", "TessentBrainTools"],
                                            execution_time_ms=execution_time,
                                            sources=[{"tool": fallback_tool, "args": fallback_args, "fallback": True}],
                                        )
                            except Exception as fallback_err:
                                logger.warning(f"Fallback LLM error: {fallback_err}")

                    execution_time = int((datetime.now() - start_time).total_seconds() * 1000)

                    # Track local usage
                    track_usage(
                        provider="local",
                        model="ollama/functiongemma",
                        input_tokens=react_result.total_input_tokens,
                        output_tokens=react_result.total_output_tokens,
                        user_id=user_id,
                        session_id=session_id,
                        agent_mode="automation",
                        request_type="tess_react",
                        success=react_result.success,
                        latency_ms=execution_time,
                        model_tier=data.model_tier,
                    )

                    # Build steps text (short, debug-ish)
                    steps_text = ""
                    for i, step in enumerate(react_result.steps, 1):
                        steps_text += f"\n**Шаг {i}:** `{step.tool}`"
                        if step.args:
                            args_str = ", ".join(f"{k}={v}" for k, v in step.args.items())
                            steps_text += f" ({args_str})"
                        # Keep thoughts/observations out of the main UI by default (they are noisy)

                    if react_result.success:
                        main_tool = react_result.steps[-1].tool if react_result.steps else ""
                        # Prefer final_answer for formatting (single-shot sets it to observation)
                        observation = react_result.final_answer or (react_result.steps[-1].observation if react_result.steps else "")
                        logger.info(f"[Tess Pretty] tool={main_tool}, observation_type={type(observation).__name__}, len={len(str(observation)) if observation else 0}")
                        pretty = _pretty_from_observation(main_tool, observation)
                        logger.info(f"[Tess Pretty] pretty result: {pretty[:200] if pretty else 'None'}")
                        final_text = pretty if pretty else observation

                        # === Temporal delay: если "через 2 часа" — создаём отложенную автоматизацию ===
                        if delayed_seconds > 0:
                            try:
                                from datetime import timedelta
                                from datetime import timezone as tz

                                from backend.core.automations.automation_service import (
                                    ActionType,
                                    AutomationService,
                                )

                                automation_service = AutomationService()
                                execute_at = datetime.now(tz.utc) + timedelta(seconds=delayed_seconds)

                                # Определяем что именно нужно отправить и куда
                                # Собираем результаты из ReAct шагов как payload
                                collected_data = observation[:3000]  # Ограничиваем размер

                                # Определяем тип действия из последних шагов
                                # (что ReAct пытался сделать на последних шагах)
                                delayed_actions = []
                                for step in react_result.steps:
                                    if step.tool in ("send_telegram_message", "send_telegram"):
                                        delayed_actions.append({
                                            "action_type": ActionType.SEND_TELEGRAM.value,
                                            "action_data": {
                                                "message": step.args.get("message", collected_data),
                                                "chat_id": step.args.get("chat_id", ""),
                                            }
                                        })
                                    elif step.tool == "gmail_send":
                                        delayed_actions.append({
                                            "action_type": ActionType.SEND_GMAIL.value,
                                            "action_data": {
                                                "to": step.args.get("to", ""),
                                                "subject": step.args.get("subject", ""),
                                                "body": step.args.get("body", collected_data),
                                            }
                                        })
                                    elif step.tool == "slack_send_message":
                                        delayed_actions.append({
                                            "action_type": ActionType.SEND_SLACK.value,
                                            "action_data": {
                                                "channel": step.args.get("channel", ""),
                                                "text": step.args.get("text", collected_data),
                                            }
                                        })

                                # Если не нашли конкретных send-шагов — делаем универсальную автоматизацию
                                if not delayed_actions:
                                    delayed_actions.append({
                                        "action_type": ActionType.INTEGRATION_TOOL.value,
                                        "action_data": {
                                            "tool_name": main_tool,
                                            "tool_args": react_result.steps[-1].args if react_result.steps else {},
                                        }
                                    })

                                delay_desc = f"{delayed_seconds // 3600}ч" if delayed_seconds >= 3600 else f"{delayed_seconds // 60}мин"
                                created_automations = []

                                for i, action in enumerate(delayed_actions):
                                    auto_name = f"Отложено ({delay_desc}): {action['action_type']}"
                                    # (фикс аудита) create_automation принимает kwargs,
                                    # а execute_at — datetime (не isoformat-строку)
                                    await automation_service.create_automation(
                                        user_id=user_id or "",
                                        name=auto_name,
                                        action_type=action["action_type"],
                                        action_data=action["action_data"],
                                        schedule_type="once",
                                        execute_at=execute_at,
                                        description=f"Создано из Tess: {original_message[:200]}",
                                        source_message=original_message[:500],
                                    )
                                    created_automations.append(auto_name)

                                final_text = (
                                    f"📋 Данные получены.\n\n"
                                    f"⏰ **Отложено на {delay_desc}** (выполнится в {execute_at.strftime('%H:%M UTC')}):\n"
                                    + "\n".join(f"  - {a}" for a in created_automations)
                                    + "\n\n💡 Посмотреть или отменить: спроси «мои автоматизации» или открой вкладку «Автоматизации» (там же отмена и история запусков)."
                                    + f"\n\n---\n{final_text}"
                                )
                                logger.info(f"[Tess] Created {len(created_automations)} delayed automations (delay={delay_desc})")

                            except Exception as delay_err:
                                logger.warning(f"[Tess] Failed to create delayed automation: {delay_err}")
                                final_text = f"⚠️ Не удалось создать отложенное выполнение: {delay_err}\n\n{final_text}"

                        # Session Memory: сохраняем результат для будущих запросов
                        try:
                            auto_session.add_turn(
                                user_message=original_message,
                                tool_name=main_tool,
                                tool_args=react_result.steps[-1].args if react_result.steps else {},
                                observation=observation,
                                pretty_result=pretty or "",
                            )
                        except Exception as sess_err:
                            logger.warning(f"[Tess] Failed to save session turn: {sess_err}")

                        return AgentChatResponse(
                            success=True,
                            message=(
                                f"**Tess** — выполнено за {len(react_result.steps)} шаг(а)\n"
                                f"{steps_text}\n\n"
                                f"---\n{final_text}"
                            ),
                            session_id=str(session_id),
                            agent_mode="automation",
                            agents_involved=["FunctionGemma(local)", "TessentBrainTools", "ReActLoop"],
                            execution_time_ms=execution_time,
                            sources=[{"step": i, "tool": s.tool, "args": s.args} for i, s in enumerate(react_result.steps, 1)],
                        )
                    else:
                        return AgentChatResponse(
                            success=False,
                            message=(
                                f"Ошибка Tess (ReAct)\n\n"
                                f"Шаги: {steps_text}\n\n"
                                f"Ошибка: {react_result.error}"
                            ),
                            session_id=str(session_id),
                            agent_mode="automation",
                            agents_involved=["FunctionGemma(local)", "TessRouter"],
                            execution_time_ms=execution_time,
                        )

                except httpx.ConnectError as e:
                    logger.error(f"Ollama connection error: {e}")
                    return AgentChatResponse(
                        success=False,
                        message=(
                            "❌ Не удалось подключиться к Ollama.\n\n"
                            "Проверь:\n"
                            "1. Ollama запущен: `ollama serve`\n"
                            "2. Модель скачана: `ollama pull functiongemma`\n"
                            "3. OLLAMA_HOST (по умолчанию http://localhost:11434)\n"
                            f"\nОшибка: {e!s}"
                        ),
                        session_id=str(session_id),
                        agent_mode="automation",
                    )
                except Exception as e:
                    logger.exception(f"Tess ReAct error: {e}")
                    return AgentChatResponse(
                        success=False,
                        message=(
                            "❌ Ошибка Tess (local FunctionGemma + ReAct).\n\n"
                            "Проверь:\n"
                            "- Ollama запущен\n"
                            "- `ollama pull functiongemma`\n"
                            "- OLLAMA_HOST (если нужно)\n"
                            f"\nОшибка: {e!s}"
                        ),
                        session_id=str(session_id),
                        agent_mode="automation",
                    )

            # ══════════════════════════════════════════════════════════════
            # Hybrid mode: FunctionGemma (step 1, fast) + Big LLM (steps 2+, smart)
            # This is a DEDICATED tab so the user can compare:
            #   - "Задачи" (MeetFlow AutoGen agents, Gemini-only, multi-agent)
            #   - "Tess" (FunctionGemma ReAct, can fall back to BigLLM if router exists)
            #   - "Hybrid" (ALWAYS BigLLM ReAct: FunctionGemma step1 + Gemini steps 2+)
            # ══════════════════════════════════════════════════════════════
            if automation_mode == "hybrid":
                try:
                    from backend.core.auto.session_memory import get_auto_session_store
                    from backend.core.llm.router import LLMRouter
                    from backend.core.local_models.ollama_functiongemma import (
                        ReActResult,
                        get_tools_catalog,
                        run_react_loop_bigllm,
                    )
                    from backend.integrations.tessent_brain_tools import TessentBrainTools

                    # --- Session Memory ---
                    auto_session_store = get_auto_session_store()
                    auto_session = auto_session_store.get_or_create(
                        session_id=str(session_id),
                        user_id=user_id or "",
                    )
                    original_message = data.message
                    enriched_message, was_enriched = auto_session.enrich_message(data.message)
                    if was_enriched:
                        logger.info(f"[Hybrid] Session memory enriched message: '{data.message[:50]}...'")
                        data.message = enriched_message

                    # --- LLM Router (обязательный для Hybrid) ---
                    hybrid_llm_router = LLMRouter()
                    logger.info("[Hybrid] LLMRouter created for BigLLM ReAct orchestration")

                    # --- Ollama settings ---
                    ollama_model = os.getenv("OLLAMA_MODEL", "functiongemma")
                    ollama_base_url = os.getenv("OLLAMA_HOST", "http://localhost:11434")

                    # --- TessentBrainTools ---
                    graph_builder = getattr(state, "graph", None)
                    vector_indexer = getattr(state, "vector_indexer", None)

                    hybrid_reasoning = None
                    if hybrid_llm_router and graph_builder:
                        try:
                            from backend.core.think.reasoning_engine import ReasoningEngine
                            hybrid_reasoning = ReasoningEngine(
                                graph_builder=graph_builder,
                                vector_indexer=vector_indexer,
                                llm_router=hybrid_llm_router,
                            )
                        except Exception:
                            logger.debug("suppressed exception", exc_info=True)

                    from backend.integrations.tessent_brain_tools import (
                        build_enhanced_search,
                    )
                    tools = TessentBrainTools(
                        graph_builder=graph_builder,
                        reasoning_engine=hybrid_reasoning,
                        vector_indexer=vector_indexer,
                        user_id=user_id,
                        # см. комментарий у первого сайта (SEARCH_CONSUMERS_AUDIT)
                        enhanced_search=build_enhanced_search(
                            graph_builder=graph_builder,
                            vector_indexer=vector_indexer,
                            llm_router=hybrid_llm_router,
                            user_id=user_id,
                        ),
                    )

                    # --- Integration Registry ---
                    integration_registry = None
                    try:
                        from backend.integrations.registry import IntegrationRegistry
                        integration_registry = IntegrationRegistry()
                        await integration_registry.load_for_user(user_id or "")
                        loaded = integration_registry.list_loaded()
                        if loaded:
                            logger.info(f"[Hybrid] Integration Registry loaded: {[i['id'] for i in loaded]}")
                    except Exception as int_err:
                        logger.warning(f"[Hybrid] IntegrationRegistry load failed: {int_err}")

                    # --- Build full tools catalog ---
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

                    # --- Tool executor ---
                    async def hybrid_execute_tool(tool_name: str, args: dict) -> str:
                        """Execute a tool by name + args, return observation string."""
                        try:
                            # 1. TessentBrainTools first (built-in tools)
                            method = getattr(tools, tool_name, None)
                            if method and callable(method):
                                if asyncio.iscoroutinefunction(method):
                                    result = await method(**args)
                                else:
                                    result = method(**args)
                                return json.dumps(result, ensure_ascii=False, default=str) if not isinstance(result, str) else result

                            # 2. IntegrationRegistry (gmail, slack, jira, trello, etc.)
                            if integration_registry:
                                integration = integration_registry.find_integration_for_tool(tool_name)
                                if integration:
                                    result = await integration.execute(tool_name, **args)
                                    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)

                            # 3. Fallback: gmail_send / send_telegram via automation_tools_async
                            if tool_name == "gmail_send":
                                try:
                                    # (фикс аудита) локальный пересчёт пути был на уровень короче
                                    # и попадал в несуществующий tessent_brain/alfa_asynk_meetflow —
                                    # используем модульный MEETFLOW_PATH (автоопределение соседа)
                                    if MEETFLOW_PATH not in sys.path:
                                        sys.path.insert(0, MEETFLOW_PATH)
                                    from automation_tools_async import send_email
                                    to_addr = args.get("to", "me")
                                    if to_addr == "me":
                                        resolved = os.getenv("USER_EMAIL", "")
                                        if not resolved and user_id:
                                            try:
                                                from backend.db.supabase_client import (
                                                    SupabaseClient,
                                                )
                                                supa = SupabaseClient()
                                                users = await supa._request("GET", "/rest/v1/users", params={"id": f"eq.{user_id}", "select": "email"})
                                                if users and users[0].get("email"):
                                                    resolved = users[0]["email"]
                                            except Exception:
                                                logger.debug("suppressed exception", exc_info=True)
                                        if resolved:
                                            to_addr = resolved
                                    logger.info(f"[Hybrid] gmail_send fallback: to={to_addr}, subject={args.get('subject', '')[:50]}")
                                    result = await send_email(
                                        to=to_addr,
                                        subject=args.get("subject", "Tessbrain — результат задачи"),
                                        body=args.get("body", "")
                                    )
                                    logger.info(f"[Hybrid] gmail_send fallback result: {result[:200]}")
                                    return result
                                except Exception as mail_err:
                                    logger.error(f"[Hybrid] gmail_send fallback error: {mail_err}")
                                    return json.dumps({"error": f"Email send error: {mail_err!s}"})

                            if tool_name in ("send_telegram_message", "send_telegram"):
                                try:
                                    # (фикс аудита) локальный пересчёт пути был на уровень короче
                                    # и попадал в несуществующий tessent_brain/alfa_asynk_meetflow —
                                    # используем модульный MEETFLOW_PATH (автоопределение соседа)
                                    if MEETFLOW_PATH not in sys.path:
                                        sys.path.insert(0, MEETFLOW_PATH)
                                    from automation_tools_async import (
                                        send_telegram_message as send_telegram,
                                    )
                                    result = await send_telegram(message=args.get("message", ""))
                                    logger.info(f"[Hybrid] send_telegram fallback result: {result[:200]}")
                                    return result
                                except Exception as tg_err:
                                    logger.error(f"[Hybrid] send_telegram fallback error: {tg_err}")
                                    return json.dumps({"error": f"Telegram send error: {tg_err!s}"})

                            # 4. analyze_with_llm
                            if tool_name == "analyze_with_llm":
                                task = str(args.get("task") or args.get("query") or "")
                                context = str(args.get("context") or "")
                                prompt = f"Задача: {task}\n\nКонтекст/данные для анализа:\n{context[:3000]}\n\nВыполни задачу и дай структурированный ответ."
                                try:
                                    response = await hybrid_llm_router.generate(prompt=prompt, max_tokens=2000)
                                    return json.dumps({"success": True, "result": str(response)}, ensure_ascii=False)
                                except Exception as llm_err:
                                    return json.dumps({"error": f"LLM error: {llm_err!s}"})

                            return json.dumps({"error": f"Tool '{tool_name}' not found. Check tool name."})
                        except Exception as e:
                            logger.error(f"[Hybrid] Tool execution error ({tool_name}): {e}")
                            return json.dumps({"error": str(e)})

                    # --- Temporal delay ---
                    delayed_seconds = _parse_time_delay(data.message.lower())
                    # Гибрид = БЫСТРЫЙ агент: короткий процесс (найти → при
                    # необходимости уточнить → отправить), а не длинная
                    # петля — для сложных многошаговых задач есть agent_oc.
                    try:
                        _hyb_cap = max(2, int(os.getenv("TESS_HYBRID_MAX_STEPS", "4")))
                    except (TypeError, ValueError):
                        _hyb_cap = 4
                    # СОСТАВНАЯ задача «найди X → создай/отправь Y» (встреча →
                    # задача → задачник → ответ) занимает 4-5 tool-вызовов —
                    # даём +2 шага, простые запросы остаются короткими.
                    _msg_l = data.message.lower()
                    _compound = (
                        any(k in _msg_l for k in ("найд", "достан", "извлек",
                                                  "из встреч", "последн",
                                                  "посмотри", "выбери"))
                        and any(k in _msg_l for k in ("отправ", "созда",
                                                      "постав", "добав",
                                                      "заведи", "запиши",
                                                      "перенеси"))
                    )
                    max_react_steps = (_hyb_cap + 2
                                       if (delayed_seconds or _compound)
                                       else _hyb_cap)

                    # МГНОВЕННОЕ отложенное напоминание: «через 2 часа отправь
                    # напоминание о созвоне» не требует данных — сразу
                    # автоматизация, без бесплодного поиска встреч (лог юзера:
                    # 5 шагов впустую). Данные нужны («найди/саммари/задачи»)
                    # → обычный путь: собрать сейчас, отправить потом.
                    _needs_data = any(k in _msg_l for k in (
                        "найди", "саммари", "задач", "данн", "отчёт", "отчет",
                        "список", "выгрузи", "собери"))
                    if delayed_seconds and not _needs_data and any(
                            k in _msg_l for k in ("напомин", "отправ", "сообщи", "напиши")):
                        try:
                            from datetime import timedelta
                            from datetime import timezone as _tz

                            from backend.core.automations.automation_service import (
                                ActionType,
                                AutomationService,
                            )
                            _asvc = AutomationService()
                            _exec_at = datetime.now(_tz.utc) + timedelta(seconds=delayed_seconds)
                            _dd = (f"{delayed_seconds // 3600}ч" if delayed_seconds >= 3600
                                   else f"{delayed_seconds // 60}мин")
                            # СУТЬ напоминания, а не вся команда (общий хелпер
                            # _extract_reminder_text: режет темпоральную часть
                            # в любом месте фразы, глагол отправки, слово
                            # «напоминание/напомни»).
                            _txt = _extract_reminder_text(data.message)
                            _reminder = f"🔔 {_txt}"
                            # Проверяем ЗАРАНЕЕ, есть ли куда слать — иначе
                            # автоматизация упадёт «No recipients specified»,
                            # а пользователь не поймёт почему.
                            _tg_ok = await _has_telegram_recipient(user_id)
                            await _asvc.create_automation(
                                user_id=user_id or "",
                                name=(_txt[:50] or "Напоминание"),
                                action_type=ActionType.SEND_TELEGRAM.value,
                                action_data={"message": _reminder, "chat_id": ""},
                                schedule_type="once",
                                execute_at=_exec_at,
                                description=f"Напоминание через {_dd}",
                                source_message=data.message[:500],
                            )
                            if _tg_ok:
                                _resp = (f"⏰ Готово! Через {_dd} (в "
                                         f"{_exec_at.strftime('%H:%M UTC')}) пришлю "
                                         f"в Telegram:\n«{_reminder}»\n\n"
                                         "💡 Посмотреть или отменить — вкладка "
                                         "«Автоматизации».")
                            else:
                                _resp = (f"⏰ Напоминание запланировано через "
                                         f"{_dd}: «{_reminder}».\n\n"
                                         "⚠️ Но Telegram пока не подключён — мне "
                                         "некуда его отправить. Подключите Telegram "
                                         "в разделе «Интеграции» (там же выбирается "
                                         "чат), и напоминание уйдёт. Управление — "
                                         "вкладка «Автоматизации».")
                            return AgentChatResponse(
                                success=True, message=_resp,
                                session_id=str(session_id), agent_mode="automation",
                                agents_involved=["Scheduler"])
                        except Exception as _rem_err:
                            logger.warning(f"[Hybrid] instant reminder failed: {_rem_err}")

                    logger.info(f"[Hybrid] Starting BigLLM ReAct: message='{data.message[:60]}...', tools={len(tools_catalog)}, max_steps={max_react_steps}")

                    # --- Run BigLLM ReAct ---
                    react_result: ReActResult = await run_react_loop_bigllm(
                        user_message=data.message,
                        tool_executor=hybrid_execute_tool,
                        llm_router=hybrid_llm_router,
                        tools=tools_catalog,
                        max_steps=max_react_steps,
                        model_tier=data.model_tier or "standard",
                        user_email=getattr(data, "user_email", "") or "",
                    )

                    execution_time = int((datetime.now() - start_time).total_seconds() * 1000)

                    # --- Track usage ---
                    track_usage(
                        provider="hybrid",
                        model="functiongemma+gemini",
                        input_tokens=react_result.total_input_tokens,
                        output_tokens=react_result.total_output_tokens,
                        user_id=user_id,
                        session_id=session_id,
                        agent_mode="automation",
                        request_type="hybrid_react",
                        success=react_result.success,
                        latency_ms=execution_time,
                        model_tier=data.model_tier,
                    )

                    # --- Build steps text (user-friendly, no model names) ---
                    steps_text = ""
                    for i, step in enumerate(react_result.steps, 1):
                        steps_text += f"\n**Шаг {i}:** `{step.tool}`"
                        if step.args:
                            args_str = ", ".join(f"{k}={v}" for k, v in list(step.args.items())[:3])
                            steps_text += f" ({args_str})"

                    if react_result.success:
                        main_tool = react_result.steps[-1].tool if react_result.steps else ""
                        observation = react_result.final_answer or (react_result.steps[-1].observation if react_result.steps else "")
                        pretty = _pretty_from_observation(main_tool, observation)
                        final_text = pretty if pretty else observation

                        # Temporal delay handling (same as Tess)
                        if delayed_seconds > 0:
                            try:
                                from datetime import timedelta
                                from datetime import timezone as tz

                                from backend.core.automations.automation_service import (
                                    ActionType,
                                    AutomationService,
                                )
                                automation_service = AutomationService()
                                execute_at = datetime.now(tz.utc) + timedelta(seconds=delayed_seconds)
                                collected_data = observation[:3000]
                                delayed_actions = []
                                for step in react_result.steps:
                                    if step.tool in ("send_telegram_message", "send_telegram"):
                                        delayed_actions.append({"action_type": ActionType.SEND_TELEGRAM.value, "action_data": {"message": step.args.get("message", collected_data), "chat_id": step.args.get("chat_id", "")}})
                                    elif step.tool == "gmail_send":
                                        delayed_actions.append({"action_type": ActionType.SEND_GMAIL.value, "action_data": {"to": step.args.get("to", ""), "subject": step.args.get("subject", ""), "body": step.args.get("body", collected_data)}})
                                if not delayed_actions:
                                    delayed_actions.append({"action_type": ActionType.INTEGRATION_TOOL.value, "action_data": {"tool_name": main_tool, "tool_args": react_result.steps[-1].args if react_result.steps else {}}})
                                delay_desc = f"{delayed_seconds // 3600}ч" if delayed_seconds >= 3600 else f"{delayed_seconds // 60}мин"
                                for action in delayed_actions:
                                    # (фикс аудита) kwargs + execute_at datetime
                                    await automation_service.create_automation(
                                        user_id=user_id or "",
                                        name=f"Hybrid отложено ({delay_desc}): {action['action_type']}",
                                        action_type=action["action_type"],
                                        action_data=action["action_data"],
                                        schedule_type="once",
                                        execute_at=execute_at,
                                        description=f"Создано из Hybrid: {original_message[:200]}",
                                        source_message=original_message[:500])
                                final_text = f"📋 Данные получены.\n\n⏰ **Отложено на {delay_desc}** (выполнится в {execute_at.strftime('%H:%M UTC')})\n\n💡 Посмотреть или отменить: спроси «мои автоматизации» или открой вкладку «Автоматизации» (там же отмена и история запусков).\n\n---\n{final_text}"
                            except Exception as delay_err:
                                logger.warning(f"[Hybrid] Failed to create delayed automation: {delay_err}")

                        # Session Memory
                        try:
                            auto_session.add_turn(
                                user_message=original_message,
                                tool_name=main_tool,
                                tool_args=react_result.steps[-1].args if react_result.steps else {},
                                observation=observation,
                                pretty_result=pretty or "",
                            )
                        except Exception:
                            logger.debug("suppressed exception", exc_info=True)

                        goal_reached, missing = _react_goal_check(
                            user_message=data.message,
                            steps=react_result.steps,
                            final_text=final_text,
                        )

                        # Динамический лейбл: к базовому "Hybrid" дописываем
                        # MeetFlow если встречи реально опрашивались.
                        _hybrid_tools = {(s.tool or "") for s in react_result.steps}
                        _hybrid_agents = ["Hybrid"]
                        if any("meetflow" in t.lower() for t in _hybrid_tools):
                            _hybrid_agents.append("MeetFlow")
                        _hybrid_agents.append("ReAct")

                        if goal_reached:
                            return AgentChatResponse(
                                success=True,
                                message=(
                                    f"🔀 Выполнено за {len(react_result.steps)} шаг(а)\n"
                                    f"{steps_text}\n\n"
                                    f"---\n{final_text}"
                                ),
                                session_id=str(session_id),
                                agent_mode="automation",
                                agents_involved=_hybrid_agents,
                                execution_time_ms=execution_time,
                                sources=[{"step": i, "tool": s.tool, "args": s.args} for i, s in enumerate(react_result.steps, 1)],
                            )

                        missing_text = ", ".join(missing) if missing else "goal_not_reached"
                        return AgentChatResponse(
                            success=False,
                            message=(
                                f"⚠️ Hybrid выполнил шаги, но цель не завершена ({missing_text}).\n"
                                f"{steps_text}\n\n"
                                f"---\n{final_text}"
                            ),
                            session_id=str(session_id),
                            agent_mode="automation",
                            agents_involved=_hybrid_agents,
                            execution_time_ms=execution_time,
                            sources=[{"step": i, "tool": s.tool, "args": s.args} for i, s in enumerate(react_result.steps, 1)],
                        )
                    else:
                        return AgentChatResponse(
                            success=False,
                            message=(
                                f"❌ Ошибка выполнения\n\n"
                                f"Шаги: {steps_text}\n\n"
                                f"Ошибка: {react_result.error}"
                            ),
                            session_id=str(session_id),
                            agent_mode="automation",
                            agents_involved=["Hybrid"],
                            execution_time_ms=execution_time,
                        )

                except httpx.ConnectError as e:
                    logger.error(f"[Hybrid] Ollama connection error: {e}")
                    return AgentChatResponse(
                        success=False,
                        message="❌ Сервис временно недоступен. Попробуйте позже или переключитесь на другой режим.",
                        session_id=str(session_id),
                        agent_mode="automation",
                    )
                except Exception as e:
                    logger.exception(f"[Hybrid] BigLLM ReAct error: {e}")
                    return AgentChatResponse(
                        success=False,
                        message="❌ Произошла ошибка при выполнении задачи. Попробуйте переформулировать запрос.",
                        session_id=str(session_id),
                        agent_mode="automation",
                    )

            # ══════════════════════════════════════════════════════════════
            #   OpenClaw-style: Pure LLM ReAct (no FunctionGemma, all steps by BigLLM)
            # ══════════════════════════════════════════════════════════════
            # P12: "agent_oc" — Hermes-трек. Фаза 1: использует тот же
            # Pure-LLM ReAct-движок, что и OpenClaw (session-memory +
            # TessentBrainTools). Скиллы/lesson-память/граф-заземление
            # наращиваются в следующих фазах поверх этой же точки.
            if automation_mode in ("openclaw", "agent_oc"):
                try:
                    from backend.core.auto.session_memory import get_auto_session_store
                    from backend.core.llm.router import LLMRouter
                    from backend.core.local_models.ollama_functiongemma import (
                        ReActResult,
                        get_tools_catalog,
                        run_react_loop_pure_llm,
                    )
                    from backend.integrations.tessent_brain_tools import TessentBrainTools

                    # --- Session Memory ---
                    auto_session_store = get_auto_session_store()
                    auto_session = auto_session_store.get_or_create(
                        session_id=str(session_id),
                        user_id=user_id or "",
                    )
                    original_message = data.message
                    enriched_message, was_enriched = auto_session.enrich_message(data.message)
                    if was_enriched:
                        logger.info(f"[AgentOC] Session memory enriched message: '{data.message[:50]}...'")
                        data.message = enriched_message

                    # Мульти-клиент: подмешиваем данные по упомянутым клиентам/
                    # сущностям из графа (агентство с несколькими клиентами).
                    try:
                        _oc_clients = await _resolve_mentioned_entities_context(
                            user_id, original_message)
                        if _oc_clients:
                            data.message = f"{_oc_clients}\n\n[Задача]\n{data.message}"
                            logger.info("[AgentOC] Подмешан контекст упомянутых клиентов из графа")
                    except Exception:
                        logger.debug("AgentOC client context skipped", exc_info=True)

                    # --- LLM Router ---
                    oc_llm_router = LLMRouter()
                    logger.info("[AgentOC] LLMRouter created for Pure LLM ReAct")

                    # --- TessentBrainTools ---
                    graph_builder = getattr(state, "graph", None)
                    vector_indexer = getattr(state, "vector_indexer", None)

                    oc_reasoning = None
                    if oc_llm_router and graph_builder:
                        try:
                            from backend.core.think.reasoning_engine import ReasoningEngine
                            oc_reasoning = ReasoningEngine(
                                graph_builder=graph_builder,
                                vector_indexer=vector_indexer,
                                llm_router=oc_llm_router,
                            )
                        except Exception:
                            logger.debug("suppressed exception", exc_info=True)

                    from backend.integrations.tessent_brain_tools import (
                        build_enhanced_search,
                    )
                    tools = TessentBrainTools(
                        graph_builder=graph_builder,
                        reasoning_engine=oc_reasoning,
                        vector_indexer=vector_indexer,
                        user_id=user_id,
                        # см. комментарий у первого сайта (SEARCH_CONSUMERS_AUDIT)
                        enhanced_search=build_enhanced_search(
                            graph_builder=graph_builder,
                            vector_indexer=vector_indexer,
                            llm_router=oc_llm_router,
                            user_id=user_id,
                        ),
                    )

                    # --- Integration Registry ---
                    integration_registry = None
                    try:
                        from backend.integrations.registry import IntegrationRegistry
                        integration_registry = IntegrationRegistry()
                        await integration_registry.load_for_user(user_id or "")
                        loaded = integration_registry.list_loaded()
                        if loaded:
                            logger.info(f"[AgentOC] Integration Registry loaded: {[i['id'] for i in loaded]}")
                    except Exception as int_err:
                        logger.warning(f"[AgentOC] IntegrationRegistry load failed: {int_err}")

                    # --- Build full tools catalog ---
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

                    # --- P12c: skills как инструменты петли (только
                    # agent_oc, env HERMES_SKILLS_ENABLED, default off).
                    # Additive + обёрнуто — OpenClaw/дефолт не меняются.
                    _hermes_skills_on = (
                        automation_mode == "agent_oc"
                        and (os.getenv("HERMES_SKILLS_ENABLED", "on")
                             or "on").strip().lower()
                        in ("1", "true", "on", "yes")
                    )
                    _hermes_store = None
                    if _hermes_skills_on:
                        try:
                            from backend.core.hermes.skill_runtime import (
                                skill_tool_defs,
                            )
                            from backend.core.hermes.skill_store import (
                                SkillStore,
                            )
                            _hermes_store = SkillStore()
                            # P12i-E: засеять bundled-скиллы (идемпотентно,
                            # один раз на пользователя, never-raises).
                            try:
                                from backend.core.hermes.bundled import (
                                    seed_bundled_skills,
                                )

                                seed_bundled_skills(
                                    _hermes_store, user_id or "")
                            except Exception as _bx:
                                logger.debug("bundled seed skipped: %s", _bx)
                            _names = {t["name"] for t in tools_catalog}
                            for _d in skill_tool_defs():
                                if _d["name"] not in _names:
                                    tools_catalog.append(_d)
                        except Exception as _hx:
                            logger.debug("hermes skills wiring skipped: %s", _hx)
                            _hermes_store = None

                    # P12f: агентные инструменты (skill_manage/delegate/
                    # execute_code) — env HERMES_AGENT_TOOLS_ENABLED,
                    # default off. Идут ЧЕРЕЗ approval-gate (P12e).
                    # delegate/execute_code — безопасные слоты
                    # (исполнители не подключены в этой фазе).
                    _hermes_agent_tools_on = (
                        automation_mode == "agent_oc"
                        and (os.getenv("HERMES_AGENT_TOOLS_ENABLED", "on")
                             or "on").strip().lower()
                        in ("1", "true", "on", "yes")
                    )
                    # P12i-IND: реальные исполнители слотов — ТОЛЬКО при
                    # HERMES_REAL_EXECUTION (default off → остаются
                    # безопасные заглушки P12f). Через approval-gate.
                    _hermes_code_runner = None
                    _hermes_delegate_fn = None
                    if _hermes_agent_tools_on and (
                        os.getenv("HERMES_REAL_EXECUTION", "off") or "off"
                    ).strip().lower() in ("1", "true", "on", "yes"):
                        try:
                            from backend.core.hermes.runners import (
                                make_brain_delegate,
                                make_executor_code_runner,
                            )

                            _hermes_code_runner = make_executor_code_runner()

                            async def _brain_answer(_q: str) -> str:
                                return await oc_llm_router.generate(_q)

                            _hermes_delegate_fn = make_brain_delegate(
                                _brain_answer)
                        except Exception as _rx:
                            logger.debug("hermes runners skipped: %s", _rx)
                            _hermes_code_runner = None
                            _hermes_delegate_fn = None
                    if _hermes_agent_tools_on:
                        try:
                            from backend.core.hermes.agent_tools import (
                                agent_tool_defs,
                            )
                            from backend.core.hermes.skill_store import (
                                SkillStore,
                            )
                            if _hermes_store is None:
                                _hermes_store = SkillStore()
                            _an = {t["name"] for t in tools_catalog}
                            for _d in agent_tool_defs():
                                if _d["name"] not in _an:
                                    tools_catalog.append(_d)
                        except Exception as _ax:
                            logger.debug("hermes agent tools skipped: %s", _ax)
                            _hermes_agent_tools_on = False

                    # P12h: MCP-серверы (agent_oc, env HERMES_MCP_ENABLED,
                    # серверы — HERMES_MCP_SERVERS JSON {name:command}).
                    # Без серверов реестр пуст → ноль эффекта. Идёт через
                    # approval-gate. never-raises.
                    _hermes_mcp = None
                    if automation_mode == "agent_oc" and (
                        os.getenv("HERMES_MCP_ENABLED", "off") or "off"
                    ).strip().lower() in ("1", "true", "on", "yes"):
                        try:
                            from backend.core.hermes.mcp_client import (
                                registry_from_config,
                            )

                            try:
                                _mcp_cfg = json.loads(
                                    os.getenv("HERMES_MCP_SERVERS", "") or "{}")
                            except Exception:
                                _mcp_cfg = {}
                            if isinstance(_mcp_cfg, dict) and _mcp_cfg:
                                _hermes_mcp = registry_from_config(_mcp_cfg)
                                _mn = {t["name"] for t in tools_catalog}
                                for _d in await _hermes_mcp.discover():
                                    if _d["name"] not in _mn:
                                        tools_catalog.append(_d)
                        except Exception as _mx:
                            logger.debug("hermes mcp skipped: %s", _mx)
                            _hermes_mcp = None

                    # --- P12d: lesson-память (agent_oc, env
                    # HERMES_LESSONS_ENABLED, default off). Recall →
                    # инжект в сообщение перед петлёй; capture — после.
                    _hermes_lessons = None
                    if automation_mode == "agent_oc" and (
                        os.getenv("HERMES_LESSONS_ENABLED", "off") or "off"
                    ).strip().lower() in ("1", "true", "on", "yes"):
                        try:
                            from backend.core.hermes.lesson_memory import (
                                LessonStore,
                                maybe_recall_block,
                            )

                            _hermes_lessons = LessonStore()
                            _lblock = maybe_recall_block(
                                user_id or "", original_message,
                                store=_hermes_lessons, enabled=True, limit=5,
                            )
                            if _lblock:
                                data.message = f"{_lblock}\n\n{data.message}"
                        except Exception as _lx:
                            logger.debug("hermes lessons wiring skipped: %s", _lx)
                            _hermes_lessons = None

                    # --- P12g: портрет клиента в сообщение (agent_oc,
                    # env HERMES_PORTRAIT_ENABLED, default off).
                    # never-raises; OpenClaw/дефолт не тронуты.
                    if automation_mode == "agent_oc" and (
                        os.getenv("HERMES_PORTRAIT_ENABLED", "off") or "off"
                    ).strip().lower() in ("1", "true", "on", "yes"):
                        try:
                            from backend.core.hermes.portrait import (
                                maybe_portrait_for_user,
                            )

                            _pblock = await maybe_portrait_for_user(
                                user_id or "", enabled=True,
                            )
                            if _pblock:
                                data.message = f"{_pblock}\n\n{data.message}"
                        except Exception as _px:
                            logger.debug("hermes portrait skipped: %s", _px)

                    # P12i-C: системный nudge — объясняет агенту, КОГДА
                    # звать skills_list/skill_view/skill_manage и учитывать
                    # уроки (иначе подключённые инструменты простаивают).
                    # Только при включённых скиллах; prepend → инструкция
                    # окажется первой. never-raises.
                    if _hermes_skills_on:
                        try:
                            from backend.core.hermes.skill_runtime import (
                                build_skill_nudge,
                            )

                            _nudge = build_skill_nudge(
                                skills_enabled=True,
                                agent_tools_enabled=_hermes_agent_tools_on,
                                lessons_enabled=_hermes_lessons is not None,
                            )
                            if _nudge:
                                data.message = f"{_nudge}\n\n{data.message}"
                        except Exception as _nx:
                            logger.debug("hermes nudge skipped: %s", _nx)

                    # --- Tool executor ---
                    async def oc_execute_tool(tool_name: str, args: dict) -> str:
                        """Execute a tool by name + args, return observation string."""
                        try:
                            # 0. P12c: skill-инструменты (agent_oc) — до всего
                            if _hermes_store is not None:
                                try:
                                    from backend.core.hermes.skill_runtime import (
                                        execute_skill_tool,
                                        is_skill_tool,
                                    )
                                    if is_skill_tool(tool_name):
                                        return execute_skill_tool(
                                            tool_name, args or {},
                                            store=_hermes_store,
                                            user_id=user_id or "",
                                        )
                                except Exception as _se:
                                    logger.debug("skill tool exec skipped: %s", _se)

                            # 0b. P12f: агентные инструменты (под approval).
                            # delegate/execute_code — слоты (исполнители
                            # None → безопасный отказ, агент адаптируется).
                            if _hermes_agent_tools_on:
                                try:
                                    from backend.core.hermes.agent_tools import (
                                        execute_agent_tool,
                                        is_agent_tool,
                                    )
                                    if is_agent_tool(tool_name):
                                        return await execute_agent_tool(
                                            tool_name, args or {},
                                            store=_hermes_store,
                                            user_id=user_id or "",
                                            delegate_fn=_hermes_delegate_fn,
                                            code_runner=_hermes_code_runner,
                                        )
                                except Exception as _ae:
                                    logger.debug("agent tool exec skipped: %s", _ae)

                            # 0c. P12h: MCP-инструменты (под approval).
                            if _hermes_mcp is not None:
                                try:
                                    from backend.core.hermes.mcp_client import (
                                        is_mcp_tool,
                                    )
                                    if is_mcp_tool(tool_name):
                                        return await _hermes_mcp.execute(
                                            tool_name, args or {})
                                except Exception as _me:
                                    logger.debug("mcp tool exec skipped: %s", _me)

                            # 1. Try TessentBrainTools first (built-in tools)
                            method = getattr(tools, tool_name, None)
                            if method and callable(method):
                                if asyncio.iscoroutinefunction(method):
                                    result = await method(**args)
                                else:
                                    result = method(**args)
                                obs = json.dumps(result, ensure_ascii=False, default=str) if not isinstance(result, str) else result
                                logger.info(f"[AgentOC] Tool {tool_name} (brain) result: {obs[:200]}")
                                return obs

                            # 2. Try IntegrationRegistry (gmail, slack, jira, trello, etc.)
                            if integration_registry:
                                integration = integration_registry.find_integration_for_tool(tool_name)
                                if integration:
                                    logger.info(f"[AgentOC] Calling integration tool {tool_name} with args: {json.dumps(args, ensure_ascii=False, default=str)[:300]}")
                                    result = await integration.execute(tool_name, **args)
                                    obs = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
                                    logger.info(f"[AgentOC] Tool {tool_name} (integration) result: {obs[:300]}")
                                    return obs

                            # 3. Fallback: gmail_send / send_telegram via automation_tools_async
                            if tool_name == "gmail_send":
                                try:
                                    # (фикс аудита) локальный пересчёт пути был на уровень короче
                                    # и попадал в несуществующий tessent_brain/alfa_asynk_meetflow —
                                    # используем модульный MEETFLOW_PATH (автоопределение соседа)
                                    if MEETFLOW_PATH not in sys.path:
                                        sys.path.insert(0, MEETFLOW_PATH)
                                    from automation_tools_async import send_email
                                    to_addr = args.get("to", "me")
                                    if to_addr == "me":
                                        # Resolve "me" to actual email
                                        resolved = os.getenv("USER_EMAIL", "")
                                        if not resolved and user_id:
                                            try:
                                                from backend.db.supabase_client import (
                                                    SupabaseClient,
                                                )
                                                supa = SupabaseClient()
                                                users = await supa._request("GET", "/rest/v1/users", params={"id": f"eq.{user_id}", "select": "email"})
                                                if users and users[0].get("email"):
                                                    resolved = users[0]["email"]
                                            except Exception:
                                                logger.debug("suppressed exception", exc_info=True)
                                        if resolved:
                                            to_addr = resolved
                                        else:
                                            logger.warning("[AgentOC] gmail_send: to='me' but no email found, using 'me' as-is")
                                    logger.info(f"[AgentOC] gmail_send fallback: to={to_addr}, subject={args.get('subject', '')[:50]}")
                                    result = await send_email(
                                        to=to_addr,
                                        subject=args.get("subject", "Tessbrain — результат задачи"),
                                        body=args.get("body", "")
                                    )
                                    logger.info(f"[AgentOC] gmail_send fallback result: {result[:200]}")
                                    return result
                                except Exception as mail_err:
                                    logger.error(f"[AgentOC] gmail_send fallback error: {mail_err}")
                                    return json.dumps({"error": f"Email send error: {mail_err!s}"})

                            if tool_name in ("send_telegram_message", "send_telegram"):
                                try:
                                    # (фикс аудита) локальный пересчёт пути был на уровень короче
                                    # и попадал в несуществующий tessent_brain/alfa_asynk_meetflow —
                                    # используем модульный MEETFLOW_PATH (автоопределение соседа)
                                    if MEETFLOW_PATH not in sys.path:
                                        sys.path.insert(0, MEETFLOW_PATH)
                                    from automation_tools_async import (
                                        send_telegram_message as send_telegram,
                                    )
                                    result = await send_telegram(message=args.get("message", ""))
                                    logger.info(f"[AgentOC] send_telegram fallback result: {result[:200]}")
                                    return result
                                except Exception as tg_err:
                                    logger.error(f"[AgentOC] send_telegram fallback error: {tg_err}")
                                    return json.dumps({"error": f"Telegram send error: {tg_err!s}"})

                            # 4. Special: analyze_with_llm
                            if tool_name == "analyze_with_llm":
                                task = str(args.get("task") or args.get("query") or "")
                                context = str(args.get("context") or "")
                                prompt = f"Задача: {task}\n\nКонтекст/данные для анализа:\n{context[:3000]}\n\nВыполни задачу и дай структурированный ответ."
                                try:
                                    response = await oc_llm_router.generate(prompt=prompt, max_tokens=2000)
                                    return json.dumps({"success": True, "result": str(response)}, ensure_ascii=False)
                                except Exception as llm_err:
                                    return json.dumps({"error": f"LLM error: {llm_err!s}"})

                            return json.dumps({"error": f"Tool '{tool_name}' not found. Check tool name."})
                        except Exception as e:
                            logger.error(f"[AgentOC] Tool execution error ({tool_name}): {e}")
                            return json.dumps({"error": str(e)})

                    max_react_steps = 10
                    # P12i-B: agent_oc получает СВОЙ больший бюджет
                    # итераций (Hermes ≈90 — без этого «любая задача»
                    # не работает). OpenClaw не трогаем. env
                    # HERMES_MAX_STEPS (default 30, clamp 1..90),
                    # never-raises.
                    if automation_mode == "agent_oc":
                        try:
                            _ms = int(os.getenv("HERMES_MAX_STEPS", "30")
                                      or "30")
                            max_react_steps = max(1, min(_ms, 90))
                        except (TypeError, ValueError):
                            max_react_steps = 30

                    logger.info(f"[AgentOC] Starting Pure LLM ReAct: message='{data.message[:60]}...', tools={len(tools_catalog)}, max_steps={max_react_steps}")

                    # --- P12e: approval-gate (agent_oc, env
                    # HERMES_APPROVAL_MODE; default off → не оборачиваем,
                    # zero-overhead). never-raises.
                    if automation_mode == "agent_oc":
                        try:
                            from backend.core.hermes.approval import (
                                guard_executor,
                                policy_from_env,
                            )

                            _ap_policy = policy_from_env()
                            if _ap_policy.mode != "off":
                                oc_execute_tool = guard_executor(
                                    oc_execute_tool, policy=_ap_policy,
                                )
                                logger.info(
                                    "[agent_oc] approval-gate mode=%s",
                                    _ap_policy.mode,
                                )
                        except Exception as _apx:
                            logger.debug("approval gate skipped: %s", _apx)

                    # --- Run Pure LLM ReAct ---
                    react_result: ReActResult = await run_react_loop_pure_llm(
                        user_message=data.message,
                        tool_executor=oc_execute_tool,
                        llm_router=oc_llm_router,
                        tools=tools_catalog,
                        max_steps=max_react_steps,
                        model_tier=data.model_tier or "standard",
                    )

                    execution_time = int((datetime.now() - start_time).total_seconds() * 1000)

                    # --- Track usage ---
                    track_usage(
                        provider="openclaw",
                        model="gemini-pure-react",
                        input_tokens=react_result.total_input_tokens,
                        output_tokens=react_result.total_output_tokens,
                        user_id=user_id,
                        session_id=session_id,
                        agent_mode="automation",
                        request_type="openclaw_react",
                        success=react_result.success,
                        latency_ms=execution_time,
                        model_tier=data.model_tier,
                    )

                    # --- Build trace ---
                    steps_text = ""
                    for i, step in enumerate(react_result.steps, 1):
                        if step.tool == "done":
                            continue
                        steps_text += f"\n**Шаг {i}:** `{step.tool}`"
                        if step.args:
                            args_str = ", ".join(f"{k}={v}" for k, v in list(step.args.items())[:3])
                            steps_text += f" ({args_str})"
                        if step.thought:
                            steps_text += f"\n  *{step.thought}*"

                    if react_result.success:
                        main_tool = ""
                        for s in reversed(react_result.steps):
                            if s.tool != "done":
                                main_tool = s.tool
                                break
                        observation = react_result.final_answer or ""
                        pretty = _pretty_from_observation(main_tool, observation) if main_tool else None
                        final_text = pretty if pretty else observation

                        # Session Memory
                        try:
                            auto_session.add_turn(
                                user_message=original_message,
                                tool_name=main_tool,
                                tool_args=react_result.steps[-1].args if react_result.steps else {},
                                observation=observation,
                                pretty_result=pretty or "",
                            )
                        except Exception:
                            logger.debug("suppressed exception", exc_info=True)

                        # P12c: автообучение скиллу из успешной траектории
                        # (agent_oc, env HERMES_SKILL_AUTOLEARN, default
                        # off). Никогда не ломает ответ агента.
                        if _hermes_store is not None and (
                            os.getenv("HERMES_SKILL_AUTOLEARN", "on")
                            or "on"
                        ).strip().lower() in ("1", "true", "on", "yes"):
                            try:
                                from backend.core.hermes.skill_runtime import (
                                    maybe_autolearn,
                                )

                                async def _hermes_llm(_p):
                                    return await oc_llm_router.generate_json(_p)

                                _learned = await maybe_autolearn(
                                    original_message,
                                    react_result.steps,
                                    True,
                                    store=_hermes_store,
                                    user_id=user_id or "",
                                    llm_json_call=_hermes_llm,
                                    enabled=True,
                                )
                                if _learned:
                                    logger.info(
                                        "[agent_oc] autolearned skill '%s'",
                                        _learned,
                                    )
                            except Exception:
                                logger.debug("autolearn skipped", exc_info=True)

                        # P12d: зафиксировать УСПЕШНый урок (заземлён на
                        # граф/lineage). env HERMES_LESSONS_ENABLED, off
                        # by default, never-raises.
                        if _hermes_lessons is not None:
                            try:
                                from backend.core.hermes.lesson_memory import (
                                    maybe_capture_lesson,
                                )

                                maybe_capture_lesson(
                                    success=True,
                                    task=original_message,
                                    steps=react_result.steps,
                                    store=_hermes_lessons,
                                    user_id=user_id or "",
                                    enabled=True,
                                )
                            except Exception:
                                logger.debug("lesson capture skipped",
                                             exc_info=True)

                        goal_reached, missing = _react_goal_check(
                            user_message=data.message,
                            steps=react_result.steps,
                            final_text=final_text,
                        )

                        # Динамический лейбл agents_involved: показывает реально
                        # использованные подсистемы вместо захардкоженного
                        # "OpenClaw → AI Agent". В agent_oc-режиме различает
                        # Hermes/Skills, MeetFlow, AutoLearn и т.д. по шагам ReAct.
                        _tools_used = {(s.tool or "") for s in react_result.steps}
                        _ai_agents: list[str] = []
                        if automation_mode == "agent_oc":
                            _ai_agents.append("agent_oc")
                            if any(t.startswith("skill") for t in _tools_used):
                                _ai_agents.append("Hermes/Skills")
                            if "skill_manage" in _tools_used:
                                _ai_agents.append("AutoLearn")
                            if _hermes_lessons is not None:
                                _ai_agents.append("Hermes/Lessons")
                            if _hermes_agent_tools_on:
                                _ai_agents.append("AgentTools")
                            if _hermes_mcp is not None:
                                _ai_agents.append("MCP")
                        else:
                            _ai_agents.append("OpenClaw")
                        if any("meetflow" in t.lower() for t in _tools_used):
                            _ai_agents.append("MeetFlow")
                        _ai_agents.append("ReAct")

                        if goal_reached:
                            return AgentChatResponse(
                                success=True,
                                message=(
                                    f"🐾 Выполнено за {len([s for s in react_result.steps if s.tool != 'done'])} шаг(а)\n"
                                    f"{steps_text}\n\n"
                                    f"---\n{final_text}"
                                ),
                                session_id=str(session_id),
                                agent_mode="automation",
                                agents_involved=_ai_agents,
                                execution_time_ms=execution_time,
                                sources=[{"step": i, "tool": s.tool, "args": s.args, "reason": s.thought} for i, s in enumerate(react_result.steps, 1) if s.tool != "done"],
                            )

                        missing_text = ", ".join(missing) if missing else "goal_not_reached"
                        # P12d: зафиксировать урок-ПРОВАЛ (не повторять
                        # неработающий подход). never-raises, gated.
                        if _hermes_lessons is not None:
                            try:
                                from backend.core.hermes.lesson_memory import (
                                    maybe_capture_lesson,
                                )

                                maybe_capture_lesson(
                                    success=False,
                                    task=original_message,
                                    steps=react_result.steps,
                                    store=_hermes_lessons,
                                    user_id=user_id or "",
                                    summary=f"goal not reached: {missing_text}",
                                    enabled=True,
                                )
                            except Exception:
                                logger.debug("lesson(fail) capture skipped",
                                             exc_info=True)
                        _mode_label = "agent_oc" if automation_mode == "agent_oc" else "OpenClaw"
                        return AgentChatResponse(
                            success=False,
                            message=(
                                f"⚠️ {_mode_label} выполнил шаги, но цель не завершена ({missing_text}).\n"
                                f"{steps_text}\n\n"
                                f"---\n{final_text}"
                            ),
                            session_id=str(session_id),
                            agent_mode="automation",
                            agents_involved=_ai_agents,
                            execution_time_ms=execution_time,
                            sources=[{"step": i, "tool": s.tool, "args": s.args, "reason": s.thought} for i, s in enumerate(react_result.steps, 1) if s.tool != "done"],
                        )
                    else:
                        return AgentChatResponse(
                            success=False,
                            message=(
                                f"❌ Ошибка выполнения\n\n"
                                f"Шаги: {steps_text}\n\n"
                                f"Ошибка: {react_result.error}"
                            ),
                            session_id=str(session_id),
                            agent_mode="automation",
                            agents_involved=["OpenClaw"],
                            execution_time_ms=execution_time,
                        )

                except Exception as e:
                    logger.exception(f"[AgentOC] Pure LLM ReAct error: {e}")
                    return AgentChatResponse(
                        success=False,
                        message="❌ Произошла ошибка. Попробуйте переформулировать запрос.",
                        session_id=str(session_id),
                        agent_mode="automation",
                    )

            # Calls → CallInsight API (НЕ браузер): телефон в запросе → карточка
            # клиента (snapshot+timeline); иначе → отчёт «Голос клиента» из
            # накопленных событий звонков (жалобы/сделки-к-спасению/менеджеры).
            if automation_mode == "calls":
                try:
                    import re as _re

                    from backend.core.integrations.callinsight import (
                        fetch_customer_card,
                        integration_enabled,
                        normalize_phone,
                        voice_of_customer,
                        voice_of_customer_markdown,
                    )
                    # База API звонков: env → start_url из фронта (вкладка
                    # Calls шлёт свой адрес) — иначе интеграция
                    # требовала ручного CALLINSIGHT_API_URL, хотя адрес
                    # уже известен. setdefault: явный env всегда главнее.
                    _calls_url = str((data.context or {}).get("start_url") or "").strip()
                    if _calls_url and not os.getenv("CALLINSIGHT_API_URL") \
                            and not os.getenv("CALLS_API_URL"):
                        if not _calls_url.startswith("http"):
                            _calls_url = f"https://{_calls_url}"
                        os.environ.setdefault("CALLS_API_URL", _calls_url)
                        logger.info(f"📞 CALLS_API_URL из start_url: {_calls_url}")
                    _uid = (data.context or {}).get("user_id")
                    _msg = data.message or ""
                    _msg_lc = _msg.lower()

                    # v4-маршруты (API_CHANGES_v4.md) — до телефонной ветки
                    # 1) Биллинг: «сколько минут осталось / квота / тариф»
                    if any(k in _msg_lc for k in ("минут", "квот", "баланс",
                                                  "тариф", "биллинг")):
                        from backend.core.integrations.callinsight import (
                            fetch_billing_usage,
                        )
                        _bu = await fetch_billing_usage()
                        if _bu.get("ok"):
                            _m = (_bu.get("data") or {}).get("minutes") or {}
                            _txt = (f"📊 Минуты CallInsight за месяц:\n"
                                    f"- использовано: {_m.get('used', '?')} из {_m.get('limit', '?')}"
                                    f" ({_m.get('pct', '?')}%)\n"
                                    f"- остаток: {_m.get('remaining', '?')} мин\n"
                                    f"- стоимость: {_m.get('cost_rub', '?')} ₽")
                        else:
                            _txt = f"⚠️ {_bu.get('error')}"
                        return AgentChatResponse(
                            success=True, message=_txt,
                            session_id=str(session_id), agent_mode="automation",
                            agents_involved=["CallInsight"])

                    # 2) Дневной отчёт по звонкам (v4: персистится на их стороне)
                    if any(k in _msg_lc for k in ("отчёт", "отчет", "дайджест",
                                                  "за день", "за вчера", "daily")):
                        from backend.core.integrations.callinsight import (
                            fetch_daily_report,
                        )
                        _refresh = any(k in _msg_lc for k in ("пересобер", "обнови", "заново", "refresh"))
                        _dr = await fetch_daily_report(refresh=_refresh)
                        if _dr.get("ok"):
                            _d = _dr.get("data") or {}
                            _body = (_d.get("report") or _d.get("text")
                                     or json.dumps(_d, ensure_ascii=False, indent=1)[:3500])
                            _cached = " (из кеша)" if _d.get("cached") else ""
                            _txt = f"📞 Дневной отчёт по звонкам{_cached}:\n\n{_body}"
                        else:
                            _txt = f"⚠️ {_dr.get('error')}"
                        return AgentChatResponse(
                            success=True, message=_txt,
                            session_id=str(session_id), agent_mode="automation",
                            agents_involved=["CallInsight"])

                    # 3) AI-подбор клиентов под предложение (v4 llm-search)
                    if any(k in _msg_lc for k in ("найди клиент", "подбери клиент",
                                                  "кому предложить", "кому продать",
                                                  "клиентов под", "клиенты под")):
                        from backend.core.integrations.callinsight import (
                            llm_search_customers,
                        )
                        _ls = await llm_search_customers(_msg)
                        if _ls.get("ok"):
                            _items = (_ls.get("data") or {}).get("items") or []
                            if _items:
                                _rows = [
                                    (f"- **{i.get('name_guess') or i.get('phone')}** "
                                     f"(score {i.get('score')}): {i.get('reason', '')} "
                                     f"[{i.get('readiness', '')}]")
                                    for i in _items[:10]]
                                _txt = ("🎯 Кандидаты под предложение "
                                        f"({len(_items)}):\n" + "\n".join(_rows))
                            else:
                                _reason = (_ls.get("data") or {}).get("reason", "")
                                _txt = f"Подходящих клиентов не нашлось ({_reason or 'пусто'})."
                        else:
                            _txt = f"⚠️ {_ls.get('error')}"
                        return AgentChatResponse(
                            success=True, message=_txt,
                            session_id=str(session_id), agent_mode="automation",
                            agents_involved=["CallInsight"])

                    _phone_m = _re.search(r"\+?\d[\d\s\-\(\)]{6,}\d", _msg)
                    if _phone_m:
                        _phone = normalize_phone(_phone_m.group(0))
                        card = await fetch_customer_card(_phone) if _phone else {}
                        if card and not card.get("error"):
                            _txt = (f"📞 Карточка клиента {_phone} (CallInsight):\n"
                                    f"```json\n"
                                    f"{json.dumps(card, ensure_ascii=False, indent=2)[:3500]}\n```")
                        else:
                            _txt = (f"По номеру {_phone} в CallInsight данных нет "
                                    f"(или интеграция не настроена).")
                    else:
                        events: list = []
                        try:
                            from backend.core.memory.event_log import EventLog
                            from backend.core.store.tenant_paths import (
                                event_log_path_for_user,
                            )
                            log = EventLog(persist_path=event_log_path_for_user(_uid))
                            events = [e.to_dict() if hasattr(e, "to_dict") else dict(e)
                                      for e in log.events()]
                        except Exception:
                            logger.debug("calls: event log read failed", exc_info=True)
                        if events:
                            _txt = voice_of_customer_markdown(voice_of_customer(events))
                        elif not integration_enabled():
                            _txt = ("Интеграция CallInsight не настроена "
                                    "(нет CALLINSIGHT API base / секрета). Подключите "
                                    "её, чтобы анализировать звонки по API.")
                        else:
                            _txt = ("Пока нет накопленных событий звонков. Как только "
                                    "CallInsight пришлёт события (вебхук) — здесь "
                                    "появится отчёт «Голос клиента». Для конкретного "
                                    "клиента укажи телефон в запросе.")
                    return AgentChatResponse(
                        success=True, message=_txt,
                        session_id=str(session_id), agent_mode="automation",
                        agents_involved=["CallInsight"],
                    )
                except Exception as _call_err:
                    logger.error(f"calls via CallInsight failed: {_call_err}")
                    return AgentChatResponse(
                        success=False,
                        message=f"❌ Calls (CallInsight) ошибка: {_call_err!s}",
                        session_id=str(session_id), agent_mode="automation",
                    )

            # Web-based automation modes (web, meetflow, calls)
            if automation_mode in ("web", "meetflow", "calls"):
                # MeetFlow read fast-path: информационные запросы («покажи
                # последние встречи», «найди встречу X») обслуживаем напрямую
                # через Supabase REST (get_recent_meetings/get_meeting_context),
                # не поднимая браузер. Это убирает зависимость от Chromium/
                # Stagehand для самого частого сценария и работает сразу, если
                # заданы SUPABASE_URL/SUPABASE_KEY. Любые НЕ-read запросы
                # (навигация, скачивание PDF, действия на сайте) падают дальше
                # в Web Operator без изменений.
                if automation_mode == "meetflow":
                    _msg_lc = (data.message or "").lower()
                    # Браузер (Web Operator) — ТОЛЬКО для явной навигации по
                    # сайту с URL. Всё остальное («что обсуждали», «расскажи
                    # про встречу», «покажи последние») — это РАЗГОВОР по
                    # данным встреч, а не автоматизация браузера (фикс: раньше
                    # всё, кроме списка, падало в Web Operator → ошибка без
                    # Chromium). Собеседник ниже, 0 браузера.
                    _browser_kw = ("открой сайт", "перейди на", "зайди на",
                                   "заполни форму", "нажми кнопку", "navigate to")
                    _needs_browser = ("http" in _msg_lc and any(
                        k in _msg_lc for k in ("открой", "перейди", "зайди",
                                               "скачай", "заполни", "нажми")))
                    _needs_browser = _needs_browser or any(k in _msg_lc for k in _browser_kw)
                    if not _needs_browser:
                        try:
                            _mf_answer = await _meetflow_chat_answer(
                                data.message, user_id, data.chat_history,
                                data.model_tier)
                            execution_time = int((datetime.now() - start_time).total_seconds() * 1000)
                            return AgentChatResponse(
                                success=True, message=_mf_answer,
                                session_id=str(session_id),
                                agent_mode="automation",
                                agents_involved=["MeetFlow"],
                                execution_time_ms=execution_time,
                            )
                        except Exception as mf_err:
                            logger.warning(
                                f"MeetFlow chat failed, fallback to Web Operator: {mf_err}",
                                exc_info=True)
                            # падаем дальше в Web Operator

                try:
                    from backend.core.web_operator import WebOperatorConfig, run_web_task
                    from backend.core.web_operator.operator import OperatorEnv

                    # Configure based on automation mode
                    start_url = data.context.get("start_url")
                    # Default to headless=False for visual mode (user can see browser)
                    headless = data.context.get("headless", False)

                    # Список разрешённых доменов — из одного места
                    # (operator.default_allowed_domains): общеизвестные
                    # сервисы плюс адреса своей установки из
                    # WEB_OPERATOR_ALLOWED_DOMAINS. Раньше тот же список
                    # был захардкожен здесь копией и разъезжался с ним.
                    from backend.core.web_operator.operator import (
                        default_allowed_domains)
                    allowed_domains = default_allowed_domains()

                    # Select model based on tier
                    if data.model_tier == "premium":
                        model_name = "google/gemini-flash-latest"
                    else:
                        model_name = "google/gemini-flash-lite-latest"

                    config = WebOperatorConfig(
                        env=OperatorEnv(os.getenv("WEB_OPERATOR_ENV", "LOCAL")),
                        headless=headless,
                        allowed_domains=allowed_domains,
                        model_name=model_name,
                        fallback_model="openai/gpt-4o",
                        retry_on_rate_limit=True,
                        rate_limit_retry_delay=10
                    )

                    result = await run_web_task(
                        task=data.message,
                        start_url=start_url,
                        config=config,
                        timeout_seconds=120
                    )

                    execution_time = int((datetime.now() - start_time).total_seconds() * 1000)

                    # Track usage for Web Operator
                    track_external_agent_usage(
                        agent_mode=f"automation_{automation_mode}",
                        input_text=data.message,
                        output_text=str(result),
                        user_id=user_id,
                        session_id=session_id,
                        model=model_name,
                        execution_time_ms=execution_time
                    )

                    if result.get("success"):
                        actions_count = len(result.get("actions", []))
                        extracted = result.get("extracted_data")

                        message_parts = [f"✅ Выполнено {actions_count} действий в браузере."]
                        if extracted:
                            message_parts.append(f"\n📊 Извлечённые данные: {extracted}")

                        return AgentChatResponse(
                            success=True,
                            message="\n".join(message_parts),
                            session_id=str(session_id),
                            agent_mode="automation",
                            agents_involved=["WebOperator", f"Mode:{automation_mode}"],
                            execution_time_ms=execution_time,
                            sources=[{"type": "web_action", "data": a} for a in result.get("actions", [])[:5]]
                        )
                    else:
                        return AgentChatResponse(
                            success=False,
                            message=(
                                f"❌ Ошибка Web Operator: {result.get('error', 'Unknown error')}\n\n"
                                f"ℹ️ Подсказка: если в ошибке есть 429/RESOURCE_EXHAUSTED — это лимит/квота Gemini, "
                                f"нужно проверить billing/limits и повторить позже."
                            ),
                            session_id=str(session_id),
                            agent_mode="automation",
                            agents_involved=["WebOperator"],
                            execution_time_ms=execution_time
                        )

                except ImportError as e:
                    return AgentChatResponse(
                        success=False,
                        message=f"❌ Web Operator не установлен. Выполните: pip install stagehand playwright && playwright install chromium\n\nОшибка: {e}",
                        session_id=str(session_id),
                        agent_mode="automation"
                    )
                except Exception as e:
                    logger.exception(f"Web Operator error in automation: {e}")
                    return AgentChatResponse(
                        success=False,
                        message=f"❌ Ошибка Web Operator: {e!s}",
                        session_id=str(session_id),
                        agent_mode="automation"
                    )

            # TASKS FAST-PATH: короткие read-запросы («выгрузи список встреч»,
            # «покажи мои задачи») не требуют 12-агентного роя — мгновенный
            # ответ из Supabase, 0 LLM. Действия (создай/обнови/отправь) —
            # в рой, он умеет менять статусы и ходить в задачники.
            _t_msg = data.message.lower()
            _t_read = any(k in _t_msg for k in (
                "покажи", "список", "выгрузи", "какие", "последн", "мои "))
            _t_action = any(k in _t_msg for k in (
                "создай", "обнови", "отправь", "постав", "добав", "удали",
                "перенеси", "заведи", "напиши", "запусти", "презентац",
                "письмо", "почт", "телеграм", " тг"))
            # «достань задачи из встречи <название>» — тоже быстрый read,
            # но ТОЛЬКО без действий («…и заведи в YouGile» — это рой)
            if "задач" in _t_msg and "встреч" in _t_msg and not _t_action:
                _t_read = True
            if _t_read and not _t_action:
                try:
                    import re as _re_fp

                    from backend.integrations.tessent_brain_tools import (
                        TessentBrainTools,
                    )
                    _tt = TessentBrainTools(user_id=user_id)
                    # лимит из сообщения: «последние 5 встреч» → 5
                    _n_m = _re_fp.search(r"\b(\d{1,2})\b", _t_msg)
                    _n = max(1, min(int(_n_m.group(1)), 50)) if _n_m else None
                    _title_m = _re_fp.search(
                        r"встреч[иеу]\s+[«\"']?(.{3,80}?)[»\"']?\s*$", data.message)
                    if "задач" in _t_msg and _title_m:
                        # задачи КОНКРЕТНОЙ встречи (резолв по названию внутри)
                        _raw = await _tt.get_meeting_tasks_meetflow(
                            meeting_id=_title_m.group(1).strip())
                    elif any(k in _t_msg for k in ("задач", "task")):
                        _raw = await _tt.get_all_tasks(limit=_n or 20)
                    else:
                        _raw = await _tt.get_recent_meetings(limit=_n or 10)
                    _fast = _format_meetflow_fastpath(str(_raw))
                    if _fast and not _fast.startswith("⚠️"):
                        logger.info("⚡ tasks read fast-path (0 LLM)")
                        return AgentChatResponse(
                            success=True, message=_fast,
                            session_id=str(session_id),
                            agent_mode="automation",
                            agents_involved=["MeetFlow", "Supabase"],
                            execution_time_ms=int(
                                (datetime.now() - start_time).total_seconds() * 1000),
                        )
                except Exception:
                    logger.debug("tasks fast-path failed → рой", exc_info=True)

            # Default: tasks mode — MeetFlow Automation agents (original architecture).
            # MeetFlow = полноценная AutoGen multi-agent система.
            # Агенты САМИ решают: найти встречу → извлечь задачи → отправить на почту/в TG.
            # FunctionGemma НЕ используется в Tasks — она только для Tess.
            task_model_tier = data.model_tier or "standard"
            if await check_agent_health(MEETFLOW_URL):
                result = await call_meetflow_api(data.message, session_id, user_id)
            else:
                logger.info(f"MeetFlow API offline, using direct mode (tier={task_model_tier})")
                result = await run_meetflow_direct(data.message, session_id, user_id, model_tier=task_model_tier)

            task_model_name = "gemini-flash-lite-latest"  # AutoGen uses same base model; premium routing handled at agent level
            if result.get("success"):
                response_text = str(result.get("response") or result.get("summary") or "")
                track_external_agent_usage(
                    agent_mode="automation_tasks",
                    input_text=data.message,
                    output_text=response_text,
                    user_id=user_id,
                    session_id=session_id,
                    model=task_model_name,
                    execution_time_ms=int(result.get("execution_time_ms", 0))
                )
                goal_reached, missing = _tasks_goal_check(data.message, response_text)
                missing_text = ", ".join(missing) if missing else ""
                final_message = response_text if goal_reached else (
                    f"⚠️ Задача выполнена частично ({missing_text}).\n\n{response_text}"
                )
                return AgentChatResponse(
                    success=goal_reached,
                    message=final_message,
                    session_id=str(session_id),
                    agent_mode="automation",
                    agents_involved=[str(a) for a in result.get("agents_involved", [])],
                    execution_time_ms=int(result.get("execution_time_ms", 0))
                )
            else:
                return AgentChatResponse(
                    success=False,
                    message=f"❌ Ошибка: {result.get('error', 'Unknown error')}",
                    session_id=session_id,
                    agent_mode="automation"
                )


        elif data.agent_mode == "transcripts":
            # Chat with Transcripts (Gemma 3n or Gemini depending on tier)
            # Pass the graph builder from app state
            graph_builder = getattr(state, "graph", None)
            result = await run_gemma_chat(data.message, session_id, data.context, graph_builder=graph_builder, model_tier=data.model_tier, chat_history=data.chat_history)

            if result.get("success"):
                try:
                    return AgentChatResponse(
                        success=True,
                        message=str(result.get("response", "")),
                        session_id=str(session_id),
                        agent_mode="transcripts",
                        agents_involved=[str(a) for a in result.get("agents_involved", [])],
                        execution_time_ms=int(result.get("execution_time_ms", 0)),
                        sources=[dict(s) for s in result.get("sources", [])]
                    )
                except Exception as e:
                    logger.error(f"❌ Response validation error in transcripts: {e}")
                    return AgentChatResponse(
                        success=False,
                        message=f"❌ Ошибка формирования ответа: {e!s}",
                        session_id=str(session_id),
                        agent_mode="transcripts"
                    )
            else:
                return AgentChatResponse(
                    success=False,
                    message=f"❌ Ошибка: {result.get('error', 'Unknown error')}",
                    session_id=session_id,
                    agent_mode="transcripts"
                )

        else:
            # Default: use Tessbrain reasoning
            return AgentChatResponse(
                success=False,
                message="Use /chat/completions for Tessbrain mode",
                session_id=session_id,
                agent_mode="brain"
            )

    except Exception as e:
        logger.error(f"Agent chat error: {e}")
        return AgentChatResponse(
            success=False,
            message=f"❌ Ошибка: {e!s}",
            session_id=session_id,
            agent_mode=data.agent_mode
        )
    finally:
        _usage_context.reset(ctx_token)


@get("/status")
async def get_agents_status() -> list[AgentStatus]:
    """Получить статус всех агентных систем"""
    now = datetime.utcnow().isoformat()
    statuses = []

    # Tessbrain
    statuses.append(AgentStatus(
        name="Tessbrain",
        status="online",
        url="local",
        agents_count=5,  # ReasoningEngine agents
        last_check=now
    ))

    # Mark001
    mark_online = await check_agent_health(MARK001_URL)
    statuses.append(AgentStatus(
        name="Mark001 Marketing",
        status="online" if mark_online else "offline",
        url=MARK001_URL,
        agents_count=6 if mark_online else 0,
        last_check=now
    ))

    # MeetFlow
    meetflow_online = await check_agent_health(MEETFLOW_URL)
    statuses.append(AgentStatus(
        name="MeetFlow Automation",
        status="online" if meetflow_online else "offline",
        url=MEETFLOW_URL,
        agents_count=12 if meetflow_online else 0,
        last_check=now
    ))

    # Private LLM
    statuses.append(AgentStatus(
        name="Private LLM",
        status="online",
        url="local",
        agents_count=1,
        last_check=now
    ))

    return statuses


@get("/modes")
async def get_agent_modes() -> list[dict]:
    """Получить список доступных режимов агентов"""
    return [
        {
            "id": "brain",
            "name": "🧠 Tessbrain",
            "description": "Анализ базы знаний, поиск информации, инсайты",
            "icon": "brain",
            "color": "purple"
        },
        {
            "id": "mark",
            "name": "📈 Mark (Marketing)",
            "description": "Маркетинговый анализ, контент, стратегии",
            "icon": "trending-up",
            "color": "orange"
        },
        {
            "id": "automation",
            "name": "⚡ Автоматизация",
            "description": "Задачи, Web-браузер, MeetFlow, Calls",
            "icon": "zap",
            "color": "blue"
        },
        {
            "id": "transcripts",
            "name": "🎙️ Private LLM",
            "description": "Локальная модель в закрытом контуре",
            "icon": "message-circle",
            "color": "green"
        },
        {
            "id": "task_spec",
            "name": "📋 Генератор ТЗ",
            "description": "Анализ задачи, генерация ТЗ и планов выполнения",
            "icon": "clipboard-list",
            "color": "cyan"
        }
    ]


# === Task Specification System Endpoints ===

def _caller_user_id(authorization: Optional[str]) -> Optional[str]:
    """Best-effort извлечение user_id из Authorization header.

    Returns None если token отсутствует/невалиден. Mutating endpoints
    должны делать жёсткую проверку отдельно через RBAC.
    """
    if not authorization:
        return None
    try:
        from backend.api.middleware.auth_middleware import get_user_id_from_token
        return get_user_id_from_token(authorization)
    except Exception as e:
        logger.debug("auth extraction failed: %s", e)
        return None


async def get_task_spec_system(state: State,
                               user_id: Optional[str] = None) -> TaskSpecificationSystem:
    """Получить/создать TaskSpecificationSystem ДЛЯ КОНКРЕТНОГО ПОЛЬЗОВАТЕЛЯ.

    Аудит-фикс (understanding-layer): раньше был один process-global
    синглтон с ЗАХАРДКОЖЕННЫМ user_id — /tasks/process использовал граф
    чужого юзера для всех (мульти-тенант утечка) + замороженный граф (#7).
    Теперь per-user кэш (dict); пересоздаём, если число узлов выросло.

    Wave 2.3 (эта сессия): граф строится через federated view
    (personal ∪ org) — agent_oc видит данные команды (decisions/tasks/
    meetings, promoted в org-graph коллегами), а не только свои.
    """
    global _task_spec_systems
    if "_task_spec_systems" not in globals() or _task_spec_systems is None:
        _task_spec_systems = {}

    cache_key = (user_id or "__default__").strip() or "__default__"

    graph_builder = None
    current_node_count = 0
    if user_id:
        try:
            # Wave 2.3: federated view (personal ∪ org) вместо простого
            # per-user графа — объединяет мультитенант-фикс с командной
            # видимостью. Save заблокирован (read-only).
            from backend.core.store.graph_view import merged_graph_view_for_user
            graph_builder = await merged_graph_view_for_user(user_id, use_networkx=None)
            if getattr(graph_builder, "nx_graph", None):
                current_node_count = graph_builder.nx_graph.number_of_nodes()
            logger.info(f"📊 TaskSpec federated graph for user {user_id[:8]}: {current_node_count} nodes")
        except Exception as e:
            logger.warning(f"TaskSpec: could not build graph for {user_id}: {e}")

    cached = _task_spec_systems.get(cache_key)
    cached_nodes = getattr(cached, "_graph_node_count", -1) if cached else -1
    # пересоздаём, если нет кэша ИЛИ граф вырос (свежие данные — #7)
    if cached is None or (current_node_count > cached_nodes):
        llm_router = None
        try:
            from backend.core.llm.router import LLMRouter
            llm_router = LLMRouter()
        except Exception as e:
            logger.warning(f"Could not create LLMRouter: {e}")

        vector_indexer = None
        if user_id:
            try:
                from backend.core.store.tenant_paths import vector_index_path_for_user
                from backend.core.store.vector_indexer import VectorIndexer
                vector_indexer = VectorIndexer(
                    use_qdrant=None,
                    storage_path=vector_index_path_for_user(user_id),
                    namespace=user_id,
                )
                await vector_indexer.connect()
            except Exception as e:
                logger.warning(f"TaskSpec: could not build VectorIndexer: {e}")

        storage = None
        if graph_builder:
            storage = {
                "graph_builder": graph_builder,
                "nx_graph": getattr(graph_builder, "nx_graph", None),
                "vector_indexer": vector_indexer,
            }

        system = create_task_specification_system(
            storage=storage, llm_router=llm_router, output_dir="task_specifications")
        system._graph_node_count = current_node_count
        _task_spec_systems[cache_key] = system
        logger.info(f"✅ TaskSpecificationSystem[{cache_key[:8]}] init: {current_node_count} nodes")

    return _task_spec_systems[cache_key]


@post("/tasks/process")
async def process_task(
    data: TaskSpecRequest,
    state: State,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> TaskSpecResponse:
    """
    Обработка задачи через DataDrivenTaskSystem (6 data-driven этапов:
    понимание → план поиска → сбор данных из графа → анализ полноты →
    сборка контекста → ГЕНЕРАЦИЯ ТЗ).

    ВАЖНО (аудит): это НЕ 4-агентный пайплайн Analyzer/ContextSearch/SpecGen —
    те классы не вызываются. Реальный путь — data-driven движок (генерация ТЗ).
    ВЫПОЛНЕНИЕ (supervised/autonomous через TaskExecutorAgent) ПОДКЛЮЧЕНО, но
    реальный запуск инструментов — только за флагом enable_autonomous_execution
    (default OFF → деградация в plan_only: детальный план без действий). При
    сбое LLM-этапов статус = 'completed_degraded'. См. docs/ru/PRODUCTION_HARDENING.md.
    """
    start_time = datetime.now()

    try:
        # per-user граф/векторы (аудит-фикс мульти-тенант утечки):
        # анти-IDOR — body user_id ОБЯЗАН совпасть с токеном, иначе игнор и
        # берём uid из токена (раньше body-id побеждал → доступ к чужому графу).
        from backend.core.auth.user_guard import resolve_user_or_none
        _uid = resolve_user_or_none(authorization, data.user_id, scope="agents/tasks") \
            or _caller_user_id(authorization)
        system = await get_task_spec_system(state, user_id=_uid)

        # user_id в контекст оркестратора: без него молча скипались wisdom-brief,
        # граф-обогащение и маршрут модели по выбору тенанта (workload_policy)
        _ctx = {**(data.additional_context or {})}
        if _uid and not _ctx.get("user_id"):
            _ctx["user_id"] = _uid

        result = await system.process_task(
            task_description=data.task_description,
            additional_context=_ctx,
            execution_mode=data.execution_mode,
            skip_execution=data.skip_execution
        )

        execution_time = int((datetime.now() - start_time).total_seconds() * 1000)

        # Извлекаем данные из результата (новая data-driven система)
        stages = result.get("stages", {})
        final_result = stages.get("result", {})
        exec_plan = stages.get("execution", {})

        # Markdown может быть в разных местах
        markdown_spec = (
            final_result.get("markdown") or
            stages.get("specification", {}).get("markdown") or
            ""
        )

        return TaskSpecResponse(
            success=result.get("status") == "completed",
            task_id=result.get("task_id", ""),
            status=result.get("status", "unknown"),
            specification_id=final_result.get("title", ""),
            stages_completed=list(stages.keys()),
            markdown_spec=markdown_spec,
            execution_plan=exec_plan.get("detailed_plan") if exec_plan else None,
            file_paths=result.get("file_paths", {}),
            errors=result.get("errors", []),
            execution_time_ms=execution_time
        )

    except Exception as e:
        logger.error(f"Task processing error: {e}")
        import traceback
        logger.error(traceback.format_exc())

        return TaskSpecResponse(
            success=False,
            task_id="",
            status="error",
            errors=[str(e)],
            execution_time_ms=int((datetime.now() - start_time).total_seconds() * 1000)
        )


@get("/tasks/{task_id:str}/status")
async def get_task_status(task_id: str, state: State) -> dict:
    """Получить статус задачи (ищем по всем per-user системам)."""
    try:
        # per-user кэш: задача могла быть создана под любым user_id
        for system in (_task_spec_systems or {}).values():
            status = system.get_task_status(task_id)
            if status:
                return {"success": True, "status": status}
        return {"success": False, "error": f"Task {task_id} not found"}

    except Exception as e:
        return {"success": False, "error": str(e)}


@get("/tasks/history")
async def get_tasks_history(state: State) -> dict:
    """Получить историю обработанных задач (по всем per-user системам)."""
    try:
        history = []
        for system in (_task_spec_systems or {}).values():
            history.extend(system.get_processing_history())

        # Возвращаем краткую информацию
        summary = [
            {
                "task_id": item.get("task_id"),
                "task_description": item.get("task_description", "")[:100],
                "status": item.get("status"),
                "stages_completed": list(item.get("stages", {}).keys()),
                "started_at": item.get("metadata", {}).get("started_at"),
                "completed_at": item.get("metadata", {}).get("completed_at")
            }
            for item in history
        ]

        return {"success": True, "tasks": summary, "total": len(summary)}

    except Exception as e:
        return {"success": False, "error": str(e)}


# === Временные файлы чата (скрепка) ===

class ChatUploadRequest(BaseModel):
    """Файл, прикреплённый к чату скрепкой. content — Data URL (base64)
    или чистый текст. НЕ попадает в базу знаний — только контекст диалога."""
    filename: str
    content: str
    session_id: Optional[str] = None


def _chat_upload_dir(user_id: str):
    """Папка временных файлов чата ДЛЯ КОНКРЕТНОГО пользователя.

    Раньше все файлы лежали в одной общей папке, а читались просто по id
    (`data/chat_uploads/<id>.txt`). Никакой привязки к владельцу не было —
    значит, зная или подобрав идентификатор, можно было втянуть чужой
    загруженный файл в свой диалог. Идентификатор из 10 hex-символов — это
    не право доступа, а надежда, что не угадают.

    Теперь владелец зашит в путь, и файл другого пользователя недостижим
    по построению, а не по сложности перебора.
    """
    from pathlib import Path as _P
    safe_uid = "".join(c for c in str(user_id) if c.isalnum() or c in "-_")[:64]
    if not safe_uid:
        return None
    return _P("data") / "chat_uploads" / safe_uid


# Разобранный текст режется до 500 000 символов ниже, но парсер работает с
# СЫРЫМИ байтами: docx/xlsx — это zip-архивы, и распаковка «маленького»
# файла может занять гигабайты. Общий лимит тела 100 МБ для этого слишком
# щедрый, поэтому здесь своя, более узкая граница.
_CHAT_UPLOAD_MAX_BYTES = 25 * 1024 * 1024


@post("/chat-upload")
async def chat_upload(
    data: ChatUploadRequest,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> Dict[str, Any]:
    """Принять ВРЕМЕННЫЙ файл для чата: распарсить (pdf/docx/xlsx/txt через
    FileParser документов), сохранить текст в data/chat_uploads/<uid>/<id>.txt.
    В базу знаний не пишется; файлы старше 7 дней чистятся при каждой загрузке.

    Требует авторизации. Раньше её тут не было вовсе: ручка принимала до
    100 МБ от кого угодно и гоняла на них парсер офисных форматов — то есть
    бесплатно жгла процессор и диск по запросу из интернета. Фронт токен и
    так присылал, просто сервер его не смотрел.
    """
    import base64 as _b64
    import uuid as _uuid

    uid = _caller_user_id(authorization)
    if not uid:
        return {"success": False, "error": "Требуется авторизация"}

    try:
        updir = _chat_upload_dir(uid)
        if updir is None:
            return {"success": False, "error": "Требуется авторизация"}
        updir.mkdir(parents=True, exist_ok=True)

        # Ретеншн: «закинул и больше не используем» — старое удаляется само
        try:
            import time as _time
            cutoff = _time.time() - 7 * 86400
            for old in updir.glob("*"):
                if old.is_file() and old.stat().st_mtime < cutoff:
                    old.unlink(missing_ok=True)
        except Exception:
            logger.debug("chat uploads retention skipped", exc_info=True)

        text = ""
        raw = data.content or ""
        if raw.startswith("data:"):
            try:
                _header, encoded = raw.split(",", 1)
                blob = _b64.b64decode(encoded)
            except Exception as e:
                return {"success": False, "error": f"base64 не декодируется: {e}"}
            if len(blob) > _CHAT_UPLOAD_MAX_BYTES:
                return {"success": False, "error": (
                    f"Файл больше {_CHAT_UPLOAD_MAX_BYTES // (1024 * 1024)} МБ — "
                    "прикрепите файл поменьше.")}
            try:
                from backend.core.documents.file_parser import FileParser
                parsed, ftype = FileParser.parse_file(blob, data.filename)
                text = parsed or ""
            except Exception as e:
                logger.warning(f"chat upload parse failed: {e}")
            if not text:
                try:
                    text = blob.decode("utf-8")
                except Exception:
                    return {"success": False,
                            "error": ("Не удалось извлечь текст из файла — "
                                      "поддерживаются pdf/docx/xlsx/txt/md/csv")}
        else:
            text = raw

        text = text.replace("\x00", "").strip()
        if len(text) < 5:
            return {"success": False, "error": "Файл пустой или нечитаемый"}

        fid = f"tmp_{_uuid.uuid4().hex[:10]}"
        (updir / f"{fid}.txt").write_text(text[:500_000], encoding="utf-8")
        logger.info(f"📎 chat upload: {data.filename} → {fid} "
                    f"({len(text)} chars, session={data.session_id})")
        return {"success": True, "file_id": fid,
                "name": data.filename, "chars": len(text)}
    except Exception as e:
        logger.error(f"chat upload failed: {e}")
        return {"success": False, "error": str(e)}


# === Файлы креативной студии Mark (картинки/презентации/отчёты) ===

@get("/creative-file/{name:str}")
async def get_creative_file(name: str) -> Any:
    """Отдать файл, созданный Mark (data/creative_out, data/presentations
    пакета mark001). Только имя файла — без путей (защита от traversal).
    Благодаря этому роуту картинки и отчёты открываются прямо из чата."""
    from pathlib import Path as _P

    from litestar.exceptions import NotFoundException
    from litestar.response import File
    safe = _P(name).name  # отрезает любые ../ и слэши
    if not safe or safe.startswith("."):
        raise NotFoundException()
    candidates = [
        _P("data") / "creative_out" / safe,
        _P(MARK001_PATH) / "data" / "presentations" / safe,
    ]
    for f in candidates:
        if f.is_file():
            media = {
                ".png": "image/png", ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg", ".html": "text/html",
                ".md": "text/markdown; charset=utf-8",
                ".pptx": ("application/vnd.openxmlformats-officedocument"
                          ".presentationml.presentation"),
                ".mp4": "video/mp4",
            }.get(f.suffix.lower(), "application/octet-stream")
            return File(path=f, filename=safe, media_type=media,
                        content_disposition_type=(
                            "inline" if media.startswith(("image/", "text/"))
                            else "attachment"))
    raise NotFoundException()


async def _has_telegram_recipient(user_id: str | None) -> bool:
    """Есть ли у пользователя куда слать в Telegram: бот-токен + чат/контакт
    (или env). Чтобы не создавать обречённые «No recipients» напоминания."""
    import os as _os
    if _os.getenv("TELEGRAM_CHAT_ID") and (_os.getenv("TELEGRAM_BOT_TOKEN")
                                           or _os.getenv("TESSENT_TESS_CHAT_ID")):
        return True
    url = _os.getenv("SUPABASE_URL", "").rstrip("/")
    key = _os.getenv("SUPABASE_KEY", "")
    if not url or not key or not user_id:
        return False
    try:
        import httpx
        headers = {"apikey": key, "Authorization": f"Bearer {key}"}
        params = {"user_id": f"eq.{user_id}", "provider": "eq.telegram",
                  "select": "key_name"}
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{url}/rest/v1/user_integrations",
                                 headers=headers, params=params)
            if r.status_code != 200:
                return False
            rows = r.json() or []
        # нужен и токен, и хотя бы один контакт (contact_*)
        has_token = any(not str(x.get("key_name", "")).startswith("contact_") for x in rows)
        has_contact = any(str(x.get("key_name", "")).startswith("contact_") for x in rows)
        return bool(rows) and (has_token or has_contact)
    except Exception:
        return False


async def _meetflow_fetch_meetings(user_id: str | None, limit: int = 12) -> list:
    """Последние встречи пользователя С САММАРИ (для контекста разговора).
    Прямой Supabase REST — не зависит от графа/пакета MeetFlow."""
    import os as _os
    url = _os.getenv("SUPABASE_URL", "").rstrip("/")
    key = _os.getenv("SUPABASE_KEY", "")
    if not url or not key or not user_id:
        return []
    import httpx
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    params = {
        "select": "id,meeting_id,title,created_at,summary,transcription_status",
        "order": "created_at.desc", "limit": str(limit),
        "user_id": f"eq.{user_id}", "status": "eq.active",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(f"{url}/rest/v1/meetings", headers=headers, params=params)
        r.raise_for_status()
        return r.json() or []


async def _meetflow_fetch_transcript(user_id: str | None, meeting_id: str,
                                     max_chars: int = 18000) -> str:
    """Транскрипт конкретной встречи (для «расскажи подробнее про X»)."""
    import os as _os
    url = _os.getenv("SUPABASE_URL", "").rstrip("/")
    key = _os.getenv("SUPABASE_KEY", "")
    if not url or not key:
        return ""
    import httpx
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    params = {"select": "transcription_text,title",
              "id": f"eq.{meeting_id}", "user_id": f"eq.{user_id}"}
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(f"{url}/rest/v1/meetings", headers=headers, params=params)
        if r.status_code != 200:
            return ""
        rows = r.json() or []
    if not rows:
        return ""
    return str(rows[0].get("transcription_text") or "")[:max_chars]


async def _meetflow_chat_answer(message: str, user_id: str | None,
                                chat_history: list | None,
                                model_tier: str = "standard") -> str:
    """Собеседник по встречам MeetFlow: отвечает на вопросы по данным
    встреч (список, саммари, содержание) обычным языком, с памятью диалога.
    Не поднимает браузер. Если встреч нет/Supabase не настроен — честно
    сообщает."""
    meetings = await _meetflow_fetch_meetings(user_id, limit=12)
    if not meetings:
        return ("Пока не вижу ваших встреч. Проверьте, что встречи "
                "обработаны в MeetFlow и заданы SUPABASE_URL/SUPABASE_KEY.")

    _msg_lc = (message or "").lower()
    # Контекст: список встреч с датами и саммари
    lines = []
    for m in meetings:
        date = str(m.get("created_at") or "")[:10]
        title = m.get("title") or "Без названия"
        summ = (m.get("summary") or "").strip()
        lines.append(f"- [{m.get('id')}] {date} «{title}»"
                     + (f": {summ[:300]}" if summ else " (саммари нет)"))
    context = "СПИСОК ВСТРЕЧ (последние):\n" + "\n".join(lines)

    # Если вопрос про содержание/подробности — дочитываем транскрипт самой
    # релевантной встречи (по совпадению слов заголовка или «последн»).
    wants_detail = any(k in _msg_lc for k in (
        "о чём", "о чем", "подробн", "расскажи", "что обсужд", "что решил",
        "детал", "содержан", "транскрипт", "итог", "резюме", "саммари"))
    if wants_detail:
        target = None
        import re as _re
        words = set(_re.findall(r"[а-яёa-z0-9]{4,}", _msg_lc))
        best, best_score = None, 0
        for m in meetings:
            tl = (m.get("title") or "").lower()
            score = sum(1 for w in words if w in tl)
            if score > best_score:
                best, best_score = m, score
        if best_score == 0 and any(k in _msg_lc for k in ("последн", "недавн")):
            best = meetings[0]
        target = best
        if target:
            tr = await _meetflow_fetch_transcript(user_id, str(target.get("id")))
            if tr:
                context += (f"\n\nТРАНСКРИПТ ВСТРЕЧИ «{target.get('title')}» "
                            f"(для подробного ответа):\n{tr}")

    # Память диалога
    hist = ""
    for m in (chat_history or [])[-6:]:
        role = "Пользователь" if m.get("role") == "user" else "Ассистент"
        c = str(m.get("content") or "")[:400]
        if c:
            hist += f"{role}: {c}\n"

    from backend.core.llm import get_llm_router
    from backend.core.llm.usage_tracker import UsageContext
    llm = get_llm_router()
    if llm is None:
        # LLM недоступен — хотя бы список отдадим
        return _format_meetflow_fastpath(
            __import__("json").dumps({"meetings": meetings}, ensure_ascii=False))
    prompt = (
        "Ты — ассистент по встречам компании. Отвечай на вопрос пользователя "
        "ТОЛЬКО по данным встреч ниже, на русском, по-человечески и по делу. "
        "Если просят список — дай аккуратный список с датами. Если спрашивают "
        "про содержание — опирайся на саммари/транскрипт. Если данных нет — "
        "честно скажи.\n\n"
        + context
        + (f"\n\nПРЕДЫДУЩИЙ ДИАЛОГ:\n{hist}" if hist else "")
        + f"\n\nВОПРОС: {message}")
    async with UsageContext(agent_mode="automation",
                            request_type="meetflow_chat", user_id=user_id):
        resp = await llm.generate(
            prompt=prompt, temperature=0.3, max_tokens=1500,
            **({"model_tier": __import__(
                "backend.core.llm.router", fromlist=["ModelTier"]
            ).ModelTier.PREMIUM} if model_tier == "premium" else {}))
    text = (resp.get("text", "") if isinstance(resp, dict)
            else str(resp or "")).strip()
    return text or _format_meetflow_fastpath(
        __import__("json").dumps({"meetings": meetings}, ensure_ascii=False))


def _format_meetflow_fastpath(raw_json: str) -> str:
    """Человекочитаемый ответ fast-path (0 LLM): сырой JSON со встречами/
    задачами → компактный markdown-список. Не смогли распарсить → как есть."""
    try:
        data = json.loads(raw_json)
    except Exception:
        return raw_json
    if isinstance(data, dict) and data.get("error"):
        return f"⚠️ {data['error']}"
    lines: list = []
    meetings = (data.get("meetings") if isinstance(data, dict) else None) or []
    tasks = (data.get("tasks") if isinstance(data, dict) else None) or []
    if meetings:
        lines.append(f"📅 **Встречи** ({len(meetings)}):")
        for m in meetings[:15]:
            if not isinstance(m, dict):
                continue
            date = str(m.get("created_at") or m.get("date") or "")[:10]
            title = m.get("title") or "Без названия"
            summ = (m.get("summary") or "").strip()
            line = f"- {date} · **{title}**"
            if summ:
                line += f" — {summ[:140]}"
            lines.append(line)
    if tasks:
        lines.append(f"\n✅ **Задачи** ({len(tasks)}):")
        for t in tasks[:20]:
            if not isinstance(t, dict):
                continue
            line = f"- {t.get('title') or t.get('name') or '?'}"
            bits = [b for b in (t.get("assignee"), t.get("due_date"),
                                t.get("status")) if b]
            if bits:
                line += f" ({', '.join(str(b) for b in bits)})"
            if t.get("meeting_title"):
                line += f" — из «{t['meeting_title']}»"
            lines.append(line)
    if not lines:
        return raw_json
    return "\n".join(lines)


# === Automation Endpoint (alias for agent_chat with automation mode) ===

@post("/automation")
async def automation_chat(
    data: AgentChatRequest,
    state: State,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> AgentChatResponse:
    """
    Эндпоинт для MeetFlow Automation.
    Это алиас для /chat с agent_mode="automation".
    """
    # Force automation mode
    data.agent_mode = "automation"
    # NOTE: In Litestar, decorated handlers are HTTPRouteHandler objects, not plain callables.
    # Call the underlying function via `.fn`. Пробрасываем токен — иначе
    # анти-IDOR в agent_chat не сработает (body user_id остался бы главным).
    return await agent_chat.fn(data=data, state=state, authorization=authorization)


# Router
router = Router(
    path="/agents",
    route_handlers=[
        agent_chat,
        automation_chat,  # Alias endpoint
        chat_upload,
        get_creative_file,
        get_agents_status,
        get_agent_modes,
        process_task,
        get_task_status,
        get_tasks_history,
    ],
    tags=["Agents"],
)
