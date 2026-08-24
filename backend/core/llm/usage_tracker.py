# -*- coding: utf-8 -*-
"""
LLM Usage Tracker - Система учёта токенов и расходов.

Отслеживает использование токенов для всех LLM вызовов:
- GeminiClient
- OpenAI Client
- Agent chats
- Any other LLM calls

Хранит данные локально в SQLite и опционально в Supabase.
"""
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from backend.core.llm.usage_stores import UsageStore

logger = logging.getLogger(__name__)


class ModelTier(str, Enum):
    """Уровень модели"""
    STANDARD = "standard"
    PREMIUM = "premium"


class LLMProvider(str, Enum):
    """Провайдер LLM"""
    GEMINI = "gemini"
    OPENAI = "openai"
    LOCAL = "local"


@dataclass
class UsageRecord:
    """Запись об использовании LLM"""
    id: Optional[int] = None
    timestamp: str = ""
    provider: str = ""
    model: str = ""
    model_tier: str = "standard"

    # Токены
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0

    # Стоимость в USD
    input_cost: float = 0.0
    output_cost: float = 0.0
    cached_cost: float = 0.0
    total_cost: float = 0.0

    # Контекст
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    agent_mode: Optional[str] = None  # brain, mark, automation, transcripts
    request_type: Optional[str] = None  # chat, search, synthesis, etc.
    tenant_id: Optional[str] = None  # Phase 7: для per-tenant квот и аналитики

    # Атрибуция «по сущности» (issue #110, см. docs/LLM_COST_ATTRIBUTION.md):
    # — document_id/meeting_id отвечают на «сколько стоил прогон документа X».
    # — job_id отвечает на «сколько стоила фоновая задача Y» (массовый ingest).
    # — surface — высокоуровневая категория поверх agent_mode
    #   (chat|agent|ingest|ontology|digest|coach|sleep|other) для отчёта
    #   «по способу использования».
    document_id: Optional[str] = None
    meeting_id: Optional[str] = None
    job_id: Optional[str] = None
    surface: Optional[str] = None

    # Метаданные
    success: bool = True
    error: Optional[str] = None
    latency_ms: int = 0


# Цены за 1M токенов (USD)
MODEL_PRICING = {
    # Local models (no direct token cost)
    "functiongemma": {
        "input": 0.0,
        "cached": 0.0,
        "output": 0.0,
    },
    "ollama/functiongemma": {
        "input": 0.0,
        "cached": 0.0,
        "output": 0.0,
    },
    # Gemini models (актуальные цены 2.5-поколения; alias *-latest
    # указывает на 2.5: flash-lite $0.10/$0.40, flash $0.30/$2.50)
    "gemini-flash-lite-latest": {
        "input": 0.10,
        "cached": 0.025,
        "output": 0.40,
    },
    # Anthropic (для бенча тиринга и BYO-ключа; cached ≈ input×0.1)
    "claude-opus-4-8": {
        "input": 5.00,
        "cached": 0.50,
        "output": 25.00,
    },
    "claude-sonnet-5": {
        "input": 3.00,
        "cached": 0.30,
        "output": 15.00,
    },
    "claude-fable-5": {
        "input": 10.00,
        "cached": 1.00,
        "output": 50.00,
    },
    # Qwen self-host: денежная marginal-стоимость ≈ 0 (платишь GPU/временем)
    "qwen-27b-local": {
        "input": 0.0,
        "cached": 0.0,
        "output": 0.0,
    },
    "gemini-2.5-flash-lite": {
        "input": 0.10,
        "cached": 0.025,
        "output": 0.40,
    },
    "gemini-flash-latest": {
        "input": 0.30,
        "cached": 0.075,  # 75% discount
        "output": 2.50,
    },
    "gemini-2.5-pro": {
        "input": 1.25,
        "cached": 0.3125,
        "output": 10.00,
    },
    "gemini-1.5-pro": {
        "input": 1.25,
        "cached": 0.3125,
        "output": 5.00,
    },
    "gemini-2.5-flash-preview-05-20": {
        "input": 0.15,
        "cached": 0.0375,
        "output": 0.60,
    },
    "gemini-2.5-flash": {
        "input": 0.30,
        "cached": 0.075,
        "output": 2.50,
    },
    "gemini-3-pro-preview": {
        "input": 1.25,
        "cached": 0.3125,
        "output": 5.00,
    },
    # OpenAI models
    "gpt-4o": {
        "input": 2.50,
        "cached": 1.25,
        "output": 10.00,
    },
    "gpt-4o-mini": {
        "input": 0.15,
        "cached": 0.075,
        "output": 0.60,
    },
    "gpt-4.1-mini": {
        "input": 0.40,
        "cached": 0.10,
        "output": 1.60,
    },
    "gpt-4.1-nano": {
        "input": 0.10,
        "cached": 0.025,
        "output": 0.40,
    },
    # Gemini 3 поколение: alias *-latest у пользователя может указывать УЖЕ
    # на 3.0 — до калибровки берём цены 2.5-уровня, точные значения задаются
    # локально в config/model_pricing.json (см. ниже) по данным консоли Google.
    "gemini-3-flash-lite": {
        "input": 0.10,
        "cached": 0.025,
        "output": 0.40,
    },
    "gemini-3-flash": {
        "input": 0.30,
        "cached": 0.075,
        "output": 2.50,
    },
    # Default fallback
    "default": {
        "input": 0.50,
        "cached": 0.125,
        "output": 2.00,
    }
}

