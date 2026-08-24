# -*- coding: utf-8 -*-
"""
Automation Scheduler - фоновый планировщик задач.

Использует APScheduler для периодической проверки и выполнения задач.
Работает как фоновый процесс в asyncio event loop.

Включает:
1. AutomationScheduler — проверка и выполнение автоматизаций из БД
2. NightlyConsolidationJob — адаптивная ночная консолидация по расписанию
   - Автоматически определяет время на основе нагрузки
   - Запускается через APScheduler CronTrigger
   - Конфигурация через ENV: NIGHT_PROCESSING_START_HOUR, NIGHT_PROCESSING_END_HOUR
"""
import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .automation_service import AutomationService, get_automation_service

logger = logging.getLogger(__name__)

# Интервал проверки задач (секунды)
CHECK_INTERVAL = int(os.getenv("AUTOMATION_CHECK_INTERVAL", "10"))
# Интервал проактивного скана инсайтов (часы); сам скан — за флагом
PROACTIVE_SCAN_HOURS = int(os.getenv("PROACTIVE_SCAN_HOURS", "6"))

# Ночная консолидация — конфигурация
NIGHT_PROCESSING_ENABLED = os.getenv("NIGHT_PROCESSING_ENABLED", "true").lower() in ("true", "1", "yes")
NIGHT_PROCESSING_START_HOUR = int(os.getenv("NIGHT_PROCESSING_START_HOUR", "2"))
NIGHT_PROCESSING_END_HOUR = int(os.getenv("NIGHT_PROCESSING_END_HOUR", "6"))

# Адаптивные пороги нагрузки
# Если за последний час было >LOAD_THRESHOLD запросов, откладываем консолидацию
LOAD_THRESHOLD = int(os.getenv("NIGHT_LOAD_THRESHOLD", "5"))


class LoadMonitor:
    """
    Монитор нагрузки системы.

    Отслеживает количество запросов и определяет,
    безопасно ли запускать тяжёлые фоновые задачи.

    Используется ночным планировщиком для адаптивного
    запуска консолидации — если система под нагрузкой,
    задача откладывается до следующего слота.
    """

    def __init__(self):
        self._request_timestamps: list[float] = []
        self._max_history = 1000

    def record_request(self):
        """Записать факт запроса к системе."""
        now = time.time()
        self._request_timestamps.append(now)
        # Чистим старые записи (>2 часа)
        cutoff = now - 7200
        self._request_timestamps = [
            ts for ts in self._request_timestamps if ts > cutoff
        ]
        if len(self._request_timestamps) > self._max_history:
            self._request_timestamps = self._request_timestamps[-self._max_history:]

    def get_load_last_hour(self) -> int:
        """Количество запросов за последний час."""
        cutoff = time.time() - 3600
        return sum(1 for ts in self._request_timestamps if ts > cutoff)

    def get_load_last_15min(self) -> int:
        """Количество запросов за последние 15 минут."""
        cutoff = time.time() - 900
        return sum(1 for ts in self._request_timestamps if ts > cutoff)

    def is_system_idle(self, threshold: int = LOAD_THRESHOLD) -> bool:
        """
        Проверить, что система не под нагрузкой.

        Returns:
            True если запросов за последний час < threshold
        """
        return self.get_load_last_hour() < threshold

    def get_stats(self) -> Dict[str, Any]:
        """Статистика нагрузки."""
        return {
            "requests_last_15min": self.get_load_last_15min(),
            "requests_last_hour": self.get_load_last_hour(),
            "is_idle": self.is_system_idle(),
            "threshold": LOAD_THRESHOLD,
        }


# Глобальный монитор нагрузки (singleton)
_load_monitor: Optional[LoadMonitor] = None

def get_load_monitor() -> LoadMonitor:
    """Получить глобальный монитор нагрузки."""
    global _load_monitor
    if _load_monitor is None:
        _load_monitor = LoadMonitor()
    return _load_monitor


