# -*- coding: utf-8 -*-
"""
LLM Router - интеллектуальная маршрутизация между LLM провайдерами.
Поддерживает fallback, load balancing и выбор модели по задаче.
"""
import contextvars
import logging
import os
import time
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from .base import BaseLLMClient
from .gemini_client import GeminiClient
from .local_client import LocalLLMClient
from .openai_client import OpenAIClient

logger = logging.getLogger(__name__)


# Кеш дефолтных провайдер-клиентов по api_key. LLMRouter() конструируется в
# десятках мест (per-request, per-item в циклах снапшотов/инсайтов), и раньше
# КАЖДЫЙ конструктор поднимал новый OpenAIClient+GeminiClient — сотни httpx-
# пулов и genai.configure за минуты → распухание памяти процесса и, как
# следствие, «os error 1455: файл подкачки слишком мал» при ленивой загрузке
# модели эмбеддингов (эмбеддинги молча отключались, поиск деградировал).
# Клиенты не держат per-request состояния (один роутер и так шарит клиент между
# всеми своими вызовами), поэтому безопасно переиспользовать их между роутерами.
# Ключ — api_key; спец-клиенты с context_for_caching строятся мимо роутера и
# сюда не попадают.
_CLIENT_CACHE: Dict[str, "BaseLLMClient"] = {}


def _cached_client(kind: str, api_key: str, factory) -> "BaseLLMClient":
    """Вернуть закешированный клиент провайдера или создать и закешировать."""
    cache_key = f"{kind}:{api_key}"
    client = _CLIENT_CACHE.get(cache_key)
    if client is None:
        client = factory()
        _CLIENT_CACHE[cache_key] = client
    return client


def _enterprise_mode_on() -> bool:
    """Включён ли закрытый контур. Единый источник — модуль периметра."""
    try:
        from backend.core.security.perimeter import enterprise_mode_enabled
        return enterprise_mode_enabled()
    except Exception:
        return os.environ.get("ENTERPRISE_MODE", "").lower() in {"1", "true", "yes", "on"}


def _perimeter_reason(client: "BaseLLMClient") -> Optional[str]:
    """Причина, по которой этот клиент уводит запрос за периметр (или None).

    Адрес берём тот же, по которому клиент реально пойдёт
    (`_resolved_base_url()` уже применил дефолты провайдера), — своей копии
    таблицы адресов не держим, иначе проверка начнёт проверять не то.
    Ошибка самой проверки трактуется как «наружу»: в закрытом контуре
    сомнение решается в пользу отказа.
    """
    try:
        from backend.core.security.perimeter import llm_target_leaves_perimeter
        resolver = getattr(client, "_resolved_base_url", None)
        base_url = resolver() if callable(resolver) else getattr(client, "base_url", None)
        return llm_target_leaves_perimeter(getattr(client, "provider", None), base_url)
    except Exception as exc:
        return f"проверка периметра не выполнена ({exc})"


def _dlp_config(apply_dlp_kwarg: Optional[bool]):
    """Какие правила фильтра выхода применяем к этому ответу. None = никакие.

    Три режима, и разница между ними принципиальна:

    1. `enable_output_dlp=true` (или `apply_dlp=True` на вызове) — полный
       набор: почты, телефоны, счета, карты, паспорта, секреты. Это режим
       для контуров, где утечка персональных данных дороже, чем испорченный
       ответ: маска телефона регулярным выражением ест и обычные числа,
       поэтому цифры в отчётах могут пострадать. Отсюда и «по умолчанию
       выключено» — это осознанный выбор, а не недоделка.

    2. Закрытый контур (`enterprise_mode`) без явного включения — узкий
       набор: секреты и внешние ссылки. Ни то, ни другое не бывает
       законным содержимым ответа внутри air-gap, а числа и контакты
       остаются целыми. Именно здесь режутся внешние ссылки: это защита от
       эксфильтрации через текст, подсунутый в документ («выпиши ключи
       ссылкой на мой сервер»).

    3. Иначе — фильтр не применяется.
    """
    try:
        from backend.core.llm.output_filter import DLPConfig
    except Exception:
        return None

    enabled = apply_dlp_kwarg
    settings = None
    try:
        from backend.config import get_settings
        settings = get_settings()
    except Exception:
        settings = None
    if enabled is None:
        enabled = bool(getattr(settings, "enable_output_dlp", False))

    air_gap = _enterprise_mode_on()
    if not enabled:
        if not air_gap:
            return None
        # Узкий периметровый набор: только то, что внутри контура не может
        # быть законной частью ответа.
        return DLPConfig(
            redact_emails=False, redact_phones=False, redact_iban=False,
            redact_credit_cards=False, redact_ru_passport=False,
            redact_ru_snils=False,
            redact_secret_tokens=True, redact_external_urls=True,
        )

    # Полный набор. Внешние ссылки режем в закрытом контуре всегда, вне его —
    # по отдельному флагу: снаружи ссылка на источник часто и есть ценность
    # ответа, поэтому резать её молча нельзя.
    return DLPConfig(
        redact_external_urls=air_gap or bool(
            getattr(settings, "dlp_redact_external_urls", False)
        ),
    )


