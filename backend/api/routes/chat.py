"""
TESSENT BRAIN - Chat Routes
Эндпоинты для интерактивного чата с мозгом компании

Интегрирован с KAG Reasoning Engine для умных ответов.
"""
import asyncio
import os
from datetime import datetime
from typing import Any
from uuid import uuid4

import structlog
from litestar import Request, Router, get, post
from backend.core.auth.user_guard import enforce_user_id_matches_token
from litestar.exceptions import HTTPException
from litestar.params import Parameter
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)

# Lazy imports для компонентов
_reasoning_engine = None
_graph_builder = None
_vector_indexer = None
_hybrid_search = None  # NEW: Hybrid Search Orchestrator
_enhanced_search = None  # NEW: Enhanced Search Orchestrator

# PER-USER кэш чат-движков (LRU). Раньше движки были одиночными глобалами:
# 1) смена user_id → ПОЛНАЯ пересборка графа+вектора (при 2+ активных юзерах
#    вперемешку — на каждый запрос); 2) enhanced-оркестратор был глобальным
#    синглтоном и НАВСЕГДА держал hybrid_search ПЕРВОГО юзера — риск
#    кросс-тенантного поиска; 3) _personalization сетился на общем объекте.
# Теперь у каждого юзера свой комплект; старые глобалы остаются указателем
# на «текущий» комплект для legacy-кода ниже.
from collections import OrderedDict as _OrderedDict

_ENGINES_BY_USER: "_OrderedDict[str, dict]" = _OrderedDict()
_ENGINES_MAX = int(os.getenv("TESSENT_CHAT_ENGINE_CACHE", "4"))


# === Request/Response Models ===

class ChatMessage(BaseModel):
    """Сообщение в чате"""
    role: str = Field(..., pattern="^(user|assistant|system)$")
    content: str


class ModelTier(str):
    """Уровень модели для чата"""
    STANDARD = "standard"  # gemini-flash-lite-latest (быстрая, экономичная)
    PREMIUM = "premium"    # gemini-3.0-flash-preview (мощная, экспериментальная)


class ChatRequest(BaseModel):
    """Запрос к чату"""
    messages: list[ChatMessage]
    session_id: str | None = None
    stream: bool = False
    context: dict[str, Any] | None = None
    strategy: str | None = None # "auto", "quick", "standard", "deep"
    model_tier: str = "standard"  # "standard" или "premium"
    # Аудит LLM: per-request выбор LLM-профиля (как в /agents/chat) — без
    # него обычный чат всегда шёл через tenant-default профиль
    llm_profile_id: str | None = None
    # Deep dive: читать ПОЛНЫЕ транскрипты топ-встреч вместо сниппетов.
    # Включается кнопкой «Погрузиться глубже» во фронте (второй проход).
    deep_dive: bool = False


class ChatResponse(BaseModel):
    """Ответ чата"""
    session_id: str
    message: ChatMessage
    sources: list[dict[str, Any]] = Field(default_factory=list)
    reasoning_steps: list[str] = Field(default_factory=list)
    response_metadata: dict[str, Any] = Field(default_factory=dict)
    suggested_actions: list[dict[str, Any]] = Field(default_factory=list)


# === Route Handlers ===