class AutomationScheduler:
    """
    Фоновый планировщик автоматизаций.

    Периодически проверяет базу данных на наличие задач,
    готовых к выполнению, и запускает их.

    Также управляет ночной консолидацией:
    - CronTrigger на NIGHT_PROCESSING_START_HOUR (по умолчанию 2:00 UTC)
    - Адаптивный запуск: если система под нагрузкой, задача
      откладывается на 30 минут (до NIGHT_PROCESSING_END_HOUR)
    - Полный pipeline: коррекции → decay → консолидация памяти → снэпшоты → мудрость
    """

    def __init__(self, service: Optional[AutomationService] = None):
        self.service = service or get_automation_service()
        self.scheduler: Optional[AsyncIOScheduler] = None
        self._running = False
        self._check_task: Optional[asyncio.Task] = None
        self._nightly_running = False
        self._nightly_last_result: Optional[Dict[str, Any]] = None
        self._nightly_last_run: Optional[str] = None
        self._nightly_postpone_count = 0
        # health: когда планировщик последний раз реально тикал
        self._last_tick_at: Optional[str] = None
        self._tick_count = 0

    async def start(self):
        """Запустить планировщик"""
        if self._running:
            logger.warning("⚠️ Scheduler already running")
            return

        self._running = True

        # Создаем APScheduler
        self.scheduler = AsyncIOScheduler(
            timezone="UTC",
            job_defaults={
                'coalesce': True,
                'max_instances': 1,
                'misfire_grace_time': 300  # 5 мин grace для ночных задач
            }
        )

        # Задача 1: проверка автоматизаций (каждые N секунд)
        self.scheduler.add_job(
            self._check_and_execute,
            trigger=IntervalTrigger(seconds=CHECK_INTERVAL),
            id="automation_checker",
            name="Check and execute due automations",
            replace_existing=True,
        )

        # Задача 2: ночная консолидация (по cron-расписанию)
        if NIGHT_PROCESSING_ENABLED:
            self.scheduler.add_job(
                self._run_nightly_consolidation_adaptive,
                trigger=CronTrigger(
                    hour=NIGHT_PROCESSING_START_HOUR,
                    minute=0,
                    timezone="UTC"
                ),
                id="nightly_consolidation",
                name=f"Nightly consolidation (adaptive, {NIGHT_PROCESSING_START_HOUR}:00-{NIGHT_PROCESSING_END_HOUR}:00 UTC)",
                replace_existing=True,
            )
            logger.info(
                f"🌙 Nightly consolidation scheduled: "
                f"{NIGHT_PROCESSING_START_HOUR}:00-{NIGHT_PROCESSING_END_HOUR}:00 UTC "
                f"(adaptive, load threshold={LOAD_THRESHOLD} req/h)"
            )
        else:
            logger.info("ℹ️ Nightly consolidation disabled (NIGHT_PROCESSING_ENABLED=false)")

        # Задача 3: ПРОАКТИВНЫЙ цикл инсайтов (за флагом
        # enable_proactive_insight_scan). Система сама, без участия человека,
        # раз в PROACTIVE_SCAN_HOURS прогоняет сканеры (blind spots,
        # рассинхроны, аномалии, сигнатуры встреч) по каждому пользователю
        # и складывает НОВЫЕ находки в ленту инсайтов (идемпотентно —
        # появляется только реально новое). Это «новая исследовательская
        # информация появляется сама»; Telegram-доставка — следующий шаг.
        try:
            from backend.core.config.feature_flags import get_feature_flags
            _proactive_on = bool(get_feature_flags().enable_proactive_insight_scan)
        except Exception:
            _proactive_on = False
        if _proactive_on:
            self.scheduler.add_job(
                self._proactive_insight_scan,
                trigger=IntervalTrigger(hours=PROACTIVE_SCAN_HOURS),
                id="proactive_insight_scan",
                name=f"Proactive insight scan (every {PROACTIVE_SCAN_HOURS}h)",
                replace_existing=True,
            )
            logger.info(f"🔮 Proactive insight scan scheduled: every {PROACTIVE_SCAN_HOURS}h")
        else:
            logger.info("ℹ️ Proactive insight scan disabled (enable_proactive_insight_scan=false)")

        # Наблюдатель Ф2: цикл агента (скрин фронтов → ротация → зацепка).
        # Каденс на пользователя держит сам цикл (OBSERVER_CYCLE_DAYS);
        # job лишь регулярно даёт ему шанс. За флагом enable_observer_agent.
        try:
            from backend.core.config.feature_flags import get_feature_flags
            _observer_on = bool(get_feature_flags().enable_observer_agent)
        except Exception:
            _observer_on = False
        if _observer_on:
            _obs_hours = int(os.getenv("OBSERVER_SCAN_HOURS", "6"))
            self.scheduler.add_job(
                self._observer_cycle_job,
                trigger=IntervalTrigger(hours=_obs_hours),
                id="observer_cycle",
                name=f"Observer agent cycle (every {_obs_hours}h)",
                replace_existing=True,
            )
            logger.info(f"👁 Observer agent scheduled: every {_obs_hours}h")
        else:
            logger.info("ℹ️ Observer agent disabled (enable_observer_agent=false)")

        # Ретеншн логов выполнений — ежедневно (логи копились без чистки).
        self.scheduler.add_job(
            self._purge_logs_job,
            trigger=IntervalTrigger(hours=24),
            id="automation_logs_retention",
            name="Automation logs retention (daily)",
            replace_existing=True,
        )
        logger.info("🧹 Log retention scheduled: daily (keep 90 days)")

        # Reaper зависших RUNNING executor-задач (fire-and-forget не переживает
        # рестарт → задача висела бы RUNNING навсегда). Каждые N минут.
        self.scheduler.add_job(
            self._reap_stale_executors,
            trigger=IntervalTrigger(
                minutes=int(os.getenv("EXECUTOR_REAP_INTERVAL_MIN", "15"))),
            id="executor_stale_reaper",
            name="Reap stale RUNNING executor tasks",
            replace_existing=True,
        )
        logger.info("🪦 Executor reaper scheduled: every %s min",
                    os.getenv("EXECUTOR_REAP_INTERVAL_MIN", "15"))

        # Цикл самоулучшения: еженедельный дайджест обратной связи в саппорт.
        self.scheduler.add_job(
            self._feedback_digest_job,
            trigger=CronTrigger.from_crontab(
                os.getenv("FEEDBACK_DIGEST_CRON", "0 9 * * 1")),  # пн 9:00
            id="feedback_digest_weekly",
            name="Weekly feedback digest to support",
            replace_existing=True,
        )
        logger.info("📊 Feedback digest scheduled: %s",
                    os.getenv("FEEDBACK_DIGEST_CRON", "0 9 * * 1"))

        # Еженедельный пуш персонального совета в Telegram (ВЫКЛ по умолчанию —
        # работает только при GUIDE_WEEKLY_PUSH=on; частота — раз в неделю).
        self.scheduler.add_job(
            self._guide_advice_push_job,
            trigger=CronTrigger.from_crontab(
                os.getenv("GUIDE_ADVICE_PUSH_CRON", "0 10 * * 1")),  # пн 10:00
            id="guide_advice_weekly_push",
            name="Weekly personal advice push (Telegram)",
            replace_existing=True,
        )

        # Safety-поллер синхронизации: досинк встреч, пропущенных webhook'ом
        # (коннект с MeetFlow push-only). ВЫКЛ по умолчанию — SYNC_POLLER=on;
        # дедуп гарантирует работу только по новым встречам.
        self.scheduler.add_job(
            self._sync_poller_job,
            trigger=IntervalTrigger(
                minutes=int(os.getenv("SYNC_POLL_INTERVAL_MIN", "60"))),
            id="knowledge_sync_safety_poller",
            name="Knowledge-sync safety poller (missed webhooks)",
            replace_existing=True,
        )

        # IMAP-поллер входящей почты (если нет внешнего mail-вебхука). ВЫКЛ по
        # умолчанию — ENABLE_IMAP_POLLER=on; \\Seen-дедуп гарантирует обработку
        # только новых писем. Джоба сама no-op'ит, когда флаг выключен.
        self.scheduler.add_job(
            self._imap_poller_job,
            trigger=IntervalTrigger(
                minutes=int(os.getenv("IMAP_POLL_INTERVAL_MIN", "5"))),
            id="inbound_imap_poller",
            name="Inbound IMAP poller (new_email events)",
            replace_existing=True,
        )

        # Триггеры процесс-досок по расписанию (P3 BOARD_ROADMAP). ВЫКЛ по
        # умолчанию — BOARD_PROCESS_TRIGGERS=on; дедуп по last_triggered_at
        # (не гоняем раньше интервала → без runaway-расхода).
        self.scheduler.add_job(
            self._board_trigger_job,
            trigger=IntervalTrigger(
                minutes=int(os.getenv("BOARD_TRIGGER_INTERVAL_MIN", "5"))),
            id="board_process_triggers",
            name="Board process schedule triggers",
            replace_existing=True,
        )

        # Регулярный inbound-sync датасетов (A1): тянуть данные из
        # CRM/BI/Sheets по расписанию. ВЫКЛ по умолчанию (enable_dataset_sync
        # / ENABLE_DATASET_SYNC); дедуп по sync.last_at на датасете.
        self.scheduler.add_job(
            self._dataset_sync_job,
            trigger=IntervalTrigger(
                minutes=int(os.getenv("DATASET_SYNC_INTERVAL_MIN", "15"))),
            id="dataset_inbound_sync",
            name="Scheduled dataset inbound sync",
            replace_existing=True,
        )

        # Скан неявных упоминаний Tessbrain/MeetFlow с проблемой (ВЫКЛ по
        # умолчанию — GUIDE_MENTION_SCAN=on; агрегат в саппорт, без LLM).
        self.scheduler.add_job(
            self._mention_scan_job,
            trigger=CronTrigger.from_crontab(
                os.getenv("GUIDE_MENTION_SCAN_CRON", "0 11 * * 1")),  # пн 11:00
            id="guide_mention_scan_weekly",
            name="Weekly implicit-mention scan to support",
            replace_existing=True,
        )

        # Пульс исполнения: еженедельная сводка сигналов по задачам владельцу
        # (ВЫКЛ по умолчанию — EXECUTION_PULSE_WEEKLY=on; только чтение
        # источников + доставка владельцу, сотрудникам ничего не шлётся).
        self.scheduler.add_job(
            self._execution_pulse_job,
            trigger=CronTrigger.from_crontab(
                os.getenv("EXECUTION_PULSE_CRON", "0 7 * * 1")),  # пн 07:00 UTC
            id="execution_pulse_weekly",
            name="Weekly execution pulse report to owners",
            replace_existing=True,
        )

        # Сверка синхронизации организации. Раньше отчёт считался ТОЛЬКО
        # когда руководитель открывал дашборд — то есть обещание «ловится в
        # ту неделю, когда возникло» держалось на том, что кто-то зайдёт.
        # Теперь считается по расписанию, и уведомления о новых значимых
        # рассинхронах уходят сторонам сами (дедуп по отпечатку внутри
        # sync_notify не даст повторов). Отчёт детерминированный, без LLM.
        # Недельный снимок целей (OKR-чекпоинт): раньше weekly_review
        # существовал только как эндпоинт — историю недель фиксировал тот,
        # кто не забыл открыть. Снимок дешёвый (with_llm=False).
        self.scheduler.add_job(
            self._goals_snapshot_job,
            trigger=CronTrigger.from_crontab(
                os.getenv("GOALS_SNAPSHOT_CRON", "30 5 * * 1")),  # пн 05:30 UTC
            id="goals_weekly_snapshot",
            name="Weekly goals progress snapshot (OKR checkpoint)",
            replace_existing=True,
        )

        self.scheduler.add_job(
            self._org_sync_job,
            trigger=CronTrigger.from_crontab(
                os.getenv("ORG_SYNC_CRON", "0 6 * * 1")),  # пн 06:00 UTC
            id="org_sync_weekly",
            name="Weekly organization sync check + desync notifications",
            replace_existing=True,
        )

        # Персональные пуши исполнителям (фаза 2). Двойной гейт: джоба и сам
        # движок проверяют ENABLE_EXECUTION_PULSE_PUSH (default OFF) —
        # рассылка сотрудникам включается только осознанно.
        self.scheduler.add_job(
            self._execution_pulse_push_job,
            trigger=CronTrigger.from_crontab(
                os.getenv("EXECUTION_PULSE_PUSH_CRON", "0 9 * * 1-5")),  # будни 09:00 UTC
            id="execution_pulse_push_daily",
            name="Daily execution pulse pushes to assignees",
            replace_existing=True,
        )

        # Автоцикл переписок (chat_ingest): Slack re-pull → анализ с
        # цитатами → KB-документ. Гейт внутри: ENABLE_CHAT_INGEST (OFF).
        # TG-группам pull не нужен — вебхук приносит сообщения сразу;
        # джоба лишь регулярно ПЕРЕРАБАТЫВАЕТ накопленное в мозг.
        self.scheduler.add_job(
            self._chat_ingest_cycle_job,
            trigger=CronTrigger.from_crontab(
                os.getenv("CHAT_INGEST_CRON", "30 6 * * *")),  # ежедневно 06:30 UTC
            id="chat_ingest_daily",
            name="Daily chat sources analyze+digest",
            replace_existing=True,
        )

        # Линза кластеров (Louvain по графу знаний + LLM-имена). Гейт
        # ENABLE_COMMUNITY_LENS (OFF): LLM-вызовы на каждого пользователя —
        # включается осознанно; fingerprint графа не изменился → без LLM.
        self.scheduler.add_job(
            self._community_lens_job,
            trigger=CronTrigger.from_crontab(
                os.getenv("COMMUNITY_LENS_CRON", "15 5 * * *")),  # 05:15 UTC
            id="community_lens_daily",
            name="Daily graph community detection + naming",
            replace_existing=True,
        )

        self.scheduler.start()
        logger.info(f"🚀 Automation Scheduler started (check interval: {CHECK_INTERVAL}s)")

        # ВОССТАНОВЛЕНИЕ EVENT-ПОДПИСОК (фикс аудита): подписки EventBus
        # живут только в памяти и регистрировались лишь при create() —
        # после рестарта сервера событийные автоматизации молча умирали.
        # Перечитываем активные event-триггеры из БД и подписываем заново.
        try:
            await self._resubscribe_event_automations()
        except Exception:
            logger.warning("event automations resubscribe failed", exc_info=True)

    async def _resubscribe_event_automations(self) -> None:
        from backend.core.automations.event_bus import get_event_bus
        bus = get_event_bus()
        restored = 0
        # классические scheduled_automations
        try:
            autos = await self.service.list_automations(status="active")
        except Exception:
            autos = []
        for a in autos or []:
            if getattr(a, "trigger_type", "") == "event" and getattr(a, "event_type", None):
                try:
                    bus.register_event_automation(
                        user_id=a.user_id, event_type=a.event_type,
                        automation_id=a.id,
                        filter_conditions=getattr(a, "event_filter", None))
                    restored += 1
                except Exception:
                    logger.debug("resubscribe failed for %s", a.id, exc_info=True)
        # агентные automations
        try:
            from backend.core.automations.agent_automation_service import (
                get_agent_automation_service,
            )
            asvc = get_agent_automation_service()
            agent_autos = await asvc.list(status="active")
        except Exception:
            agent_autos = []
        for a in agent_autos or []:
            if getattr(a, "trigger_type", "") == "event" and getattr(a, "event_type", None):
                try:
                    bus.register_event_automation(
                        user_id=a.user_id, event_type=a.event_type,
                        automation_id=a.id,
                        filter_conditions=getattr(a, "event_filter", None))
                    restored += 1
                except Exception:
                    logger.debug("resubscribe failed for agent %s", a.id, exc_info=True)
        if restored:
            logger.info(f"🔁 Event-подписки восстановлены после рестарта: {restored}")

    async def stop(self):
        """Остановить планировщик"""
        if not self._running:
            return

        self._running = False

        if self.scheduler:
            self.scheduler.shutdown(wait=False)
            self.scheduler = None

        if self._check_task and not self._check_task.done():
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                logger.debug("suppressed exception", exc_info=True)

        logger.info("🛑 Automation Scheduler stopped")

    async def _check_and_execute(self):
        """Проверить и выполнить готовые задачи (классические + агентные)"""
        if not self._running:
            return
        self._last_tick_at = datetime.now(timezone.utc).isoformat()
        self._tick_count += 1

        try:
            # === Классические автоматизации ===
            due_automations = await self.service.get_due_automations()

            if due_automations:
                logger.info(f"📋 Found {len(due_automations)} due classic automations")

            for automation in due_automations:
                try:
                    logger.info(f"🔄 Executing: {automation.name} ({automation.action_type})")
                    result = await self.service.execute_automation(automation)

                    if result.get("success"):
                        logger.info(f"✅ Success: {automation.name}")
                    else:
                        logger.warning(f"⚠️ Failed: {automation.name} - {result.get('message')}")

                except Exception as e:
                    logger.error(f"❌ Error executing {automation.name}: {e}")

            # === Агентные автоматизации (OpenClaw-style) ===
            try:
                from backend.core.automations.agent_automation_service import (
                    get_agent_automation_service,
                )
                agent_service = get_agent_automation_service()
                due_agent = await agent_service.get_due_automations()

                if due_agent:
                    logger.info(f"🤖 Found {len(due_agent)} due agent automations")

                for agent_auto in due_agent:
                    try:
                        logger.info(f"🤖 Executing agent: {agent_auto.name} (instruction={agent_auto.instruction[:60]}...)")
                        result = await agent_service.execute(agent_auto)

                        if result.get("success"):
                            logger.info(f"✅ Agent success: {agent_auto.name} ({result.get('steps_count', 0)} steps)")
                        else:
                            logger.warning(f"⚠️ Agent failed: {agent_auto.name} - {result.get('message')}")
                    except Exception as e:
                        logger.error(f"❌ Error executing agent automation {agent_auto.name}: {e}")
            except Exception as agent_err:
                logger.debug(f"Agent automations check skipped: {agent_err}")

        except Exception as e:
            logger.error(f"❌ Scheduler check error: {e}")

    async def _proactive_insight_scan(self):
        """Проактивный цикл: прогнать сканеры инсайтов по всем пользователям.

        Дистрибутивный лок (как у nightly) — на нескольких репликах скан
        идёт один. Per-user ошибки не валят цикл. Новые находки попадают
        в персистентную ленту страницы инсайтов (идемпотентно)."""
        try:
            from backend.core.observability.dist_lock import try_acquire
            async with try_acquire("proactive_insight_scan",
                                   ttl_seconds=3600) as got:
                if not got:
                    logger.info("🔮 Proactive scan: lock held elsewhere, skip")
                    return
                from backend.core.sleep.insight_collector import (
                    collect_and_store_insights,
                )
                from backend.core.store.tenant_paths import graphs_dir
                users = [f.stem for f in graphs_dir().glob("*.json")
                         if len(f.stem) == 36 and "-" in f.stem]
                total_new = 0
                for uid in users:
                    try:
                        res = await collect_and_store_insights(uid)
                        total_new += int(res.get("added", 0))
                    except Exception as e:
                        logger.warning(f"🔮 Proactive scan: user {uid[:8]} failed: {e}")
                logger.info(f"🔮 Proactive scan done: {len(users)} users, "
                            f"{total_new} new insights")
        except Exception as e:
            logger.error(f"🔮 Proactive scan failed: {e}")

    async def _observer_cycle_job(self):
        """Наблюдатель: дать каждому пользователю шанс на цикл. Дистрибутивный
        лок как у proactive-скана; per-user ошибки не валят проход."""
        try:
            from backend.core.observability.dist_lock import try_acquire
            async with try_acquire("observer_cycle", ttl_seconds=3600) as got:
                if not got:
                    logger.info("👁 Observer: lock held elsewhere, skip")
                    return
                from backend.core.observer.agent import run_observer_cycle
                from backend.core.store.tenant_paths import graphs_dir
                users = [f.stem for f in graphs_dir().glob("*.json")
                         if len(f.stem) == 36 and "-" in f.stem]
                observed = 0
                for uid in users:
                    try:
                        res = await run_observer_cycle(uid)
                        if res.get("status") == "observed":
                            observed += 1
                    except Exception as e:
                        logger.warning(f"👁 Observer: user {uid[:8]} failed: {e}")
                logger.info(f"👁 Observer cycle done: {len(users)} users, "
                            f"{observed} observations")
        except Exception as e:
            logger.error(f"👁 Observer cycle failed: {e}")

    async def _purge_logs_job(self):
        """Ежедневная чистка старых логов выполнений (ретеншн 90 дней)."""
        try:
            import os
            days = int(os.getenv("AUTOMATION_LOG_RETENTION_DAYS", "90"))
            await self.service.purge_old_logs(days=days)
        except Exception as e:
            logger.warning("log retention job failed: %s", e)

    async def _feedback_digest_job(self):
        """Еженедельно: собрать дайджест обратной связи и отправить в саппорт."""
        try:
            from backend.core.help.feedback_digest import build_digest
            from backend.core.help.support_notify import send_to_support
            days = int(os.getenv("FEEDBACK_DIGEST_DAYS", "7"))
            res = await build_digest(days=days)
            if res.get("items_count", 0) <= 0:
                return
            await send_to_support(
                f"📊 Дайджест обратной связи Tessbrain за {days} дн. "
                f"(тикетов: {res.get('items_count')})\n\n{res.get('summary', '')}")
            logger.info("📊 Feedback digest sent (%d tickets)", res.get("items_count", 0))
        except Exception as e:
            logger.debug("feedback digest job failed: %s", e)

    async def _board_trigger_job(self):
        """Запустить процесс-доски с schedule-триггером (если BOARD_PROCESS_TRIGGERS=on)."""
        try:
            from backend.core.board.triggers import run_due_process_boards, triggers_enabled
            # Человек-в-цикле (§C): добить просроченные ожидания ответа. За своим
            # флагом (ENABLE_BOARD_WAIT) — без него no-op. Не зависит от
            # triggers_enabled, поэтому вызываем отдельно и до раннего выхода.
            try:
                from backend.core.board.process_engine import reap_expired_waits
                await reap_expired_waits()
            except Exception as _e:
                logger.debug("board wait reap skipped: %s", _e)
            if not triggers_enabled():
                return
            await run_due_process_boards()
        except Exception as e:
            logger.debug("board trigger job failed: %s", e)

    async def _dataset_sync_job(self):
        """Регулярно тянуть данные из CRM/BI/Sheets по расписанию (если вкл)."""
        try:
            from backend.core.ontology.dataset_sync import (
                run_due_dataset_syncs,
                sync_enabled,
            )
            if not sync_enabled():
                return
            res = await run_due_dataset_syncs()
            if res.get("ran") or res.get("errors"):
                logger.info("🔄 dataset sync: обновлено %d, ошибок %d",
                            res.get("ran", 0), res.get("errors", 0))
        except Exception as e:
            logger.debug("dataset sync job failed: %s", e)

    async def _sync_poller_job(self):
        """Периодически досинкивать встречи, пропущенные webhook'ом (если вкл)."""
        try:
            from backend.core.knowledge_sync_poller import (
                poll_all_subscriptions,
                poller_enabled,
            )
            if not poller_enabled():
                return
            res = await poll_all_subscriptions()
            if res.get("processed", 0):
                logger.info("🔄 sync poller: досинк %d встреч", res["processed"])
        except Exception as e:
            logger.debug("sync poller job failed: %s", e)

    async def _imap_poller_job(self):
        """Периодически тянуть входящую почту по IMAP → new_email (если вкл)."""
        try:
            from backend.core.automations.imap_poller import poll_once, poller_enabled
            if not poller_enabled():
                return
            res = await poll_once()
            if res.get("emitted", 0):
                logger.info("📬 imap poller: %d писем → new_email", res["emitted"])
        except Exception as e:
            logger.debug("imap poller job failed: %s", e)

    async def _mention_scan_job(self):
        """Еженедельно: агрегат неявных упоминаний с проблемой → в саппорт
        (если включено). Токено-экономно: без LLM по умолчанию."""
        try:
            from backend.core.help.mention_scanner import (
                build_mention_report,
                mention_scan_enabled,
            )
            if not mention_scan_enabled():
                return
            rep = await build_mention_report()
            if rep.get("count", 0) <= 0:
                return
            from backend.core.help.support_notify import send_to_support
            await send_to_support("🔎 " + rep.get("summary", ""))
            logger.info("🔎 Implicit-mention report sent (%d hits)", rep.get("count", 0))
        except Exception as e:
            logger.debug("mention scan job failed: %s", e)

    async def _guide_advice_push_job(self):
        """Еженедельно: разослать персональный совет в Telegram (если включено)."""
        try:
            from backend.core.help.advice_push import push_weekly_advice
            n = await push_weekly_advice()
            if n:
                logger.info("📨 Weekly advice pushed to %d users", n)
        except Exception as e:
            logger.debug("advice push job failed: %s", e)

    async def _goals_snapshot_job(self):
        """Еженедельно: снимок прогресса целей каждому владельцу (без LLM)."""
        try:
            from backend.core.goals.goal_tracker import weekly_review
            from backend.memory.user_profiles import get_user_profile_service
            uids = await get_user_profile_service().list_user_ids(limit=2000)
            done = 0
            for uid in uids:
                try:
                    await weekly_review(uid, with_llm=False)
                    done += 1
                except Exception:
                    continue
            if done:
                logger.info("🎯 Goals snapshot recorded for %d accounts", done)
        except Exception as e:
            logger.debug("goals snapshot job failed: %s", e)

    async def _org_sync_job(self):
        """Еженедельно: сверка синхронизации организации + уведомления сторонам.

        Гейт тот же, что у эндпоинтов (cross_dept_desync_enabled). Судью не
        зовём: скелет детерминированный и бесплатный, а LLM-подтверждение
        soft-пар остаётся ручным действием руководителя в дашборде.
        """
        try:
            from backend.config import settings as _s
            if not getattr(_s, "cross_dept_desync_enabled", False):
                return
        except Exception:
            return
        try:
            from backend.core.think.org_sync import org_sync_report
            from backend.memory.user_profiles import get_user_profile_service
            uids = await get_user_profile_service().list_user_ids(limit=2000)
            checked = 0
            for uid in uids:
                try:
                    await org_sync_report(uid, judge=False, notify=True)
                    checked += 1
                except Exception:
                    continue
            if checked:
                logger.info("🔗 Org sync checked for %d accounts", checked)
        except Exception as e:
            logger.debug("org sync job failed: %s", e)

    async def _execution_pulse_job(self):
        """Еженедельно: пульс исполнения каждому владельцу (если включено)."""
        # Включено по умолчанию (2026-08-13): еженедельный пульс — часть
        # обещания «система сигналит сама». EXECUTION_PULSE_WEEKLY=off выключает.
        if os.getenv("EXECUTION_PULSE_WEEKLY", "on").strip().lower() not in (
                "1", "on", "true", "yes"):
            return
        try:
            from backend.core.pulse import build_pulse_report
            from backend.memory.user_profiles import get_user_profile_service
            uids = await get_user_profile_service().list_user_ids(limit=2000)
            sent = 0
            for uid in uids:
                try:
                    res = await build_pulse_report(uid, deliver=True)
                    if (res.get("delivered") or {}).get("telegram") or \
                            (res.get("delivered") or {}).get("email"):
                        sent += 1
                except Exception:
                    continue
            if sent:
                logger.info("🫀 Execution pulse delivered to %d owners", sent)
        except Exception as e:
            logger.debug("execution pulse job failed: %s", e)

    async def _execution_pulse_push_job(self):
        """Будни: персональные напоминания исполнителям (если включено)."""
        try:
            from backend.core.pulse.push_engine import push_enabled, send_pushes
            if not push_enabled():
                return
            from backend.memory.user_profiles import get_user_profile_service
            uids = await get_user_profile_service().list_user_ids(limit=2000)
            total = 0
            for uid in uids:
                try:
                    res = await send_pushes(uid)
                    total += len(res.get("delivered") or [])
                except Exception:
                    continue
            if total:
                logger.info("🫀 Pulse pushes delivered: %d", total)
        except Exception as e:
            logger.debug("pulse push job failed: %s", e)

    async def _community_lens_job(self):
        """Ежедневно: кластеры графа каждого пользователя (если включено)."""
        try:
            from backend.core.insight.communities import (
                build_communities,
                lens_enabled,
            )
            if not lens_enabled():
                return
            from backend.memory.user_profiles import get_user_profile_service
            uids = await get_user_profile_service().list_user_ids(limit=2000)
            rebuilt = 0
            for uid in uids:
                try:
                    res = await build_communities(uid, with_llm=True)
                    if res.get("status") == "success" and not res.get("unchanged"):
                        rebuilt += 1
                except Exception:
                    continue
            if rebuilt:
                logger.info("🕸️ Community lens rebuilt for %d users", rebuilt)
        except Exception as e:
            logger.debug("community lens job failed: %s", e)

    async def _chat_ingest_cycle_job(self):
        """Ежедневно: включённые источники переписки → анализ → в мозг."""
        try:
            from backend.core.messengers.chat_ingest import (
                auto_cycle,
                ingest_enabled,
                list_ingest_users,
            )
            if not ingest_enabled():
                return
            digested = 0
            failed = 0
            crashed: list[str] = []
            for uid in list_ingest_users():
                try:
                    res = await auto_cycle(uid)
                    digested += int(res.get("digested") or 0)
                    failed += int(res.get("failed") or 0)
                except Exception:
                    # Раньше здесь стоял голый `continue`: цикл падал по
                    # пользователю бесследно. Теперь это видно.
                    crashed.append(uid)
                    logger.exception(
                        "chat ingest cycle: пользователь %s пропущен", uid)
            if digested:
                logger.info("💬 Chat sources digested to brain: %d", digested)
            if failed or crashed:
                logger.warning(
                    "💬 Chat ingest прошёл частично: %d шагов не доехало, "
                    "%d пользователей пропущено. Подробности — журнал "
                    "неудач (/chat-sources/failures).", failed, len(crashed))
        except Exception as e:
            logger.debug("chat ingest cycle job failed: %s", e)

    async def _reap_stale_executors(self):
        """Пометить зависшие RUNNING executor-задачи как FAILED (по таймауту).

        Порог EXECUTOR_STALE_MINUTES должен превышать max timeout задачи
        (default 90m > 60m timeout), иначе можно закрыть живую задачу."""
        try:
            from backend.core.executors.store import reap_stale_running
            n = await reap_stale_running(
                int(os.getenv("EXECUTOR_STALE_MINUTES", "90")))
            if n:
                logger.info("🪦 Reaped %d stale RUNNING executor tasks", n)
        except Exception as e:
            logger.debug("executor reaper failed: %s", e)

    async def _run_nightly_consolidation_adaptive(self):
        """
        Адаптивная ночная консолидация.

        Логика:
        1. Проверяет нагрузку на систему (LoadMonitor)
        2. Если система idle — запускает полный pipeline
        3. Если под нагрузкой — откладывает на 30 минут (до end_hour)
        4. Если до конца окна не удалось запустить — логирует пропуск

        Pipeline консолидации (NightlyConsolidationService):
        - Применение pending коррекций (TwoPhaseManager)
        - Temporal decay для связей (Hebbian Learning)
        - Консолидация памяти агентов (episodic → semantic)
        - Dream-inspired Memory Consolidation (саммаризация, кластеризация)
        - Обновление снапшотов (CompanySnapshot, PersonSnapshot)
        - Синтез мудрости (DIKW: паттерны → принципы)
        """
        if self._nightly_running:
            logger.warning("🌙 Nightly consolidation already running, skipping")
            return

        monitor = get_load_monitor()
        datetime.now(timezone.utc)
        self._nightly_postpone_count = 0

        # Адаптивная петля: пытаемся запустить пока в пределах ночного окна
        max_attempts = (NIGHT_PROCESSING_END_HOUR - NIGHT_PROCESSING_START_HOUR) * 2  # каждые 30 мин

        for attempt in range(max_attempts):
            current_hour = datetime.now(timezone.utc).hour

            # Проверяем, что мы всё ещё в ночном окне
            if current_hour >= NIGHT_PROCESSING_END_HOUR:
                logger.warning(
                    f"🌙 Nightly window expired ({NIGHT_PROCESSING_END_HOUR}:00 UTC). "
                    f"Postponed {self._nightly_postpone_count} times. Skipping this night."
                )
                return

            # Проверяем нагрузку
            if monitor.is_system_idle():
                logger.info(
                    f"🌙 System is idle (load: {monitor.get_load_last_hour()} req/h). "
                    f"Starting nightly consolidation (attempt {attempt + 1})..."
                )
                break
            else:
                self._nightly_postpone_count += 1
                logger.info(
                    f"🌙 System under load ({monitor.get_load_last_hour()} req/h > {LOAD_THRESHOLD}). "
                    f"Postponing consolidation for 30 min (attempt {attempt + 1}/{max_attempts})..."
                )
                await asyncio.sleep(1800)  # 30 минут
        else:
            logger.warning("🌙 Could not find idle window for nightly consolidation")
            return

        # Запускаем консолидацию
        self._nightly_running = True
        start_time = time.time()

        try:
            from backend.core.corrections.nightly_consolidation import run_nightly_consolidation

            result = await run_nightly_consolidation()

            elapsed = time.time() - start_time
            self._nightly_last_result = result
            self._nightly_last_run = datetime.now(timezone.utc).isoformat()

            errors_count = len(result.get("errors", []))
            logger.info(
                f"✅ Nightly consolidation completed in {elapsed:.1f}s: "
                f"users={result.get('users_processed', 0)}, "
                f"corrections={result.get('corrections_applied', 0)}, "
                f"edges_decayed={result.get('edges_decayed', 0)}, "
                f"memories={result.get('memories_consolidated', 0)}, "
                f"snapshots={result.get('snapshots_updated', 0)}, "
                f"wisdom={result.get('wisdom_principles_created', 0)}, "
                f"errors={errors_count}"
            )

            if errors_count > 0:
                for err in result["errors"][:5]:
                    logger.warning(f"  ⚠️ Error: {err}")

        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(f"❌ Nightly consolidation failed after {elapsed:.1f}s: {e}", exc_info=True)
            self._nightly_last_result = {"error": str(e), "elapsed_seconds": elapsed}
            self._nightly_last_run = datetime.now(timezone.utc).isoformat()
        finally:
            self._nightly_running = False

    def get_nightly_status(self) -> Dict[str, Any]:
        """
        Получить статус ночной консолидации.

        Returns:
            Информация: включена ли, когда последний запуск,
            результаты, текущий статус нагрузки.
        """
        monitor = get_load_monitor()
        return {
            "enabled": NIGHT_PROCESSING_ENABLED,
            "schedule": f"{NIGHT_PROCESSING_START_HOUR}:00-{NIGHT_PROCESSING_END_HOUR}:00 UTC",
            "is_running": self._nightly_running,
            "last_run": self._nightly_last_run,
            "last_result": self._nightly_last_result,
            "postpone_count": self._nightly_postpone_count,
            "load_monitor": monitor.get_stats(),
        }

    @property
    def is_running(self) -> bool:
        """Проверить, запущен ли планировщик"""
        return self._running and self.scheduler is not None


# Singleton instance
_scheduler: Optional[AutomationScheduler] = None


def get_scheduler() -> AutomationScheduler:
    """Получить singleton экземпляр планировщика"""
    global _scheduler
    if _scheduler is None:
        _scheduler = AutomationScheduler()
    return _scheduler


async def start_scheduler():
    """Запустить глобальный планировщик"""
    scheduler = get_scheduler()
    await scheduler.start()


async def stop_scheduler():
    """Остановить глобальный планировщик"""
    scheduler = get_scheduler()
    await scheduler.stop()


