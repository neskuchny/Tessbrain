"""
TESSENT BRAIN - LiteStar Application Factory
Создание и конфигурация API приложения
"""
# ── OpenMP duplicate-runtime guard (ДОЛЖНО быть до любого импорта, тянущего
# torch/MKL) ────────────────────────────────────────────────────────────────
# На Windows granian-воркер уже загружает нативные либы (browser-use/playwright,
# google.generativeai, neo4j-driver…), каждая может принести свой OpenMP-рантайм
# (libiomp5md.dll). Когда sentence-transformers/torch лениво грузит модель
# эмбеддингов в первом запросе, второй libiomp5md.dll инициализируется поверх
# первого → нативный abort БЕЗ Python-трейсбека («Unexpected exit from
# worker-1») ровно на «Load pretrained SentenceTransformer». Standalone-скрипт
# не падает, потому что тех либ там нет.
#
# KMP_DUPLICATE_LIB_OK=TRUE снимает fatal-проверку дублирующегося рантайма —
# каноничный и безопасный для inference обходной путь этого Windows-кейса.
# Ставим ДО импорта torch (модель грузится лениво позже) и только если оператор
# не задал значение сам.
import os

os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

# ── OpenBLAS/OMP thread-buffer guard (тоже ДО первого импорта numpy/torch) ────
# Второй нативный краш воркера на Windows: «OpenBLAS error: Memory allocation
# still failed after 10 retries, giving up» → «Unexpected exit from worker-1».
# OpenBLAS при инициализации (первый import numpy — его тянут torch/
# sentence-transformers для e5-эмбеддингов, numpy в vector_indexer и пр.)
# выделяет отдельный буфер на КАЖДЫЙ поток, а потоков по умолчанию = числу
# логических ядер. На многоядерной/зажатой по RAM машине суммарная аллокация
# не проходит и воркер умирает БЕЗ Python-трейсбека ещё до отдачи порта.
# Ограничение до 1 потока убирает мультипликатор буферов — стандартный и
# безопасный для inference фикс. setdefault: оператор на «толстом» сервере
# может поднять значение (напр. OPENBLAS_NUM_THREADS=8) через окружение.
for _thr_var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_thr_var, "1")

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

# Load environment variables
from dotenv import load_dotenv
from litestar import Litestar, Router
from litestar.config.cors import CORSConfig
from litestar.openapi import OpenAPIConfig
from litestar.openapi.spec import Contact, License

load_dotenv()

# Конфигурируем structlog ДО первого импорта роутеров — иначе их `getLogger`
# подцепит дефолтный stderr-handler и мы получим разнобой формата.
from backend.config import settings as _settings
from backend.core.observability import configure_logging

configure_logging(level=_settings.log_level, fmt=_settings.log_format)