@post("/completions")
async def chat_completions(
    data: ChatRequest,
    request: Request,
    user_id: str = Parameter(query="user_id", required=True),
) -> ChatResponse:
    """
    Отправить сообщение в чат и получить ответ

    Этот эндпоинт:
    1. Понимает контекст разговора
    2. Использует KAG Reasoning Engine для ответа
    3. Возвращает структурированный ответ с источниками

    Args:
        data: Сообщения и параметры

    Returns:
        Ответ с reasoning steps и источниками
    """
    session_id = data.session_id or str(uuid4())

    # S1: если запрос несёт ВАЛИДНЫЙ токен (сервисный ИЛИ пользовательский
    # веб-UI JWT) — user_id берём из его claim (service.user_id / user.sub),
    # а попытку дёрнуть чужой ?user_id= блокируем (403). Без токена — passthrough
    # (обратная совместимость; за доверенным прокси). docs/ru/PRODUCTION_HARDENING.md.
    try:
        from backend.core.auth.service_token import trusted_user_id
        user_id, _auth_src = trusted_user_id(request.headers, user_id)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e

    # Записываем запрос в LoadMonitor для адаптивного ночного планировщика
    try:
        from backend.core.automations.scheduler import get_load_monitor
        get_load_monitor().record_request()
    except Exception:
        logger.debug("suppressed exception", exc_info=True)

    # Палантир (enable_data_ontology): вопрос про цифры подключённого
    # датасета → отвечаем планом→кодом, не общим синтезом. Детект
    # консервативный и детерминированный; не уверены → обычный путь.
    try:
        from backend.core.config.feature_flags import get_feature_flags
        if get_feature_flags().enable_data_ontology and not data.strategy:
            from backend.core.ontology.dataset_service import (
                try_dataset_route,
            )
            _last_user_msg = next(
                (m.content for m in reversed(data.messages)
                 if m.role == "user"), "")
            _ds = await try_dataset_route(user_id, _last_user_msg)
            if _ds:
                return ChatResponse(
                    session_id=session_id,
                    message=ChatMessage(role="assistant", content=_ds["text"]),
                    sources=[{"type": "dataset",
                              "dataset_id": _ds["dataset_id"],
                              "title": _ds["dataset_title"]}],
                    reasoning_steps=[
                        "вопрос распознан как запрос к датасету",
                        f"план: {_ds['raw'].get('plan')}",
                        "цифры посчитаны детерминированным кодом"],
                    response_metadata={
                        "route": "dataset_ontology",
                        "assessment": _ds["raw"].get("assessment")})
    except Exception:
        logger.debug("dataset route skipped", exc_info=True)

    # Единые метрики: вопрос называет метрику из metric_registry («какая
    # выручка?») и не сматчился с конкретным датасетом → карточка
    # «из встреч vs из данных» со сверкой. Детект детерминированный.
    try:
        from backend.core.config.feature_flags import get_feature_flags
        if get_feature_flags().enable_data_ontology and not data.strategy:
            from backend.core.ontology.dataset_service import try_metric_route
            _last_user_msg = next(
                (m.content for m in reversed(data.messages)
                 if m.role == "user"), "")
            _mt = await try_metric_route(user_id, _last_user_msg)
            if _mt:
                return ChatResponse(
                    session_id=session_id,
                    message=ChatMessage(role="assistant", content=_mt["text"]),
                    sources=[{"type": "metric", "name": _mt["metric"]}],
                    reasoning_steps=[
                        "вопрос распознан как запрос к единой метрике",
                        "цифры из встреч и таблиц сведены из metric_registry"],
                    response_metadata={
                        "route": "metric_registry",
                        "summary": _mt.get("summary")})
    except Exception:
        logger.debug("metric route skipped", exc_info=True)

    # Эксперт-роутинг (enable_expert_routing, Glean-перенос №2): «у кого
    # узнать про X?» → живые эксперты с ролями вместо синтеза. Детект
    # детерминированный; не кто-вопрос / экспертов нет → обычный путь.
    try:
        from backend.core.config.feature_flags import get_feature_flags
        if get_feature_flags().enable_expert_routing and not data.strategy:
            from backend.core.ontology.expert_finder import try_expert_route
            _last_q = next(
                (m.content for m in reversed(data.messages)
                 if m.role == "user"), "")
            _ex = await try_expert_route(user_id, _last_q)
            if _ex:
                return ChatResponse(
                    session_id=session_id,
                    message=ChatMessage(role="assistant", content=_ex["text"]),
                    sources=[{"type": "expert", "name": e["name"],
                              "evidence": e["evidence"]}
                             for e in _ex["experts"]],
                    reasoning_steps=[
                        "вопрос распознан как «кто знает / у кого узнать»",
                        f"тема: {_ex['topic']}",
                        "эксперты найдены по графу, упоминаниям и ролям"],
                    response_metadata={"route": "expert_finder"})
    except Exception:
        logger.debug("expert route skipped", exc_info=True)

    # Устанавливаем контекст для трекинга использования LLM.
    # Профиль: явный из запроса > ПЕРСИСТЕНТНОЕ предпочтение пользователя >
    # tenant default (цепочка в router._maybe_get_active_profile_client).
    from backend.core.llm.router import set_llm_context
    profile_id = data.llm_profile_id
    if not profile_id:
        try:
            from backend.core.llm.user_llm_pref import get_user_llm_profile
            profile_id = get_user_llm_profile(user_id)
        except Exception:
            profile_id = None
    set_llm_context(user_id=user_id, session_id=session_id, agent_mode="brain",
                    llm_profile_id=profile_id)

    # Получаем последнее сообщение пользователя
    user_message = None
    for msg in reversed(data.messages):
        if msg.role == "user":
            user_message = msg.content
            break

    if not user_message:
        return ChatResponse(
            session_id=session_id,
            message=ChatMessage(
                role="assistant",
                content="Пожалуйста, задайте вопрос."
            ),
            sources=[],
            reasoning_steps=[],
            suggested_actions=[],
        )

    # ═══ SESSION MEMORY: собираем контекст из истории сессии ═══
    # Берём последние N сообщений (кроме текущего) для cross-query контекста
    session_context_text = ""
    if len(data.messages) > 1:
        # Собираем предыдущие пары Q&A (макс. 5 последних)
        history_pairs = []
        messages_list = list(data.messages)
        # Исключаем последнее сообщение (текущий запрос)
        prev_messages = messages_list[:-1] if messages_list else []
        # Берём последние 10 сообщений (5 пар Q&A)
        recent = prev_messages[-10:]
        for msg in recent:
            role_label = "Пользователь" if msg.role == "user" else "Ассистент"
            # Обрезаем длинные ответы
            content = msg.content[:300] if msg.role == "assistant" else msg.content[:200]
            history_pairs.append(f"{role_label}: {content}")

        if history_pairs:
            session_context_text = "\n".join(history_pairs)

    # ═══ OVERLAY ПРАВОК: pending-исправления пользователя видны чату СРАЗУ ═══
    # Two-phase буфер применяется к графу только ночью — раньше пользователь
    # исправлял факт, тут же спрашивал и получал СТАРОЕ значение (удар по
    # доверию). Подмешиваем свежие правки как приоритетный блок контекста.
    try:
        from backend.core.corrections import get_two_phase_manager
        _tpm = get_two_phase_manager(user_id=user_id)
        _pending = (await _tpm.get_pending_corrections(status="pending"))[:15]
        _lines = []
        for c in _pending:
            _ent = c.get("entity_id") or ""
            _fld = c.get("field") or c.get("field_name") or ""
            _val = c.get("new_value") or c.get("corrected_value") or ""
            if _ent and _fld:
                _lines.append(f"- {_ent}.{_fld} = {_val}")
        if _lines:
            session_context_text = (
                "СВЕЖИЕ ПРАВКИ ПОЛЬЗОВАТЕЛЯ (ещё не применены к базе; их "
                "значения ПРИОРИТЕТНЕЕ найденных фактов):\n"
                + "\n".join(_lines) + "\n\n" + session_context_text
            )
    except Exception:
        logger.debug("pending corrections overlay skipped", exc_info=True)

    # ═══ ПОДКЛЮЧЁННЫЙ КОНТЕКСТ: переписки / CRM / задачи ═══
    # Пользователь отметил в «Контексте чата» переписки (в т.ч. режима
    # «только контекст» — не синхронизированные в мозг), данные CRM или
    # задачи — их содержимое приезжает приоритетным блоком во ВСЕ стратегии
    # тем же путём, что overlay правок. Best-effort: чат важнее контекста.
    try:
        from backend.core.messengers.context_bridge import (
            build_extra_context,
            wants_extra_context,
        )
        _extra_ctx_attached = False
        if wants_extra_context(data.context):
            _extra = await build_extra_context(user_id, data.context)
            if _extra:
                session_context_text = (
                    _extra + "\n\n" + session_context_text)
                _extra_ctx_attached = True
    except Exception:
        _extra_ctx_attached = False
        logger.debug("extra context bridge skipped", exc_info=True)

    logger.info(
        "[Chat] Request",
        session_id=session_id,
        message=user_message[:100],
    )

    # Инициализируем компоненты при первом запросе (per-user)
    # Важно: _reasoning_engine может быть None даже при живом _graph_builder (например после reload/reset),
    # поэтому проверяем и его тоже.
    global _reasoning_engine, _graph_builder, _vector_indexer, _hybrid_search, _enhanced_search

    reasoning_steps = []
    if _extra_ctx_attached:
        reasoning_steps.append(
            "🔌 Подключён выбранный контекст (переписки/CRM/задачи)")
    sources = []
    response_content = ""
    response_metadata: dict[str, Any] = {}

    try:
        # Пробуем использовать KAG Reasoning Engine и Enhanced Search
        from backend.core.llm.router import LLMRouter
        from backend.core.search.enhanced_search_orchestrator import (
            get_enhanced_search_orchestrator,
        )

        # NEW: Enhanced Search
        from backend.core.search.hybrid_search_orchestrator import get_hybrid_search_orchestrator
        from backend.core.store.graph_builder import GraphBuilder
        from backend.core.store.tenant_paths import graph_path_for_user, vector_index_path_for_user
        from backend.core.store.vector_indexer import VectorIndexer
        from backend.core.think.reasoning_engine import ReasoningEngine

        graph_path = graph_path_for_user(user_id)
        vector_path = vector_index_path_for_user(user_id)

        # PER-USER движки из LRU-кэша: изоляция не промптом, а объектами.
        _cached_eng = _ENGINES_BY_USER.get(user_id)
        if _cached_eng and getattr(_cached_eng["graph"], "connected", False) \
                and getattr(_cached_eng["vector"], "connected", False):
            _ENGINES_BY_USER.move_to_end(user_id)  # LRU-touch
            _graph_builder = _cached_eng["graph"]
            _vector_indexer = _cached_eng["vector"]
            _reasoning_engine = _cached_eng["reasoning"]
            _hybrid_search = _cached_eng["hybrid"]
            _enhanced_search = _cached_eng["enhanced"]
            reasoning_steps.append("1. Движки из кэша (per-user)")
        else:
            from backend.core.llm.router import LLMRouter
            from backend.core.store.graph_builder import GraphBuilder
            from backend.core.store.graph_view import merged_graph_view_for_user
            from backend.core.store.vector_indexer import VectorIndexer
            from backend.core.think.reasoning_engine import ReasoningEngine

            # Federated read: personal-граф пользователя ∪ org-граф (если он
            # привязан к организации). Это даёт chat'у видеть данные команды,
            # promoted в org-граф из встреч коллег. Save заблокирован — этот
            # builder только для read; writes идут по своим WRITE-путям в
            # personal-граф (knowledge_sync._process_single_meeting).
            # use_networkx=None: auto-detect; для Neo4j-mode merge отключён.
            _graph_builder = await merged_graph_view_for_user(
                user_id,
                use_networkx=None,
            )
            connected = getattr(_graph_builder, "connected", False)

            if connected:
                node_count = _graph_builder.nx_graph.number_of_nodes() if _graph_builder.nx_graph else 0
                reasoning_steps.append(f"1. Граф загружен: {node_count} узлов")
            else:
                reasoning_steps.append("1. ⚠️ Не удалось загрузить граф")

            # Инициализируем VectorIndexer (in-memory)
            # Модель эмбеддингов берётся из config.py (local_embedding_model)
            _vector_indexer = VectorIndexer(
                storage_path=vector_path,
                namespace=user_id,
            )
            await _vector_indexer.connect()
            reasoning_steps.append(f"   Векторный индекс: {_vector_indexer.get_stats().get('total_vectors', 0)} векторов")

            # Инициализируем LLM Router
            llm_router = LLMRouter()

            # Инициализируем reasoning engine с VectorIndexer
            _reasoning_engine = ReasoningEngine(
                graph_builder=_graph_builder,
                vector_indexer=_vector_indexer,
                llm_router=llm_router
            )

            # NEW: Инициализируем Enhanced Search — ПЕРСОНАЛЬНЫЙ инстанс.
            # Раньше get_enhanced_search_orchestrator был глобальным синглтоном
            # и навсегда держал hybrid_search ПЕРВОГО юзера → кросс-тенантный
            # поиск + утечка _personalization между юзерами.
            _hybrid_search = get_hybrid_search_orchestrator(
                vector_indexer=_vector_indexer,
                graph_builder=_graph_builder,
                user_id=user_id
            )
            from backend.core.search.enhanced_search_orchestrator import (
                EnhancedSearchOrchestrator,
            )
            _enhanced_search = EnhancedSearchOrchestrator(
                hybrid_search=_hybrid_search,
                llm_router=llm_router,
            )
            reasoning_steps.append("   Enhanced Search: инициализирован (Vector + BM25 + Graph)")

            # В LRU-кэш; выселяем самый старый комплект сверх лимита.
            _ENGINES_BY_USER[user_id] = {
                "graph": _graph_builder,
                "vector": _vector_indexer,
                "reasoning": _reasoning_engine,
                "hybrid": _hybrid_search,
                "enhanced": _enhanced_search,
            }
            _ENGINES_BY_USER.move_to_end(user_id)
            while len(_ENGINES_BY_USER) > _ENGINES_MAX:
                _old_uid, _old = _ENGINES_BY_USER.popitem(last=False)
                try:
                    await _old["graph"].close(save=False)
                except Exception:
                    logger.debug("evicted engine close failed")

        # Выполняем reasoning
        from backend.core.think.logical_planner import SearchStrategy

        strategy_enum = None
        use_ag2 = False
        meeting_filter = None
        document_filter = None
        use_enhanced_search = True  # По умолчанию используем новый поиск

        # Извлекаем фильтры из контекста
        if data.context:
            meeting_filter = data.context.get("meeting_filter")
            document_filter = data.context.get("document_filter")
            data.context.get("meeting_title", "")

            if meeting_filter:
                reasoning_steps.append(f"📅 Фильтр: {len(meeting_filter) if isinstance(meeting_filter, list) else 1} встреч")
            if document_filter:
                reasoning_steps.append(f"📄 Фильтр: {len(document_filter) if isinstance(document_filter, list) else 1} документов")

        use_instant = False
        use_tasker = False
        use_tz = False
        use_compose = False    # C11: композитор задач-артефактов из чата

        if data.strategy:
            strategy_lower = data.strategy.lower()
            if strategy_lower == "instant":
                use_instant = True
                use_enhanced_search = False
                reasoning_steps.append("2. Выбрана стратегия: INSTANT (снапшот без LLM)")
            elif strategy_lower == "tasker":
                use_tasker = True
                use_enhanced_search = False
                reasoning_steps.append("2. Выбрана стратегия: TASKER (быстрое ТЗ)")
            elif strategy_lower == "tz":
                use_tz = True
                use_enhanced_search = False
                reasoning_steps.append("2. Выбрана стратегия: TZ (детальное техническое задание)")
            elif strategy_lower == "compose":
                # C11: композитор — артефакт-задача из чата (резюме/КП/exit/...).
                # Тот же конвейер, что у POST /compose: досье → схема → оценка.
                use_compose = True
                use_enhanced_search = False
                reasoning_steps.append("2. Выбрана стратегия: COMPOSE (артефакт)")
            elif strategy_lower == "agentic" or strategy_lower == "ag2":
                use_ag2 = True
                use_enhanced_search = False
                reasoning_steps.append("2. Выбрана стратегия: AGENTIC (AG2 Multi-Agent)")
            elif strategy_lower == "legacy":
                use_enhanced_search = False
                reasoning_steps.append("2. Выбрана стратегия: LEGACY (Reasoning Engine)")
            elif strategy_lower == "auto" or not strategy_lower:
                # ═══ AUTO MODE: SearchModeRouter определяет оптимальный режим ═══
                from backend.core.search.search_mode_router import (
                    SearchMode,
                    get_search_mode_router,
                )
                router = get_search_mode_router()
                # LLM fallback для неочевидных запросов
                try:
                    from backend.core.llm.router import LLMRouter
                    _llm_for_router = LLMRouter()
                    auto_mode = await router.classify_with_llm_fallback(user_message, _llm_for_router)
                except Exception:
                    auto_mode = router.classify(user_message)
                reasoning_steps.append(f"2. 🤖 AUTO → {auto_mode.value.upper()}")

                if auto_mode == SearchMode.INSTANT:
                    use_instant = True
                    use_enhanced_search = False
                elif auto_mode == SearchMode.TASKER:
                    use_tasker = True
                    use_enhanced_search = False
                elif auto_mode == SearchMode.QUICK:
                    strategy_enum = SearchStrategy.QUICK
                elif auto_mode == SearchMode.DEEP:
                    strategy_enum = SearchStrategy.DEEP
                else:  # STANDARD
                    strategy_enum = SearchStrategy.STANDARD
            else:
                try:
                    strategy_enum = SearchStrategy(strategy_lower)
                    reasoning_steps.append(f"2. Выбрана стратегия: {strategy_enum.value.upper()}")
                except ValueError:
                    reasoning_steps.append(f"2. ⚠️ Неизвестная стратегия '{data.strategy}', используем AUTO")
        else:
            # Стратегия не указана → AUTO
            from backend.core.search.search_mode_router import SearchMode, get_search_mode_router
            router = get_search_mode_router()
            try:
                from backend.core.llm.router import LLMRouter
                _llm_for_router = LLMRouter()
                auto_mode = await router.classify_with_llm_fallback(user_message, _llm_for_router)
            except Exception:
                auto_mode = router.classify(user_message)
            reasoning_steps.append(f"2. 🤖 AUTO → {auto_mode.value.upper()}")

            if auto_mode == SearchMode.INSTANT:
                use_instant = True
                use_enhanced_search = False
            elif auto_mode == SearchMode.TASKER:
                use_tasker = True
                use_enhanced_search = False
            elif auto_mode == SearchMode.QUICK:
                strategy_enum = SearchStrategy.QUICK
            elif auto_mode == SearchMode.DEEP:
                strategy_enum = SearchStrategy.DEEP
            else:
                strategy_enum = SearchStrategy.STANDARD

        # ═══════════════════════════════════════════════════════════
        # INSTANT MODE: Снапшот без LLM — ответ из кэша за <1 сек
        # ═══════════════════════════════════════════════════════════
        if use_instant:
            try:
                reasoning_steps.append("3. ⚡ Instant: поиск по снапшотам")

                from backend.core.sleep.enhanced_snapshot import get_enhanced_snapshot_generator
                snapshot_gen = get_enhanced_snapshot_generator(_graph_builder, user_id=user_id)

                # Определяем тип запроса по ключевым словам
                query_lower = user_message.lower()

                snapshot_text = None

                # Проект — ищем конкретный проект по имени
                if any(kw in query_lower for kw in ["проект"]):
                    # Сначала берём снапшот компании для списка проектов
                    company_snap = await snapshot_gen.get_company_snapshot()
                    if company_snap and company_snap.active_projects:
                        # Ищем конкретный проект по словам из запроса
                        for proj in company_snap.active_projects:
                            proj_name = proj.get("name", "").lower()
                            if any(word in proj_name for word in query_lower.split() if len(word) > 3):
                                proj_snap = await snapshot_gen.get_project_snapshot(proj.get("id", ""))
                                if proj_snap:
                                    snapshot_text = proj_snap.to_text(max_length=4000)
                                    break

                        # Если конкретный не найден — список всех проектов из компании
                        if not snapshot_text:
                            projects_lines = ["## Активные проекты\n"]
                            for p in company_snap.active_projects:
                                status = p.get("status", "")
                                lead = p.get("lead", "")
                                projects_lines.append(f"- **{p.get('name', 'N/A')}** — {status} (ведёт: {lead})")
                            snapshot_text = "\n".join(projects_lines)

                # Человек — ищем конкретного человека
                elif any(kw in query_lower for kw in ["кто", "сотрудник", "человек", "команда"]):
                    company_snap = await snapshot_gen.get_company_snapshot()
                    if company_snap and company_snap.key_people:
                        # Ищем конкретного человека
                        for person in company_snap.key_people:
                            p_name = person.get("name", "").lower()
                            if any(word in p_name for word in query_lower.split() if len(word) > 3):
                                person_snap = await snapshot_gen.get_person_snapshot(person.get("id", ""))
                                if person_snap:
                                    snapshot_text = person_snap.to_text(max_length=4000)
                                    break

                        # Если конкретный не найден — список всех людей из компании
                        if not snapshot_text:
                            people_lines = ["## Команда\n"]
                            for p in company_snap.key_people:
                                role = p.get("role", "")
                                people_lines.append(f"- **{p.get('name', 'N/A')}** — {role}")
                            snapshot_text = "\n".join(people_lines)

                # Компания целиком (дефолт)
                if not snapshot_text:
                    company_snap = await snapshot_gen.get_company_snapshot()
                    if company_snap:
                        snapshot_text = company_snap.to_text(max_length=4000)

                if snapshot_text:
                    response_content = snapshot_text
                    reasoning_steps.append(f"   ✅ Снапшот найден ({len(snapshot_text)} символов)")
                    # INSTANT+LLM (бэклог №4): «покажи X» — сырой снапшот
                    # мгновенно; а ВОПРОС про X — 1 дешёвый LLM-вызов над
                    # снапшотом, чтобы ответить на вопрос, а не вывалить текст.
                    _is_question = ("?" in user_message) or any(
                        w in query_lower for w in (
                            "почему", "сколько", "когда", "какой", "какие",
                            "как ", "что ", "есть ли", "можно ли"))
                    if _is_question:
                        try:
                            from backend.core.llm import get_llm_router
                            _llm = get_llm_router()
                            if _llm is not None:
                                _prompt = (
                                    "Ответь на вопрос пользователя ТОЛЬКО по "
                                    "данным ниже, кратко и по делу. Если ответа "
                                    "в данных нет — скажи об этом.\n\n"
                                    f"Вопрос: {user_message}\n\nДанные:\n{snapshot_text[:3500]}")

                                async def _legacy(_p):
                                    r = await _llm.generate(prompt=_prompt,
                                                            temperature=0.1, max_tokens=500)
                                    return r.get("text", "") if isinstance(r, dict) else str(r or "")

                                # чат → модель тенанта уровня standard (мелкая) +
                                # Google-страховка, за флагом; иначе прежний путь
                                _txt = ""
                                try:
                                    from backend.core.config import flags as _flags
                                    if getattr(_flags, "enable_tiered_extraction", False):
                                        from backend.core.llm.workload_policy import generate_for_workload
                                        _txt = (await generate_for_workload(
                                            user_id, "chat", _prompt,
                                            fallback_generate=_legacy) or "").strip()
                                except Exception:
                                    _txt = ""
                                if not _txt:
                                    _txt = (await _legacy(None)).strip()
                                if _txt:
                                    response_content = _txt
                                    reasoning_steps.append("   🧠 Instant+LLM: синтез ответа по снапшоту")
                        except Exception:
                            logger.debug("instant llm synth skipped", exc_info=True)
                else:
                    response_content = "Снапшоты ещё не сгенерированы. Запустите синхронизацию или используйте другой режим поиска."
                    reasoning_steps.append("   ⚠️ Снапшоты не найдены")

            except Exception as e:
                logger.error(f"Instant search failed: {e}", exc_info=True)
                reasoning_steps.append(f"3. ⚠️ Instant ошибка: {str(e)[:100]}")
                # Fallback: переключаемся на quick enhanced search
                use_enhanced_search = True
                use_instant = False
                strategy_enum = SearchStrategy.QUICK

        # ═══════════════════════════════════════════════════════════
        # TASKER MODE: Генерация ТЗ через data_driven_orchestrator
        # ═══════════════════════════════════════════════════════════
        if use_tasker:
            try:
                reasoning_steps.append("3. 📋 Tasker: генерация технического задания")

                from backend.core.llm.router import LLMRouter
                from backend.core.think.task_specification.data_driven_orchestrator import (
                    DataDrivenTaskSystem,
                )

                tasker = DataDrivenTaskSystem(
                    storage=_graph_builder,
                    llm_router=LLMRouter(),
                )

                tasker_result = await tasker.process_task(
                    task_description=user_message,
                    additional_context={"user_id": user_id},
                )

                if tasker_result and tasker_result.get("status") != "failed":
                    # Результат — markdown ТЗ из stages.result.markdown
                    final_stage = tasker_result.get("stages", {}).get("result", {})
                    response_content = final_stage.get("markdown", "")
                    # Признак для фронта: этот ответ — ТЗ, можно «в работу»
                    # (кнопка handoff → CLI-исполнители, как в SIMA/vibe).
                    response_metadata["is_tz"] = True
                    response_metadata["tz_source"] = "tasker"

                    if not response_content:
                        # Fallback: пробуем собрать из других полей
                        response_content = str(final_stage) if final_stage else str(tasker_result)

                    # Выполненное ТЗ → в библиотеку результатов (чтобы не терялось)
                    if response_content:
                        try:
                            from backend.core.documents.results_library import save_result_document
                            await save_result_document(
                                user_id, title=(user_message or "ТЗ")[:120],
                                content_markdown=response_content,
                                document_type="task_result",
                                summary="Выполненное ТЗ (Tasker)")
                        except Exception:
                            logger.debug("save task_result to library skipped", exc_info=True)

                    # Метаданные о процессе
                    meta = tasker_result.get("metadata", {})
                    duration = meta.get("duration_seconds", 0)

                    # Что нашли / чего не хватает
                    found_data = tasker_result.get("found_data", {})
                    missing_data = tasker_result.get("missing_data", [])
                    assumptions = tasker_result.get("assumptions", [])

                    reasoning_steps.append(f"   ✅ ТЗ сгенерировано ({len(response_content)} символов, {duration:.1f} сек)")
                    if found_data:
                        reasoning_steps.append(f"   📊 Найдено данных: {len(found_data)} категорий")
                    if missing_data:
                        reasoning_steps.append(f"   ⚠️ Не хватает: {len(missing_data)} типов данных")
                    if assumptions:
                        reasoning_steps.append(f"   💡 Предположений: {len(assumptions)}")
                else:
                    response_content = "Не удалось сгенерировать ТЗ. Попробуйте уточнить задачу."
                    reasoning_steps.append("   ⚠️ Tasker вернул пустой результат")

            except Exception as e:
                logger.error(f"Tasker failed: {e}", exc_info=True)
                reasoning_steps.append(f"3. ⚠️ Tasker ошибка: {str(e)[:100]}")
                # Fallback: переключаемся на deep enhanced search
                use_enhanced_search = True
                use_tasker = False
                strategy_enum = SearchStrategy.DEEP

        # ═══════════════════════════════════════════════════════════
        # COMPOSE MODE (C11): композитор задач-артефактов из веб-чата.
        # Тот же конвейер, что в боте POST /compose: досье → план → артефакт →
        # опц. оценка. Память планов per-user. Best-effort fallback в search.
        # ═══════════════════════════════════════════════════════════
        if use_compose:
            try:
                from backend.core.config.feature_flags import get_feature_flags
                if not get_feature_flags().enable_chat_compose_strategy:
                    raise RuntimeError("flag enable_chat_compose_strategy = false")

                reasoning_steps.append("3. 🧩 Compose: композитор задач-артефактов")

                import anyio

                from backend.api.routes.compose import (
                    ComposeRequest,
                    _run_compose,
                )

                cres = await anyio.to_thread.run_sync(
                    _run_compose,
                    ComposeRequest(
                        user_id=user_id,
                        request=user_message,
                        model_tier=data.model_tier,
                    ),
                )
                response_content = cres.artifact.text or ""
                response_metadata = {
                    "strategy": "compose",
                    "compose": {
                        "schema": cres.artifact.schema_name,
                        "plan_source": cres.plan_source,
                        "data_gaps": list(cres.artifact.data_gaps),
                        "entities": [dict(e) for e in cres.plan.entities],
                        "memory_record_id": cres.memory_record_id,
                    },
                }
                if cres.evaluation is not None:
                    response_metadata["compose"]["evaluation"] = {
                        "decision": cres.evaluation.decision,
                        "score": cres.evaluation.score,
                    }
                reasoning_steps.append(
                    f"   ✅ Артефакт «{cres.artifact.schema_name}» "
                    f"(способ: {cres.plan_source})"
                )
            except Exception as e:
                logger.error(f"Compose failed: {e}", exc_info=True)
                reasoning_steps.append(f"3. ⚠️ Compose ошибка: {str(e)[:100]}")
                # Fallback: переключаемся на enhanced search
                use_enhanced_search = True
                use_compose = False
                strategy_enum = SearchStrategy.STANDARD

        # ═══════════════════════════════════════════════════════════
        # TZ MODE: Детальное техническое задание (Enhanced TZ Generator)
        # ═══════════════════════════════════════════════════════════
        if use_tz:
            try:
                reasoning_steps.append("3. Enhanced TZ Generator: детальная генерация ТЗ")

                from backend.core.llm.router import LLMRouter
                from backend.core.think.task_specification.enhanced_tz_generator import (
                    EnhancedTZGenerator,
                    TZRequest,
                )
                from backend.core.tz.template_prompt import (
                    build_tz_template_resolver,
                )

                _llm_for_tz = LLMRouter()

                # W29: per-tenant TZ template injection. Best-effort —
                # без БД resolver вернёт None, generator уйдёт на seeds.
                _template_resolver = await build_tz_template_resolver()

                tz_gen = EnhancedTZGenerator(
                    llm=_llm_for_tz,
                    enhanced_search=_enhanced_search,
                    graph_builder=_graph_builder,
                    template_resolver=_template_resolver,
                )

                # Собираем фильтры
                tz_meeting_ids = []
                tz_document_ids = []
                if meeting_filter:
                    tz_meeting_ids = meeting_filter if isinstance(meeting_filter, list) else [meeting_filter]
                if document_filter:
                    tz_document_ids = document_filter if isinstance(document_filter, list) else [document_filter]

                tz_request = TZRequest(
                    description=user_message,
                    user_id=user_id,
                    meeting_ids=tz_meeting_ids,
                    document_ids=tz_document_ids,
                    extra_context=session_context_text,
                )

                tz_result = await tz_gen.generate(tz_request)

                response_content = tz_result.markdown
                # Признак для фронта: ответ — ТЗ, доступна отправка в работу
                response_metadata["is_tz"] = True
                response_metadata["tz_source"] = "tz"
                reasoning_steps.extend(tz_result.stages_log)

                # Выполненное ТЗ → в библиотеку результатов (чтобы не терялось)
                if response_content:
                    try:
                        from backend.core.documents.results_library import save_result_document
                        await save_result_document(
                            user_id, title=(user_message or "ТЗ")[:120],
                            content_markdown=response_content,
                            document_type="task_result",
                            summary="Выполненное ТЗ")
                    except Exception:
                        logger.debug("save tz to library skipped", exc_info=True)

                # Добавляем метаданные в конец ТЗ
                if tz_result.gaps:
                    response_content += "\n\n---\n**Пробелы в данных:**\n"
                    for gap in tz_result.gaps:
                        response_content += f"- {gap}\n"

                if tz_result.assumptions:
                    response_content += "\n**Допущения:**\n"
                    for assumption in tz_result.assumptions[:5]:
                        response_content += f"- {assumption}\n"

                sources = [{"name": s, "type": "data_source"} for s in tz_result.data_sources_used[:15]]

            except Exception as e:
                logger.error(f"Enhanced TZ Generator failed: {e}", exc_info=True)
                reasoning_steps.append(f"3. TZ Generator error: {str(e)[:100]}")
                # Fallback: переключаемся на tasker
                try:
                    from backend.core.llm.router import LLMRouter as _LR
                    from backend.core.think.task_specification.data_driven_orchestrator import (
                        DataDrivenTaskSystem,
                    )
                    tasker_fb = DataDrivenTaskSystem(storage=_graph_builder, llm_router=_LR())
                    fb_result = await tasker_fb.process_task(task_description=user_message, additional_context={"user_id": user_id})
                    if fb_result and fb_result.get("status") != "failed":
                        final_stage = fb_result.get("stages", {}).get("result", {})
                        response_content = final_stage.get("markdown", str(fb_result))
                        reasoning_steps.append("4. Fallback: Tasker (DataDriven)")
                    else:
                        response_content = "Не удалось сгенерировать ТЗ. Попробуйте уточнить задачу."
                except Exception:
                    response_content = "Не удалось сгенерировать ТЗ. Попробуйте позже."

        # Enhanced Search Mode (DEFAULT)
        logger.info(f"[Search] mode: use_enhanced_search={use_enhanced_search}, _enhanced_search={_enhanced_search is not None}")

        # Если Enhanced Search запрошен, но не инициализирован — fallback
        if use_enhanced_search and _enhanced_search is None:
            logger.warning("[Warning] Enhanced Search not initialized, falling back to legacy")
            use_enhanced_search = False

        if use_enhanced_search and _enhanced_search:
            try:
                reasoning_steps.append("3. Запуск Enhanced Search Orchestrator")
                logger.info("[Search] Starting Enhanced Search Orchestrator...")

                # Собираем фильтры
                search_filters = {}
                if meeting_filter:
                    search_filters["meeting_id"] = meeting_filter
                if document_filter:
                    search_filters["document_id"] = document_filter

                # Маппинг strategy → search_depth для Enhanced Search
                # Кнопки UI: quick/standard/deep → search_depth
                search_depth = "standard"  # дефолт
                if strategy_enum:
                    strategy_to_depth = {
                        SearchStrategy.QUICK: "quick",
                        SearchStrategy.STANDARD: "standard",
                        SearchStrategy.DEEP: "deep",
                    }
                    search_depth = strategy_to_depth.get(strategy_enum, "standard")
                    reasoning_steps.append(f"   🔍 Search depth: {search_depth}")

                # Собираем memory-context через централизованный loader
                combined_context = ""
                try:
                    from backend.core.memory.context_loader import ChatMemoryContextLoader

                    entity_targets = None
                    if data.context:
                        entity_targets = (
                            data.context.get("entity_targets")
                            or data.context.get("snapshot_targets")
                            or data.context.get("entity_ids")
                        )

                    context_loader = ChatMemoryContextLoader(
                        graph_builder=_graph_builder,
                        user_id=user_id,
                    )
                    loaded_memory_context = await context_loader.build_context(
                        query=user_message,
                        search_depth=search_depth,
                        session_context_text=session_context_text,
                        document_filter=document_filter,
                        entity_targets=entity_targets,
                    )
                    combined_context = loaded_memory_context.to_text()
                    reasoning_steps.extend(
                        [f"   {line}" for line in loaded_memory_context.summary_lines()]
                    )
                except Exception as e:
                    logger.warning(f"Could not load centralized memory context: {e}")

                # ═══ ТЕМПОРАЛЬНЫЙ КОНТЕКСТ ═══
                # Для вопросов «как менялось / эволюция / с сентября»
                # подкладываем РЕАЛЬНУЮ хронологию из TemporalTracker по
                # сущностям из запроса. Иначе «как менялось» отвечалось
                # реконструкцией LLM по обрывкам, без опоры на даты.
                try:
                    import re as _re_t
                    _temporal_q = bool(_re_t.search(
                        r"(как\s+менял|со\s+времен|эволюц|динамик|измен(и|я)л|"
                        r"\bистори[яю]\b|хронолог|over\s+time|с\s+сентяб|"
                        r"раньше|стало|по\s+мере|с\s+\d{1,2}[.\s])",
                        user_message.lower()))
                    if _temporal_q and _graph_builder is not None:
                        from backend.core.temporal.temporal_tracker import (
                            get_temporal_tracker,
                        )
                        tracker = get_temporal_tracker(user_id)
                        try:
                            _hits = await _graph_builder.search_nodes(
                                query=user_message, limit=12)
                        except Exception:
                            _hits = []
                        _ql = user_message.lower()
                        _picked = []
                        _seen = set()
                        for h in _hits or []:
                            nm = (h.get("name") or "").strip()
                            hid = h.get("id")
                            label = (h.get("_label") or h.get("label") or "").lower()
                            if not nm or not hid or hid in _seen:
                                continue
                            if (len(nm) > 2 and nm.lower() in _ql
                                    and label in ("person", "project", "product", "entity")):
                                _picked.append((nm, hid))
                                _seen.add(hid)
                            if len(_picked) >= 3:
                                break
                        _tl_blocks = []
                        for nm, hid in _picked:
                            try:
                                _events = await tracker.get_entity_timeline(hid, limit=40)
                            except Exception:
                                _events = []
                            if not _events:
                                continue
                            _events = sorted(_events, key=lambda e: e.get("event_time") or "")
                            _lines = []
                            for ev in _events[-25:]:
                                dt = (ev.get("event_time") or "")[:10]
                                desc = ev.get("description") or ev.get("event_type") or ""
                                _lines.append(f"  {dt}: {desc}")
                            if _lines:
                                _tl_blocks.append(f"Хронология «{nm}»:\n" + "\n".join(_lines))
                        if _tl_blocks:
                            combined_context = (
                                combined_context
                                + "\n\n=== ХРОНОЛОГИЯ (по датам встреч) ===\n"
                                + "\n\n".join(_tl_blocks)).strip()
                            reasoning_steps.append(
                                f"   🕒 Подключена хронология по {len(_tl_blocks)} сущностям")
                except Exception as e:
                    logger.debug(f"temporal context injection skipped: {e}")

                # ═══ DEEP RESEARCH ENGINE (4-агентный) ═══
                # Для deep + исследовательских запросов используем DeepResearchEngine
                use_deep_research_engine = False
                if search_depth == "deep":
                    import re
                    research_markers = r"\b(исследуй|ресерч|research|проанализируй\s+всё|почему\s+.{10,}|сравни\s+.{10,}|полный\s+анализ)\b"
                    if re.search(research_markers, user_message.lower()):
                        use_deep_research_engine = True

                if use_deep_research_engine:
                    try:
                        from backend.core.search.deep_research_engine import (
                            get_deep_research_engine,
                        )
                        deep_engine = get_deep_research_engine(
                            hybrid_search=_enhanced_search.hybrid_search if _enhanced_search else None,
                            llm_router=_enhanced_search.llm if _enhanced_search else None,
                            graph_builder=_graph_builder,
                        )
                        reasoning_steps.append("🧠 Запуск 4-агентного Deep Research Engine...")

                        report = await deep_engine.research(
                            query=user_message,
                            user_id=user_id,
                            extra_context=combined_context if combined_context.strip() else None,
                        )

                        response_content = report.answer
                        sources = report.sources
                        response_metadata = {
                            "strategy": "deep_research",
                            "confidence": report.confidence,
                            "duration_ms": report.duration_ms,
                        }

                        reasoning_steps.append(f"4. Deep Research завершён ({report.duration_ms}ms, уверенность {report.confidence:.0%})")
                        reasoning_steps.extend(report.research_steps)
                    except Exception as e:
                        logger.warning(f"Deep Research Engine failed, falling back to enhanced: {e}")
                        use_deep_research_engine = False

                if not use_deep_research_engine:
                    # Endpoint-таймаут (аудит P1 №8): висящий LLM-провайдер
                    # держал запрос до 2-5 минут клиентского ожидания без
                    # ответа. Теперь по дедлайну — вежливый ответ с советом,
                    # соединение не висит. TESSENT_CHAT_TIMEOUT_S default 240.
                    import os as _os
                    try:
                        _chat_deadline = float(_os.getenv("TESSENT_CHAT_TIMEOUT_S", "240"))
                    except (TypeError, ValueError):
                        _chat_deadline = 240.0
                    try:
                      if getattr(data, "deep_dive", False):
                        # Второй проход: читаем ПОЛНЫЕ транскрипты топ-встреч.
                        search_result = await asyncio.wait_for(
                            _enhanced_search.deep_dive_search(
                                query=user_message,
                                user_id=user_id,
                                filters=search_filters if search_filters else None,
                                extra_context=combined_context if combined_context.strip() else None,
                                model_tier=data.model_tier,
                            ),
                            timeout=_chat_deadline,
                        )
                      else:
                        # Стандартный Enhanced Search (quick/standard/deep)
                        search_result = await asyncio.wait_for(
                            _enhanced_search.search(
                                query=user_message,
                                user_id=user_id,
                                filters=search_filters if search_filters else None,
                                search_depth=search_depth,
                                extra_context=combined_context if combined_context.strip() else None,
                                model_tier=data.model_tier,
                            ),
                            timeout=_chat_deadline,
                        )
                    except asyncio.TimeoutError:
                        search_result = None
                        response_content = (
                            "⏱ Поиск занял слишком долго и был остановлен "
                            f"({int(_chat_deadline)}с). Попробуйте режим Quick "
                            "или сузьте вопрос; для больших вопросов «по всем "
                            "встречам» используйте 🔬 Research.")
                        reasoning_steps.append("⏱ Таймаут поиска — вежливая остановка")

                    if search_result is not None:
                        response_content = search_result.answer
                        sources = search_result.sources
                        response_metadata = getattr(search_result, "metadata", {}) or {}

                    # Предлагаем углубиться, если уверенность низкая и есть встречи
                    # с транскриптами (и это ещё не был сам deep-dive проход).
                    if search_result is not None and not getattr(data, "deep_dive", False):
                        try:
                            from backend.core.search.enhanced_search_orchestrator import (
                                DEEP_DIVE_CONFIDENCE_THRESHOLD,
                            )
                            conf = float(getattr(search_result, "confidence", 1.0) or 1.0)
                            has_meetings = any(
                                (s or {}).get("meeting_id") for s in (sources or []))
                            if conf < DEEP_DIVE_CONFIDENCE_THRESHOLD and has_meetings:
                                response_metadata["deep_dive_available"] = True
                        except Exception:
                            logger.debug("deep_dive suggtest check skipped", exc_info=True)

                    if search_result is not None:
                        reasoning_steps.append(f"4. Поиск завершен ({search_result.processing_time_ms}ms)")
                        reasoning_steps.extend(search_result.reasoning_steps)

                    # Abstention в QUICK (MEMORY_DESIGN_PRINCIPLES §8 — ОСТОРОЖНО).
                    # ТОЛЬКО quick + пустой ретрив + НЕ-синтез → честное «нет в
                    # памяти». Синтез-вопросы защищены (classify → compile). За
                    # флагом OFF. Best-effort: не роняем чат.
                    try:
                        from backend.core.config.feature_flags import get_feature_flags
                        if (search_depth == "quick" and not sources
                                and get_feature_flags().enable_quick_abstention):
                            from backend.core.think.abstention_advisor import (
                                advise,
                                classify_question_intent,
                            )
                            adv = advise(retrieval_count=0,
                                         intent=classify_question_intent(user_message),
                                         fast_lookup=True)
                            if adv.should_abstain:
                                response_content = adv.message
                                reasoning_steps.append("   ⚪ Quick: нет данных в памяти (abstention)")
                    except Exception:
                        logger.debug("quick abstention skipped", exc_info=True)

            except Exception as e:
                from backend.core.llm.quota import QuotaExceeded
                if isinstance(e, QuotaExceeded) or "quota exceeded" in str(e).lower():
                    # Квота исчерпана. Контракт-безопасно (enterprise): внешние
                    # клиенты (бот/meetflow) дёргают /chat/completions и ждут 200,
                    # а не 429. Поэтому НЕ пробрасываем 429 и НЕ деградируем в
                    # legacy (он тоже упрётся в квоту) — отдаём обычный 200 с
                    # ЧЕСТНЫМ текстом про лимит. use_enhanced_search оставляем
                    # True → legacy-блок ниже не запускается, response_content
                    # сохраняется.
                    logger.warning("Enhanced search blocked by quota: %s", e)
                    _reset = ""
                    try:
                        _ra = getattr(e, "reset_at", "")
                        _reset = f" Сброс лимита: {_ra}." if _ra else ""
                    except Exception:
                        _reset = ""
                    response_content = (
                        "⚠️ Дневной лимит токенов LLM исчерпан — полный анализ "
                        "временно недоступен." + _reset + " Попробуйте позже или "
                        "увеличьте лимит (LLM_QUOTA_ENABLED=false).")
                    response_metadata = {"quota_exhausted": True}
                    reasoning_steps.append("3. ⚠️ Лимит токенов LLM исчерпан — ответ ограничен")
                else:
                    logger.error(f"Enhanced search failed: {e}", exc_info=True)
                    reasoning_steps.append(f"3. ⚠️ Enhanced Search ошибка: {str(e)[:100]}")
                    # Fallback to legacy reasoning
                    use_enhanced_search = False
                    logger.warning("[Fallback] Falling back to legacy ReasoningEngine")

        # AG2 / Agentic Mode — теперь использует Session Agent Pipeline
        if use_ag2:
            try:
                from backend.core.llm.router import LLMRouter
                from backend.core.think.session_agent_pipeline import (
                    AgentSessionContext,
                    SessionAgentPipeline,
                )

                _llm_for_agents = LLMRouter()

                pipeline = SessionAgentPipeline(
                    enhanced_search=_enhanced_search,
                    llm=_llm_for_agents,
                    graph_builder=_graph_builder,
                    max_retries=1
                )

                # Собираем сессионный контекст из истории сообщений
                agent_session_ctx = AgentSessionContext(
                    session_id=session_id,
                    user_id=user_id
                )
                if len(data.messages) > 1:
                    prev_msgs = list(data.messages)[:-1]
                    for msg in prev_msgs[-10:]:
                        if msg.role == "user":
                            agent_session_ctx.previous_queries.append(msg.content[:150])
                        elif msg.role == "assistant":
                            agent_session_ctx.previous_answers.append(msg.content[:200])

                reasoning_steps.append("3. Запуск Agent Pipeline (Planner -> Researcher -> Critic -> Synthesizer)")

                pipeline_result = await pipeline.run(
                    query=user_message,
                    session_context=agent_session_ctx,
                    user_id=user_id,
                    search_depth="deep",
                    extra_context=session_context_text
                )

                response_content = pipeline_result.answer
                sources = pipeline_result.sources
                response_metadata = getattr(pipeline_result, "metadata", {}) or {
                    "strategy": "agent_pipeline",
                    "confidence": getattr(pipeline_result, "confidence", None),
                    "processing_time_ms": getattr(pipeline_result, "processing_time_ms", None),
                }
                reasoning_steps.extend(pipeline_result.reasoning_steps)
                reasoning_steps.append(f"5. Pipeline завершён за {pipeline_result.processing_time_ms}ms, confidence: {pipeline_result.confidence:.2f}")

            except Exception as agent_error:
                agent_err_msg = str(agent_error).encode('ascii', 'replace').decode('ascii')
                logger.error(f"Agent Pipeline failed: {agent_err_msg}")
                reasoning_steps.append(f"3. Agent Pipeline error: {str(agent_error)[:80]}")
                # Fallback: Enhanced Search deep
                try:
                    fallback_result = await _enhanced_search.search(
                        query=user_message,
                        user_id=user_id,
                        search_depth="deep",
                        extra_context=session_context_text
                    )
                    response_content = getattr(fallback_result, 'answer', str(fallback_result))
                    sources = getattr(fallback_result, 'sources', [])
                    response_metadata = getattr(fallback_result, "metadata", {}) or {}
                    reasoning_steps.append("4. Fallback: Enhanced Search deep")
                except Exception:
                    result = await _reasoning_engine.reason(user_message, strategy=SearchStrategy.DEEP)
                    response_content = result.answer or "Не удалось получить ответ."
                    response_metadata = getattr(result, "metadata", {}) or {}
        # Legacy Reasoning Mode (Fallback or explicit)
        # НЕ запускаем legacy если INSTANT или TASKER уже обработали запрос
        if not use_ag2 and not use_enhanced_search and not use_instant and not use_tasker and not use_tz and not use_compose:
            # Формируем контекст для reasoning engine
            reason_context = {}
            if meeting_filter:
                reason_context["meeting_filter"] = meeting_filter

            result = await _reasoning_engine.reason(
                user_message,
                strategy=strategy_enum,
                context={**(reason_context if reason_context else {}), "user_id": user_id}
            )

            # result - это ReasoningResult dataclass, используем атрибуты
            reasoning_steps.append(f"3. Тип запроса: {result.query_type or 'unknown'}")

            # Формируем ответ
            if result.answer:
                response_content = result.answer
                reasoning_steps.append("4. Ответ сгенерирован через KAG Reasoning")
            else:
                response_content = "Не удалось найти ответ на ваш вопрос."
            response_metadata = getattr(result, "metadata", {}) or {}

            # Добавляем источники
            if result.sources:
                sources = result.sources
                reasoning_steps.append(f"5. Найдено источников: {len(sources)}")

            # Добавляем шаги рассуждения из engine
            if result.reasoning_chain:
                for step in result.reasoning_chain[:5]:
                    step_desc = step.get("step", step.get("description", str(step)))
                    reasoning_steps.append(f"  → {step_desc}")

    except ImportError as e:
        logger.warning(f"Reasoning Engine not available: {e}")
        response_content = f"""
Привет! Я Tessbrain — цифровой мозг вашей компании.

**Ваш вопрос:** {user_message}

К сожалению, база знаний пока пуста. Чтобы я мог отвечать на вопросы:

1. **Загрузите встречи** через `/api/v1/analyze/ingest/meeting`
2. **Подождите обработку** — я извлеку сущности, решения и задачи
3. **Задавайте вопросы** — я буду искать ответы в графе знаний

**Примеры вопросов:**
- "Какой статус проекта X?"
- "Чем занимается Иван?"
- "Какие задачи просрочены?"
- "Какие решения приняли на прошлой неделе?"
        """.strip()
        reasoning_steps = [
            "1. Получен запрос пользователя",
            "2. База знаний пуста",
            "3. Возвращена инструкция по загрузке данных",
        ]

    except Exception as e:
        err_msg = str(e).encode('ascii', 'replace').decode('ascii')
        logger.error(f"Reasoning error: {err_msg}", exc_info=True)
        response_content = f"Произошла ошибка при обработке запроса: {e!s}"
        reasoning_steps.append(f"[Error] {e!s}")

    logger.info(f"[OK] Preparing response for session {session_id}, content length: {len(response_content)}")

    # Фоновая задача: извлечение предпочтений + запись query в UserProfileService.
    # Запись query нужна ДВУМ независимым флагам: feedback_learning (паттерны)
    # И profile_curator (ночное LLM-обучение). C8: запись теперь покрывает оба —
    # раньше при включённом только enable_profile_curator история не велась
    # и ночному куратору было нечего обрабатывать.
    if user_message:
        try:
            from backend.core.config.feature_flags import get_feature_flags
            ff = get_feature_flags()

            if ff.enable_user_feedback_learning:
                # 1. Реал-тайм извлечение предпочтений (regex, <1ms)
                from backend.core.capture.agents.chat_preference_extractor import (
                    get_chat_preference_extractor,
                )
                extractor = get_chat_preference_extractor()
                pref_result = extractor.extract_realtime(user_message)

                if pref_result.preferences or pref_result.rules or pref_result.context_updates:
                    # Применяем к профилю асинхронно
                    await extractor.apply_to_profile(pref_result, user_id)
                    logger.info(
                        f"🎓 Extracted {len(pref_result.preferences)} preferences, "
                        f"{len(pref_result.rules)} rules from chat"
                    )

                # 2. Запись запроса в UserProfileService (паттерны, темы)
                from backend.memory.user_profiles import UserProfileService
                profile_svc = UserProfileService()
                await profile_svc.initialize()
                await profile_svc.record_query(user_id, user_message)
            elif getattr(ff, "enable_profile_curator", False):
                # C8 lifecycle: куратор работает ночью на recent_queries.
                # Без этой записи история пуста — куратор не имеет данных.
                # Best-effort; чат не роняется.
                from backend.memory.user_profiles import UserProfileService
                profile_svc = UserProfileService()
                await profile_svc.initialize()
                await profile_svc.record_query(user_id, user_message)
        except Exception as e:
            logger.debug(f"Preference extraction error (non-critical): {e}")

    # ═══ CHAT → MEMORY INGESTION (третий контекст памяти) ═══
    # За флагом enable_chat_memory_ingestion (default OFF). Недеструктивно: пара
    # user_message + assistant_response пишется как retrievable документ. Без
    # lossy-извлечения (измерено в MEM0_STUDY.md). Fire-and-forget — никогда не
    # блокирует ответ и не роняет чат.
    if user_message and response_content:
        try:
            from backend.core.config.feature_flags import get_feature_flags
            if get_feature_flags().enable_chat_memory_ingestion:
                import asyncio as _aio

                from backend.core.memory.chat_ingestion import ingest_chat_exchange
                _aio.create_task(ingest_chat_exchange(
                    user_id=user_id, session_id=session_id,
                    user_message=user_message, assistant_response=response_content,
                    access_group="private",
                ))
        except Exception as e:
            logger.debug(f"Chat memory ingestion scheduling failed (non-critical): {e}")

    # Честное предупреждение: если прямо сейчас идёт синхронизация, граф/вектора
    # дописываются и ответ может быть неполным. Лучше сказать прямо, чем тихо
    # отдать вырожденный результат (см. деградацию Qdrant под нагрузкой ресинка).
    try:
        from backend.api.routes.knowledge_sync import get_active_sync_progress
        _sp = get_active_sync_progress(user_id)
        if _sp and response_content:
            _t, _d = _sp.get("total"), _sp.get("done")
            _scope = f" ({_d}/{_t} встреч)" if _t else ""
            _banner = (
                f"> ⏳ **Идёт синхронизация{_scope}** — часть данных ещё "
                f"обрабатывается, ответ может быть неполным. Повторите запрос "
                f"после завершения для полной картины.\n\n"
            )
            response_content = _banner + response_content
            response_metadata["sync_in_progress"] = _sp
    except Exception as _se:
        logger.debug(f"sync-banner check skipped: {_se}")

    return ChatResponse(
        session_id=session_id,
        message=ChatMessage(
            role="assistant",
            content=response_content,
        ),
        sources=sources,
        reasoning_steps=reasoning_steps,
        response_metadata=response_metadata,
        suggested_actions=[
            {
                "action": "ingest_meeting",
                "description": "Загрузить встречу для анализа",
                "endpoint": "/api/v1/analyze/ingest/meeting",
            },
            {
                "action": "view_graph",
                "description": "Посмотреть граф знаний",
                "endpoint": "/api/v1/entities/graph",
            },
            {
                "action": "view_docs",
                "description": "Документация API",
                "endpoint": "/docs",
            },
        ],
    )


