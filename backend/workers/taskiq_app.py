# -*- coding: utf-8 -*-
"""
Taskiq Application - Фоновые задачи для Tessbrain.

Taskiq - современная асинхронная библиотека для задач.
Поддерживает Redis, RabbitMQ и in-memory брокеры.

Использование:
    # Запуск воркера
    taskiq worker backend.workers.taskiq_app:broker

    # Запуск scheduler
    taskiq scheduler backend.workers.taskiq_app:scheduler
"""

import functools
import logging
import os
from datetime import timedelta
from typing import Any, Dict, Optional

# Taskiq imports
try:
    from taskiq import InMemoryBroker, TaskiqEvents, TaskiqScheduler
    from taskiq_redis import ListQueueBroker, RedisAsyncResultBackend
    TASKIQ_AVAILABLE = True
except ImportError:
    TASKIQ_AVAILABLE = False
    logging.warning("⚠️ Taskiq не установлен. pip install taskiq taskiq-redis")


# ============================================
# Tenant scope helper (W4 wire-up)
# ============================================
# В HTTP-handler'ах tenant ставится TenantResolverMiddleware. В worker'ах
# контекста нет — caller обязан передать `tenant_id` как kwarg, а декоратор
# `_with_tenant_kwarg` выставит его в observability.tenant_context на время
# выполнения функции и сбросит после. Если tenant_id отсутствует/None —
# поведение прежнее (single-tenant / legacy).

def _with_tenant_kwarg(func):
    """Декоратор: принимает tenant_id kwarg и оборачивает в tenant-scope.

    Накладывается ПОД @broker.task — broker регистрирует уже обёрнутую функцию,
    так что таска видна с tenant_id-сигнатурой и tenant'у автоматически
    выставляется в начале выполнения.
    """
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        tenant_id = kwargs.get("tenant_id")
        if not tenant_id:
            return await func(*args, **kwargs)
        try:
            from backend.core.observability.tenant_context import (
                reset_current_tenant,
                set_current_tenant,
            )
        except Exception:
            return await func(*args, **kwargs)
        token = set_current_tenant(tenant_id)
        try:
            return await func(*args, **kwargs)
        finally:
            reset_current_tenant(token)
    return wrapper

logger = logging.getLogger(__name__)


async def _drain_reactive_for_user(user_id: str, max_signals: int = 50) -> Dict[str, Any]:
    """Inline drain одного юзера (Phase D post-meeting hook). Best-effort.

    Не taskiq-таска — вызывается прямо внутри process_meeting_task после
    того как load участника упал (вышел из переговорной).
    """
    try:
        from ..core.reactive.drain_runner import run_drain
        return await run_drain(user_id=user_id, max_signals=max_signals)
    except Exception as exc:
        logger.debug("drain for user %s failed: %s", user_id, exc)
        return {"delivered": 0}


# Конфигурация Redis
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Создаём брокер
if TASKIQ_AVAILABLE:
    # Пробуем Redis, fallback на InMemory
    try:
        broker = ListQueueBroker(url=REDIS_URL)
        result_backend = RedisAsyncResultBackend(redis_url=REDIS_URL)
        broker = broker.with_result_backend(result_backend)
        logger.info(f"✅ Taskiq broker: Redis ({REDIS_URL})")
    except Exception as e:
        logger.warning(f"⚠️ Redis недоступен, используем InMemory broker: {e}")
        broker = InMemoryBroker()

    # Создаём планировщик
    scheduler = TaskiqScheduler(broker=broker, sources=[])
else:
    broker = None
    scheduler = None


# ============================================
# ЗАДАЧИ ОБРАБОТКИ ВСТРЕЧ
# ============================================