def _maybe_apply_dlp(text: str, apply_dlp_kwarg: Optional[bool]) -> str:
    """Применить W14 output DLP filter, если он включён для этого ответа.

    Filter idempotent — повторное применение не меняет результат.
    Метрика `dlp_redactions_total` инкрементится по каждой редакции.
    """
    if not isinstance(text, str) or not text:
        return text
    cfg = _dlp_config(apply_dlp_kwarg)
    if cfg is None:
        return text
    try:
        from backend.core.llm.output_filter import filter_output
    except Exception:
        return text
    try:
        result = filter_output(text, cfg)
        _count_redactions(result.redactions)
        return result.text
    except Exception as exc:
        logger.warning("DLP filter raised, returning unfiltered text: %s", exc)
        return text


def _count_redactions(redactions) -> None:
    try:
        from backend.core.observability import metrics as _metrics
        for label, _orig in redactions:
            _metrics.dlp_redactions_total.labels(label=label).inc()
    except Exception:
        pass


def _maybe_apply_dlp_json(value: Any, apply_dlp_kwarg: Optional[bool]) -> Any:
    """Тот же фильтр для структурированных ответов — по СТРОКОВЫМ значениям.

    Раньше `generate_json` фильтр не проходил вовсе: секрет, попавший в поле
    JSON, уходил клиенту как есть. Фильтровать сериализованный текст целиком
    нельзя — маска числа превратила бы `"count": 12345678` в невалидный JSON,
    поэтому идём по разобранной структуре и трогаем только строки; ключи не
    трогаем, чтобы не сломать контракт полей.
    """
    cfg = _dlp_config(apply_dlp_kwarg)
    if cfg is None:
        return value
    try:
        from backend.core.llm.output_filter import filter_output
    except Exception:
        return value
    redactions: list = []

    def _walk(node: Any) -> Any:
        if isinstance(node, str):
            if not node:
                return node
            res = filter_output(node, cfg)
            redactions.extend(res.redactions)
            return res.text
        if isinstance(node, list):
            return [_walk(x) for x in node]
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        return node

    try:
        out = _walk(value)
        _count_redactions(redactions)
        return out
    except Exception as exc:
        logger.warning("DLP filter (json) raised, returning unfiltered: %s", exc)
        return value


# Context variables для передачи метаданных в трекинг
_current_user_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar('user_id', default=None)
_current_session_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar('session_id', default=None)
_current_agent_mode: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar('agent_mode', default=None)


# Tenant контекст добавляется отдельно — на этапе multi-tenant (Phase 7).
_current_tenant_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar('tenant_id', default=None)

# Phase 1 multi-provider (per-request override): если установлен — _maybe_get_active_profile_client
# резолвит ИМЕННО этот profile_id, минуя глобальный default. Используется
# для «каждый юзер выбирает свою модель» в chat-request.
_current_llm_profile_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    'llm_profile_id', default=None,
)


def set_llm_context(
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    agent_mode: Optional[str] = None,
    tenant_id: Optional[str] = None,
    llm_profile_id: Optional[str] = None,
):
    """Установить контекст для трекинга LLM использования.

    llm_profile_id (per-request) перекрывает глобальный is_default профиль
    на время текущего request scope. None = использовать default.
    """
    if user_id:
        _current_user_id.set(user_id)
    if session_id:
        _current_session_id.set(session_id)
    if agent_mode:
        _current_agent_mode.set(agent_mode)
    if tenant_id:
        _current_tenant_id.set(tenant_id)
    if llm_profile_id:
        _current_llm_profile_id.set(llm_profile_id)


def get_llm_context() -> Dict[str, Optional[str]]:
    """Получить текущий контекст.

    tenant_id берётся из единого источника `core.observability.tenant_context`,
    с fallback на локальный contextvar для совместимости с прямыми вызовами
    `set_llm_context(tenant_id=...)` в обход middleware.
    """
    tenant_id = _current_tenant_id.get()
    if not tenant_id:
        try:
            from backend.core.observability.tenant_context import get_current_tenant
            tenant_id = get_current_tenant()
        except Exception:
            tenant_id = None
    return {
        "user_id": _current_user_id.get(),
        "session_id": _current_session_id.get(),
        "agent_mode": _current_agent_mode.get(),
        "tenant_id": tenant_id,
        "llm_profile_id": _current_llm_profile_id.get(),
    }


class LLMProvider(str, Enum):
    """Поддерживаемые LLM провайдеры.

    W42: добавлены RUNPOD и TURBOQUANT — оба используют OpenAI-compatible API,
    отличаются только base_url и operational характеристиками (RunPod —
    cloud GPU pod, TurboQuant — наш fork с квантизацией). Per-tenant
    profiles с конкретным base_url хранятся в `llm_profiles` таблице.
    """
    GEMINI = "gemini"
    OPENAI = "openai"
    LOCAL = "local"          # vLLM/Ollama/TGI/LM Studio in-VPC (W8 enterprise)
    RUNPOD = "runpod"        # RunPod serverless / pod proxy (W42)
    TURBOQUANT = "turboquant"  # наш fork с int4/int8 квантизацией (W42)
    AUTO = "auto"  # Автоматический выбор


