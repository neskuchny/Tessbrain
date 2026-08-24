# -*- coding: utf-8 -*-
"""
Meeting Workflows - готовые автоматизации для работы со встречами.

Шаблоны:
1. После встречи → Извлечь данные → Отправить саммари в Telegram
2. После встречи → Извлечь задачи → Создать в YouGile
3. После встречи → Анализ → Отправить отчёт на email
4. Ежедневный дайджест встреч → Telegram
5. Еженедельный отчёт по задачам → Email/Notion
"""
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import partial
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# issue #110 пакет 2 (2026-06-15): локальные LLM_PRICING и calculate_llm_cost
# удалены — pricing-словарь теперь единственный, в backend/core/llm/usage_tracker.py
# (MODEL_PRICING). До правки эти 7 строк дублировали upstream-словарь и
# при расхождении цен показывали неверный $-расчёт, а главное — `_track_llm_usage`
# писал ТОЛЬКО локально (в self._llm_cost_usd), мимо глобального трекера, поэтому
# вызовы из 4 workflow-actions не попадали ни в /usage/by-meeting, ни в /usage/stats.
# Теперь _track_llm_usage делегирует в track_usage(), а meeting_id / surface
# приходят из cost_scope() (см. execute_workflow() ниже).
# Маппинг workflow_type → (surface, использует_meeting_id):
_WORKFLOW_SURFACE: Dict[str, str] = {
    "meeting_summary_telegram": "ingest",
    "meeting_tasks_yougile":    "ingest",
    "meeting_report_email":     "ingest",
    "meeting_notes_notion":     "ingest",
    "client_followup":          "ingest",
    "hr_candidate_report":      "ingest",
    "meeting_document":         "ingest",
    "commitments_tracker":      "ingest",
    "client_risk_alert":        "ingest",
    "sales_call_scorecard":     "ingest",
    "voice_of_customer":        "ingest",
    "one_on_one_pulse":         "ingest",
    "call_qa_review":           "ingest",
    "exec_weekly_brief":        "digest",
    "daily_meetings_digest":    "digest",
    "morning_briefing":         "digest",
    "weekly_tasks_report":      "digest",
    "weekly_meetings_summary":  "digest",
    "sync_overdue_tasks":       "automation",
    "deadline_reminder":        "automation",
}

# Paths
from backend.core.utils.meetflow_path import default_meetflow_path as _dmp
MEETFLOW_PATH = _dmp()


def _md_to_email_html(text: str) -> str:
    """Markdown-текст дайджеста → простой HTML для письма.

    Если текст уже HTML (отчёты meeting_report_email) — возвращаем как есть.
    Иначе: экранирование, **жирный**, заголовки #, переводы строк → <br>."""
    import html as _html
    import re as _re
    t = (text or "").strip()
    if t.startswith("<") and ("</" in t or "/>" in t):
        return t
    t = _html.escape(t)
    t = _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t, flags=_re.S)
    t = _re.sub(r"(?m)^#{1,6}\s*(.+)$", r"<b>\1</b>", t)
    # обёртка <div> обязательна: send_email определяет text/html по тому,
    # что тело начинается с «<» — иначе теги уйдут видимым текстом
    return "<div>" + t.replace("\n", "<br>\n") + "</div>"


@dataclass
class WorkflowTemplate:
    """Шаблон автоматизации"""
    id: str
    name: str
    description: str
    category: str  # meeting_processing | daily_digest | weekly_report | task_sync
    action_type: str
    default_schedule: str  # once | recurring
    default_cron: Optional[str] = None
    default_interval: Optional[int] = None
    parameters: Dict[str, Any] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "action_type": self.action_type,
            "default_schedule": self.default_schedule,
            "default_cron": self.default_cron,
            "default_interval": self.default_interval,
            "parameters": self.parameters or {}
        }


# Готовые шаблоны автоматизаций
WORKFLOW_TEMPLATES: List[WorkflowTemplate] = [
    # === Обработка после встречи ===
    WorkflowTemplate(
        id="meeting_summary_telegram",
        name="📱 Саммари встречи → Telegram",
        description="После завершения встречи извлекает ключевые моменты и отправляет саммари в Telegram",
        category="meeting_processing",
        action_type="meeting_summary_telegram",
        default_schedule="once",
        parameters={
            "include_decisions": True,
            "include_tasks": True,
            "include_participants": True,
            "include_next_steps": True,
            "include_next_agenda": False,
            "max_length": 2000
        }
    ),
    WorkflowTemplate(
        id="meeting_tasks_yougile",
        name="✅ Задачи из встречи → Трекер",
        description="Извлекает задачи из транскрипции и создаёт их в вашем таск-трекере: YouGile, Trello или Jira (выбирается в настройке)",
        category="meeting_processing",
        action_type="meeting_tasks_yougile",
        default_schedule="once",
        parameters={
            "tracker": "yougile",
            "auto_assign": True,
            "extract_deadlines": True
        }
    ),
    WorkflowTemplate(
        id="meeting_report_email",
        name="📧 Отчёт по встрече → Email",
        description="Формирует детальный отчёт по встрече и отправляет на email участникам",
        category="meeting_processing",
        action_type="meeting_report_email",
        default_schedule="once",
        parameters={
            "include_transcript_summary": True,
            "include_action_items": True,
            "include_decisions": True,
            "include_next_agenda": False,
            "format": "html"
        }
    ),
    WorkflowTemplate(
        id="meeting_notes_notion",
        name="📝 Заметки встречи → Notion",
        description="Создаёт структурированную страницу в Notion с заметками встречи",
        category="meeting_processing",
        action_type="meeting_notes_notion",
        default_schedule="once",
        parameters={
            "template": "meeting_notes",
            "include_attendees": True,
            "include_action_items": True
        }
    ),

    # === Бизнес-сценарии по папкам ===
    WorkflowTemplate(
        id="client_followup",
        name="🤝 Саммари клиенту после встречи",
        description="Прошла встреча с клиентом (например, в папке «Клиенты») — клиенту автоматически уходит аккуратное письмо: что обсудили, договорённости, следующие шаги. Формат и тон можно задать своим промптом.",
        category="meeting_processing",
        action_type="client_followup",
        default_schedule="once",
        parameters={
            "to_email": "",
            "channel": "email",
            "custom_prompt": ""
        }
    ),
    WorkflowTemplate(
        id="commitments_tracker",
        name="🤝 Кто что пообещал — в чат",
        description="После встречи извлекает обещания и договорённости (кто, что, к какому сроку) и присылает списком в чат команды. Обещаний не было — молчит.",
        category="meeting_processing",
        action_type="commitments_tracker",
        default_schedule="once",
        parameters={
            "channel": "telegram",
            "chat_id": ""
        }
    ),
    WorkflowTemplate(
        id="client_risk_alert",
        name="🚨 Радар настроения клиента",
        description="Следит за встречами с клиентами (например, папка «Клиенты»): недовольство, упоминание конкурентов, вопросы к цене, охлаждение — и присылает алерт с цитатами. Всё спокойно — молчит.",
        category="meeting_processing",
        action_type="client_risk_alert",
        default_schedule="once",
        parameters={
            "channel": "telegram",
            "chat_id": "",
            "min_level": "medium"
        }
    ),
    WorkflowTemplate(
        id="sales_call_scorecard",
        name="📞 Скоринг звонка продаж",
        description="После звонка из папки «Продажи» — разбор для руководителя: оценка 1-10, BANT (бюджет/ЛПР/потребность/сроки), возражения, следующий шаг, что улучшить менеджеру.",
        category="meeting_processing",
        action_type="sales_call_scorecard",
        default_schedule="once",
        parameters={"channel": "telegram", "chat_id": "", "custom_prompt": ""}
    ),
    WorkflowTemplate(
        id="voice_of_customer",
        name="📣 Голос клиента → маркетингу",
        description="Из клиентских встреч извлекает боли и формулировки клиента ЕГО словами, возражения, запросы фич и фразы для рекламы — и шлёт маркетингу.",
        category="meeting_processing",
        action_type="voice_of_customer",
        default_schedule="once",
        parameters={"channel": "telegram", "chat_id": "", "custom_prompt": ""}
    ),
    WorkflowTemplate(
        id="one_on_one_pulse",
        name="💬 Пульс 1-on-1 для руководителя",
        description="После 1-on-1 — конфиденциальная сводка руководителю: настроение сотрудника, сигналы выгорания/ухода, что драйвит, договорённости, что сделать до следующей встречи.",
        category="meeting_processing",
        action_type="one_on_one_pulse",
        default_schedule="once",
        parameters={"channel": "telegram", "chat_id": "", "custom_prompt": ""}
    ),
    WorkflowTemplate(
        id="call_qa_review",
        name="🎧 Контроль качества звонка (КЦ)",
        description="Проверяет разговор оператора по чек-листу (приветствие, потребность, возражения, следующий шаг, тон) — оценка 1-10 и зоны роста супервизору.",
        category="meeting_processing",
        action_type="call_qa_review",
        default_schedule="once",
        parameters={"channel": "telegram", "chat_id": "", "custom_prompt": ""}
    ),
    WorkflowTemplate(
        id="meeting_document",
        name="📄 Документ по встрече → на редакцию",
        description="После встречи собирает документ (КП / договор-черновик / карточку) по вашему стилю и присылает на редакцию. Позиции сметы, если заданы, считает точно (не модель).",
        category="meeting_processing",
        action_type="meeting_document",
        default_schedule="once",
        parameters={
            "channel": "telegram", "chat_id": "", "as_file": True,
            "doc_kind": "kp", "style_example": "", "custom_prompt": ""
        }
    ),
    WorkflowTemplate(
        id="hr_candidate_report",
        name="🧑‍💼 Отчёт по кандидату → нанимающему",
        description="После интервью формирует отчёт о кандидате (сильные стороны, риски, рекомендация) с учётом ваших заметок — и отправляет нанимающему менеджеру. Заметки можно не заполнять.",
        category="meeting_processing",
        action_type="hr_candidate_report",
        default_schedule="once",
        parameters={
            "to_email": "",
            "channel": "email",
            "recruiter_notes": "",
            "role_description": ""
        }
    ),

    # === Периодические дайджесты ===
    WorkflowTemplate(
        id="daily_meetings_digest",
        name="📅 Ежедневный дайджест встреч",
        description="Каждый день в указанное время отправляет сводку по прошедшим встречам",
        category="daily_digest",
        action_type="daily_meetings_digest",
        default_schedule="recurring",
        default_cron="0 18 * * 1-5",  # Каждый будний день в 18:00
        parameters={
            "channel": "telegram",
            "include_upcoming": True,
            "days_back": 1
        }
    ),
    WorkflowTemplate(
        id="morning_briefing",
        name="☀️ Утренний брифинг",
        description="Утром отправляет план на день: встречи, задачи, дедлайны",
        category="daily_digest",
        action_type="morning_briefing",
        default_schedule="recurring",
        default_cron="0 9 * * 1-5",  # Каждый будний день в 9:00
        parameters={
            "channel": "telegram",
            "include_meetings": True,
            "include_tasks": True,
            "include_deadlines": True
        }
    ),

    WorkflowTemplate(
        id="exec_weekly_brief",
        name="🎯 Брифинг директора за неделю",
        description="Раз в неделю одним сообщением: ключевые решения из встреч, что зависло и требует вашего решения, риски, просроченные задачи.",
        category="weekly_report",
        action_type="exec_weekly_brief",
        default_schedule="recurring",
        default_cron="0 17 * * 5",
        parameters={"channel": "telegram", "chat_id": "", "days_back": 7}
    ),

    # === Еженедельные отчёты ===
    WorkflowTemplate(
        id="weekly_tasks_report",
        name="📊 Еженедельный отчёт по задачам",
        description="Каждую пятницу формирует отчёт по выполненным и просроченным задачам",
        category="weekly_report",
        action_type="weekly_tasks_report",
        default_schedule="recurring",
        default_cron="0 17 * * 5",  # Каждую пятницу в 17:00
        parameters={
            "channel": "email",
            "include_completed": True,
            "include_overdue": True,
            "include_upcoming": True
        }
    ),
    WorkflowTemplate(
        id="weekly_meetings_summary",
        name="📋 Еженедельная сводка встреч",
        description="Сводка всех встреч за неделю с ключевыми решениями",
        category="weekly_report",
        action_type="weekly_meetings_summary",
        default_schedule="recurring",
        default_cron="0 18 * * 5",  # Каждую пятницу в 18:00
        parameters={
            "channel": "notion",
            "include_decisions": True,
            "include_action_items": True
        }
    ),

    # === Синхронизация задач ===
    WorkflowTemplate(
        id="sync_overdue_tasks",
        name="⚠️ Уведомление о просроченных задачах",
        description="Проверяет просроченные задачи и отправляет напоминания",
        category="task_sync",
        action_type="sync_overdue_tasks",
        default_schedule="recurring",
        default_cron="0 10,15 * * 1-5",  # Дважды в день в будни
        parameters={
            "channel": "telegram",
            "escalate_after_days": 3
        }
    ),
    WorkflowTemplate(
        id="deadline_reminder",
        name="⏰ Напоминание о дедлайнах",
        description="За день до дедлайна отправляет напоминание о задаче",
        category="task_sync",
        action_type="deadline_reminder",
        default_schedule="recurring",
        default_cron="0 9 * * *",  # Каждый день в 9:00
        parameters={
            "channel": "telegram",
            "days_before": 1,
            "include_context": True
        }
    ),

    # === Исследование / дайджесты (классические, без встреч) ===
    WorkflowTemplate(
        id="daily_market_digest",
        name="📈 Ежедневный дайджест рынка (маркетинг)",
        description="Каждое утро ищет новости и тренды digital-маркетинга и присылает выжимку: что нового и что полезно нам. Доставка — в ваш Telegram по умолчанию (можно изменить).",
        category="daily_digest",
        action_type="scheduled_research",
        default_schedule="recurring",
        default_cron="0 9 * * *",  # Каждый день в 9:00
        parameters={
            "sources": {
                "search_queries": [
                    "новости digital-маркетинга и рекламы Россия",
                    "тренды интернет-маркетинга сегодня",
                ],
                "urls": [],
                "telegram_channels": [],
            },
            "lens": ("Выдели инсайты рынка, полезные маркетинговому агентству: "
                     "что нового у конкурентов и на рынке, какие форматы/каналы "
                     "растут, что стоит попробовать нам. Только конкретика и что "
                     "делать. Без воды."),
            "model_tier": "standard",
            "max_urls": 6,
            "channel": "telegram",
        },
    ),
    WorkflowTemplate(
        id="weekly_competitors_review",
        name="🔎 Недельный обзор конкурентов и трендов",
        description="Раз в неделю (пн) собирает тренды и новые инструменты маркетинга и присылает обзор с выводами для нас. Доставка — в ваш Telegram по умолчанию.",
        category="weekly_report",
        action_type="scheduled_research",
        default_schedule="recurring",
        default_cron="0 10 * * 1",  # Каждый понедельник в 10:00
        parameters={
            "sources": {
                "search_queries": [
                    "тренды digital-маркетинга неделя обзор",
                    "новые инструменты и сервисы маркетинга",
                ],
                "urls": [],
                "telegram_channels": [],
            },
            "lens": ("Недельный обзор для агентства: ключевые тренды, что изменилось "
                     "у конкурентов, новые инструменты/форматы, конкретные идеи для "
                     "наших клиентов. Структурно, с выводами «что делать»."),
            "model_tier": "premium",
            "max_urls": 8,
            "channel": "telegram",
        },
    ),
    WorkflowTemplate(
        id="slack_daily_analytics",
        name="💬 Ежедневная аналитика Slack (проблемы/решения)",
        description="Каждый будний вечер разбирает переписку команды за день: проблемы, решения, на что заострить внимание. Укажите ID канала Slack (нужен бот-токен + бот в канале). Доставка — в ваш Telegram.",
        category="daily_digest",
        action_type="chat_digest",
        default_schedule="recurring",
        default_cron="0 18 * * 1-5",  # Будни в 18:00
        parameters={
            "source": "slack",
            "channel": "",              # ← укажите ID канала Slack
            "window_days": 1,
            "mode": "daily_analytics",
            "model_tier": "standard",
        },
    ),
    WorkflowTemplate(
        id="slack_weekly_retro",
        name="🗓 Недельная ретроспектива Slack (по понедельникам)",
        description="Каждый понедельник разбирает переписку команды за неделю: о чём говорили, ключевые темы, решения, что важного. Укажите ID канала Slack (нужен бот-токен + бот в канале).",
        category="weekly_report",
        action_type="chat_digest",
        default_schedule="recurring",
        default_cron="0 10 * * 1",  # Каждый понедельник в 10:00
        parameters={
            "source": "slack",
            "channel": "",              # ← укажите ID канала Slack
            "window_days": 7,
            "mode": "retrospective",
            "model_tier": "standard",
        },
    ),
]


