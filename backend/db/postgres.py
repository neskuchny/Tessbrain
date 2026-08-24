"""
TESSENT BRAIN - PostgreSQL Client
Асинхронный клиент для PostgreSQL с SQLAlchemy
"""
import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Awaitable, Callable, TypeVar

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from backend.config import settings

logger = structlog.get_logger(__name__)

T = TypeVar("T")

# Маркеры обрыва соединения (Docker/WSL2 NAT рвёт сокет → WinError 64,
# «server closed», reset). На таких ошибках безопасно повторить read-операцию,
# которая сама открывает свежую сессию (pool_pre_ping выдаст живой коннект).
_RETRYABLE_CONN_MARKERS = (
    "winerror", "сетевое имя", "server closed", "connection is closed",
    "connection reset", "reset by peer", "broken pipe",
    "cannot perform operation", "connectiondoeexist", "connectiondoesnotexist",
    "operationalerror", "interfaceerror", "could not receive data",
    "terminating connection", "eof detected", "connection refused",
    "unreachable", "timeout",
)
# NB: НЕ включаем общий "is closed"/"network" — "This result object is closed"
# и т.п. это НЕ обрыв коннекта; иначе ретраили зря и репортили как outage.


def is_retryable_conn_error(exc: BaseException) -> bool:
    """True, если ошибка похожа на транзиентный обрыв соединения с Postgres."""
    msg = str(exc).lower()
    return any(m in msg for m in _RETRYABLE_CONN_MARKERS)


async def run_with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    retries: int = 2,
    base_delay: float = 0.15,
) -> T:
    """Выполнить async-операцию с ретраем ТОЛЬКО на обрыв соединения.

    Предназначено для идемпотентных read-операций, которые сами открывают
    `pg.session()`: на новой попытке пул отдаёт свежий коннект, и единичный
    обрыв (WinError 64 и т.п.) не доходит до вызывающего кода. Не-сетевые
    ошибки (синтаксис, отсутствие таблицы, RLS) пробрасываются сразу.

    НЕ оборачивать write-блоки целиком — частичная запись при ретрае.
    """
    last: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001 — классифицируем ниже
            if attempt >= retries or not is_retryable_conn_error(exc):
                raise
            last = exc
            await asyncio.sleep(base_delay * (2 ** attempt))
    assert last is not None
    raise last


class Base(DeclarativeBase):
    """Базовый класс для всех моделей SQLAlchemy"""
    pass


class PostgresClient:
    """Асинхронный клиент PostgreSQL"""

    def __init__(self):
        self.engine = create_async_engine(
            settings.db_url,
            echo=settings.debug,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
            # Docker Desktop / WSL2 NAT тихо рвёт долгоживущие idle-соединения
            # (на хосте это всплывает как WinError 64 «сетевое имя недоступно»).
            # pre_ping ловит дохлый коннект при выдаче, а recycle проактивно
            # пересоздаёт соединения старше 3 минут — до того как их убьёт NAT.
            pool_recycle=180,
        )
        self.async_session = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        self._initialized = False

    async def initialize(self) -> bool:
        """Инициализация подключения и создание таблиц"""
        try:
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            self._initialized = True
            logger.info("✅ PostgreSQL initialized", url=settings.db_url[:50] + "...")
            return True
        except Exception as e:
            logger.error("❌ PostgreSQL initialization failed", error=str(e))
            return False

    @asynccontextmanager
    async def session(
        self,
        *,
        apply_tenant: bool = True,
    ) -> AsyncGenerator[AsyncSession, None]:
        """Контекстный менеджер для сессии.

        По умолчанию применяет `SET LOCAL app.tenant_id` через
        `set_tenant_for_session()` сразу после открытия — это запитывает
        Postgres RLS-политику `tenant_isolation` (см.
        `backend/db/migrations/020_multitenant_rls.sql`).

        Если в `core.observability.tenant_context` нет tenant'а — no-op,
        и пользователь увидит только `tenant_id IS NULL` (legacy) строки.

        Передайте `apply_tenant=False` для админских/cross-tenant операций
        (бэкап-скрипт, данные-migration job, etc.).
        """
        async with self.async_session() as session:
            try:
                if apply_tenant:
                    # Lazy import: postgres_tenant.py сам импортирует sqlalchemy
                    # и observability — оба уже подгружены к этому моменту.
                    from backend.db.postgres_tenant import set_tenant_for_session
                    await set_tenant_for_session(session)
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def health_check(self) -> bool:
        """Проверка здоровья подключения"""
        try:
            # apply_tenant=False — health-check не должен зависеть от tenant'а;
            # SELECT 1 не задевает tenant-scoped таблицы.
            async with self.session(apply_tenant=False) as session:
                # SQLAlchemy 2.x requires explicit text() for raw SQL strings;
                # passing a bare "SELECT 1" raises ArgumentError and health_check
                # silently returns False, making /readyz misreport postgres.
                await session.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    async def close(self):
        """Закрытие подключения"""
        await self.engine.dispose()
        logger.info("PostgreSQL connection closed")


# Singleton instance
_postgres_client: PostgresClient | None = None


async def get_postgres() -> PostgresClient:
    """Получить клиент PostgreSQL"""
    global _postgres_client
    if _postgres_client is None:
        _postgres_client = PostgresClient()
        await _postgres_client.initialize()
    return _postgres_client