# КАЛИБРОВКА ЦЕН БЕЗ РЕЛИЗА: config/model_pricing.json перекрывает таблицу —
# {"gemini-flash-lite-latest": {"input": 0.2, "output": 0.8}}. Цены Google
# меняются, а alias *-latest молча переезжает на новое поколение (2.5 → 3.0);
# сверяйте $ трекера с консолью биллинга и правьте файл.
try:
    import json as _json
    _pf = Path("config") / "model_pricing.json"
    if _pf.exists():
        for _m, _p in (_json.loads(_pf.read_text(encoding="utf-8")) or {}).items():
            if isinstance(_p, dict):
                MODEL_PRICING[_m] = {
                    **MODEL_PRICING.get(_m, MODEL_PRICING["default"]), **_p}
        logger.info(f"[UsageTracker] pricing overrides loaded: {_pf}")
except Exception:
    logger.debug("pricing overrides load failed", exc_info=True)

# Цены на генерацию изображений (USD за изображение)
IMAGE_GENERATION_PRICING = {
    # Google Gemini Image models
    "gemini-2.5-flash-image": {
        "per_image": 0.02,  # ~$0.02 per image
        "description": "Gemini 2.5 Flash Image Generation"
    },
    "gemini-3-pro-image-preview": {
        "per_image": 0.05,  # ~$0.05 per image (premium)
        "description": "Gemini 3 Pro Image Generation"
    },
    # OpenAI DALL-E and GPT Image models
    "dall-e-3": {
        "per_image": 0.04,  # $0.04 for 1024x1024, $0.08 for 1024x1792
        "hd_multiplier": 2.0,
        "description": "DALL-E 3"
    },
    "gpt-image-1": {
        "per_image": 0.011,  # $0.011 for low, up to $0.17 for high
        "quality_pricing": {
            "low": 0.011,
            "medium": 0.042,
            "high": 0.167
        },
        "description": "GPT Image 1"
    },
    "gpt-image-1.5": {
        "per_image": 0.02,  # estimated
        "quality_pricing": {
            "low": 0.015,
            "medium": 0.05,
            "high": 0.20
        },
        "description": "GPT Image 1.5"
    },
    "gpt-image-1-mini": {
        "per_image": 0.005,  # cheaper mini version
        "description": "GPT Image 1 Mini"
    },
    # Default for unknown image models
    "default_image": {
        "per_image": 0.03,
        "description": "Default Image Model"
    }
}

