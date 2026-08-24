# -*- coding: utf-8 -*-
"""
Data Bus Service — Центральный сервис шины данных Tessbrain.

Координирует:
1. Аутентификацию потребителей (API keys)
2. Загрузку SharingPolicy
3. Rate limiting
4. Поиск через Brain с фильтрацией
5. Редактирование конфиденциальных данных
6. Аудит-логирование
"""
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .api_key_manager import APIKeyManager
from .models import (
    POLICY_TEMPLATES,
    ConsumerType,
    DataBusAuditEntry,
    DataBusConsumer,
    SharingPolicy,
)
from .rate_limiter import RateLimiter
from .redaction_engine import RedactionEngine
from .storage import DataBusStorage

logger = logging.getLogger(__name__)


def _sanitize_for_json(obj: Any) -> Any:
    """Рекурсивно конвертирует все значения в JSON-совместимые типы.

    Neo4j возвращает объекты типа neo4j.time.DateTime, neo4j.spatial.Point и т.д.,
    которые не сериализуются стандартными JSON-энкодерами.
    """
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return {str(k): _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(item) for item in obj]
    # neo4j.time.DateTime, datetime, date, etc.
    if hasattr(obj, 'isoformat'):
        return obj.isoformat()
    # neo4j.spatial.Point, bytes, etc.
    return str(obj)