@get("/sessions/{session_id:str}")
async def get_session(session_id: str) -> dict[str, Any]:
    """
    Получить историю сессии чата

    Args:
        session_id: ID сессии

    Returns:
        История сообщений и метаданные сессии
    """
    from backend.db import get_redis

    redis = await get_redis()
    session_data = await redis.get(f"chat_session:{session_id}")

    if session_data:
        return {
            "session_id": session_id,
            "messages": session_data.get("messages", []),
            "created_at": session_data.get("created_at"),
            "last_activity": session_data.get("last_activity"),
        }

    return {
        "session_id": session_id,
        "error": "Session not found",
    }


@post("/sessions/{session_id:str}/clear")
async def clear_session(session_id: str) -> dict[str, Any]:
    """
    Очистить историю сессии

    Args:
        session_id: ID сессии

    Returns:
        Статус очистки
    """
    from backend.db import get_redis

    redis = await get_redis()
    await redis.delete(f"chat_session:{session_id}")

    logger.info("[Session] cleared", session_id=session_id)

    return {
        "session_id": session_id,
        "status": "cleared",
    }


# OpenAI-compatible endpoint for Open WebUI integration
@post("/v1/chat/completions")
async def openai_compatible_chat(data: dict[str, Any]) -> dict[str, Any]:
    """
    OpenAI-совместимый эндпоинт для интеграции с Open WebUI

    Args:
        data: Запрос в формате OpenAI API

    Returns:
        Ответ в формате OpenAI API
    """
    messages = data.get("messages", [])

    # Преобразуем в наш формат
    chat_messages = [
        ChatMessage(role=m["role"], content=m["content"])
        for m in messages
    ]

    request = ChatRequest(
        messages=chat_messages,
        stream=data.get("stream", False),
    )

    response = await chat_completions(request)

    # Возвращаем в формате OpenAI
    return {
        "id": f"chatcmpl-{uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(datetime.utcnow().timestamp()),
        "model": "tessent-brain",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": response.message.content,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": sum(len(m.content.split()) for m in chat_messages),
            "completion_tokens": len(response.message.content.split()),
            "total_tokens": 0,
        },
    }


