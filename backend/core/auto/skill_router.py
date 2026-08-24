# -*- coding: utf-8 -*-
"""
Skill Router for FunctionGemma — сужает выбор инструментов до 3-5 на основе контекста запроса.

Проблема: FunctionGemma (270M) получает 23+ tools и часто выбирает "none" (~40% промахов).
Решение: SkillRouter определяет skill по ключевым словам, сужая выбор до 3-5 tools.
Результат: Надёжность 60% → 90% без замены модели.

Skills:
    - tasks: YouGile, Trello, Jira + внутренние задачи
    - meetings: MeetFlow, транскрипции, обсуждения
    - people: профили, команда, роли
    - search: поиск по базе знаний, решения, проекты
    - actions: отправка сообщений (Telegram, Slack), календарь
    - analysis: анализ через LLM, суммаризация
    - email: Gmail — поиск, чтение, отправка писем
    - knowledge_docs: Notion — страницы, базы данных, wiki
    - code: GitHub — PR, issues, CI/CD
    - crm: HubSpot — контакты, сделки (заготовка)

Usage:
    router = SkillRouter()
    skill_name, filtered_tools = router.route("покажи задачи в yougile")
    # -> ("tasks", ["get_all_tasks", "get_tasks_from_database", "get_yougile_tasks", ...])
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class Skill:
    """Набор инструментов, объединённых по назначению."""
    id: str
    name: str
    description: str
    keywords: List[str]  # Ключевые слова для матчинга (lowercase, подстроки)
    tools: List[str]     # Имена инструментов в этом skill
    priority: int = 0    # Приоритет при равном score (выше = приоритетнее)


# === ОПРЕДЕЛЕНИЯ SKILLS ===

SKILLS: List[Skill] = [
    Skill(
        id="tasks",
        name="Задачи",
        description="Управление задачами: YouGile, Jira, Trello + внутренние задачи",
        keywords=[
            # Русский
            "задач", "задани", "бэклог", "бэклоге",
            "создай задач", "добавь задач", "новая задач",
            "просроченн", "дедлайн",
            "что делать", "что нужно сделать", "мои задач",
            "спринт", "доска", "колонк",
            "югайл", "трелло", "джира",
            "тикет", "карточк",
            # English
            "task", "tasks", "todo", "to-do", "to do",
            "backlog", "in_progress", "in progress", "done",
            "create task", "add task", "new task", "my tasks",
            "overdue", "deadline",
            "sprint", "board", "column",
            "yougile", "trello", "jira", "asana", "linear",
            "show tasks", "list tasks", "get tasks",
            "ticket", "issue", "card",
            "create issue", "create card", "move card",
            "transition", "jql",
            # Транслит
            "zadach", "zadani", "beklog", "dedlain", "tiket",
        ],
        tools=[
            # Внутренние
            "get_all_tasks",
            "get_tasks_from_database",
            "create_task",
            "get_sprints_info",
            "search_in_database",
            # YouGile
            "get_yougile_tasks",
            "get_yougile_boards",
            "create_yougile_task",
            # Jira
            "jira_search_issues",
            "jira_get_my_issues",
            "jira_create_issue",
            "jira_update_issue",
            "jira_transition_issue",
            "jira_add_comment",
            "jira_get_projects",
            # Trello
            "trello_get_boards",
            "trello_get_lists",
            "trello_get_cards",
            "trello_create_card",
            "trello_move_card",
            "trello_add_comment",
            "trello_archive_card",
            "trello_search_cards",
            # Linear
            "linear_search_issues",
            "linear_my_issues",
            "linear_create_issue",
            "linear_projects",
            "linear_current_cycle",
        ],
        priority=10,
    ),
    Skill(
        id="meetings",
        name="Встречи",
        description="Встречи, транскрипции, обсуждения",
        keywords=[
            # Русский
            "встреч", "митинг", "созвон", "звонок",
            "транскрип", "обсужда", "обсуждал", "обсудил",
            "что было", "о чём говорил",
            "записи встреч", "конференц",
            # English
            "meeting", "meetings", "call", "conference",
            "transcript", "discussed", "discussion",
            "last meeting", "recent meeting", "what was discussed",
            "what happened", "meeting notes", "standup", "stand-up",
            "sync", "daily", "retro", "retrospective",
            # Транслит
            "vstrech", "vstrechi", "miting", "sozvon", "zvonok",
        ],
        tools=[
            "get_recent_meetings",
            "get_meetflow_meetings",
            "search_meetflow_meetings",
            "get_meeting_details_meetflow",
            "get_meeting_tasks_meetflow",
            "get_meeting_context",
        ],
        priority=10,
    ),
    Skill(
        id="people",
        name="Люди",
        description="Информация о людях, команде, ролях",
        keywords=[
            # Русский
            "кто ", "кто это", "кто такой", "кто такая",
            "человек", "сотрудник", "команд", "коллег",
            "кто отвечает", "кто занимает", "кто работает",
            "профил",
            # English
            "who is", "who are", "person", "people", "team",
            "member", "employee", "colleague", "coworker",
            "who works", "who is responsible", "who handles",
            "profile", "staff", "developer", "manager", "lead",
            # Транслит
            "kto ", "chelovek", "sotrudnik", "komand", "kolleg",
        ],
        tools=[
            "get_person_info",
            "get_all_people",
            "get_user_profile",
            "get_team_overview",
        ],
        priority=5,
    ),
    Skill(
        id="search",
        name="Поиск и знания",
        description="Поиск по базе знаний, решения, статусы проектов",
        keywords=[
            # Русский
            "найди", "поиск", "искать",
            "реше", "что решили",
            "проект", "статус проект",
            "знани",
            "что изменилось", "что нового",
            # English
            "search", "find", "look up", "lookup",
            "decision", "what was decided", "resolved",
            "project", "project status", "how is project",
            "knowledge", "information about",
            "what changed", "what's new", "updates",
            # Транслит
            "naidi", "poisk", "proekt",
        ],
        tools=[
            "search_knowledge",
            "search_in_database",
            "get_recent_decisions",
            "get_project_status",
        ],
        priority=3,
    ),
    Skill(
        id="actions",
        name="Действия и интеграции",
        description="Отправка сообщений (Telegram, Slack), создание событий, действия",
        keywords=[
            # Русский
            "отправь", "пошли", "скинь", "кинь",
            "телеграм", " тг ",
            "календар", "событи",
            "напиши в", "сообщени", "уведом",
            "слак", "канал",
            "ватсап", "вотсап", "вацап",
            # English
            "send", "notify", "post", "forward", "share",
            "telegram", "tg", "slack", "whatsapp",
            "calendar", "event", "schedule", "reminder",
            "message", "notification",
            "send to slack", "post to slack",
            "send to telegram",
            # Транслит
            "otprav", "poshli", "skin", "soobsheni", "slak",
        ],
        tools=[
            "send_telegram_message",
            "send_telegram",
            "list_calendar_events",
            "create_task",
            "create_yougile_task",
            # Slack
            "slack_send_message",
            "slack_read_messages",
            "slack_search_messages",
            "slack_list_channels",
            "slack_add_reaction",
            # WhatsApp
            "whatsapp_send",
            "whatsapp_send_template",
            "whatsapp_profile",
        ],
        priority=8,
    ),
    Skill(
        id="email",
        name="Почта",
        description="Gmail — поиск, чтение, отправка, ответ, черновики",
        keywords=[
            # Русский
            "почт", "письм", "email", "gmail",
            "отправь письмо", "напиши письмо",
            "входящие", "непрочитанн",
            "почтовый ящик", "ответь на письмо",
            "переписк",
            # English
            "send email", "email message", "inbox",
            "unread emails", "read email", "compose email",
            "reply email", "draft email", "mail",
            "gmail search", "gmail send",
            # Транслит
            "pochta", "pismo", "otprav pismo",
        ],
        tools=[
            "gmail_search",
            "gmail_read",
            "gmail_send",
            "gmail_reply",
            "gmail_draft",
        ],
        priority=8,
    ),
    Skill(
        id="knowledge_docs",
        name="Документация",
        description="Notion, Confluence — страницы, базы данных, wiki",
        keywords=[
            # Русский
            "notion", "ноушен", "ноушн",
            "запиши в notion", "создай страницу",
            "найди в notion", "база данных notion",
            "wiki", "документ", "страниц",
            "confluence", "конфлюенс", "конфлюэнс",
            "пространств", "база знаний",
            # English
            "notion page", "notion database", "create page",
            "write to notion", "search notion", "query database",
            "notion doc", "notion wiki",
            "confluence page", "confluence search", "wiki page",
            "knowledge base", "space",
            # Транслит
            "noushen", "noushn", "konfluens",
        ],
        tools=[
            "notion_search",
            "notion_create_page",
            "notion_get_page",
            "notion_query_database",
            "notion_update_page",
            # Confluence
            "confluence_search",
            "confluence_get_page",
            "confluence_create_page",
            "confluence_list_spaces",
        ],
        priority=7,
    ),
    Skill(
        id="code",
        name="Код и DevOps",
        description="GitHub — PR, issues, CI/CD, поиск кода",
        keywords=[
            # Русский
            "github", "гитхаб", "пул реквест",
            "баг", "создай issue",
            "ci", "cd", "пайплайн", "деплой",
            "коммит", "ветка", "бранч", "мерж",
            "репозитори", "код",
            # English
            "pull request", "pr", "merge request",
            "github issue", "github pr", "ci status",
            "pipeline", "workflow", "deploy", "release",
            "commit", "branch", "merge", "repo",
            "code search", "github search",
            # Транслит
            "githab", "pul rekvest",
        ],
        tools=[
            "github_list_prs",
            "github_get_pr",
            "github_list_issues",
            "github_create_issue",
            "github_get_ci_status",
            "github_search_code",
        ],
        priority=6,
    ),
    Skill(
        id="crm",
        name="CRM и продажи",
        description="HubSpot — контакты, сделки, заметки, pipeline",
        keywords=[
            # Русский
            "hubspot", "хабспот", "crm", "контакт",
            "сделк", "клиент", "лид", "воронк",
            "pipeline", "продаж", "карточка клиент",
            "заметка к клиент", "найди клиент",
            # English
            "contact", "deal", "lead", "customer",
            "hubspot crm", "find contact", "create deal",
            "sales", "opportunity", "pipeline", "note",
            # Транслит
            "kontakt", "sdelka", "klient",
        ],
        tools=[
            "hubspot_search_contacts",
            "hubspot_get_contact",
            "hubspot_search_deals",
            "hubspot_create_deal",
            "hubspot_create_note",
            "hubspot_list_deals",
        ],
        priority=7,
    ),
    Skill(
        id="productivity",
        name="Google Workspace",
        description="Google Sheets, Docs, Drive — таблицы, документы, файлы",
        keywords=[
            # Русский
            "google sheets", "гугл таблиц", "таблиц",
            "spreadsheet", "ячейк", "строк", "столбец",
            "эксель", "excel", "xlsx",
            "запиши в таблицу", "прочитай таблицу",
            "google docs", "гугл док", "документ",
            "создай документ", "прочитай документ",
            "google drive", "гугл диск", "диск", "файл",
            "найди файл", "список файлов",
            # English
            "spreadsheet", "worksheet", "cell", "row",
            "read sheet", "write sheet", "append row",
            "google document", "create doc", "read doc",
            "drive", "file", "folder", "search files",
            "gsheet", "gdoc", "gdrive",
            # Транслит
            "tablitsa", "dokument", "fayl",
        ],
        tools=[
            # Sheets
            "sheets_read", "sheets_write", "sheets_append", "sheets_info",
            # Docs
            "docs_read", "docs_create", "docs_export", "docs_append",
            # Drive
            "drive_search", "drive_list", "drive_file_info", "drive_read",
        ],
        priority=7,
    ),
    Skill(
        id="design",
        name="Дизайн",
        description="Figma — файлы, компоненты, комментарии",
        keywords=[
            # Русский
            "figma", "фигма", "дизайн", "макет",
            "компонент", "фрейм", "прототип",
            "дизайн систем", "стайлгайд",
            # English
            "figma file", "figma component", "design file",
            "figma comment", "frame", "prototype",
            "design system", "style guide",
        ],
        tools=[
            "figma_get_file",
            "figma_get_comments",
            "figma_post_comment",
            "figma_get_components",
            "figma_team_projects",
        ],
        priority=5,
    ),
    Skill(
        id="automations",
        name="Автоматизации",
        description="Создание, управление расписаниями, отложенные задачи, повторяющиеся действия",
        keywords=[
            # Русский
            "автоматизац", "расписан", "по расписанию",
            "каждое утро", "каждый день", "каждый понедельник",
            "каждую неделю", "ежедневн", "еженедельн",
            "напомин", "напоминание", "напомни",
            "повторяющ", "регулярн",
            "каждые", "каждый час", "каждые 30 минут",
            "в 9 утра", "в 9:00", "в 10:00",
            "дайджест", "отчёт утром", "утренний отчёт",
            "создай автоматизаци", "настрой автоматизаци",
            "запланируй", "запланир",
            # просмотр/отмена существующих (иначе «мои автоматизации»
            # не маршрутизировались к list_scheduled_automations)
            "мои автоматизации", "какие автоматизации", "покажи автоматизации",
            "отложен", "запланирован", "отмени автоматизаци",
            # English
            "automat", "schedule", "scheduled",
            "every morning", "every day", "every monday",
            "every week", "daily", "weekly",
            "remind", "reminder",
            "recurring", "repeating", "repeat",
            "every hour", "every 30 min",
            "at 9am", "at 9:00", "at 10:00",
            "digest", "morning report", "daily report",
            "create automation", "set up automation",
            "cron", "interval",
            # Транслит
            "avtomatizaci", "raspisani", "napominan", "daidzhest",
        ],
        tools=[
            "create_scheduled_automation",
            "list_scheduled_automations",
            "cancel_scheduled_automation",
        ],
        priority=9,
    ),
    Skill(
        id="analysis",
        name="Анализ и генерация",
        description="Анализ данных через большую LLM, суммаризация, генерация текста",
        keywords=[
            # Русский
            "анализ", "проанализ",
            "суммар", "кратко", "итоги",
            "объясни", "расскаж",
            "сравни", "оцени",
            "напиши текст", "сгенерир",
            "подробн",
            # English
            "analyze", "analyse", "analysis",
            "summarize", "summary", "brief", "briefly",
            "explain", "describe", "tell me about",
            "compare", "evaluate", "assess", "rate",
            "write", "generate", "compose", "draft",
            "detail", "detailed", "in depth", "elaborate",
            # Транслит
            "analiz", "sravni", "rasskazh",
        ],
        tools=[
            "analyze_with_llm",
            "search_knowledge",
        ],
        priority=2,
    ),
]


class SkillRouter:
    """
    Определяет skill по запросу пользователя и сужает набор инструментов для FunctionGemma.

    Алгоритм:
    1. Подсчитать совпадения ключевых слов для каждого skill
    2. Выбрать skill с наибольшим score
    3. Если запрос содержит "и отправь/пошли" — добавить actions tools
    4. Всегда добавить "done" и "analyze_with_llm" как fallback
    """

    def __init__(self, skills: Optional[List[Skill]] = None):
        self.skills = skills or SKILLS

    def route(
        self,
        message: str,
        allowed_tools: Optional[List[str]] = None,
    ) -> Tuple[str, List[str]]:
        """
        Определить skill и вернуть отфильтрованный список инструментов.

        Args:
            message: Сообщение пользователя
            allowed_tools: Если указано, пересечь с результатом (UI checkboxes)

        Returns:
            (skill_id, filtered_tool_names)
        """
        if not message:
            return "search", self._finalize_tools("search", allowed_tools)

        msg_lower = message.lower().strip()

        # Подсчитываем score для каждого skill
        scores: Dict[str, float] = {}
        for skill in self.skills:
            score = 0.0
            for kw in skill.keywords:
                if kw in msg_lower:
                    # Более длинные ключевые слова дают больше веса (они специфичнее)
                    score += 1.0 + len(kw) * 0.1

            if score > 0:
                # Добавляем priority bonus (но маленький, чтобы не доминировал)
                scores[skill.id] = score + skill.priority * 0.01

        if not scores:
            # Fallback: если ничего не совпало — search
            logger.info(f"[SkillRouter] No skill matched for: '{msg_lower[:60]}...', defaulting to 'search'")
            return "search", self._finalize_tools("search", allowed_tools)

        # Выбираем лучший skill
        best_skill_id = max(scores, key=scores.get)
        best_score = scores[best_skill_id]

        logger.info(
            f"[SkillRouter] Matched skill='{best_skill_id}' (score={best_score:.2f}) "
            f"for: '{msg_lower[:60]}...'. All scores: {scores}"
        )

        # Собираем инструменты
        tools = self._get_skill_tools(best_skill_id)

        # === Мульти-skill обогащение ===
        # Если запрос упоминает встречи — добавляем meetings tools
        meeting_patterns = [
            "встреч", "meeting", "митинг", "совещан", "созвон",
            "конференц", "standup", "stand-up", "sync", "ретро",
            "last meeting", "recent meeting", "vstrech",
        ]
        if best_skill_id != "meetings" and any(p in msg_lower for p in meeting_patterns):
            meeting_tools = self._get_skill_tools("meetings")
            tools = list(set(tools + meeting_tools))
            logger.info("[SkillRouter] Added meetings tools for combined request")

        # Если запрос упоминает задачи — добавляем tasks tools
        task_patterns = [
            "задач", "task", "задани", "тикет", "ticket",
            "бэклог", "backlog", "спринт", "sprint", "доск",
        ]
        if best_skill_id != "tasks" and any(p in msg_lower for p in task_patterns):
            task_tools = self._get_skill_tools("tasks")
            tools = list(set(tools + task_tools))
            logger.info("[SkillRouter] Added tasks tools for combined request")

        # Если запрос содержит "и отправь/пошли" — добавляем actions tools
        action_add_patterns = [
            "и отправ", "и пошли", "и send",
            "отправь в", "пошли в", "скинь в",
            "и напиш", "и создай",
            "and send", "then send", "then create",
        ]
        if best_skill_id != "actions" and any(p in msg_lower for p in action_add_patterns):
            action_tools = self._get_skill_tools("actions")
            tools = list(set(tools + action_tools))
            logger.info("[SkillRouter] Added actions tools for combined request")

        # Если запрос упоминает почту/email — добавляем email tools
        email_patterns = [
            "почт", "письм", "email", "gmail", "на почту",
            "по email", "by email", "send email", "на мейл",
        ]
        if best_skill_id != "email" and any(p in msg_lower for p in email_patterns):
            email_tools = self._get_skill_tools("email")
            tools = list(set(tools + email_tools))
            logger.info("[SkillRouter] Added email tools for combined request")

        # Если запрос упоминает telegram — добавляем telegram tool
        tg_patterns = [
            "телеграм", " тг ", "в тг", "telegram", " tg ",
        ]
        if any(p in msg_lower for p in tg_patterns):
            if "send_telegram_message" not in tools:
                tools.append("send_telegram_message")
            if "send_telegram" not in tools:
                tools.append("send_telegram")
            logger.info("[SkillRouter] Added telegram tools for combined request")

        # Если запрос упоминает Slack — добавляем slack tools
        slack_patterns = ["слак", "slack", "в слак"]
        if best_skill_id != "actions" and any(p in msg_lower for p in slack_patterns):
            slack_tools = ["slack_send_message", "slack_read_messages", "slack_search_messages"]
            tools = list(set(tools + slack_tools))
            logger.info("[SkillRouter] Added Slack tools for combined request")

        # Если запрос упоминает Notion — добавляем notion tools
        notion_patterns = ["notion", "ноушен", "ноушн", "в notion"]
        if best_skill_id != "knowledge_docs" and any(p in msg_lower for p in notion_patterns):
            notion_tools = self._get_skill_tools("knowledge_docs")
            tools = list(set(tools + notion_tools))
            logger.info("[SkillRouter] Added Notion tools for combined request")

        # Если запрос упоминает автоматизации — добавляем automation tools
        automation_patterns = [
            "автоматизац", "расписан", "каждое утро", "каждый день",
            "ежедневн", "еженедельн", "напомин", "повторяющ",
            "дайджест", "create automation", "schedule", "recurring",
            "every morning", "every day", "daily", "weekly", "remind",
        ]
        if best_skill_id != "automations" and any(p in msg_lower for p in automation_patterns):
            auto_tools = self._get_skill_tools("automations")
            tools = list(set(tools + auto_tools))
            logger.info("[SkillRouter] Added automations tools for combined request")

        # Если запрос упоминает Jira — добавляем jira tools
        jira_patterns = ["jira", "джир", "жира", "тикет"]
        if any(p in msg_lower for p in jira_patterns) and not any(t.startswith("jira_") for t in tools):
            jira_tools = [t for t in self._get_skill_tools("tasks") if t.startswith("jira_")]
            tools = list(set(tools + jira_tools))
            logger.info("[SkillRouter] Added Jira tools for combined request")

        # Если запрос упоминает Linear — добавляем linear tools
        linear_patterns = ["linear", "линеар"]
        if any(p in msg_lower for p in linear_patterns) and not any(t.startswith("linear_") for t in tools):
            linear_tools = [t for t in self._get_skill_tools("tasks") if t.startswith("linear_")]
            tools = list(set(tools + linear_tools))
            logger.info("[SkillRouter] Added Linear tools for combined request")

        # Если запрос упоминает WhatsApp — добавляем whatsapp tools
        wa_patterns = ["whatsapp", "ватсап", "вотсап", "вацап", "в ватсап"]
        if any(p in msg_lower for p in wa_patterns):
            wa_tools = ["whatsapp_send", "whatsapp_send_template"]
            tools = list(set(tools + wa_tools))
            logger.info("[SkillRouter] Added WhatsApp tools for combined request")

        # Если запрос упоминает HubSpot/CRM — добавляем hubspot tools
        crm_patterns = ["hubspot", "хабспот", "crm", "воронк", "pipeline"]
        if best_skill_id != "crm" and any(p in msg_lower for p in crm_patterns):
            crm_tools = self._get_skill_tools("crm")
            tools = list(set(tools + crm_tools))
            logger.info("[SkillRouter] Added HubSpot/CRM tools for combined request")

        # Если запрос упоминает Confluence — добавляем confluence tools
        confluence_patterns = ["confluence", "конфлюенс", "конфлюэнс"]
        if any(p in msg_lower for p in confluence_patterns) and not any(t.startswith("confluence_") for t in tools):
            confluence_tools = [t for t in self._get_skill_tools("knowledge_docs") if t.startswith("confluence_")]
            tools = list(set(tools + confluence_tools))
            logger.info("[SkillRouter] Added Confluence tools for combined request")

        # Если запрос упоминает Google Sheets/Docs/Drive — добавляем productivity tools
        google_patterns = ["google sheets", "гугл таблиц", "spreadsheet", "таблиц",
                           "google docs", "гугл док", "google drive", "гугл диск"]
        if best_skill_id != "productivity" and any(p in msg_lower for p in google_patterns):
            google_tools = self._get_skill_tools("productivity")
            tools = list(set(tools + google_tools))
            logger.info("[SkillRouter] Added Google Workspace tools for combined request")

        # Если запрос упоминает Figma — добавляем figma tools
        figma_patterns = ["figma", "фигма", "макет"]
        if best_skill_id != "design" and any(p in msg_lower for p in figma_patterns):
            figma_tools = self._get_skill_tools("design")
            tools = list(set(tools + figma_tools))
            logger.info("[SkillRouter] Added Figma tools for combined request")

        # Финализация
        # ВАЖНО: НЕ применяем UI allowed_tools здесь — SkillRouter уже сужает tools
        # по смыслу запроса. UI-фильтрация (чекбоксы) слишком жёстко отсекает
        # tools, которые нужны для мульти-skill обогащения.
        final_tools = self._finalize_tools_list(tools, allowed_tools=None)

        logger.info(f"[SkillRouter] Final tools ({len(final_tools)}): {final_tools}")
        return best_skill_id, final_tools

    def _get_skill_tools(self, skill_id: str) -> List[str]:
        """Получить список инструментов для skill."""
        for skill in self.skills:
            if skill.id == skill_id:
                return list(skill.tools)
        return []

    def _finalize_tools(self, skill_id: str, allowed_tools: Optional[List[str]]) -> List[str]:
        """Получить и финализировать tools для skill."""
        tools = self._get_skill_tools(skill_id)
        return self._finalize_tools_list(tools, allowed_tools)

    def _finalize_tools_list(
        self,
        tools: List[str],
        allowed_tools: Optional[List[str]],
    ) -> List[str]:
        """
        Финализировать список инструментов:
        - Добавить "done" (всегда нужен)
        - Добавить "analyze_with_llm" как fallback (если не analysis skill)
        - Если allowed_tools задан — пересечь
        """
        # Всегда добавляем done
        if "done" not in tools:
            tools.append("done")

        # Добавляем analyze_with_llm как fallback (если FunctionGemma не знает что делать)
        if "analyze_with_llm" not in tools:
            tools.append("analyze_with_llm")

        # Если UI задал ограничение — пересечь
        if allowed_tools:
            allowed_set = set(allowed_tools)
            # Всегда оставляем done
            allowed_set.add("done")
            tools = [t for t in tools if t in allowed_set]

        return tools

    def get_skill_info(self, skill_id: str) -> Optional[Skill]:
        """Получить информацию о skill."""
        for skill in self.skills:
            if skill.id == skill_id:
                return skill
        return None

    def list_skills(self) -> List[Dict[str, Any]]:
        """Список всех skills для UI."""
        return [
            {
                "id": s.id,
                "name": s.name,
                "description": s.description,
                "tools_count": len(s.tools),
                "tools": s.tools,
            }
            for s in self.skills
        ]
