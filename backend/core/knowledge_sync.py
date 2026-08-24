# -*- coding: utf-8 -*-
"""
TESSENT BRAIN - Knowledge Sync Core
Ядро синхронизации знаний из встреч в граф знаний

Включает:
- Обработку встреч через CaptureOrchestrator
- Сохранение в граф знаний (GraphBuilder)
- Индексацию в векторную базу (VectorIndexer)
- Entity Resolution для дедупликации
- Temporal Tracking для версионирования и timeline
"""
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

# Порядок решений во времени: чистый stdlib, тестируется без тяжёлых
# зависимостей этого модуля.
from backend.core.temporal.ordering import date_key as _date_key
from backend.core.temporal.ordering import decision_order as _decision_order
from backend.core.ontology.money_units import parse_money
from backend.core.store.entity_resolver import EntityResolver

logger = logging.getLogger(__name__)

# Idempotency / org-promote helpers (см. backend/core/ingest/README in __init__.py).
# Импорт на уровне модуля, чтобы не было раннер-time стоимости в горячем цикле.
from backend.core.ingest import (
    dedup as _ingest_dedup,
    meeting_promote as _ingest_promote,
    meeting_upsert as _ingest_upsert,
)
from backend.core.ingest.membership import get_org_for_user as _get_org_for_user