async def reload_graph_for_user(user_id: str) -> dict[str, Any]:
    """Перезагрузить граф знаний из файла (вызываемая функция).

    Аудит sync: knowledge_sync импортировал её отсюда, но существовал только
    route-handler — ImportError молча глотался, и после синхронизации чат
    отвечал по СТАРОМУ графу до рестарта. Логика вынесена из route."""
    global _reasoning_engine, _graph_builder

    # Сбрасываем кеш
    _reasoning_engine = None
    _graph_builder = None

    # Инициализируем заново (per-user)
    from backend.core.store.graph_builder import GraphBuilder
    from backend.core.store.tenant_paths import graph_path_for_user
    from backend.core.think.reasoning_engine import ReasoningEngine

    _graph_builder = GraphBuilder(
        use_networkx=None,  # Auto-detect backend
        graph_storage_path=graph_path_for_user(user_id)
    )
    await _graph_builder.connect()

    node_count = _graph_builder.nx_graph.number_of_nodes() if _graph_builder.nx_graph else 0
    edge_count = _graph_builder.nx_graph.number_of_edges() if _graph_builder.nx_graph else 0

    _reasoning_engine = ReasoningEngine(
        graph_builder=_graph_builder
    )

    # Сбрасываем кэш снапшотов: иначе страница «Компания» отдаёт СТАРЫЙ
    # снапшот до рестарта (симптом «граф старый, потом подтягивается новый, с
    # запаздыванием»; навигационная карта при этом расходилась с «Компанией»).
    try:
        from backend.core.sleep.enhanced_snapshot import invalidate_snapshot_cache
        invalidate_snapshot_cache(user_id)
    except Exception as e:
        logger.warning(f"[Reload] could not invalidate snapshot cache: {e}")

    logger.info(f"[Reload] Graph reloaded: {node_count} nodes, {edge_count} edges")

    return {
        "status": "reloaded",
        "nodes": node_count,
        "edges": edge_count,
    }