from backend.api.routes import (
    a2a,
    admin_audit,
    admin_retention,
    agent_automations,
    agents,
    aggregation,
    analysis,
    analyze,
    auth,
    automations,
    meeting_docs,
    blind_spots,
    bookmarks,
    boards,
    bridge,
    broadcast,
    cognitive_core,
    identity,
    inbound,
    chat,
    coffee,
    compose,
    corrections,
    cross_meeting,
    data_bus,
    guide,
    demo,
    desync,
    sync,
    documents,
    documents_fill,
    documents_gen,
    boardroom,
    consent,
    graph_export,
    mcp_http,
    mcp_tokens,
    agent_layer,
    federation,
    local_capture,
    person_twin,
    psych_insights,
    pulse,
    entities,
    entity_resolution,
    executor,
    external_access,
    gdpr,
    goals,
    health,
    human_thinking,
    insights,
    observer,
    integrations,
    integrations_callinsight,
    integrations_daily_pulse,
    knowledge_editor,
    knowledge_sync,
    research,
    billing_admin,
    billing,
    llm_profiles,
    chat_sources,
    agent_skills,
    client_sim,
    communities,
    mark_research,
    meetflow,
    memory,
    messengers,
    methodology_reports,
    holdings,
    onboarding,
    ontology_datasets,
    orgs,
    permissions_api,
    persona,
    reactive,
    readiness,
    reports,
    scim,
    sessions,
    sharing,
    sima,
    skills,
    snapshots,
    table_understanding,
    task_analysis,
    templates,
    temporal,
    tz_templates,
    usage,
    validator,
    wisdom,
)
from backend.api.routes import (
    lineage as lineage_routes,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: Litestar) -> AsyncGenerator[None, None]:
    """
    Lifecycle manager для инициализации и закрытия ресурсов
    """
    logger.info("🚀 Starting Tessbrain API...")

    # Видимость LLM-ключей на старте: «добавил ключ, а не работает» почти
    # всегда = не перезапустили процесс / ключ не в том .env. Одна строка
    # снимает вопрос (маршрут моделей: docs/MODEL_ALLOCATION_STRATEGY.md).
    try:
        import os as _os
        _keys = {
            "gemini": bool(_os.environ.get("GEMINI_API_KEY") or _os.environ.get("GOOGLE_API_KEY")),
            "deepseek": bool(_os.environ.get("DEEPSEEK_API_KEY")),
            "xai": bool(_os.environ.get("XAI_API_KEY")),
            "openai": bool(_os.environ.get("OPENAI_API_KEY")),
            "anthropic": bool(_os.environ.get("ANTHROPIC_API_KEY")),
            "qwen": bool(_os.environ.get("DASHSCOPE_API_KEY") or _os.environ.get("QWEN_API_KEY")),
        }
        _have = [k for k, v in _keys.items() if v]
        _miss = [k for k, v in _keys.items() if not v]
        logger.info(f"🔑 LLM provider keys: есть [{', '.join(_have) or '—'}]"
                    + (f" · нет [{', '.join(_miss)}]" if _miss else ""))
        logger.info("🎚️ Premium-цепочка платформы: grok-4.5(xai) → deepseek-4-pro"
                    "(deepseek) → flash-lite(gemini) — идёт первый, у кого есть ключ")
    except Exception:
        pass

    # Офлайн-лицензия (enterprise-образы: TESSENT_LICENSE_REQUIRED=on) —
    # без валидной лицензии приложение НЕ стартует; наш SaaS/dev — no-op.
    # RuntimeError намеренно НЕ ловим: невалидная лицензия должна валить boot.
    from backend.core.licensing import enforce_license_on_startup
    enforce_license_on_startup()

    # Целостность ядра (enterprise: TESSENT_INTEGRITY_REQUIRED=on) — правка
    # crown-jewels (вырезать лицензию/подменить промпты) ломает подпись
    # манифеста → boot заблокирован. Наш SaaS/dev — no-op.
    from backend.core.integrity import enforce_integrity_on_startup
    enforce_integrity_on_startup()

    # Запуск планировщика автоматизаций
    try:
        from backend.core.automations.scheduler import start_scheduler
        await start_scheduler()
        logger.info("✅ Automation Scheduler started")

        # Long-polling Telegram-бота для локальной разработки (без туннеля).
        # TELEGRAM_POLLING=on → фоновая задача getUpdates; на проде держать
        # выключенным (там вебхук; поллер его снимает).
        try:
            from backend.core.messengers.tg_polling import (
                start_polling_if_enabled,
            )
            if start_polling_if_enabled():
                logger.info("📥 Telegram long-polling запущен "
                            "(TELEGRAM_POLLING=on)")
        except Exception:
            logger.warning("tg polling startup failed", exc_info=True)

        # Событийные триггеры процесс-досок: подписываем один обработчик на
        # каждый тип события мозга (доска с trigger_type=event запускается по
        # событию). Идемпотентно, переживает рестарт.
        try:
            from backend.core.board.event_triggers import (
                register_board_event_handlers,
            )
            register_board_event_handlers()
        except Exception:
            logger.warning("board event triggers: регистрация пропущена",
                           exc_info=True)
    except Exception as e:
        # Аудит: в проде это значит «ни одна автоматизация не запустится» —
        # не прячем в warning. Приложение не валим (чат/поиск важнее), но
        # сигнал должен попасть в алёртинг по CRITICAL.
        from backend.config import settings as _settings
        if getattr(_settings, "app_env", "development") == "production":
            logger.critical(f"❌ AUTOMATION SCHEDULER FAILED — автоматизации не работают: {e}")
        else:
            logger.warning(f"⚠️ Could not start automation scheduler: {e}")

    # Инициализация Business Wisdom Engine (создание таблиц + Qdrant коллекции)
    try:
        from backend.core.wisdom.wisdom_engine import get_wisdom_engine
        await get_wisdom_engine()
        logger.info("✅ Business Wisdom Engine initialized")
    except Exception as e:
        logger.warning(f"⚠️ Could not initialize Business Wisdom Engine: {e}")

    # Важно: НЕ инициализируем общий граф при старте.
    # Граф и векторные индексы теперь tenant-aware и загружаются ЛЕНИВО по user_id
    # (например, через /api/v1/chat/completions?user_id=...).
    # Это устраняет утечки/смешивание данных между пользователями.
        app.state.graph = None

    yield

    # Закрытие соединений (НЕ сохраняем граф при закрытии API - он может быть пустым)
    logger.info("🔄 Shutting down Tessbrain API...")

    # Остановка планировщика
    try:
        from backend.core.automations.scheduler import stop_scheduler
        await stop_scheduler()
        logger.info("✅ Automation Scheduler stopped")
    except Exception as e:
        logger.warning(f"⚠️ Could not stop automation scheduler: {e}")
    # НЕ вызываем close() чтобы не перезаписать файл графа
    logger.info("✅ Shutdown complete")