class MeetingWorkflowExecutor:
    """Исполнитель автоматизаций для встреч"""

    def __init__(self):
        self._ensure_meetflow_path()
        # Track LLM usage for this execution
        self._llm_tokens_used = 0
        self._llm_cost_usd = 0.0
        # issue #110 пакет 2: workflow_type текущего execute_workflow() — нужен
        # как request_type в глобальном трекере, чтобы аналитика разделяла
        # «summary_telegram» от «tasks_yougile» внутри одной встречи.
        self._current_workflow_type: Optional[str] = None
        # issue #110 wave 2: content-hash кэш артефактов (паттерн fingerprint
        # из Understand-Anything). Ключ — (sha256(transcript)[:16],
        # artifact_type, model). Ловит повторную обработку: webhook-ретраи,
        # дубль-воркфлоу на ту же встречу, ре-раны. TTL не нужен — встреча
        # неизменна; cache живёт в рамках процесса (executor — singleton).
        self._artifact_cache: Dict[str, Any] = {}
        self._cache_hits = 0
        self._cache_misses = 0

    def _ensure_meetflow_path(self):
        """Добавляет путь к MeetFlow в sys.path"""
        if MEETFLOW_PATH not in sys.path:
            sys.path.insert(0, MEETFLOW_PATH)

    def _track_llm_usage(self, model: str, input_tokens: int, output_tokens: int):
        """issue #110 пакет 2: запись о LLM-вызове идёт в ЕДИНЫЙ tracker
        (backend.core.llm.usage_tracker), оттуда и читается стоимость.
        Локальные self._llm_tokens_used / self._llm_cost_usd сохранены
        для обратной совместимости — workflow result.{tokens_used, cost_usd}.

        meeting_id / surface / document_id подмешиваются автоматически
        из активного cost_scope() (см. execute_workflow ниже), tenant_id —
        из observability.tenant_context. provider="openai" фиксирован, т.к.
        все 4 workflow-action в этом файле зовут openai-клиента напрямую."""
        try:
            from backend.core.llm.usage_tracker import track_usage
            record = track_usage(
                provider="openai",
                model=model,
                input_tokens=int(input_tokens),
                output_tokens=int(output_tokens),
                agent_mode="automation",
                request_type=self._current_workflow_type,
            )
            cost = float(record.total_cost or 0.0)
        except Exception as e:
            # tracker не должен ломать workflow. fall back на старую оценку,
            # но БЕЗ дубля pricing-словаря — используем тот же MODEL_PRICING.
            logger.warning(f"_track_llm_usage: global tracker failed, "
                           f"fallback to local pricing: {e}")
            try:
                from backend.core.llm.usage_tracker import MODEL_PRICING
                p = MODEL_PRICING.get(model) or MODEL_PRICING.get("gpt-4o-mini", {})
                cost = round(
                    (int(input_tokens) / 1_000_000) * float(p.get("input", 0.15))
                    + (int(output_tokens) / 1_000_000) * float(p.get("output", 0.60)),
                    6,
                )
            except Exception:
                cost = 0.0
        self._llm_tokens_used += int(input_tokens) + int(output_tokens)
        self._llm_cost_usd += cost

    def get_llm_usage(self) -> Dict[str, Any]:
        """Get accumulated LLM usage stats"""
        return {
            "tokens_used": self._llm_tokens_used,
            "cost_usd": round(self._llm_cost_usd, 6),
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
        }

    @staticmethod
    def _resolve_model(params: Dict[str, Any]) -> str:
        """Единый выбор модели: явный params['model'] перекрывает tier.
        issue #110 wave 2 — устранён дубль логики из 4 функций."""
        model_tier = params.get("model_tier", "standard")
        return params.get("model") or (
            "gpt-4o" if model_tier == "premium" else "gpt-4o-mini")

    @staticmethod
    def _content_hash(text: str) -> str:
        """SHA256[:16] транскрипта — fingerprint для кэша артефактов."""
        import hashlib
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    async def _llm_generate(
        self,
        *,
        artifact_type: str,
        system_prompt: str,
        user_prompt: str,
        cache_key_content: str,
        params: Dict[str, Any],
        max_tokens: int = 2000,
        temperature: float = 0.3,
    ) -> Optional[str]:
        """Единая точка LLM-генерации для workflow-артефактов (issue #110
        wave 2). Заменяет 4× дублированный openai-вызов.

        Возможности:
          - content-hash кэш: тот же транскрипт + artifact_type + model →
            из кэша, без LLM-вызова (ловит ретраи/дубли/ре-раны)
          - единый выбор модели (_resolve_model)
          - единый usage-трекинг (_track_llm_usage)
          - openai-клиент создаётся один раз на вызов

        Возвращает текст ответа LLM или None при ошибке (caller делает
        свой fallback). Не бросает — workflow устойчив к LLM-сбоям.
        """
        import os

        model = self._resolve_model(params)
        cache_key = f"{self._content_hash(cache_key_content)}:{artifact_type}:{model}"

        # Cache hit — артефакт уже сгенерирован для этого транскрипта
        if cache_key in self._artifact_cache:
            self._cache_hits += 1
            logger.info("[workflow] cache HIT: %s (model=%s)", artifact_type, model)
            return self._artifact_cache[cache_key]

        self._cache_misses += 1

        # Основной путь — внутренний LLM-роутер (Gemini по умолчанию,
        # фолбэки провайдеров, квоты, prompt-cache, учёт расходов через
        # активный cost_scope). Workflow больше не требует OPENAI_API_KEY.
        # Исключение: явный params["model"] при живом OPENAI_API_KEY —
        # прежний прямой вызов OpenAI (пользовательский override модели).
        use_direct_openai = bool(params.get("model")) and bool(os.getenv("OPENAI_API_KEY"))
        if not use_direct_openai:
            try:
                from backend.core.llm.router import ModelTier, get_llm_router
                tier = (ModelTier.PREMIUM if params.get("model_tier") == "premium"
                        else ModelTier.STANDARD)
                content = await get_llm_router().generate(
                    prompt=user_prompt,
                    system_prompt=system_prompt,
                    model_tier=tier,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                if content:
                    self._artifact_cache[cache_key] = content
                    return content
            except Exception as e:
                logger.warning("[workflow] router LLM failed (%s), "
                               "falling back to direct OpenAI: %s", artifact_type, e)

        if not os.getenv("OPENAI_API_KEY"):
            logger.error("[workflow] no LLM available for %s "
                         "(router failed, OPENAI_API_KEY not set)", artifact_type)
            return None
        try:
            import openai
            # Enterprise-переносимость: fallback-клиент тоже уважает
            # OPENAI_BASE_URL → локальный vLLM/прокси обслуживает и его
            # (router-first путь уже local-совместим; это подстраховка)
            _ow_kwargs = {"api_key": os.getenv("OPENAI_API_KEY")}
            _ow_base = os.getenv("OPENAI_BASE_URL", "")
            if _ow_base:
                _ow_kwargs["base_url"] = _ow_base
            client = openai.AsyncOpenAI(**_ow_kwargs)
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=max_tokens,
                temperature=temperature,
            )
            if response.usage:
                self._track_llm_usage(
                    model, response.usage.prompt_tokens,
                    response.usage.completion_tokens)
            content = response.choices[0].message.content
            if content:
                self._artifact_cache[cache_key] = content
            return content
        except Exception as e:
            logger.error("[workflow] LLM error (%s): %s", artifact_type, e)
            return None

    async def execute_workflow(
        self,
        workflow_type: str,
        parameters: Dict[str, Any],
        user_id: str
    ) -> Dict[str, Any]:
        """
        Выполняет workflow по типу.

        Args:
            workflow_type: Тип workflow из WORKFLOW_TEMPLATES
            parameters: Параметры выполнения
            user_id: ID пользователя

        Returns:
            Результат выполнения
        """
        logger.info(f"🚀 Executing workflow: {workflow_type}")

        # Reset LLM usage tracking for this execution
        self._llm_tokens_used = 0
        self._llm_cost_usd = 0.0
        self._current_workflow_type = workflow_type

        # issue #110 пакет 2: атрибуция LLM-стоимости «по сущности».
        # Все вложенные track_usage() (4 openai-вызова в этом классе)
        # автоматически попадут в /api/v1/usage/by-meeting/{meeting_id}
        # и в /usage/attribution?group_by=surface — без явного аргумента.
        from backend.core.llm.usage_tracker import cost_scope
        scope_kwargs: Dict[str, Any] = {
            "surface": _WORKFLOW_SURFACE.get(workflow_type, "automation"),
            "agent_mode": "automation",
            "user_id": user_id,
        }
        # meeting_id есть у meeting_*-workflows; digest/weekly/automation —
        # без него, surface справится с группировкой.
        meeting_id = parameters.get("meeting_id") if parameters else None

        # Ручной запуск meeting-workflow (кнопка «Выполнить» в UI) приходит БЕЗ
        # события → meeting_id нет, и всё падало «meeting_id required». Фолбэк:
        # последняя встреча пользователя за 30 дней — ручной тест работает на
        # реальных данных. Событийный путь как прокидывал id, так и прокидывает.
        if not meeting_id and _WORKFLOW_SURFACE.get(workflow_type) == "ingest":
            try:
                from automation_tools_async import list_meetings_by_period
                _meetings = json.loads(await list_meetings_by_period(
                    days_back=30, user_id=user_id)).get("meetings", []) or []
                if _meetings:
                    meeting_id = str(_meetings[0].get("id") or "").strip() or None
                if meeting_id:
                    parameters = {**(parameters or {}), "meeting_id": meeting_id}
                    logger.info(f"   ↪ meeting_id не передан (ручной запуск) — "
                                f"беру последнюю встречу {meeting_id[:8]}…")
            except Exception as _e:
                logger.debug(f"   latest-meeting fallback failed: {_e}")

        if meeting_id:
            scope_kwargs["meeting_id"] = str(meeting_id)

        try:
            # Маппинг типов на методы
            executors = {
                "meeting_summary_telegram": self._execute_meeting_summary_telegram,
                "meeting_tasks_yougile": self._execute_meeting_tasks_yougile,
                "meeting_report_email": self._execute_meeting_report_email,
                "meeting_notes_notion": self._execute_meeting_notes_notion,
                "client_followup": self._execute_client_followup,
                "hr_candidate_report": self._execute_hr_candidate_report,
                "meeting_document": self._execute_meeting_document,
                "commitments_tracker": self._execute_commitments_tracker,
                "client_risk_alert": self._execute_client_risk_alert,
                "sales_call_scorecard": partial(self._execute_meeting_insight, insight_key="sales_call_scorecard"),
                "voice_of_customer": partial(self._execute_meeting_insight, insight_key="voice_of_customer"),
                "one_on_one_pulse": partial(self._execute_meeting_insight, insight_key="one_on_one_pulse"),
                "call_qa_review": partial(self._execute_meeting_insight, insight_key="call_qa_review"),
                "exec_weekly_brief": self._execute_exec_weekly_brief,
                "daily_meetings_digest": self._execute_daily_digest,
                "morning_briefing": self._execute_morning_briefing,
                "weekly_tasks_report": self._execute_weekly_tasks_report,
                "weekly_meetings_summary": self._execute_weekly_meetings_summary,
                "sync_overdue_tasks": self._execute_sync_overdue_tasks,
                "deadline_reminder": self._execute_deadline_reminder,
            }

            executor = executors.get(workflow_type)
            if not executor:
                return {"success": False, "message": f"Unknown workflow type: {workflow_type}"}

            async with cost_scope(**scope_kwargs):
                result = await executor(parameters, user_id)

            # Add LLM usage to result (обратная совместимость: API уже отдавал
            # эти поля; рассчитываются теперь из глобального tracker'а)
            llm_usage = self.get_llm_usage()
            result["llm_usage"] = llm_usage

            return result

        except Exception as e:
            logger.error(f"❌ Workflow {workflow_type} failed: {e}")
            return {"success": False, "message": str(e), "llm_usage": self.get_llm_usage()}
        finally:
            self._current_workflow_type = None

    # ==================== Meeting Processing ====================

    async def _execute_meeting_summary_telegram(
        self,
        params: Dict[str, Any],
        user_id: str
    ) -> Dict[str, Any]:
        """Извлекает саммари встречи и отправляет в Telegram (поддержка множественных получателей)"""
        from automation_tools_async import (
            get_meeting_details,
            send_telegram_message,
            set_user_context,
        )

        set_user_context(user_id=user_id)

        meeting_id = params.get("meeting_id")
        if not meeting_id:
            return {"success": False, "message": "meeting_id required"}

        # Получаем данные встречи
        meeting_json = await get_meeting_details(meeting_id)
        meeting_data = json.loads(meeting_json)

        if "error" in meeting_data:
            return {"success": False, "message": meeting_data["error"]}

        meeting = meeting_data.get("meeting", {})

        # Формируем саммари
        summary = await self._generate_meeting_summary(meeting, params)

        # Канал не Telegram (почта/Slack) → единая доставка
        if str(params.get("channel") or "telegram").lower() in ("email", "slack"):
            sent = await self._send_to_channel(
                user_id=user_id, params=params, text=summary,
                subject=f"Саммари встречи: {meeting.get('title', 'Встреча')}")
            return {"success": sent["success"], "message": sent["message"],
                    "data": {"summary_length": len(summary)}}

        # Получаем список получателей (поддержка множественных)
        chat_ids = params.get("chat_ids", [])
        if not chat_ids:
            # Fallback на одиночный chat_id для обратной совместимости
            single_id = params.get("chat_id", "")
            if single_id:
                chat_ids = [single_id]

        if not chat_ids:
            return {"success": False, "message": "No recipients specified (chat_id or chat_ids required)"}

        # Отправляем всем получателям
        results = []
        success_count = 0
        for chat_id in chat_ids:
            try:
                result = await send_telegram_message(summary, chat_id)
                result_data = json.loads(result)
                results.append({"chat_id": chat_id, "success": result_data.get("success", False)})
                if result_data.get("success"):
                    success_count += 1
            except Exception as e:
                results.append({"chat_id": chat_id, "success": False, "error": str(e)})

        return {
            "success": success_count > 0,
            "message": f"Meeting summary sent to {success_count}/{len(chat_ids)} recipients",
            "data": {
                "summary_length": len(summary),
                "recipients": len(chat_ids),
                "success_count": success_count,
                "results": results
            }
        }

    async def _execute_meeting_tasks_yougile(
        self,
        params: Dict[str, Any],
        user_id: str
    ) -> Dict[str, Any]:
        """Извлекает задачи из встречи и создаёт в YouGile"""
        from automation_tools_async import (
            create_task,
            get_meeting_details,
            get_meeting_tasks,
            set_user_context,
        )

        set_user_context(user_id=user_id)

        meeting_id = params.get("meeting_id")
        if not meeting_id:
            return {"success": False, "message": "meeting_id required"}

        # Получаем задачи из встречи
        tasks_json = await get_meeting_tasks(meeting_id)
        tasks_data = json.loads(tasks_json)
        tasks = tasks_data.get("tasks", [])

        if not tasks:
            # Если задач нет, пробуем извлечь из транскрипции
            meeting_json = await get_meeting_details(meeting_id)
            meeting_data = json.loads(meeting_json)
            meeting = meeting_data.get("meeting", {})

            tasks = await self._extract_tasks_from_transcript(
                meeting.get("transcription_text", ""),
                params
            )

        # Куда создавать: yougile (по умолчанию) / trello / jira — один шаблон
        # «Задачи из встречи → Трекер», трекер выбирается в настройке.
        tracker = str(params.get("tracker") or "yougile").lower()
        created_count = 0
        errors: List[str] = []

        registry = None
        if tracker in ("trello", "jira", "linear"):
            try:
                from backend.integrations.registry import IntegrationRegistry
                registry = IntegrationRegistry()
                await registry.load_for_user(user_id)
            except Exception as e:
                return {"success": False,
                        "message": f"{tracker.title()} не настроен: подключите "
                                   f"интеграцию в кабинете ({e})"}

        for task in tasks[:10]:  # Лимит 10 задач
            title = task.get("title") or task.get("name", "")
            description = task.get("description", "")
            assignee = task.get("assignee", "")
            if not title:
                continue
            try:
                if tracker == "trello":
                    raw = await registry.execute_tool(
                        "trello_create_card",
                        list_id=str(params.get("trello_list_id") or ""),
                        name=title, description=description, due="")
                elif tracker == "jira":
                    raw = await registry.execute_tool(
                        "jira_create_issue",
                        project=str(params.get("jira_project") or ""),
                        summary=title, description=description,
                        issue_type="Task", priority="")
                elif tracker == "linear":
                    raw = await registry.execute_tool(
                        "linear_create_issue",
                        title=title, description=description,
                        team_id=str(params.get("linear_team_id") or ""))
                else:  # yougile
                    raw = await create_task(
                        params.get("column_id", ""), title, description, assignee)
                result_data = json.loads(raw)
                if result_data.get("success", not result_data.get("error")):
                    created_count += 1
                elif result_data.get("error"):
                    errors.append(str(result_data["error"])[:120])
            except Exception as e:
                errors.append(str(e)[:120])

        msg = f"Создано задач в {tracker.title()}: {created_count} из {len(tasks)}"
        if not created_count and errors:
            msg += f". Ошибка: {errors[0]}"
        return {
            "success": created_count > 0,
            "message": msg,
            "data": {"created_count": created_count, "total_found": len(tasks),
                     "tracker": tracker, "errors": errors[:3]}
        }

    async def _execute_meeting_report_email(
        self,
        params: Dict[str, Any],
        user_id: str
    ) -> Dict[str, Any]:
        """Формирует отчёт по встрече и отправляет на email (поддержка множественных получателей)"""
        from automation_tools_async import get_meeting_details, send_email, set_user_context

        set_user_context(user_id=user_id)

        meeting_id = params.get("meeting_id")
        if not meeting_id:
            return {"success": False, "message": "meeting_id required"}

        # Получаем данные встречи
        meeting_json = await get_meeting_details(meeting_id)
        meeting_data = json.loads(meeting_json)

        if "error" in meeting_data:
            return {"success": False, "message": meeting_data["error"]}

        meeting = meeting_data.get("meeting", {})

        # Формируем отчёт
        report = await self._generate_meeting_report(meeting, params)

        # Получаем список получателей (поддержка множественных)
        to_emails = params.get("to_emails", [])
        if not to_emails:
            # Fallback на одиночный email для обратной совместимости
            single_email = params.get("to_email", "")
            if single_email:
                to_emails = [single_email]

        if not to_emails:
            return {"success": False, "message": "No recipients specified (to_email or to_emails required)"}

        subject = f"📋 Отчёт по встрече: {meeting.get('title', 'Встреча')}"

        # Отправляем всем получателям
        results = []
        success_count = 0
        for email in to_emails:
            try:
                result = await send_email(email, subject, report)
                result_data = json.loads(result)
                results.append({"email": email, "success": result_data.get("success", False)})
                if result_data.get("success"):
                    success_count += 1
            except Exception as e:
                results.append({"email": email, "success": False, "error": str(e)})

        return {
            "success": success_count > 0,
            "message": f"Meeting report sent to {success_count}/{len(to_emails)} recipients",
            "data": {
                "report_length": len(report),
                "recipients": len(to_emails),
                "success_count": success_count,
                "results": results
            }
        }

    async def _execute_meeting_notes_notion(
        self,
        params: Dict[str, Any],
        user_id: str
    ) -> Dict[str, Any]:
        """Создаёт заметки встречи в Notion"""
        from automation_tools_async import create_notion_page, get_meeting_details, set_user_context

        set_user_context(user_id=user_id)

        meeting_id = params.get("meeting_id")
        if not meeting_id:
            return {"success": False, "message": "meeting_id required"}

        # Получаем данные встречи
        meeting_json = await get_meeting_details(meeting_id)
        meeting_data = json.loads(meeting_json)

        if "error" in meeting_data:
            return {"success": False, "message": meeting_data["error"]}

        meeting = meeting_data.get("meeting", {})

        # Формируем контент для Notion
        title = f"📝 {meeting.get('title', 'Встреча')} - {meeting.get('created_at', '')[:10]}"
        content = await self._generate_notion_content(meeting, params)

        parent_page_id = params.get("parent_page_id", "")

        result = await create_notion_page(title, content, parent_page_id)
        result_data = json.loads(result)

        return {
            "success": "error" not in result_data,
            "message": "Meeting notes created in Notion",
            "data": result_data
        }

    # ==================== Бизнес-сценарии по папкам ====================

    async def _load_meeting_text(self, meeting_id: str) -> Dict[str, Any]:
        """Встреча + текст для LLM (транскрипт, фолбэк — саммари)."""
        from automation_tools_async import get_meeting_details
        meeting = json.loads(await get_meeting_details(meeting_id)).get("meeting", {})
        text = meeting.get("transcription_text") or meeting.get("summary") or ""
        return {"meeting": meeting, "text": text}

    async def _execute_client_followup(
        self,
        params: Dict[str, Any],
        user_id: str
    ) -> Dict[str, Any]:
        """Клиентский фоллоу-ап: встреча прошла (например, в папке
        «Клиенты») → клиенту уходит аккуратное письмо-саммари. Тон/формат
        задаётся custom_prompt («возьми саммари и опиши вот так»)."""
        from automation_tools_async import set_user_context
        set_user_context(user_id=user_id)

        meeting_id = params.get("meeting_id")
        if not meeting_id:
            return {"success": False, "message": "meeting_id required"}
        loaded = await self._load_meeting_text(meeting_id)
        meeting, text = loaded["meeting"], loaded["text"]
        if not text:
            return {"success": False,
                    "message": "У встречи нет ни транскрипта, ни саммари — письмо не из чего собрать"}

        title = meeting.get("title", "Встреча")
        custom = (params.get("custom_prompt") or "").strip()
        base_prompt = (
            "Составь письмо клиенту по итогам встречи. Требования:\n"
            "- вежливый деловой тон, обращение к клиенту, от лица нашей команды;\n"
            "- структура: короткое вступление, что обсудили, договорённости, "
            "следующие шаги с ответственными/сроками (если звучали);\n"
            "- НИЧЕГО не выдумывать — только то, что было на встрече;\n"
            "- без внутренней кухни (цены закупки, внутренние конфликты, "
            "обсуждение других клиентов — опустить);\n"
            "- на русском, без markdown-звёздочек, готовое к отправке письмо.")
        if custom:
            base_prompt += f"\n\nПожелания владельца к формату/тону (главнее правил выше): {custom}"

        content = await self._llm_generate(
            artifact_type="client_followup",
            system_prompt="Ты — аккаунт-менеджер, пишешь клиенту итоги встречи. Пишешь точно, тепло и по делу.",
            user_prompt=f"{base_prompt}\n\n--- Встреча: {title} ---\n{text[:12000]}",
            cache_key_content=f"{text[:12000]}\n::{custom}",
            params=params,
            max_tokens=1800,
            temperature=0.4,
        )
        if content is None:
            # НЕ шлём клиенту сырой транскрипт — только честный отказ
            return {"success": False,
                    "message": "LLM недоступен — письмо клиенту не сформировано (сырой транскрипт не отправляем)"}

        send_params = dict(params)
        send_params.setdefault("channel", "email")
        sent = await self._send_to_channel(
            user_id=user_id, params=send_params, text=content,
            subject=f"Итоги встречи: {title}")
        return {"success": sent["success"], "message": sent["message"],
                "data": {"meeting": title, "letter_length": len(content)}}

    async def _execute_meeting_document(
        self,
        params: Dict[str, Any],
        user_id: str
    ) -> Dict[str, Any]:
        """Собрать ДОКУМЕНТ по встрече (КП/договор/карточка) и отправить на
        редакцию. Гибкий режим: система формирует содержимое по встрече в
        стиле примера. Деньги — если заданы позиции — считает КОД."""
        from automation_tools_async import set_user_context
        set_user_context(user_id=user_id)

        meeting_id = params.get("meeting_id")
        if not meeting_id:
            return {"success": False, "message": "meeting_id required"}
        loaded = await self._load_meeting_text(meeting_id)
        meeting, text = loaded["meeting"], loaded["text"]
        if not text:
            return {"success": False, "message": "У встречи нет транскрипта/саммари"}

        from backend.core.documents import meeting_doc_engine as _E
        from backend.core.documents import meeting_doc_service as _S
        from backend.core.llm.router import get_llm_router

        doc_kind = str(params.get("doc_kind") or "kp")
        markdown = await _S.compose_document(
            get_llm_router(), doc_kind=doc_kind, meeting_text=text,
            style_example=str(params.get("style_example") or ""),
            custom_prompt=str(params.get("custom_prompt") or ""))
        if not markdown:
            return {"success": False, "message": "LLM недоступен — документ не собран"}

        # Позиции сметы → расчёт кодом, дописываем к документу
        items = params.get("line_items") or []
        if items:
            m = _E.compute_line_items(items, vat_rate=float(params.get("vat_rate", 20) or 0))
            markdown += (f"\n\n## Стоимость\nИтого без НДС: {m['subtotal']:,.2f} ₽\n"
                         f"НДС {m['vat_rate']:g}%: {m['vat']:,.2f} ₽\n"
                         f"Итого к оплате: {m['total']:,.2f} ₽\n"
                         f"Сумма прописью: {m['total_in_words']}")

        title = {"kp": "Коммерческое предложение", "contract": "Договор (черновик)",
                 "card": "Карточка по встрече"}.get(doc_kind, "Документ")
        mtitle = meeting.get("title", "встреча")

        # as_file + канал telegram → отправляем docx ФАЙЛОМ (sendDocument),
        # а не текстом. Для «на редакцию» удобнее готовый Word.
        if params.get("as_file") and str(params.get("channel") or "telegram").lower() == "telegram":
            try:
                from backend.core.analysis.export import markdown_to_docx_bytes
                from automation_tools_async import send_telegram_document
                docx = markdown_to_docx_bytes(markdown, title=title)
                raw = await send_telegram_document(
                    docx, filename=f"{title} — {mtitle}.docx"[:80],
                    chat_id=str(params.get("chat_id") or ""),
                    caption=f"📄 {title} — «{mtitle}» (на редакцию)", user_id=user_id)
                res = json.loads(raw)
                if res.get("success"):
                    return {"success": True, "message": res.get("message", "Документ отправлен файлом"),
                            "data": {"doc_kind": doc_kind, "as_file": True}}
                logger.warning("send_telegram_document failed, fallback to text: %s", res.get("error"))
            except Exception as e:
                logger.warning("docx-file send failed, fallback to text: %s", e)

        header = f"📄 **{title}** — «{mtitle}» (на редакцию):\n\n"
        sent = await self._send_to_channel(
            user_id=user_id, params=params, text=header + markdown,
            subject=f"{title}: {mtitle}")
        return {"success": sent["success"], "message": sent["message"],
                "data": {"doc_kind": doc_kind, "length": len(markdown)}}

    async def _execute_hr_candidate_report(
        self,
        params: Dict[str, Any],
        user_id: str
    ) -> Dict[str, Any]:
        """HR: после интервью — отчёт о кандидате нанимающему менеджеру.
        Учитывает заметки рекрутёра (recruiter_notes), если заполнены;
        пусто — отчёт строится только по содержанию интервью."""
        from automation_tools_async import set_user_context
        set_user_context(user_id=user_id)

        meeting_id = params.get("meeting_id")
        if not meeting_id:
            return {"success": False, "message": "meeting_id required"}
        loaded = await self._load_meeting_text(meeting_id)
        meeting, text = loaded["meeting"], loaded["text"]
        if not text:
            return {"success": False,
                    "message": "У интервью нет ни транскрипта, ни саммари"}

        title = meeting.get("title", "Интервью")
        notes = (params.get("recruiter_notes") or "").strip()
        role = (params.get("role_description") or "").strip()
        base_prompt = (
            "Составь отчёт о кандидате для нанимающего менеджера по итогам интервью:\n"
            "1. Кратко о кандидате (опыт, текущая роль — как прозвучало)\n"
            "2. Сильные стороны (с примерами из ответов)\n"
            "3. Зоны риска и что перепроверить\n"
            + ("4. Соответствие роли (см. описание ниже)\n" if role else "")
            + "5. Рекомендация: следующий этап / оффер / отказ — с обоснованием\n"
            "Только факты из интервью и заметок, без домыслов. На русском, "
            "без markdown-звёздочек.")
        if role:
            base_prompt += f"\n\nОписание роли: {role}"
        if notes:
            base_prompt += ("\n\nЗаметки рекрутёра (обязательно учесть, "
                            f"они приоритетнее общих впечатлений): {notes}")

        content = await self._llm_generate(
            artifact_type="hr_candidate_report",
            system_prompt="Ты — опытный рекрутёр-аналитик. Пишешь структурированные и честные отчёты о кандидатах.",
            user_prompt=f"{base_prompt}\n\n--- Интервью: {title} ---\n{text[:12000]}",
            cache_key_content=f"{text[:12000]}\n::{notes}::{role}",
            params=params,
            max_tokens=2000,
            temperature=0.3,
        )
        if content is None:
            return {"success": False,
                    "message": "LLM недоступен — отчёт о кандидате не сформирован"}

        send_params = dict(params)
        send_params.setdefault("channel", "email")
        sent = await self._send_to_channel(
            user_id=user_id, params=send_params, text=content,
            subject=f"Отчёт по кандидату: {title}")
        return {"success": sent["success"], "message": sent["message"],
                "data": {"meeting": title, "with_notes": bool(notes)}}

    # Ролевые разборы встречи: один исполнитель + пресеты промптов.
    # Каждый пресет = «через чьи глаза» смотрим на встречу и кому слать.
    _INSIGHT_PRESETS: Dict[str, Dict[str, str]] = {
        "sales_call_scorecard": {
            "system": "Ты — руководитель отдела продаж, разбираешь звонки менеджеров. Оцениваешь честно и по делу.",
            "prompt": (
                "Разбери этот звонок продаж:\n"
                "1. Оценка звонка 1-10 с обоснованием\n"
                "2. BANT: Бюджет / ЛПР / Потребность / Сроки — что выяснено, что нет\n"
                "3. Возражения клиента и как менеджер их отработал\n"
                "4. Договорённость о следующем шаге (есть/нет, какая)\n"
                "5. Что менеджеру сделать лучше в следующий раз (2-3 пункта)\n"
                "Только факты из разговора. На русском, без markdown-звёздочек."),
            "subject": "📞 Скоринг звонка",
        },
        "voice_of_customer": {
            "system": "Ты — продуктовый маркетолог, собираешь голос клиента из встреч.",
            "prompt": (
                "Извлеки из встречи «голос клиента» для маркетинга и продукта:\n"
                "1. Боли и задачи клиента — ЕГО СЛОВАМИ (цитаты)\n"
                "2. Возражения и сомнения\n"
                "3. Что понравилось / за что хвалили\n"
                "4. Запросы фич и пожелания\n"
                "5. Формулировки, которые стоит взять в рекламу/лендинг\n"
                "Только то, что реально прозвучало. На русском, без markdown-звёздочек."),
            "subject": "📣 Голос клиента",
        },
        "one_on_one_pulse": {
            "system": "Ты — HR-партнёр, помогаешь руководителю после 1-on-1. Пишешь только для руководителя, конфиденциально.",
            "prompt": (
                "Разбери 1-on-1 руководителя с сотрудником:\n"
                "1. Общее настроение и вовлечённость сотрудника\n"
                "2. Сигналы риска: выгорание, демотивация, мысли об уходе, конфликты\n"
                "3. Что сотрудника драйвит / что просил (рост, задачи, зарплата)\n"
                "4. Договорённости сторон (кто что пообещал)\n"
                "5. Что руководителю сделать до следующей встречи\n"
                "Бережно к формулировкам, только факты. На русском, без markdown-звёздочек."),
            "subject": "💬 Пульс 1-on-1",
        },
        "call_qa_review": {
            "system": "Ты — супервизор контакт-центра, проверяешь качество разговора оператора по чек-листу.",
            "prompt": (
                "Проверь разговор оператора по чек-листу качества:\n"
                "1. Приветствие и установление контакта (да/нет/частично)\n"
                "2. Выявление потребности (какие вопросы задал)\n"
                "3. Решение вопроса клиента / работа с возражениями\n"
                "4. Следующий шаг и завершение разговора\n"
                "5. Тон и вежливость\n"
                "Итог: оценка 1-10, главная зона роста оператора (1-2 пункта), "
                "цитата лучшего и худшего момента. На русском, без markdown-звёздочек."),
            "subject": "🎧 Контроль качества звонка",
        },
    }

    async def _execute_meeting_insight(
        self,
        params: Dict[str, Any],
        user_id: str,
        insight_key: str = "",
    ) -> Dict[str, Any]:
        """Универсальный ролевой разбор встречи (продажи/маркетинг/HR/КЦ):
        встреча → LLM с ролевым промптом пресета → доставка."""
        from automation_tools_async import set_user_context
        set_user_context(user_id=user_id)

        preset = self._INSIGHT_PRESETS.get(insight_key)
        if not preset:
            return {"success": False, "message": f"Unknown insight: {insight_key}"}
        meeting_id = params.get("meeting_id")
        if not meeting_id:
            return {"success": False, "message": "meeting_id required"}
        loaded = await self._load_meeting_text(meeting_id)
        meeting, text = loaded["meeting"], loaded["text"]
        if not text:
            return {"success": False, "message": "У встречи нет транскрипта/саммари"}

        custom = (params.get("custom_prompt") or "").strip()
        prompt = preset["prompt"]
        if custom:
            prompt += f"\n\nДополнительные пожелания владельца: {custom}"

        title = meeting.get("title", "Встреча")
        content = await self._llm_generate(
            artifact_type=insight_key,
            system_prompt=preset["system"],
            user_prompt=f"{prompt}\n\n--- Встреча: {title} ---\n{text[:12000]}",
            cache_key_content=f"{text[:12000]}\n::{custom}",
            params=params,
            max_tokens=1800,
            temperature=0.3,
        )
        if content is None:
            return {"success": False,
                    "message": "LLM недоступен — разбор не сформирован"}

        sent = await self._send_to_channel(
            user_id=user_id, params=params, text=content,
            subject=f"{preset['subject']}: {title}")
        return {"success": sent["success"], "message": sent["message"],
                "data": {"meeting": title, "insight": insight_key}}

    async def _execute_exec_weekly_brief(
        self,
        params: Dict[str, Any],
        user_id: str
    ) -> Dict[str, Any]:
        """Брифинг директора за неделю: решения из встреч + зависшие/
        просроченные задачи + риски — одним сообщением."""
        from automation_tools_async import list_meetings_by_period, set_user_context
        set_user_context(user_id=user_id)

        meetings = json.loads(await list_meetings_by_period(
            days_back=int(params.get("days_back", 7)), user_id=user_id)
        ).get("meetings", []) or []
        tasks = []
        try:
            tasks = await self._fetch_user_tasks(user_id)
        except Exception as e:
            logger.debug("exec brief tasks skipped: %s", e)
        today = date.today()
        overdue = [t for t in tasks
                   if (self._task_due(t) or today) < today and not self._task_is_closed(t)]
        done_week = [t for t in tasks if self._task_is_closed(t)]

        summaries = "\n\n".join(
            f"— {m.get('title', 'Встреча')}: {(m.get('summary') or '')[:700]}"
            for m in meetings[:25] if m.get("summary"))

        if not summaries and not tasks:
            report = ("🎯 **Брифинг за неделю**\n\nЗа неделю не было встреч с "
                      "саммари и активных задач — докладывать не о чем.")
        else:
            synthesis = None
            if summaries:
                synthesis = await self._llm_generate(
                    artifact_type="exec_brief",
                    system_prompt="Ты — начальник штаба, готовишь еженедельный брифинг для директора. Коротко, только важное.",
                    user_prompt=(
                        "По саммари встреч за неделю подготовь брифинг директору:\n"
                        "1. Ключевые решения недели (3-7 пунктов)\n"
                        "2. Что зависло / требует решения директора\n"
                        "3. Риски и тревожные сигналы\n"
                        "Максимум 1800 знаков, без воды, без markdown-звёздочек.\n\n"
                        f"--- Саммари встреч ---\n{summaries[:14000]}"),
                    cache_key_content=summaries[:14000],
                    params=params,
                    max_tokens=1500,
                    temperature=0.3,
                )
            report = "🎯 **Брифинг за неделю**\n\n"
            report += (f"📊 Встреч: {len(meetings)} · задач закрыто: {len(done_week)}"
                       f" · просрочено: {len(overdue)}\n\n")
            if synthesis:
                report += synthesis + "\n"
            elif summaries:
                report += "⚠️ LLM недоступен — синтез решений не выполнен.\n"
            if overdue:
                report += f"\n⚠️ **Просроченные задачи ({len(overdue)}):**\n"
                report += "\n".join(self._task_line(t, f"просрочена на {(today - (self._task_due(t) or today)).days} дн.")
                                    for t in sorted(overdue, key=lambda t: self._task_due(t) or today)[:8])

        sent = await self._send_to_channel(
            user_id=user_id, params=params, text=report,
            subject="🎯 Брифинг руководителя за неделю")
        return {"success": sent["success"], "message": sent["message"],
                "data": {"meetings": len(meetings), "overdue": len(overdue)}}

    @staticmethod
    def _parse_llm_json(content: Optional[str]):
        """LLM-ответ → JSON (со срезом ```-обёртки); мусор → None."""
        if not content:
            return None
        t = content.strip()
        if t.startswith("```"):
            t = t.split("```")[1]
            if t.startswith("json"):
                t = t[4:]
        try:
            return json.loads(t)
        except Exception:
            return None

    async def _execute_commitments_tracker(
        self,
        params: Dict[str, Any],
        user_id: str
    ) -> Dict[str, Any]:
        """«Кто что пообещал»: после встречи извлекает обещания/договорённости
        (кто, что, срок) и шлёт списком в чат. Нет обещаний → не шлём."""
        from automation_tools_async import set_user_context
        set_user_context(user_id=user_id)

        meeting_id = params.get("meeting_id")
        if not meeting_id:
            return {"success": False, "message": "meeting_id required"}
        loaded = await self._load_meeting_text(meeting_id)
        meeting, text = loaded["meeting"], loaded["text"]
        if not text:
            return {"success": False, "message": "У встречи нет транскрипта/саммари"}

        content = await self._llm_generate(
            artifact_type="commitments",
            system_prompt="Ты извлекаешь обещания и договорённости из встреч. Только то, что реально прозвучало.",
            user_prompt=(
                "Извлеки из встречи ОБЕЩАНИЯ и договорённости: кто, что "
                "пообещал сделать, к какому сроку (если звучал). Верни JSON-массив "
                '[{"who": "имя или роль", "what": "что сделает", "due": "срок или пусто", '
                '"quote": "короткая цитата-подтверждение"}]. Пустой массив, если обещаний '
                "не было. Только JSON.\n\n--- Встреча ---\n" + text[:12000]),
            cache_key_content=text[:12000],
            params=params,
            max_tokens=1500,
            temperature=0.2,
        )
        items = self._parse_llm_json(content)
        if items is None:
            return {"success": False, "message": "LLM недоступен — обещания не извлечены"}
        if not items:
            return {"success": True,
                    "message": "Обещаний на встрече не прозвучало — сообщение не отправлялось",
                    "data": {"count": 0, "sent": False}}

        title = meeting.get("title", "Встреча")
        msg = f"🤝 **Кто что пообещал — «{title}»:**\n\n"
        for it in items[:15]:
            line = f"• **{it.get('who') or 'Кто-то'}** — {it.get('what') or ''}"
            if it.get("due"):
                line += f" (срок: {it['due']})"
            msg += line + "\n"
        msg += "\n💡 Проверьте, что всё попало в задачник."

        sent = await self._send_to_channel(
            user_id=user_id, params=params, text=msg,
            subject=f"🤝 Обещания со встречи: {title}")
        return {"success": sent["success"], "message": sent["message"],
                "data": {"count": len(items), "sent": True}}

    async def _execute_client_risk_alert(
        self,
        params: Dict[str, Any],
        user_id: str
    ) -> Dict[str, Any]:
        """«Радар настроения клиента»: после встречи из клиентской папки LLM
        ищет сигналы риска (недовольство, конкуренты, деньги, охлаждение).
        Риск ниже порога → тишина; выше → алерт с цитатами."""
        from automation_tools_async import set_user_context
        set_user_context(user_id=user_id)

        meeting_id = params.get("meeting_id")
        if not meeting_id:
            return {"success": False, "message": "meeting_id required"}
        loaded = await self._load_meeting_text(meeting_id)
        meeting, text = loaded["meeting"], loaded["text"]
        if not text:
            return {"success": False, "message": "У встречи нет транскрипта/саммари"}

        content = await self._llm_generate(
            artifact_type="client_risk",
            system_prompt="Ты — аналитик клиентского здоровья. Оцениваешь риски по разговору строго по фактам, без паранойи.",
            user_prompt=(
                "Оцени сигналы РИСКА для отношений с клиентом в этой встрече: "
                "недовольство продуктом/сервисом, упоминание конкурентов, "
                "вопросы к цене/бюджету/оплате, срывы сроков с нашей стороны, "
                "охлаждение/уклончивость, смена ЛПР. Верни JSON: "
                '{"risk_level": "none|low|medium|high", "summary": "1-2 предложения", '
                '"signals": [{"type": "тип", "quote": "цитата", "comment": "почему это риск"}]}. '
                "Если рисков нет — risk_level none и пустой signals. Только JSON.\n\n"
                "--- Встреча ---\n" + text[:12000]),
            cache_key_content=text[:12000],
            params=params,
            max_tokens=1200,
            temperature=0.2,
        )
        data = self._parse_llm_json(content)
        if data is None:
            return {"success": False, "message": "LLM недоступен — оценка риска не выполнена"}

        level = str(data.get("risk_level") or "none").lower()
        order = {"none": 0, "low": 1, "medium": 2, "high": 3}
        min_level = str(params.get("min_level") or "medium").lower()
        if order.get(level, 0) < order.get(min_level, 2):
            return {"success": True,
                    "message": f"Риск {level or 'none'} — ниже порога ({min_level}), алерт не отправлялся",
                    "data": {"risk_level": level, "sent": False}}

        title = meeting.get("title", "Встреча")
        badge = {"medium": "🟠", "high": "🔴"}.get(level, "🟡")
        msg = (f"{badge} **Риск по клиенту ({level}) — «{title}»**\n\n"
               f"{data.get('summary') or ''}\n")
        for s in (data.get("signals") or [])[:6]:
            msg += f"\n• **{s.get('type') or 'сигнал'}**: «{(s.get('quote') or '')[:160]}»"
            if s.get("comment"):
                msg += f"\n  ↳ {s['comment']}"
        msg += "\n\n💡 Стоит связаться с клиентом в ближайшие дни."

        sent = await self._send_to_channel(
            user_id=user_id, params=params, text=msg,
            subject=f"{badge} Риск по клиенту: {title}")
        return {"success": sent["success"], "message": sent["message"],
                "data": {"risk_level": level, "signals": len(data.get("signals") or []),
                         "sent": True}}

    # ==================== Доставка по каналам ====================

    async def _send_to_channel(
        self,
        *,
        user_id: str,
        params: Dict[str, Any],
        text: str,
        subject: str,
    ) -> Dict[str, Any]:
        """Единая доставка отчётов: Telegram (по умолчанию) / Email / Slack.

        Получатель по каналу: chat_id (Telegram; пусто = чат по умолчанию
        из интеграций; группы поддерживаются — ID вида «-100…», бот должен
        быть добавлен в группу), to_email|to (почта), slack_channel (Slack
        через IntegrationRegistry с per-user ключами). Notion не здесь —
        у него свой формат страницы, вызывающие обрабатывают сами."""
        # Именованная группа получателей («Отдел продаж») — разворачиваем
        # в канал + список адресатов ДО отправки.
        if params.get("recipient_group_id"):
            try:
                from backend.core.automations.recipient_groups import resolve_group
                grp = await resolve_group(user_id, str(params["recipient_group_id"]))
                if grp and grp.get("recipients"):
                    params = dict(params)
                    params["channel"] = grp.get("channel") or params.get("channel")
                    recips = grp["recipients"]
                    if (grp.get("channel") or "telegram") == "email":
                        params["to_emails"] = recips
                    else:
                        params["chat_ids"] = recips
            except Exception as e:
                logger.warning("recipient group resolve failed: %s", e)

        channel = str(params.get("channel") or "telegram").lower()

        def _many(value, list_value) -> List[str]:
            """Группа получателей: список ИЛИ строка через запятую."""
            if isinstance(list_value, list) and list_value:
                return [str(v).strip() for v in list_value if str(v).strip()]
            return [p.strip() for p in str(value or "").split(",") if p.strip()]

        if channel == "email":
            from automation_tools_async import send_email
            tos = _many(params.get("to_email") or params.get("to"),
                        params.get("to_emails"))
            if not tos:
                return {"success": False,
                        "message": "Не указан email получателя (to_email)"}
            # markdown-разметка дайджестов в письме выглядит мусором
            # («**жирный**» звёздочками) — конвертируем в простой HTML
            body = _md_to_email_html(text)
            ok_count, last_msg = 0, ""
            for to in tos:
                result = json.loads(await send_email(to=to, subject=subject, body=body))
                if result.get("success"):
                    ok_count += 1
                else:
                    last_msg = result.get("error") or result.get("message") or ""
            if not ok_count:
                # Gmail OAuth не настроен/упал → SMTP/SendGrid-транспорт
                # (core/notifications/email). Раньше фолбэка не было, и без
                # Gmail владелец не мог отправить письмо вообще.
                try:
                    from backend.core.notifications.email import (
                        EmailMessage,
                        send_email as core_send_email,
                    )
                    for to in tos:
                        res = await core_send_email(EmailMessage(
                            to=to, subject=subject, text_body=text,
                            html_body=body))
                        if getattr(res, "ok", False):
                            ok_count += 1
                    if ok_count:
                        last_msg = "через SMTP-транспорт"
                except Exception as e:
                    last_msg = last_msg or str(e)
            return {"success": ok_count > 0,
                    "message": (f"Email → {ok_count}/{len(tos)} получателям"
                                + (f" ({last_msg})" if last_msg else ""))}

        if channel == "slack":
            try:
                from backend.integrations.registry import IntegrationRegistry
                reg = IntegrationRegistry()
                await reg.load_for_user(user_id)
                raw = await reg.execute_tool(
                    "slack_send_message",
                    channel=str(params.get("slack_channel") or ""),
                    text=text,
                )
                result = json.loads(raw)
                ok = result.get("success", not result.get("error"))
                return {"success": ok,
                        "message": result.get("error") or "Отправлено в Slack"}
            except Exception as e:
                return {"success": False,
                        "message": f"Slack не настроен или недоступен: {e}"}

        # telegram — по умолчанию; пусто = чат по умолчанию из интеграций
        from automation_tools_async import send_telegram_message
        chats = _many(params.get("chat_id"), params.get("chat_ids")) or [""]
        ok_count, last_msg = 0, ""
        for chat in chats:
            result = json.loads(await send_telegram_message(text, chat))
            if result.get("success"):
                ok_count += 1
            else:
                last_msg = result.get("error") or result.get("message") or ""
        return {"success": ok_count > 0,
                "message": (f"Telegram → {ok_count}/{len(chats)} чатам"
                            + (f" ({last_msg})" if last_msg and not ok_count else ""))}

    # ==================== Daily Digests ====================

    async def _execute_daily_digest(
        self,
        params: Dict[str, Any],
        user_id: str
    ) -> Dict[str, Any]:
        """Ежедневный дайджест встреч"""
        from automation_tools_async import list_meetings_by_period, set_user_context

        set_user_context(user_id=user_id)

        days_back = params.get("days_back", 1)

        # Получаем встречи за период
        meetings_json = await list_meetings_by_period(days_back=days_back, user_id=user_id)
        meetings_data = json.loads(meetings_json)
        meetings = meetings_data.get("meetings", [])

        if not meetings:
            digest = "📅 За сегодня встреч не было."
        else:
            digest = f"📅 **Дайджест встреч ({len(meetings)})**\n\n"
            for m in meetings[:10]:
                digest += f"• **{m.get('title', 'Встреча')}**\n"
                digest += f"  📆 {m.get('created_at', '')[:10]}\n\n"

        sent = await self._send_to_channel(
            user_id=user_id, params=params, text=digest,
            subject="📅 Дайджест встреч")
        return {"success": sent["success"],
                "message": sent["message"],
                "data": {"meetings_count": len(meetings)}}

    async def _execute_morning_briefing(
        self,
        params: Dict[str, Any],
        user_id: str
    ) -> Dict[str, Any]:
        """Утренний брифинг: календарь (если подключён) + дедлайны и
        просрочка по РЕАЛЬНЫМ задачам из встреч."""
        from automation_tools_async import list_calendar_events, set_user_context

        set_user_context(user_id=user_id)

        # События календаря (честно: «не подключён» вместо «встреч нет»)
        events: List[Dict[str, Any]] = []
        calendar_note = ""
        try:
            events_data = json.loads(await list_calendar_events(days_ahead=1))
            if events_data.get("error"):
                calendar_note = "📅 Календарь не подключён (раздел «Интеграции»)\n"
            else:
                events = events_data.get("events", [])
        except Exception:
            calendar_note = "📅 Календарь недоступен\n"

        # Формируем брифинг
        briefing = "☀️ **Доброе утро! План на сегодня:**\n\n"

        if events:
            briefing += "📅 **Встречи:**\n"
            for e in events:
                time = e.get("start", "")
                if "T" in time:
                    time = time.split("T")[1][:5]
                briefing += f"• {time} - {e.get('summary', 'Событие')}\n"
        elif calendar_note:
            briefing += calendar_note
        else:
            briefing += "📅 Встреч в календаре на сегодня нет\n"

        # Задачи из встреч: дедлайн сегодня + просроченные (реальные данные)
        if params.get("include_tasks", True) or params.get("include_deadlines", True):
            try:
                tasks = await self._fetch_user_tasks(user_id)
                today = date.today()
                due_today = [t for t in tasks
                             if self._task_due(t) == today and not self._task_is_closed(t)]
                overdue = [t for t in tasks
                           if (self._task_due(t) or today) < today and not self._task_is_closed(t)]
                if due_today:
                    briefing += f"\n⏰ **Дедлайн сегодня ({len(due_today)}):**\n"
                    briefing += "\n".join(self._task_line(t) for t in due_today[:7]) + "\n"
                if overdue:
                    briefing += f"\n⚠️ Просроченных задач: {len(overdue)}\n"
            except Exception as e:
                logger.debug("briefing tasks skipped: %s", e)

        briefing += "\n✨ Продуктивного дня!"

        sent = await self._send_to_channel(
            user_id=user_id, params=params, text=briefing,
            subject="☀️ Утренний брифинг")
        return {
            "success": sent["success"],
            "message": sent["message"],
            "data": {"events_count": len(events)}
        }

    # ==================== Weekly Reports ====================

    # --- Задачи из встреч: общие хелперы (weekly report / overdue / deadlines).
    # Источник — meeting_tasks по логину (get_all_user_tasks фильтрует через
    # встречи пользователя). Статусы в базе не нормированы → распознаём
    # «выполнено/отменено» по набору синонимов, всё остальное считаем открытым.

    _TASK_CLOSED_STATUSES = frozenset({
        "completed", "done", "closed", "finished", "resolved",
        "cancelled", "canceled",
        "выполнена", "выполнено", "готово", "сделано", "закрыта", "отменена",
    })

    async def _fetch_user_tasks(self, user_id: str, days_back: int = 90) -> List[Dict[str, Any]]:
        """Все задачи из встреч пользователя за период (по логину)."""
        from automation_tools_async import get_all_user_tasks, set_user_context

        set_user_context(user_id=user_id)
        raw = await get_all_user_tasks(days_back=days_back, user_id=user_id)
        data = json.loads(raw)
        if "error" in data:
            raise RuntimeError(data["error"])
        return data.get("tasks", []) or []

    @classmethod
    def _task_is_closed(cls, task: Dict[str, Any]) -> bool:
        return (task.get("status") or "").strip().lower() in cls._TASK_CLOSED_STATUSES

    @staticmethod
    def _task_due(task: Dict[str, Any]) -> Optional[date]:
        """due_date → date; формат в базе не гарантирован — парсим защитно."""
        raw = str(task.get("due_date") or "").strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except ValueError:
            for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
                try:
                    return datetime.strptime(raw[:10], fmt).date()
                except ValueError:
                    continue
        return None

    @staticmethod
    def _task_line(task: Dict[str, Any], note: str = "") -> str:
        line = f"• {task.get('title') or 'Задача без названия'}"
        assignee = (task.get("assignee") or "").strip()
        if assignee:
            line += f" — {assignee}"
        meeting = (task.get("meeting_title") or "").strip()
        if meeting:
            line += f" (из встречи «{meeting}»)"
        if note:
            line += f" — {note}"
        return line

    async def _execute_weekly_tasks_report(
        self,
        params: Dict[str, Any],
        user_id: str
    ) -> Dict[str, Any]:
        """Еженедельный отчёт по РЕАЛЬНЫМ задачам из встреч (раньше — заглушка
        с выдуманными цифрами). Считаем по meeting_tasks пользователя:
        выполнено / просрочено / дедлайн на неделе / в работе."""
        tasks = await self._fetch_user_tasks(
            user_id, days_back=int(params.get("days_back", 90)))
        today = date.today()

        done = [t for t in tasks if self._task_is_closed(t)]
        open_tasks = [t for t in tasks if not self._task_is_closed(t)]
        overdue, upcoming, in_progress = [], [], []
        for t in open_tasks:
            due = self._task_due(t)
            if due and due < today:
                overdue.append((t, (today - due).days))
            elif due and due <= today + timedelta(days=7):
                upcoming.append((t, due))
            else:
                in_progress.append(t)
        overdue.sort(key=lambda p: -p[1])
        upcoming.sort(key=lambda p: p[1])

        if not tasks:
            report = ("📊 **Отчёт по задачам из встреч**\n\n"
                      "За последние недели задач из встреч не найдено. "
                      "Задачи появляются автоматически после анализа встреч в MeetFlow.")
        else:
            report = (f"📊 **Отчёт по задачам из встреч**\n\n"
                      f"✅ Выполнено: {len(done)}\n"
                      f"⏳ В работе: {len(in_progress)}\n"
                      f"⚠️ Просрочено: {len(overdue)}\n"
                      f"📅 Дедлайн на этой неделе: {len(upcoming)}\n")
            # Галочки шаблона («Что включить») реально управляют секциями
            if overdue and params.get("include_overdue", True):
                report += "\n**Просрочены:**\n" + "\n".join(
                    self._task_line(t, f"просрочена на {d} дн.")
                    for t, d in overdue[:10]) + "\n"
            if upcoming and params.get("include_upcoming", True):
                report += "\n**Дедлайн близко:**\n" + "\n".join(
                    self._task_line(t, f"до {due.strftime('%d.%m')}")
                    for t, due in upcoming[:10]) + "\n"
            if done and params.get("include_completed", True):
                report += "\n**Выполнено за период:**\n" + "\n".join(
                    self._task_line(t) for t in done[:10]) + "\n"

        stats = {"total": len(tasks), "done": len(done), "overdue": len(overdue),
                 "upcoming": len(upcoming), "in_progress": len(in_progress)}

        # channel=email без адреса → Telegram (пустой chat_id = чат по умолчанию)
        send_params = dict(params)
        if send_params.get("channel", "email") == "email" and not (
                send_params.get("to_email") or send_params.get("to")):
            send_params["channel"] = "telegram"
        sent = await self._send_to_channel(
            user_id=user_id, params=send_params, text=report,
            subject="📊 Отчёт по задачам из встреч")
        return {"success": sent["success"], "message": sent["message"],
                "data": stats}

    async def _execute_weekly_meetings_summary(
        self,
        params: Dict[str, Any],
        user_id: str
    ) -> Dict[str, Any]:
        """Еженедельная сводка встреч"""
        from automation_tools_async import (
            create_notion_page,
            list_meetings_by_period,
            set_user_context,
        )

        set_user_context(user_id=user_id)

        # Получаем встречи за неделю
        meetings_json = await list_meetings_by_period(days_back=7, user_id=user_id)
        meetings_data = json.loads(meetings_json)
        meetings = meetings_data.get("meetings", [])

        # Формируем сводку
        summary = "# 📋 Сводка встреч за неделю\n\n"
        summary += f"**Всего встреч:** {len(meetings)}\n\n"

        for m in meetings[:20]:
            summary += f"## {m.get('title', 'Встреча')}\n"
            summary += f"📆 {m.get('created_at', '')[:10]}\n\n"

        # Канал: notion — страница; telegram/email/slack — отправка текста.
        # Раньше не-notion канал возвращал «generated» и НИКУДА не слал —
        # пользователь выбирал Telegram и молча не получал ничего.
        channel = params.get("channel", "notion")
        if channel == "notion":
            title = f"📋 Сводка встреч {datetime.now().strftime('%d.%m.%Y')}"
            parent_page_id = params.get("parent_page_id", "")
            result = await create_notion_page(title, summary, parent_page_id)
            result_data = json.loads(result)
            return {"success": "error" not in result_data, "message": "Weekly summary created in Notion"}

        sent = await self._send_to_channel(
            user_id=user_id, params=params, text=summary,
            subject="📋 Сводка встреч за неделю")
        return {"success": sent["success"], "message": sent["message"],
                "data": {"meetings_count": len(meetings)}}

    # ==================== Task Sync ====================

    async def _execute_sync_overdue_tasks(
        self,
        params: Dict[str, Any],
        user_id: str
    ) -> Dict[str, Any]:
        """Уведомление о РЕАЛЬНЫХ просроченных задачах из встреч (раньше —
        заглушка). Нет просроченных → ничего не шлём (запуск дважды в день,
        пустые уведомления = спам)."""
        tasks = await self._fetch_user_tasks(user_id)
        today = date.today()
        overdue = []
        for t in tasks:
            due = self._task_due(t)
            if due and due < today and not self._task_is_closed(t):
                overdue.append((t, (today - due).days))

        if not overdue:
            return {"success": True,
                    "message": "Просроченных задач нет — уведомление не отправлялось",
                    "data": {"overdue_count": 0, "sent": False}}

        overdue.sort(key=lambda p: -p[1])
        escalate_after = int(params.get("escalate_after_days", 3))
        message = f"⚠️ **Просроченные задачи ({len(overdue)}):**\n\n"
        for t, days in overdue[:15]:
            marker = "‼️ " if days >= escalate_after else ""
            message += marker + self._task_line(t, f"просрочена на {days} дн.") + "\n"
        if len(overdue) > 15:
            message += f"…и ещё {len(overdue) - 15}\n"
        message += "\nПожалуйста, обновите статус задач."

        sent = await self._send_to_channel(
            user_id=user_id, params=params, text=message,
            subject="⚠️ Просроченные задачи")
        return {
            "success": sent["success"],
            "message": sent["message"],
            "data": {"overdue_count": len(overdue), "sent": True}
        }

    async def _execute_deadline_reminder(
        self,
        params: Dict[str, Any],
        user_id: str
    ) -> Dict[str, Any]:
        """Напоминание о РЕАЛЬНЫХ приближающихся дедлайнах задач из встреч
        (раньше — заглушка). Окно: сегодня…сегодня+days_before; нет таких
        задач → ничего не шлём."""
        days_before = int(params.get("days_before", 1))
        tasks = await self._fetch_user_tasks(user_id)
        today = date.today()
        window_end = today + timedelta(days=days_before)
        due_soon = []
        for t in tasks:
            due = self._task_due(t)
            if due and today <= due <= window_end and not self._task_is_closed(t):
                due_soon.append((t, due))

        if not due_soon:
            return {"success": True,
                    "message": "Приближающихся дедлайнов нет — напоминание не отправлялось",
                    "data": {"due_soon_count": 0, "sent": False}}

        due_soon.sort(key=lambda p: p[1])
        include_context = bool(params.get("include_context", True))
        message = f"⏰ **Дедлайны в ближайшие {days_before} дн. ({len(due_soon)}):**\n\n"
        for t, due in due_soon[:15]:
            when = "сегодня" if due == today else ("завтра" if due == today + timedelta(days=1)
                                                   else due.strftime("%d.%m"))
            message += self._task_line(t, when) + "\n"
            if include_context and (t.get("description") or "").strip():
                message += f"  ↳ {str(t['description']).strip()[:120]}\n"
        if len(due_soon) > 15:
            message += f"…и ещё {len(due_soon) - 15}\n"

        sent = await self._send_to_channel(
            user_id=user_id, params=params, text=message,
            subject="⏰ Дедлайны задач")
        return {
            "success": sent["success"],
            "message": sent["message"],
            "data": {"due_soon_count": len(due_soon), "sent": True}
        }

    # ==================== Helper Methods ====================

    async def _generate_meeting_summary(
        self,
        meeting: Dict[str, Any],
        params: Dict[str, Any]
    ) -> str:
        """Генерирует саммари встречи через LLM (issue #110 wave 2 —
        тонкая обёртка над _llm_generate с кэшем)."""
        title = meeting.get("title", "Встреча")
        date = meeting.get("created_at", "")[:10]
        transcript = meeting.get("transcription_text", "")
        custom_prompt = params.get("custom_prompt", "")

        if not transcript:
            return f"📋 **{title}** ({date})\n\n⚠️ Транскрипция недоступна"

        # Секции саммари из галок (раньше промпт был статичным)
        sections: List[str] = ["Основные темы обсуждения"]
        if params.get("include_decisions", True):
            sections.append("Ключевые решения")
        if params.get("include_tasks", True):
            sections.append("Задачи и ответственные (если упомянуты)")
        if params.get("include_participants", True):
            sections.append("Участники (если упомянуты)")
        if params.get("include_next_steps", True):
            sections.append("Следующие шаги")
        if params.get("include_next_agenda", False):
            sections.append("Предлагаемая повестка следующей встречи")
        _numbered = "\n".join(f"{i}. {s}" for i, s in enumerate(sections, 1))
        base_prompt = (f"Сделай краткое саммари встречи. Включи:\n{_numbered}\n\n"
                       "Формат: структурированный текст с эмодзи для читаемости.\n"
                       "Максимум 1500 символов.")
        if custom_prompt:
            base_prompt += f"\n\nДополнительные инструкции: {custom_prompt}"

        _flags = "".join("1" if params.get(k, d) else "0" for k, d in (
            ("include_decisions", True), ("include_tasks", True),
            ("include_participants", True), ("include_next_steps", True),
            ("include_next_agenda", False)))
        content = await self._llm_generate(
            artifact_type=f"summary:{_flags}",
            system_prompt="Ты помощник для анализа встреч. Извлекай информацию точно и структурированно.",
            user_prompt=f"{base_prompt}\n\n--- Встреча: {title} ({date}) ---\n{transcript[:12000]}",
            cache_key_content=transcript,
            params=params,
            max_tokens=2000,
        )

        if content is None:
            # Fallback to simple summary
            return (f"📋 **{title}**\n📆 {date}\n\n"
                    f"📝 **Краткое содержание:**\n{transcript[:500]}...\n")

        summary = f"📋 **{title}**\n📆 {date}\n\n{content}"
        max_length = params.get("max_length", 2000)
        return summary[:max_length]

    async def _generate_meeting_report(
        self,
        meeting: Dict[str, Any],
        params: Dict[str, Any]
    ) -> str:
        """Генерирует детальный отчёт по встрече через LLM (issue #110
        wave 2 — тонкая обёртка над _llm_generate)."""
        title = meeting.get("title", "Встреча")
        date = meeting.get("created_at", "")[:10]
        transcript = meeting.get("transcription_text", "")
        custom_prompt = params.get("custom_prompt", "")

        if not transcript:
            return f"<h1>📋 {title}</h1><p>Транскрипция недоступна</p>"

        # Секции отчёта из галок пользователя («Что включить» в модалке) —
        # раньше промпт был статичным и галки НЕ влияли на результат.
        sections: List[str] = []
        if params.get("include_transcript_summary", True):
            sections.append("Краткое резюме (2-3 предложения)")
        sections.append("Основные темы обсуждения")
        if params.get("include_decisions", True):
            sections.append("Принятые решения (список)")
        if params.get("include_action_items", True):
            sections.append("Задачи с ответственными и дедлайнами (если упомянуты)")
        sections.append("Следующие шаги")
        if params.get("include_next_agenda", False):
            sections.append("Предлагаемая ПОВЕСТКА СЛЕДУЮЩЕЙ встречи: "
                            "нерешённые вопросы, темы для продолжения, "
                            "пункты для проверки прогресса")
        sections.append("Открытые вопросы (если есть)")
        _numbered = "\n".join(f"{i}. {s}" for i, s in enumerate(sections, 1))
        base_prompt = (f"Сформируй детальный отчёт по встрече в HTML формате. Включи:\n"
                       f"{_numbered}\n\n"
                       "Используй HTML теги: h1, h2, p, ul, li, strong.")
        if custom_prompt:
            base_prompt += f"\n\nДополнительные инструкции: {custom_prompt}"

        # набор секций участвует в ключе кэша — разные галки = разные артефакты
        _flags = "".join("1" if params.get(k, d) else "0" for k, d in (
            ("include_transcript_summary", True), ("include_decisions", True),
            ("include_action_items", True), ("include_next_agenda", False)))
        content = await self._llm_generate(
            artifact_type=f"report:{_flags}",
            system_prompt="Ты помощник для создания отчётов по встречам. Создавай структурированные HTML отчёты.",
            user_prompt=f"{base_prompt}\n\n--- Встреча: {title} ({date}) ---\n{transcript[:12000]}",
            cache_key_content=transcript,
            params=params,
            max_tokens=3000,
        )

        if content is None:
            return (
                f"\n<h1>📋 Отчёт по встрече: {title}</h1>\n"
                f"<p><strong>Дата:</strong> {date}</p>\n"
                f"<h2>📝 Содержание</h2>\n"
                f"<p>{transcript[:2000]}</p>\n")
        return content

    async def _generate_notion_content(
        self,
        meeting: Dict[str, Any],
        params: Dict[str, Any]
    ) -> str:
        """Генерирует контент для Notion через LLM (issue #110 wave 2 —
        тонкая обёртка над _llm_generate)."""
        title = meeting.get("title", "Встреча")
        date = meeting.get("created_at", "")[:10]
        transcript = meeting.get("transcription_text", "")
        custom_prompt = params.get("custom_prompt", "")

        if not transcript:
            return f"""## 📅 {title}
- **Дата:** {date}
- ⚠️ Транскрипция недоступна"""

        base_prompt = """Создай структурированные заметки для Notion в Markdown формате:
1. Информация о встрече (дата, участники если упомянуты)
2. Краткое резюме (2-3 предложения)
3. Основные темы обсуждения
4. Решения (как чекбоксы - [ ])
5. Задачи с ответственными (как чекбоксы - [ ])
6. Идеи и заметки
7. Открытые вопросы

Используй Markdown: ##, -, - [ ], **bold**"""
        if custom_prompt:
            base_prompt += f"\n\nДополнительные инструкции: {custom_prompt}"

        content = await self._llm_generate(
            artifact_type="notion",
            system_prompt="Ты помощник для создания заметок. Создавай структурированный Markdown контент для Notion.",
            user_prompt=f"{base_prompt}\n\n--- Встреча: {title} ({date}) ---\n{transcript[:12000]}",
            cache_key_content=transcript,
            params=params,
            max_tokens=2500,
        )

        if content is None:
            return f"""## 📅 {title}
- **Дата:** {date}

## 📝 Содержание
{transcript[:1000]}..."""
        return content

    async def _extract_tasks_from_transcript(
        self,
        transcript: str,
        params: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Извлекает задачи из транскрипции через LLM (issue #110 wave 2 —
        обёртка над _llm_generate с кэшем + keyword-fallback)."""
        import json

        if not transcript:
            return []

        custom_prompt = params.get("custom_prompt", "")

        base_prompt = """Извлеки все задачи из транскрипции встречи. Для каждой задачи определи:
- title: краткое название задачи (до 100 символов)
- description: подробное описание (если есть контекст)
- assignee: ответственный (имя, если упомянуто)
- deadline: дедлайн (если упомянут, в формате YYYY-MM-DD)
- priority: приоритет (high/medium/low, определи по контексту)

Верни JSON массив задач. Только JSON, без markdown."""
        if custom_prompt:
            base_prompt += f"\n\nДополнительные инструкции: {custom_prompt}"

        content = await self._llm_generate(
            artifact_type="tasks",
            system_prompt="Ты помощник для извлечения задач из встреч. Возвращай только валидный JSON.",
            user_prompt=f"{base_prompt}\n\n--- Транскрипция ---\n{transcript[:12000]}",
            cache_key_content=transcript,
            params=params,
            max_tokens=2000,
            temperature=0.2,
        )

        if content is not None:
            try:
                content = content.strip()
                # Убираем markdown-обёртку если есть
                if content.startswith("```"):
                    content = content.split("```")[1]
                    if content.startswith("json"):
                        content = content[4:]
                tasks = json.loads(content)
                return tasks[:20] if isinstance(tasks, list) else []
            except Exception as e:
                logger.error("Task JSON parse failed: %s", e)

        # Fallback: keyword-based extraction (LLM недоступен / JSON битый)
        tasks: List[Dict[str, Any]] = []
        keywords = ["нужно", "задача", "сделать", "выполнить", "подготовить"]
        for line in transcript.split("\n"):
            for kw in keywords:
                if kw in line.lower() and len(line) > 20:
                    tasks.append({
                        "title": line[:100].strip(),
                        "description": "",
                        "assignee": "",
                    })
                    break
        return tasks[:10]


def get_workflow_templates() -> List[Dict[str, Any]]:
    """Получить список всех шаблонов автоматизаций"""
    return [t.to_dict() for t in WORKFLOW_TEMPLATES]


def get_workflow_template(template_id: str) -> Optional[Dict[str, Any]]:
    """Получить шаблон по ID"""
    for t in WORKFLOW_TEMPLATES:
        if t.id == template_id:
            return t.to_dict()
    return None


# Singleton executor
_workflow_executor: Optional[MeetingWorkflowExecutor] = None


def get_workflow_executor() -> MeetingWorkflowExecutor:
    """Получить singleton экземпляр исполнителя"""
    global _workflow_executor
    if _workflow_executor is None:
        _workflow_executor = MeetingWorkflowExecutor()
    return _workflow_executor