@post("/reload-graph")
async def reload_graph(
    user_id: str = Parameter(query="user_id", required=True),
) -> dict[str, Any]:
    """Перезагрузить граф знаний из файла (HTTP route)."""
    return await reload_graph_for_user(user_id)


@get("/llm-preference")
async def get_llm_preference(
    request: Request,
    user_id: str = Parameter(query="user_id", required=True),
) -> dict[str, Any]:
    """ПЕРСИСТЕНТНЫЙ выбор LLM-профиля пользователя (для чата).

    Цепочка применения: явный llm_profile_id в запросе > это предпочтение >
    tenant default. Анти-спуфинг через trusted_user_id."""
    from backend.core.auth.service_token import trusted_user_id as _tuid
    try:
        uid, src = _tuid(request.headers, user_id)
        if src != "unverified" and uid:
            user_id = uid
    except PermissionError:
        raise HTTPException(status_code=403,
                            detail="token not authorized for requested user_id")
    from backend.core.llm.user_llm_pref import get_user_llm_profile
    return {"user_id": user_id,
            "llm_profile_id": get_user_llm_profile(user_id)}


@post("/llm-preference")
async def set_llm_preference(
    data: dict[str, Any],
    request: Request,
    user_id: str = Parameter(query="user_id", required=True),
) -> dict[str, Any]:
    """Сохранить (или сбросить: llm_profile_id=null) выбор LLM-профиля
    пользователя. Невалидный профиль безопасен — роутер сделает fallback
    на default с warning."""
    from backend.core.auth.service_token import trusted_user_id as _tuid
    try:
        uid, src = _tuid(request.headers, user_id)
        if src != "unverified" and uid:
            user_id = uid
    except PermissionError:
        raise HTTPException(status_code=403,
                            detail="token not authorized for requested user_id")
    from backend.core.llm.user_llm_pref import (
        get_user_llm_profile,
        set_user_llm_profile,
    )
    ok = set_user_llm_profile(user_id, (data or {}).get("llm_profile_id"))
    return {"status": "success" if ok else "error",
            "user_id": user_id,
            "llm_profile_id": get_user_llm_profile(user_id)}