async def process_meetings_for_subscription(
    subscription_id: str,
    user_id: str,
    project_id: Optional[str] = None,
    folder_id: Optional[str] = None,
    include_subfolders: bool = True,
    force: bool = False,
    model_tier: str = "standard",  # "standard" или "premium"
    progress_cb: Optional[Callable[[Dict[str, Any]], Any]] = None,
    reprocess_older_than: Optional[str] = None,
    max_process: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Обработать встречи для подписки на синхронизацию знаний.

    Args:
        subscription_id: ID подписки
        user_id: ID пользователя
        project_id: ID проекта (опционально)
        folder_id: ID папки (опционально)
        model_tier: Уровень модели - "standard" (gemini-flash-lite-latest) или "premium" (gemini-flash-latest)

    Returns:
        Статистика обработки
    """
    from backend.core.automations.event_bus import EventType, get_event_bus
    from backend.core.llm.usage_tracker import UsageContext
    from backend.db.supabase_client import get_supabase_client

    # Tenant-scope на ВСЮ обработку: новые узлы графа должны нести
    # tenant_id = org_id (если в org) ИЛИ user_id (solo). Без этого solo-
    # юзеры писались с tenant_id=NULL и видели данные друг друга. Writers
    # (create_node/merge) берут tenant из get_current_tenant() как fallback.
    from backend.core.observability.tenant_context import (
        reset_current_tenant, set_current_tenant,
    )
    _tenant_scope = _get_org_for_user(user_id) or user_id
    _tenant_token = set_current_tenant(_tenant_scope) if _tenant_scope else None

    supabase = get_supabase_client()
    stats = {
        "processed": 0,
        "skipped": 0,
        "errors": 0,
        "entities_created": 0,
        "relationships_created": 0
    }

    def _emit_progress(total: int, current_title: str = "", phase: str = "running") -> None:
        """Толкнуть снимок прогресса в callback (best-effort, никогда не роняет
        обработку). done = processed+skipped+errors — сколько встреч уже прошли
        проверку (любым исходом); remaining = total-done."""
        if progress_cb is None:
            return
        done = stats["processed"] + stats["skipped"] + stats["errors"]
        try:
            progress_cb({
                "total": total,
                "done": done,
                "processed": stats["processed"],
                "skipped": stats["skipped"],
                "errors": stats["errors"],
                "remaining": max(0, total - done),
                "current_title": current_title,
                "phase": phase,
            })
        except Exception:
            logger.debug("progress_cb failed", exc_info=True)

    try:
        # Получаем список встреч для обработки
        params = {
            "user_id": f"eq.{user_id}",
            "select": "*"
        }

        if project_id:
            params["project_id"] = f"eq.{project_id}"
        if folder_id:
            if include_subfolders:
                folder_ids = await _get_folder_tree_ids(supabase=supabase, user_id=user_id, root_folder_id=folder_id)
                # PostgREST in filter: in.(id1,id2,...)
                if folder_ids:
                    params["folder_id"] = f"in.({','.join(folder_ids)})"
                else:
                    params["folder_id"] = f"eq.{folder_id}"
            else:
                params["folder_id"] = f"eq.{folder_id}"

        meetings = await supabase._request("GET", "/rest/v1/meetings", params=params)

        if not meetings:
            logger.info(f"No meetings found for subscription {subscription_id}")
            _emit_progress(0, phase="completed")
            return stats

        _total_meetings = len(meetings)
        _emit_progress(_total_meetings, phase="starting")

        # Получаем уже обработанные встречи (если не force).
        # В force-режиме мы намеренно перерабатываем даже completed, например после фикса пайплайна.
        processed_ids = set()
        if not force:
            processed_response = await supabase._request(
                "GET",
                "/rest/v1/processed_meetings",
                params={
                    "user_id": f"eq.{user_id}",
                    "select": "meeting_id,status",
                },
            )
            # Skip only terminal statuses so error cases can be retried after fixes.
            terminal_statuses = {"completed", "no_transcript"}
            processed_ids = {
                p.get("meeting_id")
                for p in (processed_response or [])
                if p.get("meeting_id") and p.get("status") in terminal_statuses
            }

        # Resumable force: продолжить прерванный force-прогон, не начиная 200
        # заново. mark_completion пишет processed_at=now на КАЖДУЮ переобработку,
        # поэтому встречи, уже переделанные в ЭТОМ прогоне (processed_at >=
        # cutoff), имеют свежую метку и их можно пропустить; «старые» (исходный
        # ингест) — переобрабатываем. cutoff = момент старта твоей force-кампании.
        if force and reprocess_older_than:
            try:
                _resp = await supabase._request(
                    "GET",
                    "/rest/v1/processed_meetings",
                    params={
                        "user_id": f"eq.{user_id}",
                        "select": "meeting_id,processed_at",
                    },
                )
                _fresh = {
                    p.get("meeting_id")
                    for p in (_resp or [])
                    if p.get("meeting_id") and p.get("processed_at")
                    and str(p["processed_at"]) >= reprocess_older_than
                }
                processed_ids |= _fresh
                logger.info(
                    "Resumable force: %d встреч уже переобработаны с %s — пропускаю",
                    len(_fresh), reprocess_older_than,
                )
            except Exception:
                logger.warning(
                    "resumable-force skip query failed — переобрабатываю все",
                    exc_info=True,
                )

        # Обрабатываем каждую встречу с контекстом трекинга
        # model_tier передаётся в контекст, чтобы GeminiClient использовал нужную модель
        event_bus = get_event_bus()
        async with UsageContext(
            user_id=user_id,
            agent_mode="sync",
            request_type=f"knowledge_sync_{model_tier}",
            model_tier=model_tier
        ):
            for meeting in meetings:
                meeting_id = meeting.get("id")

                _title = meeting.get("title") or meeting.get("name") or str(meeting_id)

                if meeting_id in processed_ids:
                    stats["skipped"] += 1
                    _emit_progress(_total_meetings, current_title=_title)
                    continue

                try:
                    # First time we discover an unprocessed meeting in subscription scope.
                    # This allows event-driven automations to react to "new meeting appeared".
                    if not force:
                        await event_bus.emit(
                            event_type=EventType.NEW_MEETING_SCHEDULED,
                            payload={
                                "meeting_id": meeting_id,
                                "title": meeting.get("title", ""),
                                "project_id": meeting.get("project_id"),
                                "folder_id": meeting.get("folder_id"),
                                "created_at": meeting.get("created_at"),
                                "subscription_id": subscription_id,
                            },
                            user_id=user_id,
                            source="knowledge_sync_scan",
                        )

                    result = await _process_single_meeting(
                        meeting=meeting,
                        user_id=user_id,
                        subscription_id=subscription_id,
                        force=force,
                        model_tier=model_tier,
                    )

                    stats["processed"] += 1
                    stats["entities_created"] += result.get("entities_created", 0)
                    stats["relationships_created"] += result.get("relationships_created", 0)
                    _emit_progress(_total_meetings, current_title=_title)

                    # Cost-guard: ограничить число НОВЫХ встреч за один прогон.
                    # Защита от runaway-расхода, если дедуп деградировал
                    # (processed_meetings не пишется) — иначе поллер переработал
                    # бы весь бэклог каждый цикл. Остаток досинкается следующими
                    # проходами. None = без лимита (ручной sync).
                    if max_process and stats["processed"] >= max_process:
                        stats["capped"] = True
                        logger.warning(
                            "sync: достигнут лимит max_process=%d за прогон "
                            "(subscription %s) — остальное в следующий раз. "
                            "Если это повторяется каждый цикл на ОДНИХ и тех же "
                            "встречах — проблема с dedup (processed_meetings).",
                            max_process, subscription_id)
                        break

                except Exception as e:
                    logger.error(f"Error processing meeting {meeting_id}: {e}")
                    stats["errors"] += 1
                    _emit_progress(_total_meetings, current_title=_title)

        # Обновляем статистику подписки (аудит sync #5: ИНКРЕМЕНТ, а не
        # перезапись — раньше «пустой» sync обнулял накопленный счётчик)
        prev_processed = 0
        try:
            sub_rows = await supabase._request(
                "GET",
                "/rest/v1/knowledge_sync_subscriptions",
                params={"id": f"eq.{subscription_id}",
                        "select": "meetings_processed"}
            )
            if sub_rows:
                prev_processed = int(sub_rows[0].get("meetings_processed") or 0)
        except Exception:
            logger.debug("could not read prev meetings_processed", exc_info=True)
        await supabase._request(
            "PATCH",
            "/rest/v1/knowledge_sync_subscriptions",
            params={"id": f"eq.{subscription_id}"},
            json_data={
                "meetings_processed": prev_processed + stats["processed"],
                "last_sync_at": datetime.now(timezone.utc).isoformat(),
                "status": "active"
            }
        )

        _emit_progress(_total_meetings, phase="completed")
        logger.info(f"Subscription {subscription_id} sync completed: {stats}")

        # Авто-свежесть снапшотов: новые данные в графе → кэш снапшотов
        # устарел. Без этого страница «Компания» показывала старое до кнопки
        # «Пересобрать» (для клиентов недопустимо). Маркер межпроцессный.
        if stats.get("processed") or stats.get("entities_created"):
            try:
                from backend.core.sleep.enhanced_snapshot import (
                    invalidate_snapshot_cache,
                )
                invalidate_snapshot_cache(user_id)
                logger.info(
                    "♻️ Кэш снапшотов инвалидирован после sync (user %s…)",
                    str(user_id or "")[:8])
            except Exception:
                logger.debug("snapshot invalidate skipped", exc_info=True)
        return stats

    except Exception as e:
        logger.error(f"Error in process_meetings_for_subscription: {e}")
        raise
    finally:
        # Снимаем tenant-scope, чтобы не протёк в другие задачи воркера.
        if _tenant_token is not None:
            try:
                reset_current_tenant(_tenant_token)
            except Exception:
                pass


async def _get_folder_tree_ids(
    supabase,
    user_id: str,
    root_folder_id: str,
) -> List[str]:
    """
    Собрать список id папок: root + все вложенные (рекурсивно).
    Делает один запрос на получение дерева папок пользователя и затем BFS по памяти.
    """
    try:
        folders = await supabase._request(
            "GET",
            "/rest/v1/folders",
            params={
                "user_id": f"eq.{user_id}",
                "select": "id,parent_folder_id",
            },
        )
        if not folders:
            return [root_folder_id]

        children: Dict[Optional[str], List[str]] = {}
        for f in folders:
            fid = f.get("id")
            pid = f.get("parent_folder_id")
            if not fid:
                continue
            children.setdefault(pid, []).append(fid)

        # BFS
        result: List[str] = []
        seen = set()
        queue = [root_folder_id]
        while queue:
            cur = queue.pop(0)
            if cur in seen:
                continue
            seen.add(cur)
            result.append(cur)
            for child_id in children.get(cur, []):
                if child_id not in seen:
                    queue.append(child_id)

        return result
    except Exception as e:
        logger.warning(f"Could not expand subfolders for folder {root_folder_id}: {e}")
        return [root_folder_id]


# ── Кап ретраев переобработки ───────────────────────────────────────────
# Quality-gate помечает встречу FAILED → следующий цикл честно ретраит.
# Но если агенты падают СТАБИЛЬНО (деградация провайдера, битые ответы
# out=1), встреча переобрабатывалась бесконечно: каждый час ~$0.06 и залп
# досок. Кап: после N неудач помечаем completed с частичным качеством —
# громко в лог, но без вечного колеса.

def _retry_path(user_id: str) -> str:
    import os as _os
    safe = "".join(c for c in str(user_id or "anon")
                   if c.isalnum() or c == "-")[:40] or "anon"
    d = _os.path.join("data", "sync_retry")
    _os.makedirs(d, exist_ok=True)
    return _os.path.join(d, f"{safe}.json")


def _bump_retry(user_id: str, meeting_id: str) -> int:
    import json as _json
    try:
        p = _retry_path(user_id)
        data = {}
        try:
            with open(p, encoding="utf-8") as f:
                data = _json.load(f) or {}
        except Exception:
            data = {}
        n = int(data.get(str(meeting_id)) or 0) + 1
        data[str(meeting_id)] = n
        if len(data) > 300:
            for k in list(data)[:len(data) - 300]:
                data.pop(k, None)
        with open(p, "w", encoding="utf-8") as f:
            _json.dump(data, f)
        return n
    except Exception:
        logger.debug("sync retry counter failed", exc_info=True)
        return 1


def _clear_retry(user_id: str, meeting_id: str) -> None:
    import json as _json
    try:
        p = _retry_path(user_id)
        with open(p, encoding="utf-8") as f:
            data = _json.load(f) or {}
        if str(meeting_id) in data:
            data.pop(str(meeting_id), None)
            with open(p, "w", encoding="utf-8") as f:
                _json.dump(data, f)
    except Exception:
        pass


def _max_meeting_retries() -> int:
    try:
        return max(1, int(os.getenv("TESSENT_MEETING_MAX_RETRIES", "3")))
    except (TypeError, ValueError):
        return 3


def _budget_props(parsed: Optional[Dict[str, Any]], raw_text: str,
                  budget_status: str) -> Dict[str, Any]:
    """Свойства бюджета для узла проекта.

    Пустые поля не возвращаем вовсе: на встрече бюджет могли не обсуждать,
    и запись пустоты затёрла бы известную сумму «ничем». Отсутствие ключа
    в update_node означает «не трогаем», пустая строка означала бы
    «обнулили».

    `budget_currency_known=False` сохраняется намеренно: сумма названа, а
    валюта нет. Это видно потребителю, вместо молчаливой подстановки рублей.
    """
    props: Dict[str, Any] = {}
    if raw_text:
        props["budget_text"] = raw_text
    if budget_status:
        props["budget_status"] = budget_status
    if parsed:
        props["budget"] = parsed["normalized"]
        props["budget_amount"] = parsed["amount"]
        props["budget_currency"] = parsed["currency"]
        props["budget_currency_known"] = parsed["currency_known"]
    return props


async def _process_single_meeting(
    meeting: Dict[str, Any],
    user_id: str,
    subscription_id: str,
    force: bool = False,
    model_tier: str = "standard",
) -> Dict[str, Any]:
    """
    Обработать одну встречу через CaptureOrchestrator (все модули анализа).

    Args:
        meeting: Данные встречи
        user_id: ID пользователя
        subscription_id: ID подписки
        model_tier: Уровень модели - "standard" или "premium"

    Returns:
        Результат обработки
    """
    from backend.core.automations.event_bus import EventType, get_event_bus
    from backend.core.capture.orchestrator import CaptureOrchestrator
    from backend.core.store.graph_builder import GraphBuilder
    from backend.core.store.vector_indexer import VectorIndexer
    from backend.db.supabase_client import get_supabase_client

    supabase = get_supabase_client()
    meeting_id = meeting.get("id")

    result = {
        "meeting_id": meeting_id,
        "entities_created": 0,
        "relationships_created": 0,
        "agents_run": [],
        "analysis_results": {}
    }

    # Получаем транскрипт
    transcript = meeting.get("transcription_text") or meeting.get("transcript")

    # ─── ORG-AWARE DEDUP HOOK ─────────────────────────────────────────────
    # До запуска тяжёлого orchestrator'а проверим ledger: не обработан ли уже
    # этот эпизод другим участником той же org (или нами через другой канал).
    # См. backend/core/ingest/meeting_upsert.find_completed_episode для логики.
    dedup_key = _ingest_dedup.build_dedup_key(meeting, transcript or "")
    org_id_for_dedup = _get_org_for_user(user_id)  # None для solo / Mini Tess
    if not force and (dedup_key["external_id"] or dedup_key["content_hash"]):
        existing = await _ingest_upsert.find_completed_episode(
            supabase,
            org_id=org_id_for_dedup,
            source=dedup_key["source"],
            external_id=dedup_key["external_id"],
            content_hash=dedup_key["content_hash"],
        )
        if existing:
            logger.info(
                "Meeting %s already processed in this org (existing meeting_id=%s) — skip",
                meeting_id, existing.get("meeting_id"),
            )
            # Запишем pointer в ledger для этого user/meeting, чтобы при следующем
            # скане _не_ перегонять заново; reuse дедупа на уровне subscription.
            await _ingest_upsert.mark_completion(
                supabase,
                user_id=user_id, meeting_id=meeting_id,
                org_id=org_id_for_dedup,
                source=dedup_key["source"],
                external_id=dedup_key["external_id"],
                content_hash=dedup_key["content_hash"],
                subscription_id=subscription_id,
                status=_ingest_upsert.STATUS_COMPLETED,
            )
            result["deduped"] = True
            result["deduped_against"] = existing.get("meeting_id")
            return result
    # ──────────────────────────────────────────────────────────────────────

    if not transcript:
        logger.warning(f"No transcript for meeting {meeting_id}")
        await _ingest_upsert.mark_completion(
            supabase,
            user_id=user_id, meeting_id=meeting_id,
            org_id=org_id_for_dedup,
            source=dedup_key["source"],
            external_id=dedup_key["external_id"],
            content_hash=dedup_key["content_hash"],
            subscription_id=subscription_id,
            status=_ingest_upsert.STATUS_NO_TRANSCRIPT,
        )
        return result

    try:
        logger.info(f"🔄 Processing meeting {meeting_id} with CaptureOrchestrator")

        # Создаём GraphBuilder для memory reinforcement
        from backend.core.store.tenant_paths import graph_path_for_user, vector_index_path_for_user

        graph_builder = GraphBuilder(
            use_networkx=None,  # Auto-detect: uses Neo4j if available, otherwise NetworkX
            graph_storage_path=graph_path_for_user(user_id)
        )
        await graph_builder.connect()

        vector_indexer = VectorIndexer(
            use_qdrant=None,
            storage_path=vector_index_path_for_user(user_id),
            namespace=user_id,
        )
        await vector_indexer.connect()

        # Создаём оркестратор с graph_builder для memory reinforcement
        orchestrator = CaptureOrchestrator(
            graph_builder=graph_builder,
            vector_indexer=vector_indexer
        )
        # Узлы сущностей создаём ПОСЛЕ process_meeting (в _save_orchestrator_
        # results_to_graph), поэтому Hebbian co-occurrence откладываем туда же —
        # с реальными id узлов. Иначе он бежит по сырым slug'ам без узлов.
        orchestrator.defer_cooccurrence = True
        try:
            orchestrator_config = getattr(orchestrator, "config", None)
            if isinstance(orchestrator_config, dict):
                orchestrator_config.update({
                    "parallel_execution": True,
                    "use_context_caching": True,
                    "enable_cross_analysis": True,
                    "enable_cross_meeting_links": True,
                    "enable_psychological_analysis": True,
                    "enable_future_predictions": True,
                })
            else:
                orchestrator.config = {
                    "parallel_execution": True,
                    "use_context_caching": True,
                    "enable_cross_analysis": True,
                    "enable_cross_meeting_links": True,
                    "enable_psychological_analysis": True,
                    "enable_future_predictions": True,
                }
        except Exception as e:
            logger.warning(f"Could not apply CaptureOrchestrator config overrides: {e}")

        # Контекст встречи (включая транскрипт для полнотекстовой индексации)
        meeting_context = {
            "meeting_id": meeting_id,
            "title": meeting.get("title", "Untitled"),
            "date": meeting.get("date") or meeting.get("created_at"),
            "project_id": meeting.get("project_id"),
            "folder_id": meeting.get("folder_id"),
            "user_id": user_id,
            "participants": meeting.get("participants", []),
            "transcript": transcript,  # Для полнотекстовой индексации чанками
        }

        # ─── Узел встречи создаём ДО запуска агентов ──────────────────────
        # Внутри process_meeting экстракторы (knowledge/regulation/template)
        # сразу линкуют chunk/knowledge/regulation/template → узел встречи.
        # Раньше узел встречи создавался ПОЗЖЕ, в _save_orchestrator_results_to_graph,
        # поэтому на момент линковки его не было в Neo4j → лавина
        # «⚠️ Nodes not found for relationship: … -> <uuid>», и знания/чанки
        # оставались висящими. Создаём заранее (MERGE-идемпотентно по id);
        # _save_orchestrator_results_to_graph позже обогатит тот же узел.
        try:
            await graph_builder.create_node(
                node_id=f"meeting_{meeting_id}",
                label="Meeting",
                properties={
                    "meeting_id": meeting_id,
                    "user_id": user_id,
                    "title": meeting_context.get("title", ""),
                    "date": meeting_context.get("date", "") or "",
                    "project_id": meeting_context.get("project_id") or "",
                    "folder_id": meeting_context.get("folder_id"),
                },
            )
        except Exception as _mk_err:
            logger.warning(f"Pre-create meeting node failed for {meeting_id}: {_mk_err}")

        # Запускаем полный анализ через все агенты
        analysis_result = await orchestrator.process_meeting(
            transcript=transcript,
            meeting_id=meeting_id,
            meeting_context=meeting_context
        )

        result["analysis_results"] = analysis_result
        result["agents_run"] = list(analysis_result.get("agent_results", {}).keys())

        # Сохраняем результаты в граф
        if analysis_result:
            graph_result = await _save_orchestrator_results_to_graph(
                analysis_result=analysis_result,
                meeting_id=meeting_id,
                user_id=user_id,
                meeting_context=meeting_context,
                force=force,
            )

            result["entities_created"] = graph_result.get("entities_created", 0)
            result["relationships_created"] = graph_result.get("relationships_created", 0)

        # ─── CogniLayer Ф1.3: когнитивные измерения → persona_observations ───
        # Профили cogni-агента атрибутируются к аккаунтам (сшивка Ф0 / имя) и
        # пишутся каждому человеку под его user_id. Идёт ПОСЛЕ сохранения графа,
        # чтобы Person-узлы встречи уже были канонизированы entity-резолвером.
        # Never-raise: сбой не откатывает успех встречи.
        try:
            _cogni_profiles = (analysis_result or {}).get("agent_results", {}).get(
                "cogni_profiles") or []
            if _cogni_profiles:
                from backend.core.cogni.observe import persist_cogni_observations
                _cogni_resolver = EntityResolver(graph_builder=graph_builder)
                _cogni_res = await persist_cogni_observations(
                    owner_uid=user_id, meeting_id=meeting_id,
                    profiles=_cogni_profiles, entity_resolver=_cogni_resolver,
                )
                if _cogni_res.get("saved"):
                    logger.info(
                        "🧭 cogni: %d наблюдений сохранено (встреча %s, пропущено %d)",
                        _cogni_res["saved"], meeting_id, _cogni_res.get("skipped", 0))
        except Exception:
            logger.warning("cogni observations persist skipped", exc_info=True)

        # ─── CogniLayer Ф4: предиктивная сверка — решения встречи vs цели ───
        # Скелет без LLM: свежие решения против активных целей компании/отделов;
        # «решение тянет против цели» ловится ДО того, как цели разъедутся.
        # Never-raise: сбой сверки не трогает обработку встречи.
        try:
            from backend.core.think.predictive_sync import run_predictive_check
            _pred = await run_predictive_check(
                user_id, meeting_id,
                (analysis_result or {}).get("agent_results") or {})
            if _pred.get("warnings"):
                logger.info("🔮 predictive: %d ранних сигналов (встреча %s, "
                            "уведомлено %d)", _pred["warnings"], meeting_id,
                            _pred.get("notified", 0))
        except Exception:
            logger.warning("predictive sync check skipped", exc_info=True)

        # ─── QUALITY-GATE (волна B): не помечать completed при СИСТЕМНОМ
        # провале извлечения. agent_errors — только ОШИБКИ агентов (rate-
        # limit/провайдер лёг), не честные «извлечено 0». Если ошибся МАЖОР
        # базовых агентов — это транзиентно: пишем FAILED (не терминальный →
        # ретрай следующим циклом), чтобы половина решений/задач не пропала
        # молча. Битый транскрипт даёт ПУСТО, а не ошибки — он пройдёт как
        # completed и не зациклится. Порог настраиваемый; 0 = выключить гейт.
        agent_errors = (analysis_result or {}).get("agent_errors") or {}
        agents_run_cnt = len(result.get("agents_run", [])) or 1
        try:
            _fail_ratio = float(os.getenv("TESSENT_MEETING_FAIL_RATIO", "0.5"))
        except (TypeError, ValueError):
            _fail_ratio = 0.5
        failed_frac = len(agent_errors) / agents_run_cnt
        gate_failed = _fail_ratio > 0 and failed_frac >= _fail_ratio

        if gate_failed:
            logger.warning(
                f"⚠️ quality-gate: встреча {meeting_id} — {len(agent_errors)}"
                f"/{agents_run_cnt} агентов упали "
                f"({', '.join(sorted(agent_errors)[:5])}); помечаю FAILED для "
                "ретрая, чтобы не потерять извлечение")
            result["quality_gate"] = {
                "passed": False, "failed_agents": sorted(agent_errors),
                "failed_fraction": round(failed_frac, 2),
            }

        # Эмитим событие обработанной встречи для event-автоматизаций — ПОСЛЕ
        # quality-gate и только при успехе. Раньше эмиссия шла безусловно ДО
        # гейта: встреча падала → FAILED → ретрай следующим циклом → событие
        # эмитилось СНОВА при каждом ретрае → доски слали одну и ту же старую
        # встречу много раз. summary — ОБЯЗАТЕЛЬНО в payload: доски «триггер →
        # генерация/карта» без узла meeting_data получали сырой JSON события;
        # _payload_to_text предпочитает именно поле summary.
        # Наблюдатель Ф1: структурный снапшот встречи (blockers/tensions/
        # explicit_asks) — кэш навсегда, 1 вызов на встречу. За флагом
        # enable_meeting_snapshots; never-raise — сбой не трогает синк.
        if not gate_failed:
            try:
                from backend.core.observer.meeting_snapshot import (
                    ensure_snapshot,
                    snapshots_enabled,
                )
                if snapshots_enabled():
                    await ensure_snapshot(user_id, {
                        "id": meeting_id,
                        "title": meeting.get("title"),
                        "created_at": meeting.get("created_at"),
                        "transcription_text": transcript,
                    })
            except Exception:
                logger.debug("meeting snapshot skipped", exc_info=True)

        if not gate_failed:
            _sd = (analysis_result or {}).get("summary") or {}
            # ВАЖНО: .get("summary") может вернуть None — вариант без "or ''"
            # падал NoneType.strip() и глушил эмиссию (доски молчали).
            _summary_text = ((_sd.get("summary") or "") if isinstance(_sd, dict)
                             else str(_sd or "")).strip()
            event_bus = get_event_bus()
            await event_bus.emit(
                event_type=EventType.MEETING_ENDED,
                payload={
                    "meeting_id": meeting_id,
                    # force-прогон = переобработка СТАРОЙ встречи (кампания/фикс
                    # пайплайна) — событийные доски по таким не стреляют вовсе.
                    "is_reindex": bool(force),
                    "summary": _summary_text[:4000],
                    "title": meeting.get("title", ""),
                    "project_id": meeting.get("project_id"),
                    "folder_id": meeting.get("folder_id"),
                    "subscription_id": subscription_id,
                    "entities_created": result.get("entities_created", 0),
                    "relationships_created": result.get("relationships_created", 0),
                    "agents_run": result.get("agents_run", []),
                },
                user_id=user_id,
                source="knowledge_sync",
            )

        # Помечаем встречу с полным ключом для org-дедупа. FAILED = ретрай
        # следующим циклом, но с КАПОМ: стабильно падающая встреча после
        # N попыток помечается completed (частично) — не жжём бюджет вечно.
        _final_status = (_ingest_upsert.STATUS_FAILED if gate_failed
                         else _ingest_upsert.STATUS_COMPLETED)
        if gate_failed:
            _attempts = _bump_retry(user_id, meeting_id)
            if _attempts >= _max_meeting_retries():
                _final_status = _ingest_upsert.STATUS_COMPLETED
                logger.warning(
                    "⛔ встреча %s: %d неудачных обработок подряд — помечаю "
                    "completed с ЧАСТИЧНЫМ качеством, чтобы не переобрабатывать "
                    "бесконечно (деньги + повторные срабатывания досок). "
                    "Пересобрать вручную: force-режим синка.",
                    meeting_id, _attempts)
        else:
            _clear_retry(user_id, meeting_id)
        await _ingest_upsert.mark_completion(
            supabase,
            user_id=user_id, meeting_id=meeting_id,
            org_id=org_id_for_dedup,
            source=dedup_key["source"],
            external_id=dedup_key["external_id"],
            content_hash=dedup_key["content_hash"],
            subscription_id=subscription_id,
            status=_final_status,
        )

        # ─── ORG PROMOTE HOOK ────────────────────────────────────────────
        # Personal-граф уже содержит свежие узлы этого эпизода. Если user
        # привязан к org и источник не личный (Mini Tess / personal memo) —
        # копируем «слепок meeting'а» в org-graph для cross-team видимости.
        promote_org = _ingest_promote.should_promote(user_id, dedup_key["source"])
        if promote_org:
            try:
                promote_result = await _ingest_promote.promote_meeting_to_org(
                    user_id=user_id,
                    meeting_id=meeting_id,
                    org_id=promote_org,
                    source=dedup_key["source"],
                )
                result["promoted_to_org"] = promote_result
            except Exception as pe:
                # Promote — best-effort: personal flow уже отработал.
                logger.warning("promote failed for meeting %s: %s", meeting_id, pe)
                result["promoted_to_org"] = {"promoted": False, "reason": str(pe)[:200]}
        # ─────────────────────────────────────────────────────────────────

        # Закрываем ресурсы (graph_builder и vector_indexer созданы выше)
        try:
            await graph_builder.close()
            await vector_indexer.close()
        except Exception as close_err:
            logger.warning(f"Error closing resources: {close_err}")

        logger.info(f"✅ Meeting {meeting_id} processed: {len(result['agents_run'])} agents, {result['entities_created']} entities")
        return result

    except Exception as e:
        logger.error(f"Error processing meeting {meeting_id}: {e}")
        # Закрываем ресурсы при ошибке
        try:
            if 'graph_builder' in locals():
                await graph_builder.close()
            if 'vector_indexer' in locals():
                await vector_indexer.close()
        except Exception:
            logger.debug("suppressed exception", exc_info=True)
        # Тот же кап ретраев и на пути жёсткой ошибки: вечное колесо хуже
        # частичной обработки.
        _attempts = _bump_retry(user_id, meeting_id)
        _err_status = _ingest_upsert.STATUS_FAILED
        if _attempts >= _max_meeting_retries():
            _err_status = _ingest_upsert.STATUS_COMPLETED
            logger.warning(
                "⛔ встреча %s: %d ошибок обработки подряд — помечаю completed "
                "(частично), последний err в last_error. Пересобрать: force-синк.",
                meeting_id, _attempts)
        await _ingest_upsert.mark_completion(
            supabase,
            user_id=user_id, meeting_id=meeting_id,
            org_id=org_id_for_dedup,
            source=dedup_key["source"],
            external_id=dedup_key["external_id"],
            content_hash=dedup_key["content_hash"],
            subscription_id=subscription_id,
            # Статус — из фиксированного словаря (CHECK в Supabase); текст
            # ошибки уходит в last_error. Свободный "error: <текст>" нарушал
            # CHECK → строка не писалась → встреча переобрабатывалась вечно.
            status=_err_status,
            error=str(e),
        )
        raise


async def _save_orchestrator_results_to_graph(
    analysis_result: Dict[str, Any],
    meeting_id: str,
    user_id: str,
    meeting_context: Dict[str, Any],
    force: bool = False,
) -> Dict[str, Any]:
    """
    Сохранить результаты CaptureOrchestrator в граф знаний.

    Включает:
    - Создание узлов и связей в графе
    - Индексацию в векторную базу
    - Entity Resolution для дедупликации
    - Temporal Tracking для версионирования
    - Timeline events для построения истории
    """
    from backend.core.search.bm25_searcher import get_bm25_searcher  # NEW: BM25
    from backend.core.store.graph_builder import GraphBuilder
    from backend.core.store.vector_indexer import VectorIndexer
    from backend.core.temporal import get_temporal_manager, get_temporal_tracker

    result = {"entities_created": 0, "relationships_created": 0}

    try:
        from backend.core.store.tenant_paths import graph_path_for_user, vector_index_path_for_user

        def _first_text(*values: Any) -> str:
            """Return first non-empty string-like value."""
            for v in values:
                if v is None:
                    continue
                if isinstance(v, str):
                    s = v.strip()
                    if s:
                        return s
                else:
                    try:
                        s = str(v).strip()
                        if s and s.lower() != "none":
                            return s
                    except Exception:
                        continue
            return ""

        graph_builder = GraphBuilder(
            use_networkx=None,  # Auto-detect: uses Neo4j if available, otherwise NetworkX
            graph_storage_path=graph_path_for_user(user_id)
        )
        await graph_builder.connect()

        vector_indexer = VectorIndexer(
            use_qdrant=None,  # авто: Qdrant если доступен, иначе file-backed memory
            storage_path=vector_index_path_for_user(user_id),
            namespace=user_id,
        )
        await vector_indexer.connect()

        # ===== BM25 SEARCHER =====
        bm25_searcher = get_bm25_searcher(user_id=user_id)

        # ===== TEMPORAL TRACKING =====
        # Инициализируем TemporalTracker для версионирования сущностей
        temporal_tracker = get_temporal_tracker(user_id)
        await temporal_tracker.initialize()

        # Инициализируем TemporalManager для temporal episodes (Graphiti)
        temporal_manager = get_temporal_manager()
        try:
            await temporal_manager.initialize()
        except Exception as tm_err:
            logger.warning(f"TemporalManager init failed (non-critical): {tm_err}")

        # Вычисляем access_group для UI-группировки (не для security).
        # Security/изоляция делается по отдельным путям хранения и user_id.
        access_group = (
            meeting_context.get("folder_id")
            or meeting_context.get("project_id")
            or "public"
        )

        # В force-режиме чистим meeting-subgraph, чтобы не плодить дубликаты рёбер/нод в MultiDiGraph.
        if force and getattr(graph_builder, "use_networkx", False) and getattr(graph_builder, "nx_graph", None):
            try:
                prefixes = (
                    f"meeting_{meeting_id}",
                    f"decision_{meeting_id}_",
                    f"task_{meeting_id}_",
                    f"entity_{meeting_id}_",
                    f"kpi_{meeting_id}_",
                )
                g = graph_builder.nx_graph
                to_remove = [nid for nid in list(g.nodes()) if nid == prefixes[0] or any(nid.startswith(p) for p in prefixes[1:])]
                for nid in to_remove:
                    if g.has_node(nid):
                        g.remove_node(nid)
                logger.info(f"🧹 Force sync: removed {len(to_remove)} nodes for meeting {meeting_id}")
            except Exception as ce:
                logger.warning(f"Force sync cleanup failed for meeting {meeting_id}: {ce}")

        # Создаём узел встречи
        meeting_node_id = f"meeting_{meeting_id}"
        meeting_title = meeting_context.get("title", "")
        meeting_date = meeting_context.get("date", "")
        meeting_project = meeting_context.get("project_id", "")

        # Реальные id узлов сущностей/участников для Hebbian co-occurrence.
        # Hebbian внутри orchestrator.process_meeting бежит со СЫРЫМИ slug-id
        # (kpd_company и т.п.) ДО создания узлов → 0 связей и спам «Nodes not
        # found». Собираем настоящие id здесь, по мере создания узлов, и
        # запускаем co-occurrence в самом конце (orchestrator его откладывает,
        # см. defer_cooccurrence).
        _cooc_node_ids: list[str] = []

        # ═══ Step 2: Bi-temporal base props для всех связей этой встречи ═══
        _temporal_base = {
            "valid_from": meeting_date or None,  # Факт актуален с даты встречи
            "known_from": datetime.now(timezone.utc).isoformat(),  # Мы узнали сейчас
            "source_meeting_id": meeting_id,
        }

        # Та же би-темпоральность для версий сущностей: версия датируется
        # ДАТОЙ ВСТРЕЧИ, а не моментом индексации. Иначе загруженный разом
        # архив встреч даёт всем версиям сегодняшний штамп, и запрос
        # «как было на 15 марта» возвращает пустоту. None → трекер сам
        # подставит время записи.
        _occurred_at = meeting_date or None

        # История фактов из встреч. Supersede-стор до сих пор наполнялся
        # ТОЛЬКО ручными правками — то есть «цепочка датированных версий с
        # пометкой заменённого» существовала лишь для того, что человек
        # исправил руками. Разбор встречи писал версии в temporal-трекер, но
        # мимо supersede, где живёт пометка «заменено» и счётчик повторов.
        # Копим здесь и пишем одним пакетом в конце разбора.
        _fact_versions: list = []

        def _track_fact(entity_id: str, field: str, value: Any) -> None:
            """Запомнить наблюдение факта для supersede-истории.

            Пустые значения пропускаем: «поле не упомянули на встрече» — это
            не «поле стало пустым», и запись пустоты затёрла бы текущую
            версию факта новой, ложной.
            """
            if not entity_id or not field:
                return
            text = str(value or "").strip()
            if not text:
                return
            _fact_versions.append({
                "key": f"{user_id}::{entity_id}::{field}",
                "value": text,
                "source": f"meeting:{meeting_id}",
                "as_of": _occurred_at,
            })

        await graph_builder.create_node(
            node_id=meeting_node_id,
            label="Meeting",
            properties={
                "meeting_id": meeting_id,
                "user_id": user_id,
                "title": meeting_title,
                "date": meeting_date,
                "project_id": meeting_project,
                "folder_id": meeting_context.get("folder_id"),
            },
            access_group=access_group,
        )
        result["entities_created"] += 1

        # Получаем результаты агентов заранее (нужны для temporal tracking)
        agent_results = analysis_result.get("agent_results", {})

        # Спикер-резолвинг: «Speaker N» → имя по контексту (само-представление
        # «я Максим» / обращение «Максим, что думаешь?»). speaker_resolver был
        # готов, но в ingest не вызывался. Карта используется ниже как fallback,
        # когда агент вернул только speaker-label без имени.
        _speaker_map: dict = {}
        try:
            _ts_for_speakers = meeting_context.get("transcript") or ""
            if _ts_for_speakers and "Speaker" in _ts_for_speakers:
                from backend.core.transcripts import resolve_speakers_for_extraction
                _known = [p.get("name") for p in agent_results.get("participants", [])
                          if p.get("name")]
                _speaker_map = resolve_speakers_for_extraction(
                    _ts_for_speakers, known_participants=_known) or {}
                if _speaker_map:
                    logger.info(f"🗣️ Speaker resolution: {_speaker_map}")
        except Exception:
            logger.debug("speaker resolution skipped", exc_info=True)

        # ===== TEMPORAL: Добавляем встречу как temporal episode =====
        try:
            # Парсим дату встречи
            meeting_datetime = datetime.now(timezone.utc)
            if meeting_date:
                try:
                    if isinstance(meeting_date, str):
                        meeting_datetime = datetime.fromisoformat(meeting_date.replace('Z', '+00:00'))
                except Exception:
                    logger.debug("suppressed exception", exc_info=True)

            # Добавляем в Graphiti как temporal episode
            transcript = meeting_context.get("transcript", "")
            if transcript and temporal_manager._initialized:
                participants = [p.get("name", "") for p in agent_results.get("participants", [])]
                await temporal_manager.add_meeting_episode(
                    meeting_id=meeting_id,
                    title=meeting_title,
                    transcript=transcript[:50000],  # Лимит для Graphiti
                    meeting_time=meeting_datetime,
                    project_id=meeting_project,
                    participants=participants
                )
                result["temporal_episode_added"] = True

            # Добавляем timeline event для встречи
            await temporal_tracker.add_timeline_event(
                entity_id=meeting_node_id,
                entity_type="meeting",
                event_type="created",
                event_time=meeting_datetime,
                event_data={
                    "title": meeting_title,
                    "participants_count": len(agent_results.get("participants", [])),
                    "decisions_count": len(agent_results.get("decisions", [])),
                    "tasks_count": len(agent_results.get("tasks", [])),
                },
                meeting_id=meeting_id,
                description=f"Встреча: {meeting_title}"
            )
            result["timeline_events_added"] = result.get("timeline_events_added", 0) + 1

        except Exception as te:
            logger.warning(f"Temporal tracking failed for meeting {meeting_id}: {te}")

        # ===== ИНДЕКСАЦИЯ ВСТРЕЧИ В ВЕКТОРА =====
        # Формируем текст из summary если есть
        summary_data = analysis_result.get("summary", {})
        meeting_summary_text = summary_data.get("summary", "") if isinstance(summary_data, dict) else str(summary_data)

        meeting_text_parts = [
            f"Встреча: {meeting_title}" if meeting_title else "",
            f"Дата: {meeting_date}" if meeting_date else "",
            f"Проект: {meeting_project}" if meeting_project else "",
            meeting_summary_text,
        ]
        meeting_text = " | ".join(filter(None, meeting_text_parts))

        if meeting_text:
            try:
                await vector_indexer.upsert_vector_public(
                    collection=vector_indexer.collections["meetings"],
                    vector_id=meeting_node_id,
                    text=meeting_text,
                    metadata={
                        "meeting_id": meeting_id,
                        "user_id": user_id,
                        "type": "meeting",
                        "title": meeting_title,
                        "date": meeting_date,
                        "project_id": meeting_project,
                    },
                )
                result["meetings_indexed"] = result.get("meetings_indexed", 0) + 1

                # FIX #9: BM25 для meetings
                bm25_searcher.add_document(
                    doc_id=meeting_node_id,
                    text=meeting_text,
                    metadata={"type": "meeting", "meeting_id": meeting_id}
                )
            except Exception as ve:
                logger.warning(f"Vector upsert failed for meeting {meeting_node_id}: {ve}")

        # ===== ENTITY RESOLVER для дедупликации =====
        entity_resolver = EntityResolver(
            graph_builder=graph_builder,
            vector_indexer=vector_indexer
        )

        # Анти-протечка (общий Neo4j): свои tenant-ы для проверки узлов,
        # которые вернул резолвер. Чужой узел (другой аккаунт) не обновляем
        # и НЕ пишем его свойства в per-tenant temporal-хранилище — иначе
        # чужие meeting_ids/tenant_id всплывают в панели «Эволюция объекта».
        try:
            from backend.core.store.tenant_scope import allowed_tenants
            _own_tenants = allowed_tenants(user_id)
        except Exception:
            _own_tenants = {user_id}

        # Участники (с Entity Resolution)
        for participant in agent_results.get("participants", []):
            p_name = participant.get("name", "Unknown")
            p_role = participant.get("role", "")
            p_department = participant.get("department", "")
            p_email = participant.get("email", "")

            # Используем EntityResolver для поиска/создания сущности
            resolved = await entity_resolver.resolve(
                name=p_name,
                entity_type="person",
                organization_id=user_id,  # user_id как organization_id
                external_ids={"email": p_email} if p_email else None,
                properties={
                    "role": p_role,
                    "department": p_department,
                }
            )

            p_id = resolved.canonical_id
            if p_id and p_id not in _cooc_node_ids:
                _cooc_node_ids.append(p_id)

            # Если сущность новая, создаём узел в графе
            if resolved.is_new:
                await graph_builder.create_node(
                    node_id=p_id,
                    label="Person",
                    properties={
                        "name": p_name,
                        "role": p_role,
                        "department": p_department,
                        "user_id": user_id,
                        "aliases": resolved.aliases,
                        "external_ids": resolved.external_ids,
                        "meeting_ids": [meeting_id],
                        "meeting_titles": [meeting_title] if meeting_title else [],
                    },
                    access_group=access_group,
                )
                result["entities_created"] += 1

                # Temporal: версионирование новой сущности
                try:
                    await temporal_tracker.create_version(
                        entity_id=p_id,
                        entity_type="person",
                        data={
                            "name": p_name,
                            "role": p_role,
                            "department": p_department,
                            "aliases": resolved.aliases,
                        },
                        changed_by="system",
                        change_type="CREATE",
                        source=meeting_id,
                        occurred_at=_occurred_at,
                    )
                except Exception:
                    logger.debug("suppressed exception", exc_info=True)
                _track_fact(p_id, "role", p_role)
                _track_fact(p_id, "department", p_department)
            else:
                # Обновляем существующий узел (добавляем роль/отдел если не было)
                updates = {}
                try:
                    # Получаем текущие данные узла (работает для обоих бэкендов)
                    existing = {}
                    if graph_builder.use_networkx:
                        existing = graph_builder.nx_graph.nodes.get(p_id, {})
                    else:
                        # Neo4j: получаем данные через get_node_data или get_all_nodes_async
                        try:
                            _node = await graph_builder.get_node_by_id(p_id)
                            if _node:
                                existing = _node
                        except Exception:
                            logger.debug("suppressed exception", exc_info=True)

                    # Guard: резолвер вернул ЧУЖОЙ узел (общий Neo4j) —
                    # пропускаем участника целиком: не обновляем чужой узел,
                    # не пишем его свойства в свой temporal, не строим рёбра.
                    _ex_tenant = existing.get("tenant_id") if isinstance(existing, dict) else None
                    if _ex_tenant not in (None, "") and str(_ex_tenant) not in _own_tenants:
                        logger.warning(
                            f"🛑 cross-tenant resolve заблокирован: {p_id} "
                            f"(tenant {_ex_tenant}) для user {user_id}")
                        if p_id in _cooc_node_ids:
                            _cooc_node_ids.remove(p_id)
                        result["cross_tenant_blocked"] = result.get(
                            "cross_tenant_blocked", 0) + 1
                        continue

                    if p_role and not existing.get("role"):
                        updates["role"] = p_role
                    if p_department and not existing.get("department"):
                        updates["department"] = p_department

                    # FIX #3: Накапливаем meeting_ids на узле Person
                    _node_meeting_ids = existing.get("meeting_ids", [])
                    if isinstance(_node_meeting_ids, str):
                        _node_meeting_ids = [_node_meeting_ids] if _node_meeting_ids else []
                    _node_meeting_titles = existing.get("meeting_titles", [])
                    if isinstance(_node_meeting_titles, str):
                        _node_meeting_titles = [_node_meeting_titles] if _node_meeting_titles else []

                    if meeting_id not in _node_meeting_ids:
                        _node_meeting_ids.append(meeting_id)
                        updates["meeting_ids"] = _node_meeting_ids
                    if meeting_title and meeting_title not in _node_meeting_titles:
                        _node_meeting_titles.append(meeting_title)
                        updates["meeting_titles"] = _node_meeting_titles

                    if updates:
                        if graph_builder.use_networkx:
                            for k, v in updates.items():
                                graph_builder.nx_graph.nodes[p_id][k] = v
                        else:
                            # Neo4j: обновляем через create_node (MERGE обновит существующий)
                            await graph_builder.create_node(
                                node_id=p_id,
                                label="Person",
                                properties={
                                    "name": resolved.name or p_name,
                                    **updates,
                                    "user_id": user_id,
                                },
                                access_group=access_group,
                            )

                        # Temporal: версионирование обновления
                        try:
                            await temporal_tracker.create_version(
                                entity_id=p_id,
                                entity_type="person",
                                data={**existing, **updates},
                                changed_by="system",
                                change_type="UPDATE",
                                source=meeting_id,
                                occurred_at=_occurred_at,
                            )
                        except Exception:
                            logger.debug("suppressed exception", exc_info=True)
                        for _uf, _uv in (updates or {}).items():
                            _track_fact(p_id, str(_uf), _uv)
                except Exception as _upd_err:
                    logger.debug(f"Person update failed for {p_id}: {_upd_err}")
                result["entities_merged"] = result.get("entities_merged", 0) + 1
                logger.debug(f"🔗 Entity resolved: {p_name} -> {resolved.name} (confidence: {resolved.confidence:.2f}, matched_by: {resolved.matched_by})")

            # Temporal: timeline event для участия во встрече
            try:
                meeting_datetime = datetime.now(timezone.utc)
                if meeting_date:
                    try:
                        meeting_datetime = datetime.fromisoformat(meeting_date.replace('Z', '+00:00'))
                    except Exception:
                        logger.debug("suppressed exception", exc_info=True)

                await temporal_tracker.add_timeline_event(
                    entity_id=p_id,
                    entity_type="person",
                    event_type="participated",
                    event_time=meeting_datetime,
                    event_data={"role": p_role, "meeting_title": meeting_title},
                    meeting_id=meeting_id,
                    description=f"Участие во встрече: {meeting_title}"
                )
                result["timeline_events_added"] = result.get("timeline_events_added", 0) + 1
            except Exception:
                logger.debug("suppressed exception", exc_info=True)

            _participation_desc_parts = [
                f"{resolved.name} участвовал во встрече '{meeting_title}'" if meeting_title else resolved.name,
                f"как {p_role}" if p_role else "",
                f"({p_department})" if p_department else "",
            ]
            await graph_builder.create_relationship(
                from_id=p_id,
                to_id=meeting_node_id,
                rel_type="PARTICIPATED_IN",
                properties={
                    **_temporal_base,
                    "description": " ".join(p for p in _participation_desc_parts if p),
                    "role": p_role,
                    "department": p_department,
                    "confidence": float(participant.get("confidence", 0.8)),
                }
            )
            result["relationships_created"] += 1

            # ===== ОРГСТРУКТУРА: «кто кем является» =====
            # Раньше participant["relationships"] и department-как-узел
            # выбрасывались, поэтому графа оргструктуры не было. Теперь строим:
            #   Person -MEMBER_OF-> Department (членство),
            #   Person -REPORTS_TO-> Person (reports_to; "manages" — инверсия,
            #   т.к. в схеме нет MANAGES). collaborates пропускаем (шум —
            #   Hebbian уже связывает по co-mention). Всё best-effort.
            try:
                # 1) Членство в департаменте (узел Department + MEMBER_OF)
                if p_department and str(p_department).strip():
                    dep_name = str(p_department).strip()
                    dep_resolved = await entity_resolver.resolve(
                        name=dep_name,
                        entity_type="department",
                        organization_id=user_id,
                    )
                    dep_id = dep_resolved.canonical_id
                    if dep_resolved.is_new:
                        await graph_builder.create_node(
                            node_id=dep_id,
                            label="Department",
                            properties={
                                "department_id": dep_id,
                                "name": dep_name,
                                "user_id": user_id,
                            },
                            access_group=access_group,
                        )
                        result["entities_created"] += 1
                    if await graph_builder.create_relationship(
                        from_id=p_id, to_id=dep_id, rel_type="MEMBER_OF",
                        properties={**_temporal_base, "source": "participant_department"},
                    ):
                        result["relationships_created"] += 1

                # 2) Линии подчинения из participant["relationships"]
                for _rel in (participant.get("relationships") or []):
                    if not isinstance(_rel, dict):
                        continue
                    other_name = (_rel.get("with") or "").strip()
                    rtype = (_rel.get("type") or "").strip().lower()
                    if not other_name or rtype not in ("reports_to", "manages"):
                        continue
                    if other_name.lower() == (p_name or "").strip().lower():
                        continue
                    other_resolved = await entity_resolver.resolve(
                        name=other_name, entity_type="person",
                        organization_id=user_id,
                    )
                    other_id = other_resolved.canonical_id
                    if other_resolved.is_new:
                        await graph_builder.create_node(
                            node_id=other_id, label="Person",
                            properties={
                                "name": other_name, "user_id": user_id,
                                "aliases": other_resolved.aliases,
                                "inferred_from_relationship": True,
                            },
                            access_group=access_group,
                        )
                        result["entities_created"] += 1
                    # reports_to: p → other; manages: other → p (инверсия)
                    src, dst = (p_id, other_id) if rtype == "reports_to" else (other_id, p_id)
                    if src != dst and await graph_builder.create_relationship(
                        from_id=src, to_id=dst, rel_type="REPORTS_TO",
                        properties={**_temporal_base, "source": f"participant_{rtype}",
                                    "confidence": float(participant.get("confidence", 0.6))},
                    ):
                        result["relationships_created"] += 1
            except Exception as _org_err:
                logger.debug(f"org structure wiring skipped for {p_name}: {_org_err}")

            # ===== ИНДЕКСАЦИЯ УЧАСТНИКА В ВЕКТОРА =====
            # FIX #3: Накапливаем meeting_ids на графовом узле Person
            _existing_meeting_ids = []
            _existing_meeting_titles = []
            try:
                _node_data = graph_builder.get_node_data(p_id)
                if _node_data:
                    _existing_meeting_ids = _node_data.get("meeting_ids", [])
                    _existing_meeting_titles = _node_data.get("meeting_titles", [])
            except Exception:
                pass  # Первая индексация

            # Добавляем текущую встречу к списку
            if meeting_id not in _existing_meeting_ids:
                _existing_meeting_ids.append(meeting_id)
            if meeting_title and meeting_title not in _existing_meeting_titles:
                _existing_meeting_titles.append(meeting_title)

            # Embedding text включает ВСЕ встречи
            participant_text_parts = [
                resolved.name,
                f"Роль: {p_role}" if p_role else "",
                f"Отдел: {p_department}" if p_department else "",
                f"Встречи: {', '.join(_existing_meeting_titles[-5:])}" if _existing_meeting_titles else "",
                f"Также известен как: {', '.join(resolved.aliases)}" if resolved.aliases else "",
            ]
            participant_text = " | ".join(filter(None, participant_text_parts))

            try:
                await vector_indexer.upsert_vector_public(
                    collection=vector_indexer.collections["participants"],
                    vector_id=p_id,
                    text=participant_text,
                    metadata={
                        "participant_id": p_id,
                        "name": resolved.name,
                        "original_name": p_name,
                        "role": p_role,
                        "department": p_department,
                        "user_id": user_id,
                        "type": "participant",
                        "meeting_id": meeting_id,
                        "meeting_ids": _existing_meeting_ids,
                        "meeting_titles": _existing_meeting_titles,
                        "aliases": resolved.aliases,
                    },
                )
                result["participants_indexed"] = result.get("participants_indexed", 0) + 1

                # Индексируем в BM25
                bm25_searcher.add_document(
                    doc_id=p_id,
                    text=participant_text,
                    metadata={"type": "person", "meeting_id": meeting_id}
                )
            except Exception as ve:
                logger.warning(f"Vector upsert failed for participant {p_id}: {ve}")

        # Спикер-атрибуция: резолвер имени → Person node id. Экстракторы уже
        # возвращают decision_maker_resolved / assignee_name (issue #112), но
        # раньше эти поля выбрасывались и рёбра к человеку не строились —
        # отсюда «кто сказал/решил/чья задача» не работало. Создаём узел, если
        # имя новое (помечаем inferred_from_attribution), иначе дедуп через
        # EntityResolver (тот же canonical_id, что у участника).
        _attr_pid_cache: dict = {}

        async def _resolve_person_id(name):
            nm = (name or "").strip()
            if not nm or nm.lower() in ("null", "none", "unknown", "n/a", "—"):
                return None
            key = nm.lower()
            if key in _attr_pid_cache:
                return _attr_pid_cache[key]
            try:
                r = await entity_resolver.resolve(
                    name=nm, entity_type="person", organization_id=user_id)
                pid = r.canonical_id
                if getattr(r, "is_new", False):
                    await graph_builder.create_node(
                        node_id=pid, label="Person",
                        properties={
                            "name": nm, "user_id": user_id,
                            "meeting_ids": [meeting_id],
                            "inferred_from_attribution": True,
                        },
                        access_group=access_group,
                    )
                    result["entities_created"] += 1
                _attr_pid_cache[key] = pid
                return pid
            except Exception:
                logger.debug("attribution person resolve failed", exc_info=True)
                return None

        # Решения
        for i, decision in enumerate(agent_results.get("decisions", [])):
            # DecisionAgent нормализует как decision_id + decision_summary + details
            d_key = _first_text(decision.get("decision_id"), decision.get("id"), i)
            d_id = f"decision_{meeting_id}_{d_key}"
            d_summary = _first_text(
                decision.get("decision_summary"),
                decision.get("summary"),
                decision.get("title"),
                decision.get("text"),
                decision.get("content"),
            )
            d_details = _first_text(decision.get("details"), decision.get("description"), decision.get("context"))

            # Имя решившего: явное поле, иначе резолвим speaker-label через карту.
            _dm_name = decision.get("decision_maker_resolved")
            if not _dm_name and decision.get("decision_maker_speaker"):
                _dm_name = _speaker_map.get(decision.get("decision_maker_speaker"))

            # «Почему»/последствия/цитата — извлекались агентом, но раньше
            # не сохранялись (терялась причинность решений). Скаляры для
            # Neo4j: map-свойства он не хранит, поэтому уплощаем.
            _d_bi = decision.get("business_impact") or {}
            if not isinstance(_d_bi, dict):
                _d_bi = {}
            _d_tl = decision.get("timeline") or {}
            if not isinstance(_d_tl, dict):
                _d_tl = {}
            _d_actions = decision.get("action_items") or []
            _d_actions_txt = "; ".join(
                (a.get("action") if isinstance(a, dict) else str(a))
                for a in _d_actions if a)[:500]

            await graph_builder.create_node(
                node_id=d_id,
                label="Decision",
                properties={
                    "name": d_summary or d_id,
                    "summary": d_summary,
                    "details": d_details,
                    "decision_category": decision.get("decision_category") or decision.get("category") or "",
                    "confidence": float(decision.get("confidence") or 0),
                    # Спикер-атрибуция (issue #112): кто принял решение.
                    "decision_maker_speaker": decision.get("decision_maker_speaker") or "",
                    "decision_maker_resolved": _dm_name or "",
                    "attribution_confidence": decision.get("attribution_confidence") or "low",
                    # «Почему» и последствия (сохраняются впервые):
                    "reasoning": str(decision.get("reasoning") or "")[:1500],
                    "quote": str(decision.get("quote") or "")[:500],
                    "impact_scope": str(_d_bi.get("scope") or ""),
                    "impact_urgency": str(_d_bi.get("urgency") or ""),
                    "impact_risk_level": str(_d_bi.get("risk_level") or ""),
                    "deadline": str(_d_tl.get("deadline") or ""),
                    "action_items_text": _d_actions_txt,
                    "meeting_id": meeting_id,
                    "user_id": user_id
                },
                access_group=access_group,
            )
            result["entities_created"] += 1

            # Ребро к человеку, принявшему решение: Person -DECIDED-> Decision.
            # Направление Person→Decision (исходящее) — чтобы карточка человека
            # подхватывала его решения. _dm_name вычислен выше (с fallback на
            # speaker-карту).
            if _dm_name:
                _dm_pid = await _resolve_person_id(_dm_name)
                if _dm_pid:
                    try:
                        await graph_builder.create_relationship(
                            from_id=_dm_pid, to_id=d_id, rel_type="DECIDED",
                            properties={
                                **_temporal_base,
                                "speaker_label": decision.get("decision_maker_speaker") or "",
                                "attribution_confidence": decision.get("attribution_confidence") or "low",
                            },
                        )
                        result["relationships_created"] += 1
                    except Exception:
                        logger.debug("DECIDED edge failed", exc_info=True)

            await graph_builder.create_relationship(
                from_id=meeting_node_id,
                to_id=d_id,
                rel_type="HAS_DECISION",
                properties={
                    **_temporal_base,
                    "description": f"Решение: {d_summary[:120]}" if d_summary else "Решение",
                    "confidence": float(decision.get("confidence", 0.8)),
                    "category": decision.get("decision_category") or decision.get("category") or "",
                }
            )
            result["relationships_created"] += 1

            # Индексация решения: summary + details + ПОЧЕМУ + цитата —
            # чтобы вопрос «почему решили X» и «на каком основании» находил
            # решение семантически (раньше в вектор шли только summary+details)
            _decision_embed_parts = [p for p in [
                d_summary, d_details,
                str(decision.get("reasoning") or ""),
                str(decision.get("quote") or ""),
            ] if p]
            _decision_embed_text = ". ".join(_decision_embed_parts) if _decision_embed_parts else d_id

            # Supersede-цепочка: ищем похожее ПРЕДЫДУЩЕЕ решение (тот же предмет
            # из ДРУГОЙ, более ранней встречи) и ставим ребро Decision_new
            # -SUPERSEDES-> Decision_old. Делаем ДО upsert'а текущего решения,
            # чтобы оно само не попало в результаты.
            #
            # Что это ребро значит честно: «то же самое решали раньше», а не
            # «новое решение отменило старое» — сигнала об отмене в разборе
            # встречи нет, есть только семантическое сходство. Поэтому в ребро
            # пишем basis и similarity: потребитель видит основание связи.
            #
            # Хронология проверяется ЯВНО. Раньше условие было только «другая
            # встреча», и при досинхронизации архива стрелка могла указать на
            # более позднее решение — цепочка эволюции читалась задом наперёд.
            try:
                if _decision_embed_text and len(_decision_embed_text) > 15:
                    _sim = await vector_indexer.search(
                        query=_decision_embed_text,
                        collections=[vector_indexer.collections["decisions"]],
                        limit=3, score_threshold=0.90)
                    for _cand in _sim or []:
                        _cmeta = _cand.get("payload") or _cand.get("metadata") or {}
                        _cid = _cand.get("id") or _cmeta.get("vector_id")
                        _cmid = _cmeta.get("meeting_id")
                        if not (_cid and _cid != d_id and _cmid and _cmid != meeting_id):
                            continue
                        _order = _decision_order(meeting_date, _cmeta.get("meeting_date"))
                        if _order == "candidate_is_newer":
                            # Кандидат позже нашего решения — стрелка смотрела
                            # бы в будущее. Не связываем, пробуем следующий.
                            continue
                        await graph_builder.create_relationship(
                            from_id=d_id, to_id=_cid, rel_type="SUPERSEDES",
                            properties={
                                **_temporal_base,
                                "similarity": float(_cand.get("score") or 0),
                                "basis": "semantic_similarity",
                                # false → у кандидата не было даты (старые
                                # векторы): связь оставили, но не выдаём её за
                                # проверенную хронологию.
                                "chronology_verified": _order == "candidate_is_older",
                                "superseded_date": str(_cmeta.get("meeting_date") or ""),
                            },
                        )
                        result["relationships_created"] += 1
                        break  # одна (самая похожая) цепочка
            except Exception:
                logger.debug("decision supersede link skipped", exc_info=True)

            try:
                await vector_indexer.upsert_vector_public(
                    collection=vector_indexer.collections["decisions"],
                    vector_id=d_id,
                    text=_decision_embed_text,
                    metadata={
                        "meeting_id": meeting_id,
                        "user_id": user_id,
                        "type": "decision",
                        # Дата встречи в payload — чтобы будущие решения могли
                        # проверить хронологию перед тем, как объявить это
                        # решение предыдущим звеном цепочки. Заодно её берёт
                        # temporal re-rank (ключ meeting_date у него в списке).
                        "meeting_date": meeting_date or "",
                        "decision_category": decision.get("decision_category") or decision.get("category") or "",
                    },
                )
                # BM25
                bm25_searcher.add_document(
                    doc_id=d_id,
                    text=_first_text(d_summary, d_details, d_id),
                    metadata={"type": "decision", "meeting_id": meeting_id,
                              "meeting_date": meeting_date or ""}
                )
            except Exception as ve:
                logger.warning(f"Vector upsert failed for decision {d_id}: {ve}")

            # Temporal: версионирование и timeline для решения
            try:
                meeting_datetime = datetime.now(timezone.utc)
                if meeting_date:
                    try:
                        meeting_datetime = datetime.fromisoformat(meeting_date.replace('Z', '+00:00'))
                    except Exception:
                        logger.debug("suppressed exception", exc_info=True)

                await temporal_tracker.create_version(
                    entity_id=d_id,
                    entity_type="decision",
                    data={
                        "summary": d_summary,
                        "details": d_details,
                        "category": decision.get("decision_category") or decision.get("category") or "",
                        "confidence": float(decision.get("confidence") or 0),
                    },
                    changed_by="system",
                    change_type="CREATE",
                    source=meeting_id,
                    occurred_at=_occurred_at,
                )

                await temporal_tracker.add_timeline_event(
                    entity_id=d_id,
                    entity_type="decision",
                    event_type="created",
                    event_time=meeting_datetime,
                    event_data={
                        "category": decision.get("decision_category") or decision.get("category") or "",
                    },
                    meeting_id=meeting_id,
                    description=f"Решение принято: {d_summary[:100] if d_summary else d_id}"
                )
                result["timeline_events_added"] = result.get("timeline_events_added", 0) + 1
            except Exception:
                logger.debug("suppressed exception", exc_info=True)

        # Задачи
        for i, task in enumerate(agent_results.get("tasks", [])):
            # TaskAgent нормализует как task_id + title + description
            t_key = _first_text(task.get("task_id"), task.get("id"), i)
            t_id = f"task_{meeting_id}_{t_key}"
            t_title = _first_text(task.get("title"), task.get("task_title"), task.get("name"))
            t_desc = _first_text(task.get("description"), task.get("details"), task.get("text"))

            await graph_builder.create_node(
                node_id=t_id,
                label="Task",
                properties={
                    "name": t_title or t_desc or t_id,
                    "title": t_title or (t_desc[:80] if t_desc else ""),
                    "description": t_desc,
                    "assignee": task.get("assignee", ""),
                    "deadline": task.get("deadline", ""),
                    "priority": task.get("priority", "medium"),
                    "status": task.get("status", "open"),
                    "task_type": task.get("task_type") or task.get("type") or "",
                    # Спикер-атрибуция (issue #112): кому поручено.
                    "assignee_speaker": task.get("assignee_speaker") or "",
                    "assignment_confidence": task.get("assignment_confidence") or "low",
                    "meeting_id": meeting_id,
                    "user_id": user_id
                },
                access_group=access_group,
            )
            result["entities_created"] += 1

            # Ребро к исполнителю: Person -ASSIGNED_TO-> Task (исходящее от
            # человека → карточка человека подхватит его задачи).
            _asg = task.get("assignee")
            _asg_name = (task.get("assignee_name")
                         or (_asg.get("name") if isinstance(_asg, dict) else _asg if isinstance(_asg, str) else ""))
            # Fallback: только speaker-label («Speaker 2») → имя через карту.
            if not _asg_name and task.get("assignee_speaker"):
                _asg_name = _speaker_map.get(task.get("assignee_speaker"))
            if _asg_name:
                _asg_pid = await _resolve_person_id(_asg_name)
                if _asg_pid:
                    try:
                        await graph_builder.create_relationship(
                            from_id=_asg_pid, to_id=t_id, rel_type="ASSIGNED_TO",
                            properties={
                                **_temporal_base,
                                "speaker_label": task.get("assignee_speaker") or "",
                                "assignment_confidence": task.get("assignment_confidence") or "low",
                            },
                        )
                        result["relationships_created"] += 1
                    except Exception:
                        logger.debug("ASSIGNED_TO edge failed", exc_info=True)

            _task_desc = f"Задача: {t_title}" if t_title else "Задача"
            if task.get("assignee"):
                _assignee_name = task["assignee"].get("name", "") if isinstance(task["assignee"], dict) else str(task["assignee"])
                if _assignee_name:
                    _task_desc += f" (ответственный: {_assignee_name})"
            await graph_builder.create_relationship(
                from_id=meeting_node_id,
                to_id=t_id,
                rel_type="HAS_TASK",
                properties={
                    **_temporal_base,
                    "description": _task_desc,
                    "priority": task.get("priority", "medium"),
                    "status": task.get("status", "new"),
                    "confidence": float(task.get("confidence", 0.8)),
                }
            )
            result["relationships_created"] += 1

            # Индексация задачи (FIX: конкатенация title+description вместо first_non_empty)
            _task_embed_parts = [p for p in [t_title, t_desc] if p]
            _task_embed_text = ": ".join(_task_embed_parts) if _task_embed_parts else t_id
            try:
                await vector_indexer.upsert_vector_public(
                    collection=vector_indexer.collections["tasks"],
                    vector_id=t_id,
                    text=_task_embed_text,
                    metadata={
                        "meeting_id": meeting_id,
                        "user_id": user_id,
                        "type": "task",
                        "status": task.get("status", ""),
                        "priority": task.get("priority", ""),
                    },
                )
                # BM25
                bm25_searcher.add_document(
                    doc_id=t_id,
                    text=_first_text(t_title, t_desc, t_id),
                    metadata={"type": "task", "meeting_id": meeting_id}
                )
            except Exception as ve:
                logger.warning(f"Vector upsert failed for task {t_id}: {ve}")

            # Temporal: версионирование и timeline для задачи
            try:
                meeting_datetime = datetime.now(timezone.utc)
                if meeting_date:
                    try:
                        meeting_datetime = datetime.fromisoformat(meeting_date.replace('Z', '+00:00'))
                    except Exception:
                        logger.debug("suppressed exception", exc_info=True)

                await temporal_tracker.create_version(
                    entity_id=t_id,
                    entity_type="task",
                    data={
                        "title": t_title,
                        "description": t_desc,
                        "assignee": task.get("assignee", ""),
                        "deadline": task.get("deadline", ""),
                        "priority": task.get("priority", "medium"),
                        "status": task.get("status", "open"),
                    },
                    changed_by="system",
                    change_type="CREATE",
                    source=meeting_id,
                    occurred_at=_occurred_at,
                )
                _track_fact(t_id, "deadline", task.get("deadline"))
                _track_fact(t_id, "assignee", task.get("assignee"))
                _track_fact(t_id, "status", task.get("status"))

                await temporal_tracker.add_timeline_event(
                    entity_id=t_id,
                    entity_type="task",
                    event_type="created",
                    event_time=meeting_datetime,
                    event_data={
                        "assignee": task.get("assignee", ""),
                        "priority": task.get("priority", "medium"),
                    },
                    meeting_id=meeting_id,
                    description=f"Задача создана: {t_title}"
                )
                result["timeline_events_added"] = result.get("timeline_events_added", 0) + 1
            except Exception:
                logger.debug("suppressed exception", exc_info=True)

        # Идеи (IdeaAgent извлекал их с автором, но раньше они ВЫБРАСЫВАЛИСЬ —
        # поэтому «кому принадлежит идея X» не работало). Пишем узлы Idea +
        # ребро Person -PROPOSED-> Idea (авторство) + индексацию.
        for i, idea in enumerate(agent_results.get("ideas", [])):
            i_key = _first_text(idea.get("idea_id"), idea.get("id"), i)
            i_id = f"idea_{meeting_id}_{i_key}"
            i_summary = _first_text(idea.get("idea_summary"), idea.get("summary"),
                                    idea.get("title"), idea.get("text"))
            if not i_summary:
                continue
            _author = idea.get("author")
            if not _author and idea.get("author_speaker"):
                _author = _speaker_map.get(idea.get("author_speaker"))
            try:
                await graph_builder.create_node(
                    node_id=i_id, label="Idea",
                    properties={
                        "name": i_summary[:120], "summary": i_summary,
                        "category": idea.get("idea_category") or idea.get("category") or "",
                        "author": _author or "",
                        "novelty": ((idea.get("innovation_assessment") or {}).get("novelty_level")
                                    if isinstance(idea.get("innovation_assessment"), dict) else ""),
                        "meeting_id": meeting_id, "user_id": user_id,
                    },
                    access_group=access_group,
                )
                result["entities_created"] += 1
                await graph_builder.create_relationship(
                    from_id=meeting_node_id, to_id=i_id, rel_type="HAS_IDEA",
                    properties={**_temporal_base, "description": f"Идея: {i_summary[:100]}"},
                )
                result["relationships_created"] += 1
                # Авторство: Person -PROPOSED-> Idea (исходящее → карточка
                # человека подхватит его идеи; «чья идея» резолвится).
                if _author:
                    _au_pid = await _resolve_person_id(_author)
                    if _au_pid:
                        await graph_builder.create_relationship(
                            from_id=_au_pid, to_id=i_id, rel_type="PROPOSED",
                            properties={**_temporal_base},
                        )
                        result["relationships_created"] += 1
                # Индексация идеи в поиск.
                try:
                    await vector_indexer.upsert_vector_public(
                        collection=vector_indexer.collections.get("entities", "entities"),
                        vector_id=i_id, text=i_summary,
                        metadata={"meeting_id": meeting_id, "user_id": user_id,
                                  "type": "idea", "author": _author or ""},
                    )
                    bm25_searcher.add_document(
                        doc_id=i_id, text=i_summary,
                        metadata={"type": "idea", "meeting_id": meeting_id})
                except Exception as ve:
                    logger.debug(f"idea index skipped: {ve}")
            except Exception as e:
                logger.warning(f"idea node failed: {e}")

        # Мнения (OpinionAgent извлекал с автором+sentiment, но они тоже
        # выбрасывались). Узлы Opinion + ребро Person -EXPRESSED-> Opinion.
        for i, op in enumerate(agent_results.get("opinions", [])):
            o_key = _first_text(op.get("opinion_id"), op.get("id"), i)
            o_id = f"opinion_{meeting_id}_{o_key}"
            o_summary = _first_text(op.get("opinion_summary"), op.get("summary"), op.get("text"))
            if not o_summary:
                continue
            _holder = op.get("author")
            if not _holder and op.get("author_speaker"):
                _holder = _speaker_map.get(op.get("author_speaker"))
            _sent = ((op.get("sentiment_analysis") or {}).get("primary_sentiment")
                     if isinstance(op.get("sentiment_analysis"), dict) else "")
            try:
                await graph_builder.create_node(
                    node_id=o_id, label="Opinion",
                    properties={
                        "name": o_summary[:120], "summary": o_summary,
                        "topic": op.get("topic") or "",
                        "category": op.get("opinion_category") or "",
                        "sentiment": _sent or "", "author": _holder or "",
                        "meeting_id": meeting_id, "user_id": user_id,
                    },
                    access_group=access_group,
                )
                result["entities_created"] += 1
                await graph_builder.create_relationship(
                    from_id=meeting_node_id, to_id=o_id, rel_type="HAS_OPINION",
                    properties={**_temporal_base, "description": f"Мнение: {o_summary[:100]}"},
                )
                result["relationships_created"] += 1
                if _holder:
                    _h_pid = await _resolve_person_id(_holder)
                    if _h_pid:
                        await graph_builder.create_relationship(
                            from_id=_h_pid, to_id=o_id, rel_type="EXPRESSED",
                            properties={**_temporal_base, "sentiment": _sent or ""},
                        )
                        result["relationships_created"] += 1
                try:
                    await vector_indexer.upsert_vector_public(
                        collection=vector_indexer.collections.get("entities", "entities"),
                        vector_id=o_id, text=o_summary,
                        metadata={"meeting_id": meeting_id, "user_id": user_id,
                                  "type": "opinion", "author": _holder or ""},
                    )
                    bm25_searcher.add_document(
                        doc_id=o_id, text=o_summary,
                        metadata={"type": "opinion", "meeting_id": meeting_id})
                except Exception as ve:
                    logger.debug(f"opinion index skipped: {ve}")
            except Exception as e:
                logger.warning(f"opinion node failed: {e}")

        # Статусы проектов (ProjectStatusAgent извлекал, но выбрасывалось).
        # Это ТЕМПОРАЛЬНО: обновляем Project + пишем событие в таймлайн →
        # «как менялся статус проекта со временем».
        for i, ps in enumerate(agent_results.get("project_statuses", [])):
            p_name = _first_text(ps.get("project_name"), ps.get("name"))
            if not p_name:
                continue
            _sa = ps.get("status_assessment") or {}
            _pstatus = _sa.get("current_status") or ps.get("status") or ""
            _phealth = _sa.get("health_indicator") or ""
            _pprogress = _sa.get("progress_percentage")

            # Бюджет проекта. ProjectStatusAgent извлекал его всё это время
            # (resource_analysis.budget_info), но значение выбрасывалось —
            # поэтому «бюджет хранится цепочкой версий» было обещанием без
            # опоры. Берём как сказано на встрече и разбираем в число, чтобы
            # версии были сравнимы («2 млн ₽» против «2 500 000 руб»).
            _rb = (ps.get("resource_analysis") or {}).get("budget_info") or {}
            _pbudget_text = str(_rb.get("estimated_budget") or "").strip()
            _pbudget_status = str(_rb.get("budget_status") or "").strip()
            _pbudget = parse_money(_pbudget_text)
            # Формулировка без суммы («уточняется») в историю не идёт: это
            # не смена бюджета, а её отсутствие.
            _pbudget_fact = _pbudget["normalized"] if _pbudget else ""
            try:
                resolved = await entity_resolver.resolve(
                    name=p_name, entity_type="project", organization_id=user_id,
                    properties={"status": _pstatus, "health": _phealth})
                proj_id = resolved.canonical_id
                if getattr(resolved, "is_new", False):
                    await graph_builder.create_node(
                        node_id=proj_id, label="Project",
                        properties={"name": p_name, "status": _pstatus,
                                    "health": _phealth, "progress": _pprogress,
                                    **_budget_props(_pbudget, _pbudget_text,
                                                    _pbudget_status),
                                    "user_id": user_id, "meeting_ids": [meeting_id]},
                        access_group=access_group,
                    )
                    result["entities_created"] += 1
                else:
                    try:
                        await graph_builder.update_node(proj_id, {
                            "status": _pstatus, "health": _phealth,
                            "progress": _pprogress,
                            **_budget_props(_pbudget, _pbudget_text,
                                            _pbudget_status)})
                    except Exception:
                        logger.debug("project status update skipped", exc_info=True)
                try:
                    _mdt = datetime.now(timezone.utc)
                    if meeting_date:
                        try:
                            _mdt = datetime.fromisoformat(meeting_date.replace('Z', '+00:00'))
                        except Exception:
                            pass
                    _desc = f"Статус проекта: {_pstatus}"
                    if _pprogress is not None:
                        _desc += f" ({_phealth}, {_pprogress}%)"
                    await temporal_tracker.add_timeline_event(
                        entity_id=proj_id, entity_type="project",
                        event_type="status_update", event_time=_mdt,
                        event_data={"status": _pstatus, "health": _phealth,
                                    "progress": _pprogress},
                        meeting_id=meeting_id, description=_desc)
                    result["timeline_events_added"] = result.get("timeline_events_added", 0) + 1
                except Exception:
                    logger.debug("project status timeline skipped", exc_info=True)
                # Версия проекта — чтобы «Эволюция объекта» видела проект как
                # первоклассный объект (диффы status/health/progress во времени,
                # а не только timeline-события). Паттерн как у person/decision/task.
                try:
                    await temporal_tracker.create_version(
                        entity_id=proj_id, entity_type="project",
                        data={"name": p_name, "status": _pstatus,
                              "health": _phealth, "progress": _pprogress,
                              "budget": _pbudget_fact,
                              "budget_amount": _pbudget["amount"] if _pbudget else None},
                        changed_by="system",
                        change_type="CREATE" if getattr(resolved, "is_new", False) else "UPDATE",
                        source=meeting_id,
                        occurred_at=_occurred_at)
                except Exception:
                    logger.debug("project version skipped", exc_info=True)
                _track_fact(proj_id, "budget", _pbudget_fact)
                _track_fact(proj_id, "status", _pstatus)
                _track_fact(proj_id, "health", _phealth)
                _track_fact(proj_id, "progress", _pprogress)
            except Exception as e:
                logger.warning(f"project status failed: {e}")

        # Fact-checks → Contradiction-узлы. FactCheckerAgent находил
        # противоречия/несоответствия с источниками («кто что сказал»), но они
        # выбрасывались. Тип Contradiction в графе уже читается проактивным
        # сканом конфликтов + очередью human-review (needs_human_review).
        for i, fc in enumerate(agent_results.get("fact_checks", [])):
            fc_desc = _first_text(fc.get("description"), fc.get("summary"))
            if not fc_desc:
                continue
            fc_id = f"contradiction_{meeting_id}_{_first_text(fc.get('check_id'), fc.get('id'), i)}"
            _ctype = fc.get("check_type") or "inconsistency"
            _sev = fc.get("severity") or "medium"
            _conflicting = fc.get("conflicting_statements") or []
            try:
                await graph_builder.create_node(
                    node_id=fc_id, label="Contradiction",
                    properties={
                        "name": fc_desc[:120], "description": fc_desc,
                        "check_type": _ctype, "severity": _sev,
                        "conflicting_statements": (
                            json.dumps(_conflicting, ensure_ascii=False) if _conflicting else ""),
                        "needs_human_review": _sev in ("critical", "high"),
                        "meeting_id": meeting_id, "user_id": user_id,
                    },
                    access_group=access_group,
                )
                result["entities_created"] += 1
                await graph_builder.create_relationship(
                    from_id=meeting_node_id, to_id=fc_id, rel_type="HAS_CONTRADICTION",
                    properties={**_temporal_base, "severity": _sev},
                )
                result["relationships_created"] += 1
                # Привязка к людям-источникам конфликтующих утверждений.
                for _st in (_conflicting[:4] if isinstance(_conflicting, list) else []):
                    _src = _st.get("source") if isinstance(_st, dict) else None
                    _src_name = _src if isinstance(_src, str) else (
                        _src.get("name") if isinstance(_src, dict) else None)
                    if _src_name and _src_name.startswith("Speaker"):
                        _src_name = _speaker_map.get(_src_name) or _src_name
                    if _src_name:
                        _sp = await _resolve_person_id(_src_name)
                        if _sp:
                            await graph_builder.create_relationship(
                                from_id=_sp, to_id=fc_id, rel_type="INVOLVED_IN",
                                properties={**_temporal_base},
                            )
                            result["relationships_created"] += 1
                try:
                    await vector_indexer.upsert_vector_public(
                        collection=vector_indexer.collections.get("entities", "entities"),
                        vector_id=fc_id, text=fc_desc,
                        metadata={"meeting_id": meeting_id, "user_id": user_id,
                                  "type": "contradiction", "severity": _sev},
                    )
                    bm25_searcher.add_document(
                        doc_id=fc_id, text=fc_desc,
                        metadata={"type": "contradiction", "meeting_id": meeting_id})
                except Exception as ve:
                    logger.debug(f"contradiction index skipped: {ve}")
            except Exception as e:
                logger.warning(f"contradiction node failed: {e}")

        # Психологические профили. PsychologicalAnalyzer гонялся (флаг ON), но
        # узлы PsychologicalProfile не создавались → 4 читателя (карточка
        # человека, psych_to_person, поиск, consulting) получали пусто. Создаём
        # узлы в формате, который ждёт reader, + ребро Person -HAS_PROFILE->.
        _psych = analysis_result.get("psychological_analysis") or {}
        for i, prof in enumerate(_psych.get("personality_profiles", [])):
            pd = prof if isinstance(prof, dict) else getattr(prof, "__dict__", {}) or {}
            pname = (pd.get("name") or "").strip()
            if not pname:
                continue
            pp_id = f"psychprofile_{meeting_id}_{i}"
            try:
                await graph_builder.create_node(
                    node_id=pp_id, label="PsychologicalProfile",
                    properties={
                        "name": pname,
                        "personality_type": pd.get("personality_type") or "",
                        "dominant_traits": pd.get("dominant_traits") or [],
                        "communication_style": pd.get("communication_style") or "",
                        "motivation_drivers": pd.get("motivation_drivers") or [],
                        "team_role": pd.get("team_role") or "",
                        "strengths": pd.get("strengths") or [],
                        "leadership_style": pd.get("leadership_style") or "",
                        "meeting_id": meeting_id, "user_id": user_id,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    },
                    access_group=access_group,
                )
                result["entities_created"] += 1
                _pp_pid = await _resolve_person_id(pname)
                if _pp_pid:
                    await graph_builder.create_relationship(
                        from_id=_pp_pid, to_id=pp_id, rel_type="HAS_PROFILE",
                        properties={**_temporal_base},
                    )
                    result["relationships_created"] += 1
            except Exception as e:
                logger.warning(f"psych profile node failed: {e}")

        # Остальные четыре блока психоанализа — мотивы, групповая динамика,
        # прогнозы поведения, рекомендации — раньше вычислялись и
        # ВЫБРАСЫВАЛИСЬ (LLM-вызовы оплачивались, результат терялся).
        # Сохраняем узлами PsychInsight. Мотивы и прогнозы sensitive
        # (гриф 4) и в поисковые индексы НЕ пишутся — сознательно: «кто у
        # нас манипулирует» не должно работать через общий поиск.
        try:
            from backend.core.capture.psych_persistence import (
                REL_TYPE as _PSY_REL,
                build_psych_insight_payloads,
            )
            _psy_payloads = build_psych_insight_payloads(
                _psych, meeting_id=meeting_id, user_id=user_id,
                created_at=datetime.now(timezone.utc).isoformat())
            for _pl in _psy_payloads:
                await graph_builder.create_node(
                    node_id=_pl["node_id"], label="PsychInsight",
                    properties=_pl["properties"], access_group=access_group,
                )
                result["entities_created"] += 1
                if await graph_builder.create_relationship(
                    from_id=meeting_node_id, to_id=_pl["node_id"],
                    rel_type=_PSY_REL, properties={**_temporal_base},
                ):
                    result["relationships_created"] += 1
                _who = _pl.get("person_name")
                if _who:
                    _wpid = await _resolve_person_id(_who)
                    if _wpid:
                        await graph_builder.create_relationship(
                            from_id=_wpid, to_id=_pl["node_id"],
                            rel_type=_PSY_REL, properties={**_temporal_base},
                        )
                        result["relationships_created"] += 1
            if _psy_payloads:
                result["psych_insights_saved"] = len(_psy_payloads)
        except Exception as e:
            logger.warning(f"psych insights wiring failed: {e}")

        # ═══ ЦЕЛИ (goals_tracking) — раньше выбрасывались целиком ═══
        try:
            _goals_data = analysis_result.get("goals_tracking") or {}
            # карта прогресса по goal_id (для % и статуса)
            _prog_by_goal = {}
            for pu in _goals_data.get("progress_updates", []) or []:
                if isinstance(pu, dict) and pu.get("goal_id"):
                    _prog_by_goal[pu["goal_id"]] = pu
            for gi, g in enumerate((_goals_data.get("goals") or [])[:30]):
                gd = g if isinstance(g, dict) else {}
                statement = (gd.get("goal_statement") or "").strip()
                if not statement:
                    continue
                gid = gd.get("goal_id") or f"goal_{meeting_id}_{gi}"
                _pu = _prog_by_goal.get(gid, {})
                await graph_builder.create_node(
                    node_id=gid, label="Goal",
                    properties={
                        "goal_id": gid, "goal_statement": statement,
                        "level": gd.get("level") or "",
                        "entity_name": gd.get("entity_name") or "",
                        "status": _pu.get("current_status") or gd.get("status") or "",
                        "timeline": gd.get("timeline") or "",
                        "success_criteria": gd.get("success_criteria") or "",
                        "confidence": float(gd.get("confidence") or 0),
                        "why_reason": gd.get("why_reason") or gd.get("why_stated") or "",
                        "progress_percentage": int(_pu.get("progress_percentage") or 0),
                        "metrics_quantitative": gd.get("metrics_quantitative") or [],
                        "metrics_qualitative": gd.get("metrics_qualitative") or [],
                        "meeting_id": meeting_id, "user_id": user_id,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    },
                    access_group=access_group,
                )
                result["entities_created"] += 1
                if await graph_builder.create_relationship(
                    from_id=meeting_node_id, to_id=gid, rel_type="HAS_GOAL",
                    properties={**_temporal_base},
                ):
                    result["relationships_created"] += 1
                # person-level цель → привязываем к человеку
                if (gd.get("level") == "person") and gd.get("entity_name"):
                    _gp = await _resolve_person_id(gd["entity_name"])
                    if _gp:
                        await graph_builder.create_relationship(
                            from_id=_gp, to_id=gid, rel_type="HAS_GOAL",
                            properties={**_temporal_base},
                        )
        except Exception as e:
            logger.warning(f"goals wiring failed: {e}")

        # ═══ РИСКИ КОНФЛИКТОВ (psychological_analysis.conflict_risks) ═══
        try:
            for ci, c in enumerate((_psych.get("conflict_risks") or [])[:20]):
                cd = c if isinstance(c, dict) else {}
                desc = (cd.get("description") or cd.get("potential_impact") or "").strip()
                if not desc:
                    continue
                cid = cd.get("conflict_id") or f"crisk_{meeting_id}_{ci}"
                await graph_builder.create_node(
                    node_id=cid, label="ConflictRisk",
                    properties={
                        "conflict_id": cid, "description": desc,
                        "conflict_type": cd.get("type") or cd.get("conflict_type") or "",
                        "risk_level": cd.get("risk_level") or "",
                        "probability": float(cd.get("probability") or 0),
                        "parties_involved": cd.get("parties_involved") or [],
                        "root_causes": cd.get("root_causes") or [],
                        "impact": cd.get("potential_impact") or cd.get("impact") or "",
                        "resolution_suggestions": cd.get("resolution_strategies")
                            or cd.get("resolution_suggestions") or [],
                        "severity": cd.get("risk_level") or "",
                        "meeting_id": meeting_id, "user_id": user_id,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    },
                    access_group=access_group,
                )
                result["entities_created"] += 1
                if await graph_builder.create_relationship(
                    from_id=meeting_node_id, to_id=cid, rel_type="HAS_CONFLICT_RISK",
                    properties={**_temporal_base},
                ):
                    result["relationships_created"] += 1
                for party in (cd.get("parties_involved") or [])[:6]:
                    _pp = await _resolve_person_id(str(party))
                    if _pp:
                        await graph_builder.create_relationship(
                            from_id=_pp, to_id=cid, rel_type="AT_RISK_OF",
                            properties={**_temporal_base},
                        )
        except Exception as e:
            logger.warning(f"conflict_risks wiring failed: {e}")

        # ═══ ПРОГНОЗЫ И СЦЕНАРИИ (future_predictions) ═══
        try:
            _fp = analysis_result.get("future_predictions") or {}
            for si, s in enumerate((_fp.get("scenarios") or [])[:12]):
                sd = s if isinstance(s, dict) else {}
                nm = (sd.get("name") or "").strip()
                if not nm:
                    continue
                sid = f"scenario_{meeting_id}_{si}"
                await graph_builder.create_node(
                    node_id=sid, label="Scenario",
                    properties={
                        "scenario_id": sid, "name": nm,
                        "scenario_type": sd.get("type") or "",
                        "probability": float(sd.get("probability") or 0),
                        "description": sd.get("description") or "",
                        "time_horizon": sd.get("time_horizon") or "",
                        "key_events": sd.get("key_events") or [],
                        "consequences": sd.get("consequences") or [],
                        "recommended_actions": sd.get("recommended_actions") or [],
                        "affected_projects": sd.get("affected_projects") or [],
                        "meeting_id": meeting_id, "user_id": user_id,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    },
                    access_group=access_group,
                )
                result["entities_created"] += 1
                if await graph_builder.create_relationship(
                    from_id=meeting_node_id, to_id=sid, rel_type="PREDICTS",
                    properties={**_temporal_base},
                ):
                    result["relationships_created"] += 1
            for pi, p in enumerate((_fp.get("project_predictions") or [])[:12]):
                pd2 = p if isinstance(p, dict) else {}
                subj = (pd2.get("project_name") or "").strip()
                if not subj:
                    continue
                prid = f"prediction_{meeting_id}_{pi}"
                await graph_builder.create_node(
                    node_id=prid, label="Prediction",
                    properties={
                        "prediction_id": prid, "subject": subj,
                        "success_probability": float(pd2.get("success_probability") or 0),
                        "completion_date": pd2.get("completion_date") or "",
                        "key_risks": pd2.get("key_risks") or [],
                        "blockers": pd2.get("blockers") or [],
                        "recommendations": pd2.get("recommendations") or [],
                        "meeting_id": meeting_id, "user_id": user_id,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    },
                    access_group=access_group,
                )
                result["entities_created"] += 1
                if await graph_builder.create_relationship(
                    from_id=meeting_node_id, to_id=prid, rel_type="PREDICTS",
                    properties={**_temporal_base},
                ):
                    result["relationships_created"] += 1
        except Exception as e:
            logger.warning(f"future_predictions wiring failed: {e}")

        # ═══ ОБОГАЩЁННЫЕ KPI (enhanced_kpis) — KPI-узлы с трендом/таргетом ═══
        try:
            for ki, k in enumerate((agent_results.get("enhanced_kpis") or [])[:20]):
                kd = k if isinstance(k, dict) else {}
                mname = (kd.get("metric_name") or "").strip()
                if not mname:
                    continue
                kid = kd.get("kpi_id") or f"ekpi_{meeting_id}_{ki}"
                _vals = kd.get("values") or {}
                _trend = kd.get("trend_analysis") or {}
                _perf = kd.get("performance_assessment") or {}
                _biz = kd.get("business_context") or {}
                await graph_builder.create_node(
                    node_id=kid, label="KPI",
                    properties={
                        "kpi_id": kid, "name": mname,
                        "category": kd.get("metric_category") or "",
                        "current_value": str(_vals.get("current_value") or ""),
                        "target_value": str(_vals.get("target_value") or ""),
                        "unit": _vals.get("unit") or "",
                        "trend": _trend.get("direction") or "",
                        "change_percentage": _trend.get("change_percentage") or 0,
                        "performance_rating": _perf.get("performance_rating") or "",
                        "vs_target": _perf.get("vs_target") or "",
                        "strategic_importance": _biz.get("strategic_importance") or "",
                        "meeting_id": meeting_id, "user_id": user_id,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    },
                    access_group=access_group,
                )
                result["entities_created"] += 1
                if await graph_builder.create_relationship(
                    from_id=kid, to_id=meeting_node_id, rel_type="REPORTED_IN",
                    properties={**_temporal_base},
                ):
                    result["relationships_created"] += 1
        except Exception as e:
            logger.warning(f"enhanced_kpis wiring failed: {e}")

        # Сущности (проекты, технологии, концепции) с Entity Resolution
        # Step 1: Для типизированных сущностей используем типизированные label-ы в Neo4j
        TYPED_ENTITY_LABELS = {
            "project": "Project",
            "product": "Product",
            "person": "Person",
            "team": "Team",
            "department": "Department",
            "company": "Entity",      # company хранится как Entity с entity_type=company
            "organization": "Entity",  # organization хранится как Entity с entity_type=organization
            "feature": "Entity",       # feature хранится как Entity с entity_type=feature
        }
        RESOLVABLE_TYPES = (
            "project", "product", "technology", "company",
            "team", "department", "organization", "person", "feature",
        )

        # ═══ ENTITY CLASSIFICATION: Пакетная LLM-классификация entities без типа ═══
        raw_entities = agent_results.get("entities", [])
        entities_to_classify = []
        for i, entity in enumerate(raw_entities):
            e_type = entity.get("entity_type") or entity.get("type") or ""
            if not e_type or e_type.lower() in ("concept", "unknown", "other", "none", ""):
                entities_to_classify.append((i, entity))

        if entities_to_classify:
            try:
                classified = await _classify_entities_batch(
                    entities_to_classify=entities_to_classify,
                    meeting_title=meeting_title,
                )
                # Применяем классификацию обратно к raw_entities
                for idx, new_type in classified.items():
                    if 0 <= idx < len(raw_entities):
                        raw_entities[idx]["entity_type"] = new_type
                        logger.debug(f"🏷️ Classified entity '{raw_entities[idx].get('name', '')}' as '{new_type}'")

                if classified:
                    logger.info(f"🏷️ LLM classified {len(classified)}/{len(entities_to_classify)} untyped entities")
            except Exception as cls_err:
                logger.warning(f"Entity classification failed (non-critical): {cls_err}")

        for i, entity in enumerate(raw_entities):
            e_name = _first_text(entity.get("name"), entity.get("title"), entity.get("entity_name"), f"entity_{i}")
            # FIX: Правильный ключ — entity_type (возвращается EntityAgent), с fallback на type
            e_type = entity.get("entity_type") or entity.get("type") or "concept"
            e_type = e_type.strip().lower()
            e_description = entity.get("description", "")

            # Entity Resolution для проектов и важных сущностей
            if e_type in RESOLVABLE_TYPES:
                resolved = await entity_resolver.resolve(
                    name=e_name,
                    entity_type=e_type,
                    organization_id=user_id,
                    properties={"description": e_description}
                )
                e_id = resolved.canonical_id
                is_new = resolved.is_new

                if not is_new:
                    result["entities_merged"] = result.get("entities_merged", 0) + 1
                    logger.debug(f"🔗 Entity resolved: {e_name} -> {resolved.name} (type: {e_type})")
                    # Step 1: Обогащаем description при повторном обнаружении
                    if e_description and graph_builder.use_networkx:
                        try:
                            _existing_data = graph_builder.get_node_data(e_id)
                            if _existing_data:
                                _old_desc = _existing_data.get("description", "")
                                if e_description not in (_old_desc or ""):
                                    _new_desc = f"{_old_desc}. {e_description}" if _old_desc else e_description
                                    graph_builder.nx_graph.nodes[e_id]["description"] = _new_desc[:2000]
                                    graph_builder.nx_graph.nodes[e_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
                        except Exception:
                            logger.debug("suppressed exception", exc_info=True)
            else:
                # Для концепций и прочего - простой ID
                e_id = f"entity_{meeting_id}_{e_name.lower().replace(' ', '_')}"
                is_new = True

            # Step 1: Типизированный label для project/product/team/department
            e_label = TYPED_ENTITY_LABELS.get(e_type, "Entity")
            e_id_field = f"{e_type}_id" if e_type in TYPED_ENTITY_LABELS else "entity_id"

            if e_id and e_id not in _cooc_node_ids:
                _cooc_node_ids.append(e_id)

            if is_new:
                node_props = {
                    "name": e_name,
                    "description": e_description,
                    "entity_type": e_type,
                    "meeting_id": meeting_id,
                    "user_id": user_id,
                }
                # Добавляем ID-field для типизированных узлов
                if e_type in TYPED_ENTITY_LABELS:
                    node_props[e_id_field] = e_id
                else:
                    node_props["entity_id"] = e_id

                await graph_builder.create_node(
                    node_id=e_id,
                    label=e_label,
                    properties=node_props,
                    access_group=access_group,
                )
                result["entities_created"] += 1

            # Индексируем в векторную базу (обновляем даже для существующих)
            try:
                entity_text = f"{e_name}: {e_description}" if e_description else e_name
                await vector_indexer.upsert_vector_public(
                    collection=vector_indexer.collections["entities"],
                    vector_id=e_id,
                    text=entity_text,
                    metadata={
                        "entity_type": e_type,
                        "meeting_id": meeting_id,
                        "user_id": user_id,
                        "description": e_description,
                    }
                )
                # FIX #9: BM25 для entities
                bm25_searcher.add_document(
                    doc_id=e_id,
                    text=entity_text,
                    metadata={"type": "entity", "entity_type": e_type, "meeting_id": meeting_id}
                )
            except Exception as ve:
                logger.warning(f"Vector upsert failed for entity {e_id}: {ve}")

            _entity_desc = f"Упоминание: {e_name}"
            if e_description:
                _entity_desc += f" — {e_description[:100]}"
            await graph_builder.create_relationship(
                from_id=meeting_node_id,
                to_id=e_id,
                rel_type="MENTIONS",
                properties={
                    **_temporal_base,
                    "description": _entity_desc,
                    "entity_type": e_type,
                    "importance": entity.get("importance", "medium"),
                    "confidence": float(entity.get("confidence", 0.8)),
                }
            )
            result["relationships_created"] += 1

        # KPIs
        for i, kpi in enumerate(agent_results.get("kpis", [])):
            k_id = f"kpi_{meeting_id}_{i}"
            k_name = kpi.get("name", "")
            k_value = kpi.get("value", "")
            k_trend = kpi.get("trend", "")
            k_category = kpi.get("category", "")

            await graph_builder.create_node(
                node_id=k_id,
                label="KPI",
                properties={
                    "name": k_name,
                    "value": k_value,
                    "trend": k_trend,
                    "category": k_category,
                    "meeting_id": meeting_id,
                    "user_id": user_id
                },
                access_group=access_group,
            )
            result["entities_created"] += 1

            _kpi_desc_parts = [f"KPI: {k_name}" if k_name else "KPI"]
            if k_value:
                _kpi_desc_parts.append(f"= {k_value}")
            if k_trend:
                _kpi_desc_parts.append(f"(тренд: {k_trend})")
            await graph_builder.create_relationship(
                from_id=meeting_node_id,
                to_id=k_id,
                rel_type="HAS_KPI",
                properties={
                    **_temporal_base,
                    "description": " ".join(_kpi_desc_parts),
                    "category": k_category,
                    "confidence": float(kpi.get("confidence", 0.8)),
                }
            )
            result["relationships_created"] += 1

            # ===== ИНДЕКСАЦИЯ KPI В ВЕКТОРА =====
            kpi_text_parts = [
                f"KPI: {k_name}" if k_name else "",
                f"Значение: {k_value}" if k_value else "",
                f"Тренд: {k_trend}" if k_trend else "",
                f"Категория: {k_category}" if k_category else "",
                f"Встреча: {meeting_title}" if meeting_title else "",
            ]
            kpi_text = " | ".join(filter(None, kpi_text_parts))

            if k_name:  # Индексируем только если есть название
                try:
                    await vector_indexer.upsert_vector_public(
                        collection=vector_indexer.collections["kpis"],
                        vector_id=k_id,
                        text=kpi_text,
                        metadata={
                            "kpi_id": k_id,
                            "name": k_name,
                            "value": str(k_value),
                            "trend": k_trend,
                            "category": k_category,
                            "user_id": user_id,
                            "type": "kpi",
                            "meeting_id": meeting_id,
                        },
                    )
                    result["kpis_indexed"] = result.get("kpis_indexed", 0) + 1

                    # FIX #9: BM25 для KPIs
                    bm25_searcher.add_document(
                        doc_id=k_id,
                        text=kpi_text,
                        metadata={"type": "kpi", "category": k_category, "meeting_id": meeting_id}
                    )
                except Exception as ve:
                    logger.warning(f"Vector upsert failed for KPI {k_id}: {ve}")

        # ===== ЕДИНЫЕ МЕТРИКИ (мост «цифры из встреч ↔ цифры из таблиц») ==
        # KPI-цифры встречи («4,2 млн ₽») парсятся в числа и попадают в
        # metric_registry — там же живут ряды из датасетов; summary()
        # сверяет «на встрече заявляли X, данные показывают Y». Never-raise.
        try:
            from backend.core.ontology.dataset_registry import ontology_enabled
            if ontology_enabled():
                from backend.core.ontology.metric_registry import (
                    ingest_meeting_kpis,
                )
                n_pts = ingest_meeting_kpis(
                    user_id, meeting_id,
                    agent_results.get("kpis") or [],
                    agent_results.get("enhanced_kpis") or [],
                    at_iso=str(meeting_date) if meeting_date else None)
                if n_pts:
                    logger.info(f"📈 Metrics: {n_pts} точек KPI из встречи "
                                f"{meeting_id} в единый реестр метрик")
        except Exception as e:
            logger.debug(f"metric ingest (meeting) skipped: {e}")

        # ===== ИНДЕКСАЦИЯ ТРАНСКРИПТА ЧАНКАМИ =====
        # Это позволяет искать по полному тексту встречи
        transcript = meeting_context.get("transcript", "")
        if transcript and len(transcript) > 100:
            chunk_size = 1500  # ~300-400 слов на чанк
            chunk_overlap = 200
            chunks = []

            for start in range(0, len(transcript), chunk_size - chunk_overlap):
                chunk_text = transcript[start:start + chunk_size]
                if len(chunk_text) > 50:  # Минимальный размер чанка
                    chunks.append({
                        "text": chunk_text,
                        "start": start,
                        "end": min(start + chunk_size, len(transcript))
                    })

            for i, chunk in enumerate(chunks):
                chunk_id = f"chunk_{meeting_id}_{i}"
                chunk_text_with_context = f"[Встреча: {meeting_title}] {chunk['text']}"

                try:
                    await vector_indexer.upsert_vector_public(
                        collection=vector_indexer.collections["chunks"],
                        vector_id=chunk_id,
                        text=chunk_text_with_context,
                        metadata={
                            "chunk_id": chunk_id,
                            "meeting_id": meeting_id,
                            "meeting_title": meeting_title,
                            "chunk_index": i,
                            "total_chunks": len(chunks),
                            "char_start": chunk["start"],
                            "char_end": chunk["end"],
                            "user_id": user_id,
                            "type": "transcript_chunk",
                        },
                    )
                    result["chunks_indexed"] = result.get("chunks_indexed", 0) + 1

                    # Индексируем чанк в BM25
                    bm25_searcher.add_document(
                        doc_id=chunk_id,
                        text=chunk_text_with_context,
                        metadata={
                            "type": "transcript_chunk",
                            "meeting_id": meeting_id,
                            "user_id": user_id,
                            "start": chunk["start"]
                        }
                    )
                except Exception as ve:
                    logger.warning(f"Vector/BM25 upsert failed for chunk {chunk_id}: {ve}")

            logger.info(f"📝 Indexed {len(chunks)} transcript chunks for meeting {meeting_id}")

        # ===== STEP 4: SEMANTIC CHUNKING (Topics + Propositions) =====
        # Дополняет raw chunks семантическим разбиением (Tier 2+)
        if transcript and len(transcript) > 200:
            try:
                from backend.core.store.semantic_chunker import SemanticChunker

                semantic_chunker = SemanticChunker()
                semantic_result = await semantic_chunker.chunk_transcript(
                    transcript=transcript,
                    meeting_context=meeting_context,
                    meeting_id=meeting_id,
                    extract_propositions=True,
                )
                topic_by_index = {
                    topic.topic_index: topic
                    for topic in semantic_result.topics
                }

                # Индексируем Topics в Qdrant
                for topic in semantic_result.topics:
                    topic_id = f"topic_{meeting_id}_{topic.topic_index}"
                    topic_text = f"{topic.topic_name}: {topic.topic_summary}" if topic.topic_summary else topic.topic_name
                    try:
                        await vector_indexer.upsert_vector_public(
                            collection=vector_indexer.collections.get("topics", "tessent_topics"),
                            vector_id=topic_id,
                            text=topic_text,
                            metadata={
                                "topic_id": topic_id,
                                "topic_name": topic.topic_name,
                                "topic_summary": topic.topic_summary,
                                "topic_index": topic.topic_index,
                                "meeting_id": meeting_id,
                                "meeting_title": meeting_title,
                                "user_id": user_id,
                                "type": "topic",
                                "char_start": topic.start_char,
                                "char_end": topic.end_char,
                            },
                        )
                        # BM25 для topics
                        bm25_searcher.add_document(
                            doc_id=topic_id,
                            text=topic_text,
                            metadata={
                                "type": "topic",
                                "topic_name": topic.topic_name,
                                "meeting_id": meeting_id,
                            }
                        )
                    except Exception as ve:
                        logger.warning(f"Vector upsert failed for topic {topic_id}: {ve}")

                # Индексируем Propositions в Qdrant
                for prop in semantic_result.propositions:
                    try:
                        topic_meta = topic_by_index.get(prop.topic_index)
                        topic_name = topic_meta.topic_name if topic_meta else ""
                        proposition_text = prop.canonical_statement or prop.statement
                        if topic_name:
                            proposition_text = f"{topic_name}. {proposition_text}"

                        await vector_indexer.upsert_vector_public(
                            collection=vector_indexer.collections.get("propositions", "tessent_propositions"),
                            vector_id=prop.proposition_id,
                            text=proposition_text,
                            metadata={
                                "proposition_id": prop.proposition_id,
                                "statement": prop.statement,
                                "canonical_statement": prop.canonical_statement or prop.statement,
                                "prop_type": prop.prop_type,
                                "attribution": prop.attribution,
                                "confidence": prop.confidence,
                                "entities": prop.entities,
                                "temporal": prop.temporal,
                                "topic_index": prop.topic_index,
                                "topic_name": topic_name,
                                "status": prop.status,
                                "extraction_method": prop.extraction_method,
                                "source_text": prop.source_text,
                                "source_start": prop.source_start,
                                "source_end": prop.source_end,
                                "meeting_id": meeting_id,
                                "meeting_title": meeting_title,
                                "user_id": user_id,
                                "type": "proposition",
                            },
                        )

                        # BM25 для propositions
                        bm25_searcher.add_document(
                            doc_id=prop.proposition_id,
                            text=proposition_text,
                            metadata={
                                "type": "proposition",
                                "prop_type": prop.prop_type,
                                "topic_name": topic_name,
                                "meeting_id": meeting_id,
                            }
                        )
                    except Exception as ve:
                        logger.warning(f"Vector upsert failed for proposition {prop.proposition_id}: {ve}")

                result["topics_indexed"] = semantic_result.total_topics
                result["propositions_indexed"] = semantic_result.total_propositions
                logger.info(
                    f"📝 Semantic chunking: {semantic_result.total_topics} topics, "
                    f"{semantic_result.total_propositions} propositions for meeting {meeting_id}"
                )

            except Exception as sc_err:
                logger.warning(f"Semantic chunking failed (non-critical): {sc_err}")

        # ===== STEP 5: CAUSAL CHAINS (Tier 3+) =====
        try:
            from backend.core.capture.agents.causal_chain_extractor import CausalChainExtractor

            causal_extractor = CausalChainExtractor()
            causal_links = await causal_extractor.extract(
                transcript=transcript,
                meeting_id=meeting_id,
                meeting_context=meeting_context,
            )

            for link in causal_links:
                # FIX 1.4: Создаём узлы cause/effect ПЕРЕД relationships
                cause_id = f"cause_{meeting_id}_{link.cause[:30].lower().replace(' ', '_')}"
                effect_id = f"effect_{meeting_id}_{link.effect[:30].lower().replace(' ', '_')}"

                await graph_builder.create_node(
                    node_id=cause_id,
                    label="Entity",
                    properties={
                        "name": link.cause[:100],
                        "entity_type": "causal_factor",
                        "description": link.cause,
                        "meeting_id": meeting_id,
                        "user_id": user_id,
                    },
                    access_group=access_group,
                )
                await graph_builder.create_node(
                    node_id=effect_id,
                    label="Entity",
                    properties={
                        "name": link.effect[:100],
                        "entity_type": "causal_effect",
                        "description": link.effect,
                        "meeting_id": meeting_id,
                        "user_id": user_id,
                    },
                    access_group=access_group,
                )

                # Создаём связь CAUSED_BY / LED_TO
                rel_type = "CAUSED_BY" if link.link_type in ("caused_by", "because_of") else "LED_TO"
                await graph_builder.create_relationship(
                    from_id=effect_id if rel_type == "CAUSED_BY" else cause_id,
                    to_id=cause_id if rel_type == "CAUSED_BY" else effect_id,
                    rel_type=rel_type,
                    properties={
                        **_temporal_base,
                        "description": f"{link.cause} -> {link.effect}",
                        "confidence": link.confidence,
                        "quote": link.quote[:200] if link.quote else "",
                    }
                )

            result["causal_links_created"] = len(causal_links)
            if causal_links:
                logger.info(f"🔗 Created {len(causal_links)} causal links for meeting {meeting_id}")

        except Exception as causal_err:
            logger.warning(f"Causal chain extraction failed (non-critical): {causal_err}")

        # ===== STEP 5: CONTRADICTION DETECTION (Tier 3+) =====
        try:
            from backend.core.think.contradiction_detector import ContradictionDetector

            detector = ContradictionDetector(graph_builder=graph_builder)
            contradictions = await detector.check_new_data(
                meeting_id=meeting_id,
                agent_results=agent_results,
                user_id=user_id,
            )

            # Сохраняем противоречия как узлы графа
            for c in contradictions:
                await graph_builder.create_node(
                    node_id=c.contradiction_id,
                    label="Contradiction",
                    properties={
                        "contradiction_id": c.contradiction_id,
                        "new_fact": c.new_fact,
                        "existing_fact": c.existing_fact,
                        "contradiction_type": c.contradiction_type,
                        "severity": c.severity,
                        "resolution_suggestion": c.resolution_suggestion,
                        "new_source": c.new_source,
                        "existing_source": c.existing_source,
                        "entity_id": c.entity_id,
                        "meeting_id": meeting_id,
                        "user_id": user_id,
                    },
                    access_group=access_group,
                )

            result["contradictions_found"] = len(contradictions)
            if contradictions:
                logger.info(f"⚠️ Found {len(contradictions)} contradictions in meeting {meeting_id}")

        except Exception as contr_err:
            logger.warning(f"Contradiction detection failed (non-critical): {contr_err}")

        # ===== ИНДЕКСАЦИЯ СВЯЗЕЙ ГРАФА =====
        # Связи также индексируются для контекстного поиска
        relationships_indexed = 0

        # Собираем все связи для индексации
        relationship_texts = []

        # Связи участников со встречей
        for participant in agent_results.get("participants", []):
            p_name = participant.get("name", "Unknown")
            rel_text = f"{p_name} участвовал во встрече {meeting_title}"
            relationship_texts.append({
                "id": f"rel_{meeting_id}_participated_{p_name.lower().replace(' ', '_')}",
                "text": rel_text,
                "rel_type": "PARTICIPATED_IN",
                "from_name": p_name,
                "to_name": meeting_title,
            })

        # Связи решений со встречей
        for i, decision in enumerate(agent_results.get("decisions", [])):
            d_summary = _first_text(
                decision.get("decision_summary"),
                decision.get("summary"),
                decision.get("title"),
            )
            if d_summary:
                rel_text = f"На встрече {meeting_title} было принято решение: {d_summary}"
                relationship_texts.append({
                    "id": f"rel_{meeting_id}_decision_{i}",
                    "text": rel_text,
                    "rel_type": "HAS_DECISION",
                    "from_name": meeting_title,
                    "to_name": d_summary[:50],
                })

        # Связи задач со встречей и исполнителями
        for i, task in enumerate(agent_results.get("tasks", [])):
            t_title = _first_text(task.get("title"), task.get("task_title"), task.get("name"))
            t_assignee = task.get("assignee", "")
            if t_title:
                rel_text = f"На встрече {meeting_title} была поставлена задача: {t_title}"
                if t_assignee:
                    rel_text += f" (исполнитель: {t_assignee})"
                relationship_texts.append({
                    "id": f"rel_{meeting_id}_task_{i}",
                    "text": rel_text,
                    "rel_type": "HAS_TASK",
                    "from_name": meeting_title,
                    "to_name": t_title[:50],
                    "assignee": t_assignee,
                })

        # Связи сущностей со встречей
        for i, entity in enumerate(agent_results.get("entities", [])):
            e_name = _first_text(entity.get("name"), entity.get("title"))
            e_type = entity.get("entity_type") or entity.get("type") or "concept"
            if e_name:
                rel_text = f"На встрече {meeting_title} обсуждалась {e_type}: {e_name}"
                relationship_texts.append({
                    "id": f"rel_{meeting_id}_entity_{i}",
                    "text": rel_text,
                    "rel_type": "MENTIONS",
                    "from_name": meeting_title,
                    "to_name": e_name,
                    "entity_type": e_type,
                })

        # Индексируем все связи
        for rel in relationship_texts:
            try:
                # Используем коллекцию chunks для связей (можно создать отдельную)
                await vector_indexer.upsert_vector_public(
                    collection=vector_indexer.collections["chunks"],
                    vector_id=rel["id"],
                    text=rel["text"],
                    metadata={
                        "relationship_id": rel["id"],
                        "rel_type": rel["rel_type"],
                        "from_name": rel.get("from_name", ""),
                        "to_name": rel.get("to_name", ""),
                        "meeting_id": meeting_id,
                        "user_id": user_id,
                        "type": "relationship",
                    },
                )
                relationships_indexed += 1
            except Exception as ve:
                logger.warning(f"Vector upsert failed for relationship {rel['id']}: {ve}")

        result["relationships_indexed"] = relationships_indexed
        logger.info(f"🔗 Indexed {relationships_indexed} relationships for meeting {meeting_id}")

        # ===== СОХРАНЕНИЕ РЕГЛАМЕНТОВ =====
        regulations = analysis_result.get("regulations", [])
        for i, regulation in enumerate(regulations):
            r_id = regulation.get("regulation_id") or f"regulation_{meeting_id}_{i}"
            r_title = _first_text(regulation.get("title"), regulation.get("name"))
            r_description = _first_text(regulation.get("description"), regulation.get("content"))
            r_type = regulation.get("regulation_type", "regulation")

            if r_title or r_description:
                await graph_builder.create_node(
                    node_id=r_id,
                    label="Regulation",
                    properties={
                        "name": r_title or r_description[:80],
                        "title": r_title,
                        "description": r_description,
                        "regulation_type": r_type,
                        "scope": regulation.get("scope", ""),
                        "department": regulation.get("department", ""),
                        "mandatory": regulation.get("mandatory", True),
                        "enforcement": regulation.get("enforcement", ""),
                        "source_meeting_id": meeting_id,
                        "user_id": user_id,
                    },
                    access_group=access_group,
                )
                result["entities_created"] += 1

                await graph_builder.create_relationship(
                    from_id=meeting_node_id,
                    to_id=r_id,
                    rel_type="HAS_REGULATION"
                )
                result["relationships_created"] += 1

                # Индексация регламента в вектора
                regulation_text = f"{r_title}. {r_description}. Тип: {r_type}. Область: {regulation.get('scope', '')}"
                try:
                    await vector_indexer.upsert_vector_public(
                        collection=vector_indexer.collections.get("documents", vector_indexer.collections["entities"]),
                        vector_id=r_id,
                        text=regulation_text,
                        metadata={
                            "regulation_id": r_id,
                            "title": r_title,
                            "regulation_type": r_type,
                            "meeting_id": meeting_id,
                            "user_id": user_id,
                            "type": "regulation",
                        },
                    )
                    result["regulations_indexed"] = result.get("regulations_indexed", 0) + 1

                    # BM25 для регламентов
                    bm25_searcher.add_document(
                        doc_id=r_id,
                        text=regulation_text,
                        metadata={
                            "type": "regulation",
                            "regulation_type": r_type,
                            "meeting_id": meeting_id,
                        }
                    )
                except Exception as ve:
                    logger.warning(f"Vector upsert failed for regulation {r_id}: {ve}")

        if regulations:
            logger.info(f"📋 Saved {len(regulations)} regulations for meeting {meeting_id}")

        # ===== СОХРАНЕНИЕ ИЗВЛЕЧЁННЫХ ШАБЛОНОВ =====
        extracted_templates = analysis_result.get("extracted_templates", [])
        for i, template in enumerate(extracted_templates):
            t_id = template.get("template_id") or f"template_{meeting_id}_{i}"
            t_name = _first_text(template.get("name"), template.get("title"))
            t_description = _first_text(template.get("description"), template.get("content"))
            t_type = template.get("template_type", "checklist")

            if t_name or t_description:
                await graph_builder.create_node(
                    node_id=t_id,
                    label="Template",
                    properties={
                        "name": t_name or t_description[:80],
                        "title": t_name,
                        "description": t_description,
                        "template_type": t_type,
                        "category": template.get("category", ""),
                        "steps": template.get("steps", []),
                        "source_meeting_id": meeting_id,
                        "user_id": user_id,
                    },
                    access_group=access_group,
                )
                result["entities_created"] += 1

                await graph_builder.create_relationship(
                    from_id=meeting_node_id,
                    to_id=t_id,
                    rel_type="HAS_TEMPLATE"
                )
                result["relationships_created"] += 1

                # Индексация шаблона в вектора
                template_text = f"Шаблон: {t_name}. {t_description}. Тип: {t_type}. Категория: {template.get('category', '')}"
                try:
                    await vector_indexer.upsert_vector_public(
                        collection=vector_indexer.collections.get("documents", vector_indexer.collections["entities"]),
                        vector_id=t_id,
                        text=template_text,
                        metadata={
                            "template_id": t_id,
                            "name": t_name,
                            "template_type": t_type,
                            "meeting_id": meeting_id,
                            "user_id": user_id,
                            "type": "template",
                        },
                    )
                    result["templates_indexed"] = result.get("templates_indexed", 0) + 1

                    # BM25 для шаблонов
                    bm25_searcher.add_document(
                        doc_id=t_id,
                        text=template_text,
                        metadata={
                            "type": "template",
                            "template_type": t_type,
                            "meeting_id": meeting_id,
                        }
                    )
                except Exception as ve:
                    logger.warning(f"Vector upsert failed for template {t_id}: {ve}")

        if extracted_templates:
            logger.info(f"📄 Saved {len(extracted_templates)} templates for meeting {meeting_id}")

        # ===== АВТОМАТИЧЕСКОЕ СЛИЯНИЕ ДУБЛИКАТОВ =====
        # Запускаем Entity Resolution для поиска и слияния очевидных дубликатов
        try:
            merge_stats = await entity_resolver.auto_merge_duplicates(
                entity_type=None,  # Все типы
                threshold=0.95  # Только очевидные дубликаты
            )
            if merge_stats.get("merged", 0) > 0:
                logger.info(f"🔗 Auto-merged {merge_stats['merged']} duplicate entities")
                result["duplicates_merged"] = merge_stats["merged"]
        except Exception as merge_err:
            logger.warning(f"Auto-merge failed (non-critical): {merge_err}")

        # ===== Hebbian co-occurrence по РЕАЛЬНЫМ id узлов =====
        # ВАЖНО: строго ДО graph_builder.close() ниже — иначе RELATED_TO падают
        # с «Driver closed». Все узлы сущностей/участников уже созданы, id у нас
        # настоящие (entity_<meeting>_… / canonical_id), а не сырые slug'и.
        try:
            from backend.core.config import flags
            if flags.enable_hebbian_learning and len(_cooc_node_ids) >= 2:
                from backend.core.memory import get_hebbian_learning
                hebbian = get_hebbian_learning(graph_builder)
                hebb_stats = await hebbian.process_meeting_cooccurrences(
                    meeting_id=meeting_id,
                    entities=[{"entity_id": nid} for nid in _cooc_node_ids],
                    participants=None,
                )
                result["hebbian_connections"] = hebb_stats.get("connections_strengthened", 0)
                logger.info(
                    f"🔗 Hebbian (post-save): {hebb_stats.get('provisional_created', 0)} provisional, "
                    f"{hebb_stats.get('promoted', 0)} promoted, "
                    f"{hebb_stats.get('connections_strengthened', 0)} strengthened "
                    f"по {len(_cooc_node_ids)} узлам"
                )
        except Exception as _heb_err:
            logger.warning(f"Hebbian post-save co-occurrence failed (non-critical): {_heb_err}")

        # Supersede-история фактов встречи — одним пакетом (один лок, один
        # reload, одна запись файла вместо цикла на каждый факт).
        # Не критично для разбора: граф уже собран, падать из-за истории
        # фактов нельзя.
        try:
            if _fact_versions:
                from backend.core.memory.supersede_store import SupersedeStore
                from backend.core.store.tenant_paths import (
                    supersede_store_path_for_user,
                )
                _sstore = SupersedeStore(
                    persist_path=supersede_store_path_for_user(user_id))
                _sres = _sstore.record_many(_fact_versions)
                _superseded = sum(1 for r in _sres if r.get("action") == "superseded")
                result["fact_versions_recorded"] = len(_sres)
                result["facts_superseded"] = _superseded
                logger.info(
                    f"📜 История фактов: {len(_sres)} наблюдений, "
                    f"{_superseded} значений заменено (прошлые помечены, не стёрты)"
                )
        except Exception as _sup_err:
            logger.warning(f"Supersede history failed (non-critical): {_sup_err}")

        # Сохраняем граф
        # Backward compatible: GraphBuilder умеет сохранять NetworkX в файл через close(save=True)
        await graph_builder.close(save=True)

        # Сохраняем индекс (file-backed in-memory)
        try:
            await vector_indexer.close()
        except Exception as e:
            logger.warning(f"VectorIndexer close failed: {e}")

        # Закрываем temporal tracker (не критично)
        # Примечание: не закрываем, т.к. это singleton и может использоваться другими запросами

        # Сохраняем BM25 индекс
        bm25_searcher.save_index()
        logger.info(f"✅ BM25 index saved: {bm25_searcher.total_docs} documents")

        # ===== FIX #7: ОБНОВЛЕНИЕ СНЭПШОТОВ ИЗ SYNC =====
        try:
            from backend.core.sleep.enhanced_snapshot import get_enhanced_snapshot_generator
            # user_id строго kwarg'ом: раньше он уезжал в позицию graph_builder,
            # генератор оставался на каталоге ПРЕДЫДУЩЕГО тенанта, и обновление
            # снапшота из синка писалось в чужой аккаунт (сага КПД/Meflow).
            snapshot_gen = get_enhanced_snapshot_generator(user_id=user_id)
            await snapshot_gen.update_from_meeting(
                meeting_id=meeting_id,
                meeting_data={
                    "meeting_id": meeting_id,
                    "title": meeting_title,
                    "date": meeting_date,
                    "project_id": meeting_project,
                    "participants": agent_results.get("participants", []),
                    "decisions": agent_results.get("decisions", []),
                    "tasks": agent_results.get("tasks", []),
                    "kpis": agent_results.get("kpis", []),
                    "entities": agent_results.get("entities", []),
                },
            )
            result["snapshot_updated"] = True
            logger.info(f"📸 Snapshots updated from meeting {meeting_id}")
        except Exception as snap_err:
            logger.warning(f"Snapshot update failed (non-critical): {snap_err}")

        logger.info(f"✅ Full indexing complete for meeting {meeting_id}: "
                    f"meetings={result.get('meetings_indexed', 0)}, "
                    f"participants={result.get('participants_indexed', 0)}, "
                    f"kpis={result.get('kpis_indexed', 0)}, "
                    f"chunks={result.get('chunks_indexed', 0)}, "
                    f"relationships={result.get('relationships_indexed', 0)}, "
                    f"entities_merged={result.get('entities_merged', 0)}, "
                    f"timeline_events={result.get('timeline_events_added', 0)}")

        return result

    except Exception as e:
        logger.error(f"Error saving orchestrator results to graph: {e}")
        raise


async def _classify_entities_batch(
    entities_to_classify: List[tuple],
    meeting_title: str = "",
) -> Dict[int, str]:
    """
    Пакетная LLM-классификация сущностей, у которых нет entity_type
    или entity_type = concept/unknown/none.

    Args:
        entities_to_classify: список (index, entity_dict)
        meeting_title: название встречи для контекста

    Returns:
        Dict[index, new_entity_type] — маппинг индексов к определённым типам
    """
    if not entities_to_classify:
        return {}

    from backend.core.llm.gemini_client import get_gemini_client

    VALID_TYPES = [
        "project", "product", "person", "team", "department",
        "organization", "company", "technology", "feature",
        "document", "event", "idea", "concept",
    ]

    try:
        client = get_gemini_client()

        # Формируем список для классификации
        entity_list_lines = []
        for idx, entity in entities_to_classify:
            e_name = entity.get("name") or entity.get("title") or "?"
            e_desc = entity.get("description", "")[:150]
            entity_list_lines.append(f'{idx}. "{e_name}" — {e_desc}')

        entities_text = "\n".join(entity_list_lines)

        prompt = f"""Классифицируй каждую сущность ниже, выбрав СТРОГО один тип из списка.

ДОПУСТИМЫЕ ТИПЫ:
- project — проект, инициатива
- product — продукт, сервис, приложение
- person — конкретный человек
- team — команда
- department — отдел, департамент
- organization — внешняя организация
- company — собственная компания
- technology — технология, инструмент, фреймворк
- feature — функция/модуль продукта
- document — документ, отчёт
- event — событие, дедлайн
- idea — идея, предложение
- concept — абстрактное понятие (ТОЛЬКО если ничего не подходит)

Контекст встречи: "{meeting_title}"

СУЩНОСТИ ДЛЯ КЛАССИФИКАЦИИ:
{entities_text}

Верни JSON-объект где ключ = номер сущности (число), значение = тип (строка).
Пример: {{"0": "project", "3": "technology", "5": "person"}}

ТОЛЬКО JSON, без пояснений."""

        response = await client.generate(prompt, temperature=0.1)

        # Парсим ответ
        json_str = response.strip()
        if json_str.startswith("```"):
            json_str = json_str.split("```")[1]
            if json_str.startswith("json"):
                json_str = json_str[4:]
        json_str = json_str.strip()

        raw = json.loads(json_str)

        # Валидируем результат
        result = {}
        for key, value in raw.items():
            try:
                idx = int(key)
            except (ValueError, TypeError):
                continue

            if isinstance(value, str):
                value = value.strip().lower()
                if value in VALID_TYPES:
                    result[idx] = value
                else:
                    logger.debug(f"Entity classification returned invalid type '{value}' for index {idx}")

        return result

    except Exception as e:
        logger.warning(f"Batch entity classification failed: {e}")
        return {}


async def _mark_meeting_processed(
    supabase,
    user_id: str,
    meeting_id: str,
    subscription_id: str,
    status: str
):
    """Пометить встречу как обработанную"""
    processed_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "subscription_id": subscription_id,
        "processed_at": processed_at,
        "status": status,
    }

    # Важно: PostgREST PATCH по фильтру, если строка не найдена, часто возвращает 200 + пустой массив,
    # не бросая исключение. Раньше это приводило к тому, что запись НЕ создавалась и встреча
    # перерабатывалась "по кругу". Поэтому делаем upsert-поведение вручную: PATCH -> если пусто, POST.
    try:
        updated = await supabase._request(
            "PATCH",
            "/rest/v1/processed_meetings",
            params={
                "user_id": f"eq.{user_id}",
                "meeting_id": f"eq.{meeting_id}",
            },
            json_data=payload,
            headers={"Prefer": "return=representation"},
        )
        if updated:
            return
    except Exception as e:
        # если PATCH упал по сети/валидности — попробуем создать
        logger.debug(f"processed_meetings PATCH failed (will try POST): {e}")

    try:
        await supabase._request(
            "POST",
            "/rest/v1/processed_meetings",
            json_data={
                "user_id": user_id,
                "meeting_id": meeting_id,
                "subscription_id": subscription_id,
                "processed_at": processed_at,
                "status": status,
            },
            headers={"Prefer": "return=representation"},
        )
    except Exception as e:
        logger.warning(f"Could not mark meeting as processed: {e}")


async def extract_knowledge_from_transcript(
    transcript: str,
    meeting_title: str,
    meeting_id: str,
    user_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Извлечь знания из транскрипта встречи с помощью LLM.

    Args:
        transcript: Текст транскрипта
        meeting_title: Название встречи
        meeting_id: ID встречи
        user_id: ID пользователя (для трекинга)

    Returns:
        Извлечённые знания или None
    """
    from backend.core.llm.gemini_client import get_gemini_client
    from backend.core.llm.usage_tracker import UsageContext

    try:
        client = get_gemini_client()

        prompt = f"""Проанализируй транскрипт встречи и извлеки структурированные знания.

ВСТРЕЧА: {meeting_title}
ID: {meeting_id}

ТРАНСКРИПТ:
{transcript[:15000]}  # Ограничиваем размер

Извлеки и верни JSON со следующей структурой:
{{
    "entities": [
        {{"name": "название", "type": "person|project|task|decision|concept", "description": "описание"}}
    ],
    "relationships": [
        {{"from": "сущность1", "to": "сущность2", "type": "тип_связи", "description": "описание"}}
    ],
    "key_decisions": [
        {{"decision": "текст решения", "context": "контекст", "participants": ["имена"]}}
    ],
    "action_items": [
        {{"task": "задача", "assignee": "ответственный", "deadline": "срок если есть"}}
    ],
    "summary": "краткое резюме встречи"
}}

ВАЖНО — ТОЛЬКО ИЗ ТРАНСКРИПТА:
- Извлекай ТОЛЬКО то, что реально сказано. Не выдумывай сущности, связи, решения,
  задачи, ответственных и сроки, которых нет в тексте.
- name/имена — дословно; assignee и deadline указывай, только если прозвучали.
- summary — по содержанию встречи, без домыслов о причинах и результатах.
- Пустой список лучше выдуманного элемента. Нет чего-то — верни [] для этого поля.

Возвращай ТОЛЬКО валидный JSON без markdown-разметки."""

        # Трекинг использования LLM для синхронизации
        async with UsageContext(
            user_id=user_id,
            agent_mode="sync",
            request_type="knowledge_extraction"
        ):
            response = await client.generate(prompt, temperature=0.3)

        # Парсим JSON из ответа
        try:
            # Убираем возможную markdown-разметку
            json_str = response.strip()
            if json_str.startswith("```"):
                json_str = json_str.split("```")[1]
                if json_str.startswith("json"):
                    json_str = json_str[4:]
            json_str = json_str.strip()

            knowledge = json.loads(json_str)
            return knowledge
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse LLM response as JSON: {e}")
            return None

    except Exception as e:
        logger.error(f"Error extracting knowledge: {e}")
        return None


async def save_knowledge_to_graph(
    knowledge: Dict[str, Any],
    meeting_id: str,
    user_id: str
) -> Dict[str, Any]:
    """
    Сохранить извлечённые знания в граф.

    Args:
        knowledge: Извлечённые знания
        meeting_id: ID встречи
        user_id: ID пользователя

    Returns:
        Статистика сохранения
    """
    from backend.core.store.graph_builder import GraphBuilder
    from backend.core.store.vector_indexer import VectorIndexer

    result = {
        "entities_created": 0,
        "relationships_created": 0
    }

    try:
        graph_builder = GraphBuilder()
        vector_indexer = VectorIndexer(use_qdrant=False)
        await vector_indexer.connect()

        # Создаём узел встречи
        meeting_node_id = f"meeting_{meeting_id}"
        await graph_builder.create_node(
            node_id=meeting_node_id,
            label="Meeting",
            properties={
                "meeting_id": meeting_id,
                "user_id": user_id,
                "summary": knowledge.get("summary", "")
            }
        )
        result["entities_created"] += 1

        # Создаём сущности
        entity_id_map = {}  # name -> node_id

        for entity in knowledge.get("entities", []):
            entity_name = entity.get("name", "")
            if not entity_name:
                continue

            node_id = f"entity_{meeting_id}_{entity_name.lower().replace(' ', '_')}"
            entity_id_map[entity_name] = node_id

            _etype = entity.get("entity_type") or entity.get("type") or "concept"
            await graph_builder.create_node(
                node_id=node_id,
                label=_etype.capitalize(),
                properties={
                    "name": entity_name,
                    "description": entity.get("description", ""),
                    "entity_type": _etype,
                    "meeting_id": meeting_id,
                    "user_id": user_id
                }
            )
            result["entities_created"] += 1

            # Индексируем в векторную базу
            await vector_indexer.upsert_vector_public(
                collection="tessent_entities",
                vector_id=node_id,
                text=f"{entity_name}: {entity.get('description', '')}",
                metadata={
                    "entity_type": _etype,
                    "meeting_id": meeting_id,
                    "user_id": user_id
                }
            )

            # FIX #2: Связываем с встречей через EXTRACTED_FROM (Knowledge → Meeting)
            await graph_builder.create_relationship(
                from_id=node_id,
                to_id=meeting_node_id,
                rel_type="EXTRACTED_FROM",
                properties={
                    "description": f"Знание '{entity.get('name', '')[:80]}' извлечено из встречи '{knowledge.get('meeting_title', meeting_id)}'",
                    "source_meeting_id": meeting_id,
                }
            )
            result["relationships_created"] += 1

        # Создаём связи между сущностями
        for rel in knowledge.get("relationships", []):
            from_name = rel.get("from", "")
            to_name = rel.get("to", "")

            from_id = entity_id_map.get(from_name)
            to_id = entity_id_map.get(to_name)

            if from_id and to_id:
                await graph_builder.create_relationship(
                    from_id=from_id,
                    to_id=to_id,
                    rel_type=rel.get("type", "RELATED_TO"),
                    properties={"description": rel.get("description", "")}
                )
                result["relationships_created"] += 1

        # Создаём решения
        for i, decision in enumerate(knowledge.get("key_decisions", [])):
            decision_id = f"decision_{meeting_id}_{i}"

            await graph_builder.create_node(
                node_id=decision_id,
                label="Decision",
                properties={
                    "text": decision.get("decision", ""),
                    "context": decision.get("context", ""),
                    "participants": decision.get("participants", []),
                    "meeting_id": meeting_id,
                    "user_id": user_id
                }
            )
            result["entities_created"] += 1

            await graph_builder.create_relationship(
                from_id=meeting_node_id,
                to_id=decision_id,
                rel_type="HAS_DECISION"
            )
            result["relationships_created"] += 1

        # Создаём задачи
        for i, task in enumerate(knowledge.get("action_items", [])):
            task_id = f"task_{meeting_id}_{i}"

            await graph_builder.create_node(
                node_id=task_id,
                label="Task",
                properties={
                    "description": task.get("task", ""),
                    "assignee": task.get("assignee", ""),
                    "deadline": task.get("deadline", ""),
                    "meeting_id": meeting_id,
                    "user_id": user_id
                }
            )
            result["entities_created"] += 1

            await graph_builder.create_relationship(
                from_id=meeting_node_id,
                to_id=task_id,
                rel_type="HAS_TASK"
            )
            result["relationships_created"] += 1

        logger.info(f"Saved knowledge for meeting {meeting_id}: {result}")
        return result

    except Exception as e:
        logger.error(f"Error saving knowledge to graph: {e}")
        raise