class DataBusService:
    """
    Центральный сервис шины данных.

    Точка входа для всех запросов к шине — как через REST API,
    так и через чат-боты и webhook'и.
    """

    def __init__(self):
        """Инициализация сервиса."""
        self.storage = DataBusStorage()
        self.redaction = RedactionEngine()
        self.rate_limiter = RateLimiter()
        self.key_manager = APIKeyManager()

    # ══════════════════════════════════════════════
    # Аутентификация и авторизация
    # ══════════════════════════════════════════════

    async def resolve_consumer(self, api_key: str) -> Optional[DataBusConsumer]:
        """
        Аутентифицировать потребителя по API ключу.

        Args:
            api_key: API ключ (формат: tdb_{type}_{hex})

        Returns:
            DataBusConsumer с загруженной SharingPolicy, или None
        """
        if not api_key:
            return None

        # Извлекаем prefix для быстрого поиска
        key_hash = APIKeyManager.hash_key(api_key)

        # Ищем в хранилище по хешу ключа
        consumer = await self.storage.find_consumer_by_key_hash(key_hash)
        if not consumer:
            logger.warning(f"Consumer not found for API key hash: {key_hash[:12]}...")
            return None

        # Проверки активности
        if not consumer.is_active:
            logger.warning(f"Consumer {consumer.id} ({consumer.name}) is inactive")
            return None

        if consumer.is_expired:
            logger.warning(f"Consumer {consumer.id} ({consumer.name}) access expired")
            return None

        # Загружаем политику
        if consumer.sharing_policy_id:
            policy = await self.storage.get_policy(consumer.sharing_policy_id)
            if policy and policy.is_active:
                consumer.sharing_policy = policy
            else:
                logger.warning(f"Policy {consumer.sharing_policy_id} not found or inactive")
                return None

        # Обновляем last_access
        await self.storage.update_consumer_last_access(consumer.id)

        return consumer

    # ══════════════════════════════════════════════
    # Запросы к шине данных
    # ══════════════════════════════════════════════

    async def query(
        self,
        api_key: str,
        query_text: str,
        user_id: Optional[str] = None,
        project_id: Optional[str] = None,
        data_type: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        ip_address: Optional[str] = None,
        search_depth: str = "standard",
        response_format: str = "natural",
    ) -> Dict[str, Any]:
        """
        Выполнить текстовый запрос к шине данных.

        Pipeline:
        1. Authenticate consumer
        2. Check rate limit
        3. Search brain with policy filters (Hybrid: Vector + BM25 + Graph + RRF)
        4. Redact results
        5. Synthesize answer (if format=natural) via LLM
        6. Audit log
        7. Return result

        Args:
            api_key: API ключ потребителя
            query_text: Текстовый запрос
            user_id: ID владельца данных (tenant)
            project_id: Фильтр по проекту
            data_type: Фильтр по типу данных
            since: Дата начала (ISO)
            until: Дата конца (ISO)
            ip_address: IP адрес запрашивающего
            search_depth: Глубина поиска: "quick" | "standard" | "deep"
            response_format: Формат ответа:
                "natural" — человеческий ответ на естественном языке (по умолчанию)
                "structured" — JSON с массивом узлов (для API-интеграций)
                "both" — и ответ, и данные

        Returns:
            {"answer": "...", "data": [...], "meta": {...}} или {"error": "...", "code": int}
        """
        # Метка расходов: внешние запросы через шину тратят токены владельца
        # (редакция+синтез, при deep — ещё разведка) — в экране затрат они
        # должны быть видны отдельной строкой, а не «unknown».
        from backend.core.llm.usage_tracker import UsageContext
        async with UsageContext(agent_mode="data_bus",
                                request_type="external_query",
                                user_id=user_id or None):
            return await self._query_inner(
                api_key=api_key, query_text=query_text, user_id=user_id,
                project_id=project_id, data_type=data_type, since=since,
                until=until, ip_address=ip_address, search_depth=search_depth,
                response_format=response_format)

    @staticmethod
    def _tenant_for(consumer, user_id: Optional[str]) -> str:
        """Тенант запроса — ВСЕГДА из ключа, никогда из параметра.

        Раньше стояло `user_id or consumer.tenant_id`: user_id приходил
        query-параметром, guard соответствия токену на X-API-Key не
        распространяется — потребитель тенанта A с валидным ключом мог
        передать user_id=<чужой tenant> и искать по чужому графу. В старом
        контуре external_access та же дыра закрыта allowed_user_ids; здесь
        закрываем жёстче: параметр не расширяет доступ вовсе, максимум —
        совпадает с тенантом ключа.
        """
        base = str(consumer.tenant_id or "")
        if user_id and str(user_id) != base:
            logger.warning(
                "data_bus: user_id=%s из запроса не совпадает с тенантом "
                "ключа %s — игнорируем параметр", user_id, base)
        return base

    async def _query_inner(
        self,
        api_key: str,
        query_text: str,
        user_id: Optional[str] = None,
        project_id: Optional[str] = None,
        data_type: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        ip_address: Optional[str] = None,
        search_depth: str = "standard",
        response_format: str = "natural",
    ) -> Dict[str, Any]:
        start_time = time.time()
        request_id = f"bus_{uuid.uuid4().hex[:12]}"

        # 1. Аутентификация
        consumer = await self.resolve_consumer(api_key)
        if not consumer:
            return {"error": "Invalid or expired API key", "code": 401, "request_id": request_id}

        policy = consumer.sharing_policy
        if not policy:
            return {"error": "No access policy configured", "code": 403, "request_id": request_id}

        # 2. Rate limiting
        allowed, reason = await self.rate_limiter.check_and_increment(
            consumer_id=consumer.id,
            max_per_hour=policy.max_queries_per_hour,
            max_per_day=policy.max_queries_per_day,
        )
        if not allowed:
            await self._audit_log(
                consumer=consumer, query_type="search", query_text=query_text,
                results_count=0, redacted_count=0, status_code=429,
                ip_address=ip_address, response_time_ms=int((time.time() - start_time) * 1000),
                error=reason,
            )
            return {"error": f"Rate limit exceeded: {reason}", "code": 429, "request_id": request_id}

        return await self._run_pipeline(
            consumer=consumer, policy=policy, query_text=query_text,
            user_id=user_id, project_id=project_id, data_type=data_type,
            since=since, until=until, ip_address=ip_address,
            search_depth=search_depth, response_format=response_format,
            request_id=request_id, start_time=start_time)

    async def _run_pipeline(
        self,
        *,
        consumer: DataBusConsumer,
        policy: SharingPolicy,
        query_text: str,
        user_id: Optional[str] = None,
        project_id: Optional[str] = None,
        data_type: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        ip_address: Optional[str] = None,
        search_depth: str = "standard",
        response_format: str = "natural",
        request_id: str = "",
        start_time: float = 0.0,
    ) -> Dict[str, Any]:
        """Общий конвейер выдачи: срез → редакция → синтез → аудит.

        Вынесен из _query_inner, чтобы федеративный путь (доступ по СВЯЗИ
        двух организаций, а не по ключу) шёл через тот же код с теми же
        ситами: вторая реализация границы неизбежно разошлась бы с первой.
        """
        start_time = start_time or time.time()
        request_id = request_id or f"bus_{uuid.uuid4().hex[:12]}"

        # 3. Проверка доступа к проекту
        if project_id and policy.allowed_projects != ["*"]:
            if project_id not in policy.allowed_projects and project_id not in consumer.allowed_project_ids:
                return {"error": f"Access denied to project {project_id}", "code": 403, "request_id": request_id}

        # Валидация параметров
        if search_depth not in ("quick", "standard", "deep"):
            search_depth = "standard"
        if response_format not in ("natural", "structured", "both"):
            response_format = "natural"

        # 4. Поиск через brain (Hybrid: Vector + BM25 + Graph → RRF Fusion)
        try:
            raw_results = await self._search_brain(
                query=query_text,
                user_id=self._tenant_for(consumer, user_id),
                policy=policy,
                project_id=project_id,
                data_type=data_type,
                since=since,
                until=until,
                search_depth=search_depth,
            )
        except Exception as e:
            logger.error(f"Brain search error: {e}")
            raw_results = []

        # 4.5 РАЗВЕДКА (только при allow_deep_search — токены владельца):
        # сводка компании читается ВНУТРИ системы (наружу не попадает) и
        # уточняет поисковые запросы → доп. проход поиска. «Прочитать сводку →
        # уточнить запросы → искать в срезе точнее».
        if getattr(policy, "allow_deep_search", False):
            try:
                extra_qs = await self._recon_queries(
                    query_text, consumer.tenant_id)
                for q2 in extra_qs[:3]:
                    try:
                        more = await self._search_brain(
                            query=q2, user_id=self._tenant_for(consumer, user_id),
                            policy=policy, project_id=project_id,
                            data_type=data_type, since=since, until=until,
                            search_depth="quick")
                        raw_results.extend(more or [])
                    except Exception:
                        continue
                if extra_qs:
                    logger.info(f"data_bus recon: +{len(extra_qs)} уточнённых "
                                f"запросов по сводке (внутренняя навигация)")
            except Exception:
                logger.debug("data_bus recon failed", exc_info=True)
            # дедуп после доп. проходов
            seen_keys = set()
            deduped = []
            for r in raw_results:
                k = (str(r.get("type")), str(r.get("name"))[:80],
                     str((r.get("metadata") or {}).get("meeting_id") or ""))
                if k in seen_keys:
                    continue
                seen_keys.add(k)
                deduped.append(r)
            raw_results = deduped

        # 5. Sanitize Neo4j types → JSON-compatible
        raw_results = [_sanitize_for_json(r) for r in raw_results]

        # 5.5 СРЕЗ: политика открывает только выбранные встречи/папки — всё
        # вне среза отсекается ДО редакции и синтеза (жёсткая граница:
        # результат без meeting_id при заданном срезе тоже не проходит).
        scope_m = set(getattr(policy, "allowed_meeting_ids", None) or [])
        scope_f = {f.lower() for f in (getattr(policy, "allowed_folders", None) or [])}
        if scope_m or scope_f:
            def _in_scope(item: Dict[str, Any]) -> bool:
                meta = item.get("metadata") or {}
                mid = str(meta.get("meeting_id") or item.get("meeting_id") or "")
                fold = str(meta.get("folder") or meta.get("folder_name") or "").lower()
                if scope_m and mid and mid in scope_m:
                    return True
                if scope_f and fold and fold in scope_f:
                    return True
                return False
            before = len(raw_results)
            raw_results = [r for r in raw_results if _in_scope(r)]
            logger.info(f"data_bus scope: {before} → {len(raw_results)} "
                        f"результатов внутри среза потребителя")

        # 6. Фильтрация и редактирование
        filtered_results, redacted_count = self.redaction.redact_results(raw_results, policy)

        # 6.5 ПОЛНОЕ ЧТЕНИЕ В ПРЕДЕЛАХ СРЕЗА: если чанков-улик мало, а срез
        # задан — читаем сами разрешённые встречи (head+tail): это и есть
        # открытый материал. Снапшоты и граф наружу не попадают по построению
        # (срез отсекает всё без meeting_id из списка) — внутри системы они
        # лишь навигация. Паттерны редакции применяются и к транскриптам.
        synth_extra: List[Dict[str, Any]] = []
        if (scope_m and len(filtered_results) < 3
                and getattr(policy, "allow_deep_search", False)):
            import re as _re
            try:
                from backend.db.supabase_client import get_supabase_client
                sb = get_supabase_client()
                for mid in sorted(scope_m)[:3]:
                    try:
                        text = await sb.get_meeting_transcript(mid)
                    except Exception:
                        continue
                    if not text or len(text) < 200:
                        continue
                    if len(text) > 8000:
                        text = text[:4000] + "\n[...середина опущена...]\n" + text[-4000:]
                    for pat in (policy.redaction_patterns or []):
                        try:
                            text = _re.sub(pat.get("pattern", ""),
                                           pat.get("replace", "[скрыто]"), text)
                        except _re.error:
                            continue
                    synth_extra.append({
                        "type": "MeetingTranscript",
                        "name": f"Разрешённая встреча {str(mid)[:8]}",
                        "description": text,
                        "metadata": {"meeting_id": str(mid)},
                    })
                if synth_extra:
                    logger.info(f"data_bus: дочитаны {len(synth_extra)} "
                                f"встреч из среза (чанков было мало)")
            except Exception:
                logger.debug("data_bus full-read failed", exc_info=True)

        # 7. Синтез ответа на естественном языке (если запрошен)
        answer_text = None
        if response_format in ("natural", "both") and (filtered_results or synth_extra):
            try:
                answer_text = await self._synthesize_natural_answer(
                    query=query_text,
                    results=filtered_results + synth_extra,
                    policy=policy,
                    consumer_name=consumer.name,
                )
            except Exception as e:
                logger.error(f"Answer synthesis error: {e}", exc_info=True)
                answer_text = None

        # 8. Аудит
        response_time_ms = int((time.time() - start_time) * 1000)
        await self._audit_log(
            consumer=consumer, query_type="search", query_text=query_text,
            results_count=len(filtered_results), redacted_count=redacted_count,
            status_code=200, ip_address=ip_address, response_time_ms=response_time_ms,
        )

        # 9. Ответ
        result = {
            "request_id": request_id,
            "meta": {
                "count": len(filtered_results),
                "policy": policy.name,
                "redacted_fields": redacted_count,
                "processing_time_ms": response_time_ms,
                "consumer": consumer.name,
                "search_engine": "hybrid_rrf",
                "search_depth": search_depth,
                "response_format": response_format,
            }
        }

        # Формируем ответ в зависимости от формата
        if response_format == "natural":
            result["answer"] = answer_text or "К сожалению, не удалось найти информацию по вашему запросу."
            result["sources_count"] = len(filtered_results)
        elif response_format == "structured":
            result["data"] = filtered_results
        else:  # both
            result["answer"] = answer_text or "К сожалению, не удалось найти информацию по вашему запросу."
            result["data"] = filtered_results

        return result

    async def federated_query(
        self,
        *,
        requester_org: str,
        requester_user: str,
        target_org: str,
        query_text: str,
        search_depth: str = "standard",
        response_format: str = "natural",
        ip_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Запрос к памяти ДРУГОЙ организации по федеративной связи.

        Отличие от query(): доступ решает не ключ, а ОТНОШЕНИЕ двух
        организаций (federation.resolve_access) — рукопожатие состоялось,
        и целевая сторона объявила срез. Дальше — тот же конвейер с теми
        же ситами, что у ключевой выдачи: срез, редакция, синтез, аудит.
        Никакого второго пути через границу.
        """
        from backend.core.data_bus.federation import resolve_access
        from backend.core.data_bus.federation_store import (
            get_federation_store,
        )
        link = get_federation_store().open_link_between(
            requester_org, target_org)
        decision = resolve_access(link, requester_org=requester_org,
                                  target_org=target_org)
        if not decision.get("allowed"):
            return {"error": decision.get("reason") or "доступ закрыт",
                    "code": 403}

        policy = await self.storage.get_policy(decision["policy_id"])
        if not policy:
            # Связь ссылается на политику, которой нет, — это ошибка
            # конфигурации целевой стороны, а не «открыто всё».
            return {"error": "принимающая сторона объявила срез, но его "
                             "политика не найдена — доступ закрыт",
                    "code": 403}

        # Синтетический consumer: держит tenant целевой стороны (чьи
        # данные) и имя запрашивающей (кто спрашивает) — журнал целевой
        # организации показывает, какая компания и что спрашивала.
        consumer = DataBusConsumer(
            id=f"fed_{link.id}_{requester_org}",
            tenant_id=str(target_org),
            name=f"Федерация: {requester_org}",
            consumer_type=ConsumerType.PARTNER,
            sharing_policy_id=decision["policy_id"],
            sharing_policy=policy,
            metadata={"federation_link_id": link.id,
                      "requester_org": str(requester_org),
                      "requester_user": str(requester_user)},
        )

        # Квоты политики действуют и на федеративный путь: связь — не
        # безлимитный канал.
        allowed, reason = await self.rate_limiter.check_and_increment(
            consumer_id=consumer.id,
            max_per_hour=policy.max_queries_per_hour,
            max_per_day=policy.max_queries_per_day,
        )
        if not allowed:
            return {"error": f"Rate limit exceeded: {reason}", "code": 429}

        if search_depth not in ("quick", "standard", "deep"):
            search_depth = "standard"
        if response_format not in ("natural", "structured", "both"):
            response_format = "natural"

        return await self._run_pipeline(
            consumer=consumer, policy=policy, query_text=query_text,
            ip_address=ip_address, search_depth=search_depth,
            response_format=response_format)

    async def get_snapshot(
        self,
        api_key: str,
        entity_type: str,
        entity_id: str,
        level: str = "summary",
        user_id: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Получить снэпшот сущности через шину.

        Args:
            api_key: API ключ
            entity_type: Тип сущности ("project", "product", "team", "department", "company")
            entity_id: ID сущности
            level: Уровень детализации ("snippet", "summary", "full")
            user_id: ID владельца данных
            ip_address: IP адрес

        Returns:
            Отфильтрованный снэпшот
        """
        start_time = time.time()
        request_id = f"bus_{uuid.uuid4().hex[:12]}"

        # Auth
        consumer = await self.resolve_consumer(api_key)
        if not consumer:
            return {"error": "Invalid or expired API key", "code": 401}

        policy = consumer.sharing_policy
        if not policy:
            return {"error": "No access policy configured", "code": 403}

        # Rate limit
        allowed, reason = await self.rate_limiter.check_and_increment(
            consumer.id, policy.max_queries_per_hour, policy.max_queries_per_day,
        )
        if not allowed:
            return {"error": f"Rate limit exceeded: {reason}", "code": 429}

        # Проверяем что entity_type разрешён
        type_capitalized = entity_type.capitalize()
        if "*" not in policy.allowed_node_types and type_capitalized not in policy.allowed_node_types:
            return {"error": f"Access to {entity_type} not allowed by policy", "code": 403}

        # Получаем снэпшот
        try:
            snapshot = await self._get_entity_snapshot(
                entity_type=entity_type,
                entity_id=entity_id,
                level=level,
                user_id=self._tenant_for(consumer, user_id),
            )
        except Exception as e:
            logger.error(f"Snapshot error: {e}")
            snapshot = {}

        # Sanitize Neo4j types
        if snapshot:
            snapshot = _sanitize_for_json(snapshot)

        # Редактируем
        if snapshot:
            redacted_snapshot, redacted_count = self.redaction.redact_snapshot(snapshot, policy)
        else:
            redacted_snapshot, redacted_count = {}, 0

        # Аудит
        response_time_ms = int((time.time() - start_time) * 1000)
        await self._audit_log(
            consumer=consumer, query_type="snapshot",
            query_text=f"{entity_type}/{entity_id}?level={level}",
            results_count=1 if redacted_snapshot else 0, redacted_count=redacted_count,
            status_code=200, ip_address=ip_address, response_time_ms=response_time_ms,
        )

        return {
            "request_id": request_id,
            "snapshot": redacted_snapshot,
            "meta": {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "level": level,
                "policy": policy.name,
                "redacted_fields": redacted_count,
                "processing_time_ms": response_time_ms,
            }
        }

    # ══════════════════════════════════════════════
    # Ingest — двунаправленная федерация (P3)
    # ══════════════════════════════════════════════

    async def ingest(
        self,
        api_key: str,
        items: List[Dict[str, Any]],
        ip_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Принять данные от внешнего партнёра в граф (cross-org federation).

        Pipeline:
        1. Authenticate consumer (тот же resolve_consumer что и query)
        2. Rate limit (общий счётчик с query)
        3. ingest_external_payload: policy-gate + namespace-изоляция +
           provenance + запись через GraphBuilder.create_node
        4. Persist граф + двойной аудит (DataBusAuditEntry + audit_log.emit)

        Запись разрешена ТОЛЬКО если policy.allow_ingest=True
        (по умолчанию False → 403). Provenance ставится сервером.
        """
        start_time = time.time()
        request_id = f"bus_{uuid.uuid4().hex[:12]}"

        consumer = await self.resolve_consumer(api_key)
        if not consumer:
            return {"error": "Invalid or expired API key", "code": 401,
                    "request_id": request_id}

        policy = consumer.sharing_policy
        if not policy:
            return {"error": "No access policy configured", "code": 403,
                    "request_id": request_id}

        allowed, reason = await self.rate_limiter.check_and_increment(
            consumer_id=consumer.id,
            max_per_hour=policy.max_queries_per_hour,
            max_per_day=policy.max_queries_per_day,
        )
        if not allowed:
            return {"error": f"Rate limit exceeded: {reason}", "code": 429,
                    "request_id": request_id}

        from backend.core.store.external_ingest import ingest_external_payload

        # Реальный writer: tenant-изолированный GraphBuilder консьюмера.
        async def _graph_writer(*, node_id, label, properties, access_group):
            try:
                from backend.core.store.graph_builder import GraphBuilder
                from backend.core.store.tenant_paths import graph_path_for_user

                gb = GraphBuilder(
                    use_networkx=None,
                    graph_storage_path=graph_path_for_user(consumer.tenant_id),
                )
                await gb.connect()
                ok = await gb.create_node(
                    node_id=node_id, label=label,
                    properties=properties, access_group=access_group,
                )
                if ok:
                    try:
                        await gb.save()
                    except Exception as exc:
                        logger.debug(f"ingest: graph save failed: {exc}")
                return bool(ok)
            except Exception as exc:
                logger.error(f"ingest graph writer error: {exc}")
                return False

        async def _audit_cb(*, action, consumer_id, detail):
            await self._audit_log(
                consumer=consumer, query_type="ingest",
                query_text=f"{action}: {detail.get('accepted', 0)} accepted",
                results_count=int(detail.get("accepted", 0)),
                redacted_count=int(detail.get("rejected", 0)),
                status_code=200, ip_address=ip_address,
                response_time_ms=int((time.time() - start_time) * 1000),
            )
            try:
                from backend.core.observability import audit_log
                await audit_log.emit(
                    action=action,
                    resource=f"data_bus_consumer:{consumer_id}",
                    metadata=detail,
                    ip=ip_address,
                )
            except Exception as exc:
                logger.debug(f"ingest: audit_log.emit failed: {exc}")

        res = await ingest_external_payload(
            consumer=consumer,
            policy=policy,
            items=items,
            graph_writer=_graph_writer,
            audit=_audit_cb,
        )

        out = res.to_dict()
        out["request_id"] = request_id
        if res.status_code == 403:
            out["error"] = "Ingest not allowed by policy"
        elif res.status_code == 400:
            out["error"] = "Empty or invalid payload"
        return out

    # ══════════════════════════════════════════════
    # Администрирование
    # ══════════════════════════════════════════════

    async def create_consumer(
        self,
        tenant_id: str,
        name: str,
        consumer_type: str,
        contact_email: str = "",
        policy_template: Optional[str] = None,
        custom_policy: Optional[Dict] = None,
        allowed_project_ids: Optional[List[str]] = None,
        expires_at: Optional[str] = None,
        metadata: Optional[Dict] = None,
        allow_ingest: bool = False,
        ingest_max_items_per_request: int = 100,
        allowed_meeting_ids: Optional[List[str]] = None,
        allowed_folders: Optional[List[str]] = None,
        answer_prompt: str = "",
        allow_deep_search: bool = False,
        max_queries_per_day: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Создать нового потребителя шины данных.

        Args:
            tenant_id: ID тенанта (владельца данных)
            name: Имя потребителя
            consumer_type: Тип (investor, contractor, partner, ai_agent, internal_dept, ...)
            contact_email: Email
            policy_template: Шаблон политики ("investor_readonly", "contractor_project", ...)
            custom_policy: Кастомная политика (dict)
            allowed_project_ids: ID разрешённых проектов
            expires_at: Дата истечения (ISO)
            metadata: Доп. метаданные

        Returns:
            {"consumer": {...}, "api_key": "tdb_...", "policy": {...}}
            ВАЖНО: api_key возвращается ТОЛЬКО при создании!
        """
        ct = ConsumerType(consumer_type)

        # 1. Создаём или используем политику
        if custom_policy:
            policy = SharingPolicy(
                id=f"pol_{uuid.uuid4().hex[:12]}",
                tenant_id=tenant_id,
                **custom_policy,
            )
        elif policy_template and policy_template in POLICY_TEMPLATES:
            template = POLICY_TEMPLATES[policy_template]
            policy = SharingPolicy(
                id=f"pol_{uuid.uuid4().hex[:12]}",
                tenant_id=tenant_id,
                name=template.name,
                description=template.description,
                allowed_node_types=list(template.allowed_node_types),
                blocked_node_types=list(template.blocked_node_types),
                max_access_level=template.max_access_level,
                allowed_projects=list(template.allowed_projects),
                field_filters=dict(template.field_filters),
                redaction_patterns=list(template.redaction_patterns),
                max_queries_per_day=template.max_queries_per_day,
                max_queries_per_hour=template.max_queries_per_hour,
                max_results_per_query=template.max_results_per_query,
                audit_required=template.audit_required,
                notify_admin=template.notify_admin,
                is_active=True,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        else:
            # Default: partner_public (самый ограниченный)
            template = POLICY_TEMPLATES["partner_public"]
            policy = SharingPolicy(
                id=f"pol_{uuid.uuid4().hex[:12]}",
                tenant_id=tenant_id,
                name=template.name,
                description=template.description,
                allowed_node_types=list(template.allowed_node_types),
                blocked_node_types=list(template.blocked_node_types),
                max_access_level=template.max_access_level,
                allowed_projects=list(template.allowed_projects),
                field_filters=dict(template.field_filters),
                redaction_patterns=list(template.redaction_patterns),
                max_queries_per_day=template.max_queries_per_day,
                max_queries_per_hour=template.max_queries_per_hour,
                max_results_per_query=template.max_results_per_query,
                is_active=True,
                created_at=datetime.now(timezone.utc).isoformat(),
            )

        # Обновляем allowed_projects если указаны конкретные проекты
        if allowed_project_ids:
            policy.allowed_projects = allowed_project_ids

        # СРЕЗ: конкретные встречи/папки, открытые этому потребителю (как
        # фильтр чата, но наружу), и промпт владельца «как отвечать».
        if allowed_meeting_ids:
            policy.allowed_meeting_ids = [str(m) for m in allowed_meeting_ids][:500]
        if allowed_folders:
            policy.allowed_folders = [str(f).strip() for f in allowed_folders
                                      if str(f).strip()][:50]
        if answer_prompt:
            policy.answer_prompt = str(answer_prompt).strip()[:2000]
        # Расширенный поиск — только явное согласие владельца (его токены).
        policy.allow_deep_search = bool(allow_deep_search)
        # Лимит запросов в день — владелец может ужать шаблонное значение.
        if max_queries_per_day:
            try:
                policy.max_queries_per_day = max(1, min(int(max_queries_per_day), 10000))
            except (TypeError, ValueError):
                pass

        # P3: двунаправленность — write разрешается только явно.
        policy.allow_ingest = bool(allow_ingest)
        try:
            policy.ingest_max_items_per_request = max(
                1, min(int(ingest_max_items_per_request), 1000)
            )
        except (TypeError, ValueError):
            policy.ingest_max_items_per_request = 100

        # 2. Сохраняем политику
        await self.storage.save_policy(policy)

        # 3. Генерируем API ключ
        full_key, key_hash, key_prefix = APIKeyManager.generate_key(ct)

        # 4. Создаём потребителя
        consumer = DataBusConsumer(
            id=f"dbc_{uuid.uuid4().hex[:12]}",
            tenant_id=tenant_id,
            name=name,
            consumer_type=ct,
            contact_email=contact_email,
            api_key_hash=key_hash,
            api_key_prefix=key_prefix,
            sharing_policy_id=policy.id,
            allowed_project_ids=allowed_project_ids or [],
            is_active=True,
            created_at=datetime.now(timezone.utc).isoformat(),
            expires_at=expires_at,
            metadata=metadata or {},
        )

        await self.storage.save_consumer(consumer)

        logger.info(f"Created data bus consumer: {name} ({consumer_type}) key={key_prefix}")

        return {
            "consumer": consumer.to_dict(),
            "api_key": full_key,  # ТОЛЬКО при создании!
            "policy": policy.to_dict(),
        }

    async def list_consumers(self, tenant_id: str) -> List[Dict[str, Any]]:
        """Получить список потребителей для тенанта."""
        consumers = await self.storage.list_consumers(tenant_id)
        result = []
        for c in consumers:
            d = c.to_dict()
            # Добавляем usage
            usage = await self.rate_limiter.get_usage(c.id)
            d["usage"] = usage
            # P3: показать, может ли консьюмер писать (для UI)
            d["allow_ingest"] = False
            if c.sharing_policy_id:
                try:
                    pol = await self.storage.get_policy(c.sharing_policy_id)
                    if pol:
                        d["allow_ingest"] = bool(getattr(pol, "allow_ingest", False))
                except Exception as exc:
                    logger.debug(f"list_consumers: policy load failed: {exc}")
            result.append(d)
        return result

    async def _owned_consumer(self, consumer_id: str,
                              tenant_id: Optional[str]):
        """Consumer, если он принадлежит тенанту. Иначе None.

        Раньше admin-операции шли по одному consumer_id без сверки
        владельца: зная чужой dbc_-идентификатор, можно было деактивировать
        чужого потребителя, а через ротацию — ПОЛУЧИТЬ НОВЫЙ СЕКРЕТ чужого
        ключа, то есть присвоить чужой канал выдачи данных. user_id на
        роуте подтверждён токеном (guard), здесь сверяем владение.
        """
        consumer = await self.storage.get_consumer(consumer_id)
        if not consumer:
            return None
        if tenant_id and str(consumer.tenant_id) != str(tenant_id):
            logger.warning(
                "data_bus: отказ admin-операции — consumer %s принадлежит "
                "другому тенанту", consumer_id)
            return None
        return consumer

    async def deactivate_consumer(self, consumer_id: str,
                                  tenant_id: Optional[str] = None) -> bool:
        """Деактивировать потребителя (только своего тенанта)."""
        if await self._owned_consumer(consumer_id, tenant_id) is None:
            return False
        return await self.storage.deactivate_consumer(consumer_id)

    async def rotate_api_key(self, consumer_id: str,
                             tenant_id: Optional[str] = None,
                             ) -> Optional[Dict[str, str]]:
        """
        Ротация API ключа потребителя (только своего тенанта).

        Returns:
            {"api_key": "new_key", "prefix": "tdb_..."} или None
        """
        consumer = await self._owned_consumer(consumer_id, tenant_id)
        if not consumer:
            return None

        full_key, key_hash, key_prefix = APIKeyManager.generate_key(consumer.consumer_type)

        await self.storage.update_consumer_key(consumer_id, key_hash, key_prefix)

        logger.info(f"Rotated API key for consumer {consumer.name}: {key_prefix}")

        return {"api_key": full_key, "prefix": key_prefix}

    async def get_audit_log(
        self,
        tenant_id: str,
        consumer_id: Optional[str] = None,
        limit: int = 50,
        since: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Получить аудит-лог."""
        entries = await self.storage.get_audit_entries(
            tenant_id=tenant_id,
            consumer_id=consumer_id,
            limit=limit,
            since=since,
        )
        return [e.to_dict() for e in entries]

    async def get_consumer_stats(self, consumer_id: str) -> Dict[str, Any]:
        """Получить статистику потребителя."""
        consumer = await self.storage.get_consumer(consumer_id)
        if not consumer:
            return {"error": "Consumer not found"}

        usage = await self.rate_limiter.get_usage(consumer_id)

        return {
            "consumer": consumer.to_dict(),
            "usage": usage,
        }

    # ══════════════════════════════════════════════
    # Внутренние методы
    # ══════════════════════════════════════════════

    async def _search_brain(
        self,
        query: str,
        user_id: str,
        policy: SharingPolicy,
        project_id: Optional[str] = None,
        data_type: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        search_depth: str = "standard",
    ) -> List[Dict[str, Any]]:
        """
        Поиск в мозге компании с учётом политики.

        Использует полноценный HybridSearchOrchestrator (Vector + BM25 + Graph + RRF Fusion)
        — тот же движок, что работает в чате Brain.

        Args:
            query: Текстовый запрос
            user_id: ID владельца данных (tenant)
            policy: SharingPolicy потребителя
            project_id: Фильтр по проекту
            data_type: Фильтр по типу данных
            since: Дата начала (ISO)
            until: Дата конца (ISO)
            search_depth: Глубина поиска: "quick" (только hybrid, top_k=15),
                          "standard" (hybrid, top_k=25), "deep" (hybrid, top_k=50)
        """
        try:
            from backend.core.search.hybrid_search_orchestrator import (
                HybridSearchStrategy,
                get_hybrid_search_orchestrator,
            )
            from backend.core.store.graph_builder import GraphBuilder
            from backend.core.store.tenant_paths import (
                graph_path_for_user,
                vector_index_path_for_user,
            )
            from backend.core.store.vector_indexer import VectorIndexer

            graph_path = graph_path_for_user(user_id)
            vector_path = vector_index_path_for_user(user_id)

            # Инициализируем компоненты для user_id
            graph_builder = GraphBuilder(
                use_networkx=None,
                graph_storage_path=graph_path,
            )
            await graph_builder.connect()

            vector_indexer = VectorIndexer(
                storage_path=vector_path,
                namespace=user_id,
            )
            await vector_indexer.connect()

            # Построим фильтры на основе policy
            search_filters = {}
            if policy.max_access_level:
                search_filters["max_access_level"] = policy.max_access_level
            if policy.allowed_node_types and "*" not in policy.allowed_node_types:
                search_filters["node_types"] = policy.allowed_node_types
            if project_id:
                search_filters["project_id"] = project_id
            if data_type:
                search_filters["data_type"] = data_type
            if since:
                search_filters["since"] = since
            if until:
                search_filters["until"] = until

            # Определяем top_k на основе search_depth и policy limit
            max_results = policy.max_results_per_query
            if search_depth == "quick":
                top_k = min(15, max_results)
            elif search_depth == "deep":
                top_k = min(50, max_results)
            else:
                top_k = min(25, max_results)

            # HybridSearchOrchestrator — тот же движок что в чате Brain:
            # Vector Search (семантический) + BM25 (лексический) + Graph Search
            # → RRF Fusion (объединение с ранжированием)
            hybrid_search = get_hybrid_search_orchestrator(
                vector_indexer=vector_indexer,
                graph_builder=graph_builder,
                user_id=user_id,
            )

            hybrid_response = await hybrid_search.search(
                query=query,
                strategy=HybridSearchStrategy.ADAPTIVE,
                top_k=top_k,
                filters=search_filters if search_filters else None,
                include_graph=True,
            )

            logger.info(
                f"Data Bus hybrid search: {hybrid_response.total_found} results "
                f"in {hybrid_response.search_time_ms}ms "
                f"(strategy={hybrid_response.strategy_used}, depth={search_depth})"
            )

            # Конвертируем HybridSearchResult → Dict для потребителей шины
            results = self._hybrid_results_to_bus_format(
                hybrid_response.results, search_filters
            )

            # Если гибридный поиск не дал результатов — fallback на graph-only
            if not results:
                logger.info("Hybrid search returned 0 results, falling back to graph-only search")
                results = await self._graph_fallback_search(
                    graph_builder=graph_builder,
                    query=query,
                    policy=policy,
                )

            # Компактификация
            results = [self._compact_node(r) for r in results[:max_results]]
            return results

        except Exception as e:
            logger.error(f"Brain search error: {e}", exc_info=True)
            # Fallback: прямой graph-only поиск если гибридный недоступен
            return await self._graph_fallback_search_safe(query, user_id, policy)

    # Типы, которые содержат ценные бизнес-данные для потребителей шины
    _PRIMARY_TYPES = {
        "meeting", "project", "product", "task", "decision", "kpi",
        "milestone", "team", "department", "person", "entity", "risk",
        "report", "knowledge", "commitment", "pattern", "snapshot",
    }

    def _hybrid_results_to_bus_format(
        self,
        results: list,
        filters: Dict,
    ) -> List[Dict[str, Any]]:
        """
        Конвертирует HybridSearchResult в формат шины данных.

        Ключевые улучшения:
        - Умное извлечение name/description из metadata и text
        - Фильтрация бесполезных relationship без имени
        - Приоритет: сначала узлы (Project, Task...), потом relationships
        - Дедупликация по id
        """
        bus_results = []
        seen_ids = set()
        allowed_types = filters.get("node_types")

        for r in results:
            # Дедупликация
            if r.doc_id in seen_ids:
                continue
            seen_ids.add(r.doc_id)

            node_type = r.metadata.get("type", r.metadata.get("node_type", "Unknown"))

            # Фильтруем по allowed_node_types из policy
            if allowed_types and "*" not in (allowed_types or []):
                type_cap = node_type.capitalize()
                if node_type not in allowed_types and type_cap not in allowed_types:
                    continue

            # Извлекаем name — пробуем несколько источников
            name = (
                r.metadata.get("name")
                or r.metadata.get("title")
                or r.metadata.get("meeting_title")
                or r.metadata.get("from_name")
                or ""
            )

            # Для типов без полезного имени — пытаемся извлечь из text или statement
            if not name:
                statement = r.metadata.get("statement", "")
                if statement:
                    # Берём первые 80 символов statement как name
                    name = statement[:80].strip()
                    if len(statement) > 80:
                        name += "..."
                elif r.text:
                    # Берём начало текста
                    name = r.text[:80].strip()
                    if len(r.text) > 80:
                        name += "..."

            # Пропускаем записи без name и без description — это шум
            is_relationship = node_type.lower() == "relationship"
            is_unknown = node_type.lower() == "unknown"
            if (is_relationship or is_unknown) and not name:
                continue
            # Для любого типа: если нет ни name ни text — пропускаем
            if not name and not r.text:
                continue

            # Извлекаем description — пробуем text, statement, summary
            description = ""
            if r.text:
                description = r.text
            if not description:
                description = (
                    r.metadata.get("statement")
                    or r.metadata.get("description")
                    or r.metadata.get("summary")
                    or ""
                )
            # Для relationship — делаем осмысленное описание
            if is_relationship and not description:
                from_name = r.metadata.get("from_name", "")
                to_name = r.metadata.get("to_name", "")
                rel_type = r.metadata.get("rel_type", "")
                if from_name and to_name:
                    description = f"{from_name} —[{rel_type}]→ {to_name}"

            # Обрезаем description
            if len(description) > 500:
                description = description[:500] + "..."

            # Убираем служебные поля из metadata — оставляем полезные
            skip_meta_keys = {
                "type", "node_type", "name", "title", "text", "content",
                "score", "vector", "embedding", "chunks", "collection",
                "original_id", "statement", "description", "summary",
            }
            clean_metadata = {
                k: v for k, v in r.metadata.items()
                if k not in skip_meta_keys and v is not None and v != ""
            }

            item = {
                "id": r.doc_id,
                "type": node_type,
                "name": name,
                "description": description,
                "relevance_score": round(r.score, 4),
                "search_sources": r.sources,
                "metadata": clean_metadata,
            }

            # Добавляем детализированные скоры
            scores = {}
            if r.vector_score is not None:
                scores["semantic"] = round(r.vector_score, 4)
            if r.bm25_score is not None:
                scores["lexical"] = round(r.bm25_score, 4)
            if r.graph_score is not None:
                scores["graph"] = round(r.graph_score, 4)
            if scores:
                item["scores"] = scores

            bus_results.append(item)

        # Сортировка: сначала первичные типы (Project, Task, Meeting...),
        # потом relationship/proposition, внутри каждой группы — по relevance_score
        def sort_key(item):
            is_primary = item["type"].lower() in self._PRIMARY_TYPES
            return (0 if is_primary else 1, -item["relevance_score"])

        bus_results.sort(key=sort_key)

        return bus_results

    async def _graph_fallback_search(
        self,
        graph_builder,
        query: str,
        policy: SharingPolicy,
    ) -> List[Dict[str, Any]]:
        """
        Fallback: прямой поиск по графу, если гибридный поиск не дал результатов.

        Это упрощённая версия — только graph_builder.search_nodes и find_nodes_by_label.
        """
        results = []
        max_results = policy.max_results_per_query
        allowed_types = policy.allowed_node_types if "*" not in policy.allowed_node_types else None

        try:
            # Текстовый поиск по имени узлов
            if query and hasattr(graph_builder, 'search_nodes'):
                if allowed_types:
                    per_type = max(3, max_results // len(allowed_types))
                    for node_type in allowed_types:
                        if len(results) >= max_results:
                            break
                        nodes = await graph_builder.search_nodes(
                            query=query, node_type=node_type, limit=per_type,
                        )
                        results.extend(nodes)
                else:
                    nodes = await graph_builder.search_nodes(query=query, limit=max_results)
                    results.extend(nodes)

            # Если ничего — берём узлы ключевых типов
            if not results and hasattr(graph_builder, 'find_nodes_by_label'):
                labels = allowed_types or ["Project", "Product", "KPI", "Milestone", "Task"]
                per_label = max(3, max_results // len(labels))
                for label in labels:
                    if len(results) >= max_results:
                        break
                    try:
                        nodes = await graph_builder.find_nodes_by_label(label=label, limit=per_label)
                        for node in nodes:
                            if isinstance(node, dict):
                                node["type"] = node.get("type") or node.get("_label") or label
                                results.append(node)
                    except Exception as e:
                        logger.debug(f"find_nodes_by_label({label}) error: {e}")
        except Exception as e:
            logger.error(f"Graph fallback error: {e}")

        return results[:max_results]

    async def _graph_fallback_search_safe(
        self,
        query: str,
        user_id: str,
        policy: SharingPolicy,
    ) -> List[Dict[str, Any]]:
        """Безопасный fallback — инициализирует graph_builder и делает graph-only поиск."""
        try:
            from backend.core.store.graph_builder import GraphBuilder
            from backend.core.store.tenant_paths import graph_path_for_user

            graph_builder = GraphBuilder(
                use_networkx=None,
                graph_storage_path=graph_path_for_user(user_id),
            )
            await graph_builder.connect()

            results = await self._graph_fallback_search(graph_builder, query, policy)
            return [self._compact_node(r) for r in results]
        except Exception as e:
            logger.error(f"Safe graph fallback error: {e}")
            return []

    @staticmethod
    def _compact_node(node: Dict[str, Any]) -> Dict[str, Any]:
        """Убирает тяжёлые внутренние поля, оставляя полезные данные."""
        skip_keys = {
            "source_quote", "embedding", "vector", "chunks",
            "raw_text", "full_text", "transcript",
        }
        compact = {}
        for k, v in node.items():
            if k in skip_keys:
                continue
            # Обрезаем слишком длинные строки
            if isinstance(v, str) and len(v) > 500:
                compact[k] = v[:500] + "..."
            else:
                compact[k] = v
        return compact

    async def _recon_queries(self, question: str, tenant_id: str) -> List[str]:
        """Разведка для расширенного поиска: сводка компании (ВНУТРЕННЯЯ —
        наружу не отдаётся) уточняет, как искать ответ на вопрос потребителя.
        Один дешёвый LLM-вызов, 2-3 поисковых запроса."""
        try:
            from backend.core.store.tenant_paths import snapshots_dir_for_user
            comp = snapshots_dir_for_user(tenant_id) / "company" / "company.json"
            if not comp.exists():
                return []
            import json as _json
            c = _json.loads(comp.read_text(encoding="utf-8"))
            known = _json.dumps({
                "products": [p.get("name") for p in (c.get("products") or [])][:8],
                "projects": [p.get("name") for p in (c.get("projects") or [])][:8],
                "departments": [d.get("name") for d in (c.get("departments") or [])][:10],
            }, ensure_ascii=False)[:1500]
            from backend.core.llm.router import LLMRouter
            llm = LLMRouter()
            resp = await llm.generate(
                prompt=(
                    f"Вопрос внешнего пользователя: «{question}»\n"
                    f"Внутренняя сводка компании (НЕ раскрывать, только для "
                    f"навигации): {known}\n\n"
                    "Сформулируй 2-3 уточнённых поисковых запроса по базе "
                    "встреч, которые помогут найти ответ (термины компании, "
                    'синонимы). СТРОГО JSON: {"queries": ["...", "..."]}'),
                temperature=0.2, max_tokens=150)
            text = resp.get("text", "") if isinstance(resp, dict) else str(resp)
            import re as _re
            m = _re.search(r"\{[\s\S]*\}", text)
            data = _json.loads(m.group(0)) if m else {}
            return [str(q).strip() for q in (data.get("queries") or [])
                    if isinstance(q, str) and len(str(q).strip()) > 5][:3]
        except Exception:
            logger.debug("recon queries failed", exc_info=True)
            return []

    async def _synthesize_natural_answer(
        self,
        query: str,
        results: List[Dict[str, Any]],
        policy: SharingPolicy,
        consumer_name: str,
    ) -> str:
        """
        Синтез ответа на естественном языке через LLM.

        Берёт отфильтрованные (по policy) результаты поиска
        и генерирует человеческий ответ.
        """
        from backend.core.llm.router import LLMRouter

        llm = LLMRouter()

        # Собираем контекст из результатов
        context_parts = []
        for i, item in enumerate(results[:15], 1):
            node_type = item.get("type", "unknown")
            name = item.get("name", "")
            desc = item.get("description", "")
            meta = item.get("metadata", {})

            part = f"{i}. [{node_type}] {name}"
            if desc:
                part += f"\n   {desc}"

            # Добавляем ключевые метаданные
            useful_meta = []
            for key in ("status", "priority", "date", "progress", "health_score",
                        "trend", "value", "deadline", "assignee", "project_id",
                        "meeting_id", "rel_type", "from_name", "to_name"):
                if meta.get(key):
                    useful_meta.append(f"{key}: {meta[key]}")
            if useful_meta:
                part += f"\n   ({', '.join(useful_meta)})"

            context_parts.append(part)

        context_text = "\n".join(context_parts)

        system_prompt = f"""Ты — аналитик, который отвечает на вопросы через шину данных Tessbrain.
Потребитель: {consumer_name} (политика доступа: {policy.name}).

ПРАВИЛА ОТВЕТА:
1. Отвечай на вопрос кратко и по существу, используя ТОЛЬКО данные из контекста.
2. Если данных недостаточно — честно скажи, что информация ограничена.
3. Используй конкретные факты, имена, даты и цифры из контекста.
4. НЕ выдумывай данные, которых нет в контексте.
5. Отвечай на том же языке, на котором задан вопрос.
6. Будь профессионален — это деловой запрос через API.
7. Если результаты содержат связи (relationships) — объясни их суть.
8. Структурируй ответ, если данных много."""

        # Промпт ВЛАДЕЛЬЦА данных: роль/тон/правила ответа этому потребителю
        # («ты — ассистент методологии, отвечай по шагам»). Дописывается к
        # системным правилам, не заменяя ограничения безопасности.
        owner_prompt = getattr(policy, "answer_prompt", "") or ""
        if owner_prompt:
            system_prompt += (f"\n\nИНСТРУКЦИЯ ВЛАДЕЛЬЦА ДАННЫХ (следуй ей "
                              f"в роли, тоне и формате ответа):\n{owner_prompt}")

        user_prompt = f"""Вопрос: {query}

=== НАЙДЕННЫЕ ДАННЫЕ ({len(results)} результатов) ===
{context_text}

Ответь на вопрос, используя данные выше."""

        try:
            answer = await llm.generate(
                prompt=user_prompt,
                system_prompt=system_prompt,
                temperature=0.3,
                max_tokens=1500,
            )
            return answer.strip()
        except Exception as e:
            logger.error(f"LLM synthesis error: {e}")
            return None

    async def _get_entity_snapshot(
        self,
        entity_type: str,
        entity_id: str,
        level: str,
        user_id: str,
    ) -> Dict[str, Any]:
        """Получить снэпшот сущности."""
        try:
            from backend.core.sleep.enhanced_snapshot import get_enhanced_snapshot_generator
            from backend.core.store.graph_builder import GraphBuilder
            from backend.core.store.tenant_paths import graph_path_for_user

            graph_builder = GraphBuilder(
                use_networkx=None,
                graph_storage_path=graph_path_for_user(user_id),
            )
            await graph_builder.connect()

            snapshot_gen = get_enhanced_snapshot_generator(graph_builder, user_id=user_id)

            if hasattr(snapshot_gen, 'generate_snapshot'):
                snapshot = await snapshot_gen.generate_snapshot(
                    entity_type=entity_type,
                    entity_id=entity_id,
                    level=level,
                )
                return snapshot or {}

        except Exception as e:
            logger.error(f"Snapshot generation error: {e}")

        return {}

    async def _audit_log(
        self,
        consumer: DataBusConsumer,
        query_type: str,
        query_text: str,
        results_count: int,
        redacted_count: int,
        status_code: int,
        ip_address: Optional[str] = None,
        response_time_ms: int = 0,
        error: Optional[str] = None,
    ):
        """Записать событие аудита."""
        entry = DataBusAuditEntry(
            id=f"aud_{uuid.uuid4().hex[:12]}",
            timestamp=datetime.now(timezone.utc).isoformat(),
            consumer_id=consumer.id,
            consumer_name=consumer.name,
            channel_type="api",
            query_type=query_type,
            query_text=query_text,
            results_count=results_count,
            redacted_count=redacted_count,
            policy_applied=consumer.sharing_policy.name if consumer.sharing_policy else "",
            ip_address=ip_address,
            response_time_ms=response_time_ms,
            status_code=status_code,
            error=error,
        )

        await self.storage.save_audit_entry(entry)


# ══════════════════════════════════════════════
# Singleton
# ══════════════════════════════════════════════

_data_bus_service: Optional[DataBusService] = None


def get_data_bus_service() -> DataBusService:
    """Получить экземпляр DataBusService."""
    global _data_bus_service
    if _data_bus_service is None:
        _data_bus_service = DataBusService()
    return _data_bus_service