@get("/my-llm-key")
async def get_my_llm_key_pref(
    request: Request,
    user_id: str = Parameter(query="user_id", required=True),
) -> dict[str, Any]:
    """BYOK думающего слоя: включён ли «мой ключ из Интеграций» + какие
    ключи реально доступны (маскированно) и какая модель будет использована."""
    from backend.core.auth.service_token import trusted_user_id as _tuid
    try:
        uid, src = _tuid(request.headers, user_id)
        if src != "unverified" and uid:
            user_id = uid
    except PermissionError:
        raise HTTPException(status_code=403,
                            detail="token not authorized for requested user_id")
    from backend.core.llm.personal_client import personal_key_status
    return {"user_id": user_id, **(await personal_key_status(user_id))}


@post("/my-llm-key")
async def set_my_llm_key_pref(
    data: dict[str, Any],
    request: Request,
    user_id: str = Parameter(query="user_id", required=True),
) -> dict[str, Any]:
    """Включить/выключить «мой ключ из Интеграций» для ИИ Tessbrain.
    body: {enabled: bool, provider?: "anthropic"|"openai"} (без provider —
    авто: что найдётся). Явный llm_profile_id в запросе всё равно сильнее."""
    from backend.core.auth.service_token import trusted_user_id as _tuid
    try:
        uid, src = _tuid(request.headers, user_id)
        if src != "unverified" and uid:
            user_id = uid
    except PermissionError:
        raise HTTPException(status_code=403,
                            detail="token not authorized for requested user_id")
    from backend.core.llm.personal_client import personal_key_status
    from backend.core.llm.user_llm_pref import set_use_my_key
    ok = set_use_my_key(user_id, bool((data or {}).get("enabled")),
                        (data or {}).get("provider") or None)
    return {"status": "success" if ok else "error",
            "user_id": user_id, **(await personal_key_status(user_id))}