# Shadow-bill (Kanon Level 3, docs/SIMA_KANON.md): стабильная reference-цена
# за 1M токенов, чтобы сравнивать прогоны на РАЗНЫХ провайдерах в одной
# валюте («сколько это стоило бы на reference-модели»). Бесплатные пути
# (claude_cli по подписке, ollama) получают ненулевой equivalent — честная
# видимость затрат. Считается на лету из токенов, в БД не пишется.
SHADOW_PRICING = {"input": 1.0, "cached": 0.1, "output": 5.0}


def shadow_cost(input_tokens: int, output_tokens: int,
                cached_tokens: int = 0) -> float:
    """Эквивалентная стоимость по reference-цене (provider-независимая)."""
    return round(
        (input_tokens / 1_000_000) * SHADOW_PRICING["input"]
        + (output_tokens / 1_000_000) * SHADOW_PRICING["output"]
        + (cached_tokens / 1_000_000) * SHADOW_PRICING["cached"], 6)


class UsageTracker:
    """
    Трекер использования LLM.

    Хранит данные в локальной SQLite базе и опционально синхронизирует с Supabase.
    """

    _instance: Optional["UsageTracker"] = None

    def __init__(self, db_path: Optional[str] = None,
                 store: Optional["UsageStore"] = None):
        """
        Args:
            db_path: Путь к SQLite базе данных (legacy, до П4). Если задан
                и store не задан — создастся SqliteUsageStore по этому пути.
            store: Готовый UsageStore (DI). issue #110 пакет 4: позволяет
                подменить backend на PostgresUsageStore или mock в тестах.

        Backward-compat: self.db_path всё ещё выставляется для совместимости
        с прямыми sqlite3.connect(tracker.db_path) вызовами (есть в quota.py
        и attribution.py до П4.a). После полной миграции эти места читают
        через self.store, а db_path становится опциональным.
        """
        from backend.core.llm.usage_stores import (
            SqliteUsageStore,
            UsageStore,
            make_default_store,
        )
        if store is not None:
            self.store: UsageStore = store
            # db_path выставим, если store — SQLite (для backward-compat)
            self.db_path = getattr(store, "db_path", None) or ""
        elif db_path is not None:
            self.store = SqliteUsageStore(db_path)
            self.store.init_schema()
            self.db_path = db_path
        else:
            self.store = make_default_store()
            self.db_path = getattr(self.store, "db_path", "") or ""

        # Статистика текущей сессии (in-memory)
        self._session_stats = {
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cached_tokens": 0,
            "total_cost": 0.0,
            "total_cost_equivalent": 0.0,   # shadow-bill (reference-цена)
            "requests_count": 0,
            "errors_count": 0,
        }

        logger.info(f"[UsageTracker] Initialized: {db_path}")

    @classmethod
    def get_instance(cls) -> "UsageTracker":
        """Получить singleton экземпляр"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _init_db(self):
        """Backward-compat shim — schema создаёт self.store.init_schema()
        в __init__. Оставлен на случай прямого вызова из легаси-кода."""
        self.store.init_schema()

    def calculate_cost(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0
    ) -> Dict[str, float]:
        """
        Рассчитать стоимость запроса.

        Args:
            model: Название модели
            input_tokens: Количество входных токенов
            output_tokens: Количество выходных токенов
            cached_tokens: Количество кэшированных токенов

        Returns:
            Dict с input_cost, output_cost, cached_cost, total_cost
        """
        pricing = MODEL_PRICING.get(model, MODEL_PRICING["default"])

        # Стоимость = (токены / 1_000_000) * цена_за_миллион
        input_cost = (input_tokens / 1_000_000) * pricing["input"]
        output_cost = (output_tokens / 1_000_000) * pricing["output"]
        cached_cost = (cached_tokens / 1_000_000) * pricing["cached"]

        return {
            "input_cost": round(input_cost, 6),
            "output_cost": round(output_cost, 6),
            "cached_cost": round(cached_cost, 6),
            "total_cost": round(input_cost + output_cost + cached_cost, 6),
        }

    def track(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
        model_tier: str = "standard",
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        agent_mode: Optional[str] = None,
        request_type: Optional[str] = None,
        tenant_id: Optional[str] = None,
        document_id: Optional[str] = None,
        meeting_id: Optional[str] = None,
        job_id: Optional[str] = None,
        surface: Optional[str] = None,
        success: bool = True,
        error: Optional[str] = None,
        latency_ms: int = 0,
    ) -> UsageRecord:
        """
        Записать использование LLM.

        Args:
            provider: Провайдер (gemini, openai, local)
            model: Название модели
            input_tokens: Входные токены
            output_tokens: Выходные токены
            cached_tokens: Кэшированные токены
            model_tier: Уровень модели (standard, premium)
            user_id: ID пользователя
            session_id: ID сессии
            agent_mode: Режим агента (brain, mark, automation, transcripts)
            request_type: Тип запроса (chat, search, synthesis)
            success: Успешность запроса
            error: Текст ошибки
            latency_ms: Время выполнения в мс

        Returns:
            UsageRecord с заполненными данными
        """
        # Рассчитываем стоимость
        costs = self.calculate_cost(model, input_tokens, output_tokens, cached_tokens)

        # Если tenant не передан явно — подтягиваем из контекста.
        if tenant_id is None:
            try:
                from backend.core.observability.tenant_context import get_current_tenant
                tenant_id = get_current_tenant()
            except Exception:
                tenant_id = None

        # issue #110: атрибуция по сущности. Если вызывающий не передал
        # явно — подтягиваем из usage_context, который ставит cost_scope() /
        # UsageContext. Контекст-менеджеры можно вкладывать.
        # Раньше из контекста подхватывались ТОЛЬКО entity-id/surface, а
        # user_id/agent_mode/request_type/session_id — нет: поэтому
        # cost_scope(user_id=...) молча терялся для прямых track_usage-вызовов
        # (документогенерация, шаблоны, openai-вызовы автоматизаций писались
        # с user_id=NULL → 'unknown' в срезах). Теперь тянем и их.
        if any(v is None for v in (document_id, meeting_id, job_id, surface,
                                   user_id, agent_mode, request_type, session_id)):
            ctx = get_usage_context() or {}
            document_id = document_id or ctx.get("document_id")
            meeting_id = meeting_id or ctx.get("meeting_id")
            job_id = job_id or ctx.get("job_id")
            surface = surface or ctx.get("surface")
            user_id = user_id or ctx.get("user_id")
            agent_mode = agent_mode or ctx.get("agent_mode")
            request_type = request_type or ctx.get("request_type")
            session_id = session_id or ctx.get("session_id")

        record = UsageRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            provider=provider,
            model=model,
            model_tier=model_tier,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            total_tokens=input_tokens + output_tokens + cached_tokens,
            input_cost=costs["input_cost"],
            output_cost=costs["output_cost"],
            cached_cost=costs["cached_cost"],
            total_cost=costs["total_cost"],
            user_id=user_id,
            session_id=session_id,
            agent_mode=agent_mode,
            request_type=request_type,
            tenant_id=tenant_id,
            document_id=document_id,
            meeting_id=meeting_id,
            job_id=job_id,
            surface=surface,
            success=success,
            error=error,
            latency_ms=latency_ms,
        )

        # Сохраняем в базу
        self._save_record(record)

        # Трата записана в основной учёт — резерв под этот вызов больше не
        # нужен, снимаем его (закрытие гонки: quota_reservation). Best-effort,
        # никогда не роняем track из-за телеметрии квоты.
        try:
            from backend.core.llm import quota_reservation
            quota_reservation.schedule_release()
        except Exception:
            logger.debug("quota reservation release skipped", exc_info=True)

        # Обновляем in-memory статистику
        self._session_stats["total_input_tokens"] += input_tokens
        self._session_stats["total_output_tokens"] += output_tokens
        self._session_stats["total_cached_tokens"] += cached_tokens
        self._session_stats["total_cost"] += costs["total_cost"]
        self._session_stats["total_cost_equivalent"] += shadow_cost(
            input_tokens, output_tokens, cached_tokens)
        self._session_stats["requests_count"] += 1
        if not success:
            self._session_stats["errors_count"] += 1

        logger.info(
            f"[Usage] Tracked: {model} | "
            f"in={input_tokens}, out={output_tokens}, cached={cached_tokens} | "
            f"cost=${costs['total_cost']:.6f} | "
            f"feat={agent_mode or '?'}/{request_type or '?'} user={user_id or '?'}"
        )

        # Wow-screen real-time pulse (frontend HomeTab подписан на /ws/signals).
        # Fire-and-forget — synchronous, не блокирует track(). Если WS-инфры
        # нет (например в тестах) — событие просто drop'нется без шума.
        try:
            from backend.api.websocket.signals_ws import publish_signal
            publish_signal(
                "usage_tracked",
                {
                    "model": model,
                    "provider": provider,
                    "cost": round(costs["total_cost"], 6),
                    "total_tokens": input_tokens + output_tokens + cached_tokens,
                    "meeting_id": meeting_id,
                    "document_id": document_id,
                    "job_id": job_id,
                    "surface": surface,
                    "at": record.timestamp,
                },
                tenant_id=tenant_id,
            )
        except Exception:
            # Никогда не падаем из-за публикации события — telemetry stays out
            # of the critical path.
            pass

        return record

    def calculate_image_cost(
        self,
        model: str,
        quality: str = "medium",
        size: str = "1024x1024"
    ) -> float:
        """
        Рассчитать стоимость генерации изображения.

        Args:
            model: Название модели генерации изображений
            quality: Качество (low, medium, high)
            size: Размер изображения

        Returns:
            Стоимость в USD
        """
        pricing = IMAGE_GENERATION_PRICING.get(model, IMAGE_GENERATION_PRICING["default_image"])

        # Если есть ценообразование по качеству
        if "quality_pricing" in pricing:
            cost = pricing["quality_pricing"].get(quality, pricing["per_image"])
        else:
            cost = pricing["per_image"]

        # HD множитель для DALL-E
        if "hd_multiplier" in pricing and quality == "hd":
            cost *= pricing["hd_multiplier"]

        # Множитель размера (большие изображения дороже)
        if size and "x" in size:
            try:
                w, h = map(int, size.split("x"))
                if w > 1024 or h > 1024:
                    cost *= 1.5  # 50% дороже для больших изображений
            except Exception:
                logger.debug("suppressed exception", exc_info=True)

        return round(cost, 6)

    def track_image_generation(
        self,
        provider: str,
        model: str,
        quality: str = "medium",
        size: str = "1024x1024",
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        success: bool = True,
        error: Optional[str] = None,
        latency_ms: int = 0,
        prompt_length: int = 0,
    ) -> UsageRecord:
        """
        Записать использование генерации изображений.

        Args:
            provider: Провайдер (google, openai)
            model: Название модели (dall-e-3, gpt-image-1, gemini-2.5-flash-image, etc.)
            quality: Качество изображения (low, medium, high)
            size: Размер изображения
            user_id: ID пользователя
            session_id: ID сессии
            success: Успешность генерации
            error: Текст ошибки
            latency_ms: Время выполнения в мс
            prompt_length: Длина промпта в символах

        Returns:
            UsageRecord с заполненными данными
        """
        cost = self.calculate_image_cost(model, quality, size)

        # Для изображений используем prompt_length как "input_tokens" для статистики
        # и 1 как "output_tokens" (1 изображение)
        record = UsageRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            provider=provider,
            model=model,
            model_tier="standard",  # Для изображений нет tier
            input_tokens=prompt_length,  # Длина промпта
            output_tokens=1,  # 1 изображение
            cached_tokens=0,
            total_tokens=prompt_length + 1,
            input_cost=0.0,
            output_cost=cost,  # Вся стоимость в output
            cached_cost=0.0,
            total_cost=cost,
            user_id=user_id,
            session_id=session_id,
            agent_mode="board",  # Всегда board для генерации изображений
            request_type="image_generation",
            success=success,
            error=error,
            latency_ms=latency_ms,
        )

        # Сохраняем в базу
        self._save_record(record)

        # Обновляем in-memory статистику
        self._session_stats["total_cost"] += cost
        self._session_stats["requests_count"] += 1
        if not success:
            self._session_stats["errors_count"] += 1

        logger.info(
            f"[Usage] Image tracked: {model} | "
            f"quality={quality}, size={size} | "
            f"cost=${cost:.6f}"
        )

        return record

    def _save_record(self, record: UsageRecord):
        """issue #110 пакет 4: делегат в UsageStore (SQLite/Postgres)."""
        new_id = self.store.save(record)
        if new_id is not None:
            record.id = new_id

    def get_session_stats(self) -> Dict[str, Any]:
        """Получить статистику текущей сессии"""
        return {
            **self._session_stats,
            "total_cost_formatted": f"${self._session_stats['total_cost']:.4f}",
            "total_cost_equivalent_formatted":
                f"${self._session_stats['total_cost_equivalent']:.4f}",
        }

    def get_daily_stats(
        self,
        date: Optional[str] = None,
        *,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Статистика за день (делегат в UsageStore, issue #110).

        Args:
            date: YYYY-MM-DD (по умолчанию сегодня UTC).
            tenant_id: P2 #11 — явный фильтр по org (admin override). Без
                него store фильтрует по tenant_context (issue #112 RLS) и
                добавляет by_tenant breakdown для admin-обзора.
        """
        if date is None:
            date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        return self.store.daily_aggregate(date, tenant_id=tenant_id)

    def get_total_stats(self) -> Dict[str, Any]:
        """issue #110 пакет 4: делегат в UsageStore."""
        return self.store.total_aggregate()

    def get_recent_usage(self, limit: int = 50) -> List[Dict[str, Any]]:
        """issue #110 пакет 4: делегат в UsageStore."""
        return self.store.recent(limit)


# Глобальный экземпляр
_tracker: Optional[UsageTracker] = None


def get_usage_tracker() -> UsageTracker:
    """Получить глобальный экземпляр трекера"""
    global _tracker
    if _tracker is None:
        _tracker = UsageTracker()
    return _tracker



def estimate_cost(model: str, input_tokens: int, output_tokens: int,
                  cached_tokens: int = 0) -> float:
    """Оценка стоимости в $ по MODEL_PRICING (для мест, где нет трекера —
    напр. стоимость запуска агентной автоматизации)."""
    pricing = MODEL_PRICING.get(model, MODEL_PRICING["default"])
    return round(
        (input_tokens / 1_000_000) * pricing["input"]
        + (output_tokens / 1_000_000) * pricing["output"]
        + (cached_tokens / 1_000_000) * pricing.get("cached", 0),
        6)


def track_usage(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    **kwargs
) -> UsageRecord:
    """
    Удобная функция для трекинга использования.

    Пример:
    ```python
    from backend.core.llm.usage_tracker import track_usage

    track_usage(
        provider="gemini",
        model="gemini-flash-lite-latest",
        input_tokens=1000,
        output_tokens=500,
        agent_mode="brain",
        request_type="chat"
    )
    ```
    """
    try:
        tracker = get_usage_tracker()
        return tracker.track(provider, model, input_tokens, output_tokens, **kwargs)
    except Exception as e:
        # Usage telemetry should never break core user flows.
        logger.error(f"Failed to track usage (non-fatal): {e}")
        return UsageRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=max(0, int(input_tokens) + int(output_tokens)),
            success=False,
            error=str(e),
            model_tier=str(kwargs.get("model_tier") or "standard"),
            user_id=kwargs.get("user_id"),
            session_id=kwargs.get("session_id"),
            agent_mode=kwargs.get("agent_mode"),
            request_type=kwargs.get("request_type"),
            latency_ms=int(kwargs.get("latency_ms") or 0),
        )


# Context variables для глобального контекста трекинга
import contextvars

_usage_context: contextvars.ContextVar[Dict[str, Any]] = contextvars.ContextVar(
    'usage_context',
    default={}
)


class UsageContext:
    """
    Контекстный менеджер для установки контекста трекинга.

    Пример:
    ```python
    from backend.core.llm.usage_tracker import UsageContext

    async with UsageContext(agent_mode="sync", request_type="knowledge_extraction", model_tier="premium"):
        # Все LLM вызовы внутри будут иметь этот контекст и использовать премиум модель
        await llm.generate(...)
    ```
    """

    def __init__(
        self,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        agent_mode: Optional[str] = None,
        request_type: Optional[str] = None,
        model_tier: Optional[str] = None,  # "standard" или "premium"
        document_id: Optional[str] = None,
        meeting_id: Optional[str] = None,
        job_id: Optional[str] = None,
        surface: Optional[str] = None,
    ):
        # Наследуем уже установленный контекст (cost_scope может быть
        # вложенным: meeting → document_chunk; внутренний scope не должен
        # терять meeting_id, поставленный снаружи).
        parent = _usage_context.get() or {}
        self.context: Dict[str, Any] = dict(parent)
        if user_id:
            self.context["user_id"] = user_id
        if session_id:
            self.context["session_id"] = session_id
        if agent_mode:
            self.context["agent_mode"] = agent_mode
        if request_type:
            self.context["request_type"] = request_type
        if model_tier:
            self.context["model_tier"] = model_tier
        if document_id:
            self.context["document_id"] = document_id
        if meeting_id:
            self.context["meeting_id"] = meeting_id
        if job_id:
            self.context["job_id"] = job_id
        if surface:
            self.context["surface"] = surface
        self._token = None

    def __enter__(self):
        self._token = _usage_context.set(self.context)
        return self

    def __exit__(self, *args):
        if self._token:
            _usage_context.reset(self._token)

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, *args):
        return self.__exit__(*args)


def get_usage_context() -> Dict[str, Any]:
    """Получить текущий контекст трекинга"""
    return _usage_context.get()


def cost_scope(
    *,
    document_id: Optional[str] = None,
    meeting_id: Optional[str] = None,
    job_id: Optional[str] = None,
    surface: Optional[str] = None,
    agent_mode: Optional[str] = None,
    request_type: Optional[str] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> "UsageContext":
    """Удобный sync/async-контекст для атрибуции затрат LLM по сущности.

    issue #110. Используется на корневой точке прогона — например, прямо
    перед запуском пайплайна обработки встречи/документа/job:

    ```python
    from backend.core.llm.usage_tracker import cost_scope

    async with cost_scope(meeting_id=meeting.id, surface="ingest"):
        await pipeline.run(meeting)  # все вложенные LLM-вызовы
                                     # атрибутируются на meeting.id
    ```

    Любой `track_usage()` внутри этого скоупа автоматически получит
    эти поля (через usage_context-fallback в `UsageTracker.track`).
    Скоупы можно вкладывать — наследование контекста встроено в UsageContext.
    """
    return UsageContext(
        document_id=document_id,
        meeting_id=meeting_id,
        job_id=job_id,
        surface=surface,
        agent_mode=agent_mode,
        request_type=request_type,
        user_id=user_id,
        session_id=session_id,
    )


def track_image_usage(
    provider: str,
    model: str,
    quality: str = "medium",
    size: str = "1024x1024",
    **kwargs
) -> UsageRecord:
    """
    Удобная функция для трекинга генерации изображений.

    Пример:
    ```python
    from backend.core.llm.usage_tracker import track_image_usage

    track_image_usage(
        provider="openai",
        model="dall-e-3",
        quality="high",
        size="1024x1024",
        latency_ms=5000,
        prompt_length=100
    )
    ```
    """
    tracker = get_usage_tracker()
    return tracker.track_image_generation(provider, model, quality, size, **kwargs)

