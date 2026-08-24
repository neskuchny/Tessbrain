"""
TESSENT BRAIN - Configuration Module
Централизованная конфигурация всех компонентов системы
"""
import re
from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Определение «внутреннего адреса» живёт в одном месте на весь продукт:
# его же применяют роутер моделей, загрузчик внешних ссылок и веб-поиск.
# Раньше правило было переписано в каждом месте своё — и закрытый контур
# держался только там, где о нём вспомнили.
from backend.core.security.perimeter import is_internal_url as _is_internal_url

_PUBLIC_LLM_HOST_PATTERNS = (
    re.compile(r"\.openai\.com$"),
    re.compile(r"\.googleapis\.com$"),
    re.compile(r"\.anthropic\.com$"),
    re.compile(r"\.cohere\.ai$"),
    re.compile(r"\.mistral\.ai$"),
)

# Известные dev-дефолты, недопустимые в production.
_INSECURE_DEFAULTS: frozenset[str] = frozenset({
    "change-me-in-production",
    "your-jwt-secret-key",
    "your-secret-key",
    "tessent-brain-secret",
    "tessent_secret",
    "neo4j_secret",
    "password",
    "supersecret",
})


class Settings(BaseSettings):
    """Основные настройки приложения"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )

    # === Application ===
    app_name: str = "tessent-brain"
    app_env: Literal["development", "staging", "production"] = "development"
    debug: bool = True
    secret_key: str = "change-me-in-production"

    # === API Server ===
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_workers: int = 4

    # === PostgreSQL ===
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "tessent_brain"
    postgres_user: str = "tessent"
    postgres_password: str = "tessent_secret"
    database_url: str | None = None

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"

    @property
    def db_url_sync(self) -> str:
        """Синхронный URL (psycopg2) — для sync-сторов доступов (D1a),
        которые зовутся из sync-путей (фильтрация графа) и не могут стать
        async. Выводится из db_url заменой async-драйвера на psycopg2."""
        import re
        url = self.db_url
        # Снять любой async/sync-драйвер (+asyncpg / +psycopg / +psycopg2) и
        # поставить psycopg2 ровно один раз (без двойных суффиксов).
        url = re.sub(r"^postgres(ql)?(\+[a-z0-9]+)?://",
                     "postgresql+psycopg2://", url, count=1)
        return url

    # Бэкенд сторов доступов (членства/холдинги/орг-метадата): "file"
    # (по умолчанию — прежнее поведение, single-node) или "postgres"
    # (multi-node, транзакции, без переписывания всего файла). D1a.
    org_store_backend: Literal["file", "postgres"] = "file"

    # Бэкенд чат-сессий и реестра папок (D1c): "file" (per-user JSON,
    # прежнее поведение) или "postgres" (row-per-session, multi-node).
    chat_store_backend: Literal["file", "postgres"] = "file"

    # Бэкенд реестра метрик/time-series (D1e/B3): "sqlite" (per-user .db,
    # прежнее поведение) или "postgres" (общие таблицы, multi-node, org-доступ).
    metric_store_backend: Literal["sqlite", "postgres"] = "sqlite"

    # === Redis ===
    redis_url: str = "redis://localhost:6379/0"
    redis_host: str = "localhost"
    redis_port: int = 6379

    # === Qdrant ===
    qdrant_host: str = "localhost"
    qdrant_port: int = 6333
    qdrant_collection: str = "tessent_brain"
    # API-ключ Qdrant. Прод-инстанс поднят с QDRANT__SERVICE__API_KEY, поэтому
    # без него все запросы отбиваются (401/403) → readyz qdrant=false. Пустая
    # строка == без авторизации (локальный dev-Qdrant без ключа).
    qdrant_api_key: str = ""
    # ВАЖНО: qdrant-client при заданном api_key по умолчанию включает https=True
    # (расчёт на Qdrant Cloud) → к self-hosted HTTP-инстансу это даёт
    # 'SSL: WRONG_VERSION_NUMBER'. Self-hosted Qdrant в docker-сети — plain HTTP,
    # поэтому держим False. Для Qdrant Cloud выставьте QDRANT_HTTPS=true.
    qdrant_https: bool = False

    # === Neo4j ===
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "neo4j_secret"

    # === LLM Providers ===
    google_api_key: str = ""
    gemini_model: str = "gemini-flash-lite-latest"
    openai_api_key: str = ""
    openai_model: str = "gpt-4-turbo-preview"
    litellm_master_key: str = ""

    # === Local LLM (W8 enterprise/on-prem) ===
    # OpenAI-compatible endpoint: vLLM, Ollama, TGI, LM Studio.
    # Когда задан — провайдер LOCAL включается и (при отсутствии других
    # ключей) становится default. См. ENTERPRISE.md.
    llm_local_base_url: str = ""
    llm_local_model: str = ""
    llm_local_api_key: str = ""

    # === Business Crystal (Understanding Layer, Module A) ===
    # Когда True — PatternEncoder при пересчёте differentiators дополнительно
    # извлекает not_law (границы применимости) и open_holes (честные пробелы)
    # через расширенный V2-промпт. Default False → используется прежний
    # промпт, новые поля остаются пустыми. Полностью backward-compat:
    # старые читатели паттернов не зависят от этих полей.
    # Включено по решению владельца продукта (2026-08-13): затухание
    # уверенности, границы применимости и честные пробелы — часть обещания
    # раздела «Опыт компании», выключенными они делали обещание пустым.
    crystal_enrichment_enabled: bool = True

    # Module C veto-gate: когда True — срабатывание not_law (грубый матч границ
    # применимости против ситуации запроса) становится hard-veto (кристалл
    # исключается). Default False: not_law остаётся прозрачным сигналом в
    # distinguishing_axes, без отсечения (точность эвристики ограничена).
    # Требует crystal_enrichment_enabled=True, иначе gate не вызывается.
    crystal_notlaw_hard_veto: bool = False

    # Когда True — Tasker (создание ТЗ) перед генерацией подмешивает «опыт
    # прошлых решений» из BWE: рекомендацию, границы применимости (not_law) и
    # предупреждение об observer-drift. Отдельный флаг (не crystal_enrichment),
    # т.к. ТЗ — другая продуктовая поверхность; включение кристаллов не должно
    # само по себе менять вывод ТЗ.
    # Включено по решению владельца продукта (2026-08-13). Пустая база больше
    # не подмешивает рекламную строку (build_wisdom_brief возвращает [] без
    # паттернов), так что включение безопасно и на чистых аккаунтах.
    wisdom_in_tasker_enabled: bool = True

    # Когда True — чат-поиск (EnhancedSearchOrchestrator) для содержательных
    # запросов подмешивает «опыт прошлых решений» из BWE в контекст синтеза
    # (рекомендация + not_law + observer-drift). Отдельный флаг: чат — массовая
    # поверхность, +1 wisdom-запрос на сообщение стоит денег. Включено по
    # решению владельца продукта (2026-08-13); гейт в оркестраторе оставляет
    # wisdom только на решенческих запросах, так что цена — не «каждое
    # сообщение», а «каждый вопрос про выбор/решение».
    wisdom_in_search_enabled: bool = True

    # Столп C: понимание сырых таблиц с заземлением на модель компании
    # (TableUnderstandingAgent). Когда True — доступен эндпоинт /table/understand.
    # Default False. Скелет (типы/привязка/честное «незнакомое») детерминирован;
    # LLM-интерпретация — под скелетом, graceful.
    table_understanding_enabled: bool = False

    # Столп B: детектор рассинхрона между отделами (CrossDepartmentDesyncDetector)
    # и его обобщение на 4 уровня + индекс S(t) (/sync/report, CogniLayer Ф2).
    # Включён по плану Ф2 «синхронизация снизу»: скелет детерминирован (LLM не
    # зовётся), hard-сигнал только когда метрика = известный KPI графа, иначе
    # soft; уровни без данных честно пусты и в индекс не входят.
    cross_dept_desync_enabled: bool = True

    # Столп A: сканер слепых зон (BlindSpotScanner). Когда True — доступен
    # эндпоинт /blind-spots/scan. Скелет заземляет слепые зоны в
    # факты (повтор ≥3 встреч / observer-drift / противоречия с evidence).
    # Включено по решению владельца продукта (2026-08-13): сканер и так
    # работал внутри проактивного цикла, закрытым оставался только эндпоинт.
    blind_spot_scan_enabled: bool = True

    # Проактивный цикл: ночью система САМА сканирует слепые зоны/рассинхроны
    # (BlindSpotScanner/desync из графа) и доставляет как night-insights.
    # Включено по решению владельца продукта (2026-08-13): «нервная система»
    # должна сигналить сама, а не по открытию дашборда.
    proactive_scan_enabled: bool = True

    # REUSE 17 принципов / 8 патологий: обогащать слепые зоны/рассинхроны полем
    # `principle` (паттерн → системная патология → нарушенный принцип = ПОЧЕМУ).
    # Детерминированный lookup, claim-guard (только уверенные соответствия). Default
    # False: при OFF инсайты байт-в-байт прежние, без поля principle.
    principle_mapping_enabled: bool = False

    # REUSE readiness-gates / Q0: гейтить ТЗ-рекомендации скелетом — Q0 (активировать
    # уже купленное) поднять вверх, «лучше руками?» (редкое/суждение/нет владельца/
    # меняется) пометить. Детерминированный claim-guard над текстом рекомендаций.
    # Default False: при OFF секция рекомендаций ТЗ байт-в-байт прежняя.
    readiness_gates_enabled: bool = False

    # ADD-AS-SKILL «думать как человек»: дисциплина рассуждения (honest-thinking /
    # human-thinking-mode / wisdom-mode) навешивается на system-prompt тех
    # LLM-вызовов, что работают ПОД скелетом (интерпретация таблицы, генерация ТЗ).
    # Меняет КАЧЕСТВО текста LLM, не детерминированный выход → ценность измеряет
    # Gate-0, а не юнит-тест.
    #
    # ВАЖНО — два РАЗНЫХ режима (разделены после Gate-0):
    #   1) АВТО-применение во ВСЕХ пайплайнах (table/ТЗ). Gate-0 показал, что
    #      глобально не бьёт чистый LLM → default False. Это `human_thinking_enabled`.
    #   2) ЯВНЫЙ выбор пользователем (он сам нажал скилл) — должен РАБОТАТЬ, это
    #      его осознанный opt-in. Это `human_thinking_skill_enabled`, default True.
    human_thinking_enabled: bool = False          # авто-инъекция во все пайплайны
    human_thinking_skill_enabled: bool = True     # доступен по явному выбору (эндпоинты /think/*)

    # ADD-AS-SKILL readiness/Q0 как ЯВНО выбираемый скилл (эндпоинт /readiness/*).
    # Авто-обвязка над текстом рекомендаций (readiness_gates_enabled) Gate-0 не
    # прошла. Но по явному выбору на СТРУКТУРНЫХ атрибутах процесса гейт работает по
    # сути → доступен по умолчанию. Default True.
    readiness_skill_enabled: bool = True

    # W14: DLP-фильтр для ответов LLM (текст и строки в JSON). Когда True —
    # маскируются персональные данные и секреты. Опционально per-call через
    # kwarg apply_dlp=True.
    #
    # Default False сознательно, а не по недоделке: маска телефона/паспорта
    # построена на регулярных выражениях и вместе с контактами съедает
    # обычные числа — включённая всюду, она портила бы цифры в отчётах.
    # Включать стоит там, где утечка персональных данных дороже испорченного
    # ответа. В закрытом контуре (enterprise_mode) независимо от этого флага
    # работает узкий набор: секреты и внешние ссылки — их маскирование
    # ничего законного не задевает (см. router._dlp_config).
    enable_output_dlp: bool = False

    # Резать ли внешние ссылки в ответах вне закрытого контура. Отдельный
    # флаг, потому что снаружи ссылка на источник часто и есть ценность
    # ответа. В enterprise_mode ссылки режутся всегда — это защита от
    # эксфильтрации через текст, подсунутый в документ.
    dlp_redact_external_urls: bool = False

    # === Executor (W19): какой backend выполняет ТЗ ===
    # noop / claude_code_cli / cursor_cli / openhands.
    # В enterprise mode разрешены только noop и openhands (с internal URL).
    executor_backend: str = "noop"
    # OpenHands runtime endpoint (W19/W20). Только для backend=openhands.
    openhands_base_url: str = ""
    openhands_api_key: str = ""
    openhands_llm_model: str = ""

    # === Reactive engine (Phase C) ===
    # Когда True — перед доставкой Coffee-артефактов и других сигналов
    # проверяется cognitive_load юзера; сигналы с низким приоритетом
    # паркуются в reactive_signals и drain-ятся позже. По умолчанию
    # выключено для backward-compat: legacy флоу всегда отдаёт сразу.
    # Включено по решению владельца продукта (2026-08-13): без него гейт
    # «умного молчания» всегда пропускал всё подряд — антиспам существовал
    # только на бумаге.
    reactive_engine_enabled: bool = True

    # === Reactive engine rollout (Phase O — production canary) ===
    # reactive_engine_enabled выше — master kill-switch. Поля ниже дают
    # канареечный раскат поверх него (детерминированный per-user bucketing).
    # rollout_pct ∈ [0;100]: доля юзеров в раскате. Default 100 → при
    # включённом master все юзеры включены (= прежнее поведение, backward-compat).
    # Для канарейки: master=True + rollout_pct=5, далее 25, далее 100.
    reactive_engine_rollout_pct: int = 100
    # Явный allowlist для догфудинга (comma-separated user_id). Эти юзеры
    # включены независимо от процента (при master=True).
    reactive_engine_canary_users: str = ""

    # Air-gap / on-prem-only режим. Когда True:
    # - запрещено иметь openai_api_key / google_api_key,
    # - llm_local_base_url обязан быть задан и указывать на internal-сеть,
    # - LLM fallback порядок ограничен LOCAL,
    # - валидатор фейлит запуск в production при нарушении.
    enterprise_mode: bool = False

    # === Business Wisdom Engine (BWE) ===
    # gpt-4o-mini: в 30x дешевле gpt-4 при сопоставимом качестве для структурированного extraction
    bwe_chat_model: str = "gpt-4o-mini"
    # Порог кластеризации паттернов (0.72 = хорошо работает для бизнес-ситуаций)
    bwe_cluster_threshold: float = 0.72
    # Порог поиска в Wisdom Index
    bwe_search_threshold: float = 0.50

    # === Embeddings ===
    # OpenAI embeddings (для Mem0 и других OpenAI-based компонентов)
    embedding_model: str = "text-embedding-3-small"
    embedding_dimension: int = 1536

    # Local embeddings (sentence-transformers для VectorIndexer)
    # intfloat/multilingual-e5-base — лучше для русского языка (768 dim)
    # sentence-transformers/all-MiniLM-L6-v2 — быстрее, но хуже для русского (384 dim)
    local_embedding_model: str = "intfloat/multilingual-e5-base"
    local_embedding_dimension: int = 768

    @property
    def vector_embedding_model(self) -> str:
        """
        Основная embedding-модель для knowledge retrieval слоя.

        Важно отличать её от `embedding_model`, который используется OpenAI-based
        компонентами вроде Business Wisdom Engine.
        """
        return self.local_embedding_model

    @property
    def vector_embedding_dimension(self) -> int:
        """Размерность retrieval-векторов для основного Qdrant/VectorIndexer слоя."""
        return self.local_embedding_dimension

    # === JWT Auth ===
    jwt_secret_key: str = "your-jwt-secret-key"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 10080  # 7 days

    # === Logging ===
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"

    # === External Integrations ===
    supabase_url: str = ""
    supabase_key: str = ""
    meetflow_api_url: str = "http://localhost:8001"

    # === Night Processing ===
    night_processing_enabled: bool = True
    night_processing_start_hour: int = 2
    night_processing_end_hour: int = 6

    # === CORS (W6 prod-hardening) ===
    # Список allowed origins — comma-separated в env, list в коде.
    # ВАЖНО: с allow_credentials=True нельзя использовать "*" — это CSRF
    # вектор. Для прод обязательно whitelist конкретных доменов.
    # Дев-дефолт: localhost для frontend и sima.
    # NoDecode: pydantic-settings по умолчанию пытается JSON-распарсить env для
    # полей сложного типа (list/dict) В ИСТОЧНИКЕ, ДО field_validator'ов. Из-за
    # этого comma-separated строка "https://a.com,https://b.com" падала с
    # JSONDecodeError ещё до нашего сплиттера. NoDecode отключает это поведение
    # для данного поля → значение приходит сырой строкой и режется валидатором.
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "http://localhost:3000",
            "http://localhost:3001",
            "http://localhost:8000",
        ],
    )

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, v):
        # ENV приходит как строка "https://a.com,https://b.com" — режем.
        # Поддерживаем и JSON-список (на случай старого формата в env).
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("["):
                import json
                try:
                    return json.loads(s)
                except json.JSONDecodeError:
                    pass
            return [item.strip() for item in s.split(",") if item.strip()]
        return v

    # === Service-to-service JWT (W6 prod-hardening) ===
    # Shared secret для service-token'ов (meetflow → tessent_brain).
    # Если задан — middleware принимает audience='tessent-brain' токены,
    # подписанные этим ключом. Если пустой — service-auth выключен,
    # межсервисные вызовы должны использовать обычный user-JWT.
    service_jwt_secret: str = ""
    service_jwt_audience: str = "tessent-brain"
    service_jwt_issuer: str = "meetflow"

    # === Rate Limiting ===
    # Sliding-window лимит на минуту, см. backend.api.middleware.rate_limit.
    # Можно отключить (для нагрузочного теста), но в production лучше оставить.
    rate_limit_enabled: bool = True
    # Дефолты были нереалистично низкими: 60/мин = 1 req/сек, а одна страница
    # дашборда грузит несколько панелей разом (граф + снепшоты + объекты) и
    # мгновенно их выбивала (флуд rate_limit_block). Подняты до значений,
    # переживающих UI-бёрсты, но всё ещё ограничивающих abuse. Полностью
    # выключить для solo self-host: RATE_LIMIT_ENABLED=false.
    rate_limit_user_per_minute: int = 600
    rate_limit_anon_per_minute: int = 120
    rate_limit_tenant_per_minute: int = 5000

    # === LLM Quotas ===
    # Per-user / per-tenant жёсткие лимиты на расход токенов в сутки.
    # 0 == без лимита. Превышение возвращает 429 с типом 'quota_exhausted'.
    llm_quota_enabled: bool = True
    # 200K было нереалистично мало: один deep-запрос Enhanced Search тратит
    # ~115K токенов (company snapshot + entity snapshots + история), т.е. <2
    # запроса в сутки, после чего поиск молча деградирует в фоллбэк и отдаёт
    # нерелевантные узлы графа. Поднято до 2M (~17 deep-запросов/день).
    # Реальный потолок расхода всё равно держит per-tenant лимит ниже.
    # Для solo self-host можно вовсе отключить: LLM_USER_DAILY_TOKEN_LIMIT=0.
    llm_user_daily_token_limit: int = 2_000_000
    llm_tenant_daily_token_limit: int = 5_000_000

    # === LLM Budget caps (issue #110 пакет 3) ===
    # $-лимиты — дополняют токен-лимиты (разные модели стоят разное, токенов
    # одинаково — а в долларах большая разница). 0.0 == без лимита.
    # Превышение → QuotaExceeded → 429 quota_exhausted (тот же handler).
    # near_limit_ratio (0.0..1.0) — порог warning'а в логах перед блокировкой
    # (по умолчанию 0.80 = «80% бюджета съедено»).
    llm_user_daily_cost_usd: float = 0.0
    llm_tenant_daily_cost_usd: float = 50.0  # $50/день/тенант — мягкий деф.
    # Cap на одну фоновую задачу (taskiq job_id) — защита от «массовый
    # ingest CRM сожрал лимит». 0.0 == без лимита.
    llm_per_job_cost_usd: float = 5.0  # $5/job по умолчанию
    llm_quota_near_limit_ratio: float = 0.80

    # === Атомарная резервация квоты (защита от гонки) ===
    # Проверка квоты читает ЗАПИСАННЫЙ расход, а запись идёт после ответа
    # модели. Между ними окно: залп параллельных вызовов проходит проверку
    # раньше, чем хоть один записался, и пробивает лимит на величину залпа.
    # Резервация закрывает окно, атомарно бронируя оценку вызова в Redis.
    # Управляется env (backend.core.llm.quota_reservation), не только этими
    # полями — здесь для документирования дефолтов:
    #   LLM_QUOTA_ATOMIC=off        — выключатель (по умолчанию включено;
    #                                  без Redis всё равно fail-open, как было)
    #   LLM_QUOTA_RESERVE_TOKENS    — оценка токенов на вызов (деф. 4000)
    #   LLM_QUOTA_RESERVE_USD       — оценка стоимости на вызов (деф. $0.02)
    #   LLM_QUOTA_INFLIGHT_TTL_SEC  — сколько живёт резерв до самоочистки (120)
    # Оценка мала относительно суточного лимита — резерв кусается только у
    # самой границы бюджета, где залп и опасен.
    llm_quota_atomic_reserve_tokens: int = 4000
    llm_quota_inflight_ttl_sec: int = 120

    # === Биллинговые окна (SaaS: «дневные лимиты и недельные») ===
    # Недельные токен-капы (окно с понедельника UTC). 0 == выключено —
    # включаются тарифом или env. Дневные капы выше остаются первой линией.
    llm_user_weekly_token_limit: int = 0
    llm_tenant_weekly_token_limit: int = 0
    # Энфорсить месячный $-кап тарифа (tenant_limits.monthly_tokens_usd) —
    # включённый пул «минималка + токены сверху». False → advisory как раньше.
    llm_enforce_plan_monthly_cap: bool = True

    # === Outbound email (W33) ===
    # Приоритет transport'ов: SendGrid → SMTP → Noop.
    # Без этих переменных POST /shares с notify_partner=true просто логирует
    # сообщение и возвращает email_sent=false.
    sendgrid_api_key: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    email_from_addr: str = ""           # обязательно при включённом transport
    email_from_name: str = "Tessbrain"

    # === External sharing (W31) ===
    # Отдельный HMAC секрет для viewer JWT — изоляция от основного jwt_secret_key.
    # Пустой → fallback на jwt_secret_key (только dev). В production set'нуть.
    share_jwt_secret: str = ""
    # Default TTL для bundle'а если frontend не указал.
    share_default_ttl_days: int = 30
    share_max_ttl_days: int = 365
    # Базовый URL партнёрского портала — `<base>/shared/<token>` рассылается в email'ах.
    share_public_base_url: str = "http://localhost:3000"

    # === Messenger inbound (W30) ===
    # Telegram: secret_token хедера от `setWebhook` сравнивается constant-time.
    # Пусто → верификация выключена (dev). В production обязательно.
    telegram_bot_token: str = ""
    telegram_bot_username: str = ""    # для построения deep-link t.me/<bot>?start=<token>
    telegram_webhook_secret: str = ""

    # Slack: signing secret из app config'а — для HMAC verification.
    slack_signing_secret: str = ""
    # Токен бота (xoxb-…) — им уходит ОТВЕТ через chat.postMessage. Без него
    # входящие обрабатываются как раньше, но человек ответа не увидит:
    # интеграция работает наполовину, и это видно в метрике reply_failed.
    slack_bot_token: str = ""

    # Public base URL — используется при формировании deep-link'а в UI.
    public_base_url: str = "http://localhost:3000"

    # === Retention (W27) ===
    # Сколько дней хранить старые validation_results / прочие append-only
    # таблицы. Cleanup запускается nightly_consolidation'ом и через
    # admin-route /api/v1/admin/retention/run.
    # 0 == ретеншн отключён (никогда не удаляем).
    # Значения по умолчанию выбраны под GDPR / SOC2 ориентиры:
    #   - validation_results: 90 дней (диагностические данные)
    #   - executor_handles: 30 дней (Redis имеет свой TTL=7д, тут защитный
    #     пол на случай если кто-то выставит ex=None — мы дропнем по созданию)
    #   - audit_events НЕ управляется здесь: append-only по compliance
    #     (BEFORE DELETE триггер блокирует удаление on purpose).
    retention_enabled: bool = True
    validation_retention_days: int = 90
    executor_retention_days: int = 30

    # === i18n (W43) ===
    # Default locale при отсутствии Accept-Language / ?lang= / JWT claim.
    # Supported: "ru", "en". Расширяется через core/i18n/locales/<code>.json.
    default_locale: str = "ru"

    # === Multi-Tenant ===
    # Если включено, TenantResolverMiddleware требует tenant_id в JWT/заголовке;
    # запросы без него отклоняются 400. В дев-режиме держим False — single-tenant.
    multitenant_strict: bool = False
    # Если включено, Qdrant/Neo4j storage-слои фейлят без tenant'а в контексте.
    # Включается одновременно с multitenant_strict в production.
    multitenant_storage_strict: bool = False

    @model_validator(mode="after")
    def _forbid_insecure_defaults_in_production(self) -> "Settings":
        """В production все секреты должны быть переопределены через окружение.

        Локально остаются дев-дефолты ради DX, но при app_env=production
        запуск с любым из известных дефолтов фейлится сразу с понятной ошибкой.
        """
        if self.app_env != "production":
            return self
        offenders: list[str] = []
        for field_name in (
            "secret_key",
            "jwt_secret_key",
            "postgres_password",
            "neo4j_password",
        ):
            value = getattr(self, field_name, None)
            if isinstance(value, str) and value in _INSECURE_DEFAULTS:
                offenders.append(field_name.upper())
        if offenders:
            raise ValueError(
                "Insecure default secrets detected in production: "
                + ", ".join(offenders)
                + ". Set them via environment variables."
            )
        return self

    @model_validator(mode="after")
    def _require_verifiable_jwt_secret_in_production(self) -> "Settings":
        """В production обязан быть ключ, которым проверяется ПОДПИСЬ токена.

        ЗАЧЕМ ЭТО ФЕЙЛИТ СТАРТ. Строгий режим аутентификации
        (enable_strict_chat_auth) сам себя выключает, если проверять подпись
        нечем: иначе он отверг бы все токены подряд и уронил весь UI. Решение
        разумное, но у него есть тихая обратная сторона — деплой без секрета
        молча уезжает в compat-режим, где `sub` из НЕПОДПИСАННОГО токена
        принимается за личность. То есть безопасность выключается сама, а
        внешне всё работает: в логах warning, которого никто не читает.

        Поэтому решение переносится сюда, к старту. В production без секрета
        процесс не поднимается — значит ситуация «работаем в проде без
        проверки подписи» становится невозможной, а не маловероятной.
        В dev ничего не меняется: там прежний мягкий режим остаётся.

        Список кандидатов совпадает с `service_token._jwt_secret_configured`.
        Разъедутся — проверка станет враньём, поэтому оба места ссылаются
        друг на друга.
        """
        if self.app_env != "production":
            return self
        import os as _os

        def _is_real_secret(value: str) -> bool:
            """Секрет ли это или строка из шаблона.

            env.example кладёт «your-jwt-secret-key» прямо в JWT_SECRET, и
            копия шаблона в .env выглядела бы как заданный секрет. Известные
            дев-дефолты за секрет не считаем — та же политика, что у
            _forbid_insecure_defaults_in_production выше.
            """
            v = (value or "").strip()
            return bool(v) and v not in _INSECURE_DEFAULTS

        if _is_real_secret(self.jwt_secret_key):
            return self
        for env_name in ("JWT_SECRET", "SUPABASE_JWT_SECRET", "SECRET_KEY",
                         "JWT_SECRET_KEY", "LEGACY_JWT_SECRET"):
            if _is_real_secret(_os.environ.get(env_name, "")):
                return self
        raise ValueError(
            "В production не задан JWT-секрет, которым проверяется подпись "
            "токенов. Без него строгий режим аутентификации отключается сам, "
            "и подпись перестаёт проверяться — незаметно. Задайте любую из: "
            "SUPABASE_JWT_SECRET (если аутентификация через Supabase), "
            "JWT_SECRET или JWT_SECRET_KEY."
        )

    @model_validator(mode="after")
    def _enforce_enterprise_air_gap(self) -> "Settings":
        """W8 enterprise: air-gap kill-switch.

        Когда `enterprise_mode=True`, запрещаем любые managed-LLM:
        - наличие `openai_api_key` или `google_api_key` → ValueError;
        - `llm_local_base_url` обязан быть задан;
        - `llm_local_base_url` обязан указывать на internal-сеть
          (loopback / RFC1918 / *.local / *.svc.cluster.local).

        Failure mode — fail-closed: процесс не стартует. Это критично:
        Settings грузятся при старте, и если правила нарушены, мы падаем
        ДО того как уйдёт первый запрос наружу.
        """
        if not self.enterprise_mode:
            return self

        violations: list[str] = []

        if self.openai_api_key:
            violations.append("OPENAI_API_KEY must be empty in enterprise mode")
        if self.google_api_key:
            violations.append("GOOGLE_API_KEY must be empty in enterprise mode")

        base_url = (self.llm_local_base_url or "").strip()
        if not base_url:
            violations.append(
                "LLM_LOCAL_BASE_URL is required in enterprise mode "
                "(point at vLLM/Ollama/TGI inside your VPC)"
            )
        elif not _is_internal_url(base_url):
            violations.append(
                f"LLM_LOCAL_BASE_URL={base_url!r} is not internal — "
                "enterprise mode requires loopback / RFC1918 / *.local / *.svc.cluster.local"
            )
        else:
            host = (urlparse(base_url).hostname or "").lower()
            if any(p.search(host) for p in _PUBLIC_LLM_HOST_PATTERNS):
                violations.append(
                    f"LLM_LOCAL_BASE_URL={base_url!r} resolves to a known "
                    "managed-LLM host — disallowed in enterprise mode"
                )

        # W19: executor backend в enterprise — только свой сервис по
        # внутреннему адресу (openhands/openworker) или noop.
        backend = (self.executor_backend or "noop").strip().lower()
        if backend in {"claude_code_cli", "cursor_cli", "codex_cli"}:
            violations.append(
                f"executor_backend={backend!r} uses managed cloud API; "
                "use 'openhands', 'openworker' or 'noop' in enterprise mode"
            )
        if backend == "openhands":
            oh_url = (self.openhands_base_url or "").strip()
            if not oh_url:
                violations.append(
                    "OPENHANDS_BASE_URL is required in enterprise mode "
                    "with executor_backend=openhands"
                )
            elif not _is_internal_url(oh_url):
                violations.append(
                    f"OPENHANDS_BASE_URL={oh_url!r} is not internal"
                )
        # Мультимодальный endpoint (визуальная приёмка) — тот же периметр.
        # Через него уходят СКРИНШОТЫ экранов компании: чувствительнее
        # текста. Модуль обещал эту проверку в докстринге, но её не было.
        import os as _os2
        mm_url = (_os2.getenv("MULTIMODAL_BASE_URL") or "").strip()
        if mm_url and not _is_internal_url(mm_url):
            violations.append(
                f"MULTIMODAL_BASE_URL={mm_url!r} is not internal — "
                "screenshots must not leave the perimeter"
            )

        if backend == "openworker":
            import os as _os
            ow_url = (_os.getenv("OPENWORKER_BASE_URL") or "").strip()
            if not ow_url:
                violations.append(
                    "OPENWORKER_BASE_URL is required in enterprise mode "
                    "with executor_backend=openworker"
                )
            elif not _is_internal_url(ow_url):
                violations.append(
                    f"OPENWORKER_BASE_URL={ow_url!r} is not internal"
                )

        if violations:
            raise ValueError(
                "Enterprise mode violations:\n  - "
                + "\n  - ".join(violations)
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Получить настройки (с кешированием)"""
    return Settings()


# Singleton instance
settings = get_settings()