@get("/extraction-route")
async def get_extraction_route_pref(
    request: Request,
    user_id: str = Parameter(query="user_id", required=True),
) -> dict[str, Any]:
    """Маршрут ИЗВЛЕЧЕНИЯ встреч: выбранный провайдер/уровень + какая модель
    реально включится (с учётом ключей) + список опций для ЛК."""
    from backend.core.auth.service_token import trusted_user_id as _tuid
    try:
        uid, src = _tuid(request.headers, user_id)
        if src != "unverified" and uid:
            user_id = uid
    except PermissionError:
        raise HTTPException(status_code=403,
                            detail="token not authorized for requested user_id")
    from backend.core.llm import model_router as _R
    from backend.core.llm.user_llm_pref import get_extraction_route
    from backend.core.llm.extraction_route import build_extraction_route
    pref = get_extraction_route(user_id)
    resolved = await build_extraction_route(user_id)
    # не отдаём наружу объект caller
    resolved_public = {k: v for k, v in resolved.items() if k != "caller"}
    resolved_public["caller_ready"] = resolved.get("caller") is not None
    return {"user_id": user_id, "pref": pref, "resolved": resolved_public,
            "options": _R.options_for_ui()}


@post("/extraction-route")
async def set_extraction_route_pref(
    data: dict[str, Any],
    request: Request,
    user_id: str = Parameter(query="user_id", required=True),
) -> dict[str, Any]:
    """Сохранить выбор маршрута извлечения.
    body: {provider?: str, level?: "standard"|"premium", use_own_key?: bool}."""
    from backend.core.auth.service_token import trusted_user_id as _tuid
    try:
        uid, src = _tuid(request.headers, user_id)
        if src != "unverified" and uid:
            user_id = uid
    except PermissionError:
        raise HTTPException(status_code=403,
                            detail="token not authorized for requested user_id")
    from backend.core.llm.user_llm_pref import get_extraction_route, set_extraction_route
    d = data or {}
    ok = set_extraction_route(
        user_id,
        provider=d.get("provider") if "provider" in d else None,
        level=d.get("level") if "level" in d else None,
        use_own_key=bool(d["use_own_key"]) if "use_own_key" in d else None,
    )
    return {"status": "success" if ok else "error",
            "user_id": user_id, "pref": get_extraction_route(user_id)}