def create_app() -> Litestar:
    """
    Фабрика создания LiteStar приложения
    """

    # CORS конфигурация (W6 prod-hardening): whitelist из settings,
    # никаких "*" вместе с allow_credentials — это CSRF-вектор.
    cors_config = CORSConfig(
        allow_origins=_settings.cors_allowed_origins,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "authorization",
            "content-type",
            "x-tenant-id",
            "x-org-id",
            "x-request-id",
            "idempotency-key",
        ],
        allow_credentials=True,
    )

    # OpenAPI конфигурация
    openapi_config = OpenAPIConfig(
        title="Tessbrain API",
        version="0.1.0",
        description="""
# 🧠 Tessbrain API

Цифровой мозг компании - интеллектуальная система анализа и управления знаниями.

## Основные возможности

- **Ingestion**: Загрузка и анализ встреч, документов
- **Knowledge Graph**: Построение графа знаний компании
- **KAG Reasoning**: Умные ответы с multi-hop reasoning
- **Snapshots**: Слепки состояния сущностей
- **Real-time Chat**: Интерактивный чат с мозгом компании
- **Business Wisdom Engine**: Корпоративная мудрость — распознавание паттернов ситуаций

## Business Wisdom Engine (BWE)

BWE превращает корпоративный опыт в оперативную мудрость.
Слои: Tessbrain → Decision Extractor → Outcome Linker → JEPA Pattern Encoder → Wisdom Index → Wisdom Query Engine

```
Транскрипт встречи → Decision Events → Outcome Loop → Паттерны → Мудрость
```

## Архитектура

```
User Request → Intent Engine → KAG Reasoner → Response
                    ↓
              Graph + Vectors + Wisdom Index
```
        """,
        contact=Contact(name="SynLabs", email="dev@synlabs.ru"),
        license=License(name="Apache-2.0"),
        path="/docs",
    )

    # API Router (v1)
    # Продажа подписок (цены, чек-аут, webhook платёжного провайдера) —
    # отдельный модуль и НЕОБЯЗАТЕЛЬНЫЙ. В открытой поставке его нет: он
    # нужен только тому, кто продаёт подписки от своего имени, а
    # прайс-сетка в открытый репозиторий не идёт. Приложение обязано
    # стартовать без него — лимиты и расход живут в billing.py.
    _optional_routers = []
    try:
        from backend.api.routes import billing_payments
        _optional_routers.append(billing_payments.router)
    except ImportError:
        logger.info("billing_payments не установлен — продажа подписок "
                    "отключена; лимиты и расход работают")

    api_router = Router(
        path="/api/v1",
        route_handlers=[
            health.router,
            analyze.router,
            entities.router,
            snapshots.router,
            table_understanding.router,
            desync.router,
            sync.router,  # синхронизация орга: 4 уровня + S(t) (CogniLayer Ф2)
            blind_spots.router,
            human_thinking.router,
            insights.router,
            observer.router,
            a2a.router,
            readiness.router,
            chat.router,
            guide.router,
            coffee.router,
            compose.router,
            # аудит: роутер корректировок памяти существовал, но не был
            # подключён — админка правки памяти (CorrectionsPanel) получала 404
            corrections.router,
            reactive.router,
            meetflow.router,
            inbound.router,
            cognitive_core.router,
            sessions.router,
            agents.router,
            analysis.router,
            mark_research.router,
            client_sim.router,
            communities.router,
            chat_sources.router,
            agent_skills.router,
            meeting_docs.router,
            memory.router,
            temporal.router,
            entity_resolution.router,
            documents.router,
            boards.router,
            cross_meeting.router,
            reports.router,
            # методологии-отчёты (перенос из meetflow report_automation) —
            # секция «Отчёты» на странице инсайтов
            methodology_reports.router,
            # цели/эпики и недельный обзор движения (рабочий процесс)
            goals.router,
            ontology_datasets.ontology_datasets_router,
            permissions_api.permissions_router,
            integrations_daily_pulse.integrations_router,
            integrations_callinsight.callinsight_router,
            # анализ задач (done/open) + делегация доработки кодинг-агентам
            task_analysis.router,
            automations.router,
            templates.router,
            auth.router,
            demo.router,
            knowledge_sync.router,
            documents_gen.GeneratedDocumentsController,
            documents_fill.documents_fill_router,
            mcp_http.mcp_http_router,
            # выпуск/отзыв токенов подключения прямо из интерфейса
            mcp_tokens.mcp_tokens_router,
            # виртуальная планёрка директоров + слепки сотрудников
            boardroom.router,
            local_capture.router,
            person_twin.router,
            # психоаналитика встреч (мотивы/динамика/прогнозы) — чтение
            # под контролем доступа; в общий поиск слой не индексируется
            psych_insights.router,
            # федерация: связи между организациями (рукопожатие, срез,
            # односторонний разрыв) — задел под межкорпоративный обмен
            federation.router,
            # слой внешних агентов: реестр + задачи с машинной приёмкой
            agent_layer.router,
            # согласия на предоставление своего профиля (личный кабинет)
            consent.router,
            # пульс исполнения (реестр задач + сигналы руководителю)
            pulse.router,
            # экспорт анализа компании для внешних карт (призменная и др.)
            graph_export.router,
            graph_export.competency_map_router,
            usage.UsageController,
            integrations.IntegrationsController,
            external_access.external_access_router,
            gdpr.router,
            knowledge_editor.knowledge_editor_router,
            research.router,
            sima.sima_router,
            data_bus.data_bus_router,
            agent_automations.router,
            skills.router,
            bookmarks.router,
            tz_templates.router,
            executor.router,
            validator.router,
            admin_audit.router,
            admin_retention.router,
            lineage_routes.router,
            llm_profiles.router,
            llm_profiles.alias_router,   # issue #107 T-3: /api/v1/llm-profiles
            billing_admin.billing_admin_router,  # лимиты тенанта (админ)
            billing.router,  # свой лимит и расход месяца
            messengers.router,
            onboarding.router,
            orgs.router,
            holdings.router,  # консоль холдинга (Слой 1): обзор корпораций
            persona.router,
            identity.router,  # сшивка идентичности: аккаунт ↔ Person-сущность (CogniLayer Ф0)
            broadcast.router,  # трансляция стратегии: CEO → персональные версии
            sharing.router,
            wisdom.router,
            bridge.router,
            aggregation.router,
            *_optional_routers,
        ],
        tags=["API v1"],
    )


    # Создание приложения
    # request_max_body_size: 100MB для загрузки больших документов
    from litestar import Request, Response
    from litestar.middleware.base import DefineMiddleware

    from backend.api.middleware.idempotency import IdempotencyMiddleware
    from backend.api.middleware.locale_resolver import LocaleResolverMiddleware
    from backend.api.middleware.rate_limit import RateLimitMiddleware
    from backend.api.middleware.tenant_resolver import TenantResolverMiddleware
    from backend.api.middleware.user_id_enforcer import UserIdEnforcerMiddleware
    from backend.config import settings as _s
    from backend.core.llm.quota import QuotaExceeded

    # Tenant resolver идёт ПЕРВЫМ — все нижестоящие middleware и хендлеры
    # читают tenant_id из contextvar.
    tenant_mw = DefineMiddleware(TenantResolverMiddleware, strict=_s.multitenant_strict)
    # Анти-стейл user_id: при ВАЛИДНОМ токене ?user_id= принудительно
    # подменяется на uid токена ДО роутинга — чужой/протухший id из
    # localStorage становится безвреден на всех per-user маршрутах.
    uid_enforcer_mw = DefineMiddleware(UserIdEnforcerMiddleware)
    # W43: locale resolver — рядом с tenant, set'ит i18n contextvar для всех
    # нижестоящих handler'ов (t() в audit messages / API responses).
    locale_mw = DefineMiddleware(LocaleResolverMiddleware)
    # Rate-limit ДО idempotency: 429 на retry-storm нужен раньше кэш-чтения.
    rate_limit_mw = DefineMiddleware(
        RateLimitMiddleware,
        enabled=_s.rate_limit_enabled,
        user_per_minute=_s.rate_limit_user_per_minute,
        anon_per_minute=_s.rate_limit_anon_per_minute,
        tenant_per_minute=_s.rate_limit_tenant_per_minute,
    )
    # Idempotency ПЕРЕД handler'ом: на retry с тем же Idempotency-Key
    # возвращаем кешированный response без вызова бизнес-логики.
    idempotency_mw = DefineMiddleware(IdempotencyMiddleware)
    # W15: HTTP-метрики; самый внешний слой, чтобы duration включал
    # time spent в auth/rate-limit middleware.
    from backend.api.middleware.metrics_mw import MetricsMiddleware
    metrics_mw = DefineMiddleware(MetricsMiddleware)

    # Диагностический логгер исключений — самый внешний middleware, ловит
    # `__cause__` оборачивающих исключений (e.g. LitestarException). В
    # production отключается через APP_ENV=production (см. модуль).
    from backend.api.middleware.exception_logger import ExceptionLoggerMiddleware
    exc_log_mw = DefineMiddleware(ExceptionLoggerMiddleware)

    def _quota_exceeded_handler(_request: "Request", exc: QuotaExceeded) -> "Response":
        return Response(
            content={
                "error": {
                    "code": "QUOTA_EXHAUSTED",
                    "message": str(exc),
                    "scope": exc.scope,
                    "limit": exc.limit,
                    "used": exc.used,
                    "reset_at": exc.reset_at,
                },
            },
            status_code=429,
            headers={"Retry-After": "3600"},
        )

    def _permission_handler(_request: "Request", exc: PermissionError) -> "Response":
        # Единая точка конвертации PermissionError → 401. После issue #107
        # (strict-auth) и убранного DEFAULT_USER_ID-fallback в templates.py /
        # sima.py / documents_gen.py резолверы user_id бросают
        # PermissionError вместо тихого подсовывания дефолтного юзера.
        # Без этого handler'а такие случаи становились бы 500.
        return Response(
            content={"error": {"code": "UNAUTHENTICATED", "message": str(exc)}},
            status_code=401,
        )

    # SCIM-2.0 root-level: IdP'ам удобнее когда /scim/v2/Users в корне
    # домена, а не под /api/v1. См. SSO.md и backend/api/routes/scim.py.
    # Signals WebSocket: real-time pulse для wow-screen на фронте
    # (HomeTab подписывается на /ws/signals и реагирует на usage_tracked,
    # insight_added, anomaly_detected events). Отдельный root-level path
    # (не под /api/v1) — стандартное место для WS, фронт-конвенция.
    from backend.api.websocket.signals_ws import SignalsWebSocketHandler

    # Neo4j отдаёт temporal-значения как neo4j.time.DateTime/Date/Time — msgspec
    # их не умеет, и любой эндпоинт, вернувший такой объект «как есть», падал с
    # SerializationException: Unsupported type: neo4j.time.DateTime. Регистрируем
    # глобальные энкодеры → ISO-строка. Guard: если neo4j.time поменяется — не
    # роняем старт приложения.
    type_encoders: dict = {}
    try:
        from neo4j.time import Date as _Neo4jDate
        from neo4j.time import DateTime as _Neo4jDateTime
        from neo4j.time import Duration as _Neo4jDuration
        from neo4j.time import Time as _Neo4jTime

        type_encoders = {
            _Neo4jDateTime: lambda v: v.iso_format(),
            _Neo4jDate: lambda v: v.iso_format(),
            _Neo4jTime: lambda v: v.iso_format(),
            _Neo4jDuration: lambda v: v.iso_format(),
        }
    except Exception:  # noqa: BLE001 — best-effort, не критично для старта
        logger.warning("Не удалось зарегистрировать neo4j.time type_encoders", exc_info=True)

    # Корень и favicon: у API не было роутов «/» и «/favicon.ico», а именно их
    # запрашивает браузер при заходе на бэкенд напрямую. Каждый такой заход
    # валил в лог «Uncaught exception … 404» с полным трейсбеком (debug=True
    # логирует ВСЕ исключения, включая штатные 404). Роуты дают человеку
    # ориентир вместо ошибки, а disable_stack_trace глушит трейсбеки на
    # ОСТАЛЬНЫЕ 404 (опечатка в пути ≠ авария) — сам ответ 404 не меняется.
    from litestar import get as _get
    from litestar.logging import LoggingConfig
    from litestar.status_codes import HTTP_204_NO_CONTENT

    @_get("/", include_in_schema=False)
    async def _root() -> dict:
        return {"service": "Tessbrain API", "status": "ok",
                "docs": "/schema", "api": "/api/v1",
                "health": "/api/v1/health"}

    @_get("/favicon.ico", include_in_schema=False,
          status_code=HTTP_204_NO_CONTENT)
    async def _favicon() -> None:
        return None

    app = Litestar(
        route_handlers=[api_router, scim.router, SignalsWebSocketHandler,
                        _root, _favicon],
        lifespan=[lifespan],
        cors_config=cors_config,
        openapi_config=openapi_config,
        middleware=[exc_log_mw, metrics_mw, locale_mw, tenant_mw, uid_enforcer_mw, rate_limit_mw, idempotency_mw],
        exception_handlers={
            QuotaExceeded: _quota_exceeded_handler,
            PermissionError: _permission_handler,
        },
        type_encoders=type_encoders,
        debug=True,
        logging_config=LoggingConfig(disable_stack_trace={404}),
        request_max_body_size=100 * 1024 * 1024,  # 100MB
    )

    return app


# Export app instance for ASGI servers (granian, uvicorn)
app = create_app()