class TaskType(str, Enum):
    """Типы задач для оптимального выбора модели"""
    EXTRACTION = "extraction"  # Извлечение сущностей
    REASONING = "reasoning"    # Логические рассуждения
    GENERATION = "generation"  # Генерация текста
    SUMMARIZATION = "summarization"  # Суммаризация
    CLASSIFICATION = "classification"  # Классификация
    DEFAULT = "default"


class ModelTier(str, Enum):
    """Уровень модели (стандартная/премиум)"""
    STANDARD = "standard"  # Быстрая, экономичная модель
    PREMIUM = "premium"    # Мощная модель для сложных задач


class LLMRouter:
    """
    Интеллектуальный роутер для LLM запросов.

    Возможности:
    - Автоматический fallback при ошибках
    - Выбор модели по типу задачи
    - Кеширование контекста (для Gemini)
    - Единая статистика по всем провайдерам

    Пример использования:
    ```python
    router = LLMRouter()

    # Простой запрос
    result = await router.generate_json(prompt)

    # С указанием провайдера
    result = await router.generate_json(prompt, provider=LLMProvider.OPENAI)

    # С кешированием контекста (для длинных транскриптов)
    router.set_context_for_caching(transcript)
    result = await router.generate_json(prompt)
    ```
    """

    # Модели по уровню (tier)
    TIER_MODELS = {
        ModelTier.STANDARD: {
            LLMProvider.GEMINI: "gemini-flash-lite-latest",
            LLMProvider.OPENAI: "gpt-4o-mini",
            # Для LOCAL фактическая модель приходит из env (LLM_LOCAL_MODEL),
            # значение здесь — placeholder, который игнорируется LocalLLMClient.
            LLMProvider.LOCAL: "local",
        },
        ModelTier.PREMIUM: {
            LLMProvider.GEMINI: "gemini-flash-latest",  # Новейшая модель Gemini 3 Flash (preview)
            LLMProvider.OPENAI: "gpt-4o",
            LLMProvider.LOCAL: "local",
        }
    }

    # Рекомендуемые модели по типу задачи (используется для внутренних задач)
    TASK_MODELS = {
        TaskType.EXTRACTION: {
            LLMProvider.GEMINI: "gemini-flash-lite-latest",
            LLMProvider.OPENAI: "gpt-4o-mini"
        },
        TaskType.REASONING: {
            LLMProvider.GEMINI: "gemini-flash-lite-latest",
            LLMProvider.OPENAI: "gpt-4o"
        },
        TaskType.GENERATION: {
            LLMProvider.GEMINI: "gemini-flash-lite-latest",
            LLMProvider.OPENAI: "gpt-4o"
        },
        TaskType.SUMMARIZATION: {
            LLMProvider.GEMINI: "gemini-flash-lite-latest",
            LLMProvider.OPENAI: "gpt-4o-mini"
        },
        TaskType.CLASSIFICATION: {
            LLMProvider.GEMINI: "gemini-flash-lite-latest",
            LLMProvider.OPENAI: "gpt-4o-mini"
        },
        TaskType.DEFAULT: {
            LLMProvider.GEMINI: "gemini-flash-lite-latest",
            LLMProvider.OPENAI: "gpt-4o-mini"
        }
    }

    def __init__(
        self,
        default_provider: LLMProvider = LLMProvider.GEMINI,
        gemini_api_key: Optional[str] = None,
        openai_api_key: Optional[str] = None,
        enable_fallback: bool = True
    ):
        """
        Args:
            default_provider: Провайдер по умолчанию
            gemini_api_key: API ключ Gemini
            openai_api_key: API ключ OpenAI
            enable_fallback: Включить автоматический fallback
        """
        self.default_provider = default_provider
        self.enable_fallback = enable_fallback

        # Инициализация клиентов
        self._clients: Dict[LLMProvider, BaseLLMClient] = {}

        # Local (W8: vLLM/Ollama/TGI). В air-gap-режиме это единственный
        # клиент — по-этому проверяем первым и при наличии base_url ставим
        # как default-провайдера.
        local_base = os.environ.get("LLM_LOCAL_BASE_URL", "").strip()
        if local_base:
            client = LocalLLMClient()
            if client.enabled:
                self._clients[LLMProvider.LOCAL] = client
                logger.info("[OK] Local LLM client initialized (%s)", local_base)

        # Enterprise mode: запрещаем подключать managed-провайдеры,
        # даже если соответствующие ключи остались в env по недосмотру.
        # Это последняя линия защиты — основной чек делает Settings-валидатор.
        enterprise_mode = os.environ.get("ENTERPRISE_MODE", "").lower() in {"1", "true", "yes"}

        # OpenAI (check first - more reliable quotas)
        openai_key = openai_api_key or os.environ.get("OPENAI_API_KEY")
        if openai_key and not enterprise_mode:
            self._clients[LLMProvider.OPENAI] = _cached_client(
                "openai", openai_key, lambda: OpenAIClient(api_key=openai_key))

        # Gemini
        gemini_key = gemini_api_key or os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
        if gemini_key and not enterprise_mode:
            self._clients[LLMProvider.GEMINI] = _cached_client(
                "gemini", gemini_key, lambda: GeminiClient(api_key=gemini_key))

        if not self._clients:
            logger.warning(
                "[!] No LLM clients available! Set LLM_LOCAL_BASE_URL "
                "(enterprise/on-prem) or GOOGLE_API_KEY/OPENAI_API_KEY"
            )

        # Auto-detect default provider:
        # 1. enterprise_mode + LOCAL → LOCAL.
        # 2. LOCAL без enterprise (mixed deploy) → тоже LOCAL (зачем платить
        #    managed-провайдеру, если есть свой инференс).
        # 3. иначе OpenAI → Gemini.
        if default_provider is None or default_provider == LLMProvider.AUTO:
            if LLMProvider.LOCAL in self._clients:
                self.default_provider = LLMProvider.LOCAL
            elif LLMProvider.OPENAI in self._clients:
                self.default_provider = LLMProvider.OPENAI
            elif LLMProvider.GEMINI in self._clients:
                self.default_provider = LLMProvider.GEMINI
            else:
                self.default_provider = LLMProvider.AUTO
        else:
            self.default_provider = default_provider

        # Порядок fallback зависит от enterprise-режима: в air-gap нельзя
        # падать на OpenAI/Gemini — значит fallback закрыт.
        if enterprise_mode:
            self._fallback_order = [LLMProvider.LOCAL]
        else:
            self._fallback_order = [
                LLMProvider.LOCAL,
                LLMProvider.OPENAI,
                LLMProvider.GEMINI,
            ]

        # Cooldown: трекинг ошибок провайдеров
        # {provider: {"last_error_time": float, "error_count": int, "cooldown_until": float}}
        self._provider_health: Dict[LLMProvider, Dict] = {}
        self._cooldown_base_seconds = 5  # Базовый cooldown (5 сек, потом 10, 20, 40...)
        self._cooldown_max_seconds = 120  # Макс cooldown (2 мин)
        self._error_count_reset_after = 300  # Сброс счётчика ошибок через 5 мин без ошибок

        logger.info(f"LLM Router: default provider: {self.default_provider.value}")

    def _is_provider_on_cooldown(self, provider: LLMProvider) -> bool:
        """Проверить, находится ли провайдер в cooldown после ошибки"""
        health = self._provider_health.get(provider)
        if not health:
            return False

        now = time.time()

        # Сброс если давно не было ошибок
        if now - health.get("last_error_time", 0) > self._error_count_reset_after:
            self._provider_health.pop(provider, None)
            return False

        return now < health.get("cooldown_until", 0)

    def _record_provider_error(self, provider: LLMProvider):
        """Записать ошибку провайдера и установить cooldown"""
        now = time.time()
        health = self._provider_health.get(provider, {"error_count": 0, "last_error_time": 0, "cooldown_until": 0})

        health["error_count"] = health.get("error_count", 0) + 1
        health["last_error_time"] = now

        # Экспоненциальный cooldown: 5s, 10s, 20s, 40s... до 120s max
        cooldown_secs = min(
            self._cooldown_base_seconds * (2 ** (health["error_count"] - 1)),
            self._cooldown_max_seconds
        )
        health["cooldown_until"] = now + cooldown_secs

        self._provider_health[provider] = health
        logger.warning(
            f"LLM provider {provider.value}: error #{health['error_count']}, "
            f"cooldown {cooldown_secs}s"
        )

    def _record_provider_success(self, provider: LLMProvider):
        """Записать успех провайдера — сбрасывает счётчик ошибок"""
        if provider in self._provider_health:
            # Уменьшаем счётчик при успехе (но не обнуляем сразу)
            health = self._provider_health[provider]
            health["error_count"] = max(0, health.get("error_count", 0) - 1)
            if health["error_count"] == 0:
                self._provider_health.pop(provider, None)

    async def _maybe_get_active_profile_client(
        self, provider: "LLMProvider",
    ) -> Optional[BaseLLMClient]:
        """W42b + Phase 1 multi-provider per-user override.

        Логика выбора профиля:
        1. Если в контексте установлен `llm_profile_id` (per-request override
           через `set_llm_context(llm_profile_id=...)`) — резолвим именно его.
           Это даёт юзеру выбрать модель прямо в chat-request, минуя tenant-wide default.
        1b. BYOK (opt-in): если пользователь включил «мой ключ из Интеграций»
           — эфемерный клиент на его Anthropic/OpenAI ключе (personal_client).
        2. Иначе — global active profile (is_default=TRUE).
        3. Иначе — None, fallback на env-based clients (W42 baseline).

        Совместимость с provider:
        - AUTO → любой профиль подходит
        - Конкретный provider → используем только если совпадает с profile.provider
        """
        try:
            from backend.core.llm.active_profile import get_active_profile_resolver
        except ImportError:
            return None
        resolver = get_active_profile_resolver()

        # Per-request override: profile_id из contextvar (chat-request)
        requested_profile_id = _current_llm_profile_id.get()

        active = None
        try:
            if requested_profile_id:
                active = await resolver.resolve_by_id(requested_profile_id)
                if active is None:
                    # Юзер запросил несуществующий/невалидный профиль —
                    # логируем warn, но НЕ падаем (fallback на default).
                    logger.warning(
                        "llm_profile_id=%s requested but unresolvable, "
                        "falling back to global default",
                        requested_profile_id,
                    )
            # 1b. BYOK: личный ключ пользователя из Интеграций (opt-in).
            # Явный llm_profile_id сильнее; tenant default — слабее.
            if active is None:
                try:
                    from backend.core.llm.personal_client import get_personal_client
                    _uid = _current_user_id.get()
                    if _uid:
                        active = await get_personal_client(_uid)
                except Exception:
                    logger.debug("personal client resolve skipped", exc_info=True)
            if active is None:
                active = await resolver.get_active_client()
        except Exception as exc:
            logger.debug("active resolver failed: %s", exc)
            return None
        if active is None:
            return None

        # Закрытый контур: профиль модели НЕ обходит air-gap.
        #
        # Валидатор Settings проверяет переменные окружения при старте, но
        # профили живут в базе и заводятся в рантайме — через них вызов мог
        # уйти в публичное облако при включённом enterprise_mode. Личный ключ
        # пользователя (BYOK) — та же дыра. Проверяем ВСЕ три пути (явный
        # profile_id, личный ключ, tenant default) в одной точке, после
        # резолва: отказ здесь означает возврат к локальному клиенту, а если
        # его нет — генерация честно падает, а не уходит наружу.
        if _enterprise_mode_on():
            reason = _perimeter_reason(active)
            if reason:
                logger.error(
                    "[air-gap] LLM profile rejected (provider=%s): %s",
                    getattr(active, "provider", "?"), reason,
                )
                return None

        # Map LLMProvider enum к profile.provider string.
        # Phase 1 (multi-provider): новые cloud-провайдеры (deepseek/qwen/
        # openrouter/anthropic/...) пока без отдельного LLMProvider enum-члена,
        # так что они выбираются только через AUTO (active profile=default).
        _enum_to_string = {
            LLMProvider.GEMINI: "gemini",
            LLMProvider.OPENAI: "openai",
            LLMProvider.LOCAL: "local_vllm",
            LLMProvider.RUNPOD: "runpod",
            LLMProvider.TURBOQUANT: "turboquant",
        }
        if provider == LLMProvider.AUTO:
            return active
        wanted = _enum_to_string.get(provider)
        if wanted is None:
            return None
        if active.provider == wanted:
            return active
        return None

    def _get_client(self, provider: LLMProvider) -> Optional[BaseLLMClient]:
        """Получить клиент по провайдеру (с учётом cooldown)"""
        if provider == LLMProvider.AUTO:
            # Выбираем первый доступный не на cooldown
            for p in self._fallback_order:
                if p in self._clients and self._clients[p].enabled and not self._is_provider_on_cooldown(p):
                    return self._clients[p]
            # Если все на cooldown — берём первый доступный всё равно
            for p in self._fallback_order:
                if p in self._clients and self._clients[p].enabled:
                    return self._clients[p]
            return None

        # Если конкретный провайдер на cooldown — предупреждаем, но всё равно даём
        if self._is_provider_on_cooldown(provider):
            logger.warning(f"LLM provider {provider.value} is on cooldown but explicitly requested")

        return self._clients.get(provider)

    def _get_fallback_clients(self, primary: LLMProvider) -> List[BaseLLMClient]:
        """Получить список клиентов для fallback"""
        clients = []
        for p in self._fallback_order:
            if p != primary and p in self._clients and self._clients[p].enabled:
                clients.append(self._clients[p])
        return clients

    def get_model_for_tier(self, tier: ModelTier, provider: LLMProvider = None) -> str:
        """
        Получить модель для указанного уровня (tier).

        Args:
            tier: Уровень модели (standard/premium)
            provider: Провайдер (если не указан, используется default)

        Returns:
            Название модели
        """
        if provider is None:
            provider = self.default_provider

        if tier not in self.TIER_MODELS:
            tier = ModelTier.STANDARD

        # LOCAL: фактические id из env, чтобы standard/premium могли быть
        # РАЗНЫМИ моделями на одном vLLM-endpoint (Qwen-32B / Qwen-72B).
        # premium дефолтит на standard — один размер, если второй не задан.
        if provider == LLMProvider.LOCAL:
            import os as _os
            std = _os.environ.get("LLM_LOCAL_MODEL", "") or "local"
            if tier == ModelTier.PREMIUM:
                return _os.environ.get("LLM_LOCAL_MODEL_PREMIUM", "") or std
            return std

        return self.TIER_MODELS[tier].get(provider, self.TIER_MODELS[ModelTier.STANDARD][provider])

    def set_context_for_caching(self, context: str):
        """
        Установить контекст для кеширования.
        Работает только для Gemini клиента.
        """
        gemini = self._clients.get(LLMProvider.GEMINI)
        if gemini and isinstance(gemini, GeminiClient):
            gemini.set_context_for_caching(context)

    def has_cached_context(self) -> bool:
        """True, если на Gemini-клиенте выставлен кешируемый контекст (транскрипт).

        Используется, чтобы не дублировать транскрипт в теле промпта: если он
        уже идёт префиксом CONTEXT (и кешируется), повторно слать его не нужно.
        """
        gemini = self._clients.get(LLMProvider.GEMINI)
        return bool(
            gemini
            and isinstance(gemini, GeminiClient)
            and getattr(gemini, "context_for_caching", None)
        )

    def clear_cache(self):
        """Очистить кешированный контекст"""
        gemini = self._clients.get(LLMProvider.GEMINI)
        if gemini and isinstance(gemini, GeminiClient):
            gemini.clear_cache()

    async def _maybe_personalize_system_prompt(
        self,
        system_prompt: Optional[str],
        *,
        personalize: bool,
        user_id: Optional[str],
    ) -> Optional[str]:
        """Если personalize=True и доступен профиль — добавить preamble.

        Без profile-store и без ошибок при недоступном слое — возвращаем
        исходный prompt. Это не должно ломать работу LLM при отсутствии
        UserProfileService (например, при cold-старте или в тестах).
        """
        if not personalize or not user_id:
            return system_prompt
        try:
            from backend.core.llm.system_prompt_builder import (
                build_personalization_preamble,
                merge_with_base,
            )
            from backend.memory.user_profiles import UserProfileService

            service = UserProfileService()
            await service.initialize()
            profile = await service.get_profile(user_id)
            if not profile:
                return system_prompt
            preamble = build_personalization_preamble(profile.to_dict())
            return merge_with_base(system_prompt, preamble)
        except Exception as exc:
            logger.debug("personalization skipped: %s", exc)
            return system_prompt

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        provider: LLMProvider = LLMProvider.AUTO,
        task_type: TaskType = TaskType.DEFAULT,
        model_tier: ModelTier = ModelTier.STANDARD,
        temperature: float = 0.7,
        max_tokens: int = 8192,
        personalize: bool = False,
        apply_dlp: Optional[bool] = None,
        **kwargs
    ) -> str:
        """
        Генерация текста с автоматическим fallback.

        Пользовательский override уровня (настройка «Модели»: Стандарт/
        Премиум = своя модель со своим ключом, tier_overrides) пробуется
        ПЕРВЫМ, если в контексте есть user_id (set_llm_context). Сбой/пусто →
        прежняя цепочка платформы, ничего не падает. Покрывает и прямые
        вызовы мимо workload_policy (узлы досок, переводы и т.д.).

        Args:
            prompt: Промпт
            system_prompt: Системный промпт
            provider: Провайдер LLM
            task_type: Тип задачи (для выбора оптимальной модели)
            model_tier: Уровень модели (standard/premium)
            temperature: Температура генерации
            max_tokens: Максимум токенов
            personalize: W7 — если True, обогатить system_prompt профилем
                пользователя из контекста (стиль/детализация/фокус). Default
                False, чтобы prompt-cache (Phase 4b) не сбивался.
        """
        if provider == LLMProvider.AUTO:
            provider = self.default_provider

        # Phase 4a: жёсткая проверка LLM-квоты до выбора клиента/модели.
        # QuotaExceeded поднимается выше и обрабатывается на API-слое.
        from backend.core.llm.quota import assert_within_quota
        ctx = get_llm_context()
        await assert_within_quota(user_id=ctx.get("user_id"), tenant_id=ctx.get("tenant_id"))

        # Phase 7 user-adaptive (W7): обогащаем system_prompt преамбулой по профилю.
        system_prompt = await self._maybe_personalize_system_prompt(
            system_prompt, personalize=personalize, user_id=ctx.get("user_id"),
        )

        # Override уровня пользователем («Модели»: Стандарт/Премиум = своя
        # модель со своим ключом). Пробуем ПЕРВЫМ; сбой/пусто → прежняя
        # цепочка платформы ниже. Never-raise.
        if ctx.get("user_id"):
            try:
                from backend.core.llm.tier_overrides import caller_for_level
                _lvl = "premium" if model_tier == ModelTier.PREMIUM else "standard"
                _oc, _op = caller_for_level(str(ctx["user_id"]), _lvl)
                if _oc is not None:
                    try:
                        _full = (f"{system_prompt}\n\n{prompt}"
                                 if system_prompt else prompt)
                        _out = await _oc.generate(
                            _full, temperature=temperature, max_tokens=max_tokens)
                        if _out and _out.strip():
                            return _out
                        logger.info("tier override %s/%s: пустой ответ — фолбэк платформы", _lvl, _op)
                    except Exception as _oe:
                        logger.warning("tier override %s/%s упал: %s — фолбэк платформы", _lvl, _op, _oe)
                    finally:
                        try:
                            await _oc.aclose()
                        except Exception:
                            pass
            except Exception:
                logger.debug("tier override hook skipped", exc_info=True)

            # Платформенная цепочка уровня (grok-4.5 → deepseek-4-pro →
            # flash-lite) — ДЕФОЛТ и для прямых вызовов роутера (узлы досок,
            # переводы), а не только для workload-путей. Порядок приоритетов:
            # ручной override из «Моделей» (выше) → эта цепочка → легаси-путь
            # ниже (gemini/openai клиенты) как последняя страховка. Сброс
            # настройки во вкладке «Модели» возвращает ровно сюда. Never-raise.
            try:
                from backend.core.llm.extraction_route import (
                    build_extraction_route,
                )
                _lvl2 = ("premium" if model_tier == ModelTier.PREMIUM
                         else "standard")
                _route = await build_extraction_route(
                    str(ctx["user_id"]), level_override=_lvl2)
                _pc = _route.get("caller")
                if _pc is not None:
                    try:
                        _full = (f"{system_prompt}\n\n{prompt}"
                                 if system_prompt else prompt)
                        _out = await _pc.generate(_full)
                        if _out and _out.strip():
                            logger.info("[LLM] platform chain: %s (%s, tier %s)",
                                        _route.get("model"),
                                        _route.get("native"), _lvl2)
                            return _out
                        logger.info("platform chain %s: пустой ответ — легаси-путь",
                                    _route.get("model"))
                    except Exception as _pe:
                        logger.warning("platform chain %s упал: %s — легаси-путь",
                                       _route.get("model"), _pe)
                    finally:
                        try:
                            await _pc.aclose()
                        except Exception:
                            pass
            except Exception:
                logger.debug("platform chain hook skipped", exc_info=True)

        client = self._get_client(provider)
        # W42b: если есть active LLMProfile (создан admin'ом через UI),
        # он имеет приоритет над env-based clients для совместимых providers.
        try:
            active_client = await self._maybe_get_active_profile_client(provider)
            if active_client is not None and active_client.enabled:
                client = active_client
        except Exception as exc:
            logger.debug("active profile resolver failed: %s", exc)

        if not client or not client.enabled:
            if self.enable_fallback:
                fallbacks = self._get_fallback_clients(provider)
                if fallbacks:
                    client = fallbacks[0]
                    logger.warning(f"[Fallback] Primary provider {provider} unavailable, using fallback")
                else:
                    raise RuntimeError("No LLM clients available")
            else:
                raise RuntimeError(f"Provider {provider} is not available")

        # Получаем модель по tier
        model = self.get_model_for_tier(model_tier, provider)
        logger.info(f"[LLM] Using model: {model} (tier: {model_tier.value if hasattr(model_tier, 'value') else model_tier})")

        # Phase 4b: exact-match cache для детерминированных вызовов (temperature == 0).
        # Для t > 0 кэш отключён — пользователь ждёт вариативности.
        from backend.core.llm.prompt_cache import get_cached_response, store_response
        cached = await get_cached_response(
            model=model, prompt=prompt, system_prompt=system_prompt, temperature=temperature,
        )
        if cached is not None:
            logger.info(f"[LLM] cache hit on {model}, saved a full LLM call")
            return _maybe_apply_dlp(cached, apply_dlp)

        try:
            result = await client.generate(
                prompt=prompt,
                system_prompt=system_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                model=model,
                **kwargs
            )
            self._record_provider_success(provider)
            await store_response(
                model=model, prompt=prompt, system_prompt=system_prompt,
                temperature=temperature, response=result,
            )
            return _maybe_apply_dlp(result, apply_dlp)
        except Exception as e:
            self._record_provider_error(provider)
            if self.enable_fallback:
                fallbacks = self._get_fallback_clients(provider)
                for fallback_client in fallbacks:
                    # Определяем провайдера fallback-клиента
                    fb_provider = None
                    for p, c in self._clients.items():
                        if c is fallback_client:
                            fb_provider = p
                            break
                    try:
                        logger.warning(f"LLM fallback after error: {str(e)[:80]}")
                        result = await fallback_client.generate(
                            prompt=prompt,
                            system_prompt=system_prompt,
                            temperature=temperature,
                            max_tokens=max_tokens,
                            **kwargs
                        )
                        if fb_provider:
                            self._record_provider_success(fb_provider)
                        return _maybe_apply_dlp(result, apply_dlp)
                    except Exception:
                        if fb_provider:
                            self._record_provider_error(fb_provider)
                        continue
            raise

    async def generate_json(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        provider: LLMProvider = LLMProvider.AUTO,
        task_type: TaskType = TaskType.DEFAULT,
        model_tier: ModelTier = ModelTier.STANDARD,
        temperature: float = 0.7,
        max_tokens: int = 8192,
        personalize: bool = False,
        apply_dlp: Optional[bool] = None,
        **kwargs
    ) -> Union[Dict[str, Any], List[Any]]:
        """
        Генерация JSON с автоматическим fallback.

        Args:
            prompt: Промпт
            system_prompt: Системный промпт
            provider: Провайдер LLM
            task_type: Тип задачи
            model_tier: Уровень модели (standard/premium)
            temperature: Температура генерации
            max_tokens: Максимум токенов
            personalize: W7 — обогатить system_prompt профилем пользователя.
                Default False (cache-stable).
        """
        if provider == LLMProvider.AUTO:
            provider = self.default_provider

        # Phase 4a: жёсткая проверка LLM-квоты до выбора клиента/модели.
        from backend.core.llm.quota import assert_within_quota
        ctx = get_llm_context()
        await assert_within_quota(user_id=ctx.get("user_id"), tenant_id=ctx.get("tenant_id"))

        # W7: personalization preamble.
        system_prompt = await self._maybe_personalize_system_prompt(
            system_prompt, personalize=personalize, user_id=ctx.get("user_id"),
        )

        client = self._get_client(provider)
        # W42b: если есть active LLMProfile (создан admin'ом через UI),
        # он имеет приоритет над env-based clients для совместимых providers.
        try:
            active_client = await self._maybe_get_active_profile_client(provider)
            if active_client is not None and active_client.enabled:
                client = active_client
        except Exception as exc:
            logger.debug("active profile resolver failed: %s", exc)

        if not client or not client.enabled:
            if self.enable_fallback:
                fallbacks = self._get_fallback_clients(provider)
                if fallbacks:
                    client = fallbacks[0]
                    logger.warning(f"[Fallback] Primary provider {provider} unavailable, using fallback (json)")
                else:
                    raise RuntimeError("No LLM clients available")
            else:
                raise RuntimeError(f"Provider {provider} is not available")

        # Получаем модель по tier
        model = self.get_model_for_tier(model_tier, provider)

        # Phase 4b: cache для детерминированных JSON-вызовов
        from backend.core.llm.prompt_cache import get_cached_response, store_response
        cache_key_prompt = f"json::{prompt}"
        cached_raw = await get_cached_response(
            model=model, prompt=cache_key_prompt, system_prompt=system_prompt, temperature=temperature,
        )
        if cached_raw is not None:
            logger.info(f"[LLM] cache hit (json) on {model}")
            import json as _json
            try:
                return _maybe_apply_dlp_json(_json.loads(cached_raw), apply_dlp)
            except (_json.JSONDecodeError, ValueError):
                logger.warning("prompt_cache: stored JSON corrupt, ignoring")

        # Повтор ТЕМ ЖЕ клиентом до фолбэка: транзиентная сетевая ошибка
        # или битый JSON не должны сразу ронять извлечение в чужого
        # провайдера/пустоту (раньше любой сбой = мгновенный fallback, а
        # для агентов capture — тихая потеря данных). Вторая попытка
        # усиливает требование валидного JSON.
        import os as _os
        try:
            _retries = max(0, int(_os.getenv("TESSENT_JSON_RETRIES", "1")))
        except (TypeError, ValueError):
            _retries = 1
        _last_exc: Optional[Exception] = None
        try:
            result = None
            for _attempt in range(1 + _retries):
                _p = prompt if _attempt == 0 else (
                    prompt + "\n\nВАЖНО: предыдущий ответ не распарсился. "
                    "Верни СТРОГО валидный JSON без пояснений и без "
                    "markdown-обёрток.")
                try:
                    result = await client.generate_json(
                        prompt=_p,
                        system_prompt=system_prompt,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        model=model,
                        **kwargs
                    )
                    _last_exc = None
                    break
                except Exception as _e:
                    _last_exc = _e
                    if _attempt < _retries:
                        logger.warning(
                            f"[LLM] json retry {_attempt + 1}/{_retries} "
                            f"same-provider after: {str(_e)[:80]}")
            if _last_exc is not None:
                raise _last_exc
            self._record_provider_success(provider)
            try:
                import json as _json
                await store_response(
                    model=model, prompt=cache_key_prompt, system_prompt=system_prompt,
                    temperature=temperature, response=_json.dumps(result, ensure_ascii=False),
                )
            except (TypeError, ValueError) as exc:
                logger.debug("prompt_cache: skipping non-serialisable JSON: %s", exc)
            return _maybe_apply_dlp_json(result, apply_dlp)
        except Exception as e:
            self._record_provider_error(provider)
            if self.enable_fallback:
                fallbacks = self._get_fallback_clients(provider)
                for fallback_client in fallbacks:
                    fb_provider = None
                    for p, c in self._clients.items():
                        if c is fallback_client:
                            fb_provider = p
                            break
                    try:
                        logger.warning(f"LLM JSON fallback after error: {str(e)[:80]}")
                        result = await fallback_client.generate_json(
                            prompt=prompt,
                            system_prompt=system_prompt,
                            temperature=temperature,
                            max_tokens=max_tokens,
                            **kwargs
                        )
                        if fb_provider:
                            self._record_provider_success(fb_provider)
                        return _maybe_apply_dlp_json(result, apply_dlp)
                    except Exception:
                        if fb_provider:
                            self._record_provider_error(fb_provider)
                        continue
            raise

    def get_stats(self) -> Dict[str, Any]:
        """Получить объединённую статистику по всем провайдерам"""
        stats = {
            "providers": {},
            "health": {},
            "total": {
                "requests": 0,
                "errors": 0,
                "estimated_cost": 0.0
            }
        }

        # Добавляем health info
        for provider, health in self._provider_health.items():
            cooldown_remaining = max(0, health.get("cooldown_until", 0) - time.time())
            stats["health"][provider.value] = {
                "error_count": health.get("error_count", 0),
                "on_cooldown": cooldown_remaining > 0,
                "cooldown_remaining_seconds": round(cooldown_remaining, 1)
            }

        for provider, client in self._clients.items():
            client_stats = client.get_stats()
            stats["providers"][provider.value] = client_stats
            stats["total"]["requests"] += client_stats["requests"]["total"]
            stats["total"]["errors"] += client_stats["requests"]["errors"]
            # Парсим стоимость из строки
            cost_str = client_stats["cost"]["actual"]
            cost = float(cost_str.replace("$", ""))
            stats["total"]["estimated_cost"] += cost

        stats["total"]["estimated_cost"] = f"${stats['total']['estimated_cost']:.6f}"

        return stats

    def reset_stats(self):
        """Сбросить статистику всех клиентов"""
        for client in self._clients.values():
            client.reset_stats()


# Singleton для глобального использования
_global_router: Optional[LLMRouter] = None


def get_llm_router() -> LLMRouter:
    """Получить глобальный экземпляр LLMRouter"""
    global _global_router
    if _global_router is None:
        _global_router = LLMRouter()
    return _global_router


def reset_global_router():
    """Сбросить глобальный роутер"""
    global _global_router
    _global_router = None