@get("/graph-contents")
async def get_graph_contents(user_id: str | None = None) -> dict[str, Any]:
    """
    Получить содержимое графа знаний для отображения в UI

    Args:
        user_id: ID пользователя для фильтрации

    Returns:
        Статистика и списки сущностей
    """
    if not user_id:
        return {"error": "Authentication required"}

    # Важно: не используем глобальный кеш графа для graph-contents.
    # Иначе после sync (когда файл на диске обновился) UI продолжит видеть "пустой граф",
    # потому что в памяти остался старый пустой экземпляр.
    # ФЕДЕРАТИВНЫЙ вид (personal ∪ org) + backend-agnostic чтение. Раньше тут
    # был personal-GraphBuilder + `if nx_graph is None: return "Graph not loaded"`
    # → на Neo4j-деплое главная ВСЕГДА показывала 0 (граф не в файле, а в БД).
    from backend.core.ingest.membership import get_org_for_user
    from backend.core.store.graph_view import merged_graph_view_for_user
    scope = get_org_for_user(user_id) or user_id
    gb = await merged_graph_view_for_user(user_id, use_networkx=None)
    graph = getattr(gb, "nx_graph", None)  # есть только в NetworkX-режиме

    from collections import Counter

    # Узлы — федеративно (tenant_id=None → контекст org_or_user), оба бэкенда.
    nodes = await gb.get_all_nodes_async(label=None, limit=5000, tenant_id=None)

    # Кол-во связей: NetworkX → из графа; Neo4j → count одним запросом (scope).
    total_edges = 0
    if graph is not None:
        total_edges = graph.number_of_edges()
    elif getattr(gb, "driver", None):
        try:
            async with gb.driver.session() as _s:
                _r = await _s.run(
                    "MATCH (a)-[r]->(b) WHERE (a.tenant_id=$sc OR a.user_id=$sc OR a.user_id=$u OR a.tenant_id=$u) "
                    "AND (b.tenant_id=$sc OR b.user_id=$sc OR b.user_id=$u OR b.tenant_id=$u) RETURN count(r) AS c",
                    {"sc": scope, "u": user_id})
                _row = await _r.single()
                total_edges = int(_row["c"]) if _row else 0
        except Exception:
            total_edges = 0

    def _has_text(v: Any) -> bool:
        if v is None:
            return False
        if isinstance(v, str):
            return bool(v.strip())
        return bool(str(v).strip())

    # Группируем по типам
    meetings = [n for n in nodes if n.get("_label") == "Meeting"]
    people = [n for n in nodes if n.get("_label") == "Person"]
    tasks = [n for n in nodes if n.get("_label") == "Task"]
    decisions = [n for n in nodes if n.get("_label") == "Decision"]
    entities = [n for n in nodes if n.get("_label") == "Entity"]
    kpis = [n for n in nodes if n.get("_label") == "KPI"]
    projects = [n for n in nodes if n.get("_label") == "Project"]

    # Сортировка встреч по дате (новые сверху) — чтобы recent_* не брали "случайные" старые/пустые узлы.
    def _meeting_sort_key(m: dict) -> str:
        # created_at / date могут быть ISO строками — сортируем лексикографически (ISO сохраняет порядок)
        return (m.get("date") or m.get("created_at") or "")

    meetings_sorted = sorted(meetings, key=_meeting_sort_key, reverse=True)

    # Индекс узлов по id
    nodes_by_id = {n.get("id"): n for n in nodes if n.get("id")}

    # Поднимаем recent tasks/decisions по рёбрам от самых свежих встреч, и фильтруем "пустые" записи.
    recent_tasks: list[dict[str, Any]] = []
    recent_decisions: list[dict[str, Any]] = []
    seen_task_ids: set[str] = set()
    seen_decision_ids: set[str] = set()

    # Обход соседей встреч возможен только в NetworkX (nx_graph). На Neo4j
    # (graph=None) берём свежие задачи/решения из общих списков ниже.
    for m in (meetings_sorted if graph is not None else []):
        mid = m.get("id")
        if not mid or not graph.has_node(mid):
            continue

        # Соседи встречи (и входящие и исходящие) — отношения в NX не маркируются типом, поэтому фильтруем по _label.
        for nb in graph.neighbors(mid):
            node = nodes_by_id.get(nb)
            if not node:
                continue

            if node.get("_label") == "Task":
                tid = node.get("id")
                if not tid or tid in seen_task_ids:
                    continue
                title = node.get("title") or node.get("name") or ""
                desc = node.get("description") or ""
                if not (_has_text(title) or _has_text(desc)):
                    continue
                recent_tasks.append(
                    {
                        "id": tid,
                        "title": (title or desc)[:60],
                        "status": node.get("status", "?"),
                        "assignee": node.get("assignee", ""),
                    }
                )
                seen_task_ids.add(tid)

            elif node.get("_label") == "Decision":
                did = node.get("id")
                if not did or did in seen_decision_ids:
                    continue
                summary = node.get("summary") or node.get("name") or ""
                details = node.get("details") or ""
                if not (_has_text(summary) or _has_text(details)):
                    continue
                recent_decisions.append(
                    {
                        "id": did,
                        "summary": (summary or details)[:80],
                    }
                )
                seen_decision_ids.add(did)

        if len(recent_tasks) >= 20 and len(recent_decisions) >= 10:
            break

    # Neo4j-фолбэк: свежие задачи/решения из общих списков (без обхода соседей).
    if graph is None:
        def _dkey(n: dict) -> str:
            return n.get("date") or n.get("created_at") or n.get("updated_at") or ""
        for node in sorted(tasks, key=_dkey, reverse=True):
            tid = node.get("id")
            if not tid or tid in seen_task_ids:
                continue
            title = node.get("title") or node.get("name") or ""
            desc = node.get("description") or ""
            if not (_has_text(title) or _has_text(desc)):
                continue
            recent_tasks.append({"id": tid, "title": (title or desc)[:60],
                                 "status": node.get("status", "?"), "assignee": node.get("assignee", "")})
            seen_task_ids.add(tid)
            if len(recent_tasks) >= 20:
                break
        for node in sorted(decisions, key=_dkey, reverse=True):
            did = node.get("id")
            if not did or did in seen_decision_ids:
                continue
            summary = node.get("summary") or node.get("name") or ""
            details = node.get("details") or ""
            if not (_has_text(summary) or _has_text(details)):
                continue
            recent_decisions.append({"id": did, "summary": (summary or details)[:80]})
            seen_decision_ids.add(did)
            if len(recent_decisions) >= 10:
                break

    try:
        await gb.close(save=False)
    except Exception:
        pass

    # Извлекаем папки/группы из access_group
    folders_set = set()
    for n in nodes:
        ag = n.get("access_group", "public")
        if ag and ag != "public":
            folders_set.add(ag)

    # Типы сущностей
    entity_types = Counter(e.get("entity_type", "unknown") for e in entities)

    # Также ищем проекты среди сущностей типа "project"
    project_entities = [e for e in entities if e.get("entity_type") == "project"]

    # Группы доступа
    access_groups = Counter(n.get("access_group", "public") for n in nodes)

    return {
        "stats": {
            "total_nodes": len(nodes),
            "total_edges": total_edges,
            "meetings": len(meetings),
            "people": len(people),
            "tasks": len(tasks),
            "decisions": len(decisions),
            "entities": len(entities),
            "kpis": len(kpis),
        },
        "access_groups": dict(access_groups),
        "entity_types": dict(entity_types.most_common(10)),
        "meetings": [
            {"id": m.get("id"), "title": m.get("title", "?"), "date": m.get("date", "")}
            for m in meetings_sorted[:20]
        ],
        "people": [
            {"id": p.get("id"), "name": p.get("name", "?"), "role": p.get("role", "")}
            for p in people[:30]
        ],
        "recent_tasks": recent_tasks[:20],
        "recent_decisions": recent_decisions[:10],
        "projects": [
            {"id": p.get("id"), "name": p.get("name", "?")}
            for p in projects[:20]
        ] + [
            {"id": e.get("id"), "name": e.get("name", "?")}
            for e in project_entities[:20]
        ],
        "folders": [
            {"id": f, "name": f}
            for f in sorted(folders_set)
        ],
        "user_id": user_id
    }


@get("/index-status")
async def get_index_status(
    user_id: str = Parameter(query="user_id", required=True),
) -> dict[str, Any]:
    """
    Диагностика: показывает, что поиск идёт по per-user графу и per-user векторам.
    """
    from backend.core.store.graph_builder import GraphBuilder
    from backend.core.store.tenant_paths import graph_path_for_user, vector_index_path_for_user
    from backend.core.store.vector_indexer import VectorIndexer

    graph_path = graph_path_for_user(user_id)
    vector_path = vector_index_path_for_user(user_id)

    graph_builder = GraphBuilder(use_networkx=None, graph_storage_path=graph_path)  # Auto-detect backend
    await graph_builder.connect()
    nodes = graph_builder.nx_graph.number_of_nodes() if graph_builder.nx_graph else 0
    edges = graph_builder.nx_graph.number_of_edges() if graph_builder.nx_graph else 0

    vector_indexer = VectorIndexer(
        use_qdrant=None,
        storage_path=vector_path,
        namespace=user_id,
    )
    await vector_indexer.connect()
    vstats = vector_indexer.get_stats()

    return {
        "status": "success",
        "user_id": user_id,
        "graph": {"path": graph_path, "nodes": nodes, "edges": edges},
        "vectors": {
            "path": vector_path,
            "backend": vstats.get("backend"),
            "total_vectors": vstats.get("total_vectors"),
            "embedding_disabled": vstats.get("embedding_disabled"),
            "embedding_disabled_reason": vstats.get("embedding_disabled_reason"),
            "collections": vstats.get("collections"),
        },
    }


# Router
router = Router(
    path="/chat",
    guards=[enforce_user_id_matches_token],
    route_handlers=[
        chat_completions,
        get_session,
        clear_session,
        openai_compatible_chat,
        get_index_status,
        reload_graph,
        get_llm_preference,
        set_llm_preference,
        get_my_llm_key_pref,
        set_my_llm_key_pref,
        get_extraction_route_pref,
        set_extraction_route_pref,
        get_graph_contents,
    ],
    tags=["Chat"],
)