if TASKIQ_AVAILABLE:

    @broker.task
    @_with_tenant_kwarg
    async def process_meeting_task(
        meeting_id: str,
        user_id: str,
        project_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Фоновая обработка встречи.

        Выполняет:
        1. Загрузка транскрипта из Supabase
        2. Извлечение сущностей через CaptureOrchestrator
        3. Обновление графа
        4. Обновление векторного индекса
        5. Обновление Company Snapshot

        tenant_id (W4): при наличии — устанавливается в
        observability.tenant_context на время выполнения через декоратор
        `_with_tenant_kwarg`. Caller (HTTP-handler) обязан передавать
        tenant_id из текущего контекста при kicker'е задачи.
        """
        logger.info(f"📋 [TASK] Processing meeting: {meeting_id}")

        try:
            # Импорты внутри задачи для изоляции
            from ..core.capture.orchestrator import CaptureOrchestrator
            from ..core.llm.router import get_llm_router
            from ..core.store.graph_builder import GraphBuilder
            from ..core.store.vector_indexer import VectorIndexer
            from ..db.supabase_client import get_supabase_client

            # Инициализация компонентов
            supabase = get_supabase_client()
            llm = get_llm_router()
            graph_builder = GraphBuilder(graph_storage_path="data/tessent_brain_graph.json")
            vector_indexer = VectorIndexer()  # модель из config.py

            # Загружаем транскрипт
            transcript = await supabase.get_meeting_transcript(meeting_id)
            if not transcript:
                return {"status": "error", "message": "Transcript not found"}

            # Создаём оркестратор
            orchestrator = CaptureOrchestrator(
                llm_router=llm,
                graph_builder=graph_builder,
                vector_indexer=vector_indexer
            )

            # Обрабатываем встречу
            result = await orchestrator.process_meeting(
                transcript=transcript,
                meeting_id=meeting_id,
                meeting_context={"project_id": project_id, "user_id": user_id}
            )

            # Сохраняем граф
            await graph_builder.save_graph()

            logger.info(f"✅ [TASK] Meeting {meeting_id} processed successfully")

            # Авто-свежесть снапшотов: воркер — отдельный процесс, поэтому
            # инвалидация идёт через персистентный маркер .needs_regen в
            # per-user каталоге; API-процесс увидит его на следующем read.
            try:
                from ..core.sleep.enhanced_snapshot import invalidate_snapshot_cache
                invalidate_snapshot_cache(user_id)
            except Exception:
                logger.warning("snapshot invalidate after meeting task skipped",
                               exc_info=True)

            # Phase A.4: сохраняем capture-observations для PersonaAggregator
            # ДО rebuild Persona, чтобы агрегация увидела свежие данные.
            # capture-orchestrator кладёт выходы агентов в result['agent_results']
            # = {name: list[dict]} (ключ 'agents' был ошибкой — цикл никогда не
            # срабатывал). Пишем ТОЛЬКО типы, у которых есть читатель в
            # агрегаторе: остальное — мусорные строки под владельцем без
            # потребителя, а cogni_profiles к тому же требует атрибуции по
            # людям — их пишет мост core/cogni/observe.py из knowledge_sync.
            _CONSUMED_OBS_AGENTS = {"communication_style_agent"}
            try:
                from ..core.persona.observations import save_observation
                agents_outputs = (result.get("agent_results")
                                  or result.get("agents")) if isinstance(result, dict) else None
                if isinstance(agents_outputs, dict):
                    saved = 0
                    for agent_name, agent_payload in agents_outputs.items():
                        if str(agent_name) not in _CONSUMED_OBS_AGENTS:
                            continue
                        if isinstance(agent_payload, list):
                            if not agent_payload:
                                continue
                            agent_payload = {"items": agent_payload}
                        if not isinstance(agent_payload, dict):
                            continue
                        ok = await save_observation(
                            user_id=user_id,
                            meeting_id=meeting_id,
                            agent_type=str(agent_name),
                            payload=agent_payload,
                            tenant_id=tenant_id,
                            confidence=float(agent_payload.get("confidence", 0.8)),
                            source_type="capture_agent",
                        )
                        if ok:
                            saved += 1
                    if saved:
                        logger.info(
                            "✅ [TASK] Saved %d persona_observations for meeting %s",
                            saved, meeting_id,
                        )
            except Exception as obs_exc:
                logger.warning(
                    "save persona_observations after meeting %s failed: %s",
                    meeting_id, obs_exc,
                )

            # Phase A.3: после успешной обработки встречи — пересобираем
            # Persona владельца. Это убирает необходимость ?rebuild=true
            # в /persona/me для UI; cache обновится сам через ~5 сек.
            # never-raise: ошибка Persona не должна откатить успех meeting'а.
            try:
                from ..core.persona.aggregator import get_persona_aggregator
                agg = get_persona_aggregator()
                await agg.build_for_user(
                    user_id=user_id,
                    tenant_id=tenant_id,
                    llm_router=llm,
                )
                logger.info(f"✅ [TASK] Persona rebuilt for user {user_id} after meeting {meeting_id}")
            except Exception as persona_exc:
                logger.warning(
                    "Persona auto-rebuild after meeting %s failed: %s",
                    meeting_id, persona_exc,
                )

            # Опыт компании (BWE): извлечь решения из этой встречи, а из
            # ретроспектив — ещё и исходы. Раньше слой наполнялся ТОЛЬКО
            # ручным HTTP-вызовом, которого не делал ни фронтенд, ни бот, —
            # то есть «система запоминает, чем закончилось» было обещанием
            # при пустой таблице. Ретроспективность — по заголовку, той же
            # эвристикой, что /wisdom/sync. never-raise: сбой опыта не
            # откатывает успех встречи. Выключается BWE_AUTO_INGEST=off.
            try:
                import os as _os
                if _os.getenv("BWE_AUTO_INGEST", "on").strip().lower() in (
                        "1", "on", "true", "yes"):
                    from ..core.wisdom.wisdom_engine import get_wisdom_engine
                    _title = ""
                    try:
                        _md = await supabase.get_meeting_details(
                            meeting_id, include_transcript=False,
                            user_id=user_id)
                        _title = str((_md or {}).get("title") or "").lower()
                    except Exception:
                        logger.debug("BWE: meeting title lookup skipped",
                                     exc_info=True)
                    _is_retro = any(k in _title for k in (
                        "ретро", "retrospect", "разбор", "постмортем"))
                    _we = await get_wisdom_engine()
                    _bwe = await _we.process_meeting(
                        user_id=user_id,
                        transcript=transcript,
                        meeting_id=meeting_id,
                        meeting_source="auto_ingest",
                        is_retrospective=_is_retro,
                    )
                    logger.info(
                        "✅ [TASK] BWE: %s decisions, %s outcomes (retro=%s) "
                        "for meeting %s",
                        getattr(_bwe, "decisions_extracted", "?"),
                        getattr(_bwe, "outcomes_linked", "?"),
                        _is_retro, meeting_id)
            except Exception as bwe_exc:
                logger.warning("BWE auto-ingest after meeting %s failed: %s",
                               meeting_id, bwe_exc)

            # Push шины данных: подписанным потребителям уходит уведомление
            # «обработана встреча» — БЕЗ данных, только счётчики. За самими
            # данными потребитель приходит через конвейер выдачи (срез,
            # редакция, аудит). never-raise: пуш не откатывает встречу.
            # Выключается DATA_BUS_WEBHOOKS=off.
            try:
                import os as _os
                if _os.getenv("DATA_BUS_WEBHOOKS", "on").strip().lower() in (
                        "1", "on", "true", "yes"):
                    from ..core.data_bus.webhook_push import notify as _wh_notify
                    _ents = result.get("entities", {}) or {}
                    _wh_res = await _wh_notify(
                        user_id, "meeting_processed",
                        {
                            "tasks": len(_ents.get("tasks", []) or []),
                            "decisions": len(_ents.get("decisions", []) or []),
                            "participants": len(_ents.get("participants", []) or []),
                        })
                    if _wh_res.get("sent") or _wh_res.get("failed"):
                        logger.info("📣 [TASK] Webhook push: %s", _wh_res)
            except Exception as wh_exc:
                logger.warning("webhook push after meeting %s failed: %s",
                               meeting_id, wh_exc)

            # Phase B: Coffee scenario — готовим role-typed артефакты для
            # всех участников встречи. SLA target ≤5 мин от завершения
            # встречи до доставки в Telegram/email.
            # never-raise: если pipeline ляжет, meeting всё равно успешный.
            # Audit-фикс: инициализируем до try, чтобы Phase D drain-блок ниже
            # не падал в NameError если Coffee упал ДО присваивания.
            meeting_data_for_coffee: Dict[str, Any] = {}
            try:
                from ..core.coffee import prepare_coffee_for_meeting
                # Собираем meeting_data из result extraction +
                # transcript уже загружен в transcript variable
                meeting_data_for_coffee = {
                    "id": meeting_id,
                    "title": (result.get("summary", {}) or {}).get("title") or "встречи",
                    "transcript": transcript,
                    "summary": (result.get("summary", {}) or {}).get("text"),
                    "participants": (result.get("entities", {}) or {}).get("participants", []),
                    "tasks": (result.get("entities", {}) or {}).get("tasks", []),
                    "decisions": (result.get("entities", {}) or {}).get("decisions", []),
                    "key_topics": (result.get("entities", {}) or {}).get("topics", []),
                    "opinions": (result.get("entities", {}) or {}).get("opinions", []),
                }
                coffee_result = await prepare_coffee_for_meeting(
                    meeting_id=meeting_id,
                    meeting_data=meeting_data_for_coffee,
                    deliver=True,
                    llm_router=llm,
                )
                logger.info(
                    "✅ [TASK] Coffee: %d artifacts, %d delivered, %d pending (%dms)",
                    len(coffee_result.artifacts),
                    coffee_result.success_count,
                    coffee_result.pending_count,
                    coffee_result.duration_ms,
                )
            except Exception as coffee_exc:
                logger.warning(
                    "Coffee scenario for meeting %s failed: %s",
                    meeting_id, coffee_exc,
                )

            # Phase D: meeting_ended — реальный момент когда load участников
            # падает (вышли из переговорной). Инвалидируем load-cache и
            # drain'им их parked сигналы. never-raise.
            try:
                from ..core.reactive.cognitive_load import invalidate_load_cache
                participants = meeting_data_for_coffee.get("participants") or []
                participant_ids = []
                for p in participants:
                    if isinstance(p, dict):
                        pid = p.get("user_id") or p.get("id")
                        if pid:
                            participant_ids.append(str(pid))
                drained = 0
                for pid in set(participant_ids):
                    invalidate_load_cache(pid)
                    res = await _drain_reactive_for_user(pid)
                    drained += res.get("delivered", 0)
                if participant_ids:
                    logger.info(
                        "✅ [TASK] Reactive post-meeting drain: %d signals delivered "
                        "across %d participants",
                        drained, len(set(participant_ids)),
                    )
            except Exception as react_exc:
                logger.warning(
                    "Reactive post-meeting drain for %s failed: %s",
                    meeting_id, react_exc,
                )

            return {
                "status": "completed",
                "meeting_id": meeting_id,
                "entities_extracted": result.get("summary", {}).get("total_entities", 0),
                "processing_time": result.get("processing_time_seconds", 0)
            }

        except Exception as e:
            logger.error(f"❌ [TASK] Error processing meeting {meeting_id}: {e}")
            return {"status": "error", "message": str(e)}


    @broker.task
    @_with_tenant_kwarg
    async def enrich_graph_task(
        user_id: str,
        limit: int = 50,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Обогащение графа из всех встреч пользователя.
        """
        logger.info(f"📋 [TASK] Enriching graph for user: {user_id}")

        try:
            from ..core.capture.orchestrator import CaptureOrchestrator
            from ..core.llm.router import get_llm_router
            from ..core.store.graph_builder import GraphBuilder
            from ..core.store.vector_indexer import VectorIndexer
            from ..db.supabase_client import get_supabase_client

            supabase = get_supabase_client()
            llm = get_llm_router()
            graph_builder = GraphBuilder(graph_storage_path="data/tessent_brain_graph.json")
            vector_indexer = VectorIndexer()  # модель из config.py

            # Загружаем встречи пользователя
            meetings = await supabase.get_user_meetings(user_id, limit=limit)

            orchestrator = CaptureOrchestrator(
                llm_router=llm,
                graph_builder=graph_builder,
                vector_indexer=vector_indexer
            )

            processed = 0
            errors = 0

            for meeting in meetings:
                meeting_id = meeting.get("id")
                try:
                    transcript = await supabase.get_meeting_transcript(meeting_id)
                    if transcript:
                        await orchestrator.process_meeting(
                            transcript=transcript,
                            meeting_id=meeting_id,
                            meeting_context={"user_id": user_id}
                        )
                        processed += 1
                except Exception as e:
                    logger.warning(f"⚠️ Error processing meeting {meeting_id}: {e}")
                    errors += 1

            await graph_builder.save_graph()

            logger.info(f"✅ [TASK] Graph enriched: {processed} meetings, {errors} errors")

            return {
                "status": "completed",
                "user_id": user_id,
                "meetings_processed": processed,
                "errors": errors
            }

        except Exception as e:
            logger.error(f"❌ [TASK] Error enriching graph: {e}")
            return {"status": "error", "message": str(e)}


# ============================================
# ЗАДАЧИ ENTITY RESOLUTION
# ============================================

if TASKIQ_AVAILABLE:

    @broker.task
    @_with_tenant_kwarg
    async def resolve_entities_task(tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Фоновое разрешение дубликатов сущностей.
        """
        logger.info("📋 [TASK] Running entity resolution")

        try:
            from ..core.llm.router import get_llm_router
            from ..core.store.entity_resolver import EntityResolver
            from ..core.store.graph_builder import GraphBuilder

            graph_builder = GraphBuilder(graph_storage_path="data/tessent_brain_graph.json")
            llm = get_llm_router()

            resolver = EntityResolver(graph_builder=graph_builder, llm_router=llm)

            # Находим и мержим дубликаты
            duplicates = await resolver.find_all_duplicates(threshold=0.7)
            merged_count = 0

            for dup in duplicates:
                if dup.get("confidence", 0) > 0.85:
                    await resolver.merge_entities(
                        dup["entity_a_id"],
                        dup["entity_b_id"]
                    )
                    merged_count += 1

            await graph_builder.save_graph()

            logger.info(f"✅ [TASK] Entity resolution: {len(duplicates)} found, {merged_count} merged")

            return {
                "status": "completed",
                "duplicates_found": len(duplicates),
                "merged": merged_count
            }

        except Exception as e:
            logger.error(f"❌ [TASK] Entity resolution error: {e}")
            return {"status": "error", "message": str(e)}


# ============================================
# ЗАДАЧИ SNAPSHOT GENERATION
# ============================================

if TASKIQ_AVAILABLE:

    @broker.task
    @_with_tenant_kwarg
    async def generate_snapshot_task(tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Генерация Company Snapshot.
        """
        logger.info("📋 [TASK] Generating company snapshot")

        try:
            from ..core.llm.router import get_llm_router
            from ..core.sleep.company_snapshot_agent import get_company_snapshot_agent
            from ..core.store.graph_builder import GraphBuilder

            llm = get_llm_router()
            graph_builder = GraphBuilder(graph_storage_path="data/tessent_brain_graph.json")

            snapshot_agent = get_company_snapshot_agent(llm, graph_builder)

            snapshot = await snapshot_agent.generate_snapshot()

            # Сохраняем snapshot
            import json
            from pathlib import Path

            snapshot_dir = Path("data/snapshots")
            snapshot_dir.mkdir(parents=True, exist_ok=True)

            with open(snapshot_dir / "current_snapshot.json", "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)

            logger.info("✅ [TASK] Company snapshot generated")

            return {
                "status": "completed",
                "snapshot_size": len(json.dumps(snapshot))
            }

        except Exception as e:
            logger.error(f"❌ [TASK] Snapshot generation error: {e}")
            return {"status": "error", "message": str(e)}


# ============================================
# ЗАДАЧИ НОЧНОЙ ОБРАБОТКИ
# ============================================

if TASKIQ_AVAILABLE:

    @broker.task
    @_with_tenant_kwarg
    async def night_processing_task(tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Ночная обработка данных.

        Выполняет:
        1. Entity Resolution
        2. Decay Management
        3. Snapshot Generation
        4. Synthesis Analysis
        """
        logger.info("🌙 [TASK] Starting night processing")

        results = {
            "entity_resolution": None,
            "snapshot": None,
            "decay": None
        }

        try:
            # Audit-фикс (RLS foundation): forward tenant_id в сабтаски, чтобы
            # их _with_tenant_kwarg выставлял contextvar. Раньше sub-tasks
            # запускались БЕЗ tenant'а даже когда night_processing_task его
            # получал → под strict-RLS запросы вернули бы zero rows. tenant_id
            # тут это kwarg, который декоратор уже распознал на нашем уровне.
            # 1. Entity Resolution
            results["entity_resolution"] = await resolve_entities_task(tenant_id=tenant_id)

            # 2. Snapshot Generation
            results["snapshot"] = await generate_snapshot_task(tenant_id=tenant_id)

            # 3. Decay Management
            results["decay"] = await run_decay_task(tenant_id=tenant_id)

            logger.info("✅ [TASK] Night processing completed")

            return {
                "status": "completed",
                "results": results
            }

        except Exception as e:
            logger.error(f"❌ [TASK] Night processing error: {e}")
            return {"status": "error", "message": str(e), "partial_results": results}


    @broker.task
    @_with_tenant_kwarg
    async def run_decay_task(tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Управление устареванием данных.
        """
        logger.info("📋 [TASK] Running decay management")

        try:
            from ..core.sleep.decay_manager import DecayManager
            from ..core.store.graph_builder import GraphBuilder

            graph_builder = GraphBuilder(graph_storage_path="data/tessent_brain_graph.json")
            decay_manager = DecayManager(graph_builder=graph_builder)

            result = await decay_manager.apply_decay()

            await graph_builder.save_graph()

            logger.info(f"✅ [TASK] Decay applied to {result.get('nodes_updated', 0)} nodes")

            return {
                "status": "completed",
                "nodes_updated": result.get("nodes_updated", 0)
            }

        except Exception as e:
            logger.error(f"❌ [TASK] Decay error: {e}")
            return {"status": "error", "message": str(e)}


# ============================================
# ЗАДАЧИ ДОКУМЕНТОВ
# ============================================

if TASKIQ_AVAILABLE:

    @broker.task
    @_with_tenant_kwarg
    async def process_document_task(
        file_path: str,
        project_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Фоновая обработка документа.
        """
        logger.info(f"📋 [TASK] Processing document: {file_path}")

        try:
            from ..core.capture.document_processor import DocumentProcessor
            from ..core.llm.router import get_llm_router
            from ..core.store.graph_builder import GraphBuilder
            from ..core.store.vector_indexer import VectorIndexer

            llm = get_llm_router()
            graph_builder = GraphBuilder(graph_storage_path="data/tessent_brain_graph.json")
            vector_indexer = VectorIndexer()  # модель из config.py

            processor = DocumentProcessor(
                graph_builder=graph_builder,
                vector_indexer=vector_indexer,
                llm_client=llm,
                use_semantic_chunking=True
            )

            result = await processor.process_and_index(
                file_path=file_path,
                project_id=project_id
            )

            await graph_builder.save_graph()

            logger.info(f"✅ [TASK] Document processed: {result.get('chunks_indexed', 0)} chunks")

            return {
                "status": "completed",
                **result
            }

        except Exception as e:
            logger.error(f"❌ [TASK] Document processing error: {e}")
            return {"status": "error", "message": str(e)}


# ============================================
# PERSONA AGGREGATION (Phase A.3)
# ============================================
# Hook в meeting-ingest pipeline: после успешной обработки встречи
# дёргаем persona-rebuild для user'а владельца. Это убирает необходимость
# ?rebuild=true для UI — Persona всегда свежая.

if TASKIQ_AVAILABLE:

    @broker.task
    @_with_tenant_kwarg
    async def rebuild_persona_task(
        user_id: str,
        with_llm_enrichment: bool = False,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Пересобрать Persona для user_id из свежих meetings.

        Best-effort: ошибки логируются и возвращаются в result, но не
        пробрасываются — taskiq не будет ретраить (Persona не критична).

        Args:
            user_id: founder id
            with_llm_enrichment: если True — дёрнуть CommunicationStyleAgent
                на 3 свежих транскриптах (дороже, ~10-20s + ~3-5k токенов).
                По умолчанию False — только метаданные встреч, мгновенно.
            tenant_id: для RLS совместимости при upsert
        """
        logger.info(f"📋 [TASK] Rebuild Persona for user {user_id} (llm={with_llm_enrichment})")
        try:
            from ..core.persona.aggregator import get_persona_aggregator

            llm_router = None
            if with_llm_enrichment:
                # Lazy import чтобы не тащить router если LLM не нужен
                try:
                    from ..core.llm.router import LLMRouter
                    llm_router = LLMRouter()
                except Exception as exc:
                    logger.warning(
                        "rebuild_persona: LLMRouter unavailable, skipping enrichment: %s",
                        exc,
                    )

            aggregator = get_persona_aggregator()
            persona = await aggregator.build_for_user(
                user_id=user_id,
                tenant_id=tenant_id,
                llm_router=llm_router,
            )
            return {
                "status": "completed",
                "user_id": user_id,
                "source_meetings": persona.source_meetings_count,
                "confidence": persona.confidence,
                "llm_enriched": with_llm_enrichment and llm_router is not None,
            }
        except Exception as e:
            logger.error(f"❌ [TASK] Persona rebuild error for {user_id}: {e}")
            return {"status": "error", "user_id": user_id, "message": str(e)}


if TASKIQ_AVAILABLE:

    @broker.task
    @_with_tenant_kwarg
    async def reactive_drain_task(
        user_id: Optional[str] = None,
        max_signals: int = 100,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Phase C: drain reactive_signals очередь.

        Запускается периодически (cron каждые 10 мин в production) или
        triggered после события которое могло снизить load (meeting ended,
        user opened Telegram). Best-effort — никогда не raise.

        Args:
            user_id: если задан — drain только этого юзера; иначе все
                с queued сигналами (cap by max_signals).
            max_signals: лимит на этот вызов
        """
        logger.info(
            f"📋 [TASK] Reactive drain user={user_id or 'ALL'} max={max_signals}"
        )
        try:
            from ..core.reactive.drain_runner import run_drain
            stats = await run_drain(user_id=user_id, max_signals=max_signals)
            logger.info(
                f"✅ [TASK] Reactive drain: checked={stats.get('checked', 0)} "
                f"delivered={stats.get('delivered', 0)} deferred={stats.get('deferred', 0)}"
            )
            return {"status": "completed", **stats}
        except Exception as exc:
            logger.error(f"❌ [TASK] Reactive drain error: {exc}")
            return {"status": "error", "message": str(exc)}


    @broker.task
    @_with_tenant_kwarg
    async def recompute_calibration_task(
        user_id: Optional[str] = None,
        since_days: int = 30,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Phase E: пересчитать per-user калибровку из feedback.

        Cron (например раз в сутки) или triggered. Если user_id задан —
        только он; иначе все юзеры с feedback за окно. Best-effort.
        """
        logger.info(
            f"📋 [TASK] Recompute calibration user={user_id or 'ALL'} "
            f"window={since_days}d"
        )
        try:
            if user_id:
                from ..core.reactive.calibration import recompute_calibration
                prof = await recompute_calibration(
                    user_id=user_id, tenant_id=tenant_id, since_days=since_days,
                )
                logger.info(
                    "✅ [TASK] Calibration user=%s delta=%.1f samples=%d applied=%s",
                    user_id, prof.threshold_delta, prof.sample_count, prof.is_applied,
                )
                return {"status": "completed", "user": user_id, **prof.to_dict()}
            from ..core.reactive.calibration import recompute_all_calibrations
            stats = await recompute_all_calibrations(since_days=since_days)
            logger.info(
                "✅ [TASK] Calibration recomputed: users=%d applied=%d",
                stats.get("users", 0), stats.get("applied", 0),
            )
            return {"status": "completed", **stats}
        except Exception as exc:
            logger.error(f"❌ [TASK] Calibration recompute error: {exc}")
            return {"status": "error", "message": str(exc)}


    @broker.task
    @_with_tenant_kwarg
    async def deliver_night_insights_task(
        user_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Phase N: доставить ночные инсайты через reactive learning-loop.

        Обычно это шаг #9 в NightlyConsolidationService (per-user, cron 2:00 UTC),
        но этот task даёт явный триггер для одного юзера (ops/тест/догфудинг).
        Если user_id не задан — итерируется по всем юзерам с графом. Best-effort.
        """
        logger.info(f"📋 [TASK] Deliver night insights user={user_id or 'ALL'}")
        try:
            from ..core.corrections.nightly_consolidation import (
                NightlyConsolidationService,
            )
            svc = NightlyConsolidationService()
            if user_id:
                stats = await svc._deliver_night_insights(user_id)
                return {"status": "completed", "user": user_id, **stats}
            total = {"submitted": 0, "skipped": 0, "users": 0}
            for uid in svc._get_users():
                s = await svc._deliver_night_insights(uid)
                total["submitted"] += s.get("submitted", 0)
                total["skipped"] += s.get("skipped", 0)
                total["users"] += 1
            logger.info(
                "✅ [TASK] Night insights: users=%d submitted=%d skipped=%d",
                total["users"], total["submitted"], total["skipped"],
            )
            return {"status": "completed", **total}
        except Exception as exc:
            logger.error(f"❌ [TASK] Night insights error: {exc}")
            return {"status": "error", "message": str(exc)}


# ============================================
# СОБЫТИЯ ЖИЗНЕННОГО ЦИКЛА
# ============================================

if TASKIQ_AVAILABLE:

    @broker.on_event(TaskiqEvents.WORKER_STARTUP)
    async def on_startup(state):
        """Инициализация при запуске воркера."""
        logger.info("🚀 Taskiq worker starting...")

        # Здесь можно инициализировать глобальные ресурсы
        # Например, подключения к базам данных

        logger.info("✅ Taskiq worker ready")


    @broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
    async def on_shutdown(state):
        """Очистка при остановке воркера."""
        logger.info("🛑 Taskiq worker shutting down...")

        # Закрываем соединения

        logger.info("✅ Taskiq worker stopped")


# ============================================
# УТИЛИТЫ
# ============================================

def _current_tenant_or_none() -> Optional[str]:
    """Прочитать tenant_id из observability.tenant_context (или None)."""
    try:
        from backend.core.observability.tenant_context import get_current_tenant
        return get_current_tenant()
    except Exception:
        return None


async def schedule_meeting_processing(
    meeting_id: str,
    user_id: str,
    delay_seconds: int = 0,
    tenant_id: Optional[str] = None,
):
    """Планирует обработку встречи.

    tenant_id — если не передан явно, берётся из текущего HTTP-контекста.
    Worker'у он попадёт как kwarg, и `_with_tenant_kwarg` переустановит
    tenant в свой contextvar на время выполнения.
    """
    if tenant_id is None:
        tenant_id = _current_tenant_or_none()

    if not TASKIQ_AVAILABLE:
        logger.warning("⚠️ Taskiq not available, running synchronously")
        return await process_meeting_task(meeting_id, user_id, tenant_id=tenant_id)

    kwargs = {"meeting_id": meeting_id, "user_id": user_id, "tenant_id": tenant_id}
    if delay_seconds > 0:
        task = await process_meeting_task.kicker().with_delay(
            timedelta(seconds=delay_seconds)
        ).kiq(**kwargs)
    else:
        task = await process_meeting_task.kiq(**kwargs)

    logger.info(f"📋 Meeting {meeting_id} scheduled for processing (task_id={task.task_id})")
    return task


async def schedule_document_processing(
    file_path: str,
    project_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
):
    """Планирует обработку документа."""
    if tenant_id is None:
        tenant_id = _current_tenant_or_none()

    if not TASKIQ_AVAILABLE:
        logger.warning("⚠️ Taskiq not available, running synchronously")
        return await process_document_task(file_path, project_id, tenant_id=tenant_id)

    task = await process_document_task.kiq(
        file_path=file_path, project_id=project_id, tenant_id=tenant_id,
    )

    logger.info(f"📋 Document {file_path} scheduled for processing (task_id={task.task_id})")
    return task


async def schedule_persona_rebuild(
    user_id: str,
    with_llm_enrichment: bool = False,
    delay_seconds: int = 0,
    tenant_id: Optional[str] = None,
):
    """Phase A.3: запланировать пересборку Persona для user_id.

    Вызывается после ингеста новой встречи (hook в meeting pipeline) или
    из admin UI ("refresh profile"). Без taskiq — выполняется синхронно
    (не блокируем HTTP-handler, но в тестах это удобно).

    `with_llm_enrichment=True` — для тех редких случаев когда нужен
    realtime communication_style анализ; обычно False (быстро, бесплатно).

    `delay_seconds` — небольшая задержка чтобы дать meeting-pipeline
    дописать данные в Supabase перед чтением.
    """
    if tenant_id is None:
        tenant_id = _current_tenant_or_none()

    if not TASKIQ_AVAILABLE:
        logger.warning("⚠️ Taskiq not available, running persona rebuild synchronously")
        return await rebuild_persona_task(
            user_id, with_llm_enrichment=with_llm_enrichment, tenant_id=tenant_id,
        )

    kwargs = {
        "user_id": user_id,
        "with_llm_enrichment": with_llm_enrichment,
        "tenant_id": tenant_id,
    }
    if delay_seconds > 0:
        task = await rebuild_persona_task.kicker().with_delay(
            timedelta(seconds=delay_seconds)
        ).kiq(**kwargs)
    else:
        task = await rebuild_persona_task.kiq(**kwargs)

    logger.info(f"📋 Persona rebuild scheduled for user {user_id} (task_id={task.task_id})")
    return task


async def schedule_night_processing(tenant_id: Optional[str] = None):
    """Планирует ночную обработку.

    NB: night_processing_task сама итерируется по всем пользователям
    через NightlyConsolidationService, поэтому `tenant_id` обычно не нужен —
    джоба должна обработать ВСЕ tenant'ы (admin-режим). Параметр оставлен
    для будущего per-tenant scheduling, если понадобится.
    """
    if not TASKIQ_AVAILABLE:
        logger.warning("⚠️ Taskiq not available, running synchronously")
        return await night_processing_task(tenant_id=tenant_id)

    task = await night_processing_task.kiq(tenant_id=tenant_id)

    logger.info(f"🌙 Night processing scheduled (task_id={task.task_id})")
    return task




# ============================================
# АНАЛИТИЧЕСКИЙ РАЗБОР (Analysis Engine)
# ============================================

if TASKIQ_AVAILABLE:

    @broker.task
    async def analysis_run_task(run_id: str) -> Dict[str, Any]:
        """Фоновый прогон аналитического разбора (Analysis Engine).

        Durable-канал для длинных разборов (100-страничные документы =
        минуты). HTTP-handler кидает задачу через .kiq(run_id) и сразу
        возвращает pending; клиент поллит GET /analysis/runs/{id}.

        Сам dispatch_run never-raises (run_pipeline ловит ошибки и пишет
        status=failed). Здесь — тонкая обёртка для taskiq.
        """
        logger.info(f"📊 [TASK] Analysis run: {run_id}")
        try:
            from ..core.analysis.run_engine import dispatch_run
            await dispatch_run(run_id)
            return {"status": "completed", "run_id": run_id}
        except Exception as e:
            logger.error(f"❌ Analysis run {run_id} failed: {e}")
            return {"status": "error", "run_id": run_id, "error": str(e)}
